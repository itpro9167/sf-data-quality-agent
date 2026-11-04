"""Score the full scan against the generator's manifest of injected problems."""

from __future__ import annotations

from sfdq.models import FindingKind
from sfdq.rules import Rules
from sfdq.salesforce.mock import MockSalesforceClient
from sfdq.scanners import run_scan
from tests.conftest import AS_OF


def test_scan_matches_every_seeded_problem_and_nothing_else(
    rules: Rules, mock_sf: MockSalesforceClient
) -> None:
    report = run_scan(mock_sf, rules, as_of=AS_OF)
    seeded = mock_sf.seeded

    def first_ids(kind: FindingKind, obj: str) -> set[str]:
        return {f.record_ids[0] for f in report.findings if f.kind is kind and f.object_type == obj}

    for obj in ("Account", "Contact"):
        assert first_ids(FindingKind.STALE, obj) == set(seeded.stale[obj])
        assert first_ids(FindingKind.MISSING_FIELDS, obj) == set(seeded.missing_fields[obj])
        got = {
            (f.record_ids[0], f.details["field"])
            for f in report.findings
            if f.kind is FindingKind.INVALID_FORMAT and f.object_type == obj
        }
        assert got == {tuple(x) for x in seeded.invalid_formats[obj]}

    missing = {f.record_ids[0]: f.details["missing"] for f in report.findings
               if f.kind is FindingKind.MISSING_FIELDS and f.object_type == "Account"}  # fmt: skip
    assert {k: sorted(v) for k, v in missing.items()} == {
        k: sorted(v) for k, v in seeded.missing_fields["Account"].items()
    }


def test_scan_is_idempotent_and_ids_are_unique(rules: Rules, mock_sf: MockSalesforceClient) -> None:
    first = run_scan(mock_sf, rules, as_of=AS_OF)
    second = run_scan(mock_sf, rules, as_of=AS_OF)
    assert [f.id for f in first.findings] == [f.id for f in second.findings]
    assert len({f.id for f in first.findings}) == len(first.findings)


def test_kind_filter(rules: Rules, mock_sf: MockSalesforceClient) -> None:
    report = run_scan(mock_sf, rules, as_of=AS_OF, kinds=[FindingKind.STALE])
    assert {f.kind for f in report.findings} == {FindingKind.STALE}
    assert report.records_scanned == {"Account": 200, "Contact": 500}
