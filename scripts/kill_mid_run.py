"""Kill the worker while a review is waiting for a human, then finish the review.

This script is the stage's headline claim, executable:

    uv run python scripts/kill_mid_run.py

It starts a review of a high-risk contract in a CHILD PROCESS, waits until the
flow is parked on the approval gate, and sends that process `SIGKILL` — not a
graceful shutdown, not an exception, no chance to flush anything. Then a fresh
process opens the same checkpointer, approves the review, and the script checks
that the CRM holds exactly one note.

`SIGKILL` rather than `SIGTERM` is the point. A terminated process could have
run a shutdown hook; a killed one cannot, so anything that survives survived
because it was durable, not because it was tidied away.

SQLite is used here instead of Postgres so the script runs with no services and
no credentials — a reader can clone the repo and reproduce the claim in one
command. `pytest -m postgres` makes the same claim against the real database.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

CHILD = """
import sys, time
sys.path.insert(0, {src!r})
from aimai_kit.prompts import PromptRegistry
from aimai_workflows.contract import RuleBasedReviewer, compile_graph, thread_config
from aimai_workflows.contract.checkpointer import sqlite_checkpointer

with sqlite_checkpointer({db!r}) as saver:
    graph = compile_graph(
        RuleBasedReviewer(), saver, registry=PromptRegistry({prompts!r})
    )
    config = thread_config({contract!r})
    graph.invoke(
        {{"contract_no": {contract!r}, "document_uri": {document!r}}},
        config,
        durability={durability!r},
    )
    print("PAUSED", flush=True)
    # The worker is now doing what a real one does while a human decides:
    # nothing, holding no lock, occupying no thread of the flow. It will be
    # killed here.
    time.sleep(120)
"""


def start_worker(**kwargs: object) -> subprocess.Popen:
    """Run the first half of the review in a process we are going to kill."""
    return subprocess.Popen(
        [sys.executable, "-c", CHILD.format(**kwargs)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
        env=os.environ.copy(),
    )


def wait_until_paused(worker: subprocess.Popen, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = worker.stdout.readline() if worker.stdout else ""
        if line.strip() == "PAUSED":
            return
        if worker.poll() is not None:
            raise RuntimeError(
                "the worker exited before pausing:\n" + (worker.stderr.read() or "")
            )
    raise TimeoutError("the worker never reached the approval gate")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", default="SUP-2025-0042")
    parser.add_argument("--document", default="high_risk.txt")
    parser.add_argument(
        "--durability", default="sync", choices=["exit", "async", "sync"]
    )
    parser.add_argument("--keep", action="store_true", help="keep the temp databases")
    args = parser.parse_args()

    workspace = Path(tempfile.mkdtemp(prefix="kill-mid-run-"))
    checkpoints = workspace / "checkpoints.sqlite3"
    os.environ["CONTRACT_CRM_DB"] = str(workspace / "crm.sqlite3")
    os.environ.setdefault("CONTRACT_DOCUMENT_ROOT", str(REPO_ROOT / "fixtures"))

    from aimai_kit.prompts import PromptRegistry
    from langgraph.types import Command

    from aimai_workflows.contract import (
        RuleBasedReviewer,
        compile_graph,
        crm_notes,
        reset_crm,
        thread_config,
    )
    from aimai_workflows.contract.checkpointer import sqlite_checkpointer

    reset_crm()
    report: dict[str, object] = {
        "contract_no": args.contract,
        "document": args.document,
        "durability": args.durability,
    }

    print(f"starting a review of {args.document} in a child process…")
    started = time.monotonic()
    worker = start_worker(
        src=str(REPO_ROOT / "src"),
        db=str(checkpoints),
        prompts=str(REPO_ROOT / "prompts"),
        contract=args.contract,
        document=args.document,
        durability=args.durability,
    )
    wait_until_paused(worker)
    report["seconds_to_gate"] = round(time.monotonic() - started, 3)
    print(f"the flow is parked on the gate after {report['seconds_to_gate']}s")

    print(f"sending SIGKILL to pid {worker.pid}…")
    os.kill(worker.pid, signal.SIGKILL)
    worker.wait(timeout=10)
    report["worker_returncode"] = worker.returncode
    report["checkpoint_bytes"] = checkpoints.stat().st_size

    print("the worker is gone; opening the checkpointer from a new process…")
    resumed = time.monotonic()
    with sqlite_checkpointer(str(checkpoints)) as saver:
        graph = compile_graph(
            RuleBasedReviewer(), saver, registry=PromptRegistry(REPO_ROOT / "prompts")
        )
        config = thread_config(args.contract)
        state = graph.get_state(config)
        report["next_before_resume"] = list(state.next)
        report["findings_recovered"] = len(state.values.get("findings", []))
        report["notes_before_resume"] = len(crm_notes(args.contract))

        out = graph.invoke(
            Command(resume="approve"), config, durability=args.durability
        )
        report["seconds_to_finish"] = round(time.monotonic() - resumed, 3)
        report["action_receipt"] = out.get("action_receipt")
        report["notes_after_resume"] = len(crm_notes(args.contract))
        report["supersteps"] = len(list(graph.get_state_history(config)))

    ok = (
        report["next_before_resume"] == ["human_gate"]
        and report["notes_before_resume"] == 0
        and report["notes_after_resume"] == 1
        and bool(report["action_receipt"])
    )
    report["passed"] = ok

    print(json.dumps(report, indent=2))
    print(
        "\nPASS — the review survived SIGKILL and produced exactly one CRM note"
        if ok
        else "\nFAIL — see the report above"
    )
    if not args.keep:
        shutil.rmtree(workspace, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
