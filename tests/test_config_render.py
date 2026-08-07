import json
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
from html import unescape
from pathlib import Path

from scout_usage_tracker.config import ConfigError, load_config
from scout_usage_tracker.cli import command_update
from scout_usage_tracker.import_usage import import_usage
from scout_usage_tracker.render import _calendar_window, _credits, _daily_chart, _display_datetime, _money_total, render_dashboard

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
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(config["language"], "en")
        self.assertEqual(config["usd_per_credit"], "0.01")
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

    def test_cost_language_and_global_credit_rate_validation(self):
        path = self.root / "config.json"
        base = {"source_database": "source.db", "history_database": "history.db", "dashboard_path": "dash.html"}
        path.write_text(json.dumps({**base, "language": "no-NO", "usd_per_credit": "0.01"}), encoding="utf-8")
        config = load_config(path)
        self.assertEqual(config["language"], "nb")
        self.assertEqual(config["usd_per_credit"], "0.01")
        for key, value in (("language", "fr"), ("usd_per_credit", "NaN"), ("usd_per_credit", "-1")):
            path.write_text(json.dumps({**base, key: value}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

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
        self.assertNotIn("By anonymized chat", text)
        self.assertNotIn(">By chats</button>", text)
        self.assertIn("`Chat-${index + 1}`", text)
        self.assertIn("&lt;b&gt;bad&lt;/b&gt;", text)
        self.assertIn("Account-wide Copilot total (manual)", text)
        self.assertIn("Account-wide additional usage in USD (manual)", text)
        self.assertIn("&lt;img src=x&gt;", text)
        self.assertIn("never Scout-only", text)
        self.assertIn("<strong>Verified</strong>", text)
        self.assertEqual(dashboard.stat().st_mode & 0o777, 0o600)

    def test_sessions_are_omitted_by_default(self):
        source = self.root / "source.sqlite3"; history = self.root / "history.sqlite3"; dashboard = self.root / "dashboard.html"
        make_source(source, [event()]); import_usage(source, history, b"y" * 32)
        render_dashboard({"history_database": str(history), "dashboard_path": str(dashboard), "timezone": "UTC",
                          "privacy": {"include_sessions": False}, "usd_per_credit_by_model": {}, "usd_to_nok": None})
        self.assertNotIn("By anonymized chat", dashboard.read_text())
        self.assertNotIn(">By chats</button>", dashboard.read_text())

    def test_redesigned_dashboard_is_self_contained_and_accessible(self):
        source = self.root / "source.sqlite3"; history = self.root / "history.sqlite3"; dashboard = self.root / "dashboard.html"
        make_source(source, [event(model="example-model")]); import_usage(source, history, b"u" * 32)
        render_dashboard({"history_database": str(history), "dashboard_path": str(dashboard), "timezone": "UTC",
                          "privacy": {"include_sessions": False},
                          "usd_per_credit_by_model": {"example-model": "0.1"}, "usd_to_nok": "10"})
        text = dashboard.read_text()
        self.assertIn("default-src 'none'", text)
        self.assertIn('id="theme-toggle"', text)
        self.assertIn('aria-pressed="false"', text)
        self.assertIn('role="tablist"', text)
        self.assertIn('role="tab"', text)
        self.assertIn('role="tabpanel"', text)
        self.assertIn('scope="col"', text)
        self.assertIn('scope="row"', text)
        self.assertIn('data-bar-chart', text)
        self.assertIn('data-model-card', text)
        self.assertIn('data-donut', text)
        self.assertIn('data-filter-data=', text)
        self.assertIn('data-model-filter-status', text)
        self.assertIn('aria-pressed="true"', text)
        self.assertIn("Share (%)", text)
        self.assertIn("Credits", text)
        self.assertIn("Show all", text)
        self.assertIn("Filtered view", text)
        self.assertIn("Estimated cost", text)
        self.assertIn("Tool calls", text)
        self.assertNotIn(">Calls<", text)
        self.assertIn("width: 90%", text)
        self.assertIn("minmax(90px, 1fr)", text)
        self.assertIn(".donut.is-hovered", text)
        self.assertIn("perspective(520px)", text)
        self.assertIn("resetDonutLift", text)
        self.assertIn(".donut.is-hovered { transform: none", text)
        self.assertIn('class="expand-row"', text)
        self.assertIn('class="expandable-row"', text)
        self.assertIn('aria-hidden="true">›</span>', text)
        self.assertIn('headingLayout.append(expand, headingText)', text)
        self.assertIn('justify-content: flex-start; gap: 6px', text)
        self.assertNotIn(">Expand</button>", text)
        self.assertIn("makeDrilldownRow", text)
        self.assertIn("drilldownDefinitions", text)
        self.assertIn("@media (max-width: 1200px)", text)
        self.assertIn(".model-legend { width: 100%; }", text)
        self.assertIn(".quiet-row { grid-template-columns: minmax(0, 1fr); }", text)
        self.assertNotIn("title=", text)
        self.assertNotIn('<script src=', text)
        self.assertNotIn('<link rel="stylesheet"', text)
        self.assertEqual(text.count('class="bar-hit"'), 60)
        self.assertEqual(text.count('bar-fill empty'), 59)
        self.assertNotIn("Unusually high day", text.split("<script>", 1)[0])

    def test_model_filter_payload_contains_only_aggregate_usage(self):
        source = self.root / "filter-source.sqlite3"
        history = self.root / "filter-history.sqlite3"
        dashboard = self.root / "filter.html"
        make_source(source, [
            event(identifier=1, session="private-one", model="model-a", total=1_000_000_000),
            event(identifier=2, session="private-two", model="model-b", total=2_000_000_000),
        ])
        import_usage(source, history, b"m" * 32)
        render_dashboard({
            "history_database": str(history), "dashboard_path": str(dashboard), "timezone": "UTC",
            "privacy": {"include_sessions": True}, "usd_per_credit_by_model": {}, "usd_to_nok": None,
        })
        text = dashboard.read_text()
        match = re.search(r'data-filter-data="([^"]+)"', text)
        self.assertIsNotNone(match)
        payload = json.loads(unescape(match.group(1)))
        self.assertEqual([model["id"] for model in payload["models"]], ["model-a", "model-b"])
        self.assertEqual([model["total"]["nano"] for model in payload["models"]], ["1000000000", "2000000000"])
        self.assertEqual(set(payload), {"models", "records", "window_end", "money"})
        self.assertEqual(len(payload["records"]), 2)
        self.assertEqual({record["model"] for record in payload["records"]}, {"model-a", "model-b"})
        self.assertTrue(all(len(record["chat"]) == 12 for record in payload["records"]))
        self.assertNotIn("private-one", text)
        self.assertNotIn("private-two", text)
        serialized = json.dumps(payload)
        for forbidden in ("session_id", "session_digest", "event_time_utc", "source", "prompt", "path"):
            self.assertNotIn(forbidden, serialized)

    def test_plan_billing_card_has_source_scope_freshness_and_invoice_labels(self):
        source = self.root / "billing-source.sqlite3"; history = self.root / "billing-history.sqlite3"; dashboard = self.root / "billing.html"
        snapshot = self.root / "billing-snapshot.json"
        make_source(source, [event(total=2_000_000_000)]); import_usage(source, history, b"b" * 32)
        snapshot.write_text(json.dumps({
            "schema_version": 1, "source": "github", "scope": "user",
            "captured_at": "2026-08-05T10:00:00Z", "year": 2026, "month": 8,
            "gross_ai_credits": "1700", "discount_credits": "1500",
            "discount_amount_usd": "15", "net_amount_usd": "2",
        }), encoding="utf-8")
        render_dashboard({"history_database": str(history), "dashboard_path": str(dashboard), "timezone": "UTC",
                          "privacy": {"include_sessions": False}, "usd_per_credit_by_model": {}, "usd_to_nok": "10",
                          "billing": {"enabled": True, "plan": "pro", "snapshot_path": str(snapshot)},
                          "_generated_at": "2026-08-06T12:00:00Z"})
        text = dashboard.read_text()
        self.assertIn("Plan &amp; billing estimates", text)
        self.assertIn("1,500 monthly credits", text)
        self.assertIn("catalog dated 2026-08-06", text)
        self.assertIn("Personal user billing scope", text)
        self.assertIn("Current billing month", text)
        self.assertIn("Estimated gross Scout value", text)
        self.assertIn("GitHub-reported usage amount; not a final invoice", text)
        self.assertIn("200 credits · USD 2 · NOK 20", text)
        self.assertNotIn("Enable billing and configure billing.plan", text)

    def test_pooled_card_does_not_estimate_user_overage(self):
        source = self.root / "pool-source.sqlite3"; history = self.root / "pool-history.sqlite3"; dashboard = self.root / "pool.html"
        make_source(source, [event(total=9_000_000_000_000)]); import_usage(source, history, b"p" * 32)
        render_dashboard({"history_database": str(history), "dashboard_path": str(dashboard), "timezone": "UTC",
                          "privacy": {"include_sessions": False}, "usd_per_credit_by_model": {}, "usd_to_nok": None,
                          "billing": {"enabled": True, "plan": "business"}, "_generated_at": "2026-08-06T12:00:00Z"})
        text = dashboard.read_text()
        self.assertIn("1,900 per seat, pooled", text)
        self.assertIn("USD 19 per user/seat per month", text)
        self.assertIn("no user overage is estimated", text)
        self.assertNotIn("Estimated additional usage</dt>", text)

    def test_official_credit_rate_prices_unknown_model_in_hero(self):
        source = self.root / "fallback-source.sqlite3"
        history = self.root / "fallback-history.sqlite3"
        dashboard = self.root / "fallback.html"
        make_source(source, [event(total=2_500_000_000_000, model="unpriced-model")])
        import_usage(source, history, b"f" * 32)
        render_dashboard({
            "history_database": str(history), "dashboard_path": str(dashboard), "timezone": "UTC",
            "privacy": {"include_sessions": False}, "usd_per_credit_by_model": {}, "usd_to_nok": None,
            "billing": {"enabled": True, "plan": "unknown"}, "_generated_at": "2026-08-06T12:00:00Z",
        })
        text = dashboard.read_text()
        self.assertIn('Estimated cost</span><strong data-money-total>USD 25', text)
        self.assertIn('<small data-money-note>estimate, not a bill</small>', text)
        self.assertNotIn("— · AI-credit estimate", text)
        self.assertNotIn("Plan &amp; billing estimates", text)
        self.assertNotIn("Plan source", text)
        self.assertNotIn("Included allowance", text)
        self.assertNotIn("Subscription price context", text)
        self.assertNotIn("Enable billing", text)
        self.assertNotIn("Estimated additional usage</dt>", text)

    def test_sparse_daily_usage_fills_fixed_calendar_window(self):
        daily = [
            {"label": "2025-01-01", "credits": Decimal("1")},
            {"label": "2025-01-03", "credits": Decimal("3")},
        ]
        window = _calendar_window(daily, 3)
        self.assertEqual([row["label"] for row in window], ["2025-01-01", "2025-01-02", "2025-01-03"])
        self.assertEqual([row["credits"] for row in window], [Decimal("1"), Decimal("0"), Decimal("3")])

    def test_displayed_credits_are_whole_numbers(self):
        self.assertEqual(_credits(Decimal("8366.706885")), "8,367")
        self.assertEqual(_credits(Decimal("130.06365")), "130")
        self.assertEqual(_money_total(Decimal("283.5504"), "USD"), "USD 284")
        self.assertEqual(_money_total(Decimal("2639.30"), "NOK"), "NOK 2,639")

    def test_visible_dates_are_human_readable_and_local(self):
        self.assertEqual(_display_datetime("2026-08-05T12:01:08.565Z", "Europe/Oslo"), "5 Aug 2026, 14:01")
        self.assertEqual(_display_datetime("2026-02-03", "UTC"), "3 Feb 2026")
        self.assertEqual(_display_datetime("fictional", "UTC"), "fictional")

    def test_spike_detection_requires_fifteen_usage_days(self):
        daily = [
            {"label": f"2025-01-{day:02d}", "credits": Decimal("1")}
            for day in range(1, 15)
        ]
        self.assertNotIn("Unusually high day", _daily_chart(daily))
        daily.append({"label": "2025-01-15", "credits": Decimal("100")})
        chart = _daily_chart(daily)
        self.assertIn("Unusually high day", chart)
        self.assertIn("unusually high", chart)

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
            self.assertIn("<strong>Review recommended</strong>", text)
            self.assertNotIn(f"<strong>{expected}</strong>", text)
            self.assertNotIn("Status counts", text)

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
        self.assertIn("<strong>Review recommended</strong>", dashboard.read_text())
        self.assertNotIn("<strong>INCOMPLETE</strong>", dashboard.read_text())

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
        self.assertIn("<strong>Review recommended</strong>", dashboard.read_text())
        self.assertNotIn("<strong>INCOMPLETE</strong>", dashboard.read_text())

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
