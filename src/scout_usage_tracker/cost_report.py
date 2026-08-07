"""Privacy-safe, read-only cost slices for the Scout ``/cost`` skill."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from html import escape
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
    integrity: str
    checked_events: int

    @property
    def credits(self) -> Decimal:
        return credits_from_nano(self.total_nano_aiu)


@dataclass(frozen=True)
class CostReport:
    last_answer: UsageSlice
    thread: UsageSlice
    chat_today: UsageSlice
    chat_week: UsageSlice
    chat_month: UsageSlice
    all_history: UsageSlice
    all_today: UsageSlice
    all_week: UsageSlice
    all_month: UsageSlice
    session_resolution: str


_REQUIRED_USAGE_COLUMNS = {
    "id", "session_id", "turn_index", "model", "input_tokens", "output_tokens",
    "cache_read_tokens", "total_nano_aiu", "token_details_json", "created_at",
}
_PERIODS = {"last", "thread", "all", "day", "week", "month"}
_SCOPES = {"chat", "all"}


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
    integrity, checked = _integrity(rows)
    return UsageSlice(
        sum(item.total_nano_aiu for item in models),
        sum(item.model_calls for item in models),
        sum(item.input_tokens for item in models),
        sum(item.output_tokens for item in models),
        sum(item.cache_read_tokens for item in models),
        models,
        integrity,
        checked,
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

        all_history_rows = connection.execute(
            f"""SELECT {columns} FROM assistant_usage_events
                WHERE session_id <> ? OR turn_index <= ?""",
            (session_id, completed_boundary),
        ).fetchall()
        chat_period_rows: dict[str, list[sqlite3.Row]] = {}
        all_period_rows: dict[str, list[sqlite3.Row]] = {}
        for name, (start, end) in period_bounds.items():
            chat_period_rows[name] = connection.execute(
                f"""SELECT {columns} FROM assistant_usage_events
                    WHERE session_id = ? AND turn_index <= ?
                      AND julianday(created_at) >= julianday(?) AND julianday(created_at) < julianday(?)""",
                (session_id, completed_boundary, _utc_text(start), _utc_text(end)),
            ).fetchall()
            all_period_rows[name] = connection.execute(
                f"""SELECT {columns} FROM assistant_usage_events
                    WHERE julianday(created_at) >= julianday(?) AND julianday(created_at) < julianday(?)
                      AND (session_id <> ? OR turn_index <= ?)""",
                (_utc_text(start), _utc_text(end), session_id, completed_boundary),
            ).fetchall()

        connection.execute("COMMIT")
        return CostReport(
            last_answer=_rows_to_slice(last_rows),
            thread=_rows_to_slice(thread_rows),
            chat_today=_rows_to_slice(chat_period_rows["day"]),
            chat_week=_rows_to_slice(chat_period_rows["week"]),
            chat_month=_rows_to_slice(chat_period_rows["month"]),
            all_history=_rows_to_slice(all_history_rows),
            all_today=_rows_to_slice(all_period_rows["day"]),
            all_week=_rows_to_slice(all_period_rows["week"]),
            all_month=_rows_to_slice(all_period_rows["month"]),
            session_resolution=resolution,
        )
    except CostReportError:
        raise
    except sqlite3.Error as exc:
        raise CostReportError(f"Scout usage database could not be read safely: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()


def _language(value: str) -> str:
    normalized = value.lower().replace("_", "-")
    if normalized.startswith("nb") or normalized.startswith("no"):
        return "nb"
    if normalized.startswith("en"):
        return "en"
    raise ValueError(f"unsupported cost language: {value}")


def _integer(value: int, language: str) -> str:
    rendered = f"{value:,}"
    return rendered.replace(",", " ") if language == "nb" else rendered


def _whole(value: Decimal, language: str) -> str:
    return _integer(int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)), language)


def _tokens(value: int, language: str) -> str:
    if value >= 1_000_000:
        millions = Decimal(value) / Decimal(1_000_000)
        return f"{millions.quantize(Decimal('1'), rounding=ROUND_HALF_UP)}M"
    return _integer(value, language)


def _money(value: Decimal, language: str, places: str = "0.01") -> str:
    rendered = f"{value.quantize(Decimal(places), rounding=ROUND_HALF_UP):,}"
    if language == "nb":
        return rendered.replace(",", "X").replace(".", ",").replace("X", " ")
    return rendered


def _model_name(model: str) -> str:
    if model.startswith("gpt-"):
        parts = model.split("-")
        return "GPT-" + parts[1] + (" " + " ".join(part.title() for part in parts[2:]) if len(parts) > 2 else "")
    if model.startswith("mai-code"):
        return "mai-code"
    return model


def _selected(report: CostReport, period: str, scope: str, language: str) -> tuple[str, UsageSlice]:
    if period not in _PERIODS or scope not in _SCOPES:
        raise ValueError(f"unsupported cost period: {period}")
    chat = {
        "last": report.last_answer,
        "thread": report.thread,
        "day": report.chat_today,
        "week": report.chat_week,
        "month": report.chat_month,
    }
    all_chats = {
        "all": report.all_history,
        "day": report.all_today,
        "week": report.all_week,
        "month": report.all_month,
    }
    selected = chat if scope == "chat" else all_chats
    if period not in selected:
        raise ValueError(f"period {period} is not available for {scope} scope")
    titles = {
        "en": {
            ("chat", "last"): "Last completed answer in this chat",
            ("chat", "thread"): "Current chat so far (may span multiple days)",
            ("chat", "day"): "Current chat today",
            ("chat", "week"): "Current chat this ISO week",
            ("chat", "month"): "Current chat this month",
            ("all", "all"): "All locally retained Scout chats",
            ("all", "day"): "All Scout chats today",
            ("all", "week"): "All Scout chats this ISO week",
            ("all", "month"): "All Scout chats this month",
        },
        "nb": {
            ("chat", "last"): "Siste fullførte svar i denne chatten",
            ("chat", "thread"): "Denne chatten hittil (kan omfatte flere dager)",
            ("chat", "day"): "Denne chatten i dag",
            ("chat", "week"): "Denne chatten denne ISO-uken",
            ("chat", "month"): "Denne chatten denne måneden",
            ("all", "all"): "Alle lokalt beholdte Scout-chatter",
            ("all", "day"): "Alle Scout-chatter i dag",
            ("all", "week"): "Alle Scout-chatter denne ISO-uken",
            ("all", "month"): "Alle Scout-chatter denne måneden",
        },
    }
    return titles[language][(scope, period)], selected[period]


def format_cost_report(
    report: CostReport,
    period: str = "thread",
    rates: dict[str, Any] | None = None,
    usd_to_nok: Any = None,
    *,
    scope: str = "chat",
    language: str = "en",
    default_usd_per_credit: Any = "0.01",
    dashboard_uri: str | None = None,
) -> str:
    language = _language(language)
    title, usage = _selected(report, period, scope, language)
    if language == "nb":
        lines = [
            f"{title}:", "",
            f"**{_integer(usage.model_calls, language)} modellkall**",
            f"**{_whole(usage.credits, language)} Scout-credits**",
            f"Input: **{_tokens(usage.input_tokens, language)} tokens**",
            f"Output: **{_tokens(usage.output_tokens, language)} tokens**",
            f"Cache-read: **{_tokens(usage.cache_read_tokens, language)} tokens**",
        ]
    else:
        lines = [
            f"{title}:", "",
            f"**{_integer(usage.model_calls, language)} model calls**",
            f"**{_whole(usage.credits, language)} Scout credits**",
            f"Input: **{_tokens(usage.input_tokens, language)} tokens**",
            f"Output: **{_tokens(usage.output_tokens, language)} tokens**",
            f"Cache read: **{_tokens(usage.cache_read_tokens, language)} tokens**",
        ]

    pricing = estimate_costs(
        {item.model: item.credits for item in usage.models}, rates or {}, usd_to_nok,
        default_rate=default_usd_per_credit,
    )
    priced = [(item, pricing["per_model_usd"][item.model]) for item in usage.models
              if pricing["per_model_usd"].get(item.model) is not None]
    unpriced = [item for item in usage.models if pricing["per_model_usd"].get(item.model) is None]
    lines.extend(["", "Kostnadsestimat:" if language == "nb" else "Estimated gross value:"])
    if priced:
        exchange = Decimal(str(usd_to_nok)) if usd_to_nok is not None else None
        for item, usd in priced:
            cost = ("ca. **" if language == "nb" else "approx. **") + f"USD {_money(usd, language)}"
            if exchange is not None:
                cost += f" / NOK {_money(usd * exchange, language, '1')}"
            label = f"{_model_name(item.model)}-delen" if language == "nb" else _model_name(item.model)
            lines.append(f"{label}: {cost}**")
    else:
        lines.append("**—** Ingen credit-pris er konfigurert." if language == "nb" else "**—** No credit price is configured.")
    if unpriced:
        credits = sum((item.credits for item in unpriced), Decimal(0))
        names = ", ".join(dict.fromkeys(_model_name(item.model) for item in unpriced))
        if language == "nb":
            lines.append(f"Uten pris: **{_money(credits, language)} credits** fra {names}.")
        else:
            lines.append(f"Unpriced: **{_money(credits, language)} credits** from {names}.")
    safe_link = None if not dashboard_uri else f'<a href="{escape(dashboard_uri, quote=True)}">Usage tracker</a>'
    if language == "nb":
        lines.extend(["", "Dette er Scout-only og inkluderer ikke GitHub Copilot-appen eller andre Copilot-klienter."])
        if safe_link:
            lines.append(f"Sjekk {safe_link} for detaljer og historikk.")
        lines.extend(["", "Vil du vite hvordan du bruker `/cost` til flere oppgaver? Skriv “`/cost FAQ`”."])
    else:
        lines.extend(["", "This is Scout-only and excludes the GitHub Copilot app and other Copilot clients."])
        if safe_link:
            lines.append(f"Check {safe_link} for details and history.")
        lines.extend(["", "Want to learn more ways to use `/cost`? Type “`/cost FAQ`”."])
    return "\n".join(lines)


def format_cost_faq(language: str = "en") -> str:
    """Return usage help without reading or exposing any Scout usage data."""
    language = _language(language)
    if language == "nb":
        return "\n".join([
            "Slik bruker du `/cost`:", "",
            "- `/cost` — denne chatten hittil",
            "- `/cost i dag` — denne chatten i dag",
            "- `/cost denne uken` — denne chatten denne ISO-uken",
            "- `/cost denne måneden` — denne chatten denne måneden",
            "- `/cost siste svar` — siste fullførte svar i denne chatten",
            "- `/cost alle chatter` — alle lokalt beholdte Scout-chatter",
            "- `/cost alle chatter i dag` — alle Scout-chatter i dag",
            "- `/cost alle chatter denne uken` — alle Scout-chatter denne ISO-uken",
            "- `/cost alle chatter denne måneden` — alle Scout-chatter denne måneden",
            "", "Dager, uker og måneder gjelder alltid denne chatten med mindre du eksplisitt skriver «alle chatter».",
            "Credits beregnes eksakt fra nano-AIU, men vises avrundet til hele credits. USD/NOK er bruttoestimater, ikke en faktura; inkluderte credits kan gjøre faktisk belastning lavere eller null.",
            "AIU-verifisering og full historikk vises i det lokale dashboardet.",
        ])
    return "\n".join([
        "How to use `/cost`:", "",
        "- `/cost` — current chat so far",
        "- `/cost today` — current chat today",
        "- `/cost this week` — current chat this ISO week",
        "- `/cost this month` — current chat this month",
        "- `/cost last answer` — last completed answer in this chat",
        "- `/cost all chats` — all locally retained Scout chats",
        "- `/cost all chats today` — all Scout chats today",
        "- `/cost all chats this week` — all Scout chats this ISO week",
        "- `/cost all chats this month` — all Scout chats this month",
        "", "Day, week, and month always mean the current chat unless you explicitly say all chats.",
        "Credits are calculated exactly from nano-AIU but displayed as rounded whole credits. USD/NOK values are gross estimates, not a bill; included credits may make the actual charge lower or zero.",
        "AIU verification and full history are available in the local dashboard.",
    ])
