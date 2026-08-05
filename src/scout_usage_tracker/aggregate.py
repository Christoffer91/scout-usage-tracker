"""Timezone-correct aggregation over active history rows."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
import os
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .pricing import credits_from_nano
from .privacy import session_label


def local_zone(name: str):
    if name == "local":
        configured = os.environ.get("TZ")
        if configured:
            try:
                return ZoneInfo(configured)
            except Exception:
                pass
        localtime = Path("/etc/localtime")
        try:
            marker = "/zoneinfo/"
            resolved = str(localtime.resolve())
            if marker in resolved:
                return ZoneInfo(resolved.split(marker, 1)[1])
        except OSError:
            pass
        return datetime.now().astimezone().tzinfo
    return ZoneInfo(name)


def _blank() -> dict[str, int]:
    return {"nano": 0, "calls": 0, "input": 0, "output": 0, "cache_read": 0,
            "cache_write": 0, "reasoning": 0}


def _add(bucket: dict[str, int], row: Any) -> None:
    bucket["nano"] += row["total_nano_aiu"]
    bucket["calls"] += 1
    for name in ("input", "output", "cache_read", "cache_write", "reasoning"):
        bucket[name] += row[name + "_tokens"]


def _finish(groups: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    result = []
    for label, values in sorted(groups.items()):
        item: dict[str, Any] = {"label": label, **values, "credits": credits_from_nano(values["nano"])}
        denominator = values["input"] + values["cache_read"]
        item["cache_share"] = (Decimal(values["cache_read"]) / Decimal(denominator)) if denominator else None
        result.append(item)
    return result


def aggregate(rows: Iterable[Any], timezone_name: str, include_sessions: bool = False) -> dict[str, Any]:
    zone = local_zone(timezone_name)
    groups = {name: defaultdict(_blank) for name in ("day", "week", "month", "model", "session")}
    totals = _blank()
    verification: dict[str, int] = defaultdict(int)
    first = last = None
    for row in rows:
        instant = datetime.fromisoformat(row["event_time_utc"]).astimezone(zone)
        iso = instant.isocalendar()
        labels = {
            "day": instant.date().isoformat(),
            "week": f"{iso.year}-W{iso.week:02d}",
            "month": f"{instant.year:04d}-{instant.month:02d}",
            "model": row["model"],
        }
        if include_sessions:
            labels["session"] = session_label(row["session_digest"])
        for name, label in labels.items():
            _add(groups[name][label], row)
        _add(totals, row)
        verification[row["verification_status"]] += 1
        first = instant if first is None or instant < first else first
        last = instant if last is None or instant > last else last
    finished = {name: _finish(group) for name, group in groups.items() if name != "session" or include_sessions}
    total_result: dict[str, Any] = {**totals, "credits": credits_from_nano(totals["nano"])}
    denominator = totals["input"] + totals["cache_read"]
    total_result["cache_share"] = Decimal(totals["cache_read"]) / Decimal(denominator) if denominator else None
    return {
        "total": total_result,
        "groups": finished,
        "verification": dict(sorted(verification.items())),
        "first_event": first.isoformat() if first else None,
        "last_event": last.isoformat() if last else None,
        "timezone": timezone_name,
    }
