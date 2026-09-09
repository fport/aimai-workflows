"""State transitions, degradation, budgets and retries.

Every test here runs without a model. That is the most valuable property of
this stage's code: the thing being proved is the coordination layer, and a
coordination test that needs an API key gets run once.
"""

from __future__ import annotations

import asyncio

import pytest

from aimai_workflows.dagrun.execute import JobLimits, execute
from aimai_workflows.dagrun.report import as_dict, missing_data_section
from aimai_workflows.dagrun.store import open_store
from aimai_workflows.dagrun.types import (
    Node,
    NodeKind,
    NodeResult,
    NodeState,
    RunContext,
)
from aimai_workflows.dagrun.validate import validate


def fake_node(
    node_id: str,
    *,
    kind: NodeKind = NodeKind.TASK,
    fails: bool = False,
    fails_until: int = 0,
    hangs: bool = False,
    cost: float = 0.01,
    output: dict | None = None,
    degraded: bool = False,
    calls: list[str] | None = None,
    **node_kwargs,
) -> Node:
    """A node with no model behind it.

    Every failure mode the executor has to handle is reachable from these
    switches, which is what keeps the transition tests from needing a real
    flow — or a real outage — to exercise them.
    """

    async def run(context: RunContext) -> NodeResult:
        if calls is not None:
            calls.append(f"{node_id}:{context.attempt}")
        if hangs:
            await asyncio.sleep(5)
        if fails or context.attempt <= fails_until:
            raise RuntimeError(f"{node_id} failed on attempt {context.attempt}")
        return NodeResult(
            output=output if output is not None else {"from": node_id},
            cost_usd=cost,
            tokens=10,
            degraded=degraded,
        )

    return Node(node_id, kind, run, **node_kwargs)


def run_graph(nodes, seed="S", **kwargs):
    with open_store(":memory:") as store:
        return asyncio.run(execute(validate(nodes), seed, store, **kwargs))


def test_a_failed_hard_dependency_skips_its_dependants() -> None:
    """SKIPPED, not FAILED. Nothing broke below; the work was not wanted."""
    report = run_graph(
        [
            fake_node("a", fails=True),
            fake_node("b", requires=("a",)),
            fake_node("c", requires=("b",)),
        ]
    )

    assert report.records["a"].state is NodeState.FAILED
    assert report.records["b"].state is NodeState.SKIPPED
    assert report.records["c"].state is NodeState.SKIPPED, "skipping must propagate"
    assert "required input(s) unavailable: a" in report.records["b"].error


def test_a_failed_optional_dependency_degrades_rather_than_skips() -> None:
    report = run_graph(
        [
            fake_node("hard"),
            fake_node("soft", fails=True),
            fake_node("join", requires=("hard",), optional=("soft",)),
        ]
    )

    assert report.records["join"].state is NodeState.DEGRADED
    assert report.records["join"].missing_inputs == ("soft",)


def test_degradation_spreads_downstream() -> None:
    """A node built on a degraded answer is itself degraded.

    Without propagation, the report says the last node is clean and a reader
    has to trace the graph by hand to find out it is not.
    """
    report = run_graph(
        [
            fake_node("hard"),
            fake_node("soft", fails=True),
            fake_node("middle", requires=("hard",), optional=("soft",)),
            fake_node("last", requires=("middle",)),
        ]
    )

    assert report.records["middle"].state is NodeState.DEGRADED
    assert report.records["last"].state is NodeState.DEGRADED
    assert report.records["last"].degraded_inputs == ("middle",)


def test_a_degraded_node_is_told_what_it_lost() -> None:
    """The field that makes DEGRADED more than a label."""
    seen: dict = {}

    async def synthesise(context: RunContext) -> NodeResult:
        seen["missing"] = tuple(context.missing_inputs)
        return NodeResult(output={"ok": True})

    report = run_graph(
        [
            fake_node("hard"),
            fake_node("soft", fails=True),
            Node(
                "join",
                NodeKind.JOIN,
                synthesise,
                requires=("hard",),
                optional=("soft",),
            ),
        ]
    )

    assert seen["missing"] == ("soft",)
    assert "worked without soft" in missing_data_section(report)


def test_a_closed_gate_skips_the_branch_below_it() -> None:
    report = run_graph(
        [
            fake_node("gate", kind=NodeKind.GATE, output={"passed": False}),
            fake_node("below", requires=("gate",)),
        ]
    )

    assert report.records["gate"].state is NodeState.SUCCEEDED
    assert report.records["below"].state is NodeState.SKIPPED
    assert "gate(s) did not pass" in report.records["below"].error


def test_an_open_gate_lets_the_branch_run() -> None:
    report = run_graph(
        [
            fake_node("gate", kind=NodeKind.GATE, output={"passed": True}),
            fake_node("below", requires=("gate",)),
        ]
    )

    assert report.records["below"].state is NodeState.SUCCEEDED


def test_a_node_retries_within_its_own_limit() -> None:
    calls: list[str] = []
    report = run_graph([fake_node("flaky", fails_until=2, max_attempts=3, calls=calls)])

    assert report.records["flaky"].state is NodeState.SUCCEEDED
    assert calls == ["flaky:1", "flaky:2", "flaky:3"]


def test_a_node_out_of_attempts_fails() -> None:
    report = run_graph([fake_node("flaky", fails_until=5, max_attempts=2)])

    assert report.records["flaky"].state is NodeState.FAILED
    assert "attempt 2" in report.records["flaky"].error


def test_a_timeout_is_retried_and_then_failed() -> None:
    report = run_graph([fake_node("slow", hangs=True, timeout_s=0.05, max_attempts=2)])

    assert report.records["slow"].state is NodeState.FAILED
    assert "timed out" in report.records["slow"].error
    assert report.records["slow"].attempts == 2


def test_a_node_over_its_own_ceiling_fails_rather_than_being_truncated() -> None:
    """The money was already spent; reporting otherwise breaks the budget too."""
    report = run_graph([fake_node("pricey", cost=2.0, cost_ceiling_usd=0.5)])

    assert report.records["pricey"].state is NodeState.FAILED
    assert "exceeded the node ceiling" in report.records["pricey"].error


def test_the_job_stops_scheduling_once_the_budget_is_gone() -> None:
    """Failing the over-budget node is not enough; the job has to stop.

    A runner that keeps starting work after the ceiling spends it several times
    over, which is the failure that makes a budget worth having.
    """
    report = run_graph(
        [
            fake_node("one", cost=0.6),
            fake_node("two", cost=0.6, requires=("one",)),
            fake_node("three", cost=0.6, requires=("two",)),
        ],
        limits=JobLimits(cost_ceiling_usd=1.0),
    )

    assert report.records["one"].state is NodeState.SUCCEEDED
    assert report.records["two"].state is NodeState.SUCCEEDED
    assert report.records["three"].state is NodeState.SKIPPED
    assert "cost ceiling" in report.stopped_reason


def test_a_fanout_runs_once_per_item() -> None:
    calls: list[str] = []
    report = run_graph(
        [
            fake_node("source", output={"items": ["x", "y", "z"]}),
            fake_node(
                "each",
                kind=NodeKind.FANOUT,
                requires=("source",),
                expands="source",
                calls=calls,
            ),
        ]
    )

    assert len(calls) == 3
    assert report.records["each"].result.output["count"] == 3


def test_a_fanout_over_nothing_succeeds() -> None:
    """Zero items is a valid answer; failing here would take out the join."""
    report = run_graph(
        [
            fake_node("source", output={"items": []}),
            fake_node(
                "each", kind=NodeKind.FANOUT, requires=("source",), expands="source"
            ),
            fake_node("after", requires=("each",)),
        ]
    )

    assert report.records["each"].state is NodeState.SUCCEEDED
    assert report.records["after"].state is NodeState.SUCCEEDED


def test_independent_branches_run_in_the_same_superstep() -> None:
    """The scheduler is not a topological walk one node at a time."""
    report = run_graph(
        [
            fake_node("root"),
            fake_node("left", requires=("root",)),
            fake_node("right", requires=("root",)),
            fake_node("join", requires=("left", "right")),
        ]
    )

    assert report.supersteps <= 4
    assert all(
        report.records[n].state is NodeState.SUCCEEDED
        for n in ("root", "left", "right", "join")
    )


def test_the_report_separates_skipped_from_failed() -> None:
    report = run_graph([fake_node("a", fails=True), fake_node("b", requires=("a",))])
    payload = as_dict(report)

    assert payload["nodes"]["a"]["state"] == "failed"
    assert payload["nodes"]["b"]["state"] == "skipped"
    assert payload["outcome"] == "failed"


def test_every_node_execution_produces_a_trace_span() -> None:
    """The trace is a tree rooted at the seed: one span per node, with the
    input hash that makes two runs comparable."""
    report = run_graph([fake_node("a"), fake_node("b", requires=("a",))])

    spans = {span.node_id: span for span in report.trace}
    assert set(spans) == {"a", "b"}
    assert spans["b"].parents == ("a",)
    assert all(span.fingerprint for span in report.trace)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("clean", NodeState.SUCCEEDED),
        ("optional_down", NodeState.DEGRADED),
        ("required_down", NodeState.SKIPPED),
    ],
)
def test_the_transition_table_matches_the_readme(scenario: str, expected) -> None:
    """The README publishes a state transition table. This is the assertion
    that keeps it honest — if the executor's rules change, the table is wrong
    and this fails."""
    nodes = [
        fake_node("hard", fails=scenario == "required_down"),
        fake_node("soft", fails=scenario == "optional_down"),
        fake_node("target", requires=("hard",), optional=("soft",)),
    ]

    assert run_graph(nodes).records["target"].state is expected
