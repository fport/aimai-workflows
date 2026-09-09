"""The contract all four stacks implement, and the CLI they all expose.

The comparison only means anything if the four versions are interchangeable
from the outside. So the shape is fixed here — two verbs, one result type — and
`tests/test_stack_contract.py` runs the same parametrized suite over all four.
A stack that needs a different shape is telling you something about the
framework, and that is the finding, not an excuse to special-case it.

    python -m aimai_workflows.stacks.plain run T-1000
    python -m aimai_workflows.stacks.plain approve T-1000 --decision approve
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Literal, Protocol

__all__ = ["Decision", "RunOutcome", "SupportStack", "run_cli"]

Decision = Literal["approve", "reject"]


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What a stack reports after `run` or `approve`.

    `llm_calls` is here rather than in each framework's telemetry because every
    framework counts something slightly different — turns, spans, requests — and
    the benchmark has to count the same thing four times: requests that left for
    a model.
    """

    ticket_id: str
    stack: str
    status: Literal["completed", "awaiting_approval", "rejected", "unknown"]
    risk: str = "unknown"
    receipt: str | None = None
    draft: str | None = None
    llm_calls: int = 0
    resumed: bool = False
    """True when this outcome came from reading state written by another process."""
    detail: dict = field(default_factory=dict)

    def as_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


class SupportStack(Protocol):
    """Two verbs. Everything a framework adds beyond them is its own business."""

    name: str

    def run(self, ticket_id: str) -> RunOutcome:
        """Triage, draft, and either send or stop for a human."""
        ...

    def approve(self, ticket_id: str, decision: Decision = "approve") -> RunOutcome:
        """Answer a stopped run. Must work in a process that did not start it."""
        ...


def run_cli(build: object, argv: list[str] | None = None) -> int:
    """The shared entry point. `build` is a zero-argument stack factory.

    Shared so the CLI cannot drift between stacks — a difference in output shape
    would show up in the benchmark as a difference between frameworks.
    """
    parser = argparse.ArgumentParser(description="Run one support ticket.")
    parser.add_argument("command", choices=["run", "approve"])
    parser.add_argument("ticket_id")
    parser.add_argument("--decision", default="approve", choices=["approve", "reject"])
    args = parser.parse_args(argv)

    stack = build()  # type: ignore[operator]
    outcome = (
        stack.run(args.ticket_id)
        if args.command == "run"
        else stack.approve(args.ticket_id, args.decision)
    )
    print(outcome.as_json())
    # A non-zero exit for a ticket still waiting on a human, so a shell script
    # driving this can tell "done" from "someone has to look at it".
    return 0 if outcome.status in ("completed", "rejected") else 2


def state_db(stack: str) -> str:
    """Where a stack keeps whatever it has to keep between the two verbs.

    One file per stack, under one environment variable, so the chaos script can
    point all four at a temporary directory and kill processes without touching
    a developer's own runs.
    """
    root = os.getenv("SUPPORT_STATE_DIR", ".")
    return os.path.join(root, f".aimai-{stack}.sqlite3")
