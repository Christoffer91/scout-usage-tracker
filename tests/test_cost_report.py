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
            total_nano_aiu INTEGER NOT NULL, token_details_json TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE turns (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_index INTEGER NOT NULL,
            assistant_response TEXT
        );
    """)
    connection.close()


def add_turn(path: Path, session: str, turn: int, complete: bool = True) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO turns VALUES (?, ?, ?, ?)",
        (f"turn-{session}-{turn}", session, turn, "complete" if complete else None),
    )
    connection.commit(); connection.close()


def add_event(path: Path, event_id: str, session: str, turn: int, nano: int, created_at: str, raw: str | None = None) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO assistant_usage_events VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, session, turn, nano, details(nano) if raw is None else raw, created_at),
    )
    connection.commit(); connection.close()


class CostReportTests(unittest.TestCase):
    def test_last_thread_today_and_in_progress_exclusion(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_turn(source, "current", 1); add_turn(source, "current", 2)
            add_event(source, "a", "current", 1, 600_000_000, "2026-08-07T08:00:00Z")
            add_event(source, "b", "current", 2, 1_400_000_000, "2026-08-07T09:00:00Z")
            add_event(source, "c", "current", 2, 600_000_000, "2026-08-07T09:01:00Z")
            add_event(source, "in-progress", "current", 3, 9_000_000_000, "2026-08-07T09:02:00Z")
            add_event(source, "other", "other", 1, 3_000_000_000, "2026-08-07T09:03:00Z")
            report = build_cost_report(source, "current", "Europe/Oslo", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
            self.assertEqual((report.last_answer.total_nano_aiu, report.last_answer.tool_calls), (2_000_000_000, 2))
            self.assertEqual((report.thread.total_nano_aiu, report.thread.tool_calls), (2_600_000_000, 3))
            self.assertEqual((report.today.total_nano_aiu, report.today.tool_calls), (5_600_000_000, 4))
            self.assertEqual((report.integrity, report.checked_events), ("pass", 4))

    def test_fractional_credits_are_marked_approximate_and_round_half_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source); add_turn(source, "s", 1)
            add_event(source, "a", "s", 1, 1_500_000_000, "2026-08-07T08:00:00Z")
            output = format_cost_report(build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc)))
            self.assertIn("≈2 credits", output)
            self.assertIn("underlying nano-AIU calculation is exact", output)
            self.assertNotIn("1.5 credits", output)

    def test_invalid_json_and_mismatch_statuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source); add_turn(source, "s", 1)
            add_event(source, "bad-json", "s", 1, 1, "2026-08-07T08:00:00Z", "not-json")
            warning = build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
            self.assertEqual(warning.integrity, "warning")
            connection = sqlite3.connect(source)
            connection.execute("UPDATE assistant_usage_events SET token_details_json=?", (details(2),)); connection.commit(); connection.close()
            failed = build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
            self.assertEqual(failed.integrity, "failed")

    def test_dst_day_boundaries_are_transition_aware(self):
        cases = (
            (datetime(2026, 3, 29, 12, tzinfo=timezone.utc), "2026-03-28T23:00:00Z", "2026-03-29T22:00:00Z"),
            (datetime(2026, 10, 25, 12, tzinfo=timezone.utc), "2026-10-24T22:00:00Z", "2026-10-25T23:00:00Z"),
        )
        for now, start, end in cases:
            with self.subTest(now=now), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "source.db"; create_source(source); add_turn(source, "s", 1)
                add_event(source, "inside-start", "other", 1, 1, start)
                end_minus = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() - 1
                add_event(source, "inside-end", "other", 1, 1, datetime.fromtimestamp(end_minus, timezone.utc).isoformat())
                add_event(source, "outside", "other", 1, 10, end)
                add_event(source, "current", "s", 1, 1, start)
                report = build_cost_report(source, "s", "Europe/Oslo", now=now)
                self.assertEqual(report.today.tool_calls, 3)

    def test_snapshot_is_fixed_by_first_read_before_concurrent_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source); add_turn(source, "s", 1)
            add_event(source, "initial", "s", 1, 1_000_000_000, "2026-08-07T08:00:00Z")

            def commit_writer():
                add_event(source, "later", "other", 1, 50_000_000_000, "2026-08-07T09:00:00Z")

            report = build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc), _after_snapshot=commit_writer)
            self.assertEqual(report.today.total_nano_aiu, 1_000_000_000)

    def test_missing_database_schema_session_and_completed_turn_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(CostReportError, "not found"):
                build_cost_report(root / "missing.db", "s", "UTC")
            source = root / "source.db"; create_source(source)
            with self.assertRaisesRegex(CostReportError, "no active"):
                build_cost_report(source, "", "UTC")
            with self.assertRaisesRegex(CostReportError, "no completed"):
                build_cost_report(source, "s", "UTC")
            broken = root / "broken.db"; sqlite3.connect(broken).close()
            with self.assertRaisesRegex(CostReportError, "unsupported"):
                build_cost_report(broken, "s", "UTC")

    def test_exclusively_locked_database_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source); add_turn(source, "s", 1)
            locker = sqlite3.connect(source)
            locker.execute("PRAGMA journal_mode=DELETE")
            locker.execute("BEGIN EXCLUSIVE")
            try:
                with self.assertRaisesRegex(CostReportError, "could not be read safely"):
                    build_cost_report(source, "s", "UTC")
            finally:
                locker.rollback(); locker.close()

    def test_output_contains_no_session_or_message_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source); add_turn(source, "secret-session", 1)
            add_event(source, "private-id", "secret-session", 1, 1, "2026-08-07T08:00:00Z")
            output = format_cost_report(build_cost_report(source, "secret-session", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc)))
            self.assertNotIn("secret-session", output)
            self.assertNotIn("private-id", output)


if __name__ == "__main__":
    unittest.main()
