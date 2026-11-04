"""The agent's tools.

Two read-only lookups and one terminal ``submit_proposal`` tool. None of them can write to
Salesforce: ``submit_proposal`` only validates and captures a draft that the caller then queues
for human review. Reads are scoped to the finding's records (plus records the lookups returned),
so a manipulated model cannot wander through the org.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from sfdq.agent.llm import ToolCall
from sfdq.models import (
    ActionType,
    Confidence,
    FieldChange,
    Finding,
    MergePlan,
    ProposalDraft,
    Record,
)
from sfdq.policy import PolicyViolation, WritePolicy
from sfdq.salesforce.base import SalesforceClient, SalesforceError

_OBJECT_ENUM = {"type": "string", "enum": ["Account", "Contact"]}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_record",
        "description": (
            "Read-only. Fetch the current live version of one Account or Contact that is part of "
            "this finding (or was returned by get_related_records)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"object_type": _OBJECT_ENUM, "record_id": {"type": "string"}},
            "required": ["object_type", "record_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_related_records",
        "description": (
            "Read-only. For an Account: its contacts (name, email, title, phone, last activity). "
            "For a Contact: its Account and up to 10 colleagues at that Account."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"object_type": _OBJECT_ENUM, "record_id": {"type": "string"}},
            "required": ["object_type", "record_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_proposal",
        "description": (
            "Submit your single proposed fix for this finding. It is queued for human approval; "
            "nothing is changed in Salesforce. Call this exactly once, as your final step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": [a.value for a in ActionType]},
                "confidence": {"type": "string", "enum": [c.value for c in Confidence]},
                "explanation": {
                    "type": "string",
                    "description": "2-3 sentences of plain business English for the reviewer.",
                },
                "changes": {
                    "type": "array",
                    "description": (
                        "Field edits. For update_field these are the fix; for merge they fill "
                        "BLANK fields on the survivor. Omit for flag_for_review."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "record_id": {"type": "string"},
                            "field": {"type": "string"},
                            "new_value": {"type": ["string", "null"]},
                        },
                        "required": ["record_id", "field", "new_value"],
                        "additionalProperties": False,
                    },
                },
                "survivor_id": {
                    "type": "string",
                    "description": "merge only: the record to keep.",
                },
                "victim_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "merge only: records to merge away (default: all others).",
                },
                "review_note": {
                    "type": "string",
                    "description": "flag_for_review only: the question for the record owner.",
                },
            },
            "required": ["action", "confidence", "explanation"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class ToolResult:
    """What a tool returns to the model."""

    content: str
    is_error: bool = False


def _compact(record: Record) -> Record:
    return {k: v for k, v in record.items() if v not in (None, "")}


class ToolBox:
    """Executes the agent's tools for one finding."""

    def __init__(
        self, sf: SalesforceClient, finding: Finding, policy: WritePolicy, source: str, model: str
    ) -> None:
        self._sf = sf
        self._finding = finding
        self._policy = policy
        self._source = source
        self._model = model
        self._scope: set[str] = set(finding.record_ids)
        self.accepted: ProposalDraft | None = None

    @property
    def definitions(self) -> list[dict[str, Any]]:
        """Tool schemas to send to the model."""
        return TOOL_DEFINITIONS

    def run(self, call: ToolCall) -> ToolResult:
        """Dispatch one tool call. Never raises: errors go back to the model as tool errors."""
        try:
            if call.name == "get_record":
                return self._get_record(call.input)
            if call.name == "get_related_records":
                return self._get_related(call.input)
            if call.name == "submit_proposal":
                return self._submit(call.input)
        except SalesforceError as exc:
            return ToolResult(f"Salesforce error: {exc}", is_error=True)
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(f"Invalid tool input: {exc!r}", is_error=True)
        return ToolResult(f"Unknown tool {call.name!r}", is_error=True)

    # -- reads -----------------------------------------------------------------------------
    def _get_record(self, args: dict[str, Any]) -> ToolResult:
        object_type, record_id = str(args["object_type"]), str(args["record_id"])
        if record_id not in self._scope:
            return ToolResult(
                f"{record_id} is outside this finding; you may only read its records "
                "or records returned by get_related_records.",
                is_error=True,
            )
        record = self._sf.get_record(object_type, record_id)
        if record is None:
            return ToolResult(
                f"{object_type} {record_id} no longer exists (merged or deleted).", True
            )
        return ToolResult(json.dumps(_compact(record), default=str))

    def _get_related(self, args: dict[str, Any]) -> ToolResult:
        object_type, record_id = str(args["object_type"]), str(args["record_id"])
        if record_id not in self._scope:
            return ToolResult(f"{record_id} is outside this finding.", is_error=True)
        if object_type == "Account":
            contacts = self._sf.query_records("Contact", where={"AccountId": record_id}, limit=25)
            self._scope.update(c["Id"] for c in contacts)
            keep = ("Id", "FirstName", "LastName", "Email", "Title", "Phone", "LastActivityDate",
                    "LastModifiedDate")  # fmt: skip
            payload: dict[str, Any] = {
                "contacts": [{k: c.get(k) for k in keep if c.get(k)} for c in contacts]
            }
        else:
            contact = self._sf.get_record("Contact", record_id)
            account_id = (contact or {}).get("AccountId")
            if not account_id:
                return ToolResult(json.dumps({"account": None, "colleagues": []}))
            account = self._sf.get_record("Account", account_id)
            siblings = self._sf.query_records("Contact", where={"AccountId": account_id}, limit=11)
            colleagues = [c for c in siblings if c["Id"] != record_id][:10]
            self._scope.update([account_id, *(c["Id"] for c in colleagues)])
            payload = {
                "account": _compact(account) if account else None,
                "colleagues": [
                    {
                        k: c.get(k)
                        for k in ("Id", "FirstName", "LastName", "Email", "Title")
                        if c.get(k)
                    }
                    for c in colleagues
                ],
            }
        return ToolResult(json.dumps(payload, default=str))

    # -- terminal tool ---------------------------------------------------------------------
    def _submit(self, args: dict[str, Any]) -> ToolResult:
        if self.accepted is not None:
            return ToolResult("A proposal was already accepted for this finding.", is_error=True)
        finding = self._finding
        try:
            action = ActionType(args["action"])
            changes: list[FieldChange] = []
            live: dict[str, Record] = {}
            for raw in args.get("changes") or []:
                rid = str(raw["record_id"])
                if rid not in finding.record_ids:
                    raise PolicyViolation(f"change targets {rid}, outside the finding")
                if rid not in live:
                    record = self._sf.get_record(finding.object_type, rid)
                    if record is None:
                        raise PolicyViolation(f"{rid} no longer exists")
                    live[rid] = record
                # "before" always comes from the live record, never from the model.
                changes.append(
                    FieldChange(
                        record_id=rid,
                        field=str(raw["field"]),
                        before=live[rid].get(str(raw["field"])),
                        after=raw["new_value"],
                    )
                )
            merge = None
            if action is ActionType.MERGE:
                survivor = str(args.get("survivor_id") or finding.record_ids[0])
                victims = args.get("victim_ids") or [r for r in finding.record_ids if r != survivor]
                merge = MergePlan(survivor_id=survivor, victim_ids=[str(v) for v in victims])
            draft = ProposalDraft(
                finding_id=finding.id,
                action=action,
                confidence=Confidence(args["confidence"]),
                explanation=str(args["explanation"]).strip(),
                changes=changes,
                merge=merge,
                review_note=(str(args["review_note"]).strip() or None)
                if args.get("review_note")
                else None,
                source=self._source,
                model=self._model,
            )
            self._policy.check_proposal(finding, draft)
        except ValidationError as exc:
            problems = "; ".join(e["msg"] for e in exc.errors())
            return ToolResult(
                f"Proposal rejected: {problems}. Fix it and call submit_proposal again.", True
            )
        except (PolicyViolation, KeyError, ValueError) as exc:
            return ToolResult(
                f"Proposal rejected: {exc}. Fix it and call submit_proposal again.", True
            )
        self.accepted = draft
        return ToolResult("Proposal recorded and queued for human review. You are done.")
