"""Unit tests for the format validators and the invalid-format scanner."""

from __future__ import annotations

import pytest

from sfdq.scanners.formats import (
    InvalidFormatScanner,
    check_email,
    check_phone,
    check_postal_code,
    normalize_country,
)
from tests.conftest import rec


@pytest.mark.parametrize(
    "value",
    [
        "jane.doe@acme.com",
        "J.Doe+tag@sub.example.co.uk",
        "o'brien@example.org",
        "A@b.io",
        "X@GMAIL.COM",
    ],
)
def test_valid_emails(value: str) -> None:
    assert check_email(value).valid


@pytest.mark.parametrize(
    ("value", "suggestion"),
    [
        ("jane@gmial.com", "jane@gmail.com"),
        ("jane@hotmial.com", "jane@hotmail.com"),
        ("jane@@acme.com", "jane@acme.com"),
        ("  jane@acme.com ", "jane@acme.com"),
        ("jane@acme", None),
        ("jane.acme.com", None),
        ("jane doe@acme.com", None),
        ("@acme.com", None),
        ("jane@.com", None),
        ("jane..doe@acme.com", None),
    ],
)
def test_invalid_emails(value: str, suggestion: str | None) -> None:
    result = check_email(value)
    assert not result.valid
    assert result.problem
    assert result.suggestion == suggestion


@pytest.mark.parametrize(
    "value",
    [
        "(415) 555-0132", "415-555-0132", "415.555.0132", "4155550132", "+1 415 555 0132",
        "1-415-555-0132", "+44 20 7946 0123", "(415) 555-0132 x204", "415-555-0132 ext. 12",
    ],
)  # fmt: skip
def test_valid_phones(value: str) -> None:
    assert check_phone(value).valid, check_phone(value)


@pytest.mark.parametrize(
    "value",
    [
        "555-0132", "(415) 555-01", "call front desk", "N/A", "0000000000", "1234567890",
        "415-555-0132 / 415-555-0199", "+1 (415) 555-013", "1-800-FLOWERS", "(015) 555-0132",
        "+44 12", "", "()-",
    ],
)  # fmt: skip
def test_invalid_phones(value: str) -> None:
    assert not check_phone(value).valid


def test_phone_whitespace_is_fixable() -> None:
    result = check_phone("  (415) 555-0132 ")
    assert not result.valid and result.suggestion == "(415) 555-0132"


@pytest.mark.parametrize(
    ("value", "country"),
    [
        ("94105", "United States"), ("94105-1234", "USA"), ("M5V 2T6", "Canada"),
        ("K1A0B1", "CA"), ("EC1A 1BB", "United Kingdom"), ("SW1A1AA", "UK"), ("10115", "Germany"),
        ("2000", "Australia"), ("94105", None), ("M5V 2T6", ""), ("anything", "Atlantis"),
    ],
)  # fmt: skip
def test_valid_postal_codes(value: str, country: str | None) -> None:
    assert check_postal_code(value, country).valid


@pytest.mark.parametrize(
    ("value", "country", "suggestion"),
    [
        ("94105-", "United States", "94105"),
        ("941051234", "US", "94105-1234"),
        ("m5v 2t6", "Canada", "M5V 2T6"),
        ("m5v2t6", "Canada", "M5V 2T6"),
        ("ec1a1bb", "United Kingdom", "EC1A 1BB"),
        ("9410", "United States", None),
        ("N/A", "United States", None),
        ("94 105", "US", None),
        ("94105", "Canada", None),
        ("ABCDE", None, None),
    ],
)
def test_invalid_postal_codes(value: str, country: str | None, suggestion: str | None) -> None:
    result = check_postal_code(value, country)
    assert not result.valid
    assert result.suggestion == suggestion


def test_four_digit_zip_gets_leading_zero_hint_but_no_suggestion() -> None:
    result = check_postal_code("2108", "United States")
    assert result.suggestion is None
    assert result.hint and "leading zero" in result.hint


def test_blank_country_accepts_any_supported_format() -> None:
    assert check_postal_code("M5V 2T6", None).valid  # a gap in Country must not cause a false alarm


def test_normalize_country() -> None:
    assert normalize_country("United States of America") == "US"
    assert normalize_country(" canada ") == "CA"
    assert normalize_country("Narnia") is None
    assert normalize_country(None) is None


def test_scanner_flags_only_invalid_non_blank_values(rules) -> None:
    records = [
        rec(
            Id="A1",
            Name="Good Co",
            Phone="(415) 555-0132",
            BillingPostalCode="94105",
            BillingCountry="US",
        ),
        rec(
            Id="A2",
            Name="Bad Phone Co",
            Phone="call me",
            BillingPostalCode="94105",
            BillingCountry="US",
        ),
        rec(Id="A3", Name="Blank Co", Phone=None, BillingPostalCode="  ", BillingCountry=None),
        rec(
            Id="A4",
            Name="Bad Zip Co",
            Phone="415-555-0132",
            BillingPostalCode="9410",
            BillingCountry="US",
        ),
    ]
    findings = InvalidFormatScanner(rules).scan("Account", records)
    assert {(f.record_ids[0], f.details["field"]) for f in findings} == {
        ("A2", "Phone"),
        ("A4", "BillingPostalCode"),
    }
    zip_finding = next(f for f in findings if f.record_ids == ["A4"])
    assert "Bad Zip Co" in zip_finding.summary
    assert zip_finding.details["hint"]


def test_finding_ids_are_stable_across_scans(rules) -> None:
    records = [rec(Id="C1", FirstName="A", LastName="B", Email="nope")]
    first = InvalidFormatScanner(rules).scan("Contact", records)
    second = InvalidFormatScanner(rules).scan("Contact", records)
    assert [f.id for f in first] == [f.id for f in second] and first
