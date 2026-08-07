"""Exact credit verification and optional user-supplied price estimates."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

NANO_PER_CREDIT = Decimal("1000000000")


def credits_from_nano(total_nano_aiu: int) -> Decimal:
    return Decimal(total_nano_aiu) / NANO_PER_CREDIT


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{label} must be finite")
    return number


def recalculate_nano(raw: str | None) -> tuple[str, Decimal | None, str]:
    if raw is None or not str(raw).strip():
        return "missing_json", None, "token_details_json is missing"
    try:
        parsed = json.loads(
            raw,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON number: {value}")),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return "invalid_json", None, "token_details_json is invalid JSON"
    entries = parsed
    if isinstance(parsed, dict):
        entries = parsed.get("entries", parsed.get("details", parsed.get("items")))
    if not isinstance(entries, list):
        return "invalid_json", None, "token_details_json must contain an entry list"
    total = Decimal(0)
    try:
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("detail entry must be an object")
            tokens = _decimal(entry.get("tokenCount"), "tokenCount")
            cost = _decimal(entry.get("costPerBatch"), "costPerBatch")
            batch = _decimal(entry.get("batchSize"), "batchSize")
            if tokens < 0:
                raise ValueError("tokenCount must be nonnegative")
            if cost < 0:
                raise ValueError("costPerBatch must be nonnegative")
            if batch <= 0:
                raise ValueError("batchSize must be positive")
            total += tokens * cost / batch
    except (ValueError, TypeError) as exc:
        return "invalid_json", None, str(exc)
    return "calculated", total, ""


def verification(total_nano: int, raw: str | None) -> tuple[str, Decimal | None, str]:
    state, calculated, message = recalculate_nano(raw)
    if calculated is None:
        return state, None, message
    delta = abs(Decimal(total_nano) - calculated)
    if delta <= Decimal("0.5"):
        return "verified", calculated, ""
    return "mismatch", calculated, f"stored and recalculated totals differ by {delta} nano AIU"


def estimate_costs(
    credits_by_model: dict[str, Decimal],
    rates: dict[str, Any],
    usd_to_nok: Any = None,
    *,
    default_rate: Any = None,
) -> dict[str, Any]:
    per_model: dict[str, Decimal | None] = {}
    complete = True
    for model, credits in credits_by_model.items():
        configured_rate = rates.get(model, default_rate)
        if configured_rate is None:
            per_model[model] = None
            complete = False
            continue
        rate = _decimal(configured_rate, f"rate for {model}")
        if rate < 0:
            raise ValueError(f"rate for {model} must be nonnegative")
        per_model[model] = credits * rate
    total_usd = sum((value for value in per_model.values() if value is not None), Decimal(0)) if complete else None
    total_nok = None
    if total_usd is not None and usd_to_nok is not None:
        exchange = _decimal(usd_to_nok, "usd_to_nok")
        if exchange < 0:
            raise ValueError("usd_to_nok must be nonnegative")
        total_nok = total_usd * exchange
    return {"per_model_usd": per_model, "total_usd": total_usd, "total_nok": total_nok, "complete": complete}
