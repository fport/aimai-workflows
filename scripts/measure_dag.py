"""Measure the DAG runner over many jobs.

    uv run python scripts/measure_dag.py --jobs 100

Two things this script refuses to report:

**No mean cost.** A supervisor loop or a retry storm leaves the mean untouched
and multiplies the maximum, so a table with a mean and no tail hides the only
number that matters when the bill arrives. p90 and max, or nothing.

**No aggregate "success rate".** Per-node rates, because "94% of jobs
succeeded" is compatible with the case-law specialist being down all week —
every job degraded, none failed, and the aggregate looks fine.

The failure mix is deterministic: a fixed seed decides which jobs see a
case-law outage, a delivery failure or a failed retraction, so two runs of this
script produce the same table and a change in it means a change in the runner.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from aimai_workflows.dagrun import (  # noqa: E402
    as_dict,
    execute,
    open_store,
    validate,
)
from aimai_workflows.dagrun.flows import build_flow  # noqa: E402
from aimai_workflows.stacks.core import reset_outbox  # noqa: E402

RESULTS = REPO_ROOT / "results"
NODES = (
    "extract",
    "scope_gate",
    "clause_review",
    "statute",
    "caselaw",
    "synthesis",
    "deliver",
    "archive",
)


def failure_mix(index: int, rng: random.Random) -> dict:
    """Which jobs go wrong, and how.

    Roughly one in six loses case law, one in twenty-five loses the filing after
    delivery, and one in a hundred cannot retract. Chosen to put a few of every
    outcome in the table rather than to model any real service.
    """
    roll = rng.random()
    if roll < 0.16:
        return {"caselaw_fails": True}
    if roll < 0.20:
        return {"archive_fails": True}
    if roll < 0.21:
        return {"archive_fails": True, "retract_fails": True}
    return {}


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * (len(ordered) - 1)))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260909)
    args = parser.parse_args(argv)

    workspace = Path(tempfile.mkdtemp(prefix="dagmeasure-"))
    os.environ["SUPPORT_OUTBOX_DB"] = str(workspace / "outbox.sqlite3")
    os.environ.setdefault("CONTRACT_DOCUMENT_ROOT", str(REPO_ROOT / "fixtures"))
    reset_outbox()

    rng = random.Random(args.seed)
    node_states: dict[str, Counter[str]] = {node: Counter() for node in NODES}
    outcomes: Counter[str] = Counter()
    costs: list[float] = []
    rerun_savings: list[float] = []
    needs_human = 0

    with open_store(workspace / "store.sqlite3") as store:
        for index in range(args.jobs):
            seed = f"JOB-{index:04d}"
            nodes = validate(build_flow(**failure_mix(index, rng)))
            report = asyncio.run(execute(nodes, seed, store))
            payload = as_dict(report)

            outcomes[payload["outcome"]] += 1
            needs_human += bool(payload["needs_human"])
            costs.append(report.total_cost_usd)
            for node in NODES:
                node_states[node][payload["nodes"][node]["state"]] += 1

            # The rerun: same seed, same graph, nothing changed. Everything
            # that succeeded should come from the store.
            second = asyncio.run(execute(nodes, seed, store))
            if report.total_cost_usd:
                rerun_savings.append(1 - second.spent_usd / report.total_cost_usd)

    report_data = {
        "jobs": args.jobs,
        "outcomes": dict(outcomes),
        "needs_human": needs_human,
        "node_states": {node: dict(counts) for node, counts in node_states.items()},
        "cost_p90": round(percentile(costs, 0.90), 6),
        "cost_max": round(max(costs), 6),
        "cost_median": round(statistics.median(costs), 6),
        "rerun_saving_mean": round(statistics.fmean(rerun_savings), 4),
        "cache_hits": store.hits if hasattr(store, "hits") else 0,
    }

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "dagrun.json").write_text(
        json.dumps(report_data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (RESULTS / "dagrun.md").write_text(render(report_data), encoding="utf-8")
    print(render(report_data))
    shutil.rmtree(workspace, ignore_errors=True)
    return 0


def render(data: dict) -> str:
    lines = [
        f"# DAG runner over {data['jobs']} jobs",
        "",
        "Deterministic failure mix (fixed seed); the specialists are scripted",
        "agents, not models. Regenerate with `uv run python scripts/measure_dag.py`.",
        "",
        "| Node | succeeded | degraded | failed | skipped | compensated |",
        "|---|---|---|---|---|---|",
    ]
    for node, counts in data["node_states"].items():
        lines.append(
            f"| `{node}` | {counts.get('succeeded', 0)} | "
            f"{counts.get('degraded', 0)} | {counts.get('failed', 0)} | "
            f"{counts.get('skipped', 0)} | {counts.get('compensated', 0)} |"
        )

    total = data["jobs"]
    degraded = data["outcomes"].get("degraded", 0)
    lines += [
        "",
        "| Metric | How it is measured | Value |",
        "|---|---|---|",
        f"| Clean jobs | every sink node succeeded | "
        f"{data['outcomes'].get('clean', 0)}/{total} |",
        f"| Degraded jobs | delivered on partial evidence | {degraded}/{total} "
        f"({degraded / total:.0%}) |",
        f"| Failed jobs | a sink node failed or was skipped | "
        f"{data['outcomes'].get('failed', 0)}/{total} |",
        f"| Needs a human | a compensation failed | {data['needs_human']}/{total} |",
        f"| Rerun saving | 1 − (spend on rerun / cost of result) | "
        f"{data['rerun_saving_mean']:.1%} |",
        f"| Cost per job, median | | ${data['cost_median']:.4f} |",
        f"| Cost per job, p90 | the number a budget is set from | "
        f"${data['cost_p90']:.4f} |",
        f"| Cost per job, max | one job, worst case | ${data['cost_max']:.4f} |",
        "",
        "No mean cost, on purpose: a retry storm leaves the mean untouched and",
        "multiplies the maximum.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
