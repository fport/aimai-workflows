"""Kill each stack's worker and see what survives.

    uv run python -m aimai_workflows.stacks.chaos    # writes results/chaos.md

Two scenarios, because they fail differently:

**Parked** — the ticket reached the approval gate, the state was written, then
the worker died. Every stack should survive this; if one does not, its "human
in the loop" support is a coroutine waiting in memory and the feature does not
exist outside a demo.

**Mid-run** — the worker died between classification and drafting, with no
human involved. This is where the frameworks separate. A stack with a
checkpointer resumes at the step that had not finished. A stack whose state is
written only when a run *returns* has nothing to resume from and repeats the
work — which costs a model call per lost step and, in a flow with side effects
before the gate, would repeat those too.

`SIGKILL`, not `SIGTERM`: a terminated process could have run a shutdown hook,
and then the thing being measured is the hook. The child is killed while it
sleeps inside the model call.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from aimai_kit.prompts import PromptRegistry

from .core import REPO_ROOT, reset_outbox, sent_replies

__all__ = ["ChaosResult", "main"]

RESULTS = REPO_ROOT / "results"
RISKY_TICKET = "T-1000"

CHILD = '''
import sys, time
sys.path.insert(0, {src!r})
from aimai_kit.prompts import PromptRegistry
from aimai_workflows.stacks.bench import stack_factories
from aimai_workflows.stacks.stub_support import RuleBasedSupportModel


class SlowModel(RuleBasedSupportModel):
    """Sleeps inside the drafting call, so the kill lands mid-run."""

    def complete(self, req):
        if req.operation == "draft" and {stall!r}:
            print("WORKING", flush=True)
            time.sleep(120)
        return super().complete(req)


factory = stack_factories()[{stack!r}]
stack = factory(SlowModel(), PromptRegistry({prompts!r}))
outcome = stack.run({ticket!r})
print("PARKED", outcome.status, flush=True)
time.sleep(120)
'''


def paused_state_bytes(directory: Path) -> int:
    """How much a paused run costs to store, in whatever shape the stack chose.

    Summed over every text and blob column of every table the stack wrote,
    rather than the file size: SQLite rounds to 4 KB pages, which would make
    four very different states look identical. Each directory holds exactly one
    paused ticket, so this is the per-pause cost.
    """
    total = 0
    for database in directory.glob("*.sqlite3"):
        if database.name == "outbox.sqlite3":
            continue
        conn = sqlite3.connect(database)
        try:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            ]
            for table in tables:
                columns = [
                    row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                ]
                if not columns:
                    continue
                expression = " + ".join(f"COALESCE(LENGTH({c}), 0)" for c in columns)
                total += conn.execute(
                    f"SELECT COALESCE(SUM({expression}), 0) FROM {table}"
                ).fetchone()[0]
        finally:
            conn.close()
    return int(total)


@dataclass
class ChaosResult:
    """One stack, both scenarios."""

    stack: str
    paused_state_bytes: int = 0
    parked_recovered: bool = False
    parked_receipt: str | None = None
    parked_replies: int = 0
    midrun_recovered: bool = False
    midrun_repeated_model_calls: int = 0
    midrun_status: str = ""
    note: str = ""


def _spawn(stack: str, workspace: Path, *, stall: bool) -> subprocess.Popen:
    environment = os.environ.copy()
    environment["SUPPORT_STATE_DIR"] = str(workspace)
    environment["SUPPORT_OUTBOX_DB"] = str(workspace / "outbox.sqlite3")
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            CHILD.format(
                src=str(REPO_ROOT / "src"),
                prompts=str(REPO_ROOT / "prompts"),
                stack=stack,
                ticket=RISKY_TICKET,
                stall=stall,
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
    )


def _wait_for(worker: subprocess.Popen, marker: str, timeout: float = 90.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = (worker.stdout.readline() if worker.stdout else "").strip()
        if line.startswith(marker):
            return line
        if worker.poll() is not None:
            raise RuntimeError(
                f"the worker exited before {marker}:\n{worker.stderr.read()}"
            )
    raise TimeoutError(f"the worker never printed {marker}")


def _kill(worker: subprocess.Popen) -> None:
    os.kill(worker.pid, signal.SIGKILL)
    worker.wait(timeout=15)


def run_stack(name: str, factory, registry: PromptRegistry, root: Path) -> ChaosResult:
    result = ChaosResult(stack=name)

    # --- scenario 1: killed while parked on the gate ---------------------
    parked_dir = root / f"{name}-parked"
    parked_dir.mkdir()
    os.environ["SUPPORT_STATE_DIR"] = str(parked_dir)
    os.environ["SUPPORT_OUTBOX_DB"] = str(parked_dir / "outbox.sqlite3")
    worker = _spawn(name, parked_dir, stall=False)
    _wait_for(worker, "PARKED")
    _kill(worker)

    result.paused_state_bytes = paused_state_bytes(parked_dir)

    approver = factory(None, registry)
    outcome = approver.approve(RISKY_TICKET, "approve")
    result.parked_recovered = outcome.status == "completed"
    result.parked_receipt = outcome.receipt
    result.parked_replies = len(sent_replies(RISKY_TICKET))

    # --- scenario 2: killed mid-run --------------------------------------
    midrun_dir = root / f"{name}-midrun"
    midrun_dir.mkdir()
    os.environ["SUPPORT_STATE_DIR"] = str(midrun_dir)
    os.environ["SUPPORT_OUTBOX_DB"] = str(midrun_dir / "outbox.sqlite3")
    worker = _spawn(name, midrun_dir, stall=True)
    _wait_for(worker, "WORKING")
    _kill(worker)

    restarted = factory(None, registry)
    resumed = restarted.run(RISKY_TICKET)
    result.midrun_status = resumed.status
    result.midrun_recovered = resumed.status in ("awaiting_approval", "completed")
    # How much of the shared business logic had to be done again. One call
    # means the classification survived; two means the run started over.
    result.midrun_repeated_model_calls = restarted.client.calls
    result.note = (
        "resumed at the unfinished step"
        if result.midrun_repeated_model_calls <= 1
        else "no mid-run state; the run restarted"
    )
    return result


def render(rows: list[ChaosResult]) -> str:
    lines = [
        "# Chaos: SIGKILL during a run",
        "",
        "Each stack is driven to a point, its worker is killed with `SIGKILL`,",
        "and a fresh process is asked to carry on. Generated by",
        "`uv run python -m aimai_workflows.stacks.chaos`.",
        "",
        "| Stack | Killed while parked → approved? | Replies sent | "
        "Paused state | Killed mid-run → resumed? | Model calls repeated | "
        "What happened |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row.stack} | {'yes' if row.parked_recovered else 'NO'} | "
            f"{row.parked_replies} | {row.paused_state_bytes:,} B | "
            f"{'yes' if row.midrun_recovered else 'NO'} "
            f"({row.midrun_status}) | {row.midrun_repeated_model_calls} | "
            f"{row.note} |"
        )
    lines += [
        "",
        "`Replies sent` is the number of rows in the outbox for the ticket: one",
        "is correct, two would mean the kill produced a duplicate message to the",
        "customer.",
        "",
        "`Paused state` is every text and blob column the stack wrote for one",
        "paused ticket — what it costs to keep a run waiting for a human.",
        "",
        "`Model calls repeated` counts calls to the shared business logic in the",
        "process that took over. One means the classification written before the",
        "kill was reused; two means the flow started from the beginning.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from .bench import stack_factories

    registry = PromptRegistry(REPO_ROOT / "prompts")
    root = Path(tempfile.mkdtemp(prefix="chaos-"))
    rows: list[ChaosResult] = []
    for name, factory in stack_factories().items():
        reset_outbox()
        rows.append(run_stack(name, factory, registry, root))

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "chaos.md").write_text(render(rows), encoding="utf-8")
    (RESULTS / "chaos.json").write_text(
        json.dumps([asdict(row) for row in rows], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps([asdict(r) for r in rows], indent=2) if args.json else render(rows)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
