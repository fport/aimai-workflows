"""The vocabulary of the DAG runner: node kinds, node states, and the result
contract.

Two decisions in this file are the whole stage.

**`requires` and `optional` are different edges.** A hard dependency that fails
takes its dependants with it — they are `SKIPPED`, which is a decision, not an
error. A soft dependency that fails leaves its dependants runnable but marks
them `DEGRADED`, and that mark spreads downstream. Without the distinction you
get one of two bad flows: everything is required, so one flaky lookup kills the
job; or nothing is, so a synthesis quietly reports a conclusion drawn from half
its evidence.

**Nodes exchange `NodeResult`, never free text.** An agent that hands the next
agent a paragraph forces that agent to parse prose, and the coordination layer
has nothing to route on. `status`, `cost_usd`, `tokens` and `evidence` are what
the runner needs; `output` is what the next node needs; `note` is for the human
reading the report.

The state table is the contract, and it is written out in the README. Where the
table and this file disagree, the table is lying — so `test_transitions.py`
asserts them against each other.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "Node",
    "NodeKind",
    "NodeResult",
    "NodeState",
    "RunContext",
    "TERMINAL_STATES",
    "USABLE_STATES",
]


class NodeKind(StrEnum):
    """What a node is for.

    The kind is not decoration: the executor treats each differently, and a
    graph that uses only TASK is a graph the runner cannot help with.
    """

    TASK = "task"
    """One unit of work. Runs when its dependencies allow it."""

    FANOUT = "fanout"
    """Produces a list, and runs its body once per item, in parallel.

    The item count is not known until the upstream node has run, which is
    exactly why this is a kind rather than a loop in a task: the executor has to
    schedule work that did not exist when the graph was validated.
    """

    JOIN = "join"
    """Merges several upstream results into one.

    Distinct from TASK because the merge has to see *which* inputs degraded, and
    a plain task receiving a dict of outputs cannot tell a missing input from an
    empty one.
    """

    GATE = "gate"
    """Decides whether the branch below it runs at all.

    Returns a boolean in `output["passed"]`. A false gate skips its dependants
    rather than failing them — nothing went wrong, the work was not wanted.
    """

    COMPENSATE = "compensate"
    """Undoes another node's side effect.

    Never scheduled by dependencies. It runs only when the node it compensates
    has succeeded and something downstream has since failed.
    """


class NodeState(StrEnum):
    """Where a node is. Nine states, and every one of them is reachable.

    `DEGRADED` and `SKIPPED` are the two that earn their place: without them a
    runner has to describe "ran, but on incomplete inputs" and "did not run,
    because we decided not to" as either success or failure, and both lies show
    up in the report.
    """

    PENDING = "pending"
    """Waiting on dependencies."""

    READY = "ready"
    """Dependencies allow it; waiting for a slot."""

    RUNNING = "running"
    """In flight."""

    SUCCEEDED = "succeeded"
    """Produced a result on complete inputs."""

    DEGRADED = "degraded"
    """Produced a result, but an optional input was missing or degraded.

    A usable outcome that must never be reported as clean. The state propagates:
    anything downstream of a degraded node is degraded too, unless it declares
    that it does not depend on what was lost.
    """

    FAILED = "failed"
    """Out of attempts, over budget, or raised something unrecoverable."""

    SKIPPED = "skipped"
    """Not run, because a hard dependency failed or a gate said no.

    Counted separately from FAILED in every report. Conflating the two makes a
    job that correctly declined to do work look like a job that broke.
    """

    COMPENSATING = "compensating"
    """Its compensation is running."""

    COMPENSATED = "compensated"
    """Its side effect has been undone."""


TERMINAL_STATES = frozenset(
    {
        NodeState.SUCCEEDED,
        NodeState.DEGRADED,
        NodeState.FAILED,
        NodeState.SKIPPED,
        NodeState.COMPENSATED,
    }
)

USABLE_STATES = frozenset({NodeState.SUCCEEDED, NodeState.DEGRADED})
"""States whose `output` a downstream node may read.

`DEGRADED` is in this set on purpose — degraded output is still output. What it
must not do is arrive unlabelled, which is why `RunContext.degraded_inputs`
exists.
"""


@dataclass(frozen=True, slots=True)
class NodeResult:
    """What every node returns. The contract between agents.

    `evidence` is separate from `output` because the two have different
    audiences: `output` is what the next node consumes, `evidence` is what a
    human checks when they disagree with the conclusion. A synthesis node that
    merges outputs and drops evidence produces an answer nobody can audit.
    """

    output: Mapping[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    tokens: int = 0
    note: str = ""
    evidence: Sequence[str] = ()
    degraded: bool = False
    """Set by the node itself when it knows its own answer is partial.

    Independent of the executor's propagation: a node can be degraded because
    an optional input was missing (the executor's judgement) or because it hit
    its own limit (its own judgement), and both have to reach the report.
    """


@dataclass(frozen=True, slots=True)
class RunContext:
    """What a node is given when it runs.

    `degraded_inputs` is the field that makes `DEGRADED` more than a label. A
    synthesis node that receives it can say "the case-law search returned
    nothing, so this conclusion rests on statute alone" — which is the sentence
    the whole propagation rule exists to make possible.
    """

    seed: str
    node_id: str
    inputs: Mapping[str, NodeResult]
    degraded_inputs: Sequence[str] = ()
    missing_inputs: Sequence[str] = ()
    attempt: int = 1
    item: Any = None
    """The element being processed, for one branch of a FANOUT."""

    def output(self, node_id: str) -> Mapping[str, Any]:
        """The output of one dependency, or an empty mapping if it did not run."""
        result = self.inputs.get(node_id)
        return result.output if result else {}


NodeRun = Callable[[RunContext], Awaitable[NodeResult]]


@dataclass(frozen=True, slots=True)
class Node:
    """One node in the graph. Frozen: a graph that mutates while it runs cannot
    be validated before it runs.

    `version` should come from the hash of whatever actually determines the
    node's behaviour — the prompt file, usually. A hand-maintained integer goes
    stale the first time someone edits a prompt without bumping it, and the
    result is a cache that serves answers from a prompt that no longer exists.
    """

    id: str
    kind: NodeKind
    run: NodeRun
    version: str = "v1"
    requires: tuple[str, ...] = ()
    """Hard dependencies. If one fails, this node is SKIPPED."""

    optional: tuple[str, ...] = ()
    """Soft dependencies. If one fails, this node runs and is DEGRADED."""

    compensates: str | None = None
    """For COMPENSATE nodes: the id of the node whose side effect this undoes."""

    expands: str | None = None
    """For FANOUT nodes: the dependency whose `output["items"]` to iterate."""

    max_attempts: int = 1
    timeout_s: float = 30.0
    cost_ceiling_usd: float = 1.0
    side_effect: bool = False
    """True when the node changes something outside the job.

    Used by the report and by the compensation rule: only a node with a side
    effect is worth compensating, and a graph with side effects but no
    COMPENSATE nodes is a graph that cannot be rolled back — which the validator
    warns about rather than silently accepting.
    """

    @property
    def dependencies(self) -> tuple[str, ...]:
        """Both kinds of edge, for topological ordering.

        Ordering does not care whether an edge is hard or soft; only the failure
        rules do.
        """
        return (*self.requires, *self.optional)
