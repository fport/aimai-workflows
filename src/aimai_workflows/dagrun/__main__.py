"""The CLI.

    uv run dagrun --seed SZL-2026-0431
    uv run dagrun --seed SZL-2026-0431 --dry-run
    uv run dagrun --seed SZL-2026-0431 --resume
    uv run dagrun --seed SZL-2026-0431 --fail caselaw
    uv run dagrun --mermaid

`--dry-run` validates and prints the topological order without running a single
node. It is the command to run in CI on a graph change: a cycle or a dangling
reference costs nothing to catch here and costs a production job to catch at
runtime.

`--resume` is not a separate code path. The store is keyed by
`(seed, node_id, fingerprint)`, so running the same seed again *is* a resume:
every node whose inputs are unchanged is served from the store and only the
work that did not finish runs. A `--resume` flag that took a different route
through the executor would be a second implementation of the same thing, and
the two would drift.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .execute import JobLimits, execute
from .flows.due_diligence import build_flow, flow_digest
from .report import as_json, mermaid, render
from .store import open_store
from .validate import topological_order, validate, warnings_for

__all__ = ["main"]

FAILABLE = ("caselaw", "deliver", "archive", "retract")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dagrun", description=__doc__)
    parser.add_argument("--seed", default="SZL-2026-0431", help="the job identifier")
    parser.add_argument(
        "--store", default=".aimai-dagrun.sqlite3", help="result store path"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the graph and print its order; run nothing",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse cached results (the default; the flag documents intent)",
    )
    parser.add_argument(
        "--fresh", action="store_true", help="drop this seed's cached results first"
    )
    parser.add_argument(
        "--fail",
        choices=FAILABLE,
        action="append",
        default=[],
        help="make a node fail, to demonstrate degradation or compensation",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--mermaid", action="store_true", help="print the graph")
    parser.add_argument(
        "--cost-ceiling", type=float, default=5.0, help="job cost ceiling in USD"
    )
    args = parser.parse_args(argv)

    nodes = validate(
        build_flow(
            caselaw_fails="caselaw" in args.fail,
            deliver_fails="deliver" in args.fail,
            archive_fails="archive" in args.fail,
            retract_fails="retract" in args.fail,
        )
    )

    if args.mermaid:
        print(mermaid(nodes))
        return 0

    for warning in warnings_for(nodes):
        print(f"warning: {warning}", file=sys.stderr)

    if args.dry_run:
        print(f"graph {flow_digest(nodes)} — {len(nodes)} nodes, valid")
        for position, node_id in enumerate(topological_order(nodes), start=1):
            node = next(n for n in nodes if n.id == node_id)
            edges = ", ".join(node.requires) or "—"
            soft = ", ".join(node.optional)
            suffix = f" (optional: {soft})" if soft else ""
            row = f"  {position:>2}. {node_id:<15} {node.kind.value:<11}"
            print(f"{row} <- {edges}{suffix}")
        return 0

    Path(args.store).parent.mkdir(parents=True, exist_ok=True)
    with open_store(args.store) as store:
        if args.fresh:
            dropped = store.forget(args.seed)
            print(f"dropped {dropped} cached result(s) for {args.seed}")
        report = asyncio.run(
            execute(
                nodes,
                args.seed,
                store,
                limits=JobLimits(cost_ceiling_usd=args.cost_ceiling),
            )
        )

    print(as_json(report) if args.json else render(report))
    # Exit codes a shell can branch on: 0 clean, 1 degraded, 2 failed,
    # 3 needs a human. A job that degraded is not a job that failed, and a
    # pipeline that treats them the same will either page too often or not
    # enough.
    if report.needs_human:
        return 3
    if any(
        report.records[node].state.value in ("failed", "skipped")
        for node in report.sinks
    ):
        return 2
    return 1 if report.degraded else 0


if __name__ == "__main__":
    raise SystemExit(main())
