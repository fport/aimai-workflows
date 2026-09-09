"""The production checkpointer, against a real PostgreSQL.

Excluded from the default run (`-m 'not postgres'`) so `pytest` stays a
one-command, no-dependency check. CI runs them with a service container, which
is the point: SQLite and Postgres disagree about concurrency, about types, and
about what `setup()` has to create, and a durability claim tested only against
SQLite is a claim about SQLite.

    docker compose up -d postgres
    uv run pytest -m postgres
"""

from __future__ import annotations

import os

import pytest
from langgraph.types import Command

from aimai_workflows.contract import (
    RuleBasedReviewer,
    compile_graph,
    crm_notes,
    thread_config,
)
from aimai_workflows.contract.checkpointer import postgres_checkpointer, postgres_dsn

pytestmark = pytest.mark.postgres


@pytest.fixture
def saver():
    pytest.importorskip("psycopg", reason="install the 'service' extra")
    with postgres_checkpointer(os.getenv("CONTRACT_DB_DSN", postgres_dsn())) as saver:
        yield saver


def test_a_review_survives_a_new_connection_pool(registry, saver) -> None:
    """The paused review lives in Postgres, not in the pool that created it."""
    config = thread_config("PG-1")
    compile_graph(RuleBasedReviewer(), saver, registry=registry).invoke(
        {"contract_no": "PG-1", "document_uri": "high_risk.txt"},
        config,
        durability="sync",
    )

    with postgres_checkpointer() as second_pool:
        resumed = compile_graph(RuleBasedReviewer(), second_pool, registry=registry)
        assert resumed.get_state(config).next == ("human_gate",)
        out = resumed.invoke(Command(resume="approve"), config, durability="sync")

    assert out["action_receipt"]
    assert len(crm_notes("PG-1")) == 1


def test_findings_round_trip_through_the_serializer(registry, saver) -> None:
    """`Finding` objects come back as `Finding` objects, not as dicts.

    This is what `ALLOWED_STATE_TYPES` buys, and it fails loudly here rather
    than at the first attribute access in a node.
    """
    from aimai_workflows.contract.state import Finding

    config = thread_config("PG-2")
    graph = compile_graph(RuleBasedReviewer(), saver, registry=registry)
    graph.invoke(
        {"contract_no": "PG-2", "document_uri": "high_risk.txt"},
        config,
        durability="sync",
    )

    findings = graph.get_state(config).values["findings"]

    assert findings and all(isinstance(f, Finding) for f in findings)
