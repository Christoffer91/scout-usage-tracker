"""Command-line interface; no server and no network access."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigError, ensure_secret, load_config
from .cost_report import CostReportError, build_cost_report, format_cost_faq, format_cost_report
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


def command_cost(
    config: dict,
    period: str | None = None,
    scope: str = "chat",
    language: str | None = None,
    faq: bool = False,
) -> int:
    selected_language = language or config.get("language", "en")
    if faq:
        print(format_cost_faq(selected_language))
        return 0
    session_id = os.environ.get("SESSION_ID", "")
    report = build_cost_report(config["source_database"], session_id, config["timezone"])
    selected_period = period or ("all" if scope == "all" else "thread")
    dashboard_uri = Path(config["dashboard_path"]).expanduser().resolve(strict=False).as_uri()
    print(format_cost_report(
        report,
        selected_period,
        config["usd_per_credit_by_model"],
        config.get("usd_to_nok"),
        scope=scope,
        language=selected_language,
        default_usd_per_credit=config.get("usd_per_credit", "0.01"),
        dashboard_uri=dashboard_uri,
    ))
    return 0


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


def command_github_sync(config: dict, scope: str, owner: str, year: int, month: int) -> int:
    snapshot_path = (config.get("billing") or {}).get("snapshot_path")
    if not snapshot_path:
        print("FAIL: billing.snapshot_path must be configured before github-sync", file=sys.stderr)
        return 2
    from .github_billing import GitHubBillingError, sync_snapshot
    try:
        snapshot = sync_snapshot(snapshot_path, scope, owner, year, month)
    except GitHubBillingError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    print(
        f"PASS GitHub billing snapshot source=github scope={snapshot['scope']} "
        f"period={snapshot['year']:04d}-{snapshot['month']:02d}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scout-usage")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="JSON configuration path")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("update", "refresh", "render", "status", "open"):
        command = commands.add_parser(name)
        command.add_argument("--config", default=argparse.SUPPRESS, help="JSON configuration path")
    cost = commands.add_parser("cost", help="show local Scout usage before the current /cost turn")
    cost.add_argument("--config", default=argparse.SUPPRESS, help="JSON configuration path")
    cost.add_argument("--period", choices=("thread", "last", "all", "day", "week", "month"))
    cost.add_argument("--scope", choices=("chat", "all"), default="chat")
    cost.add_argument("--language", choices=("en", "nb"))
    cost.add_argument("--faq", action="store_true", help="show the /cost usage guide without reading usage data")
    current = datetime.now(timezone.utc)
    sync = commands.add_parser("github-sync", help="explicitly fetch aggregate GitHub billing usage through gh")
    sync.add_argument("--config", default=argparse.SUPPRESS, help="JSON configuration path")
    sync.add_argument("--scope", required=True, choices=("user", "organization", "enterprise"))
    sync.add_argument("--owner", required=True, help="GitHub login, organization, or enterprise slug; never persisted")
    sync.add_argument("--year", type=int, default=current.year)
    sync.add_argument("--month", type=int, default=current.month)
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
        if args.command == "cost":
            return command_cost(config, args.period, args.scope, args.language, args.faq)
        if args.command == "github-sync":
            return command_github_sync(config, args.scope, args.owner, args.year, args.month)
        return command_open(config)
    except (ConfigError, CostReportError, ValueError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
