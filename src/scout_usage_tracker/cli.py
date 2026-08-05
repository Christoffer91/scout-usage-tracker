"""Command-line interface; no server and no network access."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

from .config import ConfigError, ensure_secret, load_config
from .import_usage import import_usage
from .render import render_dashboard
from .source import read_source

DEFAULT_CONFIG = Path(os.environ.get("SCOUT_USAGE_CONFIG", "~/.config/scout-usage-tracker/config.json")).expanduser()


def _runtime_dir(config: dict) -> Path:
    return Path(config["history_database"]).parent


def command_update(config: dict) -> int:
    secret = ensure_secret(_runtime_dir(config))
    result = import_usage(config["source_database"], config["history_database"], secret)
    if result.status != "ok":
        print(f"FAIL source={result.status}: {'; '.join(result.warnings)}", file=sys.stderr)
        return 2
    target = render_dashboard(config)
    complete = not result.rows_skipped and not result.possible_id_gap and not result.warnings
    outcome = "PASS" if complete else "INCOMPLETE"
    print(f"{outcome} source=ok seen={result.seen} inserted={result.inserted} "
          f"superseded={result.superseded} skipped={result.rows_skipped} "
          f"possible_gap={str(result.possible_id_gap).lower()} verification_warnings={len(result.warnings)}")
    print(f"Dashboard: {target}")
    if result.warnings:
        print(f"Verification warnings: {len(result.warnings)}")
    return 0


def command_render(config: dict) -> int:
    target = render_dashboard(config)
    print(f"PASS rendered {target}")
    return 0


def command_status(config: dict) -> int:
    source = read_source(config["source_database"])
    history = Path(config["history_database"])
    dashboard = Path(config["dashboard_path"])
    print(json.dumps({
        "source_status": source.status,
        "history_exists": history.exists(),
        "dashboard_exists": dashboard.exists(),
        "session_breakdown_enabled": config["privacy"]["include_sessions"],
    }, indent=2))
    return 0 if source.status == "ok" else 2


def command_open(config: dict) -> int:
    target = Path(config["dashboard_path"])
    if not target.is_file():
        print("Dashboard is missing; run update first.", file=sys.stderr)
        return 2
    system = platform.system()
    command = ["open", str(target)] if system == "Darwin" else (["cmd", "/c", "start", "", str(target)] if system == "Windows" else ["xdg-open", str(target)])
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Could not open dashboard: {exc}", file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scout-usage")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="JSON configuration path")
    parser.add_argument("command", choices=("update", "refresh", "render", "status", "open"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command in ("update", "refresh"):
            return command_update(config)
        if args.command == "render":
            return command_render(config)
        if args.command == "status":
            return command_status(config)
        return command_open(config)
    except (ConfigError, ValueError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
