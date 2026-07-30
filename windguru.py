import argparse
import datetime
import hashlib
import json
import os
import re
import requests
import secrets
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from dotenv import load_dotenv

# The zone kressbronn's upstream prints its timestamps in. It publishes local
# wall-clock with no offset and no epoch, alone among the seven stations, so
# this cannot be inferred from the page and must not be inherited from the host
# -- see the note at the parse site. stdlib since 3.9; no new dependency, and
# the host resolves it from the system tzdata.
KRESSBRONN_TZ = ZoneInfo("Europe/Berlin")

# One state file PER STATION, holding its last successful upload time.
#
# It used to be a single station_state.json that every run rewrote whole:
# load_state() read the dict, the caller mutated one key, save_state() wrote it
# all back. Stations sharing a cadence run concurrently -- three of them are on
# */10 -- so two processes could interleave that read-modify-write and the
# second would clobber the first's update with its own stale copy. Splitting the
# file removes the race by construction rather than by locking: no two stations
# ever touch the same path.
STATE_DIR = Path(os.getenv("WINDSPEED_STATE_DIR", "state"))

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
    "praia-bela-vista": {
        "url": "https://widgets.ikitesurf.com/widgets/web/conditions?spot_id=602390&units_wind=kts&units_temp=C&width=400&height=500&color=1E1E1E&name=Praia%20Bela%20Vista-Waves4Life&activity=Kite&app=ikitesurf",
        "interval": 300,
        "password": os.getenv("WINDSPEED_PASS_PRAIA_BELA_VISTA"),
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


def state_path(station):
    return STATE_DIR / f"{station}.json"


def load_state():
    """Last successful upload time per station, read from one file each."""
    state = {}
    for station in stations:
        path = state_path(station)
        try:
            with path.open() as f:
                state[station] = json.load(f)["unixtime"]
        except FileNotFoundError:
            continue  # never uploaded; check_stale_updates treats it as stale
        except (json.JSONDecodeError, KeyError, OSError) as e:
            print(f"Error reading state file {path}: {e}", file=sys.stderr)
    return state


def save_state(station, unixtime):
    """Record a station's last successful upload, atomically.

    Written to a temp file in the same directory and moved into place, so a
    process killed mid-write leaves the previous value rather than a truncated
    file that the next read would report as corrupt.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=STATE_DIR, prefix=f".{station}.", suffix=".tmp", delete=False
        ) as f:
            json.dump({"unixtime": unixtime}, f)
            tmp = f.name
        os.replace(tmp, state_path(station))
    except OSError as e:
        print(f"Error writing state file for {station}: {e}", file=sys.stderr)


def check_stale_updates():
    """Report stations whose last successful upload is over 24 hours old.

    Returns the list of stale station names. This is the freshness ALERT, and
    it deliberately runs on its own daily schedule (windspeed-stale.timer)
    rather than on every poll: the threshold is 24 hours, so checking it every
    two minutes produced ~700 identical CRITICAL lines a day -- which is most
    of what the old 35 MB windspeed.log actually contained -- and, once the
    check gates a unit's exit status, would mail the same alert just as often.
    """
    state = load_state()
    current_time = int(datetime.datetime.now().timestamp())

    # Every configured station, not only those with a state file: a station
    # that has NEVER uploaded is the most stale case there is, not an absent one.
    stale_stations = [
        station
        for station in stations
        if current_time - state.get(station, 0) > 24 * 3600
    ]

    if stale_stations:
        print(
            f"CRITICAL: Stations not updated in 24 hours: {', '.join(stale_stations)}",
            file=sys.stderr,
        )
    return stale_stations


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
        latest["wind"] = wind * 1.943844
        latest["wind_direction"] = wind_direction
        latest["gusts"] = gusts * 1.943844

    elif station == "kressbronn":
        soup = BeautifulSoup(response.text, "html.parser")

        table = soup.find("table", attrs={"border": "1"})
        rows = table.find_all("tr")
        row = rows[1]
        cols = row.find_all("td")

        date_str = cols[0].text.strip()
        time_str = cols[1].text.strip()
        # The page prints local wall-clock time with no offset, so the zone has
        # to be supplied here. It used to be left implicit -- strptime returns a
        # naive datetime and .timestamp() then reads it in the HOST's zone --
        # which was invisibly correct for as long as the host was a Mac in
        # Vienna, and broke the moment this moved to app-btlg-civ-01, where the
        # fleet standard is Etc/UTC. Every reading was submitted two hours in
        # the future and windguru rejected the lot with "ERROR (time in
        # future?)"; kressbronn was the only station affected, because it is the
        # only one whose upstream gives neither an offset nor an epoch.
        #
        # ZoneInfo rather than a fixed +02:00: the offset is +1 in winter, and a
        # constant would be wrong for half the year and wrong by an hour across
        # each DST switch.
        latest["unixtime"] = int(
            datetime.datetime.strptime(date_str + " " + time_str, "%d.%m.%Y %H:%M")
            .replace(tzinfo=KRESSBRONN_TZ)
            .timestamp()
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

        # Walk the hourly buckets newest-first and take the first one that
        # actually has an observation for THIS station.
        #
        # It used to be `max(data.keys())` unconditionally. IPMA publishes the
        # current hour's bucket as soon as the hour starts, with `null` for every
        # station that has not reported into it yet -- so whether that worked
        # depended entirely on where in the hour the poll landed, and the miss
        # raised "'NoneType' object is not subscriptable" rather than saying
        # anything useful. Checked against the live feed on 2026-07-30: the
        # newest bucket was null for this station and the one before it was fine.
        latest_timestamp = None
        latest_observation = None
        for timestamp in sorted(data.keys(), reverse=True):
            observation = data[timestamp].get(station_id)
            if observation is not None:
                latest_timestamp = timestamp
                latest_observation = observation
                break

        if latest_observation is None:
            raise ValueError(
                f"IPMA has no observation for station {station_id} "
                f"in any of its {len(data)} reported hours"
            )

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

    elif station == "praia-bela-vista":
        # iKitesurf widget - extract wfToken from HTML
        soup = BeautifulSoup(response.text, "html.parser")

        # Find the script tag containing the wfToken
        scripts = soup.find_all("script")
        wf_token = None

        for script in scripts:
            if script.string and "wfToken" in script.string:
                # Extract wfToken using regex
                token_match = re.search(r"var wfToken = '([^']+)';", script.string)
                if token_match:
                    wf_token = token_match.group(1)
                    break

        # Without a token the request below would send `wf_token=None` and get
        # back something that fails much further down. Say what actually broke.
        if wf_token is None:
            raise ValueError("no wfToken in the iKitesurf widget HTML")

        api_response = requests.get(
            f"https://api.weatherflow.com/wxengine/rest/spot/getSpotDetailSetByList?units_wind=kts&units_temp=C&units_distance=mi&stormprint_only=false&spot_list=602390&wf_token={wf_token}"
        )
        data = api_response.json()

        # Extract data from the JSON response. Named spot_station, NOT station:
        # `station` is this function's own parameter, and rebinding it here made
        # every reference below this line silently mean something else.
        spot = data["spots"][0]
        spot_station = spot["stations"][0]
        data_values = spot_station["data_values"][0]  # Most recent observation

        # Map the data_values array to the data_names
        data_names = spot["data_names"]
        data_dict = dict(zip(data_names, data_values))

        # An offline station answers 200 with every field null and
        # wind_desc "Station is down" -- the normal state for this spot for days
        # at a time. Report that as what it is instead of letting strptime raise
        # "argument 1 must be str, not None" three lines down, which is how it
        # surfaced in the log for over a day.
        timestamp_str = data_dict["utc_timestamp"]
        if timestamp_str is None:
            raise ValueError(
                "iKitesurf reports no observation "
                f"({data_dict.get('wind_desc') or 'no reason given'})"
            )

        dt = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=datetime.timezone.utc)
        latest["unixtime"] = int(dt.timestamp())

        # Extract weather data
        latest["wind"] = data_dict["avg"] if data_dict["avg"] is not None else ""
        latest["gusts"] = data_dict["gust"] if data_dict["gust"] is not None else ""
        latest["wind_direction"] = (
            data_dict["dir"] if data_dict["dir"] is not None else ""
        )
        latest["temperature"] = (
            data_dict["atemp"] if data_dict["atemp"] is not None else ""
        )
        latest["humidity"] = (
            data_dict["humidity"] if data_dict["humidity"] is not None else ""
        )
        latest["air_pressure"] = (
            data_dict["pres"] if data_dict["pres"] is not None else ""
        )
        latest["rain"] = data_dict["precip"] if data_dict["precip"] is not None else ""

    return latest


def main(argv):
    # parse command line arguments and depending on the arguments, call the appropriate function
    # e.g. python windguru.py --station rohrspitz

    parser = argparse.ArgumentParser()
    parser.add_argument("--station", help="station name")
    parser.add_argument(
        "--check-stale",
        action="store_true",
        help="report stations with no successful upload in 24h and exit non-zero "
        "if there are any (run daily by windspeed-stale.timer)",
    )
    args = parser.parse_args()

    # The alert path, and the ONLY path that exits non-zero. Its unit carries
    # the OnFailure= mail drop-in, so this exit status is what turns into a
    # message to root.
    if args.check_stale:
        return 1 if check_stale_updates() else 0

    # crawl data based on the station parameter passed
    station = args.station
    if station is None:
        print(
            "No station specified. start windguru.py with --station <station_name>",
            file=sys.stderr,
        )
        return 2

    latest = None  # Initialize latest variable
    try:
        latest = crawl_data(station)

        # windguru upload api: https://stations.windguru.cz/upload_api.php

        # Windguru API upload
        # Generate salt and hash for authorization
        salt = secrets.token_hex(8)
        hash_object = hashlib.md5(
            (salt + station + stations[station]["password"]).encode()
        )
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
                f"Failed to upload data. Status code: {response.status_code}, Response: {response.text}",
                file=sys.stderr,
            )
            print(f"Data that failed to upload: {latest}", file=sys.stderr)
            return 0

        # If we got here, the update was successful
        save_state(station, latest["unixtime"])

    # A single failed poll is EXPECTED and stays exit 0, so it lands in the
    # journal without mailing anyone: upstreams go down for hours at a time and
    # the next run is two to fifteen minutes away. What escalates is a station
    # still stale after 24 hours, which --check-stale reports once a day.
    # Returning non-zero here instead would mail on every retry -- ~700 messages
    # a day for one dead station.
    except Exception as e:
        print(
            f"Error while updating station {station}: {e}, latest data was {latest}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
