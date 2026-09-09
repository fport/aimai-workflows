"""The state schema for the contract review flow, and its reducers.

Three decisions in this file carry the whole stage, so they are written down
here rather than in the README alone.

**The document text is not in the state.** The state holds `document_uri` and
`text_sha256`; a node that needs the text reads it again. A checkpointer
serializes the whole state on every superstep, so a 400 KB contract carried in
the state is written to Postgres four times per run and again on every resume.
Worse, the text would sit in a durable store for the days a human approval can
take, which is a data-retention decision nobody made on purpose. The hash keeps
the guarantee that matters — that the document behind the URI did not change
between assessment and approval.

**Findings merge by `clause_id`, they do not append.** The obvious reducer for
a list is concatenation, and it is wrong here: the human gate returns corrected
findings, and with `operator.add` a correction would sit next to the finding it
corrects instead of replacing it. Merging on the clause id makes the human
correction and the model output the same kind of write.

**The reducer is order-independent.** Two updates applied in either order must
produce the same state. This is not a style preference: a checkpointer replays
pending writes after a crash, and LangGraph gives no ordering guarantee across
parallel branches. `test_reducer.py` pins it.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

__all__ = [
    "ACTIONABLE_RISK",
    "Category",
    "ContractState",
    "Decision",
    "Finding",
    "ReviewReport",
    "Severity",
    "merge_findings",
    "risk_rank",
]

Severity = Literal["low", "medium", "high", "critical"]
"""How bad one finding is.

Closed with `Literal` rather than left as `str` because this value drives a
conditional edge. An open string would let a model invent "moderate-high" and
route the flow into the branch nobody tested.
"""

Category = Literal[
    "liability",
    "payment",
    "termination",
    "intellectual_property",
    "data_protection",
    "compliance",
]
"""What kind of clause the finding is about.

Also closed, for a different reason: these categories are what the review UI
groups by and what the measurement table counts. A free-text category makes
both meaningless within a week.
"""

Decision = Literal["approve", "edit", "reject"]
"""The three answers a reviewer can give.

`edit` exists because the realistic human response to a model's risk report is
neither yes nor no — it is "this one is not critical, it is medium". Without
`edit` the reviewer's only way to correct a finding is to reject the whole
review, and the correction is lost.
"""

_RISK_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}

ACTIONABLE_RISK: Severity = "high"
"""At and above this level the flow stops for a human.

The threshold is a constant in the topology, not a sentence in the prompt. See
`graph.py` for why that distinction is the point of this stage.
"""


def risk_rank(severity: str) -> int:
    """Order severities so they can be compared.

    Unknown values sort highest. If a future model emits a severity this file
    does not know, the safe reading is "more dangerous than anything we know",
    which sends the review to a human rather than to the CRM.
    """
    return _RISK_ORDER.get(severity, len(_RISK_ORDER))


class Finding(BaseModel):
    """One risky clause.

    The field descriptions are instructions to the model, not documentation for
    us: they are what ends up in the JSON Schema the provider enforces.
    """

    clause_id: str = Field(
        description=(
            "Stable identifier of the clause, as printed in the contract "
            "(for example '7.2'). Use 'unnumbered-<n>' when the clause has no "
            "number."
        )
    )
    category: Category = Field(description="Which area of risk this clause falls in.")
    severity: Severity = Field(
        description=(
            "How much exposure this clause creates for us. Use 'critical' only "
            "for unlimited liability, uncapped indemnity, or an obligation that "
            "cannot be exited."
        )
    )
    summary: str = Field(
        max_length=280,
        description="One sentence, plain language, on what the clause does.",
    )
    quote: str = Field(
        description=(
            "The exact sentence from the contract that creates the risk, copied "
            "verbatim. Never paraphrase; this is what the reviewer checks."
        )
    )

    def key(self) -> str:
        """Identity for merging. Two findings with this key are the same finding."""
        return self.clause_id


class ReviewReport(BaseModel):
    """Everything the assessment step produces.

    `overall_risk` is asked for explicitly instead of being derived from the
    findings, because the two disagreeing is a signal worth seeing: a report
    whose findings are all 'low' but whose overall risk is 'critical' means the
    model saw something it failed to write down as a finding. `assess_risk`
    reconciles them and records when it had to.
    """

    findings: list[Finding] = Field(
        default_factory=list,
        description="Every clause that creates material risk. Empty is a valid answer.",
    )
    overall_risk: Severity = Field(
        description="The risk of the contract as a whole, not of its worst clause."
    )
    summary: str = Field(
        max_length=600,
        description=(
            "What a reviewer needs to know before deciding, in three sentences."
        ),
    )


def merge_findings(
    current: list[Finding] | None, incoming: list[Finding] | None
) -> list[Finding]:
    """Merge findings by `clause_id`, last write winning, in a stable order.

    Two properties matter and both are tested:

    - IDEMPOTENT. Applying the same update twice changes nothing. A resumed run
      replays the pending writes of the superstep that was interrupted, so a
      reducer that is not idempotent duplicates findings after every crash.
    - ORDER-INDEPENDENT AS A SET. The result is sorted by clause id, so two
      updates that touch different clauses produce the same state whichever
      order they arrive in. Only a genuine conflict (the same clause written
      twice) depends on order, and there last-write-wins is what makes the
      human correction override the model.

    The sort is by `(risk_rank, clause_id)` descending on risk: the reviewer
    reads the worst clause first, and the ordering is a property of the state
    rather than of the template that renders it.
    """
    merged: dict[str, Finding] = {f.key(): f for f in (current or [])}
    for finding in incoming or []:
        merged[finding.key()] = finding
    return sorted(merged.values(), key=lambda f: (-risk_rank(f.severity), f.clause_id))


class ContractState(TypedDict, total=False):
    """The durable state of one contract review.

    `total=False` throughout. A checkpoint written by an older version of this
    code is read back by a newer one, and every required key added later would
    make those old checkpoints unreadable — the resume path, which is the whole
    point of the stage, would break on deploy. `test_resume.py` pins that a
    checkpoint written without a key added later still resumes.
    """

    # Identity. `contract_no` is also what the thread id is derived from.
    contract_no: str
    document_uri: str
    text_sha256: str
    fetched_at: str

    # Assessment output.
    findings: Annotated[list[Finding], merge_findings]
    overall_risk: Severity
    summary: str
    assessment_attempts: int
    risk_reconciled: bool

    # Human gate.
    decision: Decision
    decision_note: str
    decided_at: str

    # Side effect.
    action_receipt: str
    action_skipped_reason: str
