import argparse
import datetime
import hashlib
import json
import os
import re
import requests
import secrets
import sys
import pytz
import xml.etree.ElementTree as ET

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
    "rohrspitz-old": {
        "url": "https://www.kite-connection.at/weatherstation/aktuell.htm",
        "interval": 300,
        "password": os.getenv("WINDSPEED_PASS_ROHRSPITZ"),
    },
    "rohrspitz": {
        # "url": "https://admin.meteobridge.com/1bf5f40ad1e757d85cc41a993112a638/public/chart.cgi?chart=kiteconnection-grj-kn.chart&res=min&lang=de&start=H1&stop=D0",
        "url": "https://admin.meteobridge.com/1bf5f40ad1e757d85cc41a993112a638/public/livedataxml.cgi",
        "interval": 60,
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
    "praia-da-rainha": {
        "url": "https://api.ipma.pt/open-data/observation/meteorology/stations/observations.json",
        "interval": 300,
        "password": os.getenv("WINDSPEED_PASS_PRAIA_DA_RAINHA"),
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

    if station == "rohrspitz":
        # returns xml
        root = ET.fromstring(response.text)

        # WIND tag: wind (m/s), gust (m/s), dir (deg), date (YYYYMMDDhhmmss)
        wind_tag = root.find("WIND")
        wind = float(wind_tag.attrib["wind"])
        gusts = float(wind_tag.attrib["gust"])
        wind_direction = float(wind_tag.attrib["dir"])
        wind_date = wind_tag.attrib["date"]

        # TH tag: temp (°C), hum (%)
        th_tag = root.find("TH")
        temperature = float(th_tag.attrib["temp"])
        humidity = float(th_tag.attrib["hum"])

        # THB tag: press (hPa)
        thb_tag = root.find("THB")
        air_pressure = float(thb_tag.attrib["press"])

        # RAIN tag: rate (mm), date (YYYYMMDDhhmmss)
        rain_tag = root.find("RAIN")
        rain = float(rain_tag.attrib["rate"])

        # Use the most recent date (from WIND tag) for unixtime
        dt = datetime.datetime.strptime(wind_date, "%Y%m%d%H%M%S")
        dt = dt.replace(tzinfo=datetime.timezone.utc)
        unixtime = int(dt.timestamp())

        latest["unixtime"] = unixtime
        latest["temperature"] = temperature
        latest["humidity"] = humidity
        latest["air_pressure"] = air_pressure
        latest["rain"] = rain
        latest["wind"] = wind
        latest["wind_direction"] = wind_direction
        latest["gusts"] = gusts

    elif station == "kressbronn":
        soup = BeautifulSoup(response.text, "html.parser")

        table = soup.find("table", attrs={"border": "1"})
        rows = table.find_all("tr")
        row = rows[1]
        cols = row.find_all("td")

        date_str = cols[0].text.strip()
        time_str = cols[1].text.strip()
        latest["unixtime"] = int(
            datetime.datetime.strptime(
                date_str + " " + time_str, "%d.%m.%Y %H:%M"
            ).timestamp()
        )
        temperature_str = cols[2].text.strip()
        latest["temperature"] = extract_value(temperature_str)

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

        latest["unixtime"] = int(data_dict.get("wxtime"))
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

        latest["unixtime"] = int(
            datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M%z").timestamp()
        )
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
        latest["unixtime"] = int(data["date"] / 1000)
        latest["temperature"] = float(data["temperature"])
        latest["humidity"] = float(data["humidity"])
        latest["air_pressure"] = float(data["air_pressure"])
        latest["rain"] = float(data["rain"])
        latest["wind"] = float(data["wind"]) / 1.852
        latest["wind_direction"] = float(data["wind_direction"])
        latest["gusts"] = float(data["gusts"]) / 1.852

    elif station == "praia-da-rainha":
        # get stations from ipma
        # request = requests.get("http://www.ipma.pt/pt/index.html")
        # MATCH = re.search(r"var stations=(.*?)\;", request.text, re.DOTALL)
        # Almada, P.Rainha
        station_id = "1210773"

        # Invocação:
        # https://api.ipma.pt/open-data/observation/meteorology/stations/observations.json
        # Notas: Taxa de atualização horária. (valor "-99.0" = nodata)
        #
        # Resultado (formato json): { "{YYYY-mm-ddThh:mi}": { "{idEstacao}": { "intensidadeVentoKM": 0.0, "temperatura": 7.7, "idDireccVento": 3, "precAcumulada": 0.0, "intensidadeVento": 0.0, "humidade": 89.0, "pressao": -99.0, "radiacao": -99.0 }, ...}
        #
        # YYYY-mm-ddThh:mi: data/hora da observação
        # idEstacao: identificador da estação (consultar serviço auxiliar "Lista de identificadores das estações meteorológicas")
        # intensidadeVentoKM: intensidade do vento registada a 10 metros de altura (km/h)
        # temperatura: temperatura do ar registada a 1.5 metros de altura, média da hora (ºC)
        # idDireccVento: classe do rumo do vento ao rumo predominante do vento registado a 10 metros de altura (0: sem rumo, 1 ou 9: "N", 2: "NE", 3: "E", 4: "SE", 5: "S", 6: "SW", 7: "W", 8: "NW")
        # precAcumulada: precipitação registada a 1.5 metros de altura, valor acumulado da hora (mm)
        # intensidadeVento: intensidade do vento registada a 10 metros de altura (m/s)
        # humidade: humidade relativa do ar registada a 1.5 metros de altura, média da hora (%)
        # pressao: pressão atmosférica, reduzida ao nível médio do mar (NMM), média da hora (hPa)
        # radiacao: radiação solar (kJ/m2)

        data = response.json()
        # {
        #   "2025-02-20T17:00": {
        #     "1210881": {
        #       "intensidadeVentoKM": 5.0,
        #       "temperatura": 17.1,
        #       "radiacao": 335.7,
        #       "idDireccVento": 6,
        #       "precAcumulada": 0.0,
        #       "intensidadeVento": 1.4,
        #       "humidade": -99.0,
        #       "pressao": -99.0
        #     }
        #   }
        # }

        latest_timestamp = max(data.keys())
        latest_data = data[latest_timestamp]
        latest_observation = latest_data[station_id]

        # print(latest_timestamp)
        utc_datetime = datetime.datetime.strptime(latest_timestamp, "%Y-%m-%dT%H:%M")
        latest["unixtime"] = int(
            utc_datetime.replace(tzinfo=datetime.timezone.utc).timestamp()
        )

        latest["temperature"] = (
            latest_observation["temperatura"]
            if not latest_observation["temperatura"] == -99.0
            else ""
        )
        latest["humidity"] = (
            latest_observation["humidade"]
            if not latest_observation["humidade"] == -99.0
            else ""
        )
        latest["air_pressure"] = (
            latest_observation["pressao"]
            if not latest_observation["pressao"] == -99.0
            else ""
        )
        latest["rain"] = (
            latest_observation["precAcumulada"]
            if not latest_observation["precAcumulada"] == -99.0
            else ""
        )
        latest["wind"] = (
            latest_observation["intensidadeVento"] * 1.94384
            if not latest_observation["intensidadeVento"] == -99.0
            else ""
        )
        latest["gusts"] = ""
        direction_map = {
            0: "",  # no direction
            1: 0,  # N
            2: 45,  # NE
            3: 90,  # E
            4: 135,  # SE
            5: 180,  # S
            6: 225,  # SW
            7: 270,  # W
            8: 315,  # NW
            9: 0,  # N
        }
        latest["wind_direction"] = direction_map[latest_observation["idDireccVento"]]

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
        "unixtime": latest["unixtime"],
        # "interval": latest["interval"],
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
