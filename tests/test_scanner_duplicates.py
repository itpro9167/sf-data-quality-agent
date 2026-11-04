"""Unit tests for the duplicate scanner: normalizers, scoring, clustering, ground truth."""

from __future__ import annotations

import pytest
from tests.conftest import rec

from sfdq.rules import Rules
from sfdq.salesforce.mock import MockSalesforceClient
from sfdq.scanners.duplicates import (
    DuplicateScanner,
    noisy_or,
    normalize_company_name,
    normalize_person_name,
    phone_key,
    suggest_survivor,
    website_domain,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Corp.", "acme"),
        ("ACME, INCORPORATED", "acme"),
        ("The Acme Company", "acme"),
        ("Smith & Jones LLC", "smith and jones"),
        ("Bluepeak Software Ltd", "bluepeak software"),
        (None, ""),
    ],
)
def test_normalize_company_name(raw: str | None, expected: str) -> None:
    assert normalize_company_name(raw) == expected


def test_normalize_person_name_maps_nicknames() -> None:
    assert normalize_person_name("Bob", "Chen") == normalize_person_name("Robert", "Chen")
    assert normalize_person_name("Zoe", "O'Neil") == "zoe o'neil"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.Acme.com/about", "acme.com"),
        ("www.acme.com", "acme.com"),
        ("acme.com:8080", "acme.com"),
        ("https://linkedin.com/company/acme", None),
        ("not a url", None),
        (None, None),
    ],
)
def test_website_domain(raw: str | None, expected: str | None) -> None:
    assert website_domain(raw) == expected


def test_phone_key_ignores_formatting_country_code_and_extension() -> None:
    assert phone_key("(415) 555-0132") == phone_key("+1 415.555.0132 x22") == "4155550132"
    assert phone_key("555-0132") is None
    assert phone_key("0000000000") is None


def test_noisy_or() -> None:
    assert noisy_or([]) == 0.0
    assert noisy_or([0.5, 0.5]) == pytest.approx(0.75)
    assert noisy_or([1.0, 0.3]) == 1.0


def _account(i: str, name: str, **kw: object) -> dict[str, object]:
    return rec(Id=i, Name=name, Website=kw.pop("Website", None), Phone=kw.pop("Phone", None), **kw)


def test_legal_suffix_and_case_differences_are_flagged(rules: Rules) -> None:
    records = [
        _account("A1", "Bluepeak Software Inc"),
        _account("A2", "BLUEPEAK SOFTWARE, INCORPORATED"),
        _account("A3", "Unrelated Foods LLC"),
    ]
    findings = DuplicateScanner(rules.duplicates).scan("Account", records)
    assert [set(f.record_ids) for f in findings] == [{"A1", "A2"}]
    assert findings[0].score is not None and findings[0].score >= 0.7


def test_same_domain_with_different_names_is_flagged_with_evidence(rules: Rules) -> None:
    records = [
        _account("A1", "Bluepeak Software Inc", Website="https://www.bluepeak.com"),
        _account("A2", "Bluepeak Digital", Website="bluepeak.com"),
    ]
    (finding,) = DuplicateScanner(rules.duplicates).scan("Account", records)
    assert any(
        "same website domain (bluepeak.com)" in e for e in finding.details["pairs"][0]["evidence"]
    )


def test_similar_names_alone_are_not_enough_without_corroboration(rules: Rules) -> None:
    records = [_account("A1", "Summit Dental Group"), _account("A2", "Summit Dental Partners")]
    assert DuplicateScanner(rules.duplicates).scan("Account", records) == []


def test_same_phone_alone_is_below_threshold_for_accounts(rules: Rules) -> None:
    records = [
        _account("A1", "Alpha Freight", Phone="(415) 555-0132"),
        _account("A2", "Omega Textiles", Phone="415.555.0132"),
    ]
    assert DuplicateScanner(rules.duplicates).scan("Account", records) == []


def _contact(i: str, first: str, last: str, **kw: object) -> dict[str, object]:
    return rec(Id=i, FirstName=first, LastName=last, Email=kw.pop("Email", None),
               Phone=kw.pop("Phone", None), AccountId=kw.pop("AccountId", None), **kw)  # fmt: skip


def test_same_email_different_case_is_a_high_confidence_duplicate(rules: Rules) -> None:
    records = [
        _contact("C1", "Robert", "Chen", Email="robert.chen@acme.com", AccountId="A1"),
        _contact("C2", "Bob", "Chen", Email="ROBERT.CHEN@ACME.COM", AccountId="A2"),
    ]
    (finding,) = DuplicateScanner(rules.duplicates).scan("Contact", records)
    assert finding.score is not None and finding.score >= 0.9


def test_common_name_at_different_companies_is_not_a_duplicate(rules: Rules) -> None:
    records = [
        _contact(
            "C1",
            "Maria",
            "Garcia",
            Email="maria.garcia@a.com",
            Phone="415-555-0001",
            AccountId="A1",
        ),
        _contact(
            "C2",
            "Maria",
            "Garcia",
            Email="maria.garcia@b.com",
            Phone="212-555-0002",
            AccountId="A2",
        ),
    ]
    assert DuplicateScanner(rules.duplicates).scan("Contact", records) == []


def test_same_name_same_account_is_flagged(rules: Rules) -> None:
    records = [
        _contact("C1", "Maria", "Garcia", Email="maria@a.com", AccountId="A1"),
        _contact("C2", "Maria", "Garcia", Email="m.garcia@a.com", AccountId="A1"),
    ]
    assert len(DuplicateScanner(rules.duplicates).scan("Contact", records)) == 1


def test_three_way_duplicates_form_one_cluster_with_weakest_link_score(rules: Rules) -> None:
    records = [
        _contact("C1", "Ann", "Lee", Email="ann@x.com"),
        _contact("C2", "Ann", "Lee", Email="ANN@x.com", Phone="415-555-0100"),
        _contact("C3", "Ann", "Lee", Email="a@x.com", Phone="(415) 555-0100"),
    ]
    # C1-C2 share an email (~0.98); C2-C3 share name + phone (0.80); C1-C3 has name only (0.60,
    # below threshold). They still form ONE cluster, scored by its weakest edge.
    findings = DuplicateScanner(rules.duplicates).scan("Contact", records)
    assert len(findings) == 1 and set(findings[0].record_ids) == {"C1", "C2", "C3"}
    assert findings[0].score == pytest.approx(0.8)
    assert findings[0].score == min(p["confidence"] for p in findings[0].details["pairs"])


def test_survivor_is_most_complete_then_most_recent() -> None:
    sparse = rec(Id="1", Name="Acme", LastModifiedDate="2026-10-01T00:00:00.000+0000")
    full = rec(Id="2", Name="Acme", Phone="1", Website="a.com", Industry="Tech",
               LastModifiedDate="2020-01-01T00:00:00.000+0000")  # fmt: skip
    survivor, reason = suggest_survivor("Account", [sparse, full])
    assert survivor["Id"] == "2" and "most complete" in reason


def test_min_confidence_is_configurable(rules: Rules) -> None:
    strict = rules.model_copy(deep=True)
    strict.duplicates.min_confidence = 0.99
    records = [_account("A1", "Bluepeak Software Inc"), _account("A2", "Bluepeak Software LLC")]
    assert len(DuplicateScanner(rules.duplicates).scan("Account", records)) == 1
    assert DuplicateScanner(strict.duplicates).scan("Account", records) == []


@pytest.mark.parametrize("object_type", ["Account", "Contact"])
def test_matches_seeded_ground_truth_exactly(
    object_type: str, rules: Rules, mock_sf: MockSalesforceClient
) -> None:
    """Perfect recall on every seeded cluster and zero false positives (decoys included)."""
    findings = DuplicateScanner(rules.duplicates).scan(
        object_type, mock_sf.query_records(object_type)
    )  # type: ignore[arg-type]
    found = {frozenset(f.record_ids) for f in findings}
    expected = {frozenset(c) for c in mock_sf.seeded.duplicate_clusters[object_type]}
    assert found == expected
    decoys = {frozenset(c) for c in mock_sf.seeded.decoys[object_type]}
    assert not any(d <= f for d in decoys for f in found), "a decoy pair was reported as duplicate"
