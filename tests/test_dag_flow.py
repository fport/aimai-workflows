"""The real flow: compensation, the supervisor's constraints, and dependency
reality.

`test_every_required_edge_is_real` is the one worth reading. A `requires` edge
that does not change the answer is a lie that costs parallelism, and nothing
else in a codebase ever catches it.
"""

from __future__ import annotations

import asyncio

import pytest

from aimai_workflows.dagrun.execute import execute
from aimai_workflows.dagrun.flows.due_diligence import build_flow
from aimai_workflows.dagrun.report import as_dict, render
from aimai_workflows.dagrun.store import open_store
from aimai_workflows.dagrun.supervisor import (
    MAX_STEPS,
    build_supervisor,
    merge_findings,
)
from aimai_workflows.dagrun.types import NodeState
from aimai_workflows.dagrun.validate import validate
from aimai_workflows.stacks.core import sent_replies

SEED = "SZL-2026-0431"


def run_flow(seed: str = SEED, **kwargs):
    with open_store(":memory:") as store:
        return asyncio.run(execute(validate(build_flow(**kwargs)), seed, store))


def test_the_clean_run_delivers_once() -> None:
    report = run_flow()

    assert as_dict(report)["outcome"] == "clean"
    assert len(sent_replies(SEED)) == 1
    assert report.records["retract"].state is NodeState.SKIPPED


def test_a_failed_optional_specialist_degrades_the_opinion() -> None:
    """And the opinion says so, in its own text.

    Logging the missing source would not be enough: the paragraph a reader acts
    on has to carry the caveat.
    """
    report = run_flow(caselaw_fails=True)
    opinion = report.records["synthesis"].result.output["opinion"]

    assert as_dict(report)["outcome"] == "degraded"
    assert report.records["synthesis"].state is NodeState.DEGRADED
    assert "INCOMPLETE" in opinion and "caselaw" in opinion
    assert len(sent_replies(SEED)) == 1, "a degraded opinion is still delivered"


def test_a_degraded_run_never_reads_as_clean() -> None:
    report = run_flow(caselaw_fails=True)
    text = render(report)

    assert "missing data" in text
    assert "caselaw" in text


def test_a_failure_after_the_side_effect_triggers_the_retraction() -> None:
    """The saga case: the opinion went out and the filing did not."""
    report = run_flow(archive_fails=True)
    sent = sent_replies(SEED)

    assert report.records["archive"].state is NodeState.FAILED
    assert report.records["retract"].state is NodeState.COMPENSATED
    assert report.records["deliver"].state is NodeState.COMPENSATED
    assert len(sent) == 2
    # By text rather than by position: two messages written in the same second
    # sort by key, and a test that depends on that ordering fails for a reason
    # that has nothing to do with the behaviour under test.
    assert any("RETRACTED" in note.text for note in sent)


def test_an_unrelated_failure_does_not_retract_a_good_delivery() -> None:
    """The rule that is easy to get wrong.

    An optional case-law lookup timing out has no bearing on whether the
    opinion should have been sent. Retracting it because an unrelated branch
    failed would be worse than the outage.
    """
    report = run_flow(caselaw_fails=True)

    assert report.records["caselaw"].state is NodeState.FAILED
    assert report.records["deliver"].state is NodeState.DEGRADED
    assert report.records["retract"].state is NodeState.SKIPPED
    assert len(sent_replies(SEED)) == 1


def test_a_side_effect_that_raised_is_still_compensated() -> None:
    """A send that raised is not a send that did not happen."""
    report = run_flow(deliver_fails=True)

    assert report.records["deliver"].state is NodeState.COMPENSATED
    assert any("RETRACTED" in note.text for note in sent_replies(SEED))


def test_a_failed_compensation_escalates_instead_of_retrying() -> None:
    """One automatic recovery layer. The second is how one bad state becomes
    two, and the second one has no runbook."""
    report = run_flow(archive_fails=True, retract_fails=True)

    assert report.needs_human is True
    assert as_dict(report)["outcome"] == "needs_human"
    assert "still in place" in report.stopped_reason
    assert report.records["retract"].state is NodeState.FAILED


def test_the_compensation_is_idempotent() -> None:
    """Running the whole job twice retracts once."""
    run_flow(archive_fails=True)
    run_flow(archive_fails=True)

    retractions = [n for n in sent_replies(SEED) if "RETRACTED" in n.text]
    assert len(retractions) == 1


@pytest.mark.parametrize(
    ("node_id", "dropped"),
    [("synthesis", "statute"), ("synthesis", "clause_review"), ("archive", "deliver")],
)
def test_every_required_edge_is_real(node_id: str, dropped: str) -> None:
    """Remove a hard dependency; the node's output must change.

    An edge that does not change the answer is not a dependency — it is a
    serialisation of work that could have run in parallel, and nothing else
    catches it. The test drops one edge at a time and compares the output of
    the node that declared it.
    """
    nodes = build_flow()
    baseline = _output_of(nodes, node_id)

    without = tuple(
        (
            node
            if node.id != node_id
            else type(node)(
                id=node.id,
                kind=node.kind,
                run=node.run,
                version=node.version,
                requires=tuple(r for r in node.requires if r != dropped),
                optional=node.optional,
                compensates=node.compensates,
                expands=node.expands,
                side_effect=node.side_effect,
            )
        )
        for node in nodes
    )
    changed = _output_of(without, node_id)

    assert baseline != changed, (
        f"{node_id} declares {dropped} as required but produces the same output "
        "without it; the edge is costing parallelism and buying nothing"
    )


def _output_of(nodes, node_id: str) -> str:
    with open_store(":memory:") as store:
        report = asyncio.run(execute(validate(nodes), f"edge-{node_id}", store))
    record = report.records[node_id]
    return repr(record.result.output if record.result else record.state)


# --- the supervisor ---------------------------------------------------------


def test_the_supervisor_visits_each_specialist_once() -> None:
    out = build_supervisor().invoke({"seed": "S", "clauses": [{"id": "5.1"}]})

    assert out["visited"] == ["statute", "caselaw"]
    assert "INCOMPLETE" not in out["opinion"]


def test_a_supervisor_that_keeps_choosing_the_same_expert_still_terminates() -> None:
    """Constraint 1: completed specialists leave the candidate list.

    Without it, the cheapest failure mode of every supervisor appears
    immediately — the model asks for the same expert forever because its last
    answer was useful.
    """
    out = build_supervisor(chooser=lambda state, outstanding: "statute").invoke(
        {"seed": "S", "clauses": []}
    )

    assert out["visited"] == ["statute", "caselaw"]
    assert out["steps"] <= MAX_STEPS


def test_a_hallucinated_specialist_falls_back_rather_than_raising() -> None:
    """Constraint 3: a routing mistake is recoverable; a crash is not."""
    out = build_supervisor(chooser=lambda state, outstanding: "notary-expert").invoke(
        {"seed": "S", "clauses": []}
    )

    assert out["visited"] == ["statute", "caselaw"]
    assert any("is not available" in line for line in out["route_log"])


def test_the_step_ceiling_is_enforced_in_code() -> None:
    """Constraint 2: a hard ceiling, not a prompt instruction."""
    out = build_supervisor().invoke({"seed": "S", "clauses": [], "steps": MAX_STEPS})

    assert out["steps"] == MAX_STEPS + 1
    assert "ceiling" in out["route_log"][0]
    assert not out.get("visited"), "no specialist runs once the ceiling is hit"


def test_the_findings_reducer_is_order_independent() -> None:
    """Two specialists writing in parallel must produce the same state whichever
    lands first."""
    first = merge_findings(merge_findings({}, {"statute": "a"}), {"caselaw": "b"})
    second = merge_findings(merge_findings({}, {"caselaw": "b"}), {"statute": "a"})

    assert first == second
    assert merge_findings(first, {"statute": "a"}) == first
