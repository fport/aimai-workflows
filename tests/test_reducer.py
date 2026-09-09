"""The findings reducer: order independence, idempotency, and correction."""

from __future__ import annotations

from aimai_workflows.contract.state import Finding, merge_findings


def finding(clause_id: str, severity: str = "high", summary: str = "s") -> Finding:
    return Finding(
        clause_id=clause_id,
        category="liability",
        severity=severity,  # type: ignore[arg-type]
        summary=summary,
        quote="quoted text",
    )


def test_merge_is_order_independent_for_distinct_clauses() -> None:
    """The property a checkpointer needs.

    Pending writes are replayed after a crash and parallel branches arrive in
    no defined order. A reducer whose result depends on arrival order makes the
    state after a resume differ from the state before the crash — silently.
    """
    a = [finding("7.2"), finding("3.1")]
    b = [finding("9.4")]

    forward = merge_findings(merge_findings([], a), b)
    backward = merge_findings(merge_findings([], b), a)

    assert [f.clause_id for f in forward] == [f.clause_id for f in backward]


def test_merge_is_idempotent() -> None:
    """Applying the same update twice is applying it once."""
    once = merge_findings([], [finding("7.2")])
    twice = merge_findings(once, [finding("7.2")])

    assert len(twice) == 1
    assert twice == once


def test_last_write_wins_on_the_same_clause() -> None:
    """This is how a human correction overrides the model."""
    merged = merge_findings(
        [finding("7.2", "critical", "model said")],
        [finding("7.2", "medium", "reviewer disagreed")],
    )

    assert len(merged) == 1
    assert merged[0].severity == "medium"
    assert merged[0].summary == "reviewer disagreed"


def test_worst_findings_sort_first() -> None:
    """Ordering is a property of the state, not of the template rendering it."""
    merged = merge_findings(
        [], [finding("1.1", "low"), finding("9.9", "critical"), finding("5.5", "high")]
    )

    assert [f.severity for f in merged] == ["critical", "high", "low"]


def test_none_is_treated_as_empty() -> None:
    """LangGraph calls a reducer with `None` for a key the state has not seen."""
    assert merge_findings(None, None) == []
    assert len(merge_findings(None, [finding("2.2")])) == 1
