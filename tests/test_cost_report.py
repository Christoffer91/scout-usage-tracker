import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scout_usage_tracker.cost_report import CostReportError, build_cost_report, format_cost_report


def details(nano: int) -> str:
    return f'[{ {"tokenCount": nano, "costPerBatch": 1, "batchSize": 1} }]'.replace("'", '"')


def create_source(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE assistant_usage_events (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_index INTEGER,
            model TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, total_nano_aiu INTEGER NOT NULL,
            token_details_json TEXT, created_at TEXT NOT NULL
        );
    """)
    connection.close()


def add_event(
    path: Path, event_id: str, session: str, turn: int, nano: int, created_at: str,
    *, model: str = "gpt-5.6-sol", input_tokens: int = 100, output_tokens: int = 10,
    cache_read_tokens: int = 50, raw: str | None = None,
) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO assistant_usage_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, session, turn, model, input_tokens, output_tokens, cache_read_tokens,
         nano, details(nano) if raw is None else raw, created_at),
    )
    connection.commit(); connection.close()


class CostReportTests(unittest.TestCase):
    def test_last_thread_periods_tokens_models_and_current_turn_exclusion(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_event(source, "a", "current", 1, 600_000_000, "2026-08-03T08:00:00Z", input_tokens=1_000)
            add_event(source, "b", "current", 2, 1_400_000_000, "2026-08-07T09:00:00Z", input_tokens=2_000)
            add_event(source, "c", "current", 2, 600_000_000, "2026-08-07T09:01:00Z", model="gpt-5.6-luna", input_tokens=3_000)
            add_event(source, "current-cost", "current", 3, 9_000_000_000, "2026-08-07T09:02:00Z")
            add_event(source, "other", "other", 1, 3_000_000_000, "2026-08-07T09:03:00Z")
            report = build_cost_report(source, "current", "Europe/Oslo", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
            self.assertEqual((report.last_answer.total_nano_aiu, report.last_answer.model_calls), (2_000_000_000, 2))
            self.assertEqual((report.thread.total_nano_aiu, report.thread.model_calls), (2_600_000_000, 3))
            self.assertEqual(report.thread.input_tokens, 6_000)
            self.assertEqual([item.model for item in report.thread.models], ["gpt-5.6-sol", "gpt-5.6-luna"])
            self.assertEqual((report.today.total_nano_aiu, report.today.model_calls), (5_000_000_000, 3))
            self.assertEqual(report.week.total_nano_aiu, 5_600_000_000)
            self.assertEqual(report.month.total_nano_aiu, 5_600_000_000)
            self.assertEqual((report.integrity, report.checked_events), ("pass", 4))

    def test_null_assistant_response_is_irrelevant_and_fresh_chat_reports_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_event(source, "current-cost", "automation", 0, 1_000_000_000, "2026-08-07T08:00:00Z")
            report = build_cost_report(source, "automation", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
            self.assertEqual(report.thread.model_calls, 0)
            self.assertEqual(report.last_answer.total_nano_aiu, 0)
            self.assertEqual(report.today.total_nano_aiu, 0)

    def test_norwegian_thread_output_partial_pricing_and_honest_rounding(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_event(source, "rated", "s", 1, 1_500_000_000, "2026-08-07T08:00:00Z",
                      input_tokens=234_874_570, output_tokens=147_592, cache_read_tokens=227_898_154)
            add_event(source, "unrated", "s", 1, 500_000_000, "2026-08-07T08:01:00Z", model="gpt-5.6-luna")
            add_event(source, "current-cost", "s", 2, 1, "2026-08-07T08:02:00Z")
            report = build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
            output = format_cost_report(report, "thread", {"gpt-5.6-sol": "0.01"}, "10")
            self.assertIn("Denne chatten hittil", output)
            self.assertIn("**2 modellkall**", output)
            self.assertIn("**2 Scout-credits** · eksakt beregnet fra nano-AIU, avrundet visning", output)
            self.assertIn("Input: **235M tokens**", output)
            self.assertIn("Output: **147 602 tokens**", output)
            self.assertIn("Cache-read: **228M tokens**", output)
            self.assertIn("GPT-5.6 Sol-delen: ca. **0,02 USD / 0 NOK**", output)
            self.assertIn("**0,50 credits** fra GPT-5.6 Luna", output)
            self.assertIn("ikke en faktura", output)
            self.assertIn("Scout-only", output)

    def test_period_titles(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_event(source, "past", "s", 1, 1, "2026-08-07T08:00:00Z")
            add_event(source, "current", "s", 2, 1, "2026-08-07T08:01:00Z")
            report = build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
            for period, title in (("last", "Siste fullførte svar"), ("day", "I dag"),
                                  ("week", "Denne ISO-uken"), ("month", "Denne måneden")):
                self.assertTrue(format_cost_report(report, period).startswith(title), period)

    def test_invalid_json_and_mismatch_statuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_event(source, "bad-json", "s", 1, 1, "2026-08-07T08:00:00Z", raw="not-json")
            add_event(source, "current", "s", 2, 1, "2026-08-07T08:01:00Z")
            self.assertEqual(build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc)).integrity, "warning")
            connection = sqlite3.connect(source)
            connection.execute("UPDATE assistant_usage_events SET token_details_json=? WHERE id='bad-json'", (details(2),)); connection.commit(); connection.close()
            self.assertEqual(build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc)).integrity, "failed")

    def test_dst_day_boundaries_are_transition_aware(self):
        cases = (
            (datetime(2026, 3, 29, 12, tzinfo=timezone.utc), "2026-03-28T23:00:00Z", "2026-03-29T22:00:00Z"),
            (datetime(2026, 10, 25, 12, tzinfo=timezone.utc), "2026-10-24T22:00:00Z", "2026-10-25T23:00:00Z"),
        )
        for now, start, end in cases:
            with self.subTest(now=now), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "source.db"; create_source(source)
                add_event(source, "inside-start", "other", 1, 1, start)
                end_minus = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() - 1
                add_event(source, "inside-end", "other", 1, 1, datetime.fromtimestamp(end_minus, timezone.utc).isoformat())
                add_event(source, "outside", "other", 1, 10, end)
                add_event(source, "past", "s", 1, 1, start)
                add_event(source, "current", "s", 2, 1, start)
                self.assertEqual(build_cost_report(source, "s", "Europe/Oslo", now=now).today.model_calls, 3)

    def test_snapshot_is_fixed_by_first_read_before_concurrent_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_event(source, "initial", "s", 1, 1_000_000_000, "2026-08-07T08:00:00Z")
            add_event(source, "current", "s", 2, 1, "2026-08-07T08:01:00Z")

            def commit_writer():
                add_event(source, "later", "other", 1, 50_000_000_000, "2026-08-07T09:00:00Z")

            report = build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc), _after_snapshot=commit_writer)
            self.assertEqual(report.today.total_nano_aiu, 1_000_000_000)

    def test_missing_database_schema_session_and_locked_database_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(CostReportError, "not found"):
                build_cost_report(root / "missing.db", "s", "UTC")
            source = root / "source.db"; create_source(source)
            with self.assertRaisesRegex(CostReportError, "no active"):
                build_cost_report(source, "", "UTC")
            with self.assertRaisesRegex(CostReportError, "no Scout usage events"):
                build_cost_report(source, "s", "UTC")
            broken = root / "broken.db"; sqlite3.connect(broken).close()
            with self.assertRaisesRegex(CostReportError, "unsupported"):
                build_cost_report(broken, "s", "UTC")
            locker = sqlite3.connect(source); locker.execute("PRAGMA journal_mode=DELETE"); locker.execute("BEGIN EXCLUSIVE")
            try:
                with self.assertRaisesRegex(CostReportError, "could not be read safely"):
                    build_cost_report(source, "s", "UTC")
            finally:
                locker.rollback(); locker.close()

    def test_output_contains_no_session_or_source_identifiers(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_event(source, "private-id", "secret-session", 1, 1, "2026-08-07T08:00:00Z")
            add_event(source, "current-id", "secret-session", 2, 1, "2026-08-07T08:01:00Z")
            output = format_cost_report(build_cost_report(source, "secret-session", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc)))
            self.assertNotIn("secret-session", output)
            self.assertNotIn("private-id", output)
            self.assertNotIn(temporary, output)


if __name__ == "__main__":
    unittest.main()
