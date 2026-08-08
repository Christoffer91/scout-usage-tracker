import unittest
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scout_usage_tracker.aggregate import aggregate, drilldown_records


def has_zone(name):
    try:
        ZoneInfo(name)
        return True
    except ZoneInfoNotFoundError:
        return False


def row(at, model="m", nano=10, session="a" * 64):
    return {"event_time_utc": at, "model": model, "total_nano_aiu": nano,
            "input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 5,
            "cache_write_tokens": 1, "reasoning_tokens": 3,
            "verification_status": "verified", "session_digest": session}


class AggregateTests(unittest.TestCase):
    @unittest.skipUnless(has_zone("America/New_York"), "IANA timezone data is unavailable")
    def test_dst_local_day_and_iso_week_year(self):
        result = aggregate([
            row("2024-12-30T05:30:00+00:00"),
            row("2025-03-09T06:30:00+00:00"),
            row("2025-03-09T07:30:00+00:00"),
        ], "America/New_York")
        weeks = {item["label"] for item in result["groups"]["week"]}
        days = {item["label"] for item in result["groups"]["day"]}
        self.assertIn("2025-W01", weeks)
        self.assertIn("2025-03-09", days)
        self.assertEqual(sum(item["calls"] for item in result["groups"]["day"]), 3)

    def test_all_dimensions_conserve_exact_total_and_model_owns_event(self):
        rows = [row("2025-01-01T00:00:00+00:00", "a", 7), row("2025-02-01T00:00:00+00:00", "b", -2)]
        result = aggregate(rows, "UTC", include_sessions=True)
        self.assertEqual(result["total"]["credits"], Decimal("0.000000005"))
        for dimension in ("day", "week", "month", "model", "session"):
            for bucket in ("nano", "calls", "input", "output", "cache_read", "cache_write", "reasoning"):
                self.assertEqual(
                    sum(item[bucket] for item in result["groups"][dimension]),
                    result["total"][bucket],
                    f"{dimension} did not conserve {bucket}",
                )
        self.assertEqual({item["label"] for item in result["groups"]["model"]}, {"a", "b"})
        self.assertEqual(result["groups"]["session"][0]["label"], "aaaaaaaaaaaa")

    def test_drilldown_records_are_aggregate_and_chat_labels_are_opt_in(self):
        rows = [
            row("2025-01-01T10:00:00+00:00", "model-a", 7, "a" * 64),
            row("2025-01-01T11:00:00+00:00", "model-a", 5, "a" * 64),
            row("2025-01-01T12:00:00+00:00", "model-b", 3, "b" * 64),
        ]
        private = drilldown_records(rows, "UTC", include_sessions=True)
        self.assertEqual(len(private), 2)
        self.assertEqual(private[0]["calls"], 2)
        self.assertEqual(private[0]["chat"], "aaaaaaaaaaaa")
        self.assertNotIn("session_digest", private[0])
        public = drilldown_records(rows, "UTC", include_sessions=False)
        self.assertEqual(len(public), 2)
        self.assertTrue(all("chat" not in item for item in public))
