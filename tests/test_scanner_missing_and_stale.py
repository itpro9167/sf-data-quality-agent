"""Unit tests for the missing-fields and stale scanners."""

from __future__ import annotations

from datetime import date

import pytest

from sfdq.rules import Rules
from sfdq.scanners.missing_fields import MissingFieldsScanner
from sfdq.scanners.stale import StaleScanner, months_before
from tests.conftest import AS_OF, rec


def test_missing_fields_reports_each_gap_once_per_record(rules: Rules) -> None:
    records = [
        rec(Id="A1", Name="Full", Industry="Tech", Phone="1", Website="x.com", BillingCountry="US"),
        rec(Id="A2", Name="Gappy", Industry=None, Phone="  ", Website="x.com", BillingCountry="US"),
        rec(Id="A3", Name="Empty"),
    ]
    findings = MissingFieldsScanner(rules).scan("Account", records)
    by_record = {f.record_ids[0]: f for f in findings}
    assert set(by_record) == {"A2", "A3"}
    assert by_record["A2"].details["missing"] == ["Industry", "Phone"]
    assert "2 required fields" in by_record["A2"].summary
    assert len(by_record["A3"].details["missing"]) == 4


def test_missing_fields_is_configurable_per_object(rules: Rules) -> None:
    custom = rules.model_copy(deep=True)
    custom.missing_fields["Contact"].required = ["Email"]
    contact = rec(Id="C1", FirstName="A", LastName="B", Email="a@b.com", Title=None, AccountId=None)
    assert MissingFieldsScanner(custom).scan("Contact", [contact]) == []
    assert len(MissingFieldsScanner(rules).scan("Contact", [contact])) == 1


def test_missing_fields_object_without_config_is_ignored(rules: Rules) -> None:
    custom = rules.model_copy(deep=True)
    custom.missing_fields.pop("Account")
    assert MissingFieldsScanner(custom).scan("Account", [rec(Id="A1")]) == []


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        (date(2026, 11, 4), 12, date(2025, 11, 4)),
        (date(2026, 3, 31), 1, date(2026, 2, 28)),  # clamps to month end
        (date(2024, 3, 31), 1, date(2024, 2, 29)),  # leap year
        (date(2026, 1, 15), 2, date(2025, 11, 15)),  # crosses a year boundary
    ],
)
def test_months_before(day: date, months: int, expected: date) -> None:
    assert months_before(day, months) == expected


def test_stale_needs_both_activity_and_modification_to_be_old(rules: Rules) -> None:
    old = "2024-01-10T10:00:00.000+0000"
    recent = "2026-09-01T10:00:00.000+0000"
    records = [
        rec(Id="old-both", Name="Old", LastModifiedDate=old, LastActivityDate="2024-01-09"),
        rec(
            Id="old-mod-recent-activity",
            Name="A",
            LastModifiedDate=old,
            LastActivityDate="2026-10-01",
        ),
        rec(
            Id="recent-mod-old-activity",
            Name="B",
            LastModifiedDate=recent,
            LastActivityDate="2024-01-09",
        ),
        rec(Id="old-mod-no-activity", Name="C", LastModifiedDate=old, LastActivityDate=None),
        rec(Id="no-dates", Name="D", LastModifiedDate=None, LastActivityDate=None),
    ]
    findings = StaleScanner(rules, AS_OF).scan("Account", records)
    assert {f.record_ids[0] for f in findings} == {"old-both", "old-mod-no-activity"}


def test_stale_boundary_is_exclusive_of_the_cutoff_day(rules: Rules) -> None:
    exactly = rec(Id="edge", Name="E", LastModifiedDate="2025-11-04T00:00:00.000+0000")
    day_before = rec(Id="before", Name="F", LastModifiedDate="2025-11-03T23:59:00.000+0000")
    got = {
        f.record_ids[0] for f in StaleScanner(rules, AS_OF).scan("Account", [exactly, day_before])
    }
    assert got == {"before"}


def test_stale_threshold_is_configurable(rules: Rules) -> None:
    custom = rules.model_copy(deep=True)
    custom.stale.months = 3
    record = rec(Id="r", Name="R", LastModifiedDate="2026-05-01T00:00:00.000+0000")
    assert StaleScanner(rules, AS_OF).scan("Account", [record]) == []
    finding = StaleScanner(custom, AS_OF).scan("Account", [record])[0]
    assert finding.details["months_idle"] == pytest.approx(6.1, abs=0.1)
    assert "no activity or edits" in finding.summary
