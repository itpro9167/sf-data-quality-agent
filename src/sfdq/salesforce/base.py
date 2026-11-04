"""The Salesforce client interface that both the mock and the real client implement.

The rest of sfdq depends only on this protocol, which is what lets the whole pipeline (and the
test-suite) run with no Salesforce org. It is intentionally small: reads for scanning and the
agent's lookup tools, and three narrowly-scoped writes used *only* by the apply step.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from sfdq.models import Record


class SalesforceError(RuntimeError):
    """Raised when a Salesforce operation fails."""


@runtime_checkable
class SalesforceClient(Protocol):
    """Minimal Salesforce surface used by sfdq."""

    name: str

    def query_records(
        self,
        object_type: str,
        where: Mapping[str, str | None] | None = None,
        limit: int | None = None,
    ) -> list[Record]:
        """Return records of ``object_type`` whose fields equal every ``where`` entry.

        A ``None`` value matches null. With no ``where`` all records are returned.
        """
        ...

    def get_record(self, object_type: str, record_id: str) -> Record | None:
        """Return one record by Id, or ``None`` if it does not exist (or was merged away)."""
        ...

    def update_record(self, object_type: str, record_id: str, fields: Mapping[str, Any]) -> None:
        """Update ``fields`` on one record. WRITE: only called from the apply step."""
        ...

    def merge_records(self, object_type: str, survivor_id: str, victim_ids: Sequence[str]) -> None:
        """Merge victims into the survivor, re-parenting children. WRITE: apply step only."""
        ...

    def create_record(self, object_type: str, fields: Mapping[str, Any]) -> str:
        """Create a record (used for review Tasks) and return its Id. WRITE: apply step only."""
        ...
