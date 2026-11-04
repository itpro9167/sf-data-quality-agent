"""Fields sfdq reads for each object. Standard fields only, so it works in any org."""

from __future__ import annotations

OBJECT_FIELDS: dict[str, tuple[str, ...]] = {
    "Account": (
        "Id",
        "Name",
        "Website",
        "Phone",
        "Industry",
        "BillingCity",
        "BillingState",
        "BillingPostalCode",
        "BillingCountry",
        "Description",
        "OwnerId",
        "CreatedDate",
        "LastModifiedDate",
        "LastActivityDate",
    ),
    "Contact": (
        "Id",
        "AccountId",
        "FirstName",
        "LastName",
        "Email",
        "Phone",
        "MobilePhone",
        "Title",
        "MailingCity",
        "MailingState",
        "MailingPostalCode",
        "MailingCountry",
        "OwnerId",
        "CreatedDate",
        "LastModifiedDate",
        "LastActivityDate",
    ),
}
