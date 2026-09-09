"""The executor: supersteps, state transitions, budgets and compensation.

The loop is deliberately small. Each pass over the graph does three things —
work out which pending nodes have become runnable or unrunnable, run the
runnable ones concurrently, record what happened — and stops when a pass changes
nothing. Everything else in this file is one of the rules that make the states
in `types.py` mean what they say.

The rules, in the order they bite:

1. A node whose `requires` contains a FAILED or SKIPPED node is SKIPPED. Not
   failed — nothing broke, the work simply is not wanted any more.
2. A node behind a GATE that did not pass is SKIPPED, for the same reason.
3. A node whose `optional` inputs are missing or degraded runs anyway and is
   DEGRADED, and it is TOLD which inputs it lost. Degradation spreads: a node
   with a degraded input is degraded unless it never used that input.
4. A node that raises or times out returns to READY while attempts remain, and
   becomes FAILED when they run out.
5. A node that would take the job over its cost ceiling is FAILED before it
   runs, and the job stops scheduling new work. Stopping matters more than
   failing: a runaway job that keeps starting nodes after the budget is gone
   spends the ceiling several times over.
6. When anything ends FAILED, every COMPENSATE node whose target succeeded runs.
   If a compensation fails, the job is marked `needs_human` and nothing else is
   attempted — a second automatic recovery layer is how one bad state becomes
   two.

Concurrency is `asyncio.gather` over one superstep. That is enough for this
scope and it is honest about the limit: one process, no queue, no worker pool.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .fingerprint import fingerprint as compute_fingerprint
from .store import ResultStore
from .types import (
    USABLE_STATES,
    Node,
    NodeKind,
    NodeResult,
    NodeState,
    RunContext,
)

__all__ = ["JobLimits", "NodeRecord", "RunReport", "execute"]


@dataclass(frozen=True, slots=True)
class JobLimits:
    """Ceilings for the whole job, on top of each node's own."""

    cost_ceiling_usd: float = 5.0
    max_supersteps: int = 50
    """A hard stop on the loop.

    Not a safety net for cycles — the validator catches those — but for a bug in
    the transition rules that leaves a node oscillating. A runner that can loop
    forever will, on a Friday.
    """


@dataclass
class NodeRecord:
    """Everything the report needs to know about one node."""

    node_id: str
    kind: NodeKind
    state: NodeState = NodeState.PENDING
    result: NodeResult | None = None
    attempts: int = 0
    fingerprint: str = ""
    from_cache: bool = False
    degraded_inputs: tuple[str, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    error: str = ""
    seconds: float = 0.0

    @property
    def cost_usd(self) -> float:
        return self.result.cost_usd if self.result else 0.0

    @property
    def tokens(self) -> int:
        return self.result.tokens if self.result else 0


@dataclass
class TraceSpan:
    """One node's span in the run's trace tree.

    The trace is a tree rooted at the seed, with one span per node execution and
    the attributes a reader actually needs when a job goes wrong: which node,
    what state, which attempt, what it cost, and the hash of its inputs. The
    input hash is what makes two runs comparable — the same fingerprint with a
    different outcome is the only shape that says "this node is flaky" rather
    than "the inputs changed".
    """

    node_id: str
    state: str
    attempt: int
    seconds: float
    cost_usd: float
    fingerprint: str
    from_cache: bool
    parents: tuple[str, ...] = ()


@dataclass
class RunReport:
    """The outcome of one job."""

    seed: str
    records: dict[str, NodeRecord] = field(default_factory=dict)
    trace: list[TraceSpan] = field(default_factory=list)
    needs_human: bool = False
    stopped_reason: str = "completed"
    supersteps: int = 0
    cache_hits: int = 0
    spent_usd: float = 0.0
    """What THIS run actually paid for.

    Distinct from `total_cost_usd`, which is what the result cost to produce
    across every run that contributed to it. A resumed job that hits the cache
    for everything reports the same total and a spend of zero, and the gap
    between the two is what the store saved.
    """
    sinks: tuple[str, ...] = ()
    """Nodes nothing depends on — the ones that produce the job's output.

    The job's verdict is read off these rather than off "did anything fail",
    because an optional specialist failing is not the job failing. What decides
    whether the work got done is whether the work at the end of the graph got
    done.
    """

    @property
    def total_cost_usd(self) -> float:
        return round(sum(record.cost_usd for record in self.records.values()), 6)

    @property
    def total_tokens(self) -> int:
        return sum(record.tokens for record in self.records.values())

    def by_state(self, state: NodeState) -> list[str]:
        return sorted(r.node_id for r in self.records.values() if r.state is state)

    @property
    def degraded(self) -> bool:
        return bool(self.by_state(NodeState.DEGRADED))

    @property
    def failed(self) -> bool:
        return bool(self.by_state(NodeState.FAILED))


async def execute(
    nodes: Sequence[Node],
    seed: str,
    store: ResultStore | None = None,
    *,
    limits: JobLimits | None = None,
) -> RunReport:
    """Run the graph. The graph must already have been validated."""
    limits = limits or JobLimits()
    report = RunReport(seed=seed)
    depended_on = {dependency for node in nodes for dependency in node.dependencies}
    report.sinks = tuple(
        sorted(
            node.id
            for node in nodes
            if node.id not in depended_on and node.kind is not NodeKind.COMPENSATE
        )
    )
    by_id = {node.id: node for node in nodes}
    records = {node.id: NodeRecord(node_id=node.id, kind=node.kind) for node in nodes}
    report.records = records

    # COMPENSATE nodes are never scheduled by dependencies; they wait for a
    # failure. Marking them here keeps the readiness rule below simple.
    for node in nodes:
        if node.kind is NodeKind.COMPENSATE:
            records[node.id].state = NodeState.PENDING

    spent = 0.0
    budget_exhausted = False

    for superstep in range(1, limits.max_supersteps + 1):
        report.supersteps = superstep
        _settle_unrunnable(nodes, by_id, records)
        ready = [
            node
            for node in nodes
            if node.kind is not NodeKind.COMPENSATE
            and records[node.id].state is NodeState.PENDING
            and _dependencies_settled(node, records)
        ]
        if not ready:
            break

        for node in ready:
            records[node.id].state = NodeState.READY

        if budget_exhausted:
            for node in ready:
                records[node.id].state = NodeState.SKIPPED
                records[node.id].error = "job cost ceiling reached before this node ran"
            continue

        outcomes = await asyncio.gather(
            *(_run_node(node, seed, records, store, report) for node in ready)
        )
        for node, cost in zip(ready, outcomes, strict=True):
            spent += cost
            report.spent_usd = round(spent, 6)
            if spent > limits.cost_ceiling_usd:
                budget_exhausted = True
                report.stopped_reason = (
                    f"job cost ceiling of ${limits.cost_ceiling_usd:.2f} reached "
                    f"after {node.id} (${spent:.4f} spent)"
                )
    else:
        report.stopped_reason = (
            f"stopped after {limits.max_supersteps} supersteps; the transition "
            "rules are not converging"
        )

    await _compensate(nodes, by_id, records, seed, report)
    if store is not None:
        report.cache_hits = store.hits
    return report


def _dependencies_settled(node: Node, records: Mapping[str, NodeRecord]) -> bool:
    """True when every dependency has reached a terminal state."""
    from .types import TERMINAL_STATES

    return all(
        records[dependency].state in TERMINAL_STATES for dependency in node.dependencies
    )


def _settle_unrunnable(
    nodes: Sequence[Node],
    by_id: Mapping[str, Node],
    records: dict[str, NodeRecord],
) -> None:
    """Mark everything that can no longer run, and keep marking until it stops.

    Iterated to a fixed point rather than done once per superstep: skipping a
    node makes its own dependants unrunnable, and doing that one layer per
    superstep would let the executor spend supersteps discovering work it was
    never going to do.
    """
    changed = True
    while changed:
        changed = False
        for node in nodes:
            record = records[node.id]
            if (
                record.state is not NodeState.PENDING
                or node.kind is NodeKind.COMPENSATE
            ):
                continue

            blocked = [
                dependency
                for dependency in node.requires
                if records[dependency].state in (NodeState.FAILED, NodeState.SKIPPED)
            ]
            if blocked:
                record.state = NodeState.SKIPPED
                record.error = f"required input(s) unavailable: {', '.join(blocked)}"
                changed = True
                continue

            closed_gates = [
                dependency
                for dependency in node.dependencies
                if by_id[dependency].kind is NodeKind.GATE
                and records[dependency].state in USABLE_STATES
                and not (records[dependency].result or NodeResult()).output.get(
                    "passed", True
                )
            ]
            if closed_gates:
                record.state = NodeState.SKIPPED
                record.error = f"gate(s) did not pass: {', '.join(closed_gates)}"
                changed = True


async def _run_node(
    node: Node,
    seed: str,
    records: dict[str, NodeRecord],
    store: ResultStore | None,
    report: RunReport,
) -> float:
    """Run one node to a terminal state. Returns what it cost."""
    record = records[node.id]
    inputs = {
        dependency: records[dependency].result
        for dependency in node.dependencies
        if records[dependency].state in USABLE_STATES
        and records[dependency].result is not None
    }
    degraded_inputs = tuple(
        dependency
        for dependency in node.dependencies
        if records[dependency].state is NodeState.DEGRADED
    )
    missing_inputs = tuple(
        dependency
        for dependency in node.optional
        if records[dependency].state in (NodeState.FAILED, NodeState.SKIPPED)
    )
    record.degraded_inputs = degraded_inputs
    record.missing_inputs = missing_inputs

    items: list[object | None] = [None]
    if node.kind is NodeKind.FANOUT and node.expands:
        expanded = inputs.get(node.expands)
        items = list(expanded.output.get("items", [])) if expanded else []
        if not items:
            # A fan-out over nothing is not a failure: zero items is a valid
            # answer, and failing here would take out the join below it.
            record.state = NodeState.SUCCEEDED
            record.result = NodeResult(
                output={"results": [], "count": 0},
                note=f"nothing to expand from {node.expands}",
            )
            return 0.0

    started = time.perf_counter()
    total_cost = 0.0
    branch_results: list[NodeResult] = []

    for item in items:
        context_fingerprint = compute_fingerprint(node, inputs, seed, item=item)
        if not record.fingerprint:
            record.fingerprint = context_fingerprint

        cached = store.get(seed, node.id, context_fingerprint) if store else None
        if cached is not None:
            record.from_cache = True
            branch_results.append(cached)
            continue

        outcome = await _attempt(
            node, seed, inputs, degraded_inputs, missing_inputs, item, record
        )
        if outcome is None:
            record.state = NodeState.FAILED
            record.seconds = round(time.perf_counter() - started, 4)
            _trace(report, node, record)
            return total_cost

        total_cost += outcome.cost_usd
        branch_results.append(outcome)
        if store is not None:
            state = NodeState.DEGRADED if outcome.degraded else NodeState.SUCCEEDED
            store.put(
                seed, node.id, context_fingerprint, state, outcome, record.attempts
            )

    result = _merge(node, branch_results)
    is_degraded = (
        result.degraded
        or bool(degraded_inputs)
        or bool(missing_inputs)
        or any(branch.degraded for branch in branch_results)
    )
    record.result = result
    record.state = NodeState.DEGRADED if is_degraded else NodeState.SUCCEEDED
    record.seconds = round(time.perf_counter() - started, 4)
    _trace(report, node, record)
    return total_cost


async def _attempt(
    node: Node,
    seed: str,
    inputs: Mapping[str, NodeResult],
    degraded_inputs: tuple[str, ...],
    missing_inputs: tuple[str, ...],
    item: object | None,
    record: NodeRecord,
) -> NodeResult | None:
    """Run one branch, retrying within the node's own limits.

    Returns None when the node is out of attempts or over its own ceiling; the
    caller turns that into FAILED.
    """
    for attempt in range(1, node.max_attempts + 1):
        record.attempts = attempt
        context = RunContext(
            seed=seed,
            node_id=node.id,
            inputs=inputs,
            degraded_inputs=degraded_inputs,
            missing_inputs=missing_inputs,
            attempt=attempt,
            item=item,
        )
        try:
            result = await asyncio.wait_for(node.run(context), timeout=node.timeout_s)
        except TimeoutError:
            record.error = f"timed out after {node.timeout_s}s on attempt {attempt}"
            continue
        except Exception as error:  # noqa: BLE001 - the node's failure is data
            record.error = f"{type(error).__name__}: {error}"
            continue

        if result.cost_usd > node.cost_ceiling_usd:
            # Reported as a failure rather than truncated: a node that cost more
            # than its ceiling already spent the money, and pretending otherwise
            # would make the budget report wrong as well as the run.
            record.error = (
                f"cost ${result.cost_usd:.4f} exceeded the node ceiling of "
                f"${node.cost_ceiling_usd:.4f}"
            )
            return None
        record.error = ""
        return result
    return None


def _merge(node: Node, branches: Sequence[NodeResult]) -> NodeResult:
    """Combine the branches of a fan-out into one result.

    A plain node has exactly one branch and is returned unchanged; the merge
    only matters for FANOUT, where the shape has to be predictable for whatever
    JOIN reads it.
    """
    if node.kind is not NodeKind.FANOUT:
        return branches[0] if branches else NodeResult()
    return NodeResult(
        output={
            "results": [dict(branch.output) for branch in branches],
            "count": len(branches),
        },
        cost_usd=round(sum(branch.cost_usd for branch in branches), 6),
        tokens=sum(branch.tokens for branch in branches),
        evidence=tuple(item for branch in branches for item in branch.evidence),
        degraded=any(branch.degraded for branch in branches),
        note=f"{len(branches)} branch(es)",
    )


def _downstream_of(node_id: str, nodes: Sequence[Node]) -> set[str]:
    """Every node that depends on this one, transitively."""
    affected: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node.id in affected or node.kind is NodeKind.COMPENSATE:
                continue
            if node_id in node.dependencies or affected & set(node.dependencies):
                affected.add(node.id)
                changed = True
    return affected


def _needs_compensating(
    target_id: str,
    nodes: Sequence[Node],
    records: Mapping[str, NodeRecord],
) -> str:
    """Why this node's side effect has to be undone, or "" if it does not.

    Two triggers, and the second one is the one people get wrong:

    1. Something DOWNSTREAM of the node failed. The side effect happened, and
       the work it was part of did not complete — the classic saga case.
    2. The node ITSELF failed. A side effect that raised is not a side effect
       that did not happen: the send may have gone out and the acknowledgement
       may have been lost. Compensating is safe because the compensation is
       idempotent; assuming nothing happened is not.

    Deliberately NOT a trigger: a failure somewhere else in the graph that this
    node does not depend on and that does not depend on it. An optional case-law
    lookup timing out has no bearing on whether the opinion should have been
    sent, and retracting it because an unrelated branch failed would be worse
    than the outage.
    """
    target = records[target_id]
    if target.state is NodeState.FAILED:
        return f"{target_id} failed after it may already have taken effect"
    if target.state not in USABLE_STATES:
        return ""
    failed_below = sorted(
        node_id
        for node_id in _downstream_of(target_id, nodes)
        if records[node_id].state is NodeState.FAILED
    )
    if failed_below:
        return f"{', '.join(failed_below)} failed downstream of {target_id}"
    return ""


async def _compensate(
    nodes: Sequence[Node],
    by_id: Mapping[str, Node],
    records: dict[str, NodeRecord],
    seed: str,
    report: RunReport,
) -> None:
    """Undo side effects whose work did not survive.

    A compensation that fails escalates instead of retrying: two automatic
    recovery layers turn one bad state into two, and the second is always the
    one nobody has a runbook for.
    """
    for node in nodes:
        if node.kind is not NodeKind.COMPENSATE or not node.compensates:
            continue
        record = records[node.id]
        reason = _needs_compensating(node.compensates, nodes, records)
        if not reason:
            record.state = NodeState.SKIPPED
            record.error = (
                f"nothing downstream of {node.compensates} failed; "
                "no compensation needed"
            )
            continue

        target = records[node.compensates]
        previous_state = target.state
        target.state = NodeState.COMPENSATING
        record.attempts = 1
        context = RunContext(
            seed=seed,
            node_id=node.id,
            inputs={node.compensates: target.result or NodeResult()},
            attempt=1,
        )
        try:
            result = await asyncio.wait_for(node.run(context), timeout=node.timeout_s)
        except Exception as error:  # noqa: BLE001
            record.state = NodeState.FAILED
            record.error = f"{type(error).__name__}: {error}"
            target.state = previous_state
            report.needs_human = True
            report.stopped_reason = (
                f"compensation {node.id} failed ({reason}); the side effect of "
                f"{node.compensates} is still in place"
            )
            _trace(report, node, record)
            return

        record.result = result
        record.state = NodeState.COMPENSATED
        record.error = reason
        target.state = NodeState.COMPENSATED
        _trace(report, node, record)


def _trace(report: RunReport, node: Node, record: NodeRecord) -> None:
    report.trace.append(
        TraceSpan(
            node_id=node.id,
            state=record.state.value,
            attempt=record.attempts,
            seconds=record.seconds,
            cost_usd=record.cost_usd,
            fingerprint=record.fingerprint,
            from_cache=record.from_cache,
            parents=tuple(node.dependencies),
        )
    )
