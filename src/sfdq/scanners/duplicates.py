"""Likely-duplicate scanner (fuzzy name + domain / email / phone matching with a confidence).

How a pair is scored
--------------------
Each signal yields an independent probability that the two records describe the same real-world
entity, and they are combined with *noisy-OR*: ``confidence = 1 - prod(1 - p_i)``. Weights live in
the YAML rules. Name similarity ramps from 0 at ``floor`` to ``max`` at a perfect match, so a
shared common name alone can never reach the reporting threshold; it takes corroboration.

Pairs above ``min_confidence`` become edges; connected components become one finding each, whose
confidence is the weakest edge in the cluster (a chain is only as trustworthy as its weakest link).

Scaling: candidate pairs come from blocking keys (shared domain / phone / email / name-token
prefix) rather than comparing all N^2 pairs.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from rapidfuzz import fuzz

from sfdq.models import Finding, FindingKind, ObjectType, Record
from sfdq.rules import DuplicateRules, NameWeight
from sfdq.scanners.base import record_label
from sfdq.util import is_blank, parse_sf_datetime, stable_id

_LEGAL_TOKENS = frozenset(
    [
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "plc",
        "lp",
        "llp",
        "sa",
        "ag",
        "bv",
    ]
)
_NICKNAMES = {
    "bob": "robert", "rob": "robert", "bobby": "robert", "bill": "william", "will": "william",
    "billy": "william", "liz": "elizabeth", "beth": "elizabeth", "mike": "michael",
    "jim": "james", "jimmy": "james", "kate": "katherine", "kathy": "katherine",
    "jen": "jennifer", "jenny": "jennifer", "tom": "thomas", "tommy": "thomas", "dan": "daniel",
    "danny": "daniel", "joe": "joseph", "matt": "matthew", "nick": "nicholas", "andy": "andrew",
    "sue": "susan", "tony": "anthony", "steve": "steven", "dave": "david",
}  # fmt: skip
# Domains that say nothing about identity.
_GENERIC_DOMAINS = frozenset(
    [
        "facebook.com",
        "linkedin.com",
        "twitter.com",
        "x.com",
        "instagram.com",
        "google.com",
        "gmail.com",
        "yahoo.com",
        "outlook.com",
        "hotmail.com",
    ]
)
_KEY_FIELDS = {
    "Account": ("Name", "Website", "Phone", "Industry", "BillingCity", "BillingPostalCode",
                "BillingCountry", "Description"),
    "Contact": ("FirstName", "LastName", "Email", "Phone", "MobilePhone", "Title", "AccountId",
                "MailingPostalCode"),
}  # fmt: skip


# --------------------------------------------------------------------------------------------
# Normalizers (pure, individually unit-tested)
# --------------------------------------------------------------------------------------------
def normalize_company_name(name: str | None) -> str:
    """Lowercase, drop punctuation, a leading 'the' and legal suffixes ('Inc', 'LLC', ...)."""
    if not name:
        return ""
    text = name.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", "", text)
    tokens = text.split()
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    tokens = [t for t in tokens if t not in _LEGAL_TOKENS]
    return " ".join(tokens)


def normalize_person_name(first: str | None, last: str | None) -> str:
    """Lowercase ``first last`` with nicknames mapped to formal names (Bob -> robert)."""
    first_clean = re.sub(r"[^a-z\s]", "", (first or "").lower()).strip()
    last_clean = re.sub(r"[^a-z\s'-]", "", (last or "").lower()).strip()
    first_token = first_clean.split()[0] if first_clean else ""
    first_token = _NICKNAMES.get(first_token, first_token)
    return f"{first_token} {last_clean}".strip()


def website_domain(website: str | None) -> str | None:
    """Extract the bare host from a website value (``https://www.Acme.com/x`` -> ``acme.com``)."""
    if is_blank(website):
        return None
    host = re.sub(r"^[a-z]+://", "", str(website).strip().lower())
    host = host.split("/")[0].split("?")[0].split(":")[0]
    host = re.sub(r"^www\d?\.", "", host)
    if "." not in host or host in _GENERIC_DOMAINS:
        return None
    return host


def phone_key(phone: str | None) -> str | None:
    """Last ten digits of a phone number (ignores country code, punctuation, extensions)."""
    if is_blank(phone):
        return None
    base = re.split(r"(?i)\s*(?:x|ext)", str(phone))[0]
    digits = re.sub(r"\D", "", base)
    if len(digits) < 10 or len(set(digits)) == 1:
        return None
    return digits[-10:]


def email_key(email: str | None) -> str | None:
    """Lowercased, trimmed email, or ``None`` if it cannot be an address."""
    if is_blank(email):
        return None
    value = str(email).strip().lower()
    return value if "@" in value else None


# --------------------------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------------------------
def _ramp(similarity: float, weight: NameWeight) -> float:
    if similarity <= weight.floor:
        return 0.0
    return weight.max * min(1.0, (similarity - weight.floor) / (1.0 - weight.floor))


def noisy_or(probabilities: Sequence[float]) -> float:
    """Combine independent evidence: ``1 - prod(1 - p)``."""
    remaining = 1.0
    for p in probabilities:
        remaining *= 1.0 - p
    return 1.0 - remaining


@dataclass
class _Profile:
    record: Record
    name: str
    domain: str | None = None
    phones: frozenset[str] = frozenset()
    postal: str | None = None
    emails: frozenset[str] = frozenset()
    account_id: str | None = None


@dataclass
class _Edge:
    a: int
    b: int
    confidence: float
    evidence: list[str] = field(default_factory=list)
    signals: dict[str, float] = field(default_factory=dict)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)


class DuplicateScanner:
    """Finds clusters of likely-duplicate Accounts or Contacts."""

    name = "duplicate"

    def __init__(self, rules: DuplicateRules) -> None:
        self._rules = rules

    # -- public ----------------------------------------------------------------------------
    def scan(self, object_type: ObjectType, records: Sequence[Record]) -> list[Finding]:
        """Return one finding per cluster of likely duplicates."""
        profiles = [self._profile(object_type, r) for r in records]
        edges = self._score_candidates(object_type, profiles)
        edges = [e for e in edges if e.confidence >= self._rules.min_confidence]
        if not edges:
            return []

        uf = _UnionFind(len(profiles))
        for e in edges:
            uf.union(e.a, e.b)
        clusters: dict[int, list[int]] = defaultdict(list)
        for idx in sorted({i for e in edges for i in (e.a, e.b)}):
            clusters[uf.find(idx)].append(idx)

        findings = [
            self._finding(
                object_type, profiles, members, [e for e in edges if uf.find(e.a) == root]
            )
            for root, members in clusters.items()
        ]
        return sorted(findings, key=lambda f: (-(f.score or 0.0), f.id))

    # -- profiles --------------------------------------------------------------------------
    def _profile(self, object_type: str, record: Record) -> _Profile:
        if object_type == "Account":
            phone = phone_key(record.get("Phone"))
            return _Profile(
                record=record,
                name=normalize_company_name(record.get("Name")),
                domain=website_domain(record.get("Website")),
                phones=frozenset([phone] if phone else []),
                postal=(str(record["BillingPostalCode"]).strip().upper().replace(" ", "") or None)
                if not is_blank(record.get("BillingPostalCode"))
                else None,
            )
        phones = {phone_key(record.get("Phone")), phone_key(record.get("MobilePhone"))}
        email = email_key(record.get("Email"))
        return _Profile(
            record=record,
            name=normalize_person_name(record.get("FirstName"), record.get("LastName")),
            phones=frozenset(p for p in phones if p),
            emails=frozenset([email] if email else []),
            account_id=record.get("AccountId") or None,
        )

    # -- candidate generation --------------------------------------------------------------
    def _blocks(self, object_type: str, profiles: list[_Profile]) -> list[list[int]]:
        strong: dict[str, list[int]] = defaultdict(list)
        weak: dict[str, list[int]] = defaultdict(list)
        for i, p in enumerate(profiles):
            if p.domain:
                strong[f"d:{p.domain}"].append(i)
            for ph in p.phones:
                strong[f"p:{ph}"].append(i)
            for em in p.emails:
                strong[f"e:{em}"].append(i)
            if object_type == "Account":
                for token in p.name.split():
                    if len(token) >= 3:
                        weak[f"t:{token[:4]}"].append(i)
            elif p.name:
                last = p.name.split()[-1]
                weak[f"l:{last[:4]}"].append(i)
        blocks = list(strong.values())
        blocks += [b for b in weak.values() if len(b) <= self._rules.max_block_size]
        return [b for b in blocks if len(b) > 1]

    def _score_candidates(self, object_type: str, profiles: list[_Profile]) -> list[_Edge]:
        seen: set[tuple[int, int]] = set()
        edges: list[_Edge] = []
        for block in self._blocks(object_type, profiles):
            for a, b in combinations(sorted(block), 2):
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                edge = (
                    self._score_accounts(a, b, profiles)
                    if object_type == "Account"
                    else self._score_contacts(a, b, profiles)
                )
                if edge is not None:
                    edges.append(edge)
        return edges

    # -- pair scoring ----------------------------------------------------------------------
    def _score_accounts(self, a: int, b: int, profiles: list[_Profile]) -> _Edge | None:
        w = self._rules.Account
        pa, pb = profiles[a], profiles[b]
        similarity = fuzz.token_sort_ratio(pa.name, pb.name) / 100 if pa.name and pb.name else 0.0
        probs: dict[str, float] = {}
        evidence: list[str] = []
        if (p := _ramp(similarity, w.name)) > 0:
            probs["name"] = p
            evidence.append(
                f"names are {similarity:.0%} similar after ignoring legal suffixes "
                f"('{pa.record.get('Name')}' vs '{pb.record.get('Name')}')"
            )
        if pa.domain and pa.domain == pb.domain:
            probs["domain"] = w.domain
            evidence.append(f"same website domain ({pa.domain})")
        if pa.phones & pb.phones:
            probs["phone"] = w.phone
            evidence.append(f"same phone number (ending {sorted(pa.phones & pb.phones)[0][-4:]})")
        if not probs:
            return None
        if pa.postal and pa.postal == pb.postal:
            probs["postal"] = w.postal
            evidence.append(f"same postal code ({pa.record.get('BillingPostalCode')})")
        return _Edge(a, b, noisy_or(list(probs.values())), evidence, probs)

    def _score_contacts(self, a: int, b: int, profiles: list[_Profile]) -> _Edge | None:
        w = self._rules.Contact
        pa, pb = profiles[a], profiles[b]
        similarity = fuzz.ratio(pa.name, pb.name) / 100 if pa.name and pb.name else 0.0
        probs: dict[str, float] = {}
        evidence: list[str] = []
        if pa.emails & pb.emails:
            probs["email"] = w.email
            evidence.append(f"same email address ({sorted(pa.emails & pb.emails)[0]})")
        if (p := _ramp(similarity, w.name)) > 0:
            probs["name"] = p
            label_a, label_b = (record_label("Contact", p.record) for p in (pa, pb))
            evidence.append(f"names are {similarity:.0%} similar ('{label_a}' vs '{label_b}')")
            if pa.account_id and pa.account_id == pb.account_id:
                probs["same_account"] = w.same_account
                evidence.append("both belong to the same Account")
        if pa.phones & pb.phones:
            probs["phone"] = w.phone
            evidence.append(f"same phone number (ending {sorted(pa.phones & pb.phones)[0][-4:]})")
        if not probs:
            return None
        return _Edge(a, b, noisy_or(list(probs.values())), evidence, probs)

    # -- output ----------------------------------------------------------------------------
    def _finding(
        self,
        object_type: ObjectType,
        profiles: list[_Profile],
        members: list[int],
        edges: list[_Edge],
    ) -> Finding:
        records = [profiles[i].record for i in members]
        survivor, reason = suggest_survivor(object_type, records)
        ordered = [survivor["Id"], *sorted(r["Id"] for r in records if r["Id"] != survivor["Id"])]
        confidence = round(min(e.confidence for e in edges), 3)
        labels = ", ".join(f"'{record_label(object_type, r)}'" for r in records)
        return Finding(
            id=stable_id("duplicate", object_type, *sorted(ordered)),
            kind=FindingKind.DUPLICATE,
            object_type=object_type,
            record_ids=ordered,
            summary=(
                f"{len(records)} {object_type} records look like duplicates "
                f"({confidence:.0%} confidence): {labels}"
            ),
            score=confidence,
            details={
                "pairs": [
                    {
                        "a": profiles[e.a].record["Id"],
                        "b": profiles[e.b].record["Id"],
                        "confidence": round(e.confidence, 3),
                        "evidence": e.evidence,
                        "signals": {k: round(v, 3) for k, v in e.signals.items()},
                    }
                    for e in sorted(edges, key=lambda e: -e.confidence)
                ],
                "suggested_survivor": survivor["Id"],
                "survivor_reason": reason,
            },
            snapshot={r["Id"]: r for r in records},
        )


def completeness(object_type: str, record: Record) -> int:
    """Number of key fields that are populated (used to pick the better record to keep)."""
    return sum(not is_blank(record.get(f)) for f in _KEY_FIELDS[object_type])


def suggest_survivor(object_type: str, records: Sequence[Record]) -> tuple[Record, str]:
    """Pick the record to keep: most complete, then most recently touched, then lowest Id."""

    def recency(r: Record) -> float:
        stamps: list[Any] = [
            parse_sf_datetime(r.get("LastActivityDate")),
            parse_sf_datetime(r.get("LastModifiedDate")),
        ]
        valid = [s.timestamp() for s in stamps if s is not None]
        return max(valid) if valid else 0.0

    ranked = sorted(records, key=lambda r: (-completeness(object_type, r), -recency(r), r["Id"]))
    best = ranked[0]
    reason = (
        f"most complete record ({completeness(object_type, best)}/{len(_KEY_FIELDS[object_type])} "
        "key fields populated), then most recently active"
    )
    return best, reason
