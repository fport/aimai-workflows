"""The side effect at the end of the flow, and the only defence against
repeating it.

`crm_create_note` stands in for a real CRM. Faking it is deliberate — a real
integration would add credentials and a network dependency to every test while
proving nothing this stage is about. What is *not* faked is the property under
test: the note is written to durable storage behind an idempotency key, so
asking twice with the same key writes once.

Why SQLite rather than a dict in module scope: the crash test kills the worker
process. A dict dies with it, and the "resume does not repeat the side effect"
claim would then be true only because the evidence was thrown away. A real CRM
survives our crash; the fake has to as well or the test is theatre.

The key is derived from the contract number, the document hash and the
decision — not from a UUID and not from the run. Two runs over the same
document with the same decision are the same business fact and must collapse
into one note; a run over a *revised* document is a different fact and gets its
own, because the hash changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "CrmNote",
    "crm_create_note",
    "crm_notes",
    "idempotency_key",
    "reset_crm",
]

_DEFAULT_PATH = Path(".aimai-crm.sqlite3")


def _db_path() -> Path:
    """Where the fake CRM stores its notes.

    Read from the environment on every call rather than captured at import
    time, so a test can point it at a temporary directory and the subprocess in
    `scripts/kill_mid_run.py` inherits the same value.
    """
    return Path(os.getenv("CONTRACT_CRM_DB", _DEFAULT_PATH))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crm_notes (
            idempotency_key TEXT PRIMARY KEY,
            contract_no     TEXT NOT NULL,
            body            TEXT NOT NULL,
            payload         TEXT NOT NULL,
            created_at      TEXT NOT NULL
        )
        """
    )
    return conn


@dataclass(frozen=True, slots=True)
class CrmNote:
    """One note as the CRM stored it."""

    idempotency_key: str
    contract_no: str
    body: str
    payload: dict
    created_at: str
    deduplicated: bool = False
    """True when this call found an existing note instead of writing one.

    The caller needs to tell the two apart: a deduplicated write is the healthy
    outcome of a resume, while a *first* write during a resume would mean the
    key is not stable and the side effect just happened twice.
    """


def idempotency_key(contract_no: str, text_sha256: str, decision: str) -> str:
    """Derive the key from the facts, never from the attempt.

    Everything that makes this a distinct business event is in the hash and
    nothing else is. In particular the timestamp is not: including it would
    produce a fresh key on every resume and quietly disable the deduplication
    this function exists for.
    """
    raw = f"{contract_no}|{text_sha256}|{decision}"
    return "crm-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def crm_create_note(
    key: str, contract_no: str, body: str, payload: dict | None = None
) -> CrmNote:
    """Write a note, or return the note this key already wrote.

    `INSERT ... ON CONFLICT DO NOTHING` followed by a read, rather than
    check-then-write: two workers resuming the same thread at the same moment
    is exactly the situation the key is for, and a check-then-write races.
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    encoded = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO crm_notes
                (idempotency_key, contract_no, body, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (key, contract_no, body, encoded, now),
        )
        wrote = cursor.rowcount == 1
        row = conn.execute(
            "SELECT contract_no, body, payload, created_at FROM crm_notes "
            "WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
    return CrmNote(
        idempotency_key=key,
        contract_no=row[0],
        body=row[1],
        payload=json.loads(row[2]),
        created_at=row[3],
        deduplicated=not wrote,
    )


def crm_notes(contract_no: str | None = None) -> list[CrmNote]:
    """Every note, for assertions and for the crash script's report."""
    query = (
        "SELECT idempotency_key, contract_no, body, payload, created_at FROM crm_notes"
    )
    params: tuple[str, ...] = ()
    if contract_no is not None:
        query += " WHERE contract_no = ?"
        params = (contract_no,)
    with _connect() as conn:
        rows = conn.execute(query + " ORDER BY created_at, idempotency_key", params)
        return [
            CrmNote(
                idempotency_key=r[0],
                contract_no=r[1],
                body=r[2],
                payload=json.loads(r[3]),
                created_at=r[4],
            )
            for r in rows
        ]


def reset_crm() -> None:
    """Drop every note. For tests and for a clean crash-script run."""
    with _connect() as conn:
        conn.execute("DELETE FROM crm_notes")
