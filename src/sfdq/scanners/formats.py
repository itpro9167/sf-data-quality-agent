"""Pure format validators for emails, phone numbers and postal codes, plus the scanner that
applies them to configured fields.

The validators return a :class:`FormatCheck`. A ``suggestion`` is only offered when the repair is
mechanical and unambiguous (trim whitespace, fix a well-known domain typo, uppercase a Canadian
postal code). Anything that needs judgment becomes a ``hint`` for the agent to reason about.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sfdq.models import Finding, FindingKind, ObjectType, Record
from sfdq.rules import FormatRule, Rules
from sfdq.scanners.base import record_label
from sfdq.util import is_blank, stable_id


@dataclass(frozen=True)
class FormatCheck:
    """Result of validating one value."""

    valid: bool
    problem: str | None = None
    suggestion: str | None = None
    hint: str | None = None


OK = FormatCheck(valid=True)

# --------------------------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------------------------
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}$"
)
DOMAIN_TYPOS = {
    "gmial.com": "gmail.com",
    "gmaill.com": "gmail.com",
    "gmai.com": "gmail.com",
    "gamil.com": "gmail.com",
    "gmail.con": "gmail.com",
    "hotmial.com": "hotmail.com",
    "hotmal.com": "hotmail.com",
    "hotmail.con": "hotmail.com",
    "yahooo.com": "yahoo.com",
    "yaho.com": "yahoo.com",
    "yahoo.con": "yahoo.com",
    "outlok.com": "outlook.com",
    "outloook.com": "outlook.com",
}


def check_email(value: str) -> FormatCheck:
    """Validate an email address, suggesting a repair only when it is unambiguous."""
    text = value.strip()
    if _EMAIL_RE.match(text):
        local, domain = text.rsplit("@", 1)
        fixed_domain = DOMAIN_TYPOS.get(domain.lower())
        if fixed_domain:
            return FormatCheck(
                False,
                f"domain '{domain}' looks like a typo of '{fixed_domain}'",
                f"{local}@{fixed_domain}",
            )
        if text != value:
            return FormatCheck(False, "leading or trailing whitespace", text)
        return OK
    if "@@" in text:
        collapsed = re.sub(r"@+", "@", text)
        if _EMAIL_RE.match(collapsed):
            return FormatCheck(False, "repeated '@'", collapsed)
    if "@" not in text:
        return FormatCheck(False, "missing '@'")
    if " " in text:
        return FormatCheck(False, "contains spaces")
    if "." not in text.rsplit("@", 1)[1]:
        return FormatCheck(False, "domain has no top-level domain (e.g. '.com')")
    return FormatCheck(False, "not a valid email address")


# --------------------------------------------------------------------------------------------
# Phone
# --------------------------------------------------------------------------------------------
_EXT_RE = re.compile(r"\s*(?:x|ext\.?|extension)\s*\d{1,6}\s*$", re.IGNORECASE)
_PHONE_CHARS = re.compile(r"^[\d\s().+-]+$")
_PLACEHOLDERS = {"1234567890", "0123456789", "9876543210"}


def _nanp_ok(ten: str) -> bool:
    return ten[0] in "23456789" and ten[3] in "23456789"


def check_phone(value: str) -> FormatCheck:
    """Validate a phone number: NANP (10/11 digits) or ``+<country code>`` E.164-style."""
    text = value.strip()
    body = _EXT_RE.sub("", text)
    if re.search(r"[A-Za-z]", body):
        return FormatCheck(False, "contains letters")
    if not _PHONE_CHARS.match(body):
        return FormatCheck(False, "contains characters that are not part of a phone number")
    digits = re.sub(r"\D", "", body)
    if not digits:
        return FormatCheck(False, "contains no digits")
    if len(set(digits)) == 1 or digits in _PLACEHOLDERS:
        return FormatCheck(False, "looks like a placeholder number")
    if body.lstrip().startswith("+"):
        if digits.startswith("1"):
            if len(digits) != 11 or not _nanp_ok(digits[1:]):
                return FormatCheck(
                    False,
                    f"+1 numbers need 10 digits after the country code, got {len(digits) - 1}",
                )
        elif not 8 <= len(digits) <= 15:
            return FormatCheck(
                False, f"international number has {len(digits)} digits (expected 8-15)"
            )
    else:
        if len(digits) == 11 and digits[0] == "1":
            digits = digits[1:]
        if len(digits) != 10:
            return FormatCheck(
                False,
                f"has {len(digits)} digits; expected 10 (US/Canada) or a +country-code number",
            )
        if not _nanp_ok(digits):
            return FormatCheck(False, "area code and exchange cannot start with 0 or 1")
    if text != value:
        return FormatCheck(False, "leading or trailing whitespace", text)
    return OK


# --------------------------------------------------------------------------------------------
# Postal codes
# --------------------------------------------------------------------------------------------
_COUNTRY_ALIASES = {
    "US": "US", "USA": "US", "U.S.": "US", "U.S.A.": "US", "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US", "AMERICA": "US",
    "CA": "CA", "CAN": "CA", "CANADA": "CA",
    "GB": "GB", "UK": "GB", "U.K.": "GB", "UNITED KINGDOM": "GB", "GREAT BRITAIN": "GB",
    "ENGLAND": "GB", "SCOTLAND": "GB", "WALES": "GB",
    "DE": "DE", "GERMANY": "DE", "DEUTSCHLAND": "DE",
    "FR": "FR", "FRANCE": "FR",
    "AU": "AU", "AUSTRALIA": "AU",
}  # fmt: skip
_POSTAL_PATTERNS = {
    "US": re.compile(r"^\d{5}(?:-\d{4})?$"),
    "CA": re.compile(r"^[A-Z]\d[A-Z] ?\d[A-Z]\d$"),
    "GB": re.compile(r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$"),
    "DE": re.compile(r"^\d{5}$"),
    "FR": re.compile(r"^\d{5}$"),
    "AU": re.compile(r"^\d{4}$"),
}
_EXPECTED = {
    "US": "5 digits or ZIP+4 (12345 / 12345-6789)",
    "CA": "A1A 1A1",
    "GB": "a UK postcode such as EC1A 1BB",
    "DE": "5 digits",
    "FR": "5 digits",
    "AU": "4 digits",
}


def normalize_country(country: str | None) -> str | None:
    """Map a free-text country to a supported ISO code, or ``None`` if unknown/blank."""
    if is_blank(country):
        return None
    return _COUNTRY_ALIASES.get(str(country).strip().upper())


def _repair_postal(text: str, code: str | None) -> str | None:
    """Return a mechanical repair of ``text`` if one exists and validates, else ``None``."""
    candidates = [text.strip()]
    stripped = text.strip().rstrip("-").strip()
    candidates.append(stripped)
    upper = stripped.upper()
    compact = re.sub(r"\s+", "", upper)
    if len(compact) in (6, 7):  # CA: A1A1A1 -> A1A 1A1; GB: EC1A1BB -> EC1A 1BB (canonical first)
        candidates.append(f"{compact[:-3]} {compact[-3:]}")
    candidates.append(upper)
    if re.fullmatch(r"\d{9}", compact):
        candidates.append(f"{compact[:5]}-{compact[5:]}")
    codes = [code] if code else list(_POSTAL_PATTERNS)
    for cand in candidates:
        if any(_POSTAL_PATTERNS[c].match(cand) for c in codes):
            return cand
    return None


def check_postal_code(value: str, country: str | None = None) -> FormatCheck:
    """Validate a postal code for ``country``.

    If ``country`` is blank the code is accepted when it is valid in *any* supported country
    (so a missing country never causes a false alarm). If the country is present but not
    supported, validation is skipped.
    """
    text = value.strip()
    code = normalize_country(country)
    if not is_blank(country) and code is None:
        return OK
    codes = [code] if code else list(_POSTAL_PATTERNS)
    if any(_POSTAL_PATTERNS[c].match(text) for c in codes):
        return OK if text == value else FormatCheck(False, "leading or trailing whitespace", text)
    expected = _EXPECTED[code] if code else "a valid postal code"
    suggestion = _repair_postal(text, code)
    hint = None
    if re.fullmatch(r"\d{4}", text) and code in ("US", None):
        hint = (
            "4 digits: a leading zero may have been dropped "
            "(common for MA, NJ, CT, NH, RI, VT, ME and PR ZIP codes)"
        )
    return FormatCheck(False, f"expected {expected}", suggestion, hint)


def check_value(rule_type: str, value: str, country: str | None = None) -> FormatCheck:
    """Dispatch to the validator for ``rule_type`` (email | phone | postal_code)."""
    if rule_type == "email":
        return check_email(value)
    if rule_type == "phone":
        return check_phone(value)
    if rule_type == "postal_code":
        return check_postal_code(value, country)
    raise ValueError(f"unknown format type {rule_type!r}")


# --------------------------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------------------------
class InvalidFormatScanner:
    """Flags values in configured fields that fail their format check (one finding per field)."""

    name = "invalid_format"

    def __init__(self, rules: Rules) -> None:
        self._rules = rules

    def scan(self, object_type: ObjectType, records: Sequence[Record]) -> list[Finding]:
        """Return one finding per (record, field) with an invalid value."""
        format_rules: list[FormatRule] = self._rules.invalid_formats.get(object_type, [])
        findings: list[Finding] = []
        for record in records:
            for rule in format_rules:
                value = record.get(rule.field)
                if is_blank(value):
                    continue  # absence is the missing-fields scanner's business
                country = record.get(rule.country_field) if rule.country_field else None
                result = check_value(rule.type, str(value), country)
                if result.valid:
                    continue
                label = record_label(object_type, record)
                findings.append(
                    Finding(
                        id=stable_id("invalid_format", object_type, record["Id"], rule.field),
                        kind=FindingKind.INVALID_FORMAT,
                        object_type=object_type,
                        record_ids=[record["Id"]],
                        summary=(
                            f"{object_type} '{label}': {rule.field} {value!r} is not a valid "
                            f"{rule.type.replace('_', ' ')} ({result.problem})"
                        ),
                        details={
                            "field": rule.field,
                            "format": rule.type,
                            "value": value,
                            "problem": result.problem,
                            "suggestion": result.suggestion,
                            "hint": result.hint,
                            "country": country,
                        },
                        snapshot={record["Id"]: record},
                    )
                )
        return findings
