"""Graph validation: every check, and the diagnosis it produces.

A validator whose error message does not name the problem is a validator people
route around, so each test asserts the message as well as the raising.
"""

from __future__ import annotations

import pytest

from aimai_workflows.dagrun.types import Node, NodeKind, NodeResult, RunContext
from aimai_workflows.dagrun.validate import (
    GraphError,
    topological_order,
    validate,
    warnings_for,
)


async def noop(context: RunContext) -> NodeResult:
    return NodeResult()


def task(node_id: str, **kwargs) -> Node:
    return Node(node_id, NodeKind.TASK, noop, **kwargs)


def test_a_valid_graph_returns_its_nodes() -> None:
    nodes = validate([task("a"), task("b", requires=("a",))])

    assert [n.id for n in nodes] == ["a", "b"]


def test_a_cycle_is_named() -> None:
    """Kahn's failure mode is the diagnosis: the nodes it could not place."""
    with pytest.raises(GraphError, match="cycle through: a, b"):
        validate([task("a", requires=("b",)), task("b", requires=("a",))])


def test_a_dangling_reference_is_caught() -> None:
    with pytest.raises(GraphError, match="depends on unknown node 'ghost'"):
        validate([task("a", requires=("ghost",))])


def test_a_duplicate_id_is_caught() -> None:
    """Two nodes with one id means the second silently overwrites the first."""
    with pytest.raises(GraphError, match="duplicate node ids: a"):
        validate([task("a"), task("a")])


def test_a_self_edge_is_caught() -> None:
    with pytest.raises(GraphError, match="depends on itself"):
        validate([task("a", requires=("a",))])


def test_an_edge_cannot_be_both_hard_and_soft() -> None:
    """The two have opposite failure semantics; declaring both is a real bug."""
    with pytest.raises(GraphError, match="both required and optional"):
        validate([task("a"), task("b", requires=("a",), optional=("a",))])


def test_a_compensation_needs_a_target_with_a_side_effect() -> None:
    with pytest.raises(GraphError, match="which has no side effect"):
        validate(
            [
                task("send"),
                Node("undo", NodeKind.COMPENSATE, noop, compensates="send"),
            ]
        )


def test_a_compensation_target_must_exist() -> None:
    with pytest.raises(GraphError, match="compensates unknown node"):
        validate([Node("undo", NodeKind.COMPENSATE, noop, compensates="ghost")])


def test_a_fanout_must_say_what_it_expands() -> None:
    with pytest.raises(GraphError, match="FANOUT node without `expands`"):
        validate([task("a"), Node("f", NodeKind.FANOUT, noop, requires=("a",))])


def test_a_fanout_can_only_expand_a_dependency() -> None:
    with pytest.raises(GraphError, match="not one of its dependencies"):
        validate(
            [
                task("a"),
                task("b"),
                Node("f", NodeKind.FANOUT, noop, requires=("a",), expands="b"),
            ]
        )


def test_the_topological_order_is_stable() -> None:
    """An order that varies between runs makes a dry run's output meaningless."""
    nodes = [task("c", requires=("a", "b")), task("b"), task("a")]

    assert topological_order(nodes) == topological_order(nodes)
    assert topological_order(nodes)[-1] == "c"


def test_a_side_effect_without_a_compensation_warns_rather_than_raises() -> None:
    """A warning that raises trains people to skip validation."""
    nodes = [task("send", side_effect=True)]

    validate(nodes)
    warnings = warnings_for(nodes)

    assert any("cannot be rolled back" in w for w in warnings)


def test_retries_on_a_side_effect_warn_about_idempotency() -> None:
    nodes = [
        task("send", side_effect=True, max_attempts=3),
        Node("undo", NodeKind.COMPENSATE, noop, compensates="send"),
    ]

    assert any("idempotent" in w for w in warnings_for(nodes))
