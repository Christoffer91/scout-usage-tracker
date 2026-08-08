import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from unittest.mock import patch

from scout_usage_tracker.dashboard_server import _popen_kwargs, _reap_viewers, start_dashboard_viewer


class DashboardServerTests(unittest.TestCase):
    def test_private_viewer_is_loopback_only_tokenized_one_shot_and_no_store(self):
        content = b"<!doctype html><title>Synthetic dashboard</title>"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dashboard = root / "private æ dashboard.html"
            dashboard.write_bytes(content)
            url = start_dashboard_viewer(dashboard, root, lifetime=15)
            self.assertTrue(url.startswith("http://127.0.0.1:"))
            self.assertNotIn(str(dashboard), url)

            with self.assertRaises(HTTPError) as rejected:
                urlopen(Request(url, headers={"Host": "localhost"}), timeout=2)
            self.assertEqual(rejected.exception.code, 404)
            wrong_token_url = url.rsplit("/", 1)[0] + "/wrong-token"
            with self.assertRaises(HTTPError) as rejected:
                urlopen(wrong_token_url, timeout=2)
            self.assertEqual(rejected.exception.code, 404)

            with urlopen(url, timeout=2) as response:
                self.assertEqual(response.read(), content)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(response.headers["Cross-Origin-Resource-Policy"], "same-origin")
                self.assertIn("connect-src 'none'", response.headers["Content-Security-Policy"])

            for _ in range(50):
                try:
                    urlopen(url, timeout=0.1)
                except (URLError, TimeoutError, ConnectionError):
                    break
                time.sleep(0.02)
            else:
                self.fail("dashboard viewer did not stop after its successful fetch")
            _reap_viewers()
            self.assertEqual(list(root.glob(".dashboard-viewer-*.json")), [])

    def test_child_command_line_contains_neither_dashboard_path_nor_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dashboard = root / "private dashboard.html"
            dashboard.write_text("synthetic", encoding="utf-8")
            observed = {}

            class Process:
                pid = 4242

                def poll(self):
                    return None

                def terminate(self):
                    observed["terminated"] = True

            def launch(argv, **kwargs):
                observed["argv"] = argv
                observed["env"] = kwargs["env"]
                ready = Path(kwargs["env"]["SCOUT_USAGE_VIEWER_READY"])
                ready.write_text(json.dumps({"pid": 4242, "port": 54321, "token": "t" * 43}), encoding="utf-8")
                return Process()

            with patch.dict(os.environ, {"SESSION_ID": "private-session", "SCOUT_TEST_SECRET": "private-secret"}), \
                 patch("scout_usage_tracker.dashboard_server.subprocess.Popen", side_effect=launch), \
                 patch("scout_usage_tracker.dashboard_server._ACTIVE_VIEWERS", []):
                url = start_dashboard_viewer(dashboard, root)
            command_line = " ".join(observed["argv"])
            self.assertNotIn(str(dashboard), command_line)
            self.assertNotIn("t" * 43, command_line)
            self.assertNotIn("SESSION_ID", observed["env"])
            self.assertNotIn("SCOUT_TEST_SECRET", observed["env"])
            self.assertEqual(url, "http://127.0.0.1:54321/view/" + "t" * 43)
            self.assertEqual(list(root.glob(".dashboard-viewer-*.json")), [])

    def test_platform_process_is_detached_without_a_shell(self):
        kwargs = _popen_kwargs()
        self.assertNotIn("shell", kwargs)
        self.assertTrue(kwargs["close_fds"])
        if os.name == "nt":
            self.assertGreater(kwargs["creationflags"], 0)
            self.assertNotIn("start_new_session", kwargs)
        else:
            self.assertTrue(kwargs["start_new_session"])
            self.assertNotIn("creationflags", kwargs)


if __name__ == "__main__":
    unittest.main()
