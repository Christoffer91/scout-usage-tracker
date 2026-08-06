"""One-way identifiers used by the local history database."""

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any


_CHAT_ADJECTIVES = (
    "Amber", "Arctic", "Autumn", "Azure", "Bright", "Calm", "Cedar", "Cloud",
    "Coral", "Cosmic", "Crystal", "Dawn", "Ember", "Emerald", "Golden", "Indigo",
    "Ivory", "Lunar", "Maple", "Misty", "Mossy", "Ocean", "Quiet", "River",
    "Silver", "Solar", "Spring", "Stellar", "Summer", "Velvet", "Wild", "Winter",
)
_CHAT_NOUNS = (
    "Badger", "Birch", "Comet", "Falcon", "Fern", "Finch", "Forest", "Fox",
    "Garden", "Grove", "Harbor", "Hawk", "Hill", "Lake", "Lark", "Meadow",
    "Moon", "Oak", "Otter", "Owl", "Panda", "Pine", "Raven", "Reef",
    "Robin", "Star", "Stone", "Swift", "Valley", "Wave", "Willow", "Wren",
)
_CHAT_PLACES = (
    "Beacon", "Brook", "Canyon", "Cloud", "Cove", "Dawn", "Delta", "Field",
    "Flame", "Garden", "Glen", "Grove", "Harbor", "Hearth", "Hill", "Isle",
    "Lake", "Meadow", "Moon", "Peak", "Pine", "Reef", "Ridge", "River",
    "Shore", "Sky", "Spring", "Star", "Stone", "Trail", "Vale", "Wave",
)


def source_event_key(secret: bytes, source_path: str | Path, source_id: object) -> str:
    normalized = str(Path(source_path).expanduser().resolve())
    payload = (normalized + "\0" + str(source_id)).encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def session_digest(secret: bytes, session_id: str) -> str:
    return hmac.new(secret, session_id.encode("utf-8"), hashlib.sha256).hexdigest()


def session_label(digest: str) -> str:
    return digest[:12]


def friendly_session_label(label: str) -> str:
    """Create a stable content-free display name from an anonymized session label."""
    try:
        value = int(label[:12], 16)
    except (TypeError, ValueError):
        value = int(hashlib.sha256(str(label).encode("utf-8")).hexdigest()[:12], 16)
    adjective = _CHAT_ADJECTIVES[value & 31]
    noun = _CHAT_NOUNS[(value >> 5) & 31]
    place = _CHAT_PLACES[(value >> 10) & 31]
    return f"Chat {adjective} {noun} {place}"


def content_version_uid(fields: dict[str, Any]) -> str:
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
