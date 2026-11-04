"""Deterministic scanners (no LLM) and the orchestrator that runs them over a Salesforce client."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from sfdq.models import OBJECT_TYPES, Finding, FindingKind
from sfdq.rules import Rules
from sfdq.salesforce.base import SalesforceClient
from sfdq.scanners.base import Scanner
from sfdq.scanners.duplicates import DuplicateScanner
from sfdq.scanners.formats import InvalidFormatScanner
from sfdq.scanners.missing_fields import MissingFieldsScanner
from sfdq.scanners.stale import StaleScanner

__all__ = [
    "DuplicateScanner",
    "InvalidFormatScanner",
    "MissingFieldsScanner",
    "ScanReport",
    "StaleScanner",
    "build_scanners",
    "run_scan",
]

_KIND_ORDER = {k: i for i, k in enumerate(FindingKind)}


@dataclass
class ScanReport:
    """Everything one scan produced."""

    findings: list[Finding] = field(default_factory=list)
    records_scanned: dict[str, int] = field(default_factory=dict)

    def counts(self) -> Counter[tuple[str, str]]:
        """Findings per ``(object_type, kind)``."""
        return Counter((f.object_type, f.kind.value) for f in self.findings)


def build_scanners(rules: Rules, as_of: date) -> dict[FindingKind, Scanner]:
    """Instantiate one scanner per finding kind."""
    return {
        FindingKind.DUPLICATE: DuplicateScanner(rules.duplicates),
        FindingKind.MISSING_FIELDS: MissingFieldsScanner(rules),
        FindingKind.STALE: StaleScanner(rules, as_of),
        FindingKind.INVALID_FORMAT: InvalidFormatScanner(rules),
    }


def run_scan(
    client: SalesforceClient,
    rules: Rules,
    as_of: date | None = None,
    kinds: Iterable[FindingKind] | None = None,
) -> ScanReport:
    """Read Accounts and Contacts from ``client`` and run every (selected) scanner over them.

    Args:
        client: Mock or real Salesforce client (read-only use).
        rules: Parsed quality rules.
        as_of: "Today" for staleness; defaults to the current date.
        kinds: Restrict to these finding kinds; default is all four.
    """
    as_of = as_of or date.today()
    wanted = set(kinds) if kinds is not None else set(FindingKind)
    scanners = build_scanners(rules, as_of)
    report = ScanReport()
    for object_type in OBJECT_TYPES:
        records = client.query_records(object_type)
        report.records_scanned[object_type] = len(records)
        for kind, scanner in scanners.items():
            if kind in wanted:
                report.findings.extend(scanner.scan(object_type, records))
    report.findings.sort(key=lambda f: (_KIND_ORDER[f.kind], f.object_type, -(f.score or 0), f.id))
    return report
