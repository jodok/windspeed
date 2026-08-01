import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import windguru


class StationHealthTest(unittest.TestCase):
    def test_fresh_stale_and_missing_station_state(self):
        now = 2_000_000
        state = {
            "altenrhein": now - windguru.STALE_AFTER_SECONDS,
            "rohrspitz": now - windguru.STALE_AFTER_SECONDS - 1,
        }

        self.assertTrue(windguru.station_health("altenrhein", state, now)["healthy"])
        self.assertFalse(windguru.station_health("rohrspitz", state, now)["healthy"])
        self.assertFalse(windguru.station_health("kressbronn", state, now)["healthy"])

    def test_stale_stations_includes_missing_state(self):
        now = 2_000_000
        state = {station: now for station in windguru.stations}
        del state["praia-bela-vista"]

        self.assertEqual(
            windguru.stale_stations(state, now),
            ["praia-bela-vista"],
        )


class HealthServerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir_patch = patch.object(
            windguru, "STATE_DIR", Path(self.temp_dir.name)
        )
        self.state_dir_patch.start()
        self.server = windguru.make_health_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.state_dir_patch.stop()
        self.temp_dir.cleanup()

    def request(self, path):
        try:
            response = urllib.request.urlopen(self.base_url + path)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.version, json.loads(response.read())

    def test_station_endpoint_reports_freshness(self):
        windguru.save_state("altenrhein", int(windguru.time.time()))
        status, version, body = self.request("/health/altenrhein")

        self.assertEqual(status, 200)
        self.assertEqual(version, 11)
        self.assertTrue(body["healthy"])
        self.assertEqual(body["station"], "altenrhein")

    def test_missing_state_is_service_unavailable(self):
        status, version, body = self.request("/health/praia-bela-vista")

        self.assertEqual(status, 503)
        self.assertEqual(version, 11)
        self.assertFalse(body["healthy"])
        self.assertIsNone(body["last_update"])

    def test_unknown_station_is_not_found(self):
        status, version, body = self.request("/health/not-a-station")

        self.assertEqual(status, 404)
        self.assertEqual(version, 11)
        self.assertEqual(body["error"], "unknown station")


if __name__ == "__main__":
    unittest.main()
