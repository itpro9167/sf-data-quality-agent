"""Shared scanner helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sfdq.models import Finding, ObjectType, Record


class Scanner(Protocol):
    """A deterministic check over one object's records. No LLM, no network, no side effects."""

    name: str

    def scan(self, object_type: ObjectType, records: Sequence[Record]) -> list[Finding]:
        """Return the findings for ``records``."""
        ...


def record_label(object_type: str, record: Record) -> str:
    """Human-friendly name for a record (Account name, or Contact first + last name)."""
    if object_type == "Contact":
        name = f"{record.get('FirstName') or ''} {record.get('LastName') or ''}".strip()
        return name or record["Id"]
    return str(record.get("Name") or record["Id"])
