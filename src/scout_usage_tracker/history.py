"""History queries kept separate from source access."""

from __future__ import annotations

import sqlite3


def active_events(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(connection.execute(
        "SELECT session_digest, model, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, reasoning_tokens, total_nano_aiu, calculated_nano_aiu, "
        "verification_status, event_time_utc FROM usage_versions WHERE active = 1 "
        "ORDER BY event_time_utc"
    ))


def latest_run(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM import_runs ORDER BY run_id DESC LIMIT 1").fetchone()

