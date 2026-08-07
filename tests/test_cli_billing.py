import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scout_usage_tracker.cli import build_parser, command_cost, command_github_sync, main
from tests.helpers import event, make_source


class CliBillingTests(unittest.TestCase):
    def test_cost_delegates_missing_session_to_safe_autodetection(self):
        output = StringIO()
        config = {"source_database": "/not/read", "timezone": "UTC", "usd_per_credit_by_model": {}, "usd_to_nok": None}
        with patch.dict("os.environ", {}, clear=True), \
             patch("scout_usage_tracker.cli.build_cost_report", return_value=object()) as build, \
             patch("scout_usage_tracker.cli.format_cost_report", return_value="ok"), redirect_stdout(output):
            self.assertEqual(command_cost(config), 0)
        build.assert_called_once_with("/not/read", "", "UTC")
        self.assertEqual(output.getvalue().strip(), "ok")

    def test_github_sync_arguments_are_command_specific(self):
        args = build_parser().parse_args(["github-sync", "--scope", "organization", "--owner", "fictional-org", "--year", "2026", "--month", "8"])
        self.assertEqual((args.command, args.scope, args.owner, args.year, args.month), ("github-sync", "organization", "fictional-org", 2026, 8))
        for ordinary in ("update", "render", "status", "open", "cost"):
            args = build_parser().parse_args([ordinary])
            self.assertFalse(hasattr(args, "owner"))

    def test_cost_period_is_command_specific(self):
        self.assertIsNone(build_parser().parse_args(["cost"]).period)
        self.assertEqual(build_parser().parse_args(["cost", "--period", "month"]).period, "month")
        args = build_parser().parse_args(["cost", "--scope", "all", "--period", "day", "--language", "nb", "--faq"])
        self.assertEqual((args.scope, args.period, args.language, args.faq), ("all", "day", "nb", True))

    def test_cost_defaults_to_chat_but_all_scope_defaults_to_all_history(self):
        config = {"source_database": "/not/read", "timezone": "UTC", "language": "en",
                  "usd_per_credit": "0.01", "usd_per_credit_by_model": {}, "usd_to_nok": None}
        with patch.dict("os.environ", {"SESSION_ID": "s"}, clear=True), \
             patch("scout_usage_tracker.cli.build_cost_report", return_value=object()), \
             patch("scout_usage_tracker.cli.format_cost_report", return_value="ok") as render, \
             redirect_stdout(StringIO()):
            command_cost(config)
            self.assertEqual(render.call_args.args[1], "thread")
            command_cost(config, scope="all")
            self.assertEqual(render.call_args.args[1], "all")

    def test_config_path_is_accepted_before_or_after_every_subcommand(self):
        config_path = "/tmp/fictional-scout-config.json"
        legacy = ("update", "refresh", "render", "status", "open", "cost")
        for command in legacy:
            with self.subTest(command=command, position="before"):
                args = build_parser().parse_args(["--config", config_path, command])
                self.assertEqual(args.config, config_path)
            with self.subTest(command=command, position="after"):
                args = build_parser().parse_args([command, "--config", config_path])
                self.assertEqual(args.config, config_path)
        sync_args = ["--scope", "user", "--owner", "fictional"]
        before = build_parser().parse_args(["--config", config_path, "github-sync", *sync_args])
        after = build_parser().parse_args(["github-sync", *sync_args, "--config", config_path])
        self.assertEqual(before.config, config_path)
        self.assertEqual(after.config, config_path)

    def test_explicit_sync_calls_adapter_and_does_not_echo_owner(self):
        snapshot = {"scope": "user", "year": 2026, "month": 8}
        output = StringIO()
        with tempfile.TemporaryDirectory() as temporary, patch("scout_usage_tracker.github_billing.sync_snapshot", return_value=snapshot) as sync, redirect_stdout(output):
            config = {"billing": {"snapshot_path": str(Path(temporary) / "billing.json")}}
            self.assertEqual(command_github_sync(config, "user", "private-owner", 2026, 8), 0)
        sync.assert_called_once()
        self.assertNotIn("private-owner", output.getvalue())
        self.assertNotIn(temporary, output.getvalue())

    def test_missing_snapshot_path_fails_before_adapter(self):
        error = StringIO()
        with patch("scout_usage_tracker.github_billing.sync_snapshot") as sync, redirect_stderr(error):
            self.assertEqual(command_github_sync({"billing": {}}, "user", "fictional", 2026, 8), 2)
        sync.assert_not_called()
        self.assertIn("snapshot_path", error.getvalue())

    def test_ordinary_commands_never_call_github_adapter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, history, dashboard, config_path = root / "source.db", root / "history.db", root / "dashboard.html", root / "config.json"
            make_source(source, [event()])
            config_path.write_text(json.dumps({
                "schema_version": 2, "source_database": str(source), "history_database": str(history),
                "dashboard_path": str(dashboard), "timezone": "UTC", "privacy": {"include_sessions": False},
                "billing": {"enabled": True, "plan": "pro", "snapshot_path": str(root / "billing.json")},
            }), encoding="utf-8")
            config_path.chmod(0o600)
            with patch("scout_usage_tracker.github_billing._run_gh") as network, patch("scout_usage_tracker.cli.subprocess.run"), redirect_stdout(StringIO()):
                for command in ("update", "render", "status"):
                    self.assertIn(main(["--config", str(config_path), command]), (0, 2))
                self.assertEqual(main(["--config", str(config_path), "open"]), 0)
            network.assert_not_called()
