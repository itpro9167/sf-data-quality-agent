"""Missing-key-field scanner: required fields (per object, from YAML) that are null or blank."""

from __future__ import annotations

from collections.abc import Sequence

from sfdq.models import Finding, FindingKind, ObjectType, Record
from sfdq.rules import Rules
from sfdq.scanners.base import record_label
from sfdq.util import is_blank, stable_id


class MissingFieldsScanner:
    """One finding per record that lacks one or more required fields."""

    name = "missing_fields"

    def __init__(self, rules: Rules) -> None:
        self._rules = rules

    def scan(self, object_type: ObjectType, records: Sequence[Record]) -> list[Finding]:
        """Return a finding for each record missing any configured required field."""
        config = self._rules.missing_fields.get(object_type)
        if config is None or not config.required:
            return []
        findings: list[Finding] = []
        for record in records:
            missing = [f for f in config.required if is_blank(record.get(f))]
            if not missing:
                continue
            label = record_label(object_type, record)
            noun = "field" if len(missing) == 1 else "fields"
            findings.append(
                Finding(
                    id=stable_id("missing_fields", object_type, record["Id"]),
                    kind=FindingKind.MISSING_FIELDS,
                    object_type=object_type,
                    record_ids=[record["Id"]],
                    summary=(
                        f"{object_type} '{label}' is missing {len(missing)} required {noun}: "
                        + ", ".join(missing)
                    ),
                    details={"missing": missing, "required": config.required},
                    snapshot={record["Id"]: record},
                )
            )
        return findings
