import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scout_usage_tracker.config import ConfigError, load_config
from scout_usage_tracker.cli import command_update
from scout_usage_tracker.import_usage import import_usage
from scout_usage_tracker.render import render_dashboard

from tests.helpers import event, make_source


class ConfigRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_config_migration_preserves_mode(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"sourceDatabase": "source.db", "historyDatabase": "history.db", "dashboardPath": "dash.html", "includeSessions": True}), encoding="utf-8")
        path.chmod(0o640)
        config = load_config(path)
        migrated = json.loads(path.read_text())
        self.assertEqual(migrated["schema_version"], 1)
        self.assertTrue(config["privacy"]["include_sessions"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_actual_legacy_pricing_and_account_snapshot_migrate(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({
            "sourceDatabase": "source.db", "historyDatabase": "history.db", "dashboardPath": "dash.html",
            "estimatedUsdPerCredit": {"legacy-model": "0.25"},
            "accountWideSnapshot": {"credits": "42.5", "capturedAt": "2026-02-03", "scope": "account-wide/manual"},
            "additionalUsageUsd": "7.75", "pricingSource": "presentation only", "currency": "USD"
        }), encoding="utf-8")
        config = load_config(path)
        self.assertEqual(config["usd_per_credit_by_model"], {"legacy-model": "0.25"})
        self.assertEqual(config["account_comparison"], {
            "total": "42.5", "additional_usage_usd": "7.75", "as_of": "2026-02-03", "scope": "account-wide/manual"
        })
        migrated = json.loads(path.read_text())
        for removed in ("estimatedUsdPerCredit", "accountWideSnapshot", "additionalUsageUsd", "pricingSource", "currency"):
            self.assertNotIn(removed, migrated)

    def test_manual_comparison_validation_rejects_unsafe_shape(self):
        path = self.root / "config.json"
        base = {"source_database": "source.db", "history_database": "history.db", "dashboard_path": "dash.html"}
        path.write_text(json.dumps({**base, "account_comparison": {"total": "1", "scope": ""}}), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)
        path.write_text(json.dumps({**base, "account_comparison": {"total": "1", "scope": "manual", "scout_only": True}}), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_invalid_migration_rolls_back_without_write(self):
        path = self.root / "config.json"
        original = '{"sourceDatabase":"x"}'
        path.write_text(original, encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)
        self.assertEqual(path.read_text(), original)

    def test_path_collisions_and_symlink_aliases_are_rejected_without_source_write(self):
        source = self.root / "source.sqlite3"
        make_source(source, [event()])
        before = source.read_bytes()
        for history_value in (str(source), str(self.root / "source-alias.sqlite3")):
            alias = Path(history_value)
            if alias != source:
                alias.symlink_to(source)
            config_path = self.root / ("same.json" if alias == source else "alias.json")
            config_path.write_text(json.dumps({
                "source_database": str(source), "history_database": str(alias),
                "dashboard_path": str(self.root / (config_path.stem + ".html")), "timezone": "UTC"
            }), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path)
            self.assertEqual(source.read_bytes(), before)
            connection = sqlite3.connect(source)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(assistant_usage_events)")}
            connection.close()
            self.assertIn("total_nano_aiu", columns)

    def test_html_escapes_values_and_session_opt_in(self):
        source = self.root / "source.sqlite3"
        history = self.root / "history.sqlite3"
        dashboard = self.root / "dashboard.html"
        make_source(source, [event(session="raw-secret-session", model='<script>alert("x")</script>')])
        import_usage(source, history, b"x" * 32)
        config = {"history_database": str(history), "dashboard_path": str(dashboard),
                  "timezone": "UTC", "privacy": {"include_sessions": True},
                  "usd_per_credit_by_model": {}, "usd_to_nok": None,
                  "account_comparison": {"total": "<b>bad</b>", "additional_usage_usd": "<img src=x>", "scope": "account-wide/manual", "as_of": "fictional"},
                  "_generated_at": "2025-01-01T00:00:00+00:00"}
        render_dashboard(config)
        text = dashboard.read_text()
        self.assertIn("&lt;script&gt;alert", text)
        self.assertNotIn('<script>alert("x")</script>', text)
        self.assertNotIn("raw-secret-session", text)
        self.assertIn("By anonymized session", text)
        self.assertIn("&lt;b&gt;bad&lt;/b&gt;", text)
        self.assertIn("Account-wide Copilot total (manual)", text)
        self.assertIn("Account-wide additional usage in USD (manual)", text)
        self.assertIn("&lt;img src=x&gt;", text)
        self.assertIn("never Scout-only", text)
        self.assertIn("<strong>PASS</strong>", text)
        self.assertEqual(dashboard.stat().st_mode & 0o777, 0o600)

    def test_sessions_are_omitted_by_default(self):
        source = self.root / "source.sqlite3"; history = self.root / "history.sqlite3"; dashboard = self.root / "dashboard.html"
        make_source(source, [event()]); import_usage(source, history, b"y" * 32)
        render_dashboard({"history_database": str(history), "dashboard_path": str(dashboard), "timezone": "UTC",
                          "privacy": {"include_sessions": False}, "usd_per_credit_by_model": {}, "usd_to_nok": None})
        self.assertNotIn("By anonymized session", dashboard.read_text())

    def test_verification_overall_mismatch_and_incomplete(self):
        for name, details, total, expected in (
            ("mismatch", None, 21, "MISMATCH"),
            ("incomplete", "", 20, "INCOMPLETE"),
        ):
            source = self.root / f"{name}-source.sqlite3"
            history = self.root / f"{name}-history.sqlite3"
            dashboard = self.root / f"{name}.html"
            make_source(source, [event(total=total, details=details)])
            if name == "incomplete":
                connection = sqlite3.connect(source)
                connection.execute("UPDATE assistant_usage_events SET token_details_json='' WHERE id=1")
                connection.commit(); connection.close()
            import_usage(source, history, b"z" * 32)
            render_dashboard({"history_database": str(history), "dashboard_path": str(dashboard), "timezone": "UTC",
                              "privacy": {"include_sessions": False}, "usd_per_credit_by_model": {}, "usd_to_nok": None})
            text = dashboard.read_text()
            self.assertIn(f"<strong>{expected}</strong>", text)
            self.assertIn("Status counts", text)

    def test_skipped_row_makes_render_and_cli_incomplete(self):
        source = self.root / "source.sqlite3"
        history = self.root / "history.sqlite3"
        dashboard = self.root / "dashboard.html"
        invalid = list(event(identifier=2)); invalid[3] = -1
        make_source(source, [event(identifier=1), tuple(invalid)])
        config = {"source_database": str(source), "history_database": str(history), "dashboard_path": str(dashboard),
                  "timezone": "UTC", "privacy": {"include_sessions": False}, "usd_per_credit_by_model": {}, "usd_to_nok": None}
        output = StringIO()
        with redirect_stdout(output):
            code = command_update(config)
        self.assertEqual(code, 0)
        self.assertTrue(output.getvalue().startswith("INCOMPLETE "))
        self.assertIn("skipped=1", output.getvalue())
        self.assertIn("possible_gap=false", output.getvalue())
        self.assertIn("<strong>INCOMPLETE</strong>", dashboard.read_text())

    def test_gap_only_makes_render_and_cli_incomplete(self):
        source = self.root / "gap-source.sqlite3"
        history = self.root / "gap-history.sqlite3"
        dashboard = self.root / "gap-dashboard.html"
        make_source(source, [event(identifier=4)])
        config = {"source_database": str(source), "history_database": str(history), "dashboard_path": str(dashboard),
                  "timezone": "UTC", "privacy": {"include_sessions": False}, "usd_per_credit_by_model": {}, "usd_to_nok": None}
        output = StringIO()
        with redirect_stdout(output):
            code = command_update(config)
        self.assertEqual(code, 0)
        self.assertTrue(output.getvalue().startswith("INCOMPLETE "))
        self.assertIn("skipped=0", output.getvalue())
        self.assertIn("possible_gap=true", output.getvalue())
        self.assertIn("<strong>INCOMPLETE</strong>", dashboard.read_text())

    def test_cli_pass_is_reserved_for_fully_verified_events(self):
        for name, total, expected in (("verified", 20, "PASS "), ("mismatch", 21, "INCOMPLETE ")):
            source = self.root / f"cli-{name}-source.sqlite3"
            history = self.root / f"cli-{name}-history.sqlite3"
            dashboard = self.root / f"cli-{name}.html"
            make_source(source, [event(total=total)])
            config = {"source_database": str(source), "history_database": str(history), "dashboard_path": str(dashboard),
                      "timezone": "UTC", "privacy": {"include_sessions": False}, "usd_per_credit_by_model": {}, "usd_to_nok": None}
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(command_update(config), 0)
            self.assertTrue(output.getvalue().startswith(expected), output.getvalue())
