"""Turning contract text into a `ReviewReport`.

The node in `nodes.py` does not build requests or parse JSON; it calls this.
The separation is what lets the same code path run against a real model and
against the rule-based reviewer in `stub_reviewer.py` — the tests exercise the
production path, not a simplified copy of it.

Everything provider-shaped comes from aimai-kit: `build_request` renders the
versioned prompt, wraps the contract in a trust boundary and fits it to the
context budget; `generate_structured` binds the schema and repairs a bounded
number of times. This repo adds one thing on top, and it is the reconciliation
below.

RECONCILIATION. The model reports `overall_risk` separately from the findings,
and the two can disagree. A report whose findings are all `low` but whose
`overall_risk` is `critical` is not noise — it usually means the model saw
something it failed to write down. Trusting `overall_risk` alone would route
on a number with no evidence behind it; trusting the findings alone would
discard the model's own warning. So the flow routes on the maximum of the two
and records that it had to, because a rising `risk_reconciled` rate is the
first sign the prompt has drifted.
"""

from __future__ import annotations

from dataclasses import dataclass

from aimai_kit.prompts import PromptRegistry, build_request
from aimai_kit.prompts.structured import SchemaBindingFailed, generate_structured
from aimai_kit.provider.client import LLMClient

from .documents import DocumentText
from .state import ReviewReport, Severity, risk_rank

__all__ = [
    "AssessmentFailed",
    "AssessmentResult",
    "DEFAULT_PROMPT",
    "assess_contract",
    "reconcile",
]

DEFAULT_PROMPT = "assess_risk@v1"


class AssessmentFailed(Exception):
    """The reviewer produced nothing valid within the attempt budget."""


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    """A report, plus what it cost to get it.

    `attempts` and `reconciled` are written into the state and end up in the
    measurement table. They are the two numbers that show a prompt regression
    before a human notices one.
    """

    report: ReviewReport
    attempts: int
    reconciled: bool
    prompt_ref: str


def reconcile(report: ReviewReport) -> tuple[Severity, bool]:
    """Return the risk the flow routes on, and whether the two sources
    disagreed.

    The maximum, not the average and not the model's own `overall_risk`: this
    decides whether a human sees the contract at all, and the cost of an
    unnecessary review is a few minutes while the cost of a missed critical
    clause is the contract.
    """
    stated = report.overall_risk
    worst = max(
        (f.severity for f in report.findings),
        key=risk_rank,
        default=stated,
    )
    if risk_rank(worst) > risk_rank(stated):
        return worst, True
    return stated, False


def assess_contract(
    client: LLMClient,
    document: DocumentText,
    *,
    registry: PromptRegistry | None = None,
    prompt_key: str = DEFAULT_PROMPT,
    max_attempts: int = 2,
    model: str = "gpt-4o",
) -> AssessmentResult:
    """Assess one contract and return a validated report.

    `max_attempts=2` rather than three. A repair round on a 3,000-token
    contract costs another full input pass, and by the second failure the
    problem is the prompt or the schema, not the model having a bad moment.
    """
    built = build_request(
        registry or PromptRegistry("prompts"),
        prompt_key,
        document.text,
        doc_id=document.sha256[:12],
        schema=ReviewReport,
        operation="assess_risk",
        model=model,
    )
    try:
        repaired = generate_structured(
            client, built.req, ReviewReport, max_attempts=max_attempts
        )
    except SchemaBindingFailed as error:
        raise AssessmentFailed(
            f"no valid report for {document.uri} after {error.attempts} attempts: "
            f"{'; '.join(error.last_errors)}"
        ) from error

    _, reconciled = reconcile(repaired.value)
    return AssessmentResult(
        report=repaired.value,
        attempts=repaired.attempts,
        reconciled=reconciled,
        prompt_ref=str(built.ref),
    )
