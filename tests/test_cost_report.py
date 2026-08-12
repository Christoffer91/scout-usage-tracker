import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scout_usage_tracker.cost_report import CostReportError, build_cost_report, format_cost_faq, format_cost_report


def has_zone(name):
    try:
        ZoneInfo(name)
        return True
    except ZoneInfoNotFoundError:
        return False


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
            report = build_cost_report(source, "current", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
            self.assertEqual((report.last_answer.total_nano_aiu, report.last_answer.model_calls), (2_000_000_000, 2))
            self.assertEqual((report.thread.total_nano_aiu, report.thread.model_calls), (2_600_000_000, 3))
            self.assertEqual(report.thread.input_tokens, 6_000)
            self.assertEqual([item.model for item in report.thread.models], ["gpt-5.6-sol", "gpt-5.6-luna"])
            self.assertEqual((report.chat_today.total_nano_aiu, report.chat_today.model_calls), (2_000_000_000, 2))
            self.assertEqual((report.all_today.total_nano_aiu, report.all_today.model_calls), (5_000_000_000, 3))
            self.assertEqual(report.chat_week.total_nano_aiu, 2_600_000_000)
            self.assertEqual(report.all_week.total_nano_aiu, 5_600_000_000)
            self.assertEqual(report.all_month.total_nano_aiu, 5_600_000_000)
            self.assertEqual((report.thread.integrity, report.thread.checked_events), ("pass", 3))
            self.assertEqual(report.session_resolution, "environment")

    def test_missing_session_id_autodetects_only_unique_fresh_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_event(source, "target-past", "target", 1, 2_000_000_000, "2026-08-07T11:59:50Z")
            add_event(source, "other", "other", 1, 9_000_000_000, "2026-08-07T11:59:51Z")
            add_event(source, "target-current", "target", 2, 1, "2026-08-07T11:59:59Z")
            report = build_cost_report(source, "", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
            self.assertEqual(report.session_resolution, "recent_event")
            self.assertEqual(report.thread.total_nano_aiu, 2_000_000_000)

    def test_missing_session_id_rejects_stale_or_ambiguous_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            stale = Path(temporary) / "stale.db"; create_source(stale)
            add_event(stale, "old", "s", 1, 1, "2026-08-07T11:59:29Z")
            with self.assertRaisesRegex(CostReportError, "no uniquely fresh"):
                build_cost_report(stale, "", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))

            ambiguous = Path(temporary) / "ambiguous.db"; create_source(ambiguous)
            add_event(ambiguous, "one", "one", 1, 1, "2026-08-07T11:59:59Z")
            add_event(ambiguous, "two", "two", 1, 1, "2026-08-07T11:59:57Z")
            with self.assertRaisesRegex(CostReportError, "multiple Scout conversations"):
                build_cost_report(ambiguous, "", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))

    def test_null_assistant_response_is_irrelevant_and_fresh_chat_reports_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_event(source, "current-cost", "automation", 0, 1_000_000_000, "2026-08-07T08:00:00Z")
            report = build_cost_report(source, "automation", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
            self.assertEqual(report.thread.model_calls, 0)
            self.assertEqual(report.last_answer.total_nano_aiu, 0)
            self.assertEqual(report.chat_today.total_nano_aiu, 0)

    def test_norwegian_thread_output_complete_credit_pricing_and_honest_rounding(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_event(source, "rated", "s", 1, 1_500_000_000, "2026-08-07T08:00:00Z",
                      input_tokens=234_874_570, output_tokens=147_592, cache_read_tokens=227_898_154)
            add_event(source, "unrated", "s", 1, 500_000_000, "2026-08-07T08:01:00Z", model="gpt-5.6-luna")
            add_event(source, "current-cost", "s", 2, 1, "2026-08-07T08:02:00Z")
            report = build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
            output = format_cost_report(report, "thread", {"gpt-5.6-sol": "0.01"}, {"code": "EUR", "usd_rate": "0.9"}, language="nb")
            self.assertIn("Denne chatten hittil (kan omfatte flere dager)", output)
            self.assertIn("**2 modellkall**", output)
            self.assertIn("**2 Scout-credits**", output)
            self.assertIn("Input: **235M tokens**", output)
            self.assertIn("Output: **147 602 tokens**", output)
            self.assertIn("Cache-read: **228M tokens**", output)
            self.assertIn("GPT-5.6 Sol-delen: ca. **USD 0,02 / EUR 0**", output)
            self.assertIn("GPT-5.6 Luna-delen: ca. **USD 0,01 / EUR 0**", output)
            self.assertNotIn("Uten pris", output)
            self.assertIn("Scout-only", output)
            self.assertIn('"/cost FAQ"', output)
            self.assertTrue(output.rstrip().endswith('Vil du vite hvordan du bruker /cost til flere oppgaver? Skriv "/cost FAQ".'))

    def test_period_titles(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_event(source, "past", "s", 1, 1, "2026-08-07T08:00:00Z")
            add_event(source, "current", "s", 2, 1, "2026-08-07T08:01:00Z")
            report = build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
            for period, title in (("last", "Last completed answer"), ("day", "Current chat today"),
                                  ("week", "Current chat this ISO week"), ("month", "Current chat this month")):
                self.assertTrue(format_cost_report(report, period).startswith(title), period)
            self.assertTrue(format_cost_report(report, "all", scope="all").startswith("All locally retained"))
            self.assertTrue(format_cost_report(report, "day", scope="all").startswith("All Scout chats today"))

    def test_cost_faq_is_separate_english_help_with_scope_and_caveats(self):
        output = format_cost_faq()
        self.assertIn("How to use `/cost`", output)
        self.assertIn("`/cost today` — current chat today", output)
        self.assertIn("`/cost all chats today` — all Scout chats today", output)
        self.assertIn("rounded whole credits", output)
        self.assertIn("not a bill", output)
        self.assertNotIn("Slik bruker du", output)

    def test_dashboard_uri_is_an_html_hyperlink_and_normal_report_stays_short(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_event(source, "past", "s", 1, 1, "2026-08-07T08:00:00Z")
            add_event(source, "current", "s", 2, 1, "2026-08-07T08:01:00Z")
            report = build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
            uri = "file:///tmp/Scout%20Usage/dashboard.html"
            output = format_cost_report(report, language="nb", dashboard_uri=uri)
            self.assertIn(f'<a href="{uri}">Usage tracker</a>', output)
            self.assertNotIn(f'>{uri}</a>', output)
            self.assertIn("modellkall**  \n**", output)
            self.assertIn("Scout-credits**  \nInput:", output)
            self.assertIn("tokens**  \nOutput:", output)
            self.assertIn("Sjekk", output)
            self.assertTrue(output.rstrip().endswith('Vil du vite hvordan du bruker /cost til flere oppgaver? Skriv "/cost FAQ".'))
            self.assertNotIn("Slik bruker du `/cost`", output)
            self.assertNotIn("AIU-kontroll", output)
            self.assertNotIn("ikke en faktura", output)

            english = format_cost_report(report, dashboard_uri=uri)
            self.assertTrue(english.rstrip().endswith('Want to learn more ways to use /cost? Type "/cost FAQ".'))
            self.assertTrue(english.rstrip().splitlines()[-1].isascii())
            self.assertNotIn("�", english)

    def test_invalid_json_and_mismatch_statuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_event(source, "bad-json", "s", 1, 1, "2026-08-07T08:00:00Z", raw="not-json")
            add_event(source, "current", "s", 2, 1, "2026-08-07T08:01:00Z")
            self.assertEqual(build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc)).thread.integrity, "warning")
            connection = sqlite3.connect(source)
            connection.execute("UPDATE assistant_usage_events SET token_details_json=? WHERE id='bad-json'", (details(2),)); connection.commit(); connection.close()
            self.assertEqual(build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc)).thread.integrity, "failed")

    @unittest.skipUnless(has_zone("Europe/Oslo"), "IANA timezone data is unavailable")
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
                report = build_cost_report(source, "s", "Europe/Oslo", now=now)
                self.assertEqual(report.chat_today.model_calls, 1)
                self.assertEqual(report.all_today.model_calls, 3)

    def test_snapshot_is_fixed_by_first_read_before_concurrent_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"; create_source(source)
            add_event(source, "initial", "s", 1, 1_000_000_000, "2026-08-07T08:00:00Z")
            add_event(source, "current", "s", 2, 1, "2026-08-07T08:01:00Z")

            def commit_writer():
                add_event(source, "later", "other", 1, 50_000_000_000, "2026-08-07T09:00:00Z")

            report = build_cost_report(source, "s", "UTC", now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc), _after_snapshot=commit_writer)
            self.assertEqual(report.all_today.total_nano_aiu, 1_000_000_000)

    def test_missing_database_schema_session_and_locked_database_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(CostReportError, "not found"):
                build_cost_report(root / "missing.db", "s", "UTC")
            source = root / "source.db"; create_source(source)
            with self.assertRaisesRegex(CostReportError, "no recent Scout usage event"):
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
