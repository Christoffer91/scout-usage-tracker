"""Privacy-safe, read-only cost slices for the Scout ``/cost`` skill."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .aggregate import local_zone
from .pricing import credits_from_nano, estimate_costs, verification


class CostReportError(RuntimeError):
    """A safe cost report cannot be produced from the current local state."""


@dataclass(frozen=True)
class ModelUsage:
    model: str
    total_nano_aiu: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int

    @property
    def credits(self) -> Decimal:
        return credits_from_nano(self.total_nano_aiu)


@dataclass(frozen=True)
class UsageSlice:
    total_nano_aiu: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    models: tuple[ModelUsage, ...]

    @property
    def credits(self) -> Decimal:
        return credits_from_nano(self.total_nano_aiu)


@dataclass(frozen=True)
class CostReport:
    last_answer: UsageSlice
    thread: UsageSlice
    today: UsageSlice
    week: UsageSlice
    month: UsageSlice
    integrity: str
    checked_events: int
    session_resolution: str


_REQUIRED_USAGE_COLUMNS = {
    "id", "session_id", "turn_index", "model", "input_tokens", "output_tokens",
    "cache_read_tokens", "total_nano_aiu", "token_details_json", "created_at",
}
_PERIODS = {"last", "thread", "day", "week", "month"}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _value(row: sqlite3.Row, name: str) -> int:
    return int(row[name] or 0)


def _rows_to_slice(rows: list[sqlite3.Row]) -> UsageSlice:
    grouped: dict[str, dict[str, int]] = {}
    for row in rows:
        model = str(row["model"])
        values = grouped.setdefault(model, {"nano": 0, "calls": 0, "input": 0, "output": 0, "cache": 0})
        values["nano"] += _value(row, "total_nano_aiu")
        values["calls"] += 1
        values["input"] += _value(row, "input_tokens")
        values["output"] += _value(row, "output_tokens")
        values["cache"] += _value(row, "cache_read_tokens")
    models = tuple(
        ModelUsage(model, values["nano"], values["calls"], values["input"], values["output"], values["cache"])
        for model, values in sorted(grouped.items(), key=lambda item: (-item[1]["nano"], item[0]))
    )
    return UsageSlice(
        sum(item.total_nano_aiu for item in models),
        sum(item.model_calls for item in models),
        sum(item.input_tokens for item in models),
        sum(item.output_tokens for item in models),
        sum(item.cache_read_tokens for item in models),
        models,
    )


def _integrity(rows: list[sqlite3.Row]) -> tuple[str, int]:
    states = [verification(_value(row, "total_nano_aiu"), row["token_details_json"])[0] for row in rows]
    if "mismatch" in states:
        return "failed", len(states)
    if any(state != "verified" for state in states):
        return "warning", len(states)
    return "pass", len(states)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounds(day: date, zone: ZoneInfo) -> dict[str, tuple[datetime, datetime]]:
    day_start = datetime.combine(day, time.min, zone)
    week_start = day_start - timedelta(days=day.weekday())
    month_start = datetime(day.year, day.month, 1, tzinfo=zone)
    next_month = datetime(day.year + (day.month == 12), 1 if day.month == 12 else day.month + 1, 1, tzinfo=zone)
    return {
        "day": (day_start, day_start + timedelta(days=1)),
        "week": (week_start, week_start + timedelta(days=7)),
        "month": (month_start, next_month),
    }


def _resolve_recent_session(connection: sqlite3.Connection, now: datetime) -> str:
    """Resolve only a uniquely fresh session when Scout omits SESSION_ID."""
    candidates = connection.execute(
        """SELECT session_id,
                  MAX((julianday(created_at) - 2440587.5) * 86400.0) AS latest_epoch
           FROM assistant_usage_events
           GROUP BY session_id
           ORDER BY latest_epoch DESC
           LIMIT 2"""
    ).fetchall()
    if not candidates:
        raise CostReportError("no recent Scout usage event could identify this conversation")
    newest = float(candidates[0]["latest_epoch"])
    age = now.astimezone(timezone.utc).timestamp() - newest
    if age < -5 or age > 30:
        raise CostReportError("no uniquely fresh Scout usage event could identify this conversation")
    if len(candidates) > 1 and newest - float(candidates[1]["latest_epoch"]) < 3:
        raise CostReportError("multiple Scout conversations are active; wait a few seconds and retry /cost")
    return str(candidates[0]["session_id"])


def build_cost_report(
    source_database: str,
    session_id: str,
    timezone_name: str,
    *,
    now: datetime | None = None,
    _after_snapshot: Callable[[], None] | None = None,
) -> CostReport:
    """Read exact nano-AIU slices from one consistent SQLite snapshot."""
    source = Path(source_database).expanduser()
    if not source.is_file():
        raise CostReportError("Scout usage database was not found")

    zone = local_zone(timezone_name)
    if not isinstance(zone, ZoneInfo):
        raise CostReportError("configure an IANA timezone (for example Europe/Oslo) before using /cost")
    current = now or datetime.now(timezone.utc)
    period_bounds = _bounds(current.astimezone(zone).date(), zone)

    uri = "file:" + quote(str(source.resolve()), safe="/") + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if not _REQUIRED_USAGE_COLUMNS.issubset(_columns(connection, "assistant_usage_events")):
            raise CostReportError("Scout usage database has an unsupported assistant_usage_events schema")

        connection.execute("BEGIN")
        resolution = "environment"
        if not session_id.strip():
            session_id = _resolve_recent_session(connection, current)
            resolution = "recent_event"
        # The model call that invokes /cost is already the highest usage turn.
        # This first read fixes the SQLite snapshot and lets us exclude that turn
        # without relying on assistant_response, which unattended Scout surfaces
        # may leave NULL even after a completed run.
        active_turn = connection.execute(
            "SELECT MAX(turn_index) FROM assistant_usage_events WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        if _after_snapshot is not None:
            _after_snapshot()
        if active_turn is None:
            raise CostReportError("no Scout usage events were found for the active conversation")
        completed_boundary = int(active_turn) - 1

        columns = (
            "id, model, input_tokens, output_tokens, cache_read_tokens, "
            "total_nano_aiu, token_details_json"
        )
        last_turn = connection.execute(
            "SELECT MAX(turn_index) FROM assistant_usage_events WHERE session_id = ? AND turn_index <= ?",
            (session_id, completed_boundary),
        ).fetchone()[0]
        last_rows = [] if last_turn is None else connection.execute(
            f"SELECT {columns} FROM assistant_usage_events WHERE session_id = ? AND turn_index = ?",
            (session_id, last_turn),
        ).fetchall()
        thread_rows = connection.execute(
            f"SELECT {columns} FROM assistant_usage_events WHERE session_id = ? AND turn_index <= ?",
            (session_id, completed_boundary),
        ).fetchall()

        period_rows: dict[str, list[sqlite3.Row]] = {}
        for name, (start, end) in period_bounds.items():
            period_rows[name] = connection.execute(
                f"""SELECT {columns} FROM assistant_usage_events
                    WHERE julianday(created_at) >= julianday(?) AND julianday(created_at) < julianday(?)
                      AND (session_id <> ? OR turn_index <= ?)""",
                (_utc_text(start), _utc_text(end), session_id, completed_boundary),
            ).fetchall()

        all_groups = (last_rows, thread_rows, *period_rows.values())
        unique = {str(row["id"]): row for rows in all_groups for row in rows}
        integrity, checked = _integrity(list(unique.values()))
        connection.execute("COMMIT")
        return CostReport(
            last_answer=_rows_to_slice(last_rows),
            thread=_rows_to_slice(thread_rows),
            today=_rows_to_slice(period_rows["day"]),
            week=_rows_to_slice(period_rows["week"]),
            month=_rows_to_slice(period_rows["month"]),
            integrity=integrity,
            checked_events=checked,
            session_resolution=resolution,
        )
    except CostReportError:
        raise
    except sqlite3.Error as exc:
        raise CostReportError(f"Scout usage database could not be read safely: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()


def _whole(value: Decimal) -> str:
    return f"{value.quantize(Decimal('1'), rounding=ROUND_HALF_UP):,}".replace(",", " ")


def _integer(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _tokens(value: int) -> str:
    if value >= 1_000_000:
        millions = Decimal(value) / Decimal(1_000_000)
        return f"{millions.quantize(Decimal('1'), rounding=ROUND_HALF_UP)}M"
    return _integer(value)


def _money(value: Decimal, places: str = "0.01") -> str:
    rendered = f"{value.quantize(Decimal(places), rounding=ROUND_HALF_UP):,}"
    return rendered.replace(",", "X").replace(".", ",").replace("X", " ")


def _model_name(model: str) -> str:
    if model.startswith("gpt-"):
        parts = model.split("-")
        return "GPT-" + parts[1] + (" " + " ".join(part.title() for part in parts[2:]) if len(parts) > 2 else "")
    if model.startswith("mai-code"):
        return "mai-code"
    return model


def _selected(report: CostReport, period: str) -> tuple[str, UsageSlice]:
    if period not in _PERIODS:
        raise ValueError(f"unsupported cost period: {period}")
    return {
        "last": ("Siste fullførte svar", report.last_answer),
        "thread": ("Denne chatten hittil", report.thread),
        "day": ("I dag", report.today),
        "week": ("Denne ISO-uken", report.week),
        "month": ("Denne måneden", report.month),
    }[period]


def format_cost_report(
    report: CostReport,
    period: str = "thread",
    rates: dict[str, Any] | None = None,
    usd_to_nok: Any = None,
) -> str:
    title, usage = _selected(report, period)
    lines = [
        f"{title}:",
        "",
        f"**{_integer(usage.model_calls)} modellkall**",
        f"**{_whole(usage.credits)} Scout-credits** · eksakt beregnet fra nano-AIU, avrundet visning",
        f"Input: **{_tokens(usage.input_tokens)} tokens**",
        f"Output: **{_tokens(usage.output_tokens)} tokens**",
        f"Cache-read: **{_tokens(usage.cache_read_tokens)} tokens**",
    ]

    pricing = estimate_costs({item.model: item.credits for item in usage.models}, rates or {}, usd_to_nok)
    priced = [(item, pricing["per_model_usd"][item.model]) for item in usage.models
              if pricing["per_model_usd"].get(item.model) is not None]
    unpriced = [item for item in usage.models if pricing["per_model_usd"].get(item.model) is None]
    lines.extend(["", "Kostnadsestimat:"])
    if priced:
        exchange = Decimal(str(usd_to_nok)) if usd_to_nok is not None else None
        for item, usd in priced:
            cost = f"ca. **{_money(usd)} USD"
            if exchange is not None:
                cost += f" / {_money(usd * exchange, '1')} NOK"
            lines.append(f"{_model_name(item.model)}-delen: {cost}**")
    else:
        lines.append("**—** Ingen modellpris er konfigurert for denne perioden.")
    if unpriced:
        credits = sum((item.credits for item in unpriced), Decimal(0))
        names = ", ".join(dict.fromkeys(_model_name(item.model) for item in unpriced))
        lines.append(f"I tillegg: **{_money(credits)} credits** fra {names} uten konfigurert prisrate.")
    lines.extend([
        "Estimatet er ikke en faktura; inkluderte credits kan gjøre faktisk belastning lavere eller null.",
        "",
        "Dette er Scout-only og inkluderer ikke GitHub Copilot-appen eller andre Copilot-klienter.",
        f"AIU-kontroll: **{report.integrity}** ({_integer(report.checked_events)} events kontrollert).",
        "",
        "Vil du se bruken i dag, hele uken, hele måneden eller kun siste fullførte svar?",
    ])
    return "\n".join(lines)
