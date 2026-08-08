"""Short-lived, capability-protected loopback access to the local dashboard."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit


_DASHBOARD_ENV = "SCOUT_USAGE_VIEWER_DASHBOARD"
_READY_ENV = "SCOUT_USAGE_VIEWER_READY"
_LIFETIME_ENV = "SCOUT_USAGE_VIEWER_LIFETIME"
_MAX_DASHBOARD_BYTES = 32 * 1024 * 1024
_ACTIVE_VIEWERS: list[subprocess.Popen] = []


class DashboardViewerError(ValueError):
    """Raised when the private loopback viewer cannot be started safely."""


def _secure_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


def _write_ready(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        _secure_file(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _handler(content: bytes, token: str, port: int):
    expected_path = f"/view/{token}"
    expected_host = f"127.0.0.1:{port}"

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = ""
        sys_version = ""

        def log_message(self, _format: str, *args: object) -> None:
            return

        def _authorized(self) -> bool:
            parsed = urlsplit(self.path)
            return (
                hmac.compare_digest(self.headers.get("Host", ""), expected_host)
                and not parsed.query
                and not parsed.fragment
                and hmac.compare_digest(parsed.path, expected_path)
            )

        def _send(self, include_body: bool) -> None:
            if not self._authorized():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            if include_body:
                self.wfile.write(content)
                self.server.dashboard_served = True  # type: ignore[attr-defined]

        def do_GET(self) -> None:
            self._send(True)

        def do_HEAD(self) -> None:
            self._send(False)

        def do_POST(self) -> None:
            self.send_error(405)

    return DashboardHandler


def serve_from_environment() -> int:
    dashboard_value = os.environ.get(_DASHBOARD_ENV, "")
    ready_value = os.environ.get(_READY_ENV, "")
    try:
        lifetime = int(os.environ.get(_LIFETIME_ENV, "300"))
    except ValueError:
        return 2
    if not dashboard_value or not ready_value or not 15 <= lifetime <= 600:
        return 2

    dashboard = Path(dashboard_value)
    ready = Path(ready_value)
    try:
        size = dashboard.stat().st_size
        if not dashboard.is_file() or size > _MAX_DASHBOARD_BYTES:
            return 2
        content = dashboard.read_bytes()
        token = secrets.token_urlsafe(32)
        with HTTPServer(("127.0.0.1", 0), _handler(content, token, 0)) as server:
            port = server.server_address[1]
            server.RequestHandlerClass = _handler(content, token, port)
            server.timeout = 0.5
            server.dashboard_served = False  # type: ignore[attr-defined]
            _write_ready(ready, {"pid": os.getpid(), "port": port, "token": token})
            deadline = time.monotonic() + lifetime
            while time.monotonic() < deadline and not server.dashboard_served:  # type: ignore[attr-defined]
                server.handle_request()
        return 0
    except (OSError, ValueError):
        return 2
    finally:
        ready.unlink(missing_ok=True)


def _popen_kwargs() -> dict[str, object]:
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _reap_viewers() -> None:
    _ACTIVE_VIEWERS[:] = [process for process in _ACTIVE_VIEWERS if process.poll() is None]


def _child_environment() -> dict[str, str]:
    permitted = (
        "SYSTEMROOT", "WINDIR", "PATH", "PYTHONHOME", "PYTHONPATH", "PYTHONUTF8",
        "PYTHONIOENCODING", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    )
    return {name: os.environ[name] for name in permitted if name in os.environ}


def start_dashboard_viewer(dashboard: Path, runtime_dir: Path, lifetime: int = 300) -> str:
    """Start the bounded viewer and return its opaque local URL."""
    dashboard = dashboard.expanduser().resolve(strict=True)
    if not dashboard.is_file() or dashboard.stat().st_size > _MAX_DASHBOARD_BYTES:
        raise DashboardViewerError("Dashboard is missing or too large; run update first.")
    if not 15 <= lifetime <= 600:
        raise DashboardViewerError("Dashboard viewer lifetime must be between 15 and 600 seconds.")

    runtime_dir.mkdir(parents=True, exist_ok=True)
    ready = runtime_dir / f".dashboard-viewer-{secrets.token_hex(16)}.json"
    environment = _child_environment()
    environment.update({
        _DASHBOARD_ENV: str(dashboard),
        _READY_ENV: str(ready),
        _LIFETIME_ENV: str(lifetime),
    })
    _reap_viewers()
    process = subprocess.Popen(
        [sys.executable, "-m", "scout_usage_tracker.dashboard_server"],
        env=environment,
        **_popen_kwargs(),
    )
    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline:
            if ready.is_file():
                payload = json.loads(ready.read_text(encoding="utf-8"))
                port, token, pid = payload["port"], payload["token"], payload["pid"]
                if (
                    isinstance(port, int) and 1 <= port <= 65535
                    and isinstance(token, str) and len(token) >= 32
                    and pid == process.pid
                ):
                    _ACTIVE_VIEWERS.append(process)
                    return f"http://127.0.0.1:{port}/view/{token}"
                break
            if process.poll() is not None:
                break
            time.sleep(0.02)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass
    finally:
        ready.unlink(missing_ok=True)
    if process.poll() is None:
        process.terminate()
    raise DashboardViewerError("Could not start the private dashboard link; use /cost open.")


if __name__ == "__main__":
    raise SystemExit(serve_from_environment())
