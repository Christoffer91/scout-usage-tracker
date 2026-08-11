import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LifecyclePrivacyTests(unittest.TestCase):
    def test_cost_skill_bare_commands_are_english_and_stdout_is_verbatim(self):
        skill = (ROOT / "skills/cost/SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("cost --scope chat --period thread --language en --dashboard-link loopback", skill)
        self.assertIn("cost --scope chat --period thread --language en", skill)
        self.assertIn("cost --faq --language en", skill)
        self.assertIn("Bare `/cost` and bare `/cost FAQ` always use `--language en`", skill)
        self.assertIn("Return command stdout verbatim", skill)
        self.assertNotIn("translate only headings", skill)
        self.assertNotIn("starts no web server", readme)
        self.assertIn("starts no persistent or externally listening server", readme)
        self.assertIn("bounded `127.0.0.1` viewer", readme)

    @unittest.skipUnless(shutil.which("sh"), "POSIX sh is unavailable")
    def test_shell_syntax_and_install_permissions_and_opt_in(self):
        subprocess.run(["sh", "-n", str(ROOT / "install.sh"), str(ROOT / "uninstall.sh")], check=True)
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            env = {**os.environ, "HOME": str(home)}
            completed = subprocess.run(["sh", str(ROOT / "install.sh"), "install"], env=env, check=True, text=True, capture_output=True)
            self.assertIn("Installed Scout Usage Tracker", completed.stdout)
            config = home / ".config/scout-usage-tracker/config.json"
            runtime = home / ".local/share/scout-usage-tracker"
            if os.name != "nt":
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
            self.assertFalse((home / "Library/LaunchAgents/local.scout-usage-tracker.plist").exists())
            self.assertFalse((home / ".scout/m-skills/cost").exists())
            self.assertFalse((home / ".copilot/m-skills/cost").exists())
            subprocess.run(["sh", str(ROOT / "install.sh"), "uninstall"], env=env, check=True, capture_output=True)
            self.assertTrue(config.exists())
            self.assertTrue(runtime.exists())

    @unittest.skipUnless(shutil.which("sh"), "POSIX sh is unavailable")
    def test_installer_rejects_unsafe_override_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            outside = Path(temporary) / "outside"
            env = {**os.environ, "HOME": str(home), "SCOUT_USAGE_INSTALL_ROOT": str(outside)}
            completed = subprocess.run(["sh", str(ROOT / "install.sh"), "install"], env=env, text=True, capture_output=True)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("strictly under HOME", completed.stderr)
            self.assertFalse(outside.exists())

    @unittest.skipUnless(shutil.which("sh"), "POSIX sh is unavailable")
    def test_installer_rejects_nested_managed_roots_before_mutation(self):
        cases = (
            {"SCOUT_USAGE_CONFIG_DIR": ".local/share/scout-usage-tracker/src/scout-usage-tracker"},
            {
                "SCOUT_USAGE_CONFIG_DIR": ".config/scout-usage-tracker",
                "SCOUT_USAGE_INSTALL_ROOT": ".config/scout-usage-tracker/nested/scout-usage-tracker",
            },
            {"SCOUT_USAGE_BIN_DIR": ".local/share/scout-usage-tracker/bin"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                env = {**os.environ, "HOME": str(home)}
                env.update({key: str(home / value) for key, value in overrides.items()})
                completed = subprocess.run(["sh", str(ROOT / "install.sh"), "install"],
                                           env=env, text=True, capture_output=True)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("pairwise disjoint", completed.stderr)
                self.assertEqual(list(home.iterdir()), [])
                self.assertFalse(any(home.rglob(".scout-usage-tracker-owned")))

    @unittest.skipUnless(shutil.which("sh"), "POSIX sh is unavailable")
    def test_unknown_option_leaves_home_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            completed = subprocess.run(["sh", str(ROOT / "install.sh"), "install", "--unknown"],
                                       env={**os.environ, "HOME": str(home)}, text=True, capture_output=True)
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(list(home.iterdir()), [])

    @unittest.skipUnless(shutil.which("sh"), "POSIX sh is unavailable")
    def test_unowned_roots_and_skill_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            install_root = home / ".local/share/scout-usage-tracker"
            install_root.mkdir(parents=True)
            sentinel = install_root / "unrelated.txt"; sentinel.write_text("keep", encoding="utf-8")
            completed = subprocess.run(["sh", str(ROOT / "install.sh"), "install"],
                                       env={**os.environ, "HOME": str(home)}, text=True, capture_output=True)
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(sentinel.read_text(), "keep")

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            skill = home / ".codex/skills/scout-usage"
            skill.mkdir(parents=True)
            skill_file = skill / "SKILL.md"; skill_file.write_text("unowned", encoding="utf-8")
            completed = subprocess.run(["sh", str(ROOT / "install.sh"), "install", "--install-skill"],
                                       env={**os.environ, "HOME": str(home)}, text=True, capture_output=True)
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(skill_file.read_text(), "unowned")
            self.assertFalse((home / ".local").exists())

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            skill = home / ".scout/m-skills/cost"
            skill.mkdir(parents=True)
            skill_file = skill / "SKILL.md"; skill_file.write_text("unowned", encoding="utf-8")
            completed = subprocess.run(["sh", str(ROOT / "install.sh"), "install", "--install-scout-skill"],
                                       env={**os.environ, "HOME": str(home)}, text=True, capture_output=True)
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(skill_file.read_text(), "unowned")
            self.assertFalse((home / ".local").exists())

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            skill = home / ".copilot/m-skills/cost"
            skill.mkdir(parents=True)
            skill_file = skill / "SKILL.md"; skill_file.write_text("unowned", encoding="utf-8")
            completed = subprocess.run(["sh", str(ROOT / "install.sh"), "install", "--install-scout-skill"],
                                       env={**os.environ, "HOME": str(home)}, text=True, capture_output=True)
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(skill_file.read_text(), "unowned")
            self.assertFalse((home / ".local").exists())

    @unittest.skipUnless(shutil.which("sh"), "POSIX sh is unavailable")
    def test_scout_cost_skill_is_explicit_owned_and_uninstalled(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".scout").mkdir()
            env = {**os.environ, "HOME": str(home)}
            completed = subprocess.run(["sh", str(ROOT / "install.sh"), "install", "--install-scout-skill"],
                                       env=env, check=True, text=True, capture_output=True)
            skill = home / ".scout/m-skills/cost"
            portable = home / ".copilot/m-skills/cost"
            self.assertIn("Installed Scout /cost skill", completed.stdout)
            self.assertIn("Installed Copilot-compatible /cost skill", completed.stdout)
            for target in (skill, portable):
                self.assertIn("name: cost", (target / "SKILL.md").read_text(encoding="utf-8"))
                self.assertTrue((target / ".scout-usage-tracker-owned").is_file())
            subprocess.run(["sh", str(ROOT / "install.sh"), "uninstall"], env=env, check=True, capture_output=True)
            self.assertFalse(skill.exists())
            self.assertFalse(portable.exists())

    @unittest.skipUnless(shutil.which("sh"), "POSIX sh is unavailable")
    def test_purge_removes_only_enumerated_owned_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            env = {**os.environ, "HOME": str(home)}
            subprocess.run(["sh", str(ROOT / "install.sh"), "install"], env=env, check=True, capture_output=True)
            install_root = home / ".local/share/scout-usage-tracker"
            sentinel = install_root / "unrelated.txt"; sentinel.write_text("keep", encoding="utf-8")
            subprocess.run(["sh", str(ROOT / "install.sh"), "uninstall", "--purge-data"], env=env, check=True, capture_output=True)
            self.assertEqual(sentinel.read_text(), "keep")

    @unittest.skipUnless(shutil.which("sh"), "POSIX sh is unavailable")
    def test_installer_rejects_python_older_than_310(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"; home.mkdir()
            fake_bin = root / "bin"; fake_bin.mkdir()
            fake_python = fake_bin / "python3"
            fake_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake_python.chmod(0o755)
            env = {**os.environ, "HOME": str(home), "PATH": f"{fake_bin}:/bin:/usr/bin"}
            completed = subprocess.run(["sh", str(ROOT / "install.sh"), "install"], env=env, text=True, capture_output=True)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Python 3.10 or newer is required", completed.stderr)
            self.assertFalse((home / ".local").exists())

    def test_synthetic_check_does_not_rewrite_committed_example(self):
        dashboard = ROOT / "examples/synthetic-dashboard.html"
        before = (dashboard.read_bytes(), dashboard.stat().st_mtime_ns)
        completed = subprocess.run([sys.executable, str(ROOT / "scripts/generate_synthetic.py"), "--check"], check=True, text=True, capture_output=True,
                                   env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        after = (dashboard.read_bytes(), dashboard.stat().st_mtime_ns)
        self.assertEqual(before, after)
        self.assertIn("PASS synthetic dashboard is current", completed.stdout)

    def test_source_contains_no_forbidden_real_values_or_network_assets(self):
        forbidden = ("30" + "456", "27" + "625", "http://", "https://")
        for path in ROOT.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.name == "LICENSE":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for value in forbidden[:2]:
                self.assertNotIn(value, text, f"forbidden value in {path}")
        template = (ROOT / "templates/dashboard.html").read_text()
        self.assertNotIn("src=\"http", template)
        self.assertNotIn("href=\"http", template)
