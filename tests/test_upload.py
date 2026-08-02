import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import windguru


def reading(unixtime):
    """A crawl_data() return value; only unixtime matters to these tests."""
    return {
        "interval": 300,
        "unixtime": unixtime,
        "wind": 12.0,
        "gusts": 18.0,
        "wind_direction": 220.0,
        "temperature": 21.0,
        "humidity": 60.0,
        "air_pressure": 1013.0,
        "rain": 0.0,
    }


class UploadSkipsUnchangedObservationTest(unittest.TestCase):
    """A poll that reads back an already-uploaded observation must not re-send.

    Every station is polled faster than its upstream publishes, so this is the
    common case, not an edge one -- and windguru rejects a reading it has
    already taken with "ERROR (data too old)" as soon as it ages past the
    limit.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        patcher = patch.object(windguru, "STATE_DIR", Path(self.temp_dir.name))
        patcher.start()
        self.addCleanup(patcher.stop)

        self.upload = patch.object(windguru.requests, "get").start()
        self.addCleanup(patch.stopall)
        self.upload.return_value = Mock(status_code=200, text="OK")

    def run_poll(self, unixtime):
        with patch.object(windguru, "crawl_data", return_value=reading(unixtime)):
            return windguru.main(["--station", "praia-da-rainha"])

    def test_new_observation_is_uploaded_and_recorded(self):
        self.assertEqual(self.run_poll(1_700_000_000), 0)

        self.assertEqual(self.upload.call_count, 1)
        self.assertEqual(
            windguru.load_state()["praia-da-rainha"],
            1_700_000_000,
        )

    def test_same_observation_is_not_uploaded_twice(self):
        self.run_poll(1_700_000_000)
        self.upload.reset_mock()

        self.assertEqual(self.run_poll(1_700_000_000), 0)

        self.upload.assert_not_called()
        # The recorded time is the observation windguru accepted, so the health
        # endpoint keeps ageing from it rather than from this skipped poll.
        self.assertEqual(windguru.load_state()["praia-da-rainha"], 1_700_000_000)

    def test_an_older_observation_is_not_uploaded(self):
        """Upstream serving a reading older than the last one is a rollback."""
        self.run_poll(1_700_000_000)
        self.upload.reset_mock()

        self.assertEqual(self.run_poll(1_699_999_000), 0)

        self.upload.assert_not_called()
        self.assertEqual(windguru.load_state()["praia-da-rainha"], 1_700_000_000)

    def test_next_observation_is_uploaded(self):
        self.run_poll(1_700_000_000)
        self.upload.reset_mock()

        self.assertEqual(self.run_poll(1_700_003_600), 0)

        self.assertEqual(self.upload.call_count, 1)
        self.assertEqual(windguru.load_state()["praia-da-rainha"], 1_700_003_600)

    def test_a_rejected_upload_leaves_the_state_alone(self):
        """So the retry after a rejection is not itself skipped as a repeat."""
        self.upload.return_value = Mock(status_code=200, text="ERROR (invalid uid)")

        self.assertEqual(self.run_poll(1_700_000_000), 0)

        self.assertEqual(self.upload.call_count, 1)
        self.assertNotIn("praia-da-rainha", windguru.load_state())


if __name__ == "__main__":
    unittest.main()
