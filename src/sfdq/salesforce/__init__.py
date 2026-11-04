"""Salesforce access: a protocol, a realistic in-memory mock, and the real client."""

from sfdq.salesforce.base import SalesforceClient, SalesforceError

__all__ = ["SalesforceClient", "SalesforceError"]
