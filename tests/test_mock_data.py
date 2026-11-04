"""The generated org is the foundation of every other test, so pin down its properties."""

from __future__ import annotations

import json
import re
from datetime import date

from sfdq.salesforce.mock import MockSalesforceClient
from sfdq.salesforce.mock_data import generate_mock_org

AS_OF = date(2026, 11, 4)


def test_default_size_and_ids() -> None:
    org = generate_mock_org(as_of=AS_OF)
    assert len(org.records["Account"]) == 200
    assert len(org.records["Contact"]) == 500
    ids = [r["Id"] for k in ("Account", "Contact") for r in org.records[k]]
    assert len(ids) == len(set(ids)), "Ids must be unique"
    assert all(re.fullmatch(r"[a-zA-Z0-9]{18}", i) for i in ids)


def test_generation_is_deterministic() -> None:
    a = json.dumps(generate_mock_org(seed=7, as_of=AS_OF).to_dict(), sort_keys=True)
    b = json.dumps(generate_mock_org(seed=7, as_of=AS_OF).to_dict(), sort_keys=True)
    c = json.dumps(generate_mock_org(seed=8, as_of=AS_OF).to_dict(), sort_keys=True)
    assert a == b
    assert a != c


def test_manifest_covers_every_problem_category() -> None:
    seeded = generate_mock_org(as_of=AS_OF).seeded
    assert len(seeded.duplicate_clusters["Account"]) >= 10
    assert len(seeded.duplicate_clusters["Contact"]) >= 15
    assert len(seeded.stale["Account"]) >= 20 and len(seeded.stale["Contact"]) >= 50
    assert (
        len(seeded.missing_fields["Account"]) >= 20 and len(seeded.missing_fields["Contact"]) >= 30
    )
    assert (
        len(seeded.invalid_formats["Account"]) >= 10
        and len(seeded.invalid_formats["Contact"]) >= 20
    )
    assert seeded.decoys["Account"] and seeded.decoys["Contact"]


def test_contacts_reference_real_accounts() -> None:
    org = generate_mock_org(as_of=AS_OF)
    account_ids = {a["Id"] for a in org.records["Account"]}
    orphans = {c["Id"] for c in org.records["Contact"] if c["AccountId"] is None}
    assert orphans == {
        i for i, f in org.seeded.missing_fields["Contact"].items() if "AccountId" in f
    }
    assert all(c["AccountId"] in account_ids for c in org.records["Contact"] if c["AccountId"])


def test_created_never_after_modified() -> None:
    org = generate_mock_org(as_of=AS_OF)
    for obj in ("Account", "Contact"):
        assert all(r["CreatedDate"] <= r["LastModifiedDate"] for r in org.records[obj])


def test_persistence_roundtrip(tmp_path) -> None:
    path = tmp_path / "org.json"
    first = MockSalesforceClient.load_or_create(path, as_of=AS_OF)
    acct = first.query_records("Account", limit=1)[0]
    first.update_record("Account", acct["Id"], {"Industry": "Zoology"})
    reloaded = MockSalesforceClient.load_or_create(path)
    assert reloaded.get_record("Account", acct["Id"])["Industry"] == "Zoology"


def test_merge_reparents_children_and_removes_victims(mock_sf: MockSalesforceClient) -> None:
    cluster = mock_sf.seeded.duplicate_clusters["Account"][0]
    survivor, victim = cluster[0], cluster[1]
    children = mock_sf.query_records("Contact", where={"AccountId": victim})
    mock_sf.merge_records("Account", survivor, [victim])
    assert mock_sf.get_record("Account", victim) is None
    for child in children:
        assert mock_sf.get_record("Contact", child["Id"])["AccountId"] == survivor
