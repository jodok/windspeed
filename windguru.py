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
    "altenrhein": {
        "url": "https://www.meteoswiss.admin.ch/product/output/measured-values/stationMeta/messnetz-automatisch/stationMeta.messnetz-automatisch.ARH.en.json",
        "interval": 300,
        "password": os.getenv("WINDSPEED_PASS_ALTENRHEIN"),
    },
    "rohrspitz": {
        "url": "https://www.kite-connection.at/weatherstation/aktuell.htm",
        "interval": 300,
        "password": os.getenv("WINDSPEED_PASS_ROHRSPITZ"),
    },
    "rohrspitz-zamg": {
        "url": "https://dataset.api.hub.geosphere.at/v1/station/current/tawes-v1-10min?station_ids=11299&parameters=DD,FFAM,FFX,P,RFAM,RR,TL",
        "interval": 600,
        "password": os.getenv("WINDSPEED_PASS_ROHRSPITZ_ZAMG"),
    },
    "lindau-lsc": {
        "url": "https://stations.meteo-services.com/wetterstation/gatewaytest.php?station_id=3816&uw=kmh&ut=C&lp=0",
        "interval": 300,
        "password": os.getenv("WINDSPEED_PASS_LINDAU_LSC"),
    },
    "kressbronn": {
        "url": "https://www.wetter-kressbronn.de/wetter/aktuell.htm",
        "interval": 120,
        "password": os.getenv("WINDSPEED_PASS_KRESSBRONN"),
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

    latest = {
        "interval": stations[station]["interval"],
    }

    if station == "rohrspitz" or station == "kressbronn":
        soup = BeautifulSoup(response.text, "html.parser")

        table = soup.find("table", attrs={"border": "1"})
        rows = table.find_all("tr")
        row = rows[1]
        cols = row.find_all("td")

        date_str = cols[0].text.strip()
        time_str = cols[1].text.strip()
        temperature_str = cols[2].text.strip()

        latest["date"] = datetime.datetime.strptime(
            date_str + " " + time_str, "%d.%m.%Y %H:%M"
        ).date()
        latest["temperature"] = extract_value(temperature_str)

        if station == "rohrspitz":
            humidity_str = cols[3].text.strip()
            air_pressure_str = cols[4].text.strip()
            rain_str = cols[5].text.strip()
            wind_str = cols[6].text.strip()
            wind_direction_str = cols[8].text.strip()
            windgusts_str = cols[11].text.strip()

            latest["date"] = datetime.datetime.strptime(
                date_str + " " + time_str, "%d.%m.%Y %H:%M"
            ).date()
            latest["wind"] = extract_kts(wind_str)
            latest["gusts"] = extract_kts(windgusts_str)

        elif station == "kressbronn":
            humidity_str = cols[8].text.strip()
            air_pressure_str = cols[14].text.strip()
            rain_str = cols[15].text.strip()
            wind_str = cols[16].text.strip()
            wind_direction_str = cols[18].text.strip()
            windgusts_str = cols[24].text.strip()

            latest["wind"] = extract_kmh(wind_str) * 0.54
            latest["gusts"] = extract_kmh(windgusts_str) * 0.54

        latest["humidity"] = extract_value(humidity_str)
        latest["air_pressure"] = extract_value(air_pressure_str)
        latest["rain"] = extract_value(rain_str)
        latest["wind_direction"] = extract_value(wind_direction_str)

    elif station == "lindau-lsc":
        soup = BeautifulSoup(response.text, "html.parser")
        content = soup.get_text()
        data_pattern = re.compile(r"(\w+)\s*(-?\d+(\.\d+)?)")
        matches = data_pattern.findall(content)
        data_dict = {match[0]: float(match[1]) for match in matches}

        latest["date"] = datetime.datetime.fromtimestamp(data_dict.get("wxtime")).date()
        latest["temperature"] = data_dict.get("t2m")
        latest["humidity"] = data_dict.get("relhum")
        latest["air_pressure"] = data_dict.get("press")
        latest["rain"] = data_dict.get("rainrate")
        latest["wind"] = data_dict.get("windspeed") * 1.943844
        latest["wind_direction"] = data_dict.get("winddir")
        latest["gusts"] = data_dict.get("windgust") * 1.943844

    elif station == "rohrspitz-zamg":
        res = response.json()

        ts = res["timestamps"][0]
        data = res["features"][0]["properties"]["parameters"]

        latest["date"] = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M%z").date()
        latest["temperature"] = data["TL"]["data"][0]
        latest["humidity"] = data["RFAM"]["data"][0]
        latest["air_pressure"] = data["P"]["data"][0]
        latest["rain"] = data["RR"]["data"][0]
        latest["wind"] = data["FFAM"]["data"][0] * 1.943844
        latest["wind_direction"] = data["DD"]["data"][0]
        latest["gusts"] = data["FFX"]["data"][0] * 1.943844

    elif station == "altenrhein":
        base_url = "https://www.meteoswiss.admin.ch/product/output/measured-values/stationsTable/"
        paths = {
            "temperature": "messwerte-lufttemperatur-10min/stationsTable.messwerte-lufttemperatur-10min.en.json",
            "humidity": "messwerte-luftfeuchtigkeit-10min/stationsTable.messwerte-luftfeuchtigkeit-10min.en.json",
            "air_pressure": "messwerte-luftdruck-qfe-10min/stationsTable.messwerte-luftdruck-qfe-10min.en.json",
            "rain": "messwerte-niederschlag-10min/stationsTable.messwerte-niederschlag-10min.en.json",
            "wind": "messwerte-windgeschwindigkeit-kmh-10min/stationsTable.messwerte-windgeschwindigkeit-kmh-10min.en.json",
            "gusts": "messwerte-wind-boeenspitze-kmh-10min/stationsTable.messwerte-wind-boeenspitze-kmh-10min.en.json",
        }

        data = {}

        for key, path in paths.items():
            response = requests.get(base_url + path)
            res = response.json()
            # Assuming `res` is the result of `response.json()` and contains the JSON data
            station_id = "ARH"

            # Iterate through the data to find the station with the ID 'ARH'
            for station_data in res.get("stations", []):
                if station_data.get("id") == station_id:
                    current_data = station_data.get("current")
                    data["date"] = current_data.get("date")
                    data[key] = current_data.get("value")
                    if key == "wind":
                        data["wind_direction"] = current_data.get("wind_direction")
                    break

        latest["date"] = datetime.datetime.fromtimestamp(
            float(data["date"]) / 1000
        ).date()
        latest["temperature"] = float(data["temperature"])
        latest["humidity"] = float(data["humidity"])
        latest["air_pressure"] = float(data["air_pressure"])
        latest["rain"] = float(data["rain"])
        latest["wind"] = float(data["wind"]) / 1.852
        latest["wind_direction"] = float(data["wind_direction"])
        latest["gusts"] = float(data["gusts"]) / 1.852

    return latest


def main(argv):
    # parse command line arguments and depending on the arguments, call the appropriate function
    # e.g. python kressbronn.py --station rohrspitz

    parser = argparse.ArgumentParser()
    parser.add_argument("--station", help="station name")
    args = parser.parse_args()

    # crawl data based on the station parameter passed
    station = args.station
    if station is None:
        print(f"No station specified. start windguru.py with --station <station_name>")
        return

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
