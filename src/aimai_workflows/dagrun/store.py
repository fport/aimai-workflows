"""The result store: what makes a rerun cheap and a resume possible.

Keyed on `(seed, node_id, fingerprint)`. All three are needed and none is
redundant:

- `seed` scopes results to one job. Without it, two documents share a cache and
  the second gets the first's answers.
- `node_id` is obvious, and insufficient on its own: the same node with
  different inputs is different work.
- `fingerprint` is what makes the entry safe to reuse. Keying on
  `(seed, node_id)` alone would serve a stale answer after a prompt edit, which
  is the exact failure a cache is supposed to be too boring to cause.

Only `SUCCEEDED` and `DEGRADED` rows are read back. Caching a failure would
turn a transient timeout into a permanent one, and a `SKIPPED` node's non-result
says nothing about whether it should be skipped this time.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .types import NodeResult, NodeState

__all__ = ["ResultStore", "open_store"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    seed         TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    state        TEXT NOT NULL,
    output       TEXT NOT NULL,
    evidence     TEXT NOT NULL,
    note         TEXT NOT NULL,
    cost_usd     REAL NOT NULL,
    tokens       INTEGER NOT NULL,
    attempt      INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (seed, node_id, fingerprint)
);
-- The executor asks "is there a usable result for this exact work" on every
-- node of every run; the primary key answers it. This second index answers the
-- other question — "what happened in this job" — which the report and `--resume`
-- both ask once per run.
CREATE INDEX IF NOT EXISTS results_by_seed ON results (seed, created_at);
"""


class ResultStore:
    """SQLite-backed node results, scoped by job."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._conn.executescript(_SCHEMA)
        self.hits = 0
        self.misses = 0

    def get(self, seed: str, node_id: str, fingerprint: str) -> NodeResult | None:
        """A reusable result for exactly this work, or None."""
        row = self._conn.execute(
            "SELECT state, output, evidence, note, cost_usd, tokens FROM results "
            "WHERE seed = ? AND node_id = ? AND fingerprint = ? "
            "AND state IN (?, ?)",
            (
                seed,
                node_id,
                fingerprint,
                NodeState.SUCCEEDED.value,
                NodeState.DEGRADED.value,
            ),
        ).fetchone()
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        return NodeResult(
            output=json.loads(row[1]),
            evidence=tuple(json.loads(row[2])),
            note=row[3],
            cost_usd=row[4],
            tokens=row[5],
            degraded=row[0] == NodeState.DEGRADED.value,
        )

    def put(
        self,
        seed: str,
        node_id: str,
        fingerprint: str,
        state: NodeState,
        result: NodeResult,
        attempt: int,
    ) -> None:
        """Record an outcome. Terminal failures are stored too, for the report.

        They are written but never read back by `get` — the state filter there
        is what keeps a transient failure from becoming permanent.
        """
        self._conn.execute(
            "INSERT INTO results (seed, node_id, fingerprint, state, output, "
            "evidence, note, cost_usd, tokens, attempt, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(seed, node_id, fingerprint) DO UPDATE SET "
            "state = excluded.state, output = excluded.output, "
            "evidence = excluded.evidence, note = excluded.note, "
            "cost_usd = excluded.cost_usd, tokens = excluded.tokens, "
            "attempt = excluded.attempt, created_at = excluded.created_at",
            (
                seed,
                node_id,
                fingerprint,
                state.value,
                json.dumps(dict(result.output), ensure_ascii=False, default=str),
                json.dumps(list(result.evidence), ensure_ascii=False),
                result.note,
                result.cost_usd,
                result.tokens,
                attempt,
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        self._conn.commit()

    def history(self, seed: str) -> list[dict]:
        """Everything recorded for one job, oldest first."""
        rows = self._conn.execute(
            "SELECT node_id, fingerprint, state, cost_usd, tokens, attempt, "
            "created_at FROM results WHERE seed = ? ORDER BY created_at, node_id",
            (seed,),
        )
        return [
            {
                "node_id": r[0],
                "fingerprint": r[1],
                "state": r[2],
                "cost_usd": r[3],
                "tokens": r[4],
                "attempt": r[5],
                "created_at": r[6],
            }
            for r in rows
        ]

    def forget(self, seed: str, node_id: str | None = None) -> int:
        """Drop cached results, for a whole job or one node."""
        if node_id:
            cursor = self._conn.execute(
                "DELETE FROM results WHERE seed = ? AND node_id = ?", (seed, node_id)
            )
        else:
            cursor = self._conn.execute("DELETE FROM results WHERE seed = ?", (seed,))
        self._conn.commit()
        return cursor.rowcount


@contextmanager
def open_store(path: str | Path = ".aimai-dagrun.sqlite3") -> Iterator[ResultStore]:
    """Open a store for the life of a run.

    `:memory:` is a valid path and is what most tests use — the cache semantics
    are identical and nothing is left behind.
    """
    connection = sqlite3.connect(str(path))
    try:
        yield ResultStore(connection)
    finally:
        connection.close()
