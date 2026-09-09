"""Stage 06 — a durable contract review flow with a human in the loop.

A four-node LangGraph flow: fetch the document, assess its risk, stop for a
human when the risk is high, then write one note to the CRM. What makes it
worth reading is not the flow but what is underneath it — the state lives in a
checkpointer, so the run can pause for days and survive the worker dying.

    from langgraph.checkpoint.memory import InMemorySaver
    from aimai_workflows.contract import RuleBasedReviewer, compile_graph, thread_config

    app = compile_graph(RuleBasedReviewer(), InMemorySaver())
    config = thread_config("SUP-2025-0042")
    out = app.invoke(
        {"contract_no": "SUP-2025-0042", "document_uri": "high_risk.txt"},
        config,
        durability="sync",
    )
    out["__interrupt__"]          # the flow is paused at the gate
    app.invoke(Command(resume="approve"), config, durability="sync")
"""

from .assessment import AssessmentFailed, AssessmentResult, assess_contract, reconcile
from .documents import DocumentChanged, DocumentText, load_document, verify_unchanged
from .graph import build_graph, compile_graph, thread_config, thread_id_for
from .sinks import CrmNote, crm_create_note, crm_notes, idempotency_key, reset_crm
from .state import (
    ACTIONABLE_RISK,
    ContractState,
    Decision,
    Finding,
    ReviewReport,
    Severity,
    merge_findings,
    risk_rank,
)
from .stub_reviewer import RuleBasedReviewer

__all__ = [
    "ACTIONABLE_RISK",
    "AssessmentFailed",
    "AssessmentResult",
    "ContractState",
    "CrmNote",
    "Decision",
    "DocumentChanged",
    "DocumentText",
    "Finding",
    "ReviewReport",
    "RuleBasedReviewer",
    "Severity",
    "assess_contract",
    "build_graph",
    "compile_graph",
    "crm_create_note",
    "crm_notes",
    "idempotency_key",
    "load_document",
    "merge_findings",
    "reconcile",
    "reset_crm",
    "risk_rank",
    "thread_config",
    "thread_id_for",
    "verify_unchanged",
]
