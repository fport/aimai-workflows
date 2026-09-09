"""Version 2 of 4 — LangGraph.

The same five steps as `plain.py`, with the state machine handed to the
framework. What changes is not the code you write per step; it is what you stop
writing: no step column, no `match`, no hand-rolled "where was I" logic. The
checkpointer records every superstep and `interrupt()` parks the run without
occupying anything.

What you get beyond `plain.py`, concretely:

- `get_state_history(config)` — every intermediate state, for free. The plain
  version keeps one row and cannot answer "what did classification return
  before the human edited it".
- Resumption is the framework's job. `invoke(Command(resume=…))` is one call
  against a thread id; the plain version reads a row and re-enters a `match`.
- Adding a branch is `add_conditional_edges`, not an edit in two places.

What you pay: a dependency with its own release cadence, a state schema that
has to be serializable, and a mental model — supersteps, channels, reducers,
replay — that a new team member has to learn before they can debug anything.

The prompts and the business logic are untouched imports from `core`. That is
the lock-in measurement: moving this flow to another framework means rewriting
this file and nothing else.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, TypedDict

from aimai_kit.prompts import PromptRegistry
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..contract.checkpointer import serializer
from .contract import Decision, RunOutcome, run_cli, state_db
from .core import (
    Classification,
    CountingClient,
    Draft,
    classify,
    draft_reply,
    is_risky,
    load_ticket,
    retrieve_articles,
    send_reply,
)
from .stub_support import RuleBasedSupportModel

__all__ = ["LangGraphStack", "SupportState", "build"]

NAME = "langgraph"


def _last(current, incoming):
    """Last write wins.

    The default reducer already does this; naming it makes the state schema say
    what it means and gives one place to change if a key ever needs merging.
    """
    return incoming if incoming is not None else current


class SupportState(TypedDict, total=False):
    """`total=False` for the same reason as stage 06: old checkpoints must
    survive a deploy that adds a key."""

    ticket_id: Annotated[str, _last]
    classification: Annotated[dict, _last]
    articles: Annotated[list[str], _last]
    draft: Annotated[dict, _last]
    decision: Annotated[str, _last]
    receipt: Annotated[str, _last]


def _thread(ticket_id: str) -> dict:
    return {"configurable": {"thread_id": f"ticket-{ticket_id}"}}


class LangGraphStack:
    """The support flow as a graph over a SQLite checkpointer."""

    name = NAME

    def __init__(self, client=None, registry: PromptRegistry | None = None) -> None:
        self.client = CountingClient(client or RuleBasedSupportModel())
        self.registry = registry or PromptRegistry("prompts")
        self._conn = sqlite3.connect(state_db(NAME), check_same_thread=False)
        saver = SqliteSaver(self._conn, serde=serializer())
        saver.setup()
        self.graph = self._build().compile(checkpointer=saver)

    def _build(self) -> StateGraph:
        def triage(state: SupportState) -> SupportState:
            ticket = load_ticket(state["ticket_id"])
            classification = classify(self.client, ticket, registry=self.registry)
            articles = retrieve_articles(ticket, classification)
            return {
                "classification": classification.model_dump(),
                "articles": [a.article_id for a in articles],
            }

        def compose(state: SupportState) -> SupportState:
            ticket = load_ticket(state["ticket_id"])
            classification = Classification.model_validate(state["classification"])
            articles = retrieve_articles(ticket, classification)
            draft = draft_reply(
                self.client, ticket, classification, articles, registry=self.registry
            )
            return {"draft": draft.model_dump()}

        def approval_gate(state: SupportState) -> SupportState:
            # No side effect above this line — see stage 06's README for the
            # replay semantics that make that a rule rather than a preference.
            answer = interrupt(
                {
                    "ticket_id": state["ticket_id"],
                    "risk": state["classification"]["risk"],
                    "question": "Send this reply?",
                }
            )
            decision = answer if isinstance(answer, str) else str(answer)
            return {
                "decision": decision if decision in ("approve", "reject") else "reject"
            }

        def deliver(state: SupportState) -> SupportState:
            draft = Draft.model_validate(state["draft"])
            receipt = send_reply(state["ticket_id"], draft.text, stack=NAME)
            return {"receipt": receipt.idempotency_key}

        def needs_approval(state: SupportState) -> str:
            ticket = load_ticket(state["ticket_id"])
            classification = Classification.model_validate(state["classification"])
            return "approval_gate" if is_risky(ticket, classification) else "deliver"

        def after_gate(state: SupportState) -> str:
            return "deliver" if state.get("decision") == "approve" else "end"

        graph = StateGraph(SupportState)
        graph.add_node("triage", triage)
        graph.add_node("compose", compose)
        graph.add_node("approval_gate", approval_gate)
        graph.add_node("deliver", deliver)
        graph.add_edge(START, "triage")
        graph.add_edge("triage", "compose")
        graph.add_conditional_edges(
            "compose",
            needs_approval,
            {"approval_gate": "approval_gate", "deliver": "deliver"},
        )
        graph.add_conditional_edges(
            "approval_gate", after_gate, {"deliver": "deliver", "end": END}
        )
        graph.add_edge("deliver", END)
        return graph

    # --- the two verbs ---------------------------------------------------

    def run(self, ticket_id: str) -> RunOutcome:
        config = _thread(ticket_id)
        state = self.graph.get_state(config)
        if state.created_at and not state.next:
            return self._outcome(ticket_id, resumed=True)
        if state.created_at and any(task.interrupts for task in state.tasks):
            # Parked on the gate: a second `run` is a retry, and answering it
            # is `approve`'s job.
            return self._outcome(ticket_id, resumed=True)
        if state.created_at:
            # Killed mid-run. Resuming is `invoke(None, config)` — no branch per
            # step, no resume path to keep in sync with the flow.
            self.graph.invoke(None, config, durability="sync")
            return self._outcome(ticket_id, resumed=True)
        self.graph.invoke({"ticket_id": ticket_id}, config, durability="sync")
        return self._outcome(ticket_id)

    def approve(self, ticket_id: str, decision: Decision = "approve") -> RunOutcome:
        config = _thread(ticket_id)
        state = self.graph.get_state(config)
        if not state.created_at:
            raise KeyError(f"no run for {ticket_id}; call run first")
        if not state.next:
            return self._outcome(ticket_id, resumed=True)
        self.graph.invoke(Command(resume=decision), config, durability="sync")
        return self._outcome(ticket_id, resumed=True)

    def _outcome(self, ticket_id: str, *, resumed: bool = False) -> RunOutcome:
        config = _thread(ticket_id)
        state = self.graph.get_state(config)
        values = state.values or {}
        if state.next:
            status = "awaiting_approval"
        elif values.get("receipt"):
            status = "completed"
        elif values.get("decision") == "reject":
            status = "rejected"
        else:
            status = "unknown"
        return RunOutcome(
            ticket_id=ticket_id,
            stack=NAME,
            status=status,  # type: ignore[arg-type]
            risk=values.get("classification", {}).get("risk", "unknown"),
            # `or None`: LangGraph initialises a declared channel to its type's
            # empty value, so an unwritten key reads back as "" rather than as
            # missing. A caller checking `receipt is None` would be wrong.
            receipt=values.get("receipt") or None,
            draft=(values.get("draft") or {}).get("text"),
            llm_calls=self.client.calls,
            resumed=resumed,
            detail={
                "supersteps": len(
                    list(self.graph.get_state_history(_thread(ticket_id)))
                )
            },
        )


def build() -> LangGraphStack:
    return LangGraphStack()


if __name__ == "__main__":
    raise SystemExit(run_cli(build))
