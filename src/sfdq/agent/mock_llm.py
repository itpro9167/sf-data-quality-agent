"""A mock LLM that returns canned-but-realistic responses, with no network and no API key.

It is *not* a shortcut around the agent: it speaks the same tool-use protocol as Claude. On the
first turn it asks for the live records (``get_record`` / ``get_related_records``); on the second
it reads those tool results and calls ``submit_proposal`` with an explanation written from
templates and a proposal derived from the evidence. So the real agent loop, tool validation and
write policy all execute in mock mode.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from sfdq.agent.llm import LLMTurn, ToolCall
from sfdq.scanners.formats import check_value

_FINDING_RE = re.compile(r"<finding>\n(.*?)\n</finding>", re.DOTALL)
_LEADING_ZERO_STATES = {"MA", "NJ", "CT", "NH", "RI", "VT", "ME", "PR"}
_FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com"}


_FORMAT_IMPACT = {
    "email": "Emails to an invalid address bounce, which hurts sender reputation and wastes rep time.",
    "phone": "Reps can't use click-to-call or dialer campaigns with a number like this.",
    "postal_code": "Bad postal codes break address validation, territory routing and mailings.",
}


def _label(field: str) -> str:
    """BillingPostalCode -> 'billing postal code'."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", field).lower()


def _fmt_kind(field: str) -> str | None:
    if "Email" in field:
        return "email"
    if "Phone" in field:
        return "phone"
    if "PostalCode" in field:
        return "postal_code"
    return None


def _name(object_type: str, record: dict[str, Any]) -> str:
    if object_type == "Contact":
        return f"{record.get('FirstName') or ''} {record.get('LastName') or ''}".strip()
    return str(record.get("Name") or record.get("Id"))


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


class MockLLMClient:
    """Deterministic stand-in for Claude. Implements :class:`sfdq.agent.llm.LLMClient`."""

    name = "mock"

    def __init__(self, model: str = "mock-claude") -> None:
        self.model = model

    # -- LLMClient -------------------------------------------------------------------------
    def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMTurn:
        """Turn 1: look at the records. Turn 2: submit a proposal. Anything later: finish."""
        finding = self._finding_from(messages)
        assistant_turns = sum(1 for m in messages if m["role"] == "assistant")
        if assistant_turns == 0:
            return self._look(finding)
        if assistant_turns == 1:
            return self._decide(finding, self._tool_results(messages))
        return self._turn("Proposal submitted.", [], "end_turn")

    # -- turn builders ---------------------------------------------------------------------
    def _turn(self, text: str, calls: list[ToolCall], stop: str) -> LLMTurn:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        content += [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls
        ]
        return LLMTurn(
            content=content,
            text=text,
            tool_calls=calls,
            stop_reason=stop,
            input_tokens=900 + 250 * len(calls),
            output_tokens=60 + 90 * len(calls),
        )

    def _look(self, f: dict[str, Any]) -> LLMTurn:
        obj, ids, kind = f["object_type"], f["record_ids"], f["kind"]
        calls = [
            ToolCall(f"toolu_mock_{i}", "get_record", {"object_type": obj, "record_id": rid})
            for i, rid in enumerate(ids[:3])
        ]
        if (
            kind == "missing_fields" and obj == "Account" and "Website" in f["details"]["missing"]
        ) or (kind == "stale" and obj == "Account"):
            calls.append(
                ToolCall(
                    "toolu_mock_rel",
                    "get_related_records",
                    {"object_type": obj, "record_id": ids[0]},
                )
            )
        return self._turn(
            "I'll start by checking the live records for this finding.", calls, "tool_use"
        )

    def _decide(self, f: dict[str, Any], results: dict[str, Any]) -> LLMTurn:
        records: dict[str, dict[str, Any]] = results["records"]
        for rid, snap in f["records"].items():  # fall back to the scanner snapshot
            records.setdefault(rid, snap)
        handler = {
            "duplicate": self._duplicate,
            "invalid_format": self._invalid_format,
            "missing_fields": self._missing,
            "stale": self._stale,
        }[f["kind"]]
        proposal = handler(f, records, results.get("related"))
        call = ToolCall("toolu_mock_submit", "submit_proposal", proposal)
        return self._turn("Here is my proposal.", [call], "tool_use")

    # -- reading what the loop sent us -----------------------------------------------------
    @staticmethod
    def _finding_from(messages: list[dict[str, Any]]) -> dict[str, Any]:
        match = _FINDING_RE.search(str(messages[0]["content"]))
        if not match:
            raise ValueError("mock LLM could not find a <finding> block in the first message")
        finding: dict[str, Any] = json.loads(match.group(1))
        return finding

    @staticmethod
    def _tool_results(messages: list[dict[str, Any]]) -> dict[str, Any]:
        records: dict[str, dict[str, Any]] = {}
        related: dict[str, Any] | None = None
        for m in messages:
            if m["role"] != "user" or not isinstance(m["content"], list):
                continue
            for block in m["content"]:
                if block.get("type") != "tool_result" or block.get("is_error"):
                    continue
                data = json.loads(block["content"])
                if "Id" in data:
                    records[data["Id"]] = data
                else:
                    related = data
        return {"records": records, "related": related}

    # -- per-kind reasoning ----------------------------------------------------------------
    def _duplicate(self, f: dict[str, Any], records: dict[str, Any], _: Any) -> dict[str, Any]:
        obj, score = f["object_type"], f["confidence_score"] or 0.0
        survivor = f["details"]["suggested_survivor"]
        victims = [r for r in f["record_ids"] if r != survivor]
        evidence: list[str] = []
        for pair in f["details"]["pairs"]:
            for e in pair["evidence"]:
                if e not in evidence:
                    evidence.append(e)
        noun = "company" if obj == "Account" else "person"
        names = _join([f"'{_name(obj, records[r])}'" for r in f["record_ids"]])

        if score < 0.75:
            return {
                "action": "flag_for_review",
                "confidence": "low",
                "explanation": (
                    f"{names} might be the same {noun}, but the evidence is thin: "
                    f"{_join(evidence[:2])}. Merging by mistake would combine two customers' "
                    "history, so this is better checked by the owner than merged automatically."
                ),
                "review_note": (
                    f"Possible duplicate {obj}s ({names}). Please confirm whether they are the same "
                    f"{noun}; if so, tell data-quality to merge them."
                ),
            }

        changes = self._fill_blanks(f, records, survivor, victims)
        fill = ""
        if changes:
            filled = _join([_label(c["field"]) for c in changes])
            fill = f" The {filled} missing on the kept record will be copied over from the duplicate first."
        impact = (
            "Reps currently see split contact lists and activity history, and the same company can be "
            "pitched twice."
            if obj == "Account"
            else "The same person can receive duplicate outreach and their email history is split "
            "across records."
        )
        confidence = "high" if score >= 0.9 else "medium"
        return {
            "action": "merge",
            "confidence": confidence,
            "explanation": (
                f"These {len(records)} {obj} records look like the same {noun}: {_join(evidence[:3])}. "
                f"{impact} I suggest keeping '{_name(obj, records[survivor])}' (the most complete "
                f"record) and merging the other{'s' if len(victims) > 1 else ''} into it.{fill}"
            ),
            "survivor_id": survivor,
            "victim_ids": victims,
            "changes": changes,
        }

    @staticmethod
    def _fill_blanks(
        f: dict[str, Any], records: dict[str, Any], survivor: str, victims: list[str]
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for field in f["writable_fields"]:
            if records[survivor].get(field):
                continue
            for v in victims:
                value = records[v].get(field)
                kind = _fmt_kind(field)
                if value and (kind is None or check_value(kind, str(value), None).valid):
                    out.append({"record_id": survivor, "field": field, "new_value": value})
                    break
        return out

    def _invalid_format(self, f: dict[str, Any], records: dict[str, Any], _: Any) -> dict[str, Any]:
        d, obj = f["details"], f["object_type"]
        record = records[f["record_ids"][0]]
        name, field, value = _name(obj, record), d["field"], d["value"]
        pretty = _label(field)
        base = f"The {pretty} stored for '{name}' is {value!r}, which isn't a valid {d['format'].replace('_', ' ')} ({d['problem']})."

        fix, why, confidence = d.get("suggestion"), "", "high"
        if fix:
            why = "This is a mechanical correction, so it is safe to approve at a glance."
        elif d["format"] == "postal_code" and d.get("hint"):
            state = record.get("BillingState") or record.get("MailingState")
            if state in _LEADING_ZERO_STATES and re.fullmatch(r"\d{4}", str(value)):
                fix, confidence = f"0{value}", "medium"
                why = (
                    f"{state} ZIP codes begin with 0, and spreadsheet imports often drop it, so I "
                    "restored it. Worth a quick look."
                )
        elif d["format"] == "phone":
            candidate = str(value).translate(str.maketrans("OolI", "0011"))
            if candidate != value and check_value("phone", candidate, None).valid:
                fix, confidence = candidate, "medium"
                why = "It looks like letters were typed in place of digits (O for 0, l for 1)."

        if fix:
            return {
                "action": "update_field",
                "confidence": confidence,
                "explanation": (
                    f"{base} {_FORMAT_IMPACT[d['format']]} I propose changing it to {fix!r}. {why}"
                ),
                "changes": [{"record_id": record["Id"], "field": field, "new_value": fix}],
            }
        asker = "customer" if obj == "Account" else "contact"
        return {
            "action": "flag_for_review",
            "confidence": "high",
            "explanation": f"{base} I can't tell what the correct value should be from the data in "
            f"Salesforce, so I won't guess. The record owner can confirm it with the {asker}.",
            "review_note": (
                f"Please confirm the correct {pretty} for {name}. Current value: {value!r}."
            ),
        }

    def _missing(self, f: dict[str, Any], records: dict[str, Any], related: Any) -> dict[str, Any]:
        obj, missing = f["object_type"], f["details"]["missing"]
        record = records[f["record_ids"][0]]
        name = _name(obj, record)
        labels = _join([_label(m) for m in missing])
        gaps = f"'{name}' is missing {labels}."

        if obj == "Account" and "Website" in missing and related:
            domains = Counter(
                c["Email"].split("@")[1].lower()
                for c in related.get("contacts", [])
                if "@" in c.get("Email", "") and c["Email"].split("@")[1].lower() not in _FREE_MAIL
            )
            if domains:
                domain, votes = domains.most_common(1)[0]
                others = [_label(m) for m in missing if m != "Website"]
                tail = f" The {_join(others)} will still need the owner's input." if others else ""
                return {
                    "action": "update_field",
                    "confidence": "medium" if votes >= 2 else "low",
                    "explanation": (
                        f"{gaps} Without a website, enrichment and territory tools skip this "
                        "account. "
                        f"{votes} of its contacts use @{domain} email addresses, so I propose "
                        f"setting the website to https://{domain}.{tail}"
                    ),
                    "changes": [
                        {
                            "record_id": record["Id"],
                            "field": "Website",
                            "new_value": f"https://{domain}",
                        }
                    ],
                }
        impact = (
            "Incomplete accounts drop out of segmentation, territory routing and reporting."
            if obj == "Account"
            else "Incomplete contacts can't be emailed, called or tied to the right account."
        )
        return {
            "action": "flag_for_review",
            "confidence": "high",
            "explanation": f"{gaps} {impact} Nothing in Salesforce lets me work out the missing "
            "values reliably, so I'm asking the record owner to fill them in rather than guessing.",
            "review_note": (
                f"Please complete these required fields on {name}: {', '.join(missing)}."
            ),
        }

    def _stale(self, f: dict[str, Any], records: dict[str, Any], related: Any) -> dict[str, Any]:
        d, obj = f["details"], f["object_type"]
        record = records[f["record_ids"][0]]
        name = _name(obj, record)
        months = d["months_idle"]
        active = 0
        if related:
            active = sum(
                1
                for c in related.get("contacts", [])
                if str(c.get("LastActivityDate") or "")[:10] >= d["cutoff"]
            )
        extra = ""
        confidence = "medium"
        if active:
            extra = (
                f" Note that {active} of its contacts "
                f"{'has' if active == 1 else 'have'} had recent activity, so this account may "
                "still be live even though the account record itself is untouched."
            )
            confidence = "low"
        return {
            "action": "flag_for_review",
            "confidence": confidence,
            "explanation": (
                f"'{name}' hasn't had any activity or edits for about {months:g} months. Stale "
                "records inflate pipeline and mailing-list numbers and send reps after "
                f"customers who may have moved on.{extra} I won't archive anything automatically; "
                "I'm asking the owner to confirm."
            ),
            "review_note": (
                f"{name} has been inactive for ~{months:g} months. Is this {obj.lower()} still "
                "active? If not, please mark it inactive; if yes, log a quick touchpoint."
            ),
        }
