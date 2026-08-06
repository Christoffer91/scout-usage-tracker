import json
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from scout_usage_tracker.github_billing import GitHubBillingError, parse_usage, sync_snapshot


PAYLOAD = {"timePeriod": {"year": 2026, "month": 8}, "user": "fictional-user", "usageItems": [{
    "product": "Copilot AI Credits", "sku": "AI Credit", "unitType": "ai-credits",
    "grossQuantity": "2100.5", "discountQuantity": "1500",
    "discountAmount": "15.00", "netAmount": "6.005",
}]}


class GitHubBillingTests(unittest.TestCase):
    def test_parser_persists_only_aggregate_allowlisted_fields(self):
        snapshot = parse_usage(PAYLOAD, "user", 2026, 8, "2026-08-05T10:00:00Z")
        self.assertEqual(snapshot["gross_ai_credits"], 2100.5)
        self.assertNotIn("usageItems", snapshot)
        self.assertNotIn("owner", snapshot)
        self.assertNotIn("model", json.dumps(snapshot, default=str))

    def test_sync_uses_fixed_argv_shell_false_timeout_and_mode_0600(self):
        calls = []
        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, json.dumps(PAYLOAD), "")
        with tempfile.TemporaryDirectory() as temporary, patch("scout_usage_tracker.github_billing.subprocess.run", side_effect=fake_run):
            path = Path(temporary) / "snapshot.json"
            sync_snapshot(path, "user", "fictional-user", 2026, 8)
            argv, kwargs = calls[0]
            self.assertEqual(argv[:9], ["gh", "api", "--hostname", "github.com", "--method", "GET", "-H", "X-GitHub-Api-Version: 2026-03-10", "/users/fictional-user/settings/billing/ai_credit/usage"])
            self.assertIn("year=2026", argv)
            self.assertIn("month=8", argv)
            self.assertIn("product=Copilot", argv)
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["timeout"], 20)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            stored = json.loads(path.read_text())
            self.assertNotIn("fictional-user", path.read_text())
            self.assertNotIn("timePeriod", stored)
            self.assertNotIn("user", stored)
            self.assertEqual(stored["source"], "github")

    def test_failure_preserves_existing_snapshot_byte_identical(self):
        for failure in (
            subprocess.CompletedProcess(["gh"], 1, "", "forbidden secret output"),
            subprocess.TimeoutExpired(["gh"], 20),
            FileNotFoundError("gh"),
        ):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "snapshot.json"; path.write_bytes(b"prior snapshot bytes\n")
                if isinstance(failure, BaseException):
                    side_effect, return_value = failure, None
                else:
                    side_effect, return_value = None, failure
                with patch("scout_usage_tracker.github_billing.subprocess.run", side_effect=side_effect, return_value=return_value):
                    with self.assertRaises(GitHubBillingError):
                        sync_snapshot(path, "user", "fictional", 2026, 8)
                self.assertEqual(path.read_bytes(), b"prior snapshot bytes\n")

    def test_malformed_and_unsupported_responses_preserve_snapshot(self):
        for stdout in ("not json", json.dumps({"usageItems": [{"product": "Copilot"}]}), json.dumps({"items": []})):
            with self.subTest(stdout=stdout), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "snapshot.json"; path.write_bytes(b"prior\n")
                completed = subprocess.CompletedProcess(["gh"], 0, stdout, "")
                with patch("scout_usage_tracker.github_billing.subprocess.run", return_value=completed):
                    with self.assertRaises(GitHubBillingError):
                        sync_snapshot(path, "user", "fictional", 2026, 8)
                self.assertEqual(path.read_bytes(), b"prior\n")

    def test_injection_owner_and_invalid_period_are_rejected_before_subprocess(self):
        with tempfile.TemporaryDirectory() as temporary, patch("scout_usage_tracker.github_billing.subprocess.run") as run:
            for owner in ("bad/name", "--hostname=evil", "bad;echo"):
                with self.assertRaises(GitHubBillingError):
                    sync_snapshot(Path(temporary) / "x", "user", owner, 2026, 8)
            with self.assertRaises(GitHubBillingError):
                sync_snapshot(Path(temporary) / "x", "user", "good", 1999, 8)
            with self.assertRaises(GitHubBillingError):
                sync_snapshot(Path(temporary) / "x", "user", "good", 2026, 13)
            run.assert_not_called()

    def test_org_plan_detection_is_best_effort_after_valid_usage(self):
        responses = [
            subprocess.CompletedProcess(["gh"], 0, json.dumps({"timePeriod": PAYLOAD["timePeriod"], "organization": "fictional-org", "usageItems": PAYLOAD["usageItems"]}), ""),
            subprocess.CompletedProcess(["gh"], 1, "", "denied"),
        ]
        with tempfile.TemporaryDirectory() as temporary, patch("scout_usage_tracker.github_billing.subprocess.run", side_effect=responses):
            path = Path(temporary) / "snapshot.json"
            snapshot = sync_snapshot(path, "organization", "fictional-org", 2026, 8)
            self.assertNotIn("plan_type", snapshot)
            self.assertTrue(path.exists())

    def test_org_plan_detection_can_add_allowlisted_plan_only(self):
        responses = [
            subprocess.CompletedProcess(["gh"], 0, json.dumps({"timePeriod": PAYLOAD["timePeriod"], "organization": "fictional-org", "usageItems": PAYLOAD["usageItems"]}), ""),
            subprocess.CompletedProcess(["gh"], 0, json.dumps({"plan_type": "business", "seat_breakdown": {"total": 99}}), ""),
        ]
        with tempfile.TemporaryDirectory() as temporary, patch("scout_usage_tracker.github_billing.subprocess.run", side_effect=responses):
            path = Path(temporary) / "snapshot.json"
            snapshot = sync_snapshot(path, "organization", "fictional-org", 2026, 8)
            self.assertEqual(snapshot["plan_type"], "business")
            self.assertNotIn("seat_breakdown", path.read_text())

    def test_official_organization_envelope_and_non_copilot_rows(self):
        payload = {
            "timePeriod": {"year": 2026, "month": 8},
            "organization": {"login": "fictional-org", "accountId": 123},
            "usageItems": [
                {"product": "Copilot", "sku": "Copilot AI Credits", "unitType": "credits", "grossQuantity": "10", "discountQuantity": "4", "discountAmount": "0.04", "netAmount": "0.06"},
                {"product": "Actions", "sku": "Actions Linux", "unitType": "minutes", "grossQuantity": "999", "discountQuantity": "0", "discountAmount": "0", "netAmount": "99"},
            ],
        }
        snapshot = parse_usage(payload, "organization", 2026, 8, "2026-08-06T10:00:00Z")
        self.assertEqual(snapshot["gross_ai_credits"], 10)
        self.assertEqual(snapshot["net_amount_usd"], Decimal("0.06"))
        serialized = json.dumps(snapshot, default=str)
        self.assertNotIn("fictional-org", serialized)
        self.assertNotIn("accountId", serialized)
