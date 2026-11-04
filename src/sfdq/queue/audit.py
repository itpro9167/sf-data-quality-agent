"""Append-only, hash-chained audit log.

Each entry stores ``hash = sha256(prev_hash + canonical_json(entry))``. SQLite triggers reject
UPDATE and DELETE, so the table is append-only at the database level, and the chain makes any
out-of-band tampering (e.g. editing the file with another tool) detectable via :func:`verify_chain`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from sfdq.models import AuditEntry
from sfdq.util import utcnow_iso

GENESIS_HASH = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    event       TEXT NOT NULL,
    proposal_id INTEGER,
    details     TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def _digest(
    prev_hash: str, ts: str, actor: str, event: str, proposal_id: int | None, details: str
) -> str:
    payload = json.dumps(
        [ts, actor, event, proposal_id, details], separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


def append(
    conn: sqlite3.Connection,
    actor: str,
    event: str,
    proposal_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEntry:
    """Add one entry. The caller must already hold a write transaction (``BEGIN IMMEDIATE``)."""
    row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = row["hash"] if row else GENESIS_HASH
    ts = utcnow_iso()
    details_json = json.dumps(details or {}, sort_keys=True, default=str, ensure_ascii=False)
    digest = _digest(prev_hash, ts, actor, event, proposal_id, details_json)
    cur = conn.execute(
        "INSERT INTO audit_log (ts, actor, event, proposal_id, details, prev_hash, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, actor, event, proposal_id, details_json, prev_hash, digest),
    )
    return AuditEntry(
        id=int(cur.lastrowid or 0),
        ts=ts,
        actor=actor,
        event=event,
        proposal_id=proposal_id,
        details=json.loads(details_json),
        prev_hash=prev_hash,
        hash=digest,
    )


def row_to_entry(row: sqlite3.Row) -> AuditEntry:
    """Convert a database row to an :class:`AuditEntry`."""
    return AuditEntry(
        id=row["id"],
        ts=row["ts"],
        actor=row["actor"],
        event=row["event"],
        proposal_id=row["proposal_id"],
        details=json.loads(row["details"]),
        prev_hash=row["prev_hash"],
        hash=row["hash"],
    )


@dataclass(frozen=True)
class ChainStatus:
    """Result of verifying the hash chain."""

    ok: bool
    entries: int
    first_bad_id: int | None = None


def verify_chain(conn: sqlite3.Connection) -> ChainStatus:
    """Recompute every hash and check each entry links to its predecessor."""
    prev = GENESIS_HASH
    count = 0
    for row in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        expected = _digest(
            prev, row["ts"], row["actor"], row["event"], row["proposal_id"], row["details"]
        )
        if row["prev_hash"] != prev or row["hash"] != expected:
            return ChainStatus(False, count, row["id"])
        prev = row["hash"]
        count += 1
    return ChainStatus(True, count)
