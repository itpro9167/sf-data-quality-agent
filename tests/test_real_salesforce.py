"""RealSalesforceClient against a stub simple-salesforce object (no network, no org)."""

from __future__ import annotations

from typing import Any

import pytest
from simple_salesforce.exceptions import SalesforceMalformedRequest, SalesforceResourceNotFound

from sfdq.salesforce.base import SalesforceClient, SalesforceError
from sfdq.salesforce.real import RealSalesforceClient

ACC = "001Dq0000000001AAA"
ACC2 = "001Dq0000000002AAA"


class StubSObject:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.rows: dict[str, dict[str, Any]] = {}

    def get(self, record_id: str) -> dict[str, Any]:
        if record_id not in self.rows:
            raise SalesforceResourceNotFound("u", 404, "Account", "gone")
        return {"attributes": {"type": "Account"}, **self.rows[record_id], "Secret__c": "x"}

    def update(self, record_id: str, data: dict[str, Any]) -> int:
        self.calls.append(("update", (record_id, data)))
        return 204

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create", data))
        return {"id": "00TDq0000000001AAA", "success": True}


class StubSF:
    def __init__(self) -> None:
        self.Account, self.Contact, self.Task = StubSObject(), StubSObject(), StubSObject()
        self.soql: list[str] = []
        self.apex: list[str] = []
        self.apex_result: dict[str, Any] = {"compiled": True, "success": True}
        self.query_rows: list[dict[str, Any]] = []

    def query_all(self, soql: str) -> dict[str, Any]:
        self.soql.append(soql)
        return {"records": self.query_rows}

    def restful(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        assert path == "tooling/executeAnonymous"
        self.apex.append((params or {})["anonymousBody"])
        return self.apex_result


@pytest.fixture
def sf() -> StubSF:
    return StubSF()


@pytest.fixture
def client(sf: StubSF) -> RealSalesforceClient:
    return RealSalesforceClient(sf)


def test_satisfies_the_client_protocol(client: RealSalesforceClient) -> None:
    assert isinstance(client, SalesforceClient)


def test_query_builds_escaped_soql_from_an_allow_list(
    client: RealSalesforceClient, sf: StubSF
) -> None:
    sf.query_rows = [{"attributes": {"type": "Contact"}, "Id": "003X"}]
    rows = client.query_records("Contact", where={"AccountId": ACC, "Email": None}, limit=5)
    assert rows == [{"Id": "003X"}]  # 'attributes' stripped
    soql = sf.soql[0]
    assert soql.startswith("SELECT Id, AccountId, FirstName") and "FROM Contact" in soql
    assert "WHERE AccountId = '001Dq0000000001AAA' AND Email = null" in soql and soql.endswith(
        "LIMIT 5"
    )


def test_soql_values_are_escaped_and_unknown_fields_refused(
    client: RealSalesforceClient, sf: StubSF
) -> None:
    client.query_records("Account", where={"Name": "O'Brien\\ & Sons"})
    assert "Name = 'O\\'Brien\\\\ & Sons'" in sf.soql[0]
    with pytest.raises(SalesforceError, match="cannot filter"):
        client.query_records("Account", where={"Name = 'x' OR Name": "y"})
    with pytest.raises(SalesforceError, match="unsupported object"):
        client.query_records("Opportunity")


def test_get_record_trims_to_known_fields_and_maps_404_to_none(
    client: RealSalesforceClient, sf: StubSF
) -> None:
    sf.Account.rows[ACC] = {"Id": ACC, "Name": "Acme"}
    assert client.get_record("Account", ACC) == {
        "Id": ACC,
        "Name": "Acme",
    }  # no attributes, no Secret__c
    assert client.get_record("Account", ACC2) is None
    with pytest.raises(SalesforceError, match="not a valid Salesforce Id"):
        client.get_record("Account", "x' OR '1'='1")


def test_update_only_allows_known_non_id_fields(client: RealSalesforceClient, sf: StubSF) -> None:
    client.update_record("Account", ACC, {"Phone": "(415) 555-0132"})
    assert sf.Account.calls == [("update", (ACC, {"Phone": "(415) 555-0132"}))]
    for bad in ({"Id": "x"}, {"Not_A_Field__c": "x"}):
        with pytest.raises(SalesforceError, match="refusing to write"):
            client.update_record("Account", ACC, bad)


def test_merge_uses_apex_database_merge_in_batches_of_two(
    client: RealSalesforceClient, sf: StubSF
) -> None:
    victims = [f"001Dq00000000{i:02d}AAA" for i in range(3, 6)]
    client.merge_records("Account", ACC, victims)
    assert len(sf.apex) == 2  # 3 victims -> batches of 2 + 1
    assert sf.apex[0] == (
        f"Database.merge(new Account(Id='{ACC}'), new List<Id>{{'{victims[0]}', '{victims[1]}'}});"
    )


def test_merge_validates_ids_before_building_apex_and_surfaces_failures(
    client: RealSalesforceClient, sf: StubSF
) -> None:
    with pytest.raises(SalesforceError, match="not a valid Salesforce Id"):
        client.merge_records("Account", ACC, ["'); delete [select Id from Account]; //"])
    assert sf.apex == []
    sf.apex_result = {"compiled": True, "success": False, "exceptionMessage": "DUPLICATE_VALUE"}
    with pytest.raises(SalesforceError, match="DUPLICATE_VALUE"):
        client.merge_records("Account", ACC, [ACC2])
    with pytest.raises(SalesforceError, match="not supported"):
        client.merge_records("Opportunity", ACC, [ACC2])


def test_create_only_supports_tasks_and_wraps_api_errors(
    client: RealSalesforceClient, sf: StubSF
) -> None:
    assert client.create_record("Task", {"Subject": "s"}) == "00TDq0000000001AAA"
    with pytest.raises(SalesforceError, match="only supported for Task"):
        client.create_record("Account", {"Name": "x"})

    def boom(_: dict[str, Any]) -> None:
        raise SalesforceMalformedRequest("u", 400, "Task", "bad")

    sf.Task.create = boom  # type: ignore[method-assign]
    with pytest.raises(SalesforceError, match="create Task failed"):
        client.create_record("Task", {"Subject": "s"})
