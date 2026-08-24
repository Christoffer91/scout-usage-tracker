import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from scout_usage_tracker.billing import BillingError, PLAN_CATALOG, billing_summary, validate_snapshot
from scout_usage_tracker.config import ConfigError, load_config


class BillingTests(unittest.TestCase):
    def test_catalog_and_personal_estimates(self):
        self.assertEqual(PLAN_CATALOG["pro"]["included_credits"], Decimal("1500"))
        self.assertEqual(PLAN_CATALOG["pro_plus"]["included_credits"], Decimal("7000"))
        self.assertEqual(PLAN_CATALOG["pro_plus"]["monthly_price_usd"], Decimal("39"))
        self.assertEqual(PLAN_CATALOG["max"]["included_credits"], Decimal("20000"))
        self.assertEqual(PLAN_CATALOG["max"]["monthly_price_usd"], Decimal("100"))
        summary = billing_summary(
            {"billing": {"enabled": True, "plan": "pro"}, "secondary_currency": {"code": "EUR", "usd_rate": "0.9"}},
            Decimal("25"), now=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["included_credits"], Decimal("1500"))
        self.assertEqual(summary["estimated_gross_scout_usd"], Decimal("0.25"))
        self.assertEqual(summary["secondary_currency_code"], "EUR")
        self.assertEqual(summary["estimated_gross_scout_secondary"], Decimal("0.225"))
        self.assertIsNone(summary["estimated_additional_credits"])

    def test_free_and_unknown_allowances_are_not_guessed(self):
        for plan in ("free", "unknown"):
            summary = billing_summary({"billing": {"enabled": True, "plan": plan}}, Decimal("99"))
            self.assertIsNone(summary["included_credits"])
            self.assertIsNone(summary["estimated_additional_credits"])

    def test_pooled_plan_never_allocates_allowance_to_scout_user(self):
        summary = billing_summary(
            {"billing": {"enabled": True, "plan": "business", "seat_count": 3}}, Decimal("9999"),
            now=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        self.assertTrue(summary["pooled"])
        self.assertEqual(summary["effective_allowance"], Decimal("5700"))
        self.assertIsNone(summary["estimated_additional_credits"])

    def test_matching_manual_monthly_snapshot_computes_only_entity_overage(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot_path = Path(temporary) / "snapshot.json"
            snapshot_path.write_text(json.dumps({
                "schema_version": 1, "source": "manual", "scope": "organization",
                "captured_at": "2026-08-05T10:00:00Z", "year": 2026, "month": 8,
                "gross_ai_credits": "8000", "discount_credits": "5700",
                "discount_amount_usd": "57", "net_amount_usd": "23",
            }), encoding="utf-8")
            summary = billing_summary({
                "billing": {"enabled": True, "plan": "business", "seat_count": 3, "snapshot_path": str(snapshot_path)},
                "secondary_currency": {"code": "EUR", "usd_rate": "0.9"},
            }, Decimal("10"), now=datetime(2026, 8, 10, tzinfo=timezone.utc))
            self.assertEqual(summary["estimated_additional_credits"], Decimal("2300"))
            self.assertEqual(summary["estimated_additional_usd"], Decimal("23.00"))
            self.assertEqual(summary["estimated_additional_secondary"], Decimal("20.700"))

    def test_mismatched_period_or_scope_never_computes_overage(self):
        snapshot = {
            "schema_version": 1, "source": "manual", "scope": "user",
            "captured_at": "2026-07-05T10:00:00Z", "year": 2026, "month": 7,
            "gross_ai_credits": "2000", "discount_credits": "1500",
            "discount_amount_usd": "15", "net_amount_usd": "5",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.json"; path.write_text(json.dumps(snapshot), encoding="utf-8")
            summary = billing_summary({"billing": {"enabled": True, "plan": "pro", "snapshot_path": str(path)}}, Decimal("3"),
                                      now=datetime(2026, 8, 1, tzinfo=timezone.utc))
            self.assertIsNone(summary["estimated_additional_credits"])

    def test_snapshot_and_config_validation_reject_unsafe_shapes(self):
        with self.assertRaises(BillingError):
            validate_snapshot({"schema_version": 1, "source": "manual", "scope": "user", "owner": "secret"})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            base = {"source_database": "s", "history_database": "h", "dashboard_path": "d"}
            path.write_text(json.dumps({**base, "schema_version": 1, "billing": {"plan": "pro+", "snapshot_path": "billing.json"}}), encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config["billing"]["plan"], "pro_plus")
            self.assertEqual(config["schema_version"], 5)
            self.assertEqual(Path(config["billing"]["snapshot_path"]).resolve(), (Path(temporary) / "billing.json").resolve())
            path.write_text(json.dumps({**base, "billing": {"plan": "pro", "seat_count": True}}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)
            for bad_exchange in ("NaN", "Infinity", "-1"):
                path.write_text(json.dumps({**base, "secondary_currency": {"code": "EUR", "usd_rate": bad_exchange}}), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(path)
            path.write_text(json.dumps({**base, "billing": {"snapshot_path": "s"}}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_snapshot_path_cannot_alias_hmac_secret_directly_or_through_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "runtime" / "history.sqlite3"
            history.parent.mkdir()
            secret = history.parent / "hmac-secret"
            secret.write_bytes(b"unchanged-secret")
            alias = root / "secret-alias"
            alias.symlink_to(secret)
            config_path = root / "config.json"
            base = {
                "schema_version": 2,
                "source_database": str(root / "source.db"),
                "history_database": str(history),
                "dashboard_path": str(root / "dashboard.html"),
                "billing": {"enabled": True, "plan": "pro"},
            }
            for snapshot_path in (secret, alias):
                with self.subTest(snapshot_path=snapshot_path):
                    config_path.write_text(json.dumps({
                        **base,
                        "billing": {**base["billing"], "snapshot_path": str(snapshot_path)},
                    }), encoding="utf-8")
                    with self.assertRaises(ConfigError):
                        load_config(config_path)
                    self.assertEqual(secret.read_bytes(), b"unchanged-secret")

    def test_plan_type_is_limited_to_github_organization_snapshots(self):
        base = {
            "schema_version": 1,
            "captured_at": "2026-08-06T10:00:00Z",
            "year": 2026,
            "month": 8,
            "gross_ai_credits": "10",
            "discount_credits": "5",
            "discount_amount_usd": "0.05",
            "net_amount_usd": "0.05",
            "plan_type": "business",
        }
        valid = validate_snapshot({**base, "source": "github", "scope": "organization"})
        self.assertEqual(valid["plan_type"], "business")
        for source, scope in (("manual", "organization"), ("github", "user"), ("github", "enterprise")):
            with self.subTest(source=source, scope=scope), self.assertRaises(BillingError):
                validate_snapshot({**base, "source": source, "scope": scope})

    def test_promotional_allowance_requires_explicit_selection(self):
        standard = billing_summary({"billing": {"enabled": True, "plan": "enterprise", "seat_count": 2}}, Decimal(0))
        promo = billing_summary({"billing": {"enabled": True, "plan": "enterprise", "seat_count": 2, "promotional_allowance": True}}, Decimal(0),
                                now=datetime(2026, 8, 1, tzinfo=timezone.utc))
        self.assertEqual(standard["effective_allowance"], Decimal("7800"))
        self.assertEqual(promo["effective_allowance"], Decimal("14000"))
        self.assertEqual(promo["allowance_source"], "explicit promotional eligibility")
        with self.assertRaises(BillingError):
            billing_summary({"billing": {"enabled": True, "plan": "enterprise", "promotional_allowance": True}}, Decimal(0),
                            now=datetime(2026, 9, 1, tzinfo=timezone.utc))

    def test_disabled_billing_ignores_plan_and_snapshot(self):
        summary = billing_summary({"billing": {"enabled": False, "plan": "pro", "snapshot_path": "/does/not/exist"}}, Decimal("10"))
        self.assertEqual(summary["plan"], "unknown")
        self.assertIsNone(summary["snapshot"])
        self.assertIsNone(summary["estimated_gross_scout_usd"])
