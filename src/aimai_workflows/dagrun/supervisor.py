"""The same work, routed dynamically: a LangGraph supervisor.

Fifteen lines of routing rather than the `langgraph-supervisor` package. That
package is still in its 0.0.x band, and the whole value of this file is the
three constraints below — a dependency that owns the loop owns those
constraints too, and they are the part worth keeping.

**The model chooses; the code constrains.** The supervisor asks which
specialist should go next and then refuses three classes of answer:

1. A specialist that has already run. Without this, the cheapest failure mode
   of every supervisor appears immediately: the model asks for the statute
   specialist forever because its last answer was useful.
2. Anything past `MAX_STEPS`. A hard ceiling, not a prompt instruction. This is
   what keeps a bad routing decision from becoming an unbounded bill.
3. A name that is not on the list. The model gets a menu; if it invents an item,
   the router falls back to the first outstanding specialist rather than
   raising. A supervisor that crashes on a hallucinated name turns a recoverable
   routing mistake into a failed job.

**When to use this instead of the static graph** — the rule, in one line: if
the shape of the work is fixed, use the graph; if it is not, use a supervisor.
The due-diligence flow always extracts, always reviews clauses, and always asks
both specialists, so the graph is the right answer and this file exists to make
the comparison concrete rather than theoretical. A flow whose next step really
does depend on what the last step found — triage that sometimes needs a
valuation and sometimes needs a translation — is where this stops being
overhead.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

__all__ = [
    "MAX_STEPS",
    "SPECIALISTS",
    "SupervisorState",
    "build_supervisor",
    "merge_findings",
]

SPECIALISTS = ("statute", "caselaw")
MAX_STEPS = 6
"""A hard ceiling on routing decisions, enforced in code.

Six, for two specialists: enough for each to run and for the supervisor to
change its mind once. A ceiling that is generous enough never to bite is not a
ceiling.
"""


def merge_findings(
    current: dict[str, Any] | None, incoming: dict[str, Any] | None
) -> dict[str, Any]:
    """Merge specialist findings by specialist name.

    Order-independent by construction: two specialists writing different keys
    produce the same dictionary whichever arrives first, and a specialist
    writing its own key twice is idempotent. Concatenating into a list — the
    obvious first implementation — is neither, and under parallel execution it
    produces a different state on every run.
    """
    merged = dict(current or {})
    merged.update(incoming or {})
    return merged


class SupervisorState(TypedDict, total=False):
    """State shared by the supervisor and the specialists."""

    seed: str
    clauses: list[dict]
    findings: Annotated[dict[str, Any], merge_findings]
    visited: Annotated[list[str], operator.add]
    steps: int
    route_log: Annotated[list[str], operator.add]
    opinion: str


def _choose(
    state: SupervisorState, chooser
) -> tuple[Literal["statute", "caselaw", "synthesise"], str]:
    """Apply the three constraints to whatever the chooser returns."""
    visited = set(state.get("visited", []))
    outstanding = [name for name in SPECIALISTS if name not in visited]

    if state.get("steps", 0) >= MAX_STEPS:
        return "synthesise", f"step ceiling of {MAX_STEPS} reached"
    if not outstanding:
        return "synthesise", "every specialist has reported"

    choice = chooser(state, tuple(outstanding))
    if choice not in outstanding:
        # Not an error: a routing mistake is recoverable and a crash is not.
        return outstanding[0], f"{choice!r} is not available; fell back"
    return choice, "chosen"


def build_supervisor(chooser=None, specialists=None):
    """Compile the dynamic version.

    `chooser` stands in for the model's routing decision — a function of the
    state, returning a specialist name. Injected so the constraints can be
    tested against a chooser that misbehaves on purpose, which is the only way
    to know the constraints work.
    """
    chooser = chooser or (lambda state, outstanding: outstanding[0])
    specialists = specialists or {
        name: _default_specialist(name) for name in SPECIALISTS
    }

    def supervisor(state: SupervisorState) -> Command:
        target, reason = _choose(state, chooser)
        return Command(
            goto=target,
            update={
                "steps": state.get("steps", 0) + 1,
                "route_log": [f"{target}: {reason}"],
            },
        )

    def synthesise(state: SupervisorState) -> SupervisorState:
        findings = state.get("findings", {})
        missing = [name for name in SPECIALISTS if name not in findings]
        caveat = f" INCOMPLETE without {', '.join(missing)}." if missing else ""
        return {
            "opinion": " ".join(findings.get(n, "") for n in SPECIALISTS).strip()
            + caveat
        }

    graph = StateGraph(SupervisorState)
    graph.add_node("supervisor", supervisor)
    for name, body in specialists.items():
        graph.add_node(name, body)
        # Every specialist returns to the supervisor. That edge is what makes
        # this dynamic: the next step is decided after each result, not laid
        # out in advance.
        graph.add_edge(name, "supervisor")
    graph.add_node("synthesise", synthesise)
    graph.add_edge(START, "supervisor")
    graph.add_edge("synthesise", END)
    return graph.compile()


def _default_specialist(name: str):
    def run(state: SupervisorState) -> SupervisorState:
        return {
            "findings": {
                name: f"{name} reviewed {len(state.get('clauses', []))} clause(s)"
            },
            "visited": [name],
        }

    return run
