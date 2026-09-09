"""Run the same 50 tickets through all four stacks and produce the table.

    uv run stack-bench                  # writes results/bench.md

Fairness rules, because a benchmark that is not obviously fair is not evidence:

- Every stack gets the SAME tickets in the SAME order, and the same
  deterministic model. No stack sees a different classification.
- The business logic is one import (`core`), so nothing here measures four
  implementations of the same idea.
- The counters are computed from the OUTBOX, not from what a stack reports
  about itself. `duplicate_sends` in particular is the number of extra rows the
  customer would have received, which is the only definition that matters.
- Each stack runs against its own state file, wiped first, so a previous run
  cannot make a stack look idempotent.

`llm_calls` is the one column that is not a fairness issue but a finding: the
two graph versions spend exactly what the business logic costs, and the two
agent versions spend that plus one model turn per tool call.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import statistics
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from aimai_kit.prompts import PromptRegistry

from .contract import RunOutcome
from .core import REPO_ROOT, load_tickets, reset_outbox, sent_replies
from .stub_support import RuleBasedSupportModel

__all__ = ["StackMeasurement", "benchmark", "main", "orchestration_lines"]

RESULTS = REPO_ROOT / "results"


@dataclass
class StackMeasurement:
    """One row of the table."""

    stack: str
    completed: int = 0
    escalated: int = 0
    resumed_after_restart: int = 0
    duplicate_sends: int = 0
    llm_calls: int = 0
    orchestration_lines: int = 0
    seconds: float = 0.0
    replies_sent: int = 0


def stack_factories() -> dict[str, object]:
    """Imported lazily: two of the four pull in a third-party framework, and a
    reader who only wants the plain version should not have to install them."""
    from . import langgraph_stack, openai_agents_stack, plain, pydantic_ai_stack

    return {
        plain.NAME: plain.PlainStack,
        langgraph_stack.NAME: langgraph_stack.LangGraphStack,
        pydantic_ai_stack.NAME: pydantic_ai_stack.PydanticAIStack,
        openai_agents_stack.NAME: openai_agents_stack.OpenAIAgentsStack,
    }


def orchestration_lines(module_path: Path) -> int:
    """Count the orchestration code, excluding docstrings, comments and blanks.

    The point of the column is "how much code does this framework make you
    write to get a durable, resumable, approvable flow" — so the shared business
    logic is excluded by construction (it is not in these files) and so is
    prose. Counting comment lines would reward the version with the most
    explaining to do, which is the opposite of the intent.
    """
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                docstring_lines.update(
                    range(node.lineno, (node.end_lineno or node.lineno) + 1)
                )
    count = 0
    for number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or number in docstring_lines:
            continue
        count += 1
    return count


def benchmark(name: str, factory, registry: PromptRegistry) -> StackMeasurement:
    """One stack, 50 tickets, then the same 50 again."""
    from . import langgraph_stack, openai_agents_stack, plain, pydantic_ai_stack

    modules = {
        plain.NAME: plain,
        langgraph_stack.NAME: langgraph_stack,
        pydantic_ai_stack.NAME: pydantic_ai_stack,
        openai_agents_stack.NAME: openai_agents_stack,
    }
    measurement = StackMeasurement(
        stack=name,
        orchestration_lines=orchestration_lines(Path(modules[name].__file__)),
    )
    tickets = load_tickets()
    started = time.perf_counter()

    stack = factory(RuleBasedSupportModel(), registry)
    outcomes: dict[str, RunOutcome] = {}
    for ticket in tickets:
        outcome = stack.run(ticket.ticket_id)
        outcomes[ticket.ticket_id] = outcome
        if outcome.status == "awaiting_approval":
            measurement.escalated += 1

    # A NEW instance for the approvals: the reviewer answers hours later, and
    # by then the process that started the run is gone. A stack that can only
    # be approved by the object that started it fails here.
    approver = factory(RuleBasedSupportModel(), registry)
    for ticket_id, outcome in outcomes.items():
        if outcome.status != "awaiting_approval":
            continue
        resumed = approver.approve(ticket_id, "approve")
        if resumed.status == "completed":
            measurement.resumed_after_restart += 1

    # The retry: every ticket processed a second time, from a third instance.
    # Nothing should reach the customer twice.
    retrier = factory(RuleBasedSupportModel(), registry)
    for ticket in tickets:
        again = retrier.run(ticket.ticket_id)
        if again.status == "awaiting_approval":
            retrier.approve(ticket.ticket_id, "approve")

    measurement.seconds = round(time.perf_counter() - started, 3)
    measurement.llm_calls = (
        stack.client.calls
        + approver.client.calls
        + retrier.client.calls
        + sum(
            getattr(getattr(s, "model", None), "turns", 0)
            + getattr(s, "model_turns", 0)
            for s in (stack, approver, retrier)
        )
    )

    sent = sent_replies()
    per_ticket: dict[str, int] = {}
    for receipt in sent:
        per_ticket[receipt.ticket_id] = per_ticket.get(receipt.ticket_id, 0) + 1
    measurement.replies_sent = len(sent)
    measurement.completed = len(per_ticket)
    measurement.duplicate_sends = sum(count - 1 for count in per_ticket.values())
    return measurement


def render(rows: list[StackMeasurement]) -> str:
    lines = [
        "# Stack benchmark",
        "",
        "50 synthetic tickets through four orchestrators, same order, same",
        "deterministic model (`RuleBasedSupportModel`, a stub — not an LLM).",
        "Each stack is run three times over the fixture set: once to triage and",
        "escalate, once from a fresh instance to approve, and once more to prove",
        "a retry sends nothing twice.",
        "",
        "| Stack | completed | escalated | resumed after restart | duplicate sends "
        "| llm calls | orchestration lines | seconds |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row.stack} | {row.completed} | {row.escalated} | "
            f"{row.resumed_after_restart} | {row.duplicate_sends} | "
            f"{row.llm_calls} | {row.orchestration_lines} | {row.seconds:.2f} |"
        )
    lines += [
        "",
        "`completed` counts tickets with at least one reply in the outbox;",
        "`duplicate_sends` counts extra rows the customer would have received.",
        "Both are read from the outbox rather than from what a stack says about",
        "itself.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the raw rows")
    args = parser.parse_args(argv)

    registry = PromptRegistry(REPO_ROOT / "prompts")
    workspace = Path(tempfile.mkdtemp(prefix="bench-"))
    os.environ["SUPPORT_STATE_DIR"] = str(workspace)
    os.environ["SUPPORT_OUTBOX_DB"] = str(workspace / "outbox.sqlite3")

    rows: list[StackMeasurement] = []
    for name, factory in stack_factories().items():
        reset_outbox()
        for stale in workspace.glob("*.sqlite3"):
            if stale.name != "outbox.sqlite3":
                stale.unlink()
        rows.append(benchmark(name, factory, registry))

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "bench.md").write_text(render(rows), encoding="utf-8")
    (RESULTS / "bench.json").write_text(
        json.dumps(
            [asdict(row) | {"seconds": None} for row in rows], indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )

    if args.json:
        print(json.dumps([asdict(row) for row in rows], indent=2))
    else:
        print(render(rows))
    print(
        f"median orchestration lines: "
        f"{statistics.median(r.orchestration_lines for r in rows):.0f}"
    )
    for path in workspace.glob("*"):
        path.unlink()
    workspace.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
