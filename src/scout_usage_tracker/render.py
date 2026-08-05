"""Render the standalone, network-free dashboard."""

from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .aggregate import aggregate
from .config import atomic_write
from .database import connect_history, secure_history_files
from .history import active_events, latest_run
from .pricing import estimate_costs


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _decimal(value: Decimal, places: int = 9) -> str:
    text = f"{value:.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _number(value: int) -> str:
    return f"{value:,}"


def _money(value: Decimal | None, currency: str) -> str:
    return "—" if value is None else f"{currency} {_decimal(value, 4)}"


def _table(title: str, rows: list[dict[str, Any]], prices: dict[str, Decimal | None] | None = None) -> str:
    body = []
    for row in rows:
        price = ""
        if prices is not None:
            price = f"<td>{_escape(_money(prices.get(row['label']), 'USD'))}</td>"
        share = "—" if row["cache_share"] is None else f"{row['cache_share'] * 100:.1f}%"
        body.append(
            "<tr><th scope=\"row\">" + _escape(row["label"]) + "</th>"
            f"<td>{_escape(_decimal(row['credits']))}</td><td>{_number(row['calls'])}</td>"
            f"<td>{_number(row['input'])}</td><td>{_number(row['output'])}</td>"
            f"<td>{_number(row['cache_read'])}</td><td>{_escape(share)}</td>{price}</tr>"
        )
    price_head = "<th scope=\"col\">Estimated cost</th>" if prices is not None else ""
    return (
        f"<section><h2>{_escape(title)}</h2><div class=\"table-wrap\"><table>"
        f"<caption>{_escape(title)} usage breakdown</caption><thead><tr>"
        "<th scope=\"col\">Period or group</th><th scope=\"col\">Exact credits</th>"
        "<th scope=\"col\">Calls</th><th scope=\"col\">Input</th><th scope=\"col\">Output</th>"
        f"<th scope=\"col\">Cache read</th><th scope=\"col\">Cache share</th>{price_head}"
        "</tr></thead><tbody>" + ("".join(body) or "<tr><td colspan=\"8\">No usage events.</td></tr>") + "</tbody></table></div></section>"
    )


def render_dashboard(config: dict[str, Any], template_path: str | Path | None = None) -> Path:
    history_path = Path(config["history_database"])
    connection = connect_history(history_path)
    try:
        rows = active_events(connection)
        run = latest_run(connection)
    finally:
        secure_history_files(history_path)
        connection.close()
    data = aggregate(rows, config["timezone"], config["privacy"]["include_sessions"])
    credits_by_model = {item["label"]: item["credits"] for item in data["groups"]["model"]}
    estimates = estimate_costs(credits_by_model, config.get("usd_per_credit_by_model", {}), config.get("usd_to_nok"))
    model_prices = estimates["per_model_usd"]
    sections = [
        _table("By day", data["groups"]["day"]),
        _table("By ISO week", data["groups"]["week"]),
        _table("By month", data["groups"]["month"]),
        _table("By model", data["groups"]["model"], model_prices),
    ]
    if config["privacy"]["include_sessions"]:
        sections.append(_table("By anonymized session", data["groups"].get("session", [])))
    total = data["total"]
    cache_share = "—" if total["cache_share"] is None else f"{total['cache_share'] * 100:.1f}%"
    verification_counts = data["verification"]
    incomplete = verification_counts.get("invalid_json", 0) + verification_counts.get("missing_json", 0)
    run_incomplete = run is not None and (run["rows_skipped"] > 0 or bool(run["possible_id_gap"]))
    if incomplete or not rows or run_incomplete:
        overall_verification = "INCOMPLETE"
    elif verification_counts.get("mismatch", 0):
        overall_verification = "MISMATCH"
    elif verification_counts.get("verified", 0) == len(rows):
        overall_verification = "PASS"
    else:
        overall_verification = "INCOMPLETE"
    verification = ", ".join(f"{_escape(key)}: {_number(value)}" for key, value in verification_counts.items()) or "No events"
    warnings = [] if run is None else json.loads(run["warnings_json"])
    if run is not None and run["possible_id_gap"]:
        warnings.append("Possible source history gap detected from non-contiguous source row identifiers.")
    warning_html = "".join(f"<li>{_escape(item)}</li>" for item in warnings) or "<li>None reported.</li>"
    comparison = config.get("account_comparison") or {}
    comparison_html = ""
    if comparison:
        comparison_html = (
            "<section><h2>Manual account-wide comparison</h2><dl>"
            f"<dt>Account-wide Copilot total (manual)</dt><dd>{_escape(comparison.get('total', '—'))}</dd>"
            f"<dt>Account-wide additional usage in USD (manual)</dt><dd>{_escape(comparison.get('additional_usage_usd', '—'))}</dd>"
            f"<dt>As of</dt><dd>{_escape(comparison.get('as_of', '—'))}</dd>"
            f"<dt>Scope</dt><dd>{_escape(comparison.get('scope', 'account-wide/manual'))}</dd>"
            "</dl><p>These are account-wide manual values, never Scout-only measurements.</p></section>"
        )
    update_time = config.get("_generated_at", datetime.now(timezone.utc).isoformat())
    values = {
        "TITLE": "Scout Usage Tracker",
        "UPDATE_TIME": _escape(update_time),
        "TIMEZONE": _escape(data["timezone"]),
        "TOTAL_CREDITS": _escape(_decimal(total["credits"])),
        "CALLS": _number(total["calls"]),
        "INPUT": _number(total["input"]),
        "OUTPUT": _number(total["output"]),
        "CACHE_READ": _number(total["cache_read"]),
        "CACHE_SHARE": _escape(cache_share),
        "TOTAL_USD": _escape(_money(estimates["total_usd"], "USD")),
        "TOTAL_NOK": _escape(_money(estimates["total_nok"], "NOK")),
        "FIRST_EVENT": _escape(data["first_event"] or "—"),
        "LAST_EVENT": _escape(data["last_event"] or "—"),
        "VERIFICATION": verification,
        "VERIFICATION_OVERALL": overall_verification,
        "WARNINGS": warning_html,
        "SECTIONS": "".join(sections),
        "COMPARISON": comparison_html,
    }
    if template_path is None:
        template_path = Path(__file__).resolve().parents[2] / "templates" / "dashboard.html"
    document = Path(template_path).read_text(encoding="utf-8")
    for key, value in values.items():
        document = document.replace("{{" + key + "}}", value)
    target = Path(config["dashboard_path"])
    atomic_write(target, document, 0o600)
    os.chmod(target, 0o600)
    return target
