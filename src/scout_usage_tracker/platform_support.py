"""Small cross-platform helpers for private files, timezones, and SQLite."""

from __future__ import annotations

import os
import time as _time
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TimezoneDataError(ValueError):
    """An explicitly configured IANA timezone is unavailable locally."""


_ZERO = timedelta(0)
_SECOND = timedelta(seconds=1)
_EPOCH = datetime(1970, 1, 1)
_STANDARD_OFFSET = timedelta(seconds=-_time.timezone)
class _SystemLocalTimezone(tzinfo):
    """Use the operating system's timezone rules for each converted instant."""

    @staticmethod
    def _candidates(value: datetime) -> list[tuple[float, timedelta, bool]]:
        """Return valid UTC instants for a local wall time, earliest first."""
        wall = value.replace(tzinfo=None)
        wall_seconds = wall.replace(microsecond=0)
        fields = (
            wall.year, wall.month, wall.day, wall.hour, wall.minute,
            wall.second, wall.weekday(), 0,
        )
        candidates: dict[float, tuple[float, timedelta, bool]] = {}
        for dst_hint in (0, 1):
            try:
                stamp = _time.mktime(fields + (dst_hint,))
                local = _time.localtime(stamp)
            except (OSError, OverflowError, ValueError):
                continue
            if local[:6] != fields[:6]:
                continue
            utc_wall = datetime.fromtimestamp(stamp, timezone.utc).replace(tzinfo=None)
            candidates[stamp] = (stamp, wall_seconds - utc_wall, local.tm_isdst > 0)
        if candidates:
            return sorted(candidates.values(), key=lambda item: item[0])

        # A skipped wall time has no exact candidate. Retain the operating
        # system's normalization choice; aggregation boundaries are at
        # midnight and therefore do not normally use this fallback.
        stamp = _time.mktime(fields + (-1,))
        local = _time.localtime(stamp)
        offset = timedelta(seconds=-(_time.altzone if local.tm_isdst > 0 else _time.timezone))
        return [(stamp, offset, local.tm_isdst > 0)]

    def _selected(self, value: datetime) -> tuple[float, timedelta, bool]:
        candidates = self._candidates(value)
        return candidates[min(value.fold, len(candidates) - 1)]

    def fromutc(self, value: datetime) -> datetime:
        if value.tzinfo is not self:
            raise ValueError("fromutc requires the system-local timezone")
        stamp = (value.replace(tzinfo=None) - _EPOCH) // _SECOND
        local = _time.localtime(stamp)
        local_tuple = local[:6]
        wall = datetime(*local_tuple, microsecond=value.microsecond)
        candidates = self._candidates(wall)
        matching = [index for index, candidate in enumerate(candidates) if int(candidate[0]) == stamp]
        fold = 1 if matching and matching[0] > 0 else 0
        return datetime(*local_tuple, microsecond=value.microsecond, tzinfo=self, fold=fold)

    def utcoffset(self, value: datetime | None) -> timedelta:
        return _STANDARD_OFFSET if value is None else self._selected(value)[1]

    def dst(self, value: datetime | None) -> timedelta:
        if value is None:
            return _ZERO
        _, offset, is_dst = self._selected(value)
        return offset - _STANDARD_OFFSET if is_dst else _ZERO

    def tzname(self, value: datetime | None) -> str:
        is_dst = False if value is None else self._selected(value)[2]
        return _time.tzname[1 if is_dst else 0]


SYSTEM_LOCAL_TIMEZONE = _SystemLocalTimezone()


def timezone_for(name: str) -> tzinfo:
    """Return a dynamic local zone or an explicitly requested IANA zone."""
    if name == "local":
        if os.name != "nt":
            configured = os.environ.get("TZ")
            if configured:
                try:
                    return ZoneInfo(configured)
                except ZoneInfoNotFoundError:
                    pass
            try:
                resolved = str(Path("/etc/localtime").resolve())
                if "/zoneinfo/" in resolved:
                    return ZoneInfo(resolved.split("/zoneinfo/", 1)[1])
            except (OSError, ZoneInfoNotFoundError):
                pass
        return SYSTEM_LOCAL_TIMEZONE
    if name.upper() in {"UTC", "ETC/UTC", "ETC/GMT", "GMT"}:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise TimezoneDataError(
            f"IANA timezone data is unavailable for {name!r}; install timezone data or use local"
        ) from exc


def sqlite_readonly_uri(path: str | Path) -> str:
    """Build a percent-encoded absolute file URI accepted by SQLite."""
    return Path(path).expanduser().resolve().as_uri() + "?mode=ro"


def secure_chmod(path: str | Path, mode: int) -> None:
    """Apply Unix privacy modes where they are meaningful.

    Windows ACLs are not represented by POSIX mode bits. Windows installations
    instead validate that every managed path is inside the current user profile.
    """
    if os.name != "nt":
        os.chmod(path, mode)
