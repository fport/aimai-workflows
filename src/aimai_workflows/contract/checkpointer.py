"""Checkpointer lifecycles, and the serializer every one of them needs.

Three backends, one for each place the flow runs: memory in the fast tests,
a SQLite file in the crash script, Postgres in the service. They are here
together because they share the two things that are easy to get wrong.

**One connection per process, not per request.** `PostgresSaver.from_conn_string`
is a context manager: it opens a connection on enter and closes it on exit. Used
inside a request handler that is correct and useless — every review pays a TCP
handshake and TLS negotiation, and under any load the database runs out of
connections. The service opens it once in FastAPI's `lifespan` and hands the
same saver to every request. `setup()` is called there too, exactly once; it is
idempotent DDL, but calling it per request would serialize every review behind
a schema check.

**The serializer needs an allowlist.** The state holds `Finding` objects, and
LangGraph will not deserialize an arbitrary class out of a checkpoint by
default — reading a checkpoint means constructing whatever type the bytes name,
which is a remote-code-execution shape if the store is ever writable by someone
else. Recent versions warn; a future one blocks. Registering exactly the types
this flow stores keeps the state typed without opening that door.

The alternative — storing findings as plain dicts and validating on the way out
— is defensible and was rejected: the reducer, the gate and the API would each
have to remember to validate, and the one that forgets is the one that writes a
malformed finding into the CRM.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

__all__ = [
    "ALLOWED_STATE_TYPES",
    "in_memory_checkpointer",
    "postgres_checkpointer",
    "postgres_dsn",
    "serializer",
    "sqlite_checkpointer",
]

ALLOWED_STATE_TYPES: tuple[tuple[str, str], ...] = (
    ("aimai_workflows.contract.state", "Finding"),
)
"""Every non-builtin type that may be reconstructed from a checkpoint.

Adding a type to the state means adding it here. That friction is the feature:
it is a moment to ask whether the thing really belongs in durable storage.
"""


def serializer() -> JsonPlusSerializer:
    """The serializer every checkpointer in this package is built with."""
    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_STATE_TYPES)


def in_memory_checkpointer() -> InMemorySaver:
    """For tests that do not need to outlive the process.

    Still built with the real serializer, so a type that would fail to
    round-trip in Postgres fails in the fast tests too.
    """
    return InMemorySaver(serde=serializer())


@contextmanager
def sqlite_checkpointer(path: str) -> Iterator[object]:
    """A file-backed checkpointer, for the crash script and for local runs.

    `check_same_thread=False` because the saver is read from FastAPI's thread
    pool. SQLite is single-writer; that is fine for one worker and is exactly
    why the service uses Postgres.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        saver = SqliteSaver(conn, serde=serializer())
        saver.setup()
        yield saver
    finally:
        conn.close()


def postgres_dsn() -> str:
    """Where the service stores its checkpoints."""
    return os.getenv(
        "CONTRACT_DB_DSN",
        "postgresql://contract:contract@localhost:5432/contract?sslmode=disable",
    )


@contextmanager
def postgres_checkpointer(dsn: str | None = None) -> Iterator[object]:
    """Open one pooled Postgres checkpointer for the life of the process.

    `autocommit=True` is required by the LangGraph Postgres saver: it issues
    `CREATE TABLE IF NOT EXISTS` in `setup()` and writes checkpoints outside an
    outer transaction. Leaving the default would leave the DDL uncommitted and
    the first write would fail on a missing table — with an error that blames
    the write.
    """
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo=dsn or postgres_dsn(),
        max_size=int(os.getenv("CONTRACT_DB_POOL_SIZE", "10")),
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=True,
    )
    try:
        saver = PostgresSaver(pool, serde=serializer())
        saver.setup()
        yield saver
    finally:
        pool.close()
