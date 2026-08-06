#!/usr/bin/env python3
"""Generate the committed dashboard from deterministic fictional data."""

from __future__ import annotations

import json
import argparse
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scout_usage_tracker.import_usage import import_usage
from scout_usage_tracker.render import render_dashboard

SCHEMA = """
CREATE TABLE assistant_usage_events (
 id INTEGER PRIMARY KEY, session_id TEXT, model TEXT,
 input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
 cache_write_tokens INTEGER, reasoning_tokens INTEGER, total_nano_aiu INTEGER,
 token_details_json TEXT, created_at TEXT, api_endpoint TEXT
)
"""


def generate(destination: Path = ROOT / "examples" / "synthetic-dashboard.html") -> Path:
    with tempfile.TemporaryDirectory(prefix="scout-usage-synthetic-") as temporary:
        work = Path(temporary)
        source = work / "fictional-source.sqlite3"
        history = work / "private-history.sqlite3"
        billing_snapshot = work / "fictional-billing-snapshot.json"
        config_path = work / "config.json"
        connection = sqlite3.connect(source)
        connection.execute(SCHEMA)
        models = ("example-small", "example-large", "example-reasoning")
        start = date(2025, 10, 23)
        events = []
        for offset in range(75):
            day = start + timedelta(days=offset)
            model_index = offset % len(models)
            base_nano = 1_200_000_000 + model_index * 650_000_000 + (offset % 9) * 110_000_000
            total_nano = base_nano * (4 if offset in (17, 43, 68) else 1)
            details = json.dumps([{"tokenCount": 1000, "costPerBatch": str(total_nano), "batchSize": 1000}])
            events.append((
                offset + 1,
                f"fictional-session-{offset % 5}",
                models[model_index],
                160_000 + offset * 1_700,
                34_000 + offset * 430,
                72_000 + offset * 950,
                0,
                9_000 + offset * 120,
                total_nano,
                details,
                f"{day.isoformat()}T{9 + offset % 8:02d}:15:00+00:00",
                "/fictional",
            ))
        connection.executemany(
            "INSERT INTO assistant_usage_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            events,
        )
        connection.commit()
        connection.close()
        config = {
            "schema_version": 2,
            "source_database": str(source),
            "history_database": str(history),
            "dashboard_path": str(destination),
            "timezone": "UTC",
            "privacy": {"include_sessions": True},
            "usd_per_credit_by_model": {
                "example-small": "0.04",
                "example-large": "0.08",
                "example-reasoning": "0.12",
            },
            "usd_to_nok": "10.00",
            "billing": {"enabled": True, "plan": "pro", "snapshot_path": str(billing_snapshot)},
            "_generated_at": "2026-01-06T12:00:00+00:00",
        }
        billing_snapshot.write_text(json.dumps({
            "schema_version": 1,
            "source": "manual",
            "scope": "user",
            "captured_at": "2026-01-06T11:30:00Z",
            "year": 2026,
            "month": 1,
            "gross_ai_credits": "2400",
            "discount_credits": "1500",
            "discount_amount_usd": "15.00",
            "net_amount_usd": "9.00",
        }, indent=2), encoding="utf-8")
        billing_snapshot.chmod(0o600)
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        config_path.chmod(0o600)
        result = import_usage(source, history, b"synthetic-secret-material-for-tests-32")
        if result.status != "ok":
            raise RuntimeError(result)
        return render_dashboard(config)


def check(destination: Path = ROOT / "examples" / "synthetic-dashboard.html") -> bool:
    if not destination.is_file():
        return False
    with tempfile.TemporaryDirectory(prefix="scout-usage-synthetic-check-") as temporary:
        candidate = Path(temporary) / "synthetic-dashboard.html"
        generate(candidate)
        return candidate.read_bytes() == destination.read_bytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify committed output without modifying it")
    args = parser.parse_args(argv)
    if args.check:
        if check():
            print("PASS synthetic dashboard is current")
            return 0
        print("FAIL synthetic dashboard differs; run the generator", file=sys.stderr)
        return 1
    print(generate())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
