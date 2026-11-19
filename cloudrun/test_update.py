"""Offline regression tests for Cloud Run rate refresh and its fallback."""
import copy
import unittest
from unittest.mock import patch
from cloudrun import update


class RateTests(unittest.TestCase):
    def test_fred_csv_formats(self):
        parsed = update._decode_rates_csv(
            "observation_date,DFF\n2026-11-18,3.88\n2026-11-19,.\n"
        )
        self.assertEqual(parsed, {"2026-11-18": 3.88})

    def test_fallback_works_after_fred_error(self):
        with patch.object(update, "fetch_dff", side_effect=TimeoutError("test")), \
             patch.object(update, "fetch_new_york_fed_effr",
                          return_value={"2026-11-19": 3.88}):
            self.assertEqual(
                update.fetch_recent_rates(update.dt.date(2026, 11, 18)),
                {"2026-11-19": 3.88},
            )

    def test_no_backdating_of_historical_rates(self):
        history = {
            "markets": [
                {"date": "2026-11-17", "dff": 3.87},
                {"date": "2026-11-18", "dff": 3.88},
                {"date": "2026-11-19", "dff": 3.88},
            ]
        }
        data = {"dff": {"date": "2026-11-18", "value": 3.88}}
        originals = copy.deepcopy(history)
        written = {}
        with patch.object(update, "get_file", side_effect=[
            (history, "hsha"), (data, "dsha")
        ]), patch.object(update, "fetch_recent_rates", return_value={
            "2026-11-18": 3.88, "2026-11-19": 3.90,
        }), patch.object(update, "update_file", side_effect=lambda p, obj, sha:
            written.__setitem__(p, copy.deepcopy(obj))):
            update.refresh_fred()
        self.assertEqual(history["markets"][0], originals["markets"][0])
        self.assertEqual(history["markets"][1], originals["markets"][1])
        self.assertEqual(history["markets"][2]["dff"], 3.90)
        self.assertEqual(written["data.json"]["dff"],
                         {"date": "2026-11-19", "value": 3.90})


if __name__ == "__main__":
    unittest.main()
