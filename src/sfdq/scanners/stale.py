"""Stale-record scanner: nothing touched the record for N months."""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from datetime import date

from sfdq.models import Finding, FindingKind, ObjectType, Record
from sfdq.rules import Rules
from sfdq.scanners.base import record_label
from sfdq.util import parse_sf_datetime, stable_id


def months_before(day: date, months: int) -> date:
    """Calendar-aware ``day`` minus ``months`` (clamps the day to the target month's length)."""
    year, month0 = divmod(day.year * 12 + day.month - 1 - months, 12)
    month = month0 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


class StaleScanner:
    """A record is stale when BOTH its last activity and last modification predate the cutoff."""

    name = "stale"

    def __init__(self, rules: Rules, as_of: date) -> None:
        self._months = rules.stale.months
        self._as_of = as_of
        self._cutoff = months_before(as_of, self._months)

    def scan(self, object_type: ObjectType, records: Sequence[Record]) -> list[Finding]:
        """Return a finding for each record untouched since the cutoff."""
        findings: list[Finding] = []
        for record in records:
            activity = parse_sf_datetime(record.get("LastActivityDate"))
            modified = parse_sf_datetime(record.get("LastModifiedDate"))
            touches = [t for t in (activity, modified) if t is not None]
            if not touches:
                continue  # no timestamps at all: nothing to judge by
            last = max(touches)
            if last.date() >= self._cutoff:
                continue
            months_idle = round((self._as_of - last.date()).days / 30.44, 1)
            label = record_label(object_type, record)
            findings.append(
                Finding(
                    id=stable_id("stale", object_type, record["Id"]),
                    kind=FindingKind.STALE,
                    object_type=object_type,
                    record_ids=[record["Id"]],
                    summary=(
                        f"{object_type} '{label}' has had no activity or edits for "
                        f"{months_idle:g} months (threshold: {self._months})"
                    ),
                    score=None,
                    details={
                        "months_idle": months_idle,
                        "threshold_months": self._months,
                        "last_activity_date": record.get("LastActivityDate"),
                        "last_modified_date": record.get("LastModifiedDate"),
                        "cutoff": self._cutoff.isoformat(),
                        "owner_id": record.get("OwnerId"),
                    },
                    snapshot={record["Id"]: record},
                )
            )
        return findings
