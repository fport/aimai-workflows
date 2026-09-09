"""The four nodes of the review flow.

Where the boundaries fall is the whole design, so each one is justified where
it is drawn.

**`fetch_document` and `assess_risk` are separate** even though fetching is one
line. They are separate because a checkpoint is written between them: a run
that dies during a 20-second assessment resumes with the document already
identified, and — more importantly — the hash that pins the document was
committed before the model ever saw it.

**`human_gate` contains no side effect.** A node that calls `interrupt()` is
re-executed *from its first line* when the flow resumes; `interrupt()` is not a
coroutine suspension point, it raises, and the resumed run replays the node
until the call returns the value. Any write above that line therefore happens
once per resume. `test_resume.py` pins this behaviour rather than trusting the
sentence.

**`execute_action` is a separate node** for the same reason inverted: it is the
only node with a side effect, it runs strictly after the gate, and it takes an
idempotency key derived from the state rather than from the attempt. Those
three facts together are what makes "resume does not send the note twice" a
property of the topology instead of a hope.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aimai_kit.prompts import PromptRegistry
from aimai_kit.provider.client import LLMClient
from langgraph.types import interrupt

from .assessment import assess_contract, reconcile
from .documents import load_document, verify_unchanged
from .sinks import crm_create_note, idempotency_key
from .state import ContractState, Decision, Finding

__all__ = [
    "build_nodes",
    "gate_payload",
    "needs_human",
    "route_after_assessment",
    "route_after_gate",
]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def gate_payload(state: ContractState) -> dict:
    """What the reviewer is shown while the flow is paused.

    Deliberately thin. The `interrupt()` payload is itself written into the
    checkpoint, so anything put here is stored a second time and stays stored
    for as long as the approval takes. The findings — quotes from the contract
    included — are already in the state; the payload references them by clause
    id and the API serves the full text from `get_state()` when the screen asks
    for it.
    """
    return {
        "contract_no": state["contract_no"],
        "overall_risk": state.get("overall_risk", "low"),
        "text_sha256": state.get("text_sha256", ""),
        "question": (
            f"Contract {state['contract_no']} was assessed as "
            f"{state.get('overall_risk', 'low')} risk. Approve the CRM note, "
            "correct the findings, or reject."
        ),
        "clauses": [
            {
                "clause_id": f.clause_id,
                "category": f.category,
                "severity": f.severity,
            }
            for f in state.get("findings", [])
        ],
    }


def needs_human(state: ContractState) -> bool:
    """Whether this contract stops for a human.

    Imported by the API so the two cannot disagree about what "risky" means.
    """
    from .state import ACTIONABLE_RISK, risk_rank

    return risk_rank(state.get("overall_risk", "low")) >= risk_rank(ACTIONABLE_RISK)


def route_after_assessment(state: ContractState) -> str:
    """The risk threshold lives here, in the topology.

    This is the single most important line in the stage. The alternative — a
    sentence in the prompt saying "ask for approval when the contract is
    risky" — makes the guardrail a request to a model that is free to decline
    it, that reads it differently after a prompt edit, and that can be talked
    out of it by text inside the very document under review. As an edge, it is
    a branch: it cannot be argued with, it is visible in the compiled graph,
    and `test_gate.py` can prove it holds for every fixture.
    """
    return "human_gate" if needs_human(state) else "execute_action"


def route_after_gate(state: ContractState) -> str:
    """A rejection ends the run; anything else proceeds to the side effect."""
    return "end" if state.get("decision") == "reject" else "execute_action"


def build_nodes(
    client: LLMClient,
    *,
    registry: PromptRegistry | None = None,
    prompt_key: str | None = None,
) -> dict:
    """Bind the reviewer into the four node functions.

    Closures rather than LangGraph's runtime context. The reviewer is a live
    object with a socket behind it; the context is per-invocation state that
    LangGraph carries around, and putting a client there invites someone to
    put it in the *graph* state next, where it would have to be serialized into
    the checkpoint. A closure keeps the boundary obvious: everything in the
    state is data, everything a node needs besides data is bound at build time.
    """
    prompts = registry or PromptRegistry("prompts")

    def fetch_document(state: ContractState) -> ContractState:
        """Resolve the URI, and pin the bytes with a hash."""
        document = load_document(state["document_uri"])
        return {
            "text_sha256": document.sha256,
            "fetched_at": _now(),
        }

    def assess_risk(state: ContractState) -> ContractState:
        """Extract findings, re-reading the document from its URI.

        `verify_unchanged` rather than `load_document`: between the fetch and
        this call the run may have crashed and resumed hours later, and
        assessing a revision of the document under the hash of the original
        would produce a report that cannot be traced to any text.
        """
        document = verify_unchanged(state["document_uri"], state["text_sha256"])
        kwargs = {"registry": prompts}
        if prompt_key:
            kwargs["prompt_key"] = prompt_key
        result = assess_contract(client, document, **kwargs)
        routed_risk, reconciled = reconcile(result.report)
        return {
            "findings": result.report.findings,
            "overall_risk": routed_risk,
            "summary": result.report.summary,
            "assessment_attempts": result.attempts,
            "risk_reconciled": reconciled,
        }

    def human_gate(state: ContractState) -> ContractState:
        """Pause until a human answers. NO SIDE EFFECT ABOVE THIS LINE.

        `interrupt()` returns the value passed to `Command(resume=...)`. Three
        shapes are accepted, because the screen and the tests should not have
        to agree on ceremony:

            "approve"                                  — a bare decision
            {"decision": "reject", "note": "..."}      — with a reason
            {"decision": "edit", "findings": [...]}    — with corrections

        Corrected findings go back through the state's own reducer, so a human
        correction is written exactly the way the model's output is written.
        """
        answer = interrupt(gate_payload(state))
        decision, note, corrected = _parse_answer(answer)
        update: ContractState = {
            "decision": decision,
            "decision_note": note,
            "decided_at": _now(),
        }
        if corrected:
            update["findings"] = corrected
        return update

    def execute_action(state: ContractState) -> ContractState:
        """The only side effect in the flow.

        The key is derived from the contract number, the document hash and the
        decision. Re-running this node — after a crash, after a duplicate
        resume, after a replay of the pending writes — produces the same key
        and therefore the same single note.
        """
        decision = state.get("decision", "auto")
        key = idempotency_key(
            state["contract_no"], state.get("text_sha256", ""), decision
        )
        findings = state.get("findings", [])
        body = (
            f"Contract {state['contract_no']} reviewed: "
            f"{state.get('overall_risk', 'low')} risk, {len(findings)} finding(s). "
            f"Decision: {decision}."
        )
        note = crm_create_note(
            key,
            state["contract_no"],
            body,
            {
                "document_uri": state["document_uri"],
                "text_sha256": state.get("text_sha256", ""),
                "overall_risk": state.get("overall_risk", "low"),
                "decision": decision,
                "clause_ids": [f.clause_id for f in findings],
            },
        )
        return {"action_receipt": note.idempotency_key}

    return {
        "fetch_document": fetch_document,
        "assess_risk": assess_risk,
        "human_gate": human_gate,
        "execute_action": execute_action,
    }


def _parse_answer(answer: object) -> tuple[Decision, str, list[Finding]]:
    """Normalize whatever came back through `Command(resume=...)`.

    An unrecognised answer becomes a rejection, not an approval. The failure
    mode of a typo in the approval screen must be "nothing happened", never
    "the note went out".
    """
    if isinstance(answer, str):
        decision, note, raw_findings = answer, "", []
    elif isinstance(answer, dict):
        decision = str(answer.get("decision", ""))
        note = str(answer.get("note", ""))
        raw_findings = answer.get("findings") or []
    else:
        decision, note, raw_findings = "", "", []

    if decision not in ("approve", "edit", "reject"):
        return "reject", f"unrecognised decision {decision!r}; treated as reject", []

    corrected = [
        f if isinstance(f, Finding) else Finding.model_validate(f) for f in raw_findings
    ]
    if decision == "edit" and not corrected:
        return "approve", note or "edit with no corrections; treated as approve", []
    return decision, note, corrected  # type: ignore[return-value]
