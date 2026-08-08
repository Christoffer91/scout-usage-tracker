"""Private audit-history SQLite storage."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .platform_support import secure_chmod

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_versions (
  source_event_key TEXT NOT NULL,
  content_version_uid TEXT NOT NULL,
  active INTEGER NOT NULL CHECK (active IN (0,1)),
  superseded_at TEXT,
  session_digest TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
  output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
  cache_read_tokens INTEGER NOT NULL CHECK (cache_read_tokens >= 0),
  cache_write_tokens INTEGER NOT NULL CHECK (cache_write_tokens >= 0),
  reasoning_tokens INTEGER NOT NULL CHECK (reasoning_tokens >= 0),
  total_nano_aiu INTEGER NOT NULL,
  calculated_nano_aiu TEXT,
  verification_status TEXT NOT NULL,
  event_time_utc TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  PRIMARY KEY (source_event_key, content_version_uid)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_version
  ON usage_versions(source_event_key) WHERE active = 1;
CREATE TABLE IF NOT EXISTS import_runs (
  run_id INTEGER PRIMARY KEY,
  imported_at TEXT NOT NULL,
  source_status TEXT NOT NULL,
  rows_seen INTEGER NOT NULL,
  rows_inserted INTEGER NOT NULL,
  rows_superseded INTEGER NOT NULL,
  rows_skipped INTEGER NOT NULL,
  possible_id_gap INTEGER NOT NULL,
  warnings_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect_history(path: str | Path) -> sqlite3.Connection:
    target = Path(path).expanduser()
    parent_existed = target.parent.exists()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        secure_chmod(target.parent, 0o700)
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    run_columns = {row[1] for row in connection.execute("PRAGMA table_info(import_runs)")}
    if "rows_skipped" not in run_columns:
        connection.execute("ALTER TABLE import_runs ADD COLUMN rows_skipped INTEGER NOT NULL DEFAULT 0")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.commit()
    secure_history_files(target)
    return connection


def secure_history_files(path: str | Path) -> None:
    target = Path(path)
    for candidate in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
        if candidate.exists():
            secure_chmod(candidate, 0o600)
