"""Stage 08 — a framework-free DAG runner.

Five node kinds, nine states, a written state transition table, hard and soft
dependencies, fingerprint-based reruns and a SQLite result store. On top of it,
one real flow and — for comparison — a fifteen-line LangGraph supervisor that
does the same work dynamically.

    from aimai_workflows.dagrun import execute, open_store, validate
    from aimai_workflows.dagrun.flows import build_flow

    nodes = validate(build_flow())
    with open_store(":memory:") as store:
        report = await execute(nodes, "SZL-2026-0431", store)
    print(report.by_state(NodeState.DEGRADED))

    uv run dagrun --seed SZL-2026-0431 --dry-run

What this stage makes visible is what LangGraph and CrewAI do on your behalf.
Writing the coordination layer once teaches three things a framework hides:
partial success is a state, dependencies come in two strengths, and the right
definition of a rerun is "do what changed", not "do everything again".
"""

from .execute import JobLimits, NodeRecord, RunReport, execute
from .fingerprint import fingerprint, subtree, version_from_files
from .report import as_dict, as_json, mermaid, missing_data_section, render
from .store import ResultStore, open_store
from .types import (
    TERMINAL_STATES,
    USABLE_STATES,
    Node,
    NodeKind,
    NodeResult,
    NodeState,
    RunContext,
)
from .validate import GraphError, topological_order, validate, warnings_for

__all__ = [
    "GraphError",
    "JobLimits",
    "Node",
    "NodeKind",
    "NodeRecord",
    "NodeResult",
    "NodeState",
    "ResultStore",
    "RunContext",
    "RunReport",
    "TERMINAL_STATES",
    "USABLE_STATES",
    "as_dict",
    "as_json",
    "execute",
    "fingerprint",
    "mermaid",
    "missing_data_section",
    "open_store",
    "render",
    "subtree",
    "topological_order",
    "validate",
    "version_from_files",
    "warnings_for",
]
