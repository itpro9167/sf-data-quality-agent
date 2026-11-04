"""Shared fixtures. Everything runs against the mock org -- no Salesforce, no API key."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from sfdq.rules import Rules, load_rules
from sfdq.salesforce.mock import MockSalesforceClient
from sfdq.settings import Settings, load_settings

AS_OF = date(2026, 11, 4)


@pytest.fixture(scope="session")
def rules() -> Rules:
    return load_rules()


@pytest.fixture
def mock_sf() -> MockSalesforceClient:
    """A fresh in-memory mock org (not persisted)."""
    return MockSalesforceClient.load_or_create(None, as_of=AS_OF)


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    """Mock-mode settings rooted in a temp dir, immune to the developer's environment."""
    yield load_settings(
        {"SFDQ_HOME": str(tmp_path / "sfdq"), "SFDQ_MODE": "mock", "SFDQ_REVIEWER": "tester"}
    )


def rec(**fields: object) -> dict[str, object]:
    """Tiny record builder for hand-written scanner cases."""
    return {"Id": fields.pop("Id", "X1"), **fields}
