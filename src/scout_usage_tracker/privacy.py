"""One-way identifiers used by the local history database."""

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any


def source_event_key(secret: bytes, source_path: str | Path, source_id: object) -> str:
    normalized = str(Path(source_path).expanduser().resolve())
    payload = (normalized + "\0" + str(source_id)).encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def session_digest(secret: bytes, session_id: str) -> str:
    return hmac.new(secret, session_id.encode("utf-8"), hashlib.sha256).hexdigest()


def session_label(digest: str) -> str:
    return digest[:12]


def content_version_uid(fields: dict[str, Any]) -> str:
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

