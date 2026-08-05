"""Idempotent, privacy-preserving source import."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .database import connect_history, secure_history_files
from .pricing import verification
from .privacy import content_version_uid, session_digest, source_event_key
from .source import SourceResult, read_source

TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"
)


@dataclass
class ImportResult:
    status: str
    seen: int = 0
    inserted: int = 0
    superseded: int = 0
    rows_skipped: int = 0
    possible_id_gap: bool = False
    warnings: tuple[str, ...] = ()


def parse_timestamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _token_details_digest(raw: str | None) -> str:
    payload = b"\x00" if raw is None else b"\x01" + str(raw).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_row(row: sqlite3.Row) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in TOKEN_FIELDS:
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a nonnegative integer")
        values[field] = value
    total = row["total_nano_aiu"]
    if isinstance(total, bool) or not isinstance(total, int):
        raise ValueError("total_nano_aiu must be an integer")
    model = row["model"]
    session = row["session_id"]
    if not isinstance(model, str) or not isinstance(session, str):
        raise ValueError("model and session_id must be text")
    timestamp = parse_timestamp(row["created_at"])
    values.update(model=model, total_nano_aiu=total, event_time_utc=timestamp.isoformat())
    return values


def import_usage(source_path: str | Path, history_path: str | Path, secret: bytes, *, timeout: float = 0.25) -> ImportResult:
    source = read_source(source_path, timeout=timeout)
    if source.status != "ok":
        return ImportResult(source.status, warnings=(source.message,))
    now = datetime.now(timezone.utc).isoformat()
    inserted = superseded = rows_skipped = 0
    warnings: list[str] = []
    connection = connect_history(history_path)
    try:
        with connection:
            for row in source.rows:
                try:
                    values = _validate_row(row)
                except (ValueError, TypeError) as exc:
                    warnings.append(f"skipped invalid source row: {exc}")
                    rows_skipped += 1
                    continue
                key = source_event_key(secret, source_path, row["id"])
                digest = session_digest(secret, row["session_id"])
                canonical = {
                    **values,
                    "session_digest": digest,
                    "token_details_digest": _token_details_digest(row["token_details_json"]),
                }
                version = content_version_uid(canonical)
                active = connection.execute(
                    "SELECT content_version_uid FROM usage_versions "
                    "WHERE source_event_key = ? AND active = 1", (key,)
                ).fetchone()
                if active is not None and active[0] == version:
                    continue
                state, calculated, warning = verification(values["total_nano_aiu"], row["token_details_json"])
                if warning:
                    warnings.append(warning)
                if active is not None:
                    connection.execute(
                        "UPDATE usage_versions SET active = 0, superseded_at = ? "
                        "WHERE source_event_key = ? AND active = 1", (now, key)
                    )
                    superseded += 1
                existing = connection.execute(
                    "SELECT active FROM usage_versions WHERE source_event_key = ? AND content_version_uid = ?",
                    (key, version),
                ).fetchone()
                if existing is not None:
                    connection.execute(
                        "UPDATE usage_versions SET active = 1, superseded_at = NULL "
                        "WHERE source_event_key = ? AND content_version_uid = ?", (key, version)
                    )
                    continue
                connection.execute(
                    "INSERT INTO usage_versions (source_event_key, content_version_uid, active, "
                    "session_digest, model, input_tokens, output_tokens, cache_read_tokens, "
                    "cache_write_tokens, reasoning_tokens, total_nano_aiu, calculated_nano_aiu, "
                    "verification_status, event_time_utc, imported_at) "
                    "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, version, digest, values["model"],
                     values["input_tokens"], values["output_tokens"], values["cache_read_tokens"],
                     values["cache_write_tokens"], values["reasoning_tokens"], values["total_nano_aiu"],
                     str(calculated) if calculated is not None else None, state,
                     values["event_time_utc"], now),
                )
                inserted += 1
            connection.execute(
                "INSERT INTO import_runs (imported_at, source_status, rows_seen, rows_inserted, "
                "rows_superseded, rows_skipped, possible_id_gap, warnings_json) VALUES (?, 'ok', ?, ?, ?, ?, ?, ?)",
                (now, len(source.rows), inserted, superseded, rows_skipped, int(source.possible_id_gap),
                 json.dumps(warnings, ensure_ascii=False)),
            )
            connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES ('last_update', ?)", (now,))
        return ImportResult("ok", len(source.rows), inserted, superseded, rows_skipped,
                            source.possible_id_gap, tuple(warnings))
    finally:
        secure_history_files(history_path)
        connection.close()
