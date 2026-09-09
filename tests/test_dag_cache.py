"""Fingerprints and the result store: what gets reused, and what must not.

The claim under test is narrow and load-bearing: a second run of the same job
runs nothing, and a changed node reruns exactly itself and its subtree.
"""

from __future__ import annotations

import asyncio

from aimai_workflows.dagrun.execute import execute
from aimai_workflows.dagrun.fingerprint import fingerprint, subtree, version_from_files
from aimai_workflows.dagrun.store import open_store
from aimai_workflows.dagrun.types import (
    Node,
    NodeKind,
    NodeResult,
    NodeState,
    RunContext,
)
from aimai_workflows.dagrun.validate import validate


def counting_node(
    node_id: str, calls: list[str], *, version: str = "v1", **kwargs
) -> Node:
    async def run(context: RunContext) -> NodeResult:
        calls.append(node_id)
        return NodeResult(output={"from": node_id, "v": version}, cost_usd=0.05)

    return Node(node_id, NodeKind.TASK, run, version=version, **kwargs)


def chain(calls: list[str], *, middle_version: str = "v1") -> list[Node]:
    return [
        counting_node("a", calls),
        counting_node("b", calls, version=middle_version, requires=("a",)),
        counting_node("c", calls, requires=("b",)),
    ]


def test_the_second_run_of_the_same_job_runs_nothing() -> None:
    calls: list[str] = []
    with open_store(":memory:") as store:
        asyncio.run(execute(validate(chain(calls)), "S1", store))
        assert calls == ["a", "b", "c"]

        calls.clear()
        report = asyncio.run(execute(validate(chain(calls)), "S1", store))

    assert calls == [], "a rerun of unchanged work must call no node"
    assert report.cache_hits == 3
    assert report.spent_usd == 0.0
    assert report.total_cost_usd == 0.15, "the result still cost what it cost"


def test_a_different_seed_shares_nothing() -> None:
    """Two documents must not share a cache."""
    calls: list[str] = []
    with open_store(":memory:") as store:
        asyncio.run(execute(validate(chain(calls)), "S1", store))
        calls.clear()
        asyncio.run(execute(validate(chain(calls)), "S2", store))

    assert calls == ["a", "b", "c"]


def test_changing_one_node_version_reruns_it_and_its_subtree() -> None:
    """And nothing above it. Rerunning the whole graph on any change is the
    behaviour that makes people turn the cache off."""
    calls: list[str] = []
    with open_store(":memory:") as store:
        asyncio.run(execute(validate(chain(calls)), "S1", store))
        calls.clear()
        asyncio.run(execute(validate(chain(calls, middle_version="v2")), "S1", store))

    assert calls == ["b", "c"], "a is upstream of the change and must not rerun"


def test_a_failure_is_not_cached() -> None:
    """Caching a transient failure makes it permanent."""
    attempts: list[str] = []

    def flaky(should_fail: bool) -> Node:
        async def run(context: RunContext) -> NodeResult:
            attempts.append("run")
            if should_fail:
                raise RuntimeError("transient")
            return NodeResult(output={"ok": True})

        return Node("x", NodeKind.TASK, run)

    with open_store(":memory:") as store:
        first = asyncio.run(execute(validate([flaky(True)]), "S1", store))
        second = asyncio.run(execute(validate([flaky(False)]), "S1", store))

    assert first.records["x"].state is NodeState.FAILED
    assert second.records["x"].state is NodeState.SUCCEEDED
    assert len(attempts) == 2


def test_a_degraded_result_is_cached() -> None:
    """Degraded is a usable answer; recomputing it would spend money to learn
    the same thing."""
    calls: list[str] = []

    def graph() -> list[Node]:
        async def soft(context: RunContext) -> NodeResult:
            raise RuntimeError("down")

        return [
            counting_node("hard", calls),
            Node("soft", NodeKind.TASK, soft),
            counting_node("join", calls, requires=("hard",), optional=("soft",)),
        ]

    with open_store(":memory:") as store:
        asyncio.run(execute(validate(graph()), "S1", store))
        calls.clear()
        asyncio.run(execute(validate(graph()), "S1", store))

    assert calls == [], "the degraded join must come from the cache"


def test_the_fingerprint_ignores_execution_policy() -> None:
    """Raising a timeout must not invalidate a graph's worth of results."""
    calls: list[str] = []
    base = counting_node("a", calls, timeout_s=10, max_attempts=1)
    relaxed = counting_node("a", calls, timeout_s=60, max_attempts=5)

    assert fingerprint(base, {}, "S1") == fingerprint(relaxed, {}, "S1")


def test_the_fingerprint_tracks_upstream_output() -> None:
    calls: list[str] = []
    node = counting_node("b", calls, requires=("a",))

    with_one = fingerprint(node, {"a": NodeResult(output={"x": 1})}, "S1")
    with_two = fingerprint(node, {"a": NodeResult(output={"x": 2})}, "S1")

    assert with_one != with_two


def test_a_missing_optional_input_is_part_of_the_identity() -> None:
    """The same node run with and without its optional input produced different
    answers; the cache must not confuse them."""
    calls: list[str] = []
    node = counting_node("b", calls, requires=("a",), optional=("opt",))
    inputs = {"a": NodeResult(output={"x": 1})}

    assert fingerprint(node, inputs, "S1") != fingerprint(
        node, inputs | {"opt": NodeResult(output={"y": 2})}, "S1"
    )


def test_key_order_does_not_change_the_fingerprint() -> None:
    """Without canonical serialisation the cache would never hit."""
    calls: list[str] = []
    node = counting_node("b", calls, requires=("a",))

    first = fingerprint(node, {"a": NodeResult(output={"x": 1, "y": 2})}, "S")
    second = fingerprint(node, {"a": NodeResult(output={"y": 2, "x": 1})}, "S")

    assert first == second


def test_version_from_files_changes_with_the_prompt(tmp_path) -> None:
    """A hand-maintained version goes stale the first time someone forgets."""
    prompt = tmp_path / "p.md"
    prompt.write_text("first")
    before = version_from_files(prompt)
    prompt.write_text("second")

    assert version_from_files(prompt) != before


def test_subtree_names_what_a_change_invalidates() -> None:
    calls: list[str] = []
    nodes = chain(calls) + [counting_node("side", calls)]

    assert subtree(["b"], nodes) == {"b", "c"}


def test_the_store_records_failures_for_the_report_but_never_serves_them() -> None:
    import sqlite3

    from aimai_workflows.dagrun.store import ResultStore

    store = ResultStore(sqlite3.connect(":memory:"))
    store.put("S", "x", "fp", NodeState.FAILED, NodeResult(note="boom"), 1)

    assert store.get("S", "x", "fp") is None
    assert store.history("S")[0]["state"] == "failed"
