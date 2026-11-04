"""Small shared helpers (time handling, ID hashing)."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from typing import Any

_SF_TZ_NO_COLON = re.compile(r"([+-]\d{2})(\d{2})$")


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with second precision."""
    return utcnow().isoformat(timespec="seconds")


def parse_sf_datetime(value: Any) -> datetime | None:
    """Parse a Salesforce date or datetime string into an aware UTC datetime.

    Salesforce returns ``2025-03-14T10:22:31.000+0000`` for datetimes and ``2025-03-14`` for
    dates. Returns ``None`` for empty or unparseable input rather than raising, because a bad
    timestamp is itself a data-quality matter that callers should handle gracefully.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    text = _SF_TZ_NO_COLON.sub(r"\1:\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def format_sf_datetime(value: datetime) -> str:
    """Format a datetime the way Salesforce does (``2025-03-14T10:22:31.000+0000``)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def stable_id(*parts: str, length: int = 12) -> str:
    """Return a short deterministic hex ID derived from ``parts``."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


def is_blank(value: Any) -> bool:
    """True when a field value is ``None`` or only whitespace."""
    return value is None or (isinstance(value, str) and not value.strip())
