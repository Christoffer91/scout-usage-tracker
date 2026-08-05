"""Configuration loading, migration, and permission-safe writes."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1
LEGACY_KEYS = {
    "sourceDatabase": "source_database",
    "historyDatabase": "history_database",
    "dashboardPath": "dashboard_path",
    "timezoneName": "timezone",
    "usdPerCreditByModel": "usd_per_credit_by_model",
    "estimatedUsdPerCredit": "usd_per_credit_by_model",
    "usdToNok": "usd_to_nok",
    "accountComparison": "account_comparison",
}


class ConfigError(ValueError):
    pass


def _expand(value: str, base: Path) -> str:
    expanded = Path(os.path.expanduser(value))
    return str(expanded if expanded.is_absolute() else base / expanded)


def _migrate(raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    data = dict(raw)
    changed = False
    for old, new in LEGACY_KEYS.items():
        if old in data:
            if new not in data:
                data[new] = data[old]
            data.pop(old)
            changed = True
    if "includeSessions" in data:
        privacy = dict(data.get("privacy") or {})
        privacy.setdefault("include_sessions", data.pop("includeSessions"))
        data["privacy"] = privacy
        changed = True
    snapshot = data.pop("accountWideSnapshot", None)
    if snapshot is not None:
        if not isinstance(snapshot, dict):
            raise ConfigError("accountWideSnapshot must be an object")
        comparison = dict(data.get("account_comparison") or {})
        comparison.setdefault("total", snapshot.get("credits"))
        comparison.setdefault("as_of", snapshot.get("capturedAt"))
        comparison.setdefault("scope", snapshot.get("scope"))
        data["account_comparison"] = {key: value for key, value in comparison.items() if value is not None}
        changed = True
    if "additionalUsageUsd" in data:
        comparison = dict(data.get("account_comparison") or {})
        comparison.setdefault("additional_usage_usd", data.pop("additionalUsageUsd"))
        data["account_comparison"] = comparison
        changed = True
    for presentation_key in ("pricingSource", "currency"):
        if presentation_key in data:
            data.pop(presentation_key)
            changed = True
    version = data.get("schema_version", 0)
    if version not in (0, SCHEMA_VERSION):
        raise ConfigError(f"unsupported config schema_version: {version}")
    if version != SCHEMA_VERSION:
        data["schema_version"] = SCHEMA_VERSION
        changed = True
    return data, changed


def validate_config(data: dict[str, Any], config_path: Path) -> dict[str, Any]:
    required = ("source_database", "history_database", "dashboard_path")
    missing = [key for key in required if not isinstance(data.get(key), str) or not data[key]]
    if missing:
        raise ConfigError("missing required config keys: " + ", ".join(missing))
    result = dict(data)
    base = config_path.parent
    for key in required:
        result[key] = _expand(result[key], base)
    canonical_paths = {
        "config": config_path.expanduser().resolve(strict=False),
        **{key: Path(result[key]).expanduser().resolve(strict=False) for key in required},
    }
    by_path: dict[Path, list[str]] = {}
    for label, canonical in canonical_paths.items():
        by_path.setdefault(canonical, []).append(label)
    collisions = [labels for labels in by_path.values() if len(labels) > 1]
    if collisions:
        details = "; ".join(" = ".join(labels) for labels in collisions)
        raise ConfigError(f"config, source, history, and dashboard paths must not alias: {details}")
    timezone = result.get("timezone", "local")
    if timezone != "local":
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"unknown IANA timezone: {timezone}") from exc
    result["timezone"] = timezone
    privacy = result.get("privacy", {})
    if not isinstance(privacy, dict) or not isinstance(privacy.get("include_sessions", False), bool):
        raise ConfigError("privacy.include_sessions must be a boolean")
    result["privacy"] = {"include_sessions": privacy.get("include_sessions", False)}
    rates = result.get("usd_per_credit_by_model", {})
    if not isinstance(rates, dict):
        raise ConfigError("usd_per_credit_by_model must be an object")
    result["usd_per_credit_by_model"] = rates
    if result.get("usd_to_nok") is not None:
        try:
            if float(result["usd_to_nok"]) < 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ConfigError("usd_to_nok must be a nonnegative number") from exc
    comparison = result.get("account_comparison")
    if comparison is not None and not isinstance(comparison, dict):
        raise ConfigError("account_comparison must be an object")
    if comparison is not None:
        allowed = {"total", "additional_usage_usd", "as_of", "scope"}
        unknown = set(comparison) - allowed
        if unknown:
            raise ConfigError("unknown account_comparison keys: " + ", ".join(sorted(unknown)))
        for key in ("total", "additional_usage_usd"):
            if comparison.get(key) is not None:
                try:
                    number = Decimal(str(comparison[key]))
                except (InvalidOperation, ValueError) as exc:
                    raise ConfigError(f"account_comparison.{key} must be a nonnegative finite number") from exc
                if not number.is_finite() or number < 0:
                    raise ConfigError(f"account_comparison.{key} must be a nonnegative finite number")
        for key in ("as_of", "scope"):
            if comparison.get(key) is not None and not isinstance(comparison[key], str):
                raise ConfigError(f"account_comparison.{key} must be text")
        if (comparison.get("total") is not None or comparison.get("additional_usage_usd") is not None) and not comparison.get("scope"):
            raise ConfigError("account_comparison.scope is required for manual account-wide values")
        result["account_comparison"] = comparison
    return result


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(path.parent, 0o700)
    old_mode = ((path.stat().st_mode & 0o777) & mode) if path.exists() else mode
    old_mode = old_mode or mode
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, old_mode)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_config(path: str | Path, write_migration: bool = True) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be an object")
    migrated, changed = _migrate(raw)
    validated = validate_config(migrated, config_path)
    if changed and write_migration:
        atomic_write(config_path, json.dumps(migrated, indent=2, sort_keys=True) + "\n")
    else:
        os.chmod(config_path, 0o600)
    return validated


def ensure_secret(runtime_dir: str | Path) -> bytes:
    directory = Path(runtime_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / "hmac-secret"
    if not path.exists():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, secrets.token_bytes(32).hex().encode("ascii"))
        finally:
            os.close(fd)
    os.chmod(path, 0o600)
    try:
        secret = bytes.fromhex(path.read_text(encoding="ascii"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"invalid local HMAC secret: {exc}") from exc
    if len(secret) < 32:
        raise ConfigError("local HMAC secret is too short")
    return secret
