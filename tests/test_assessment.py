"""Assessment: the repair loop on the real path, and risk reconciliation."""

from __future__ import annotations

import pytest

from aimai_workflows.contract import (
    AssessmentFailed,
    RuleBasedReviewer,
    assess_contract,
    load_document,
    reconcile,
)
from aimai_workflows.contract.state import Finding, ReviewReport


def report(*severities: str, stated: str = "low") -> ReviewReport:
    return ReviewReport(
        findings=[
            Finding(
                clause_id=f"{i + 1}.1",
                category="liability",
                severity=s,  # type: ignore[arg-type]
                summary="s",
                quote="q",
            )
            for i, s in enumerate(severities)
        ],
        overall_risk=stated,  # type: ignore[arg-type]
        summary="summary",
    )


def test_reconcile_takes_the_worse_of_the_two_sources() -> None:
    """A model that lists a critical clause and calls the contract low risk.

    Trusting `overall_risk` would route this straight to the CRM.
    """
    routed, reconciled = reconcile(report("critical", "low", stated="low"))

    assert routed == "critical"
    assert reconciled is True


def test_reconcile_keeps_a_higher_stated_risk() -> None:
    """The model is allowed to be more worried than its own findings.

    Three medium clauses that interact badly are a real reason to call a
    contract high risk, and lowering it to the worst finding would discard the
    model's judgement.
    """
    routed, reconciled = reconcile(report("medium", "medium", stated="high"))

    assert routed == "high"
    assert reconciled is False


def test_the_repair_loop_recovers_from_unparseable_output(registry) -> None:
    """aimai-kit's repair loop, exercised through the production call path."""
    document = load_document("high_risk.txt")
    result = assess_contract(
        RuleBasedReviewer(invalid_attempts=1), document, registry=registry
    )

    assert result.attempts == 2
    assert result.report.findings


def test_a_reviewer_that_never_parses_fails_loudly(registry) -> None:
    """Two attempts, then an exception — never a silent empty report.

    An empty `findings` list is a valid answer from a working reviewer, so a
    broken one must not be allowed to produce the same shape.
    """
    document = load_document("high_risk.txt")

    with pytest.raises(AssessmentFailed, match="no valid report"):
        assess_contract(
            RuleBasedReviewer(invalid_attempts=5), document, registry=registry
        )


def test_the_prompt_reference_is_recorded(registry) -> None:
    """A report that cannot be traced to a prompt version is unmeasurable."""
    result = assess_contract(
        RuleBasedReviewer(), load_document("low_risk.txt"), registry=registry
    )

    assert result.prompt_ref.startswith("assess_risk@v1")
