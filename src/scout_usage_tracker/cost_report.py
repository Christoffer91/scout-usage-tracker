"""Privacy-safe, read-only cost slices for the Scout ``/cost`` skill."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .aggregate import local_zone
from .pricing import credits_from_nano, verification


class CostReportError(RuntimeError):
    """A safe cost report cannot be produced from the current local state."""


@dataclass(frozen=True)
class UsageSlice:
    total_nano_aiu: int
    tool_calls: int

    @property
    def credits(self) -> Decimal:
        return credits_from_nano(self.total_nano_aiu)


@dataclass(frozen=True)
class CostReport:
    last_answer: UsageSlice
    thread: UsageSlice
    today: UsageSlice
    integrity: str
    checked_events: int


_REQUIRED_USAGE_COLUMNS = {
    "id", "session_id", "turn_index", "total_nano_aiu", "token_details_json", "created_at",
}
_REQUIRED_TURN_COLUMNS = {"session_id", "turn_index", "assistant_response"}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _rows_to_slice(rows: list[sqlite3.Row]) -> UsageSlice:
    return UsageSlice(sum(int(row["total_nano_aiu"]) for row in rows), len(rows))


def _integrity(rows: list[sqlite3.Row]) -> tuple[str, int]:
    states = [verification(int(row["total_nano_aiu"]), row["token_details_json"])[0] for row in rows]
    if "mismatch" in states:
        return "failed", len(states)
    if any(state != "verified" for state in states):
        return "warning", len(states)
    return "pass", len(states)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_cost_report(
    source_database: str,
    session_id: str,
    timezone_name: str,
    *,
    now: datetime | None = None,
    _after_snapshot: Callable[[], None] | None = None,
) -> CostReport:
    """Read three exact nano-AIU slices from one consistent SQLite snapshot.

    The private callback is solely a deterministic concurrency-test barrier.
    """
    if not session_id.strip():
        raise CostReportError("no active Scout conversation was identified")
    source = Path(source_database).expanduser()
    if not source.is_file():
        raise CostReportError("Scout usage database was not found")

    zone = local_zone(timezone_name)
    if not isinstance(zone, ZoneInfo):
        raise CostReportError("configure an IANA timezone (for example Europe/Oslo) before using /cost")
    current = now or datetime.now(timezone.utc)
    local_date = current.astimezone(zone).date()
    start_local = datetime.combine(local_date, time.min, zone)
    end_local = datetime.combine(local_date + timedelta(days=1), time.min, zone)

    uri = "file:" + quote(str(source.resolve()), safe="/") + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if not _REQUIRED_USAGE_COLUMNS.issubset(_columns(connection, "assistant_usage_events")):
            raise CostReportError("Scout usage database has an unsupported assistant_usage_events schema")
        if not _REQUIRED_TURN_COLUMNS.issubset(_columns(connection, "turns")):
            raise CostReportError("Scout usage database has an unsupported turns schema")

        connection.execute("BEGIN")
        # This SELECT is deliberately the first read after BEGIN: it establishes
        # the SQLite snapshot before any concurrent Scout writer can affect it.
        completed = connection.execute(
            "SELECT MAX(turn_index) FROM turns WHERE session_id = ? AND assistant_response IS NOT NULL",
            (session_id,),
        ).fetchone()[0]
        if _after_snapshot is not None:
            _after_snapshot()
        if completed is None:
            raise CostReportError("the active Scout conversation has no completed answer yet")

        columns = "id, total_nano_aiu, token_details_json"
        last_rows = connection.execute(
            f"SELECT {columns} FROM assistant_usage_events WHERE session_id = ? AND turn_index = ?",
            (session_id, completed),
        ).fetchall()
        thread_rows = connection.execute(
            f"SELECT {columns} FROM assistant_usage_events WHERE session_id = ? AND turn_index <= ?",
            (session_id, completed),
        ).fetchall()
        today_rows = connection.execute(
            f"""SELECT {columns} FROM assistant_usage_events
                WHERE julianday(created_at) >= julianday(?) AND julianday(created_at) < julianday(?)
                  AND (session_id <> ? OR turn_index <= ?)""",
            (_utc_text(start_local), _utc_text(end_local), session_id, completed),
        ).fetchall()

        unique = {str(row["id"]): row for rows in (last_rows, thread_rows, today_rows) for row in rows}
        integrity, checked = _integrity(list(unique.values()))
        connection.execute("COMMIT")
        return CostReport(
            last_answer=_rows_to_slice(last_rows),
            thread=_rows_to_slice(thread_rows),
            today=_rows_to_slice(today_rows),
            integrity=integrity,
            checked_events=checked,
        )
    except CostReportError:
        raise
    except sqlite3.Error as exc:
        raise CostReportError(f"Scout usage database could not be read safely: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()


def _whole(credits: Decimal) -> str:
    return f"{credits.quantize(Decimal('1'), rounding=ROUND_HALF_UP):,}"


def format_cost_report(report: CostReport) -> str:
    lines = [
        "Scout credits before this /cost request",
        f"Last completed answer: ≈{_whole(report.last_answer.credits)} credits · {report.last_answer.tool_calls:,} tool calls",
        f"Completed thread: ≈{_whole(report.thread.credits)} credits · {report.thread.tool_calls:,} tool calls",
        f"Today (all local Scout chats): ≈{_whole(report.today.credits)} credits · {report.today.tool_calls:,} tool calls",
        f"AIU data: {report.integrity} ({report.checked_events:,} events checked)",
        "Displayed credits are rounded to whole numbers; the underlying nano-AIU calculation is exact.",
    ]
    return "\n".join(lines)
