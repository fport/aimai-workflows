"""Produce the numbers in the README, and the ones that must not drift.

    uv run python scripts/measure.py                # SQLite, writes results/
    uv run python scripts/measure.py --postgres     # against a real database

Two outputs, on purpose:

`results/durability.json` holds only DETERMINISTIC facts — how many checkpoint
writes each durability mode performs, how many supersteps a run takes, how many
times the reviewer was called, whether a killed run resumed. CI regenerates it
and fails if it changed, so a refactor that quietly doubles the writes per run,
or that makes a resume re-run the assessment, is caught by a diff rather than
by a reader.

`results/measurements.md` holds the human-readable table, timings included.
Timings differ between machines, so CI does not diff it; the JSON is what is
pinned.

WHAT IS MEASURED AND WHAT IS SIMULATED. Everything about the flow — checkpoint
sizes, write counts, resume behaviour, replay — is measured here for real. Two
rows of the README table are not, and cannot be: approval waiting time and the
rate at which humans disagree with the model both need humans. Those are
produced from a simulated decision stream and are labelled as such in every
output this script writes. A portfolio table that quietly presents an invented
p95 as an observation is worth less than no table.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from langgraph.checkpoint.base import BaseCheckpointSaver  # noqa: E402

from aimai_workflows.contract import RuleBasedReviewer  # noqa: E402

DURABILITY_MODES = ("exit", "async", "sync")
RESULTS = REPO_ROOT / "results"


class CountingSaver(BaseCheckpointSaver):
    """A checkpointer that counts what it was asked to do.

    It has to BE a `BaseCheckpointSaver`, not merely quack like one —
    `compile()` checks the type — but it delegates everything to whatever saver
    is underneath, so the same counter works over SQLite and Postgres.

    `get_next_version` is delegated too, and that one matters: LangGraph asks
    the saver how to number channel versions, and a proxy that answered with
    the base implementation would produce versions the underlying store does
    not recognise on the next read.
    """

    def __init__(self, inner: BaseCheckpointSaver) -> None:
        super().__init__(serde=inner.serde)
        self._inner = inner
        self.counts: Counter[str] = Counter()
        self.write_seconds: list[float] = []

    def put(self, *args: Any, **kwargs: Any) -> Any:
        self.counts["put"] += 1
        started = time.perf_counter()
        try:
            return self._inner.put(*args, **kwargs)
        finally:
            self.write_seconds.append(time.perf_counter() - started)

    def put_writes(self, *args: Any, **kwargs: Any) -> Any:
        self.counts["put_writes"] += 1
        started = time.perf_counter()
        try:
            return self._inner.put_writes(*args, **kwargs)
        finally:
            self.write_seconds.append(time.perf_counter() - started)

    def get_tuple(self, *args: Any, **kwargs: Any) -> Any:
        self.counts["get_tuple"] += 1
        return self._inner.get_tuple(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.list(*args, **kwargs)

    def get_next_version(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.get_next_version(*args, **kwargs)

    def delete_thread(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.delete_thread(*args, **kwargs)


@contextmanager
def backend(use_postgres: bool, workspace: Path):
    from aimai_workflows.contract.checkpointer import (
        postgres_checkpointer,
        sqlite_checkpointer,
    )

    if use_postgres:
        with postgres_checkpointer() as saver:
            yield saver
    else:
        with sqlite_checkpointer(str(workspace / "checkpoints.sqlite3")) as saver:
            yield saver


def state_bytes(graph, config) -> int:
    """How large one review's state is, as the checkpointer stores it.

    Measured through the serializer the checkpointer actually uses, not through
    `json.dumps`: msgpack and JSON differ by enough that a JSON estimate would
    misreport the row size by a third.
    """
    from aimai_workflows.contract.checkpointer import serializer

    values = graph.get_state(config).values
    return len(serializer().dumps_typed(values)[1])


def run_mode(mode: str, saver, registry, contract_no: str, document: str) -> dict:
    """One full review — start, pause at the gate, approve — under one mode."""
    from langgraph.types import Command

    from aimai_workflows.contract import (
        RuleBasedReviewer,
        compile_graph,
        crm_notes,
        thread_config,
    )

    reviewer = RuleBasedReviewer()
    counting = CountingSaver(saver)
    graph = compile_graph(reviewer, counting, registry=registry)
    config = thread_config(contract_no)

    started = time.perf_counter()
    graph.invoke(
        {"contract_no": contract_no, "document_uri": document}, config, durability=mode
    )
    to_gate = time.perf_counter() - started
    paused_at = list(graph.get_state(config).next)
    calls_at_gate = reviewer.calls

    resumed = time.perf_counter()
    out = graph.invoke(Command(resume="approve"), config, durability=mode)
    to_finish = time.perf_counter() - resumed

    return {
        "mode": mode,
        "checkpoint_writes": counting.counts["put"],
        "pending_write_batches": counting.counts["put_writes"],
        "supersteps": len(list(graph.get_state_history(config))),
        "paused_at": paused_at,
        "reviewer_calls_before_resume": calls_at_gate,
        "reviewer_calls_total": reviewer.calls,
        "state_bytes": state_bytes(graph, config),
        "notes_written": len(crm_notes(contract_no)),
        "resumed": bool(out.get("action_receipt")),
        "seconds_to_gate": round(to_gate, 4),
        "seconds_to_finish": round(to_finish, 4),
        "checkpoint_write_ms_mean": round(
            1000 * statistics.fmean(counting.write_seconds), 3
        )
        if counting.write_seconds
        else 0.0,
    }


SLOW_WORKER = """
import sys, time
sys.path.insert(0, {src!r})
from aimai_kit.prompts import PromptRegistry
from aimai_workflows.contract import RuleBasedReviewer, compile_graph, thread_config
from aimai_workflows.contract.checkpointer import sqlite_checkpointer


class SlowReviewer(RuleBasedReviewer):
    def complete(self, req):
        print("ASSESSING", flush=True)
        time.sleep(120)


with sqlite_checkpointer({db!r}) as saver:
    graph = compile_graph(
        SlowReviewer(), saver, registry=PromptRegistry({prompts!r})
    )
    graph.invoke(
        {{"contract_no": {contract!r}, "document_uri": "high_risk.txt"}},
        thread_config({contract!r}),
        durability={mode!r},
    )
"""


def measure_crash_recovery(mode: str, workspace: Path, registry) -> dict:
    """Where a run that is KILLED mid-assessment picks up again, per mode.

    An exception is the wrong instrument here: LangGraph catches it and still
    persists what it has, so all three modes look identical. A process that is
    gone cannot persist anything, which is what makes `SIGKILL` the honest
    test — and it is also the failure that actually happens in production, as
    an OOM kill, a node eviction or a lost spot instance.

    The worker is killed while the reviewer is running, i.e. after
    `fetch_document` completed and before `assess_risk` returned. Under `sync`
    the first node's result is already committed and the resumed run starts at
    the assessment. Under `exit` nothing is written until the invocation ends,
    so nothing survived: the thread does not exist and the review restarts from
    zero.
    """
    from aimai_workflows.contract import compile_graph, thread_config
    from aimai_workflows.contract.checkpointer import sqlite_checkpointer

    contract_no = f"CRASH-{mode}"
    database = workspace / f"crash-{mode}.sqlite3"
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            SLOW_WORKER.format(
                src=str(REPO_ROOT / "src"),
                db=str(database),
                prompts=str(REPO_ROOT / "prompts"),
                contract=contract_no,
                mode=mode,
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=REPO_ROOT,
        env=os.environ.copy(),
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if (worker.stdout.readline() if worker.stdout else "").strip() == "ASSESSING":
            break
        if worker.poll() is not None:
            raise RuntimeError(f"the worker exited before assessing ({mode})")
    else:
        raise TimeoutError(f"the worker never reached the assessment ({mode})")

    os.kill(worker.pid, signal.SIGKILL)
    worker.wait(timeout=10)

    with sqlite_checkpointer(str(database)) as saver:
        graph = compile_graph(RuleBasedReviewer(), saver, registry=registry)
        state = graph.get_state(thread_config(contract_no))
        values = state.values or {}
        resumes_at = list(state.next)

    return {
        "crash_resumes_at": resumes_at or ["nothing survived; the review restarts"],
        "crash_kept_document_hash": bool(values.get("text_sha256")),
    }


def simulate_human_decisions(runs: int, seed: int = 7) -> dict:
    """SIMULATED. Approval latency and disagreement rate.

    Both need a real reviewer answering real emails over real weeks. The
    distribution here — lognormal around four working hours, 22% of decisions
    something other than a plain approve — is a plausible shape, not an
    observation, and every table that prints it says so.
    """
    import random

    rng = random.Random(seed)
    waits = [rng.lognormvariate(1.4, 1.0) for _ in range(runs)]
    decisions = [
        rng.choices(["approve", "edit", "reject"], weights=[78, 15, 7])[0]
        for _ in range(runs)
    ]
    waits.sort()
    return {
        "simulated": True,
        "runs": runs,
        "wait_hours_p50": round(statistics.median(waits), 2),
        "wait_hours_p95": round(waits[int(0.95 * (runs - 1))], 2),
        "disagreement_rate": round(
            sum(1 for d in decisions if d != "approve") / runs, 3
        ),
    }


def measure_load(saver, registry, runs: int) -> dict:
    """Run the flow `runs` times and report what the store grew to."""
    from langgraph.types import Command

    from aimai_workflows.contract import (
        RuleBasedReviewer,
        compile_graph,
        crm_notes,
        thread_config,
    )

    reviewer = RuleBasedReviewer()
    counting = CountingSaver(saver)
    graph = compile_graph(reviewer, counting, registry=registry)

    sizes: list[int] = []
    resumed_ok = 0
    replayed_assessments = 0
    started = time.perf_counter()

    for index in range(runs):
        contract_no = f"LOAD-{index:04d}"
        config = thread_config(contract_no)
        document = "high_risk.txt" if index % 3 else "low_risk.txt"
        calls_before = reviewer.calls
        graph.invoke(
            {"contract_no": contract_no, "document_uri": document},
            config,
            durability="sync",
        )
        if graph.get_state(config).next:
            out = graph.invoke(Command(resume="approve"), config, durability="sync")
            resumed_ok += bool(out.get("action_receipt"))
        else:
            resumed_ok += 1
        # One assessment per review is correct; two means a resumed run
        # re-executed a node that had already produced its result.
        if reviewer.calls - calls_before > 1:
            replayed_assessments += 1
        sizes.append(state_bytes(graph, config))

    elapsed = time.perf_counter() - started
    ordered = sorted(counting.write_seconds)
    write_ms_p95 = ordered[int(0.95 * (len(ordered) - 1))] if ordered else 0.0
    return {
        "runs": runs,
        "seconds_total": round(elapsed, 2),
        "seconds_per_review_mean": round(elapsed / runs, 4),
        "resume_success_rate": round(resumed_ok / runs, 4),
        "replay_rate": round(replayed_assessments / runs, 4),
        "state_bytes_mean": int(statistics.fmean(sizes)),
        "state_bytes_max": max(sizes),
        "checkpoint_writes_total": counting.counts["put"],
        "checkpoint_write_ms_p95": round(1000 * write_ms_p95, 3),
        "notes_total": len(crm_notes()),
    }


def postgres_table_size() -> dict:
    """How much disk 100 reviews cost, from the database's own accounting."""
    import psycopg

    from aimai_workflows.contract.checkpointer import postgres_dsn

    with psycopg.connect(postgres_dsn()) as conn:
        rows = conn.execute(
            """
            SELECT relname, pg_total_relation_size(relid) AS bytes, n_live_tup
            FROM pg_stat_user_tables
            WHERE relname LIKE 'checkpoint%'
            ORDER BY relname
            """
        ).fetchall()
    return {
        name: {"bytes": int(size), "rows": int(rows_count)}
        for name, size, rows_count in rows
    }


def render_markdown(report: dict) -> str:
    modes = report["durability"]
    load = report["load"]
    human = report["human"]
    lines = [
        "# Measurements",
        "",
        f"Backend: **{report['backend']}** · reviewer: **rule-based stub** · ",
        f"generated by `scripts/measure.py` on {report['generated_at']}.",
        "",
        "Every number below is measured except the two rows marked SIMULATED,",
        "which describe human behaviour this repo has no humans for.",
        "",
        "## Durability modes",
        "",
        "The same review — start, pause at the gate, approve — under each mode.",
        "",
        "| Mode | End to end (s) | Checkpoint writes | Supersteps | "
        "Resumed | After SIGKILL during the assessment, the run resumes at |",
        "|---|---|---|---|---|---|",
    ]
    for row in modes:
        total = row["seconds_to_gate"] + row["seconds_to_finish"]
        resumes_at = ", ".join(row["crash_resumes_at"])
        lines.append(
            f"| `{row['mode']}` | {total:.3f} | {row['checkpoint_writes']} | "
            f"{row['supersteps']} | {'yes' if row['resumed'] else 'NO'} | "
            f"`{resumes_at}` |"
        )

    lines += [
        "",
        f"## Load: {load['runs']} reviews",
        "",
        "| Metric | How it is measured | Value |",
        "|---|---|---|",
        f"| Resume success rate | completed `Command(resume=…)` / reviews "
        f"| {load['resume_success_rate']:.3f} |",
        f"| Replay rate | reviews whose assessment node ran twice / reviews "
        f"| {load['replay_rate']:.3f} |",
        f"| Mean state size | serialized state per thread "
        f"| {load['state_bytes_mean']} B |",
        f"| Largest state | same, worst thread | {load['state_bytes_max']} B |",
        f"| Checkpoint write latency p95 | per `put`, `sync` mode "
        f"| {load['checkpoint_write_ms_p95']} ms |",
        f"| Checkpoint writes | total `put` calls over the run "
        f"| {load['checkpoint_writes_total']} |",
        f"| Time per review | wall clock / reviews "
        f"| {load['seconds_per_review_mean'] * 1000:.1f} ms |",
        f"| CRM notes | one per approved review | {load['notes_total']} |",
        f"| Approval wait p50 | SIMULATED, not observed "
        f"| {human['wait_hours_p50']} h |",
        f"| Approval wait p95 | SIMULATED, not observed "
        f"| {human['wait_hours_p95']} h |",
        f"| Human disagreement rate | SIMULATED, not observed "
        f"| {human['disagreement_rate']:.3f} |",
        "",
    ]

    if report.get("storage"):
        lines += [
            "## Checkpoint storage",
            "",
            "| Table | Size | Rows |",
            "|---|---|---|",
        ]
        for name, stats in report["storage"].items():
            lines.append(
                f"| `{name}` | {stats['bytes'] / 1024:.0f} KiB | {stats['rows']} |"
            )
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--runs", type=int, default=100)
    args = parser.parse_args()

    from aimai_kit.prompts import PromptRegistry

    from aimai_workflows.contract import reset_crm

    workspace = Path(tempfile.mkdtemp(prefix="measure-"))
    os.environ["CONTRACT_CRM_DB"] = str(workspace / "crm.sqlite3")
    os.environ.setdefault("CONTRACT_DOCUMENT_ROOT", str(REPO_ROOT / "fixtures"))
    registry = PromptRegistry(REPO_ROOT / "prompts")
    reset_crm()

    report: dict[str, Any] = {
        "backend": "postgres" if args.postgres else "sqlite",
        "generated_at": time.strftime("%Y-%m-%d"),
    }

    with backend(args.postgres, workspace) as saver:
        report["durability"] = [
            run_mode(mode, saver, registry, f"MODE-{mode}", "high_risk.txt")
            | measure_crash_recovery(mode, workspace, registry)
            for mode in DURABILITY_MODES
        ]
        reset_crm()
        report["load"] = measure_load(saver, registry, args.runs)

    report["human"] = simulate_human_decisions(args.runs)
    if args.postgres:
        report["storage"] = postgres_table_size()

    RESULTS.mkdir(exist_ok=True)
    # Postgres numbers go to their own files. CI pins the SQLite run, which
    # needs no services; overwriting it from a local Postgres run would make
    # the pinned file depend on which database the last person happened to
    # have running.
    suffix = "-postgres" if args.postgres else ""
    deterministic = {
        "backend": report["backend"],
        "durability": [
            {
                k: v
                for k, v in row.items()
                if not k.startswith("seconds") and "ms" not in k
            }
            for row in report["durability"]
        ],
        "load": {
            k: v
            for k, v in report["load"].items()
            if not k.startswith("seconds") and "ms" not in k
        },
    }
    (RESULTS / f"durability{suffix}.json").write_text(
        json.dumps(deterministic, indent=2, sort_keys=True) + "\n"
    )
    (RESULTS / f"measurements{suffix}.md").write_text(render_markdown(report))

    print(json.dumps(report, indent=2))
    print(
        f"\nwrote results/durability{suffix}.json and results/measurements{suffix}.md"
    )
    shutil.rmtree(workspace, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
