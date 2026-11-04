"""Real Salesforce client built on simple-salesforce.

Activated only when SF_USERNAME / SF_PASSWORD / SF_SECURITY_TOKEN are set. All SOQL is built from
a fixed field allow-list and escaped values; Salesforce IDs are format-checked before they are
placed in any request body.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from simple_salesforce.api import Salesforce
from simple_salesforce.exceptions import SalesforceError as _SimpleSalesforceError
from simple_salesforce.exceptions import SalesforceResourceNotFound

from sfdq.models import Record
from sfdq.salesforce.base import SalesforceError
from sfdq.salesforce.schema import OBJECT_FIELDS
from sfdq.settings import SalesforceCredentials

_SF_ID = re.compile(r"^[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?$")
_MERGE_BATCH = 2  # Salesforce merges at most a master plus two duplicates per call


def _check_id(value: str) -> str:
    if not _SF_ID.match(value):
        raise SalesforceError(f"not a valid Salesforce Id: {value!r}")
    return value


def _soql_literal(value: str | None) -> str:
    if value is None:
        return "null"
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


class RealSalesforceClient:
    """Implements :class:`sfdq.salesforce.base.SalesforceClient` against a live org."""

    name = "salesforce"

    def __init__(self, sf: Any) -> None:
        self._sf = sf

    @classmethod
    def from_credentials(cls, creds: SalesforceCredentials) -> RealSalesforceClient:
        """Log in with username / password / security token."""
        try:
            sf = Salesforce(
                username=creds.username,
                password=creds.password,
                security_token=creds.security_token,
                domain=creds.domain,
            )
        except _SimpleSalesforceError as exc:
            raise SalesforceError(f"Salesforce login failed: {exc}") from exc
        return cls(sf)

    # -- reads -----------------------------------------------------------------------------
    def query_records(
        self,
        object_type: str,
        where: Mapping[str, str | None] | None = None,
        limit: int | None = None,
    ) -> list[Record]:
        """Run a generated SOQL query and return records without the ``attributes`` noise."""
        fields = self._fields(object_type)
        clauses = []
        for key, value in (where or {}).items():
            if key not in fields:
                raise SalesforceError(f"cannot filter {object_type} on {key!r}")
            clauses.append(f"{key} = {_soql_literal(value)}")
        soql = f"SELECT {', '.join(fields)} FROM {object_type}"
        if clauses:
            soql += " WHERE " + " AND ".join(clauses)
        if limit is not None:
            soql += f" LIMIT {int(limit)}"
        try:
            result = self._sf.query_all(soql)
        except _SimpleSalesforceError as exc:
            raise SalesforceError(f"query failed: {exc}") from exc
        return [self._strip(r) for r in result["records"]]

    def get_record(self, object_type: str, record_id: str) -> Record | None:
        """Fetch one record by Id; ``None`` when it no longer exists (e.g. merged away)."""
        _check_id(record_id)
        fields = self._fields(object_type)
        try:
            row = getattr(self._sf, object_type).get(record_id)
        except SalesforceResourceNotFound:
            return None
        except _SimpleSalesforceError as exc:
            raise SalesforceError(f"get {object_type} {record_id} failed: {exc}") from exc
        return {k: v for k, v in self._strip(row).items() if k in fields}

    # -- writes (apply step only) ----------------------------------------------------------
    def update_record(self, object_type: str, record_id: str, fields: Mapping[str, Any]) -> None:
        """PATCH ``fields`` onto one record."""
        _check_id(record_id)
        allowed = self._fields(object_type)
        bad = [f for f in fields if f not in allowed or f == "Id"]
        if bad:
            raise SalesforceError(f"refusing to write fields {bad} on {object_type}")
        try:
            getattr(self._sf, object_type).update(record_id, dict(fields))
        except _SimpleSalesforceError as exc:
            raise SalesforceError(f"update {object_type} {record_id} failed: {exc}") from exc

    def merge_records(self, object_type: str, survivor_id: str, victim_ids: Sequence[str]) -> None:
        """Merge via Apex ``Database.merge`` (REST has no merge verb; run through Tooling API).

        IDs are regex-validated before being interpolated into the Apex snippet.
        """
        if object_type not in ("Account", "Contact"):
            raise SalesforceError(f"merge is not supported for {object_type}")
        master = _check_id(survivor_id)
        victims = [_check_id(v) for v in victim_ids]
        for i in range(0, len(victims), _MERGE_BATCH):
            batch = victims[i : i + _MERGE_BATCH]
            id_list = ", ".join(f"'{v}'" for v in batch)
            apex = f"Database.merge(new {object_type}(Id='{master}'), new List<Id>{{{id_list}}});"
            try:
                result = self._sf.restful(
                    "tooling/executeAnonymous", params={"anonymousBody": apex}
                )
            except _SimpleSalesforceError as exc:
                raise SalesforceError(f"merge failed: {exc}") from exc
            if not result.get("compiled") or not result.get("success"):
                detail = result.get("compileProblem") or result.get("exceptionMessage")
                raise SalesforceError(f"merge failed: {detail}")

    def create_record(self, object_type: str, fields: Mapping[str, Any]) -> str:
        """Create a record (a review ``Task``) and return its Id."""
        if object_type != "Task":
            raise SalesforceError(f"create is only supported for Task, not {object_type}")
        try:
            result = self._sf.Task.create(dict(fields))
        except _SimpleSalesforceError as exc:
            raise SalesforceError(f"create Task failed: {exc}") from exc
        return str(result["id"])

    # -- helpers ---------------------------------------------------------------------------
    @staticmethod
    def _fields(object_type: str) -> tuple[str, ...]:
        try:
            return OBJECT_FIELDS[object_type]
        except KeyError:
            raise SalesforceError(f"unsupported object {object_type!r}") from None

    @staticmethod
    def _strip(row: Mapping[str, Any]) -> Record:
        return {k: v for k, v in row.items() if k != "attributes"}
