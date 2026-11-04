"""Typed loader for the YAML quality rules."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sfdq.models import OBJECT_TYPES


class RulesError(ValueError):
    """Raised when the rules file is missing or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StaleRules(_Strict):
    """Staleness threshold."""

    months: int = Field(default=12, ge=1)


class MissingFieldRules(_Strict):
    """Fields that must be populated for one object."""

    required: list[str] = Field(default_factory=list)


class FormatRule(_Strict):
    """One field to validate and how."""

    field: str
    type: Literal["email", "phone", "postal_code"]
    country_field: str | None = None


class NameWeight(_Strict):
    """Name similarity ramps from 0 at ``floor`` to ``max`` at 1.0."""

    floor: float = Field(ge=0, lt=1)
    max: float = Field(ge=0, le=1)


class AccountDuplicateWeights(_Strict):
    """Per-signal probabilities for Account matching."""

    name: NameWeight
    domain: float = Field(ge=0, le=1)
    phone: float = Field(ge=0, le=1)
    postal: float = Field(ge=0, le=1)


class ContactDuplicateWeights(_Strict):
    """Per-signal probabilities for Contact matching."""

    email: float = Field(ge=0, le=1)
    name: NameWeight
    phone: float = Field(ge=0, le=1)
    same_account: float = Field(ge=0, le=1)


class DuplicateRules(_Strict):
    """Duplicate-detection thresholds and weights."""

    min_confidence: float = Field(default=0.70, ge=0, le=1)
    max_block_size: int = Field(default=300, ge=2)
    Account: AccountDuplicateWeights
    Contact: ContactDuplicateWeights


class Rules(_Strict):
    """The full rules document."""

    stale: StaleRules = Field(default_factory=StaleRules)
    missing_fields: dict[str, MissingFieldRules] = Field(default_factory=dict)
    default_country: str = "US"
    invalid_formats: dict[str, list[FormatRule]] = Field(default_factory=dict)
    duplicates: DuplicateRules
    writable_fields: dict[str, list[str]] = Field(default_factory=dict)

    def writable(self, object_type: str) -> frozenset[str]:
        """Fields a proposal may modify on ``object_type``."""
        return frozenset(self.writable_fields.get(object_type, []))


def _read_default() -> str:
    return resources.files("sfdq.config").joinpath("quality_rules.yaml").read_text("utf-8")


def load_rules(path: Path | None = None) -> Rules:
    """Load and validate rules from ``path`` (or the bundled defaults).

    Raises:
        RulesError: if the file is missing, is not valid YAML, or fails validation.
    """
    try:
        text = path.read_text("utf-8") if path else _read_default()
    except OSError as exc:
        raise RulesError(f"cannot read rules file {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
        rules = Rules.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise RulesError(f"invalid rules file {path or '(bundled)'}: {exc}") from exc
    unknown = {
        obj
        for section in (rules.missing_fields, rules.invalid_formats, rules.writable_fields)
        for obj in section
    } - set(OBJECT_TYPES)
    if unknown:
        raise RulesError(f"rules reference unsupported objects: {sorted(unknown)}")
    return rules
