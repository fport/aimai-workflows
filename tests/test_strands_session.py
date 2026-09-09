"""What Strands does that the other two agent SDKs do not.

The parametrized suite in `test_stack_contract.py` proves Strands meets the
same contract as the other four. This file is about the one property that puts
it in a different quarter of the comparison: the model decides the sequence
*and* the framework keeps the state.
"""

from __future__ import annotations

import pytest

from aimai_workflows.stacks.core import sent_replies
from aimai_workflows.stacks.strands_stack import StrandsStack

RISKY = "T-1000"


@pytest.fixture
def stack(registry) -> StrandsStack:
    return StrandsStack(None, registry)


def test_a_restarted_agent_knows_it_is_parked(stack, registry) -> None:
    """A brand new Agent over the same session comes back already interrupted.

    This is the finding. pydantic-ai and the Agents SDK both hand the paused
    state back to the caller to store and re-supply; Strands writes it to the
    session and restores it when an agent is constructed. A restarted worker
    that builds a fresh agent is parked before it does anything.

    Reaching for `_interrupt_state` is touching a private attribute, and the
    stack itself does not do that — it records the interrupt id from the run's
    public result. The private access is confined to this test, because the
    claim being made is about the SDK's behaviour rather than about the stack.
    """
    stack.run(RISKY)

    restarted = StrandsStack(None, registry)._agent(RISKY)
    state = restarted._interrupt_state

    assert state.activated is True
    assert list(state.interrupts.values())[0].name == "approve_send"
    assert list(state.interrupts.values())[0].reason["ticket_id"] == RISKY


def test_the_session_is_readable_json(stack) -> None:
    """The paused run is inspectable during an incident.

    A different trade from the Agents SDK's single opaque blob: more files,
    and a person can read them.
    """
    import json
    from pathlib import Path

    stack.run(RISKY)
    session_dir = Path(stack._session_dir()) / f"session_ticket-{RISKY}"

    agent_state = json.loads(
        (session_dir / "agents/agent_default/agent.json").read_text()
    )
    interrupts = agent_state["_internal_state"]["interrupt_state"]["interrupts"]

    assert (session_dir / "session.json").is_file()
    assert list((session_dir / "agents/agent_default/messages").glob("*.json"))
    assert len(interrupts) == 1


def test_the_tool_resumes_without_re_sending(stack, registry) -> None:
    """The interrupt is inside the tool that performs the send.

    The same rule as LangGraph's `interrupt()` applies — the tool is re-entered
    from its first line — so the send must sit below the interrupt call. If it
    did not, this test would find two replies.
    """
    stack.run(RISKY)
    StrandsStack(None, registry).approve(RISKY, "approve")

    assert len(sent_replies(RISKY)) == 1


def test_a_rejection_leaves_the_outbox_empty(stack, registry) -> None:
    stack.run(RISKY)
    outcome = StrandsStack(None, registry).approve(RISKY, "reject")

    assert outcome.status == "rejected"
    assert sent_replies(RISKY) == []
