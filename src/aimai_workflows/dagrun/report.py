"""Turning a run into something a human can act on.

One rule shapes this file: **a degraded job must not read as a clean one.**
Every renderer here puts what was lost before what was produced, because the
failure mode of a coordination layer is not a crash — it is a synthesis that
looks finished and quietly rests on half its evidence.

That is also why `missing_data_section` exists and why the flow passes it into
the synthesis prompt. A synthesis node that is not told what it is missing
writes with the confidence of a node that has everything.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from .execute import NodeRecord, RunReport
from .types import NodeState

__all__ = ["as_dict", "mermaid", "missing_data_section", "render"]

_ORDER = (
    NodeState.FAILED,
    NodeState.SKIPPED,
    NodeState.DEGRADED,
    NodeState.COMPENSATED,
    NodeState.SUCCEEDED,
)


def missing_data_section(report: RunReport) -> str:
    """What the job did not have, in the words a reader needs.

    Returned even when nothing is missing — an explicit "nothing" is a different
    statement from an absent section, and the difference is what stops a reader
    assuming the section was forgotten.
    """
    lines: list[str] = []
    for state in (NodeState.FAILED, NodeState.SKIPPED):
        for node_id in report.by_state(state):
            record = report.records[node_id]
            lines.append(f"- {node_id}: {state.value} — {record.error or 'no detail'}")
    degraded = report.by_state(NodeState.DEGRADED)
    for node_id in degraded:
        record = report.records[node_id]
        lost = ", ".join(record.missing_inputs + record.degraded_inputs) or "unstated"
        lines.append(f"- {node_id}: degraded — worked without {lost}")
    if not lines:
        return "No inputs were missing; every node ran on complete data."
    return "The following inputs were missing or partial:\n" + "\n".join(lines)


def render(report: RunReport) -> str:
    """The run report, as printed by the CLI."""
    lines = [
        f"job {report.seed}",
        f"  outcome        {_outcome(report)}",
        f"  supersteps     {report.supersteps}",
        f"  cost           ${report.total_cost_usd:.4f}"
        f"  (spent this run ${report.spent_usd:.4f})",
        f"  tokens         {report.total_tokens:,}",
        f"  cache hits     {report.cache_hits}",
        f"  reason         {report.stopped_reason}",
        "",
        "  node                 state         attempts  cost      cached  note",
        "  " + "-" * 74,
    ]
    for state in _ORDER:
        for node_id in report.by_state(state):
            lines.append(_row(report.records[node_id]))
    for _, record in sorted(report.records.items()):
        if record.state not in _ORDER:
            lines.append(_row(record))

    lines += ["", "  missing data", "  " + "-" * 74]
    lines += [f"  {line}" for line in missing_data_section(report).splitlines()]
    if report.needs_human:
        lines += [
            "",
            "  *** NEEDS A HUMAN ***",
            f"  {report.stopped_reason}",
            "  No second automatic recovery was attempted, by design.",
        ]
    return "\n".join(lines)


def _outcome(report: RunReport) -> str:
    """The job's verdict, read off the nodes at the end of the graph.

    "Did anything fail" is the wrong question — an optional case-law lookup
    failing is exactly the situation `optional` edges exist for, and reporting
    that job as `failed` would train readers to ignore the word. What decides
    the verdict is whether the work at the end of the graph got done, and on
    what.
    """
    if report.needs_human:
        return "needs_human"
    sink_states = {report.records[node_id].state for node_id in report.sinks}
    if sink_states & {NodeState.FAILED, NodeState.SKIPPED}:
        return "failed"
    if NodeState.DEGRADED in sink_states or report.degraded:
        return "degraded"
    return "clean"


def _row(record: NodeRecord) -> str:
    note = record.error or (record.result.note if record.result else "")
    return (
        f"  {record.node_id:<20} {record.state.value:<13} {record.attempts:<9} "
        f"${record.cost_usd:<8.4f} {'yes' if record.from_cache else 'no':<7} "
        f"{note[:28]}"
    )


def as_dict(report: RunReport) -> dict:
    """The machine-readable form, for tests and for `--json`."""
    return {
        "seed": report.seed,
        "outcome": _outcome(report),
        "needs_human": report.needs_human,
        "stopped_reason": report.stopped_reason,
        "supersteps": report.supersteps,
        "cache_hits": report.cache_hits,
        "total_cost_usd": report.total_cost_usd,
        "spent_usd": report.spent_usd,
        "total_tokens": report.total_tokens,
        "nodes": {
            node_id: {
                "state": record.state.value,
                "attempts": record.attempts,
                "cost_usd": record.cost_usd,
                "from_cache": record.from_cache,
                "degraded_inputs": list(record.degraded_inputs),
                "missing_inputs": list(record.missing_inputs),
                "error": record.error,
            }
            for node_id, record in sorted(report.records.items())
        },
        "trace": [
            {
                "node_id": span.node_id,
                "state": span.state,
                "attempt": span.attempt,
                "cost_usd": span.cost_usd,
                "fingerprint": span.fingerprint,
                "from_cache": span.from_cache,
                "parents": list(span.parents),
            }
            for span in report.trace
        ],
    }


def as_json(report: RunReport) -> str:
    return json.dumps(as_dict(report), indent=2, sort_keys=True)


def mermaid(nodes: Sequence, report: RunReport | None = None) -> str:
    """A Mermaid diagram of the graph, optionally coloured by outcome.

    Deliberately the whole visualisation story. A graph runner tempts you into
    building a UI; this is fifteen lines, renders in any Markdown viewer, and
    goes stale only when the graph does.
    """
    styles = {
        NodeState.SUCCEEDED: "fill:#dff0d8",
        NodeState.DEGRADED: "fill:#fcf8e3",
        NodeState.FAILED: "fill:#f2dede",
        NodeState.SKIPPED: "fill:#eee",
        NodeState.COMPENSATED: "fill:#d9edf7",
    }
    lines = ["graph TD"]
    for node in nodes:
        label = f"{node.id}<br/>{node.kind.value}"
        lines.append(f'    {node.id}["{label}"]')
        for dependency in node.requires:
            lines.append(f"    {dependency} --> {node.id}")
        for dependency in node.optional:
            lines.append(f"    {dependency} -.optional.-> {node.id}")
        if node.compensates:
            lines.append(f"    {node.id} -.compensates.-> {node.compensates}")
    if report:
        for node_id, record in report.records.items():
            style = styles.get(record.state)
            if style:
                lines.append(f"    style {node_id} {style}")
    return "\n".join(lines)
