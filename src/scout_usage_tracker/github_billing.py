"""Explicit GitHub billing sync through the user's existing ``gh`` login."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .billing import BillingError, decimal_value, normalize_plan, validate_snapshot

API_VERSION = "2026-03-10"
TIMEOUT_SECONDS = 20
ENDPOINTS = {
    "user": "/users/{owner}/settings/billing/ai_credit/usage",
    "organization": "/organizations/{owner}/settings/billing/ai_credit/usage",
    "enterprise": "/enterprises/{owner}/settings/billing/ai_credit/usage",
}
OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,98}[A-Za-z0-9])?\Z")


class GitHubBillingError(BillingError):
    pass


def _validate_request(scope: str, owner: str, year: int, month: int) -> None:
    if scope not in ENDPOINTS:
        raise GitHubBillingError("scope must be user, organization, or enterprise")
    if not isinstance(owner, str) or not OWNER_RE.fullmatch(owner):
        raise GitHubBillingError("owner must be a valid GitHub login or slug")
    if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100:
        raise GitHubBillingError("year must be between 2000 and 2100")
    if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
        raise GitHubBillingError("month must be between 1 and 12")


def _run_gh(endpoint: str, fields: list[str] | None = None) -> Any:
    argv = ["gh", "api", "--hostname", "github.com", "--method", "GET", "-H", f"X-GitHub-Api-Version: {API_VERSION}", endpoint]
    for field in fields or []:
        argv.extend(("-f", field))
    try:
        completed = subprocess.run(
            argv, shell=False, check=False, capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise GitHubBillingError("GitHub CLI is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubBillingError("GitHub billing request timed out") from exc
    except OSError as exc:
        raise GitHubBillingError("GitHub billing request could not start") from exc
    if completed.returncode:
        raise GitHubBillingError("GitHub billing request was denied or failed")
    try:
        return json.loads(completed.stdout, parse_float=Decimal, parse_int=int)
    except (json.JSONDecodeError, ValueError) as exc:
        raise GitHubBillingError("GitHub billing response was not valid JSON") from exc


def parse_usage(payload: Any, scope: str, year: int, month: int, captured_at: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("usageItems"), list):
        raise GitHubBillingError("GitHub billing response schema is unsupported")
    allowed_envelope = {"usageItems", "timePeriod", scope}
    if set(payload) != allowed_envelope:
        raise GitHubBillingError("GitHub billing response envelope is unsupported")
    period = payload.get("timePeriod")
    if not isinstance(period, dict) or set(period) != {"year", "month"} or period.get("year") != year or period.get("month") != month:
        raise GitHubBillingError("GitHub billing response period does not match the request")
    if not isinstance(payload[scope], (str, dict)):
        raise GitHubBillingError("GitHub billing response entity is unsupported")
    gross = discount_credits = discount_amount = net_amount = Decimal(0)
    for item in payload["usageItems"]:
        if not isinstance(item, dict):
            raise GitHubBillingError("GitHub billing usage item schema is unsupported")
        required = {"product", "sku", "grossQuantity", "discountQuantity", "discountAmount", "netAmount"}
        if not required.issubset(item):
            raise GitHubBillingError("GitHub billing usage item schema is unsupported")
        if not isinstance(item["product"], str) or not isinstance(item["sku"], str):
            raise GitHubBillingError("GitHub billing product fields are unsupported")
        normalize = lambda value: re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
        product = normalize(item["product"])
        sku = normalize(item["sku"])
        if product not in ("copilot", "copilot_ai_credits") or sku not in ("ai_credit", "ai_credits", "copilot_ai_credit", "copilot_ai_credits"):
            continue
        unit = item.get("unitType")
        if not isinstance(unit, str) or normalize(unit) not in ("ai_credit", "ai_credits", "credit", "credits"):
            raise GitHubBillingError("GitHub billing unit type is unsupported")
        gross += decimal_value(item["grossQuantity"], "grossQuantity") or Decimal(0)
        discount_credits += decimal_value(item["discountQuantity"], "discountQuantity") or Decimal(0)
        discount_amount += decimal_value(item["discountAmount"], "discountAmount") or Decimal(0)
        net_amount += decimal_value(item["netAmount"], "netAmount") or Decimal(0)
    return validate_snapshot({
        "schema_version": 1,
        "source": "github",
        "scope": scope,
        "captured_at": captured_at,
        "year": year,
        "month": month,
        "gross_ai_credits": str(gross),
        "discount_credits": str(discount_credits),
        "discount_amount_usd": str(discount_amount),
        "net_amount_usd": str(net_amount),
    })


def _detect_org_plan(owner: str) -> str | None:
    try:
        payload = _run_gh(f"/orgs/{owner}/copilot/billing")
        if not isinstance(payload, dict) or not isinstance(payload.get("plan_type"), str):
            return None
        plan = normalize_plan(payload["plan_type"])
        return plan if plan in ("business", "enterprise") else None
    except (GitHubBillingError, BillingError):
        return None


def _write_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(path.parent, 0o700)
    text = json.dumps(snapshot, indent=2, sort_keys=True, default=str) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def sync_snapshot(path: str | Path, scope: str, owner: str, year: int, month: int) -> dict[str, Any]:
    _validate_request(scope, owner, year, month)
    endpoint = ENDPOINTS[scope].format(owner=owner)
    payload = _run_gh(endpoint, [f"year={year}", f"month={month}", "product=Copilot"])
    captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    snapshot = parse_usage(payload, scope, year, month, captured_at)
    if scope == "organization":
        plan = _detect_org_plan(owner)
        if plan:
            snapshot["plan_type"] = plan
    _write_snapshot(Path(path), snapshot)
    return snapshot
