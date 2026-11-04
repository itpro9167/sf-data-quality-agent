"""In-memory mock Salesforce org with the same interface as the real client.

State is optionally persisted to a JSON file so that a mock-mode ``sfdq apply`` is visible to the
next ``sfdq scan`` -- you can watch the number of findings drop after fixes are applied.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from sfdq.models import Record
from sfdq.salesforce.base import SalesforceError
from sfdq.salesforce.mock_data import MockOrgData, generate_mock_org
from sfdq.util import format_sf_datetime, utcnow

_ID_PREFIX = {"Account": "001", "Contact": "003", "Task": "00T", "User": "005"}


class MockSalesforceClient:
    """A fake Salesforce org. Implements :class:`sfdq.salesforce.base.SalesforceClient`."""

    name = "mock"

    def __init__(self, data: MockOrgData, path: Path | None = None) -> None:
        self._data = data
        self._path = path
        self._merged: dict[str, str] = {}  # victim Id -> survivor Id

    # -- construction ----------------------------------------------------------------------
    @classmethod
    def load_or_create(
        cls, path: Path | None, *, as_of: date | None = None, seed: int = 42
    ) -> MockSalesforceClient:
        """Load the org snapshot at ``path``, generating (and saving) a fresh one if absent."""
        if path is not None and path.exists():
            payload = json.loads(path.read_text("utf-8"))
            client = cls(MockOrgData.from_dict(payload["org"]), path)
            client._merged = payload.get("merged", {})
            return client
        client = cls(generate_mock_org(seed=seed, as_of=as_of), path)
        client._save()
        return client

    def reset(self, *, as_of: date | None = None, seed: int = 42) -> None:
        """Throw away all changes and regenerate the pristine seeded org."""
        self._data = generate_mock_org(seed=seed, as_of=as_of)
        self._merged = {}
        self._save()

    @property
    def seeded(self) -> Any:
        """Ground-truth manifest of injected problems (used by tests and demos)."""
        return self._data.seeded

    # -- reads -----------------------------------------------------------------------------
    def query_records(
        self,
        object_type: str,
        where: Mapping[str, str | None] | None = None,
        limit: int | None = None,
    ) -> list[Record]:
        """Return copies of matching records (see :class:`SalesforceClient`)."""
        rows = self._table(object_type)
        out: list[Record] = []
        for row in rows:
            if where and any(row.get(k) != v for k, v in where.items()):
                continue
            out.append(copy.deepcopy(row))
            if limit is not None and len(out) >= limit:
                break
        return out

    def get_record(self, object_type: str, record_id: str) -> Record | None:
        """Return a copy of one record, or ``None`` if absent or merged away."""
        for row in self._table(object_type):
            if row["Id"] == record_id:
                return copy.deepcopy(row)
        return None

    # -- writes (apply step only) ----------------------------------------------------------
    def update_record(self, object_type: str, record_id: str, fields: Mapping[str, Any]) -> None:
        """Set ``fields`` on a record and bump ``LastModifiedDate`` like Salesforce does."""
        row = self._find(object_type, record_id)
        unknown = [f for f in fields if f not in row and f != "Id"]
        if unknown:
            raise SalesforceError(f"No such column(s) on {object_type}: {unknown}")
        if "Id" in fields:
            raise SalesforceError("Id is not writable")
        row.update(fields)
        row["LastModifiedDate"] = format_sf_datetime(utcnow())
        self._save()

    def merge_records(self, object_type: str, survivor_id: str, victim_ids: Sequence[str]) -> None:
        """Merge victims into the survivor: re-parent children, then delete the victims."""
        if object_type not in ("Account", "Contact"):
            raise SalesforceError(f"merge is not supported for {object_type}")
        survivor = self._find(object_type, survivor_id)
        victims = [self._find(object_type, v) for v in victim_ids]
        if survivor_id in victim_ids:
            raise SalesforceError("survivor cannot be merged into itself")
        victim_set = set(victim_ids)
        if object_type == "Account":
            for contact in self._data.records["Contact"]:
                if contact.get("AccountId") in victim_set:
                    contact["AccountId"] = survivor_id
        for task in self._data.records["Task"]:
            if task.get("WhatId") in victim_set:
                task["WhatId"] = survivor_id
            if task.get("WhoId") in victim_set:
                task["WhoId"] = survivor_id
        table = self._data.records[object_type]
        self._data.records[object_type] = [r for r in table if r["Id"] not in victim_set]
        for victim in victims:
            self._merged[victim["Id"]] = survivor_id
        survivor["LastModifiedDate"] = format_sf_datetime(utcnow())
        self._save()

    def create_record(self, object_type: str, fields: Mapping[str, Any]) -> str:
        """Create a record (only ``Task`` is needed by sfdq) and return its Id."""
        if object_type != "Task":
            raise SalesforceError(f"create is only supported for Task, not {object_type}")
        record_id = f"{_ID_PREFIX['Task']}Dq{len(self._data.records['Task']) + 1:010d}AAA"
        now = format_sf_datetime(utcnow())
        self._data.records["Task"].append(
            {"Id": record_id, "CreatedDate": now, "LastModifiedDate": now, **fields}
        )
        self._save()
        return record_id

    # -- helpers ---------------------------------------------------------------------------
    def _table(self, object_type: str) -> list[Record]:
        try:
            return self._data.records[object_type]
        except KeyError:
            raise SalesforceError(f"sObject type '{object_type}' is not supported") from None

    def _find(self, object_type: str, record_id: str) -> Record:
        for row in self._table(object_type):
            if row["Id"] == record_id:
                return row
        raise SalesforceError(f"{object_type} {record_id} not found (deleted or merged?)")

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"org": self._data.to_dict(), "merged": self._merged}
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1), "utf-8")
        tmp.replace(self._path)
