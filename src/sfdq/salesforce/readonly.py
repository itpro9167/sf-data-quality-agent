"""A read-only view of a Salesforce client.

Used by integrations that must never write (the MCP server, the eval harness): the wrapper
delegates reads and raises on every write, so "this process cannot change Salesforce" is a
structural guarantee instead of a convention.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sfdq.models import Record
from sfdq.salesforce.base import SalesforceClient, SalesforceError


class ReadOnlyError(SalesforceError):
    """Raised when something tries to write through a read-only client."""


class ReadOnlySalesforceClient:
    """Wraps another :class:`SalesforceClient`, allowing reads and refusing all writes."""

    def __init__(self, inner: SalesforceClient) -> None:
        self._inner = inner
        self.name = f"{inner.name} (read-only)"

    def query_records(
        self,
        object_type: str,
        where: Mapping[str, str | None] | None = None,
        limit: int | None = None,
    ) -> list[Record]:
        """Delegate to the wrapped client."""
        return self._inner.query_records(object_type, where, limit)

    def get_record(self, object_type: str, record_id: str) -> Record | None:
        """Delegate to the wrapped client."""
        return self._inner.get_record(object_type, record_id)

    def update_record(self, object_type: str, record_id: str, fields: Mapping[str, Any]) -> None:
        """Always raises: this client is read-only."""
        raise ReadOnlyError("this Salesforce client is read-only; use `sfdq apply` to write")

    def merge_records(self, object_type: str, survivor_id: str, victim_ids: Sequence[str]) -> None:
        """Always raises: this client is read-only."""
        raise ReadOnlyError("this Salesforce client is read-only; use `sfdq apply` to write")

    def create_record(self, object_type: str, fields: Mapping[str, Any]) -> str:
        """Always raises: this client is read-only."""
        raise ReadOnlyError("this Salesforce client is read-only; use `sfdq apply` to write")
