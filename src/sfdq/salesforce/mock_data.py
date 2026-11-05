"""Deterministic generator for a realistic fake Salesforce org with *seeded* data problems.

The generator builds a clean org first and then injects a known set of problems (duplicates,
gaps, stale records, bad formats). Every injected problem is written to a :class:`SeededProblems`
manifest, which the test-suite uses as ground truth to score each scanner's precision and recall.

Everything is driven by ``random.Random(seed)`` and lists/dicts only (never set iteration), so the
same seed always yields byte-identical output.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sfdq.models import Record
from sfdq.util import format_sf_datetime

# --------------------------------------------------------------------------------------------
# Word lists
# --------------------------------------------------------------------------------------------
PREFIXES = [
    "Bluepeak", "Northwind", "Ironwood", "Brightwave", "Lakeside", "Redfern", "Silverline",
    "Harborview", "Pinecrest", "Summit", "Evergreen", "Falcon", "Granite", "Horizon", "Juniper",
    "Keystone", "Lumen", "Meridian", "Nimbus", "Orchard", "Paragon", "Quantum", "Riverbend",
    "Sterling", "Trident", "Union", "Vertex", "Willow", "Zenith", "Apex", "Beacon", "Cascade",
    "Delta", "Ember", "Foxglove", "Glacier", "Highland", "Iris", "Jasper", "Kestrel", "Longview",
    "Maple", "Nova", "Oakmont", "Primrose", "Quarry", "Redwood", "Sagebrush", "Tidewater",
    "Upland", "Vista", "Westgate", "Yarrow", "Aspen", "Birch", "Cobalt", "Driftwood", "Elmwood",
    "Fairview", "Goldleaf",
]  # fmt: skip

CORE_INDUSTRY = {
    "Logistics": "Transportation", "Software": "Technology", "Foods": "Food & Beverage",
    "Energy": "Energy", "Dental": "Healthcare", "Partners": "Consulting", "Systems": "Technology",
    "Analytics": "Technology", "Robotics": "Manufacturing", "Capital": "Finance",
    "Health": "Healthcare", "Media": "Media", "Labs": "Biotechnology", "Consulting": "Consulting",
    "Networks": "Telecommunications", "Materials": "Manufacturing", "Supply": "Wholesale",
    "Freight": "Transportation", "Insurance": "Insurance", "Security": "Technology",
    "Realty": "Real Estate", "Biotech": "Biotechnology", "Aerospace": "Manufacturing",
    "Marine": "Transportation", "Textiles": "Manufacturing", "Pharma": "Healthcare",
    "Education": "Education", "Automotive": "Manufacturing", "Hospitality": "Hospitality",
    "Construction": "Construction", "Telecom": "Telecommunications", "Brewing": "Food & Beverage",
    "Solar": "Energy", "Payments": "Finance", "Studios": "Media", "Wellness": "Healthcare",
}  # fmt: skip
CORES = list(CORE_INDUSTRY)
REBRAND_CORES = ["Digital", "Ventures", "Holdings", "Interactive", "Global"]
LEGAL_SUFFIXES = ["Inc", "LLC", "Corp", "Ltd", "Co"]
LEGAL_VARIANT = {"Inc": "Incorporated", "LLC": "L.L.C.", "Corp": "Corporation", "Ltd": "Limited",
                 "Co": "Company"}  # fmt: skip

# (city, state, postal, country, area code / dial prefix)
US_CITIES = [
    ("San Francisco", "CA", "94105", "415"), ("Oakland", "CA", "94607", "510"),
    ("Los Angeles", "CA", "90012", "213"), ("San Diego", "CA", "92101", "619"),
    ("Seattle", "WA", "98101", "206"), ("Portland", "OR", "97204", "503"),
    ("Denver", "CO", "80202", "303"), ("Austin", "TX", "78701", "512"),
    ("Dallas", "TX", "75201", "214"), ("Houston", "TX", "77002", "713"),
    ("Chicago", "IL", "60601", "312"), ("Minneapolis", "MN", "55401", "612"),
    ("Detroit", "MI", "48226", "313"), ("Atlanta", "GA", "30303", "404"),
    ("Miami", "FL", "33131", "305"), ("Nashville", "TN", "37203", "615"),
    ("Charlotte", "NC", "28202", "704"), ("Washington", "DC", "20001", "202"),
    ("Philadelphia", "PA", "19103", "215"), ("New York", "NY", "10001", "212"),
    ("Boston", "MA", "02108", "617"), ("Cambridge", "MA", "02139", "617"),
    ("Newark", "NJ", "07102", "973"), ("Hartford", "CT", "06103", "860"),
    ("Phoenix", "AZ", "85004", "602"), ("Salt Lake City", "UT", "84101", "801"),
]  # fmt: skip
CA_CITIES = [("Toronto", "ON", "M5V 2T6", "416"), ("Vancouver", "BC", "V6B 4N9", "604"),
             ("Ottawa", "ON", "K1A 0B1", "613")]  # fmt: skip
UK_CITIES = [("London", "", "EC1A 1BB", ""), ("Manchester", "", "M1 1AE", ""),
             ("Bristol", "", "BS1 4DJ", "")]  # fmt: skip

# Formal first name -> nickname, used to create "Bob" vs "Robert" duplicates.
NICK_PAIRS = [
    ("Robert", "Bob"), ("William", "Bill"), ("Elizabeth", "Liz"), ("Michael", "Mike"),
    ("James", "Jim"), ("Katherine", "Kate"), ("Jennifer", "Jen"), ("Thomas", "Tom"),
    ("Daniel", "Dan"), ("Joseph", "Joe"), ("Matthew", "Matt"), ("Nicholas", "Nick"),
    ("Andrew", "Andy"), ("Susan", "Sue"), ("Anthony", "Tony"), ("Steven", "Steve"),
    ("David", "Dave"),
]  # fmt: skip
OTHER_FIRST = [
    "Maria", "Priya", "Wei", "Aisha", "Carlos", "Olivia", "Liam", "Noah", "Emma", "Sophia",
    "Lucas", "Mia", "Ethan", "Ava", "Grace", "Henry", "Isabel", "Jack", "Laura", "Omar",
    "Rachel", "Sam", "Tara", "Victor", "Zoe", "Hannah", "Diego", "Yuki", "Fatima", "Ingrid",
]  # fmt: skip
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez",
    "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Taylor", "Moore",
    "Jackson", "Martin", "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
    "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King", "Wright", "Scott",
    "Torres", "Nguyen", "Hill", "Flores", "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera",
    "Campbell", "Mitchell", "Carter", "Roberts", "Chen", "Patel", "Kim", "Singh", "Okafor",
    "Novak", "Larsen", "Fischer", "Rossi", "Tanaka", "Bennett",
]  # fmt: skip
TITLES = [
    "VP of Sales", "Director of Operations", "Procurement Manager", "IT Manager", "CFO",
    "Head of Marketing", "Account Executive", "Operations Analyst", "CEO", "Office Manager",
    "Controller", "Customer Success Manager", "Product Manager", "HR Director", "Legal Counsel",
    "Supply Chain Lead",
]  # fmt: skip
DESCRIPTIONS = [
    "Met at the regional trade show; interested in the enterprise tier.",
    "Long-standing customer, renews every January.",
    "Referred by an existing partner. Prefers email over phone.",
    "Expanding into a second office next year.",
    "Procurement requires three quotes for any purchase over $25k.",
]
OWNER_NAMES = ["Dana Whitfield", "Marcus Lee", "Priya Raman", "Tom Becker", "Sofia Alvarez"]

LEADING_ZERO_STATES = ("MA", "NJ", "CT")
_FORMAT_FIELDS = ("Phone", "MobilePhone", "Email", "BillingPostalCode", "MailingPostalCode")
_MISSING_FIELDS = ("Industry", "Phone", "Website", "BillingCountry", "Email", "AccountId", "Title")


# --------------------------------------------------------------------------------------------
# Output containers
# --------------------------------------------------------------------------------------------
@dataclass
class SeededProblems:
    """Ground truth: exactly which problems the generator injected."""

    duplicate_clusters: dict[str, list[list[str]]] = field(default_factory=dict)
    decoys: dict[str, list[list[str]]] = field(default_factory=dict)  # similar but NOT duplicates
    missing_fields: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    stale: dict[str, list[str]] = field(default_factory=dict)
    invalid_formats: dict[str, list[list[str]]] = field(default_factory=dict)  # [id, field]
    # "<record id>:<field>" -> the CORRECT value before it was corrupted/blanked. Ground truth for
    # the eval harness (what a careful analyst would restore), independent of any scanner/agent.
    truth: dict[str, Any] = field(default_factory=dict)


@dataclass
class MockOrgData:
    """A generated org: records per object plus the seeded-problem manifest."""

    seed: int
    as_of: str
    records: dict[str, list[Record]]
    seeded: SeededProblems

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "seed": self.seed,
            "as_of": self.as_of,
            "records": self.records,
            "seeded": asdict(self.seeded),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MockOrgData:
        """Inverse of :meth:`to_dict`."""
        return cls(
            seed=data["seed"],
            as_of=data["as_of"],
            records=data["records"],
            seeded=SeededProblems(**data["seeded"]),
        )


# --------------------------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------------------------
class _Builder:
    """Stateful helper that holds the RNG and the org being built."""

    def __init__(self, seed: int, as_of: date) -> None:
        self.rng = random.Random(seed)
        self.seed = seed
        self.now = datetime(as_of.year, as_of.month, as_of.day, 12, 0, tzinfo=UTC)
        self._counters: dict[str, int] = {}
        self.seeded = SeededProblems()
        self.users = [self._new_id("005") for _ in OWNER_NAMES]
        self.accounts: list[Record] = []
        self.contacts: list[Record] = []
        self._account_by_id: dict[str, Record] = {}
        self._used_account_names: set[str] = set()
        self._used_contact_keys: set[tuple[str, str]] = set()
        self._used_phones: set[str] = set()
        self.dup_account_ids: set[str] = set()  # every member of a duplicate cluster
        self.dup_extra_account_ids: set[str] = set()  # members other than the original
        self.dup_contact_ids: set[str] = set()
        self._snap: dict[tuple[str, str], Any] = {}  # pristine values, taken before corruption

    # -- primitives ------------------------------------------------------------------------
    def _new_id(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = n
        return f"{prefix}Dq{n:010d}AAA"  # 3 + 2 + 10 + 3 = 18 chars, like a real Salesforce Id

    def _dt(self, days_ago: int) -> str:
        return format_sf_datetime(self.now - timedelta(days=days_ago, hours=self.rng.randint(0, 9)))

    def _date(self, days_ago: int) -> str:
        return (self.now - timedelta(days=days_ago)).date().isoformat()

    def _phone(self, area: str, country: str, style: int | None = None) -> str:
        """Return a unique, valid-looking phone number in one of several common formats."""
        rng = self.rng
        while True:
            if country == "United Kingdom":
                number = f"+44 20 7946 0{rng.randint(0, 999):03d}"  # Ofcom drama range
                style_pick = 0
            else:
                line = f"{rng.randint(0, 9999):04d}"
                style_pick = rng.randint(0, 3) if style is None else style
                number = [
                    f"({area}) 555-{line}",
                    f"{area}-555-{line}",
                    f"{area}.555.{line}",
                    f"+1 {area} 555 {line}",
                ][style_pick]
            digits = "".join(ch for ch in number if ch.isdigit())[-10:]
            if digits not in self._used_phones:
                self._used_phones.add(digits)
                return number

    # -- accounts --------------------------------------------------------------------------
    def build_accounts(self, count: int, decoy_pairs: int) -> None:
        for i in range(count):
            reuse = i > 0 and i <= decoy_pairs * 2 and i % 2 == 1
            prev = self.accounts[-1] if reuse else None
            self.accounts.append(self._new_account(prev))
        for a in self.accounts:
            self._account_by_id[a["Id"]] = a
        # Decoys: same prefix + same city, different line of business, different domain.
        pairs = []
        for i in range(1, decoy_pairs * 2, 2):
            pairs.append([self.accounts[i - 1]["Id"], self.accounts[i]["Id"]])
        self.seeded.decoys["Account"] = pairs

    def _new_account(self, near: Record | None) -> Record:
        rng = self.rng
        while True:
            prefix = near["_prefix"] if near else rng.choice(PREFIXES)
            core = rng.choice(CORES)
            suffix = rng.choice(LEGAL_SUFFIXES)
            base = f"{prefix} {core}"
            if base.lower() not in self._used_account_names:
                break
        self._used_account_names.add(base.lower())
        country_roll = rng.random()
        if near:
            city = (near["BillingCity"], near["BillingState"], near["_postal"], near["_area"])
            country = near["BillingCountry"]
        elif country_roll < 0.08:
            city, country = rng.choice(CA_CITIES), "Canada"
        elif country_roll < 0.13:
            city, country = rng.choice(UK_CITIES), "United Kingdom"
        else:
            city, country = rng.choice(US_CITIES), "United States"
        domain = f"{prefix}{core}".lower() + rng.choice([".com", ".com", ".com", ".io", ".net"])
        website = rng.choice(
            [f"https://www.{domain}", f"https://{domain}", f"www.{domain}", domain]
        )
        mod_days = rng.randint(3, 320)
        act_days = rng.randint(1, 320)
        return {
            "Id": self._new_id("001"),
            "Name": f"{base} {suffix}",
            "Website": website,
            "Phone": self._phone(city[3], country),
            "Industry": CORE_INDUSTRY[core],
            "BillingCity": city[0],
            "BillingState": city[1],
            "BillingPostalCode": city[2],
            "BillingCountry": country,
            "Description": rng.choice(DESCRIPTIONS) if rng.random() < 0.3 else None,
            "OwnerId": rng.choice(self.users),
            "CreatedDate": self._dt(mod_days + rng.randint(30, 1800)),
            "LastModifiedDate": self._dt(mod_days),
            "LastActivityDate": self._date(act_days) if rng.random() > 0.1 else None,
            # private helpers, stripped before the org is returned:
            "_prefix": prefix,
            "_core": core,
            "_postal": city[2],
            "_area": city[3],
            "_domain": domain,
        }

    def _account_variant(self, original: Record, kind: str) -> Record:
        """Create a duplicate of ``original`` as a messy second entry."""
        rng = self.rng
        dup = dict(original)
        dup["Id"] = self._new_id("001")
        mod_days = rng.randint(3, 200)
        dup["CreatedDate"] = self._dt(mod_days + rng.randint(30, 300))
        dup["LastModifiedDate"] = self._dt(mod_days)
        dup["LastActivityDate"] = self._date(rng.randint(1, 200))
        dup["OwnerId"] = rng.choice(self.users)
        dup["Description"] = None
        base = f"{original['_prefix']} {original['_core']}"
        suffix = original["Name"].rsplit(" ", 1)[1]
        if kind == "suffix":
            dup["Name"] = f"{base.upper()}, {LEGAL_VARIANT[suffix].upper()}"
        elif kind == "typo":
            word = original["_prefix"]
            positions = [i for i in range(1, len(word) - 2) if word[i] != word[i + 1]]
            i = rng.choice(positions)
            typo = word[:i] + word[i + 1] + word[i] + word[i + 2 :]  # swap two adjacent letters
            dup["Name"] = f"{typo} {original['_core']} {suffix}"
        elif kind == "the_company":
            dup["Name"] = f"The {base} Company"
        elif kind == "rebrand":
            dup["Name"] = f"{original['_prefix']} {rng.choice(REBRAND_CORES)}"
            dup["Phone"] = self._phone(original["_area"], original["BillingCountry"])
        else:  # pragma: no cover - programmer error
            raise ValueError(kind)
        self._used_account_names.add(dup["Name"].lower())
        return dup

    def inject_account_duplicates(self) -> None:
        """12 clusters: 10 pairs + 2 triples = 14 extra accounts."""
        rng = self.rng
        specs = [["suffix"], ["suffix"], ["suffix"], ["typo"], ["typo"], ["the_company"],
                 ["the_company"], ["rebrand"], ["rebrand"], ["suffix"],
                 ["typo", "the_company"], ["suffix", "rebrand"]]  # fmt: skip
        # Skip the first 2*decoys accounts so decoys stay clean, then take originals in order.
        pool = [a for a in self.accounts[10:] if a["Id"] not in self.dup_account_ids]
        originals = rng.sample(pool, len(specs))
        clusters: list[list[str]] = []
        self.dup_clusters_accounts: list[list[Record]] = []
        for original, kinds in zip(originals, specs, strict=True):
            members = [original]
            for kind in kinds:
                dup = self._account_variant(original, kind)
                self.accounts.append(dup)
                self._account_by_id[dup["Id"]] = dup
                members.append(dup)
            self.dup_clusters_accounts.append(members)
            self.dup_account_ids.update(m["Id"] for m in members)
            self.dup_extra_account_ids.update(m["Id"] for m in members[1:])
            clusters.append([m["Id"] for m in members])
        self.seeded.duplicate_clusters["Account"] = clusters

        # Three clusters have *complementary* gaps so a merge has something to fill in:
        # the original lacks Industry while the duplicate lacks Phone.
        for members in self.dup_clusters_accounts[:3]:
            self._snap.update(self._snapshot(members[:2], _MISSING_FIELDS))
            members[0]["Industry"] = None
            members[1]["Phone"] = None
            self._mark_missing("Account", members[0], ["Industry"])
            self._mark_missing("Account", members[1], ["Phone"])

    # -- contacts --------------------------------------------------------------------------
    def build_contacts(self, count: int) -> None:
        rng = self.rng
        base_accounts = [a for a in self.accounts if a["Id"] not in self.dup_extra_account_ids]
        # every base account gets at least one contact; the rest are spread randomly
        owners = list(base_accounts)
        while len(owners) < count:
            owners.append(rng.choice(base_accounts))
        rng.shuffle(owners)
        for account in owners[:count]:
            self.contacts.append(self._new_contact(account))

    def _pick_name(self) -> tuple[str, str]:
        rng = self.rng
        firsts = [p[0] for p in NICK_PAIRS] + OTHER_FIRST
        while True:
            key = (rng.choice(firsts), rng.choice(LAST_NAMES))
            if key not in self._used_contact_keys:
                self._used_contact_keys.add(key)
                return key

    def _new_contact(
        self, account: Record, name: tuple[str, str] | None = None, local: str | None = None
    ) -> Record:
        rng = self.rng
        first, last = name or self._pick_name()
        domain = account["_domain"]
        email_local = local or f"{first}.{last}".lower()
        phone = self._phone(account["_area"], account["BillingCountry"])
        mod_days = rng.randint(3, 320)
        return {
            "Id": self._new_id("003"),
            "AccountId": account["Id"],
            "FirstName": first,
            "LastName": last,
            "Email": f"{email_local}@{domain}",
            "Phone": phone,
            "MobilePhone": self._phone(account["_area"], account["BillingCountry"])
            if rng.random() < 0.5
            else None,
            "Title": rng.choice(TITLES),
            "MailingCity": account["BillingCity"],
            "MailingState": account["BillingState"],
            "MailingPostalCode": account["BillingPostalCode"],
            "MailingCountry": account["BillingCountry"],
            "OwnerId": account["OwnerId"],
            "CreatedDate": self._dt(mod_days + rng.randint(30, 1800)),
            "LastModifiedDate": self._dt(mod_days),
            "LastActivityDate": self._date(rng.randint(1, 320)) if rng.random() > 0.1 else None,
        }

    def _contact_copy(self, original: Record, **overrides: Any) -> Record:
        dup = dict(original)
        dup["Id"] = self._new_id("003")
        mod_days = self.rng.randint(3, 200)
        dup["CreatedDate"] = self._dt(mod_days + self.rng.randint(30, 300))
        dup["LastModifiedDate"] = self._dt(mod_days)
        dup["LastActivityDate"] = self._date(self.rng.randint(1, 200))
        dup.update(overrides)
        return dup

    def inject_contact_duplicates(self) -> None:
        """18 duplicate clusters -> 19 extra contacts (7 split across duplicate Accounts)."""
        rng = self.rng
        by_account: dict[str, list[Record]] = {}
        for c in self.contacts:
            by_account.setdefault(c["AccountId"], []).append(c)
        formal_to_nick = dict(NICK_PAIRS)
        clusters: list[list[str]] = []

        def register(members: list[Record]) -> None:
            self.dup_contact_ids.update(m["Id"] for m in members)
            clusters.append([m["Id"] for m in members])

        # 1) Contacts split across duplicate Accounts (same person, same email)
        for idx in (0, 1, 2, 3, 4, 10):
            members = self.dup_clusters_accounts[idx]
            candidates = [
                c
                for c in by_account.get(members[0]["Id"], [])
                if c["Id"] not in self.dup_contact_ids
            ]
            src = candidates[0]
            copies = [
                self._contact_copy(src, AccountId=m["Id"], OwnerId=m["OwnerId"])
                for m in members[1:]
            ]
            self.contacts.extend(copies)
            register([src, *copies])

        # 2) Same email, different case and a nickname (Robert -> "Bob")
        formal_pool = [
            c for c in self.contacts
            if c["FirstName"] in formal_to_nick and c["Id"] not in self.dup_contact_ids
        ]  # fmt: skip
        for src in rng.sample(formal_pool, 5):
            dup = self._contact_copy(
                src,
                FirstName=formal_to_nick[src["FirstName"]],
                Email=src["Email"].upper(),
            )
            self.contacts.append(dup)
            register([src, dup])

        # 3) Same account + same phone, last-name typo, different email address
        pool = [
            c
            for c in self.contacts
            if c["Id"] not in self.dup_contact_ids and len(c["LastName"]) >= 5
        ]
        for src in rng.sample(pool, 4):
            last = src["LastName"]
            typo_last = last + last[-1]  # "Chen" -> "Chenn"
            dup = self._contact_copy(
                src,
                LastName=typo_last,
                Email=f"{src['FirstName'][0]}{last}".lower() + "@" + src["Email"].split("@")[1],
            )
            self.contacts.append(dup)
            register([src, dup])

        # 4) Same account + same phone, nickname vs formal first name, different email
        formal_pool = [
            c for c in self.contacts
            if c["FirstName"] in formal_to_nick and c["Id"] not in self.dup_contact_ids
        ]  # fmt: skip
        for src in rng.sample(formal_pool, 3):
            nickname = formal_to_nick[src["FirstName"]]
            dup = self._contact_copy(
                src,
                FirstName=nickname,
                Email=f"{nickname}.{src['LastName']}".lower() + "@" + src["Email"].split("@")[1],
            )
            self.contacts.append(dup)
            register([src, dup])

        self.seeded.duplicate_clusters["Contact"] = clusters

    def inject_contact_decoys(self, pairs: int) -> None:
        """People who share a name but are clearly different (other company, email, phone)."""
        rng = self.rng
        pool = [c for c in self.contacts if c["Id"] not in self.dup_contact_ids]
        decoys: list[list[str]] = []
        used: set[str] = set()
        while len(decoys) < pairs:
            a, b = rng.sample(pool, 2)
            if a["AccountId"] == b["AccountId"] or a["Id"] in used or b["Id"] in used:
                continue
            account_b = self._account_by_id[b["AccountId"]]
            b["FirstName"], b["LastName"] = a["FirstName"], a["LastName"]
            b["Email"] = f"{a['FirstName']}.{a['LastName']}".lower() + "@" + account_b["_domain"]
            used.update((a["Id"], b["Id"]))
            decoys.append([a["Id"], b["Id"]])
        self.seeded.decoys["Contact"] = decoys

    # -- seeded problems -------------------------------------------------------------------
    @staticmethod
    def _snapshot(records: list[Record], fields: tuple[str, ...]) -> dict[tuple[str, str], Any]:
        return {(r["Id"], f): r.get(f) for r in records for f in fields if f in r}

    def _mark_missing(self, obj: str, record: Record, fields: list[str]) -> None:
        for f in fields:
            self.seeded.truth[f"{record['Id']}:{f}"] = self._snap[(record["Id"], f)]
        bucket = self.seeded.missing_fields.setdefault(obj, {})
        bucket.setdefault(record["Id"], [])
        for f in fields:
            if f not in bucket[record["Id"]]:
                bucket[record["Id"]].append(f)

    def _mark_format(self, obj: str, record: Record, field_name: str) -> None:
        self.seeded.truth[f"{record['Id']}:{field_name}"] = self._snap[(record["Id"], field_name)]
        self.seeded.invalid_formats.setdefault(obj, []).append([record["Id"], field_name])

    def inject_formats(self) -> None:
        rng = self.rng
        acc_pool = [a for a in self.accounts if a["Id"] not in self.dup_account_ids]
        con_pool = [c for c in self.contacts if c["Id"] not in self.dup_contact_ids]
        decoy_ids = {i for pair in self.seeded.decoys.get("Account", []) for i in pair}
        acc_pool = [a for a in acc_pool if a["Id"] not in decoy_ids]
        self.format_touched: set[str] = set()
        self._snap.update(self._snapshot([*self.accounts, *self.contacts], _FORMAT_FIELDS))

        def take(pool: list[Record], n: int, predicate: Any = None) -> list[Record]:
            eligible = [r for r in pool if r["Id"] not in self.format_touched and
                        (predicate is None or predicate(r))]  # fmt: skip
            chosen = rng.sample(eligible, n)
            self.format_touched.update(r["Id"] for r in chosen)
            return chosen

        def bad_phone(valid: str) -> str:
            digits = "".join(ch for ch in valid if ch.isdigit())[-10:]
            options = [
                f"{digits[3:6]}-{digits[6:]}",  # 7 digits, no area code
                f"({digits[:3]}) {digits[3:6]}-{digits[6:8]}",  # truncated
                "call front desk",
                "N/A",
                "0000000000",
                f"{digits[:3]}-{digits[3:6]}-{digits[6:]} / {digits[:3]}-555-0199",
                f"+1 ({digits[:3]}) {digits[3:6]}-{digits[6:9]}",  # 9 digits after +1
                "1-800-FLOWERS",
                f" {valid} ",  # stray whitespace (fixable)
            ]
            return rng.choice(options)

        # Accounts: phones, then postal codes (US-only so every mutation is well-defined)
        for acc in take(acc_pool, 6, lambda r: r["BillingCountry"] == "United States"):
            acc["Phone"] = bad_phone(acc["Phone"])
            self._mark_format("Account", acc, "Phone")
        for acc in take(acc_pool, 3, lambda r: r["BillingState"] in LEADING_ZERO_STATES):
            acc["BillingPostalCode"] = acc["BillingPostalCode"].lstrip("0")  # lost leading zero
            self._mark_format("Account", acc, "BillingPostalCode")
        for acc in take(acc_pool, 3, lambda r: r["BillingCountry"] == "United States"):
            acc["BillingPostalCode"] = rng.choice(["N/A", acc["BillingPostalCode"] + "-", "94 105"])
            self._mark_format("Account", acc, "BillingPostalCode")

        # Contacts
        for con in take(con_pool, 5):
            local = f"{con['FirstName']}.{con['LastName']}".lower()
            domain = con["Email"].split("@")[1]
            con["Email"] = rng.choice(
                [f"{local}@gmial.com", f"{local}@hotmial.com", f"{local}@{domain.split('.')[0]}"]
            )
            self._mark_format("Contact", con, "Email")
            # A typo'd personal domain was meant to be that personal address (not the work one).
            for typo, fixed in (("@gmial.com", "@gmail.com"), ("@hotmial.com", "@hotmail.com")):
                if con["Email"].endswith(typo):
                    self.seeded.truth[f"{con['Id']}:Email"] = f"{local}{fixed}"
        for con in take(con_pool, 4):
            con["Email"] = rng.choice(
                [
                    con["Email"].replace("@", "@@"),
                    f" {con['Email']} ",
                    con["Email"].replace("@", "."),
                    f"{con['FirstName']} {con['LastName']}@{con['Email'].split('@')[1]}".lower(),
                ]
            )
            self._mark_format("Contact", con, "Email")
        for con in take(con_pool, 8, lambda r: r["MailingCountry"] != "United Kingdom"):
            con["Phone"] = bad_phone(con["Phone"])
            self._mark_format("Contact", con, "Phone")
        for con in take(con_pool, 3, lambda r: r["MailingState"] in LEADING_ZERO_STATES):
            con["MailingPostalCode"] = con["MailingPostalCode"].lstrip("0")
            self._mark_format("Contact", con, "MailingPostalCode")
        for con in take(con_pool, 2, lambda r: r["MailingCountry"] == "Canada"):
            con["MailingPostalCode"] = con["MailingPostalCode"].lower().replace(" ", "")
            self._mark_format("Contact", con, "MailingPostalCode")
        for con in take(con_pool, 2, lambda r: r["MailingCountry"] == "United States"):
            con["MailingPostalCode"] = con["MailingPostalCode"] + "-"
            self._mark_format("Contact", con, "MailingPostalCode")

    def inject_missing(self) -> None:
        rng = self.rng
        self._snap.update(self._snapshot([*self.accounts, *self.contacts], _MISSING_FIELDS))
        acc_pool = [
            a for a in self.accounts
            if a["Id"] not in self.dup_account_ids and a["Id"] not in self.format_touched
        ]  # fmt: skip
        con_pool = [
            c for c in self.contacts
            if c["Id"] not in self.dup_contact_ids and c["Id"] not in self.format_touched
        ]  # fmt: skip
        for acc in rng.sample(acc_pool, 22):
            fields = rng.sample(
                ["Industry", "Phone", "Website", "BillingCountry"], rng.randint(1, 2)
            )
            for f in fields:
                acc[f] = None
            self._mark_missing("Account", acc, fields)
        chosen = rng.sample(con_pool, 40)
        for i, con in enumerate(chosen):
            if i < 8:
                fields = ["AccountId"]
            elif i < 20:
                fields = ["Email"]
            elif i < 32:
                fields = ["Title"]
            else:
                fields = ["Title", "Email"]
            for f in fields:
                con[f] = None
            self._mark_missing("Contact", con, fields)

    def inject_stale(self) -> None:
        rng = self.rng
        acc_pool = [a for a in self.accounts if a["Id"] not in self.dup_account_ids]
        con_pool = [c for c in self.contacts if c["Id"] not in self.dup_contact_ids]
        stale_acc = rng.sample(acc_pool, 28)
        stale_con = rng.sample(con_pool, 70)
        for rec in [*stale_acc, *stale_con]:
            mod_days = rng.randint(400, 1100)
            rec["CreatedDate"] = self._dt(mod_days + rng.randint(30, 800))
            rec["LastModifiedDate"] = self._dt(mod_days)
            rec["LastActivityDate"] = (
                self._date(rng.randint(400, 1100)) if rng.random() > 0.25 else None
            )
        self.seeded.stale["Account"] = [r["Id"] for r in stale_acc]
        self.seeded.stale["Contact"] = [r["Id"] for r in stale_con]

    # -- assembly --------------------------------------------------------------------------
    def finish(self) -> dict[str, list[Record]]:
        def clean(rec: Record) -> Record:
            return {k: v for k, v in rec.items() if not k.startswith("_")}

        users = [
            {"Id": uid, "Name": name, "Email": name.lower().replace(" ", ".") + "@example-corp.com"}
            for uid, name in zip(self.users, OWNER_NAMES, strict=True)
        ]
        return {
            "Account": [clean(a) for a in self.accounts],
            "Contact": [clean(c) for c in self.contacts],
            "User": users,
            "Task": [],
        }


def generate_mock_org(
    *,
    seed: int = 42,
    as_of: date | None = None,
    n_accounts: int = 200,
    n_contacts: int = 500,
) -> MockOrgData:
    """Generate a fake org of ``n_accounts`` Accounts and ``n_contacts`` Contacts.

    The org mimics a mid-size B2B CRM that has accreted problems over the years. Dates are
    generated relative to ``as_of`` (default: today) so "stale" always means what it says.

    Args:
        seed: RNG seed; the same seed and ``as_of`` always give identical output.
        as_of: Reference "today" for all relative dates.
        n_accounts: Total Accounts including the injected duplicates.
        n_contacts: Total Contacts including the injected duplicates.
    """
    as_of = as_of or datetime.now(UTC).date()
    b = _Builder(seed, as_of)
    extra_accounts = 14  # see inject_account_duplicates
    extra_contacts = 19  # see inject_contact_duplicates: 7 split + 5 + 4 + 3
    b.build_accounts(n_accounts - extra_accounts, decoy_pairs=4)
    b.inject_account_duplicates()
    b.build_contacts(n_contacts - extra_contacts)
    b.inject_contact_duplicates()
    b.inject_contact_decoys(pairs=6)
    b.inject_formats()
    b.inject_missing()
    b.inject_stale()
    records = b.finish()
    if len(records["Account"]) != n_accounts or len(records["Contact"]) != n_contacts:
        raise AssertionError(  # pragma: no cover - guards the hard-coded duplicate counts
            f"generated {len(records['Account'])}/{len(records['Contact'])} records, "
            f"expected {n_accounts}/{n_contacts}"
        )
    return MockOrgData(seed=seed, as_of=as_of.isoformat(), records=records, seeded=b.seeded)
