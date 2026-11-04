"""Choose the mock or real Salesforce client based on :class:`Settings`."""

from __future__ import annotations

from sfdq.salesforce.base import SalesforceClient
from sfdq.salesforce.mock import MockSalesforceClient
from sfdq.salesforce.real import RealSalesforceClient
from sfdq.settings import Settings


def build_salesforce_client(settings: Settings) -> SalesforceClient:
    """Return a real client when SF credentials are configured, otherwise the mock org."""
    if settings.salesforce_mode == "real" and settings.salesforce is not None:
        return RealSalesforceClient.from_credentials(settings.salesforce)
    return MockSalesforceClient.load_or_create(settings.mock_org_path)
