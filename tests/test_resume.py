"""Durability: what survives losing the process, and what must not happen twice.

These are the tests the stage is for. Each one destroys something — the graph
object, the database connection, the schema the checkpoint was written with —
and then asks the flow to carry on.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, interrupt

from aimai_workflows.contract import (
    RuleBasedReviewer,
    compile_graph,
    crm_notes,
    thread_config,
)
from aimai_workflows.contract.checkpointer import in_memory_checkpointer, serializer
from aimai_workflows.contract.sinks import crm_create_note, idempotency_key
from aimai_workflows.contract.state import Finding, merge_findings


# These schemas are defined at module level, not inside the tests that use
# them. LangGraph resolves a state schema's annotations with
# `get_type_hints()`, which looks names up in the defining MODULE's globals —
# a TypedDict declared inside a function raises `NameError: Annotated` there.
# The same trap catches anyone who defines a state class in a factory.
class Probe(TypedDict, total=False):
    log: Annotated[list[str], lambda a, b: (a or []) + (b or [])]


class Naive(TypedDict, total=False):
    contract_no: str
    receipt: str


class ExtendedState(TypedDict, total=False):
    """`ContractState` as a later deploy might extend it."""

    contract_no: str
    document_uri: str
    text_sha256: str
    fetched_at: str
    findings: Annotated[list[Finding], merge_findings]
    overall_risk: str
    summary: str
    assessment_attempts: int
    risk_reconciled: bool
    decision: str
    decision_note: str
    decided_at: str
    action_receipt: str
    action_skipped_reason: str
    # The new key this "deploy" adds.
    reviewer_team: str


def test_resume_after_losing_the_graph_object(registry, reviewer) -> None:
    """A redeploy in the middle of an approval.

    The paused review is not in the graph, the app or a queue — it is rows in
    the checkpointer. Building an entirely new graph over the same store and
    resuming has to work, because that is what every deploy during business
    hours looks like.
    """
    saver = in_memory_checkpointer()
    config = thread_config("SUP-100")

    first = compile_graph(reviewer, saver, registry=registry)
    first.invoke(
        {"contract_no": "SUP-100", "document_uri": "high_risk.txt"},
        config,
        durability="sync",
    )
    assert first.get_state(config).next == ("human_gate",)

    del first
    second = compile_graph(RuleBasedReviewer(), saver, registry=registry)
    out = second.invoke(Command(resume="approve"), config, durability="sync")

    assert out["decision"] == "approve"
    assert out["action_receipt"]
    assert len(crm_notes()) == 1


def test_resume_across_a_closed_database_connection(registry, sqlite_path) -> None:
    """The same review, resumed by what is effectively another process.

    The connection is closed and reopened between the two halves, so nothing
    in memory carries the run: the second half reads the state back out of the
    file, exactly as a restarted worker would.
    """
    config = thread_config("SUP-101")

    connection = sqlite3.connect(sqlite_path, check_same_thread=False)
    saver = SqliteSaver(connection, serde=serializer())
    saver.setup()
    graph = compile_graph(RuleBasedReviewer(), saver, registry=registry)
    graph.invoke(
        {"contract_no": "SUP-101", "document_uri": "high_risk.txt"},
        config,
        durability="sync",
    )
    connection.close()

    reopened = sqlite3.connect(sqlite_path, check_same_thread=False)
    resumed = compile_graph(
        RuleBasedReviewer(),
        SqliteSaver(reopened, serde=serializer()),
        registry=registry,
    )
    state = resumed.get_state(config)
    assert state.next == ("human_gate",)
    assert state.values["overall_risk"] == "critical"

    out = resumed.invoke(Command(resume="approve"), config, durability="sync")
    reopened.close()

    assert len(crm_notes("SUP-101")) == 1
    assert out["action_receipt"] == idempotency_key(
        "SUP-101", out["text_sha256"], "approve"
    )


def test_the_side_effect_is_not_repeated_by_a_replayed_action(
    registry, sqlite_path
) -> None:
    """The action node runs twice; the CRM still holds one note.

    This simulates the ugly case the idempotency key is actually for: the note
    was written, and the acknowledgement was lost before the checkpoint
    recorded it, so the work is done again on recovery. Calling the sink a
    second time with the same state has to be a no-op that returns the
    original receipt.
    """
    config = thread_config("SUP-102")
    connection = sqlite3.connect(sqlite_path, check_same_thread=False)
    saver = SqliteSaver(connection, serde=serializer())
    saver.setup()
    graph = compile_graph(RuleBasedReviewer(), saver, registry=registry)
    graph.invoke(
        {"contract_no": "SUP-102", "document_uri": "high_risk.txt"},
        config,
        durability="sync",
    )
    out = graph.invoke(Command(resume="approve"), config, durability="sync")

    replayed = crm_create_note(
        idempotency_key("SUP-102", out["text_sha256"], "approve"),
        "SUP-102",
        "a second attempt at the same note",
    )
    connection.close()

    assert replayed.deduplicated is True
    assert replayed.idempotency_key == out["action_receipt"]
    assert len(crm_notes("SUP-102")) == 1
    assert "second attempt" not in crm_notes("SUP-102")[0].body


def test_the_interrupted_node_re_executes_from_its_first_line(registry) -> None:
    """Why the side effect is not in the gate node.

    `interrupt()` is not a suspension point: it raises, and on resume LangGraph
    replays the node from the top until the call can return the human's answer.
    Anything above that line therefore runs once per resume. This test pins the
    behaviour instead of trusting the documentation — if a LangGraph release
    ever changed it, the design note in the README would silently become wrong.
    """
    from langgraph.graph import END, START, StateGraph

    executions: list[str] = []

    def gate(state: Probe) -> Probe:
        executions.append("before-interrupt")
        answer = interrupt({"ask": "approve?"})
        return {"log": [f"decided:{answer}"]}

    builder = StateGraph(Probe)
    builder.add_node("gate", gate)
    builder.add_edge(START, "gate")
    builder.add_edge("gate", END)
    graph = builder.compile(checkpointer=in_memory_checkpointer())
    config = {"configurable": {"thread_id": "probe"}}

    graph.invoke({}, config, durability="sync")
    assert executions == ["before-interrupt"]

    graph.invoke(Command(resume="yes"), config, durability="sync")
    assert executions == ["before-interrupt", "before-interrupt"], (
        "the node above interrupt() ran twice — this is why execute_action is "
        "a separate node"
    )


def test_a_side_effect_inside_the_gate_would_fire_twice(registry) -> None:
    """The counter-example, kept executable.

    The same flow with the CRM write moved into the gate node produces two
    notes for one approval. It is here so the design decision is demonstrated
    rather than asserted, and so a future refactor that "simplifies" the two
    nodes into one fails a test that explains why.
    """
    from langgraph.graph import END, START, StateGraph

    calls: list[str] = []

    def gate_with_side_effect(state: Naive) -> Naive:
        # Written BEFORE the interrupt, and with a key derived from the
        # attempt rather than the facts: the two mistakes together.
        calls.append(state["contract_no"])
        crm_create_note(
            f"attempt-{len(calls)}", state["contract_no"], "written from the gate"
        )
        answer = interrupt({"ask": "approve?"})
        return {"receipt": str(answer)}

    builder = StateGraph(Naive)
    builder.add_node("gate", gate_with_side_effect)
    builder.add_edge(START, "gate")
    builder.add_edge("gate", END)
    graph = builder.compile(checkpointer=in_memory_checkpointer())
    config = {"configurable": {"thread_id": "naive"}}

    graph.invoke({"contract_no": "SUP-103"}, config, durability="sync")
    graph.invoke(Command(resume="approve"), config, durability="sync")

    assert len(crm_notes("SUP-103")) == 2, (
        "expected the naive design to double-write; if this fails, LangGraph "
        "changed its replay semantics and the design note needs revisiting"
    )


def test_an_old_checkpoint_resumes_after_the_schema_gains_a_key(
    registry, sqlite_path
) -> None:
    """A deploy that adds a state key must not strand paused reviews.

    Every key in `ContractState` is optional (`total=False`) for exactly this
    reason. Here the checkpoint is written by the current flow and read back by
    a graph whose state schema has an extra key — the shape of a normal deploy
    while approvals are outstanding.
    """
    config = thread_config("SUP-104")
    connection = sqlite3.connect(sqlite_path, check_same_thread=False)
    saver = SqliteSaver(connection, serde=serializer())
    saver.setup()
    compile_graph(RuleBasedReviewer(), saver, registry=registry).invoke(
        {"contract_no": "SUP-104", "document_uri": "high_risk.txt"},
        config,
        durability="sync",
    )

    from langgraph.graph import END, START, StateGraph

    from aimai_workflows.contract.nodes import build_nodes, route_after_gate

    nodes = build_nodes(RuleBasedReviewer(), registry=registry)
    builder = StateGraph(ExtendedState)
    builder.add_node("human_gate", nodes["human_gate"])
    builder.add_node("execute_action", nodes["execute_action"])
    builder.add_edge(START, "human_gate")
    builder.add_conditional_edges(
        "human_gate", route_after_gate, {"execute_action": "execute_action", "end": END}
    )
    builder.add_edge("execute_action", END)
    upgraded = builder.compile(checkpointer=saver)

    out = upgraded.invoke(Command(resume="approve"), config, durability="sync")
    connection.close()

    assert out["decision"] == "approve"
    assert out["action_receipt"]
    assert "reviewer_team" not in out
    assert len(crm_notes("SUP-104")) == 1


def test_a_second_start_does_not_fork_the_review(registry, reviewer) -> None:
    """The thread id is derived from the contract, so a retry rejoins the run.

    A random UUID per request would have produced a second, invisible review of
    the same contract — and eventually a second CRM note with a different key.
    """
    saver = in_memory_checkpointer()
    graph = compile_graph(reviewer, saver, registry=registry)
    payload = {"contract_no": "SUP-105", "document_uri": "high_risk.txt"}

    graph.invoke(payload, thread_config("SUP-105"), durability="sync")
    graph.invoke(Command(resume="approve"), thread_config("SUP-105"), durability="sync")

    # The retried request: same contract, same document, brand new call.
    graph.invoke(payload, thread_config("SUP-105"), durability="sync")

    assert len(crm_notes("SUP-105")) == 1


def test_measured_durability_modes_all_resume(registry) -> None:
    """`durability` changes when checkpoints are written, not whether resume
    works.

    All three modes are exercised here so the README's comparison table is not
    the only place the difference is claimed. `exit` writes at the end of the
    run — which for an interrupted run still means the pause is durable,
    because the interrupt ends the invocation.
    """
    for mode in ("exit", "async", "sync"):
        contract_no = f"SUP-mode-{mode}"
        saver = in_memory_checkpointer()
        graph = compile_graph(RuleBasedReviewer(), saver, registry=registry)
        config = thread_config(contract_no)
        graph.invoke(
            {"contract_no": contract_no, "document_uri": "high_risk.txt"},
            config,
            durability=mode,
        )
        assert graph.get_state(config).next == ("human_gate",), mode

        out = graph.invoke(Command(resume="approve"), config, durability=mode)
        assert out["action_receipt"], mode
        assert len(crm_notes(contract_no)) == 1, mode
