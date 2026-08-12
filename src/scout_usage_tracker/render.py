"""Render the standalone, network-free dashboard."""

from __future__ import annotations

import html
import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any

from .aggregate import aggregate, drilldown_records, local_zone
from .billing import billing_summary
from .config import atomic_write
from .database import connect_history, secure_history_files
from .history import active_events, latest_run
from .pricing import estimate_costs
from .platform_support import secure_chmod

MODEL_COLORS = ("#008a00", "#7bc87a", "#ba8e6b", "#ec7a2e", "#8a9499", "#33a133")


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _decimal(value: Decimal, places: int = 9) -> str:
    text = f"{value:.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _credits(value: Any) -> str:
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    return f"{number:,.0f}"


def _number(value: int) -> str:
    return f"{value:,}"


def _compact(value: int) -> str:
    absolute = abs(value)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if absolute >= divisor:
            compact = Decimal(value) / Decimal(divisor)
            text = f"{compact:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return str(value)


def _money(value: Decimal | None, currency: str) -> str:
    return "—" if value is None else f"{currency} {_decimal(value, 4)}"


def _money_total(value: Decimal | None, currency: str) -> str:
    return "—" if value is None else f"{currency} {value:,.0f}"


def _friendly_day(value: str) -> str:
    try:
        instant = datetime.fromisoformat(value)
        return f"{instant.day} {instant.strftime('%b')}"
    except ValueError:
        return value


def _display_datetime(value: Any, timezone_name: str | None = None) -> str:
    if value in (None, ""):
        return "—"
    source = str(value)
    try:
        instant = datetime.fromisoformat(source.replace("Z", "+00:00"))
    except ValueError:
        return source
    includes_time = "T" in source or " " in source
    if includes_time and timezone_name and instant.tzinfo is not None:
        instant = instant.astimezone(local_zone(timezone_name))
    date_text = f"{instant.day} {instant.strftime('%b')} {instant.year}"
    return f"{date_text}, {instant.strftime('%H:%M')}" if includes_time else date_text


def _calendar_window(daily: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    """Return a fixed calendar window ending on the latest retained usage day."""
    if not daily:
        return []
    try:
        end = date.fromisoformat(str(daily[-1]["label"]))
    except (KeyError, TypeError, ValueError):
        return daily[-days:]
    by_day = {str(row["label"]): row for row in daily}
    start = end - timedelta(days=days - 1)
    return [
        {
            "label": (day := start + timedelta(days=offset)).isoformat(),
            "credits": by_day.get(day.isoformat(), {}).get("credits", Decimal(0)),
        }
        for offset in range(days)
    ]


def _table_panel(
    tab_id: str,
    title: str,
    rows: list[dict[str, Any]],
    *,
    prices: dict[str, Decimal | None] | None = None,
    active: bool = False,
) -> str:
    max_credits = max((abs(row["credits"]) for row in rows), default=Decimal(0))
    body = []
    for row in reversed(rows):
        display_label = row["label"]
        price = ""
        if prices is not None:
            price = f'<td class="numeric">{_escape(_money(prices.get(row["label"]), "USD"))}</td>'
        share = "—" if row["cache_share"] is None else f"{row['cache_share'] * 100:.1f}%"
        width = Decimal(0) if not max_credits else abs(row["credits"]) / max_credits * 100
        body.append(
            '<tr class="expandable-row"><th scope="row"><span class="row-heading">'
            f'<button type="button" class="expand-row" aria-expanded="false" '
            f'aria-label="Show details for {_escape(display_label)}" '
            f'data-expand-group="{_escape(tab_id)}" data-expand-label="{_escape(row["label"])}">'
            '<span aria-hidden="true">›</span></button><span>' + _escape(display_label) + '</span></span></th>'
            '<td class="credits-cell numeric"><strong>' + _escape(_credits(row["credits"])) + "</strong>"
            f'<span class="credit-bar" aria-hidden="true"><span style="width:{float(width):.2f}%"></span></span></td>'
            f'<td class="numeric">{_number(row["calls"])}</td>'
            f'<td class="numeric">{_number(row["input"])}</td>'
            f'<td class="numeric">{_number(row["output"])}</td>'
            f'<td class="numeric">{_number(row["cache_read"])}</td>'
            f'<td class="numeric">{_escape(share)}</td>{price}</tr>'
        )
    column_count = 8 if prices is not None else 7
    price_head = '<th scope="col" class="numeric">Estimated cost</th>' if prices is not None else ""
    hidden = "" if active else " hidden"
    return (
        f'<div class="tab-panel" id="panel-{tab_id}" role="tabpanel" '
        f'aria-labelledby="tab-{tab_id}" tabindex="0" data-breakdown-group="{tab_id}" '
        f'data-has-prices="{str(prices is not None).lower()}"{hidden}>'
        '<div class="table-wrap"><table>'
        f'<caption>{_escape(title)} usage breakdown</caption><thead><tr>'
        '<th scope="col">Period or group</th><th scope="col" class="numeric">Credits</th>'
        '<th scope="col" class="numeric">Tool calls</th><th scope="col" class="numeric">Input</th>'
        '<th scope="col" class="numeric">Output</th><th scope="col" class="numeric">Cache read</th>'
        f'<th scope="col" class="numeric">Cache share</th>{price_head}</tr></thead><tbody>'
        + ("".join(body) or f'<tr><td colspan="{column_count}" class="empty">No usage events.</td></tr>')
        + "</tbody></table></div></div>"
    )


def _breakdown(data: dict[str, Any], model_prices: dict[str, Decimal | None]) -> str:
    definitions = [
        ("day", "By day", "By day", data["groups"]["day"], None),
        ("week", "By ISO week", "By ISO week", data["groups"]["week"], None),
        ("month", "By month", "By month", data["groups"]["month"], None),
        ("model", "By model", "By model", data["groups"]["model"], model_prices),
    ]
    tabs = []
    panels = []
    for index, (tab_id, label, title, rows, prices) in enumerate(definitions):
        selected = index == 0
        tabs.append(
            f'<button type="button" id="tab-{tab_id}" class="tab" role="tab" '
            f'aria-selected="{str(selected).lower()}" aria-controls="panel-{tab_id}" '
            f'tabindex="{0 if selected else -1}">{_escape(label)}</button>'
        )
        panels.append(_table_panel(tab_id, title, rows, prices=prices, active=selected))
    return (
        '<section class="card breakdown-card"><div class="tab-list" role="tablist" '
        'aria-label="Usage breakdown">' + "".join(tabs) + '</div><div class="tab-panels">'
        + "".join(panels) + "</div></section>"
    )


def _trend(daily: list[dict[str, Any]]) -> tuple[str, str, str]:
    window = _calendar_window(daily, 60)
    current = sum((row["credits"] for row in window[-30:]), Decimal(0))
    previous = sum((row["credits"] for row in window[-60:-30]), Decimal(0))
    if not previous:
        return "— vs previous 30 days", "delta-neutral", ""
    delta = (current - previous) / abs(previous) * 100
    if delta > 0:
        return f"▲ +{delta:.0f}% vs previous 30 days", "delta-up", "up"
    if delta < 0:
        return f"▼ {delta:.0f}% vs previous 30 days", "delta-down", "down"
    return "— 0% vs previous 30 days", "delta-neutral", ""


def _sparkline(daily: list[dict[str, Any]]) -> str:
    series = _calendar_window(daily, 30)
    if not series:
        return '<div class="spark-empty">No 30-day trend yet</div>'
    values = [float(row["credits"]) for row in series]
    low, high = min(values), max(values)
    points = []
    for index, value in enumerate(values):
        x = 110.0 if len(values) == 1 else index * 220.0 / (len(values) - 1)
        y = 26.0 if math.isclose(high, low) else 48.0 - (value - low) / (high - low) * 44.0
        points.append((round(x, 2), round(y, 2)))
    payload = {
        "labels": [_friendly_day(row["label"]) for row in series],
        "values": [_credits(row["credits"]) for row in series],
        "points": points,
    }
    polyline = " ".join(f"{x},{y}" for x, y in points)
    return (
        '<div class="spark-wrap" data-spark data-series="' + _escape(json.dumps(payload, separators=(",", ":"))) + '">'
        '<div class="chart-tooltip spark-tooltip" role="status" aria-live="polite" hidden></div>'
        '<svg class="sparkline" viewBox="0 0 220 52" role="img" aria-label="30-day credit trend">'
        f'<polyline points="{polyline}"></polyline><circle class="spark-dot" r="4" hidden></circle>'
        '<rect class="spark-hit" width="220" height="52"></rect></svg>'
        '<div class="chart-caption">30-day trend</div></div>'
    )


def _daily_chart(daily: list[dict[str, Any]]) -> str:
    series = _calendar_window(daily, 60)
    detect_spikes = len(daily) >= 15
    if not series:
        bars = '<p class="empty">No daily usage events.</p>'
        start = end = "—"
    else:
        positive = [max(0.0, float(row["credits"])) for row in series]
        maximum = max(positive) or 1.0
        mean = fmean(positive)
        deviation = pstdev(positive) if len(positive) > 1 else 0.0
        threshold = mean + 1.6 * deviation
        rendered = []
        for index, (row, value) in enumerate(zip(series, positive)):
            spike = detect_spikes and deviation > 0 and value > threshold
            latest = index == len(series) - 1
            height = 0.0 if value == 0 else max(3.0, value / maximum * 100.0)
            classes = "bar-fill" + (" empty" if value == 0 else "") + (" spike" if spike else "") + (" latest" if latest else "")
            suffix = " · unusually high" if spike else ""
            tip = f'{_friendly_day(row["label"])} · {_credits(row["credits"])} credits{suffix}'
            rendered.append(
                f'<button type="button" class="bar-hit" data-tip="{_escape(tip)}" '
                f'aria-label="{_escape(tip)}"><span class="{classes}" style="height:{height:.2f}%"></span></button>'
            )
        bars = "".join(rendered)
        start, end = _friendly_day(series[0]["label"]), _friendly_day(series[-1]["label"])
    spike_legend = '<span class="spike-key"><i></i>Unusually high day</span>' if detect_spikes else ""
    return (
        '<section class="card daily-card"><div class="card-heading"><h2>Daily credits</h2>'
        f'<div class="chart-meta">{spike_legend}<span>Last 60 days</span></div></div>'
        '<div class="bar-area" data-bar-chart><div class="chart-tooltip bar-tooltip" role="status" aria-live="polite" hidden></div>'
        f'<div class="bar-chart">{bars}</div></div><div class="axis"><span>{_escape(start)}</span><span>{_escape(end)}</span></div></section>'
    )


def _model_share(models: list[dict[str, Any]]) -> str:
    positive = [max(Decimal(0), row["credits"]) for row in models]
    total = sum(positive, Decimal(0))
    stops = []
    legend = []
    cursor = Decimal(0)
    for index, (row, credits) in enumerate(zip(models, positive)):
        share = Decimal(0) if not total else credits / total * 100
        color = MODEL_COLORS[index % len(MODEL_COLORS)]
        start, cursor = cursor, cursor + share
        stops.append(f"{color} {float(start):.2f}% {float(cursor):.2f}%")
        label = row["label"]
        center = f"{label} {share:.0f}%"
        legend.append(
            f'<button type="button" class="model-row" data-model="{_escape(label)}" '
            f'data-color="{color}" data-center="{_escape(center)}" aria-pressed="true" '
            f'aria-label="Include {_escape(label)} in dashboard totals">'
            f'<i style="--model-color:{color}" aria-hidden="true"></i><span class="model-name">{_escape(label)}</span>'
            f'<span class="model-share">{share:.0f}%</span>'
            f'<span class="model-credits">{_escape(_credits(row["credits"]))}</span></button>'
        )
    gradient = "conic-gradient(" + ",".join(stops) + ")" if stops and total else "var(--d-border)"
    return (
        '<section class="card model-card" data-model-card><div class="model-heading"><div><h2>Model share</h2>'
        '<p>Select models to include across usage totals and charts.</p></div>'
        '<button type="button" class="show-all-models is-concealed" data-show-all '
        'aria-hidden="true" tabindex="-1">Show all</button></div>'
        '<div class="model-layout"><div class="donut-wrap">'
        f'<div class="donut" data-donut style="--donut:{gradient}" role="img" tabindex="0" '
        'aria-label="Credit share by model. Move the pointer around the ring for model details.">'
        '<div class="donut-center" aria-live="polite">credits</div></div>'
        '<div class="chart-tooltip donut-tooltip" role="status" aria-live="polite" hidden></div></div>'
        '<div class="model-legend"><div class="model-legend-head" aria-hidden="true">'
        '<span></span><span>Model</span><span>Share (%)</span><span>Credits</span></div>'
        + ("".join(legend) or '<p class="empty">No model usage.</p>')
        + '<p class="model-filter-status" data-model-filter-status aria-live="polite"></p></div></div></section>'
    )


def _filter_metrics(item: dict[str, Any]) -> dict[str, Any]:
    result = {
        "nano": str(item["nano"]),
        "calls": item["calls"],
        "input": item["input"],
        "output": item["output"],
        "cache_read": item["cache_read"],
    }
    if "label" in item:
        result["label"] = item["label"]
    return result


def _filter_payload(
    rows: list[Any],
    data: dict[str, Any],
    config: dict[str, Any],
    model_prices: dict[str, Decimal | None],
    billing: dict[str, Any],
) -> str:
    include_sessions = bool(config["privacy"]["include_sessions"])
    model_rows = data["groups"]["model"]
    models = []
    for index, model_row in enumerate(model_rows):
        label = model_row["label"]
        model_data = aggregate(
            (row for row in rows if row["model"] == label),
            config["timezone"],
            include_sessions,
        )
        groups = {
            name: [_filter_metrics(item) for item in model_data["groups"][name]]
            for name in ("day", "week", "month", "session")
            if name in model_data["groups"]
        }
        models.append({
            "id": label,
            "color": MODEL_COLORS[index % len(MODEL_COLORS)],
            "total": _filter_metrics(model_data["total"]),
            "groups": groups,
            "estimated_usd": None if model_prices.get(label) is None else str(model_prices[label]),
        })
    secondary = config.get("secondary_currency")
    if secondary is None and config.get("usd_to_nok") is not None:
        secondary = {"code": "NOK", "usd_rate": config["usd_to_nok"]}
    money_mode = "none"
    if models and all(model["estimated_usd"] is not None for model in models):
        money_mode = "model"
    elif billing["estimated_gross_scout_usd"] is not None:
        money_mode = "credit"
    payload = {
        "models": models,
        "records": [
            {
                **{name: item[name] for name in ("model", "day", "week", "month")},
                **({"chat": item["chat"]} if "chat" in item else {}),
                **_filter_metrics(item),
            }
            for item in drilldown_records(rows, config["timezone"], include_sessions)
        ],
        "window_end": data["groups"]["day"][-1]["label"] if data["groups"]["day"] else None,
        "money": {
            "mode": money_mode,
            "usd_per_credit": "0.01" if money_mode == "credit" else None,
            "secondary_currency": None if secondary is None else {
                "code": secondary["code"],
                "usd_rate": str(secondary["usd_rate"]),
            },
        },
    }
    return _escape(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def _billing_card(
    config: dict[str, Any],
    scout_credits: Decimal,
    generated_at: datetime,
    summary: dict[str, Any] | None = None,
) -> str:
    summary = summary or billing_summary(config, scout_credits, now=generated_at)
    snapshot = summary["snapshot"]
    legacy = config.get("account_comparison") or {}
    if summary["plan"] == "unknown" and not snapshot and not legacy:
        return ""
    included = summary["included_credits"]
    if summary["pooled"]:
        allowance = "—" if included is None else f"{_credits(included)} per seat, pooled"
        if summary["effective_allowance"] is not None:
            allowance += f" · {_credits(summary['effective_allowance'])} configured pool"
    else:
        allowance = "—" if included is None else f"{_credits(included)} monthly credits"
    price = _money(summary["monthly_price_usd"], "USD")
    if summary["monthly_price_usd"] is not None:
        price += " per user/seat per month" if summary["pooled"] else " / month"
    gross_row = f"<dt>Estimated gross Scout value</dt><dd>{_escape(_money_total(summary['estimated_gross_scout_usd'], 'USD'))}"
    if summary["estimated_gross_scout_secondary"] is not None:
        gross_row += f" · {_escape(_money_total(summary['estimated_gross_scout_secondary'], summary['secondary_currency_code']))}"
    gross_row += " · estimate, not an invoice</dd>"
    if not summary["enabled"]:
        rows = []
        note = "Plan estimates are off. Exact Scout credits remain available above."
    elif summary["plan"] == "unknown":
        rows = ["<dt>Plan</dt><dd>Not selected</dd>", gross_row]
        note = "Select a plan only to add included-credit and overage context; the gross value above does not depend on a plan."
    else:
        rows = [
            f"<dt>Plan</dt><dd>{_escape(summary['label'])}</dd>",
            f"<dt>Plan source</dt><dd>{_escape(summary['allowance_source'])}</dd>",
            f"<dt>Included allowance</dt><dd>{_escape(allowance)}</dd>",
            f"<dt>Subscription price context</dt><dd>{_escape(price)} · {_escape(summary['price_source'])} · context, not an invoice</dd>",
            gross_row,
        ]
        note = "Plan context is an estimate, not an invoice."
    if snapshot:
        note = "The billing snapshot matches the configured billing scope and month."
        source = "GitHub-reported aggregate usage" if snapshot["source"] == "github" else "Manual aggregate snapshot"
        scope_labels = {
            "user": "Personal user billing scope",
            "organization": "Organization billing pool",
            "enterprise": "Enterprise billing pool",
        }
        freshness = "Current billing month" if summary["period_matches"] else "Different billing month; no overage estimate"
        if not summary["scope_matches"]:
            freshness += " · scope does not match configured plan"
            note = "The snapshot scope does not match the configured plan, so no additional-usage estimate is shown."
        elif not summary["period_matches"]:
            note = "The snapshot covers a different billing month, so no additional-usage estimate is shown."
        rows.extend((
            f"<dt>Billing source</dt><dd>{_escape(source)}</dd>",
            f"<dt>Scope</dt><dd>{_escape(scope_labels[snapshot['scope']])}</dd>",
            f"<dt>Period</dt><dd>{snapshot['year']:04d}-{snapshot['month']:02d}</dd>",
            f"<dt>Captured</dt><dd>{_escape(_display_datetime(snapshot['captured_at'], config['timezone']))}</dd>",
            f"<dt>Freshness</dt><dd>{_escape(freshness)}</dd>",
            f"<dt>Gross entity AI credits</dt><dd>{_escape(_credits(snapshot['gross_ai_credits']))}</dd>",
            f"<dt>Discount credits</dt><dd>{_escape(_credits(snapshot['discount_credits']))}</dd>",
            f"<dt>Discount amount</dt><dd>{_escape(_money(snapshot['discount_amount_usd'], 'USD'))}</dd>",
        ))
        if snapshot.get("plan_type"):
            rows.append(f"<dt>GitHub-detected organization plan</dt><dd>{_escape(snapshot['plan_type'])} · best-effort context only</dd>")
        net_label = "GitHub-reported usage amount; not a final invoice" if snapshot["source"] == "github" else "Manual reported amount; not a final invoice"
        rows.append(f"<dt>Net usage amount</dt><dd>{_escape(_money(snapshot['net_amount_usd'], 'USD'))} · {_escape(net_label)}</dd>")
        if summary["estimated_additional_credits"] is not None:
            extra = f"{_credits(summary['estimated_additional_credits'])} credits · {_money(summary['estimated_additional_usd'], 'USD')}"
            if summary["estimated_additional_secondary"] is not None:
                extra += f" · {_money(summary['estimated_additional_secondary'], summary['secondary_currency_code'])}"
            rows.append(f"<dt>Estimated additional usage</dt><dd>{_escape(extra)} · estimate, not an invoice</dd>")
        elif summary["pooled"]:
            note = "Business and Enterprise allowances are billing-entity pools; this tracker never allocates a pool share to one user."
        else:
            note = "Additional usage needs a matching current-month personal billing snapshot; Scout-only usage is not account-wide usage."
    elif summary["pooled"]:
        note = "Business and Enterprise allowances are pooled. Without an aggregate organization or enterprise snapshot, no user overage is estimated."
    elif summary["plan"] == "free":
        note = "No Free allowance is assumed. Add an explicit custom allowance only when it is known to apply."

    legacy_html = ""
    if legacy:
        legacy_html = (
            '<div class="warning-list"><h3>Legacy manual comparison</h3><dl>'
            f'<dt>Account-wide Copilot total (manual)</dt><dd>{_escape(_credits(legacy["total"])) if legacy.get("total") is not None else "—"}</dd>'
            f'<dt>Account-wide additional usage in USD (manual)</dt><dd>{_escape(legacy.get("additional_usage_usd", "—"))}</dd>'
            f'<dt>As of</dt><dd>{_escape(_display_datetime(legacy.get("as_of"), config["timezone"]))}</dd>'
            f'<dt>Scope</dt><dd>{_escape(legacy.get("scope", "manual"))}</dd></dl>'
            '<p>Compatibility display only; these manual values are never Scout-only measurements.</p></div>'
        )
    return (
        '<section class="quiet-card billing-card"><h2>Plan &amp; billing estimates</h2><dl>'
        + "".join(rows) + f"</dl><p>{_escape(note)}</p>{legacy_html}</section>"
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
    estimates = estimate_costs(
        credits_by_model,
        config.get("usd_per_credit_by_model", {}),
        config.get("secondary_currency", config.get("usd_to_nok")),
        default_rate=config.get("usd_per_credit", "0.01"),
    )
    model_prices = estimates["per_model_usd"]
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
    status_class = "pass" if overall_verification == "PASS" else ("failed" if overall_verification == "MISMATCH" else "warning")
    status_chip = "Verification pass" if status_class == "pass" else ("Verification failed" if status_class == "failed" else "Verification warning")
    warnings = [] if run is None else json.loads(run["warnings_json"])
    if run is not None and run["possible_id_gap"]:
        warnings.append("Possible source history gap detected from non-contiguous source row identifiers.")
    warning_html = "".join(f"<li>{_escape(item)}</li>" for item in warnings)
    impact_html = ""
    if overall_verification != "PASS":
        impact = (
            "Stored and recalculated AIU differ for one or more events. Exact Scout credits still use stored total_nano_aiu."
            if overall_verification == "MISMATCH"
            else "Some events could not be verified or a possible history gap was detected. Totals reflect the locally retained Scout ledger."
        )
        impact_html = f'<div class="verification-notice">{_escape(impact)}</div>'
    integrity_label = "Verified" if overall_verification == "PASS" else "Review recommended"
    warnings_block = f'<div class="warning-list"><h3>Details</h3><ul>{warning_html}</ul></div>' if warning_html else ""
    verification_html = (
        '<section class="quiet-card verification-card"><h2>Usage integrity</h2><dl>'
        f'<dt>AIU check</dt><dd class="verification-status {status_class}"><strong>{integrity_label}</strong></dd>'
        f'<dt>Events checked</dt><dd>{_number(verification_counts.get("verified", 0))} of {_number(len(rows))}</dd>'
        f'<dt>First event</dt><dd>{_escape(_display_datetime(data["first_event"], config["timezone"]))}</dd>'
        f'<dt>Last event</dt><dd>{_escape(_display_datetime(data["last_event"], config["timezone"]))}</dd></dl>{impact_html}'
        f'{warnings_block}</section>'
    )

    generated_source = config.get("_generated_at", datetime.now(timezone.utc).isoformat())
    try:
        generated_at = datetime.fromisoformat(str(generated_source).replace("Z", "+00:00"))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=timezone.utc)
    except ValueError:
        generated_at = datetime.now(timezone.utc)
    billing = billing_summary(config, total["credits"], now=generated_at)

    money_kpi = ""
    if estimates["total_usd"] is not None:
        estimate_note = "estimate, not a bill"
        if estimates["total_secondary"] is not None:
            estimate_note = f'{_escape(_money_total(estimates["total_secondary"], estimates["secondary_currency_code"]))} · {estimate_note}'
        money_kpi = (
            '<div class="hero-kpi" data-money-kpi><span>Estimated cost</span>'
            f'<strong data-money-total>{_escape(_money_total(estimates["total_usd"], "USD"))}</strong>'
            f'<small data-money-note>{estimate_note}</small></div>'
        )
    elif billing["estimated_gross_scout_usd"] is not None:
        estimate_note = "AI-credit estimate, not a bill"
        if billing["estimated_gross_scout_secondary"] is not None:
            estimate_note = f'{_escape(_money_total(billing["estimated_gross_scout_secondary"], billing["secondary_currency_code"]))} · {estimate_note}'
        money_kpi = (
            '<div class="hero-kpi" data-money-kpi><span>Estimated gross Scout value</span>'
            f'<strong data-money-total>{_escape(_money_total(billing["estimated_gross_scout_usd"], "USD"))}</strong>'
            f'<small data-money-note>{estimate_note}</small></div>'
        )
    delta_text, delta_class, _ = _trend(data["groups"]["day"])
    values = {
        "TITLE": "Scout usage",
        "FILTER_DATA": _filter_payload(rows, data, config, model_prices, billing),
        "UPDATE_TIME": _escape(_display_datetime(
            generated_source, config["timezone"]
        )),
        "TIMEZONE": _escape(data["timezone"]),
        "VERIFICATION_CHIP": status_chip,
        "VERIFICATION_CLASS": status_class,
        "TOTAL_CREDITS": _escape(_credits(total["credits"])),
        "DELTA_TEXT": _escape(delta_text),
        "DELTA_CLASS": delta_class,
        "SPARKLINE": _sparkline(data["groups"]["day"]),
        "CALLS": _compact(total["calls"]),
        "TOKENS": f'{_compact(total["input"])} / {_compact(total["output"])}',
        "CACHE_SHARE": _escape(cache_share),
        "MONEY_KPI": money_kpi,
        "DAILY_CHART": _daily_chart(data["groups"]["day"]),
        "MODEL_SHARE": _model_share(data["groups"]["model"]),
        "BREAKDOWN": _breakdown(data, model_prices),
        "VERIFICATION_CARD": verification_html,
        "BILLING_CARD": _billing_card(config, total["credits"], generated_at, billing),
    }
    if template_path is None:
        template_path = Path(__file__).resolve().parents[2] / "templates" / "dashboard.html"
    document = Path(template_path).read_text(encoding="utf-8")
    for key, value in values.items():
        document = document.replace("{{" + key + "}}", value)
    target = Path(config["dashboard_path"])
    atomic_write(target, document, 0o600)
    secure_chmod(target, 0o600)
    return target
