import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, time, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scout_usage_tracker.aggregate import aggregate, local_zone
from scout_usage_tracker.cli import command_open
from scout_usage_tracker.config import ConfigError
from scout_usage_tracker.cost_report import build_cost_report, format_cost_report
from scout_usage_tracker.platform_support import TimezoneDataError, sqlite_readonly_uri, timezone_for
from tests.test_cost_report import add_event, create_source


ROOT = Path(__file__).resolve().parents[1]


class WindowsPortableTests(unittest.TestCase):
    def test_posix_local_timezone_invalid_absolute_tz_keys_fall_back(self):
        for configured in ("/etc/localtime", "/synthetic/not-a-zoneinfo-key"):
            with self.subTest(configured=configured), \
                    patch.dict(os.environ, {"TZ": configured}), \
                    patch("scout_usage_tracker.platform_support.os.name", "posix"), \
                    patch("scout_usage_tracker.platform_support.Path.resolve", return_value=Path("/usr/share/zoneinfo/Etc/UTC")), \
                    patch("scout_usage_tracker.platform_support.ZoneInfo") as zone_info:
                zone_info.side_effect = lambda key: (_ for _ in ()).throw(ValueError("absolute key")) \
                    if key == configured else timezone.utc
                self.assertIs(timezone_for("local"), timezone.utc)
                self.assertEqual([call.args[0] for call in zone_info.call_args_list], [configured, "Etc/UTC"])

    def test_explicit_invalid_absolute_timezone_is_actionable(self):
        with patch("scout_usage_tracker.platform_support.ZoneInfo", side_effect=ValueError("absolute key")):
            with self.assertRaisesRegex(TimezoneDataError, "install timezone data or use local"):
                timezone_for("/synthetic/not-a-zoneinfo-key")

    def test_sqlite_uri_encodes_windows_sensitive_path_and_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "Scout space-æ#%-usage.sqlite3"
            sqlite3.connect(source).close()
            uri = sqlite_readonly_uri(source)
            self.assertTrue(uri.startswith("file:///"))
            self.assertNotIn("\\", uri)
            for encoded in ("%20", "%23", "%25", "%C3%A6"):
                self.assertIn(encoded, uri)
            connection = sqlite3.connect(uri, uri=True)
            connection.execute("PRAGMA query_only=ON")
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forbidden(value)")
            connection.close()

    def test_local_timezone_is_per_instant_and_preserves_iso_year(self):
        zone = timezone_for("local")
        for instant in (
            datetime(2026, 1, 15, 12, tzinfo=timezone.utc),
            datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
        ):
            observed = instant.astimezone(zone)
            native = instant.astimezone()
            self.assertEqual((observed.date(), observed.utcoffset()), (native.date(), native.utcoffset()))

        local_instant = datetime(2024, 12, 30, 0, 30).astimezone(timezone.utc)
        row = {
            "event_time_utc": local_instant.isoformat(), "model": "synthetic",
            "total_nano_aiu": 1, "input_tokens": 1, "output_tokens": 1,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "reasoning_tokens": 0, "verification_status": "verified",
            "session_digest": "a" * 64,
        }
        result = aggregate([row], "local")
        self.assertEqual(result["groups"]["week"][0]["label"], "2025-W01")

    def test_local_timezone_preserves_both_folds_across_dst_rollback(self):
        start = datetime(datetime.now().year, 1, 1, tzinfo=timezone.utc)
        previous = start.astimezone().utcoffset()
        transition = None
        for hour in range(1, 24 * 367):
            sample = start + timedelta(hours=hour)
            current = sample.astimezone().utcoffset()
            if current < previous:
                window = sample - timedelta(hours=1)
                before_offset = window.astimezone().utcoffset()
                for minute in range(1, 61):
                    candidate = window + timedelta(minutes=minute)
                    if candidate.astimezone().utcoffset() < before_offset:
                        transition = candidate
                        break
                break
            previous = current
        if transition is None:
            self.skipTest("system timezone has no DST rollback transition this year")

        rollback = (transition - timedelta(minutes=1)).astimezone().utcoffset() - transition.astimezone().utcoffset()
        fraction = timedelta(microseconds=654321)
        fold_zero_utc = transition - rollback / 2 + fraction
        fold_one_utc = transition + rollback / 2 + fraction
        instants = (
            transition - rollback - timedelta(minutes=1) + fraction,
            fold_zero_utc,
            transition - timedelta(minutes=1) + fraction,
            transition + fraction,
            fold_one_utc,
            transition + rollback + fraction,
        )
        zone = timezone_for("local")
        converted = []
        for instant in instants:
            observed = instant.astimezone(zone)
            native = instant.astimezone()
            self.assertEqual(
                (observed.replace(tzinfo=None), observed.utcoffset()),
                (native.replace(tzinfo=None), native.utcoffset()),
            )
            self.assertEqual(observed.microsecond, 654321)
            self.assertEqual(observed.astimezone(timezone.utc), instant)
            converted.append(observed)

        fold_zero, fold_one = converted[1], converted[4]
        self.assertEqual(fold_zero.replace(tzinfo=None), fold_one.replace(tzinfo=None))
        self.assertEqual((fold_zero.fold, fold_one.fold), (0, 1))
        repeated_wall = fold_zero.replace(tzinfo=None)
        self.assertEqual(repeated_wall.replace(tzinfo=zone, fold=0).astimezone(timezone.utc), fold_zero_utc)
        self.assertEqual(repeated_wall.replace(tzinfo=zone, fold=1).astimezone(timezone.utc), fold_one_utc)

        def usage_row(identifier, instant):
            return {
                "event_time_utc": instant.isoformat(), "model": identifier,
                "total_nano_aiu": 1, "input_tokens": 1, "output_tokens": 1,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "reasoning_tokens": 0, "verification_status": "verified",
                "session_digest": identifier * 64,
            }

        grouped = aggregate([
            usage_row("a", fold_zero_utc), usage_row("b", fold_one_utc),
        ], "local")["groups"]
        self.assertEqual(len(grouped["day"]), 1)
        self.assertEqual(len(grouped["week"]), 1)
        self.assertEqual(len(grouped["month"]), 1)

    def test_cost_local_uses_local_naive_day_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.db"
            create_source(source)
            local_now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
            start = datetime.combine(local_now.date(), time.min).astimezone(timezone.utc)
            add_event(source, "inside", "s", 1, 1, (start.replace(microsecond=0)).isoformat())
            add_event(source, "current", "s", 2, 1, (start.replace(microsecond=0)).isoformat())
            report = build_cost_report(source, "s", "local", now=local_now.astimezone(timezone.utc))
            self.assertEqual(report.chat_today.model_calls, 1)
            self.assertIn("Usage tracker", format_cost_report(report, dashboard_uri="file:///safe/dashboard.html"))

    def test_unavailable_explicit_iana_zone_has_actionable_config_error(self):
        with self.assertRaisesRegex(ConfigError, "install timezone data or use local"):
            local_zone("Fictional/Unavailable")

    def test_dashboard_open_uses_native_windows_api_or_shell_false_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            dashboard = Path(temporary) / "dashboard with spaces.html"
            dashboard.write_text("ok", encoding="utf-8")
            if os.name == "nt":
                with patch("scout_usage_tracker.cli.os.startfile", create=True) as startfile, patch("scout_usage_tracker.cli.subprocess.run") as run:
                    self.assertEqual(command_open({"dashboard_path": str(dashboard)}), 0)
                startfile.assert_called_once_with(str(dashboard.resolve()))
                run.assert_not_called()
            else:
                with patch("scout_usage_tracker.cli.platform.system", return_value="Linux"), patch("scout_usage_tracker.cli.subprocess.run") as run:
                    self.assertEqual(command_open({"dashboard_path": str(dashboard)}), 0)
                run.assert_called_once_with(["xdg-open", str(dashboard)], check=True, shell=False)

    @unittest.skipUnless(os.name == "nt", "Windows native open error handling")
    def test_dashboard_open_error_does_not_reveal_local_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            dashboard = Path(temporary) / "dashboard.html"
            dashboard.write_text("ok", encoding="utf-8")
            sentinel = r"C:\Synthetic-Redaction-Sentinel\dashboard.html"
            error = StringIO()
            with patch("scout_usage_tracker.cli.os.startfile", side_effect=OSError(sentinel), create=True), redirect_stderr(error):
                self.assertEqual(command_open({"dashboard_path": str(dashboard)}), 2)
            self.assertEqual(error.getvalue().strip(), "Could not open dashboard.")
            self.assertNotIn(sentinel, error.getvalue())
            self.assertNotIn(temporary, error.getvalue())

    def test_cost_skill_has_platform_specific_commands_and_private_link_contract(self):
        text = (ROOT / "skills/cost/SKILL.md").read_text(encoding="utf-8")
        windows_launcher = r'& "$env:USERPROFILE\.local\bin\scout-usage.cmd"'
        self.assertIn(windows_launcher, text)
        self.assertNotIn("%USERPROFILE%", text)
        self.assertIn("${HOME}/.local/bin/scout-usage", text)
        for phrase in ("--period thread", "--period last", "--scope all --period day", "--faq"):
            self.assertGreaterEqual(text.count(phrase), 2)
        self.assertEqual(text.count("--dashboard-link loopback"), 10)
        for line in text.splitlines():
            if line.startswith("${HOME}/.local/bin/scout-usage cost"):
                self.assertNotIn("--dashboard-link loopback", line)
        self.assertIn(r'& "$env:USERPROFILE\.local\bin\scout-usage.cmd" open', text)
        self.assertIn("${HOME}/.local/bin/scout-usage open", text)
        self.assertIn("`Usage tracker`", text)
        self.assertIn("Scout can reject local", text)
        self.assertIn("Never display the launcher path or dashboard path", text)
        self.assertIn("never show the selected launcher path", text.lower())

    def test_installer_source_scopes_skill_validation_and_preflights_uninstall(self):
        text = (ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn('$InstallRoots = @($InstallRoot, $BinRoot, $ConfigRoot)', text)
        self.assertIn('if ($InstallScoutSkill) { $InstallRoots += @($ScoutSkillRoot, $CopilotSkillRoot) }', text)
        self.assertIn('$ownedSkillRoots = @(Get-OwnedSkillDeletionRoots)', text)
        self.assertIn('Assert-UninstallPreflight $ownedSkillRoots', text)
        self.assertLess(text.index('Assert-UninstallPreflight $ownedSkillRoots'), text.index('Remove-FileIfPresent $Launcher'))
        self.assertLess(text.index('Test-SkillPathContainsReparsePoint $root'), text.index('Join-Path $root $OwnerMarkerName'))
        self.assertIn('if ($Action -eq "uninstall" -or $InstallScoutSkill)', text)
        self.assertIn('Assert-SkillRootsDoNotOverlapCoreDeletionPaths', text)
        self.assertLess(
            text.index('Assert-SkillRootsDoNotOverlapCoreDeletionPaths\n    $ownedSkillRoots'),
            text.index('$ownedSkillRoots = @(Get-OwnedSkillDeletionRoots)'),
        )

    @unittest.skipUnless(os.name == "nt", "PowerShell launcher selection is Windows-specific")
    def test_cost_skill_powershell_launcher_handles_spaced_userprofile(self):
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not powershell.is_file():
            self.skipTest("Windows PowerShell 5.1 is unavailable")
        command = r'& "$env:USERPROFILE\.local\bin\scout-usage.cmd" cost --scope chat --period thread'
        skill = (ROOT / "skills/cost/SKILL.md").read_text(encoding="utf-8")
        self.assertIn(command, skill)
        with tempfile.TemporaryDirectory(prefix="Scout User With Spaces ") as temporary:
            profile = Path(temporary)
            launcher = profile / ".local/bin/scout-usage.cmd"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("@echo off\r\necho %*\r\n", encoding="utf-8")
            env = {
                **os.environ,
                "USERPROFILE": str(profile),
                "PSModuleAnalysisCachePath": str(profile / "ModuleAnalysisCache"),
            }
            completed = subprocess.run(
                [str(powershell), "-NoProfile", "-Command", command],
                cwd=profile, env=env, text=True, capture_output=True, check=True,
            )
            self.assertEqual(completed.stdout.strip(), "cost --scope chat --period thread")

            open_command = r'& "$env:USERPROFILE\.local\bin\scout-usage.cmd" open'
            self.assertIn(open_command, skill)
            completed = subprocess.run(
                [str(powershell), "-NoProfile", "-Command", open_command],
                cwd=profile, env=env, text=True, capture_output=True, check=True,
            )
            self.assertEqual(completed.stdout.strip(), "open")


@unittest.skipUnless(os.name == "nt", "Windows PowerShell lifecycle")
class WindowsPowerShellLifecycleTests(unittest.TestCase):
    @staticmethod
    def powershell():
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not executable.is_file():
            raise unittest.SkipTest("Windows PowerShell 5.1 is unavailable")
        return executable

    def run_installer(self, profile, *arguments, check=True, overrides=None):
        env = {**os.environ, "USERPROFILE": str(profile), "SCOUT_USAGE_PYTHON": sys.executable}
        if overrides:
            env.update(overrides)
        return subprocess.run(
            [str(self.powershell()), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "install.ps1"), *arguments],
            env=env, text=True, capture_output=True, check=check, timeout=30,
        )

    @staticmethod
    def make_junction(link, target):
        target.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            text=True, capture_output=True,
        )
        if completed.returncode != 0:
            raise unittest.SkipTest(f"Windows junction creation is unavailable: {completed.stderr.strip()}")

    def test_install_without_skill_flag_ignores_inactive_skill_roots(self):
        with tempfile.TemporaryDirectory(prefix="scout-win-inactive-", dir=Path.home()) as temporary:
            profile = Path(temporary)
            scout_skill = profile / ".scout/m-skills/cost"
            scout_skill.mkdir(parents=True)
            scout_sentinel = scout_skill / "unowned.txt"
            scout_sentinel.write_text("keep", encoding="utf-8")
            copilot_skill = profile / ".copilot/m-skills/cost"
            copilot_skill.parent.mkdir(parents=True)
            junction_target = profile / "junction-target"
            self.make_junction(copilot_skill, junction_target)

            self.run_installer(profile, "install")
            self.run_installer(profile, "update")

            self.assertEqual(scout_sentinel.read_text(encoding="utf-8"), "keep")
            self.assertTrue(copilot_skill.exists())

    def test_install_without_skill_flag_does_not_resolve_invalid_skill_overrides(self):
        with tempfile.TemporaryDirectory(prefix="scout-win-invalid-inactive-", dir=Path.home()) as temporary:
            profile = Path(temporary)
            overrides = {
                "SCOUT_COST_SKILL_DIR": "::invalid::scout::skill::",
                "COPILOT_COST_SKILL_DIR": "::invalid::copilot::skill::",
            }

            self.run_installer(profile, "install", overrides=overrides)
            self.run_installer(profile, "update", overrides=overrides)

            self.assertTrue((profile / ".local/bin/scout-usage.cmd").is_file())
            self.assertFalse((profile / ".scout").exists())
            self.assertFalse((profile / ".copilot").exists())

    def test_uninstall_preserves_unowned_skill_directory_and_junction(self):
        with tempfile.TemporaryDirectory(prefix="scout-win-unowned-", dir=Path.home()) as temporary:
            profile = Path(temporary)
            self.run_installer(profile, "install")
            scout_skill = profile / ".scout/m-skills/cost"
            scout_skill.mkdir(parents=True)
            scout_sentinel = scout_skill / "unowned.txt"
            scout_sentinel.write_text("keep", encoding="utf-8")
            copilot_skill = profile / ".copilot/m-skills/cost"
            copilot_skill.parent.mkdir(parents=True)
            junction_target = profile / "unowned-junction-target"
            junction_sentinel = junction_target / "keep.txt"
            junction_target.mkdir()
            junction_sentinel.write_text("keep", encoding="utf-8")
            self.make_junction(copilot_skill, junction_target)

            self.run_installer(profile, "uninstall")

            self.assertEqual(scout_sentinel.read_text(encoding="utf-8"), "keep")
            self.assertTrue(copilot_skill.exists())
            self.assertEqual(junction_sentinel.read_text(encoding="utf-8"), "keep")
            self.assertFalse((profile / ".local/bin/scout-usage.cmd").exists())

    def test_uninstall_rejects_unowned_skill_nested_under_core_tree_atomically(self):
        with tempfile.TemporaryDirectory(prefix="scout-win-overlap-", dir=Path.home()) as temporary:
            profile = Path(temporary)
            self.run_installer(profile, "install")
            runtime = profile / ".local/share/scout-usage-tracker"
            launcher = profile / ".local/bin/scout-usage.cmd"
            nested_skill = runtime / "src/unowned-cost-skill"
            nested_skill.mkdir()
            sentinel = nested_skill / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")

            completed = self.run_installer(
                profile, "uninstall", check=False,
                overrides={"SCOUT_COST_SKILL_DIR": str(nested_skill)},
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("must not overlap core deletion paths", completed.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertTrue(launcher.is_file())
            self.assertTrue((runtime / "templates").is_dir())
            self.assertTrue((runtime / ".scout-usage-tracker-owned").is_file())

    def test_uninstall_preserves_skill_below_junction_ancestor(self):
        with tempfile.TemporaryDirectory(prefix="scout-win-parent-junction-", dir=Path.home()) as temporary:
            profile = Path(temporary)
            self.run_installer(profile, "install")
            junction_target = profile / "scout-junction-target"
            skill_target = junction_target / "m-skills/cost"
            skill_target.mkdir(parents=True)
            marker = skill_target / ".scout-usage-tracker-owned"
            marker.write_text("synthetic owner marker", encoding="utf-8")
            scout_parent = profile / ".scout"
            self.make_junction(scout_parent, junction_target)

            self.run_installer(profile, "uninstall")

            self.assertTrue(scout_parent.exists())
            self.assertEqual(marker.read_text(encoding="utf-8"), "synthetic owner marker")
            self.assertFalse((profile / ".local/bin/scout-usage.cmd").exists())
            self.assertFalse((profile / ".local/share/scout-usage-tracker/src").exists())

    def test_uninstall_removes_owned_skills(self):
        with tempfile.TemporaryDirectory(prefix="scout-win-owned-", dir=Path.home()) as temporary:
            profile = Path(temporary)
            self.run_installer(profile, "install", "-InstallScoutSkill")
            skill_roots = (profile / ".scout/m-skills/cost", profile / ".copilot/m-skills/cost")
            for root in skill_roots:
                self.assertTrue((root / ".scout-usage-tracker-owned").is_file())

            self.run_installer(profile, "uninstall")

            for root in skill_roots:
                self.assertFalse(root.exists())

    def test_failed_uninstall_preflight_does_not_delete_core(self):
        with tempfile.TemporaryDirectory(prefix="scout-win-atomic-", dir=Path.home()) as temporary:
            profile = Path(temporary)
            self.run_installer(profile, "install")
            runtime = profile / ".local/share/scout-usage-tracker"
            launcher = profile / ".local/bin/scout-usage.cmd"
            source_tree = runtime / "src"
            source_target = profile / "source-target"
            source_tree.rename(source_target)
            self.make_junction(source_tree, source_target)

            completed = self.run_installer(profile, "uninstall", check=False)

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("reparse point", completed.stderr)
            self.assertTrue(launcher.is_file())
            self.assertTrue((runtime / "templates").is_dir())
            self.assertTrue((runtime / ".scout-usage-tracker-owned").is_file())

    def test_installer_rejects_source_package_overlap_before_mutation(self):
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not powershell.is_file():
            self.skipTest("Windows PowerShell 5.1 is unavailable")
        for relationship in ("equal", "ancestor", "descendant"):
            with self.subTest(relationship=relationship), tempfile.TemporaryDirectory(prefix=".scout-overlap-", dir=ROOT) as temporary:
                profile = Path(temporary)
                workspace = profile / "workspace"
                checkout = workspace / "checkout"
                checkout.mkdir(parents=True)
                script = checkout / "install.ps1"
                script.write_bytes((ROOT / "install.ps1").read_bytes())
                targets = {
                    "equal": checkout,
                    "ancestor": workspace,
                    "descendant": checkout / "nested",
                }
                env = {
                    key: value for key, value in os.environ.items()
                    if key not in {
                        "SCOUT_USAGE_INSTALL_ROOT", "SCOUT_USAGE_BIN_DIR", "SCOUT_USAGE_CONFIG_DIR",
                        "SCOUT_COST_SKILL_DIR", "COPILOT_COST_SKILL_DIR",
                    }
                }
                env.update({
                    "USERPROFILE": str(profile),
                    "SCOUT_USAGE_INSTALL_ROOT": str(targets[relationship]),
                    "SCOUT_USAGE_PYTHON": sys.executable,
                    "PSModuleAnalysisCachePath": str(profile / "ModuleAnalysisCache"),
                })
                before = script.read_bytes()
                completed = subprocess.run(
                    [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "install"],
                    cwd=checkout, env=env, text=True, capture_output=True, timeout=20,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("must not overlap the source package", completed.stderr)
                self.assertEqual(script.read_bytes(), before)
                self.assertEqual([path.name for path in checkout.iterdir()], ["install.ps1"])
                self.assertFalse((profile / ".local").exists())
                self.assertFalse((profile / ".config").exists())
                self.assertFalse((profile / ".scout").exists())
                self.assertFalse((profile / ".copilot").exists())

    def test_install_update_uninstall_and_manifest_purge(self):
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not powershell.is_file():
            self.skipTest("Windows PowerShell 5.1 is unavailable")
        profile_parent = Path.home()
        with tempfile.TemporaryDirectory(prefix="scout-win-test-", dir=profile_parent) as temporary:
            profile = Path(temporary)
            env = {**os.environ, "USERPROFILE": str(profile), "SCOUT_USAGE_PYTHON": sys.executable}

            def run(script, *arguments):
                return subprocess.run(
                    [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / script), *arguments],
                    env=env, text=True, capture_output=True, check=True,
                )

            run("install.ps1", "install", "-InstallScoutSkill")
            runtime = profile / ".local/share/scout-usage-tracker"
            config = profile / ".config/scout-usage-tracker/config.json"
            launcher = profile / ".local/bin/scout-usage.cmd"
            history = runtime / "history.sqlite3"
            original_config = config.read_bytes()
            history.write_bytes(b"synthetic-history")
            launcher_text = launcher.read_text(encoding="utf-8")
            self.assertIn(f'"{sys.executable}"', launcher_text)
            self.assertIn('--config "', launcher_text)
            self.assertIn(" %*", launcher_text)
            run("install.ps1", "update")
            self.assertEqual(config.read_bytes(), original_config)
            self.assertEqual(history.read_bytes(), b"synthetic-history")

            run("uninstall.ps1")
            self.assertTrue(config.exists())
            self.assertTrue(history.exists())
            self.assertFalse(launcher.exists())

            run("install.ps1", "install")
            runtime_sentinel = runtime / "adjacent-sentinel.txt"
            config_sentinel = config.parent / "adjacent-sentinel.txt"
            runtime_sentinel.write_text("keep", encoding="utf-8")
            config_sentinel.write_text("keep", encoding="utf-8")
            run("uninstall.ps1", "-PurgeData")
            self.assertEqual(runtime_sentinel.read_text(encoding="utf-8"), "keep")
            self.assertEqual(config_sentinel.read_text(encoding="utf-8"), "keep")
            self.assertFalse(config.exists())
            self.assertFalse(history.exists())


if __name__ == "__main__":
    unittest.main()
