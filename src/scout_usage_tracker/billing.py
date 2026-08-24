"""Plan context and invoice-safe billing estimates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

CATALOG_AS_OF = "2026-08-06"
AI_CREDIT_USD = Decimal("0.01")

# Allowances are monthly. Business and Enterprise values are per seat and
# pooled at the billing entity; they are never allocated to one user here.
PLAN_CATALOG: dict[str, dict[str, Any]] = {
    "free": {"label": "Free", "included_credits": None, "monthly_price_usd": Decimal("0"), "pooled": False},
    "pro": {"label": "Pro", "included_credits": Decimal("1500"), "monthly_price_usd": Decimal("10"), "pooled": False},
    "pro_plus": {"label": "Pro+", "included_credits": Decimal("7000"), "monthly_price_usd": Decimal("39"), "pooled": False},
    "max": {"label": "Max", "included_credits": Decimal("20000"), "monthly_price_usd": Decimal("100"), "pooled": False},
    "business": {"label": "Business", "included_credits": Decimal("1900"), "monthly_price_usd": Decimal("19"), "pooled": True, "promotional_allowance": Decimal("3000")},
    "enterprise": {"label": "Enterprise", "included_credits": Decimal("3900"), "monthly_price_usd": Decimal("39"), "pooled": True, "promotional_allowance": Decimal("7000")},
    "custom": {"label": "Custom", "included_credits": None, "monthly_price_usd": None, "pooled": False},
    "unknown": {"label": "Unknown", "included_credits": None, "monthly_price_usd": None, "pooled": False},
}

SNAPSHOT_KEYS = {
    "schema_version", "source", "scope", "captured_at", "year", "month",
    "gross_ai_credits", "discount_credits", "discount_amount_usd",
    "net_amount_usd", "plan_type",
}


class BillingError(ValueError):
    pass


def decimal_value(value: Any, label: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise BillingError(f"{label} must be a nonnegative finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BillingError(f"{label} must be a nonnegative finite number") from exc
    if not number.is_finite() or number < 0:
        raise BillingError(f"{label} must be a nonnegative finite number")
    return number


def normalize_plan(value: Any) -> str:
    if not isinstance(value, str):
        raise BillingError("billing.plan must be text")
    normalized = value.strip().lower().replace("+", "_plus").replace("-", "_")
    if normalized not in PLAN_CATALOG:
        raise BillingError("billing.plan must be free, pro, pro+, pro_plus, max, business, enterprise, custom, or unknown")
    return normalized


def validate_snapshot(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BillingError("billing snapshot root must be an object")
    unknown = set(raw) - SNAPSHOT_KEYS
    if unknown:
        raise BillingError("unknown billing snapshot keys: " + ", ".join(sorted(unknown)))
    if raw.get("schema_version") != 1:
        raise BillingError("unsupported billing snapshot schema_version")
    source = raw.get("source")
    if source not in ("github", "manual"):
        raise BillingError("billing snapshot source must be github or manual")
    scope = raw.get("scope")
    if scope not in ("user", "organization", "enterprise"):
        raise BillingError("billing snapshot scope must be user, organization, or enterprise")
    year, month = raw.get("year"), raw.get("month")
    if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100:
        raise BillingError("billing snapshot year is invalid")
    if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
        raise BillingError("billing snapshot month is invalid")
    captured_at = raw.get("captured_at")
    if not isinstance(captured_at, str):
        raise BillingError("billing snapshot captured_at must be UTC text")
    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BillingError("billing snapshot captured_at must be ISO 8601") from exc
    if captured.tzinfo is None or captured.utcoffset() != timezone.utc.utcoffset(captured):
        raise BillingError("billing snapshot captured_at must be UTC")
    result = {
        "schema_version": 1,
        "source": source,
        "scope": scope,
        "captured_at": captured_at,
        "year": year,
        "month": month,
    }
    for key in ("gross_ai_credits", "discount_credits", "discount_amount_usd", "net_amount_usd"):
        result[key] = decimal_value(raw.get(key), key)
    plan_type = raw.get("plan_type")
    if plan_type is not None:
        if source != "github" or scope != "organization":
            raise BillingError("billing snapshot plan_type is allowed only for GitHub organization snapshots")
        result["plan_type"] = normalize_plan(plan_type)
    return result


def load_snapshot(path: str | Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    snapshot_path = Path(path)
    if not snapshot_path.is_file():
        return None
    try:
        raw = json.loads(snapshot_path.read_text(encoding="utf-8"), parse_float=Decimal, parse_int=int)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise BillingError(f"cannot load billing snapshot: {exc}") from exc
    return validate_snapshot(raw)


def billing_summary(config: dict[str, Any], scout_credits: Decimal, *, now: datetime | None = None) -> dict[str, Any]:
    billing = config.get("billing") or {}
    enabled = bool(billing.get("enabled", False))
    current = now or datetime.now(timezone.utc)
    plan_key = normalize_plan(billing.get("plan", "unknown")) if enabled else "unknown"
    catalog = PLAN_CATALOG[plan_key]
    included = billing.get("included_credits")
    allowance_source = "custom override" if included is not None else f"catalog dated {CATALOG_AS_OF}"
    if included is None:
        included = catalog["included_credits"]
    else:
        included = decimal_value(included, "billing.included_credits")
    promotional = billing.get("promotional_allowance")
    if promotional is True:
        if "promotional_allowance" not in catalog:
            raise BillingError("billing.promotional_allowance eligibility is unavailable for this plan")
        if current.year != 2026 or current.month not in (6, 7, 8):
            raise BillingError("catalog promotional allowance applies only from June through August 2026")
        included = catalog["promotional_allowance"]
        allowance_source = "explicit promotional eligibility"
    elif promotional not in (None, False):
        included = decimal_value(promotional, "billing.promotional_allowance")
        allowance_source = "custom promotional override"
    monthly_price = billing.get("monthly_price_usd")
    price_source = "custom override" if monthly_price is not None else f"catalog dated {CATALOG_AS_OF}"
    if monthly_price is None:
        monthly_price = catalog["monthly_price_usd"]
    else:
        monthly_price = decimal_value(monthly_price, "billing.monthly_price_usd")

    seat_count = billing.get("seat_count")
    pooled = bool(catalog["pooled"])
    effective_allowance = included
    if pooled:
        effective_allowance = included * seat_count if included is not None and seat_count is not None else None

    snapshot = load_snapshot(billing.get("snapshot_path")) if enabled else None
    period_matches = bool(snapshot and snapshot["year"] == current.year and snapshot["month"] == current.month)
    scope_matches = bool(snapshot and ((pooled and snapshot["scope"] in ("organization", "enterprise")) or (not pooled and snapshot["scope"] == "user")))
    estimated_additional = None
    if snapshot and period_matches and scope_matches and effective_allowance is not None:
        estimated_additional = max(snapshot["gross_ai_credits"] - effective_allowance, Decimal(0))
    secondary = config.get("secondary_currency")
    if secondary is None and config.get("usd_to_nok") is not None:
        secondary = {"code": "NOK", "usd_rate": config["usd_to_nok"]}
    currency_code = secondary.get("code") if secondary else None
    exchange = decimal_value(secondary.get("usd_rate"), "secondary_currency.usd_rate") if secondary else None
    gross_scout_usd = scout_credits * AI_CREDIT_USD if enabled else None
    additional_usd = estimated_additional * AI_CREDIT_USD if estimated_additional is not None else None
    return {
        "enabled": enabled,
        "plan": plan_key,
        "label": catalog["label"],
        "pooled": pooled,
        "included_credits": included,
        "effective_allowance": effective_allowance,
        "allowance_source": allowance_source,
        "monthly_price_usd": monthly_price,
        "price_source": price_source,
        "seat_count": seat_count,
        "snapshot": snapshot,
        "period_matches": period_matches,
        "scope_matches": scope_matches,
        "estimated_additional_credits": estimated_additional,
        "estimated_additional_usd": additional_usd,
        "secondary_currency_code": currency_code,
        "secondary_currency_rate": exchange,
        "estimated_additional_secondary": additional_usd * exchange if additional_usd is not None and exchange is not None else None,
        "estimated_additional_nok": additional_usd * exchange if additional_usd is not None and currency_code == "NOK" else None,
        "estimated_gross_scout_usd": gross_scout_usd,
        "estimated_gross_scout_secondary": gross_scout_usd * exchange if gross_scout_usd is not None and exchange is not None else None,
        "estimated_gross_scout_nok": gross_scout_usd * exchange if gross_scout_usd is not None and currency_code == "NOK" else None,
    }
