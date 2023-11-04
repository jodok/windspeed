import argparse
import datetime
import hashlib
import os
import re
import requests
import secrets
import sys

from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ZAMG
# DD Windrichtung der letzten 10 Minuten
# FFAM Arithmetisches Mittel der Windgeschwindigkeit
# FFX Windspitze
# P Luftdruck
# RFAM Relative Feuchte arithmetisches Mittel
# RR Niederschlag der letzten 10 Minuten
# TL Lufttemperatur
# "lat":47.49722222222222,"lon":9.63,"altitude":395.0

load_dotenv()

stations = {
    "rohrspitz": {
        "url": "https://www.kite-connection.at/weatherstation/aktuell.htm",
        "password": os.getenv('WINDSPEED_PASS_ROHRSPITZ'),
    },
    "rohrspitz-zamg": {
        "url": "https://dataset.api.hub.geosphere.at/v1/station/current/tawes-v1-10min?station_ids=11299&parameters=DD,FFAM,FFX,P,RFAM,RR,TL",
        "password": os.getenv('WINDSPEED_PASS_ROHRSPITZ_ZAMG'),
    },
    "kressbronn": {
        "url": "https://www.wetter-kressbronn.de/wetter/aktuell.htm",
        "password": os.getenv('WINDSPEED_PASS_KRESSBRONN'),

    },
}


def extract_value(s):
    s = s.replace(",", ".")  # Replace comma with dot
    s = s.split(" ")[0]  # Remove unit
    return float(s)


def extract_kmh(s):
    pattern = r"([\d,]+)\s*km/h\s*\((\d+)\s*Bft\)"
    match = re.search(pattern, s)
    return float(match.group(1).replace(",", "."))


def extract_kts(s):
    pattern = r"([\d,]+)\s*kts\s*\((\d+)\s*Bft\)"
    match = re.search(pattern, s)
    return float(match.group(1).replace(",", "."))


def crawl_data(station):
    url = stations[station]["url"]
    response = requests.get(url)

    latest = {}

    if station == "rohrspitz" or station == "kressbronn":
        soup = BeautifulSoup(response.text, "html.parser")

        table = soup.find("table", attrs={"border": "1"})
        rows = table.find_all("tr")

        data = []
        for row in rows[1:]:
            cols = row.find_all("td")

            if station == "rohrspitz":
                if len(cols) == 12:
                    date_str = cols[0].text.strip()
                    time_str = cols[1].text.strip()
                    temperature_str = cols[2].text.strip()
                    humidity_str = cols[3].text.strip()
                    air_pressure_str = cols[4].text.strip()
                    rain_str = cols[5].text.strip()
                    wind_str = cols[6].text.strip()
                    wind_direction_str = cols[8].text.strip()
                    windgusts_str = cols[11].text.strip()
                    data.append(
                        {
                            "interval": 300,
                            "date": datetime.datetime.strptime(
                                date_str + " " + time_str, "%d.%m.%Y %H:%M"
                            ).date(),
                            "temperature": extract_value(temperature_str),
                            "humidity": extract_value(humidity_str),
                            "air_pressure": extract_value(air_pressure_str),
                            "rain": extract_value(rain_str),
                            "wind": extract_kts(wind_str),
                            "wind_direction": extract_value(wind_direction_str),
                            "gusts": extract_kts(windgusts_str),
                        }
                    )
            elif station == "kressbronn":
                if len(cols) == 25:
                    date_str = cols[0].text.strip()
                    time_str = cols[1].text.strip()
                    temperature_str = cols[2].text.strip()
                    humidity_str = cols[8].text.strip()
                    air_pressure_str = cols[14].text.strip()
                    rain_str = cols[15].text.strip()
                    wind_str = cols[16].text.strip()
                    wind_direction_str = cols[18].text.strip()
                    windgusts_str = cols[24].text.strip()
                    data.append(
                        {
                            "interval": 120,
                            "date": datetime.datetime.strptime(
                                date_str + " " + time_str, "%d.%m.%Y %H:%M"
                            ).date(),
                            "temperature": extract_value(temperature_str),
                            "humidity": extract_value(humidity_str),
                            "air_pressure": extract_value(air_pressure_str),
                            "rain": extract_value(rain_str),
                            "wind": extract_kmh(wind_str) * 0.54,
                            "wind_direction": extract_value(wind_direction_str),
                            "gusts": extract_kmh(windgusts_str) * 0.54,
                        }
                    )
        latest = data[0]

    elif station == "rohrspitz-zamg":
        res = response.json()

        ts = res["timestamps"][0]
        data = res["features"][0]["properties"]["parameters"]

        latest = {
            "interval": 600,
            "date": datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M%z").date(),
            "temperature": data["TL"]["data"][0],
            "humidity": data["RFAM"]["data"][0],
            "air_pressure": data["P"]["data"][0],
            "rain": data["RR"]["data"][0],
            "wind": data["FFAM"]["data"][0] * 1.943844,
            "wind_direction": data["DD"]["data"][0],
            "gusts": data["FFX"]["data"][0] * 1.943844,
        }

    return latest


def main(argv):
    # parse command line arguments and depending on the arguments, call the appropriate function
    # e.g. python kressbronn.py --station rohrspitz

    parser = argparse.ArgumentParser()
    parser.add_argument("--station", help="station name")
    args = parser.parse_args()

    # crawl data based on the station parameter passed
    station = args.station
    latest = crawl_data(station)

    # windguru upload api: https://stations.windguru.cz/upload_api.php

    # Windguru API upload
    # Generate salt and hash for authorization
    salt = secrets.token_hex(8)
    hash_object = hashlib.md5((salt + station + stations[station]["password"]).encode())
    hash_hex = hash_object.hexdigest()

    # Prepare GET parameters
    params = {
        "uid": station,
        "interval": latest["interval"],
        "wind_avg": latest["wind"],
        "wind_max": latest["gusts"],
        "wind_direction": latest["wind_direction"],
        "temperature": latest["temperature"],
        "rh": latest["humidity"],
        "mslp": latest["air_pressure"],
        "precip_interval": latest["rain"],
        "salt": salt,
        "hash": hash_hex,
    }
    # Make the GET request to upload data
    response = requests.get("https://www.windguru.cz/upload/api.php", params=params)
    # Check the response
    if (response.status_code != 200) or (response.text != "OK"):
        print(
            f"Failed to upload data. Status code: {response.status_code}, Response: {response.text}"
        )
        print(latest)


if __name__ == "__main__":
    main(sys.argv[1:])
