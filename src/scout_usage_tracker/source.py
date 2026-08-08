"""Read-only access to Scout's SQLite usage database."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .platform_support import sqlite_readonly_uri

REQUIRED_COLUMNS = {
    "id", "session_id", "model", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
    "total_nano_aiu", "token_details_json", "created_at",
}


@dataclass
class SourceResult:
    status: str
    rows: list[sqlite3.Row]
    message: str = ""
    possible_id_gap: bool = False


def read_source(path: str | Path, timeout: float = 0.25) -> SourceResult:
    source = Path(path).expanduser()
    if not source.exists():
        return SourceResult("missing", [], "source database does not exist")
    if not os.access(source, os.R_OK):
        return SourceResult("permission", [], "source database is not readable")
    uri = sqlite_readonly_uri(source)
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=timeout)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {max(1, int(timeout * 1000))}")
        connection.execute("PRAGMA query_only = ON")
        column_rows = list(connection.execute("PRAGMA table_info(assistant_usage_events)"))
        columns = {row[1] for row in column_rows}
        if not REQUIRED_COLUMNS.issubset(columns):
            missing = sorted(REQUIRED_COLUMNS - columns)
            return SourceResult("schema", [], "missing columns: " + ", ".join(missing))
        declared = {row[1]: (str(row[2]).upper(), row[5]) for row in column_rows}
        integer_fields = {"id", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "total_nano_aiu"}
        text_fields = {"session_id", "model", "token_details_json", "created_at"}
        if declared["id"] != ("INTEGER", 1):
            return SourceResult("schema", [], "id must be an INTEGER PRIMARY KEY")
        if any("INT" not in declared[name][0] for name in integer_fields - {"id"}):
            return SourceResult("schema", [], "usage token and nano-AIU columns must be INTEGER")
        if any("TEXT" not in declared[name][0] for name in text_fields):
            return SourceResult("schema", [], "session, model, details, and timestamp columns must be TEXT")
        api = ", api_endpoint" if "api_endpoint" in columns else ""
        rows = list(connection.execute(
            "SELECT id, session_id, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, reasoning_tokens, total_nano_aiu, "
            f"token_details_json, created_at{api} FROM assistant_usage_events ORDER BY id"
        ))
        gap = False
        if rows:
            summary = connection.execute(
                "SELECT COUNT(*), MIN(id), MAX(id) FROM assistant_usage_events"
            ).fetchone()
            gap = summary[1] > 1 or summary[2] - summary[1] + 1 > summary[0]
        return SourceResult("ok", rows, possible_id_gap=gap)
    except sqlite3.OperationalError as exc:
        lowered = str(exc).lower()
        if "locked" in lowered or "busy" in lowered:
            return SourceResult("locked", [], "source database is locked")
        if "permission" in lowered or "readonly" in lowered or "unable to open" in lowered:
            return SourceResult("permission", [], "source database cannot be opened")
        if "malformed" in lowered or "not a database" in lowered:
            return SourceResult("corrupt", [], "source database is corrupt")
        return SourceResult("corrupt", [], f"source database error: {exc}")
    except sqlite3.DatabaseError as exc:
        return SourceResult("corrupt", [], f"source database is corrupt: {exc}")
    finally:
        if connection is not None:
            connection.close()
