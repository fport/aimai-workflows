"""Graph validation, before anything runs.

Every check here answers a failure that is expensive at runtime and free at
startup. A cycle is an executor that never terminates. A dangling reference is a
node that waits forever on something that does not exist. A duplicate id is two
nodes writing to the same result key, where the second silently wins.

Kahn's algorithm rather than a depth-first search: it produces the topological
order the dry run prints, and its failure mode is exactly the diagnosis you
want — the nodes it could not place ARE the cycle.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence

from .types import Node, NodeKind

__all__ = ["GraphError", "topological_order", "validate", "warnings_for"]


class GraphError(Exception):
    """The graph cannot be executed. Raised before any node runs."""


def validate(nodes: Iterable[Node]) -> tuple[Node, ...]:
    """Check the graph and return its nodes in a stable order.

    Returns rather than mutates, so a caller cannot half-validate a graph and
    run it anyway.
    """
    nodes = tuple(nodes)
    _check_unique_ids(nodes)
    _check_references(nodes)
    _check_compensation(nodes)
    _check_fanout(nodes)
    topological_order(nodes)  # raises on a cycle
    return nodes


def _check_unique_ids(nodes: Sequence[Node]) -> None:
    seen: set[str] = set()
    duplicates = sorted({n.id for n in nodes if n.id in seen or seen.add(n.id)})
    if duplicates:
        raise GraphError(f"duplicate node ids: {', '.join(duplicates)}")


def _check_references(nodes: Sequence[Node]) -> None:
    known = {n.id for n in nodes}
    problems = [
        f"{node.id} depends on unknown node {reference!r}"
        for node in nodes
        for reference in node.dependencies
        if reference not in known
    ]
    overlapping = [
        f"{node.id} lists {reference!r} as both required and optional"
        for node in nodes
        for reference in set(node.requires) & set(node.optional)
    ]
    self_edges = [
        f"{node.id} depends on itself" for node in nodes if node.id in node.dependencies
    ]
    if problems or overlapping or self_edges:
        raise GraphError("; ".join([*problems, *overlapping, *self_edges]))


def _check_compensation(nodes: Sequence[Node]) -> None:
    known = {n.id: n for n in nodes}
    problems: list[str] = []
    for node in nodes:
        if node.kind is NodeKind.COMPENSATE:
            if not node.compensates:
                problems.append(f"{node.id} is a COMPENSATE node with no target")
            elif node.compensates not in known:
                problems.append(
                    f"{node.id} compensates unknown node {node.compensates!r}"
                )
            elif not known[node.compensates].side_effect:
                # Not fatal in principle, but it is always a mistake: something
                # is being undone that never did anything.
                problems.append(
                    f"{node.id} compensates {node.compensates!r}, which has no "
                    "side effect"
                )
        elif node.compensates:
            problems.append(
                f"{node.id} names a compensation target but is a {node.kind}"
            )
    if problems:
        raise GraphError("; ".join(problems))


def _check_fanout(nodes: Sequence[Node]) -> None:
    problems = [
        f"{node.id} is a FANOUT node without `expands`"
        for node in nodes
        if node.kind is NodeKind.FANOUT and not node.expands
    ]
    problems += [
        f"{node.id} expands {node.expands!r}, which is not one of its dependencies"
        for node in nodes
        if node.kind is NodeKind.FANOUT
        and node.expands
        and node.expands not in node.dependencies
    ]
    if problems:
        raise GraphError("; ".join(problems))


def topological_order(nodes: Sequence[Node]) -> tuple[str, ...]:
    """Kahn's algorithm. Raises `GraphError` naming the cycle.

    COMPENSATE nodes are ordered too, even though the executor never schedules
    them from dependencies: an unreachable compensation is still a graph error
    worth catching, and leaving them out would hide a cycle that runs through
    one.
    """
    by_id = {node.id: node for node in nodes}
    indegree = {node.id: len(set(node.dependencies)) for node in nodes}
    dependants: dict[str, list[str]] = {node.id: [] for node in nodes}
    for node in nodes:
        for reference in set(node.dependencies):
            dependants[reference].append(node.id)

    # Sorted, so the order is stable across runs. An unstable topological order
    # makes a dry run's output differ between machines for no reason.
    queue = deque(sorted(node_id for node_id, degree in indegree.items() if not degree))
    order: list[str] = []
    while queue:
        node_id = queue.popleft()
        order.append(node_id)
        for dependant in sorted(dependants[node_id]):
            indegree[dependant] -= 1
            if indegree[dependant] == 0:
                queue.append(dependant)

    if len(order) != len(by_id):
        stuck = sorted(set(by_id) - set(order))
        raise GraphError(
            f"the graph has a cycle through: {', '.join(stuck)}. "
            "Kahn's algorithm could not place these nodes, which means every "
            "one of them waits on another one of them."
        )
    return tuple(order)


def warnings_for(nodes: Sequence[Node]) -> list[str]:
    """Problems worth saying out loud that are not worth refusing to run.

    Kept separate from `validate` deliberately: a warning that raises trains
    people to skip validation, and a fatal error that only warns trains them to
    ignore the output.
    """
    warnings: list[str] = []
    compensated = {n.compensates for n in nodes if n.kind is NodeKind.COMPENSATE}
    for node in nodes:
        if node.side_effect and node.id not in compensated:
            warnings.append(
                f"{node.id} has a side effect and no COMPENSATE node; a failure "
                "downstream cannot be rolled back"
            )
        if node.max_attempts > 1 and node.side_effect:
            warnings.append(
                f"{node.id} has a side effect and max_attempts="
                f"{node.max_attempts}; make sure the effect is idempotent"
            )
    return warnings
