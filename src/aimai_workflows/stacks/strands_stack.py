"""Version 5 of 5 — Strands Agents.

Added last, and it breaks the pattern the other three drew.

The first four versions fall into two camps: the two graph versions hand the
state machine to a framework (or write it by hand) and keep control of the
sequence; the two agent SDKs hand the sequence to the model and hand the state
back to *you*. Strands is the fourth quarter of that square — **the model
decides the sequence and the framework keeps the state** — and it is the only
one of the five that does both.

Concretely, three things it does that neither other agent SDK does:

**The interrupt lives in the tool and is persisted by the session.** A tool
calls `tool_context.interrupt(...)`, the run ends with `stop_reason="interrupt"`,
and the session manager writes the pending tool execution to disk. A brand new
`Agent` built over the same `session_id` in another process comes back already
knowing it is parked — `test_a_restarted_strands_agent_knows_it_is_parked`
asserts exactly that.

**Resuming is a first-class input.** `agent([{"interruptResponse": {...}}])`,
not a bespoke replay of a message history. The shape is the SDK's, and so is
the guarantee that the tool resumes where it stopped rather than from the top.

**The session format is inspectable.** `session.json`, `agent.json` and one
file per message, all JSON. That is a different trade from the Agents SDK's
single opaque blob: bigger on disk, and readable by a person during an
incident.

The prompts and the business logic are untouched imports from `core`, as in
every other version. What this file contains is the model shim, three tools and
two verbs.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aimai_kit.prompts import PromptRegistry
from strands import Agent, ToolContext, tool
from strands.models.model import Model
from strands.session import FileSessionManager

from .contract import Decision, RunOutcome, run_cli, state_db
from .core import (
    Classification,
    CountingClient,
    classify,
    draft_reply,
    is_risky,
    load_ticket,
    retrieve_articles,
    send_reply,
)
from .stub_support import RuleBasedSupportModel

__all__ = ["ScriptedStrandsModel", "StrandsStack", "build"]

NAME = "strands"
_TOOL_ORDER = ("triage", "compose", "deliver")


class ScriptedStrandsModel(Model):
    """A deterministic stand-in for the model's control decisions.

    Strands models stream Bedrock-shaped events, so this yields the five events
    a tool call is made of rather than returning an object. Determinism is what
    makes the benchmark measure the framework rather than the weather — the
    same reason the other three versions have scripted models.
    """

    def __init__(self) -> None:
        self.turns = 0

    def get_config(self) -> dict:
        return {}

    def update_config(self, **kwargs: Any) -> None:
        return None

    async def structured_output(
        self, output_model, prompt, system_prompt=None, **kwargs
    ):
        raise NotImplementedError("this comparison never asks for structured output")
        yield {}  # pragma: no cover - makes the method an async generator

    async def stream(
        self,
        messages: list[dict],
        tool_specs: list[dict] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[dict]:
        self.turns += 1
        called = {
            block["toolUse"]["name"]
            for message in messages
            for block in message.get("content", [])
            if isinstance(block, dict) and "toolUse" in block
        }
        ticket_id = _ticket_id_from(messages)

        for index, tool_name in enumerate(_TOOL_ORDER):
            if tool_name in called:
                continue
            call_id = f"call_{index}_{ticket_id}"
            yield {"messageStart": {"role": "assistant"}}
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": call_id, "name": tool_name}}
                }
            }
            yield {
                "contentBlockDelta": {
                    "delta": {
                        "toolUse": {"input": json.dumps({"ticket_id": ticket_id})}
                    }
                }
            }
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return

        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": "handled"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


class StrandsStack:
    """The support flow as a Strands agent with a session-backed interrupt."""

    name = NAME

    def __init__(self, client=None, registry: PromptRegistry | None = None) -> None:
        self.client = CountingClient(client or RuleBasedSupportModel())
        self.registry = registry or PromptRegistry("prompts")
        self.model = ScriptedStrandsModel()
        self._scratch: dict[str, dict] = {}
        self._connect().close()

    # --- the agent -------------------------------------------------------

    def _agent(self, ticket_id: str) -> Agent:
        """Build an agent bound to this ticket's session.

        Rebuilt per call on purpose: that is what a restarted worker does, and
        a stack that only works when the same object is reused would be hiding
        the property the benchmark is measuring.
        """
        stack = self

        @tool
        def triage(ticket_id: str) -> str:
            """Classify a support ticket."""
            ticket = load_ticket(ticket_id)
            classification = classify(stack.client, ticket, registry=stack.registry)
            stack._remember(ticket_id, classification=classification.model_dump())
            return classification.model_dump_json()

        @tool
        def compose(ticket_id: str) -> str:
            """Draft a reply for a classified ticket."""
            ticket = load_ticket(ticket_id)
            classification = Classification.model_validate(
                stack._recall(ticket_id)["classification"]
            )
            articles = retrieve_articles(ticket, classification)
            draft = draft_reply(
                stack.client, ticket, classification, articles, registry=stack.registry
            )
            stack._remember(ticket_id, draft=draft.model_dump())
            return draft.model_dump_json()

        @tool(context=True)
        def deliver(ticket_id: str, tool_context: ToolContext) -> str:
            """Send the drafted reply to the customer."""
            ticket = load_ticket(ticket_id)
            classification = Classification.model_validate(
                stack._recall(ticket_id)["classification"]
            )
            if is_risky(ticket, classification):
                # Raises on the first pass and returns the human's answer on the
                # resumed one — the same shape as LangGraph's `interrupt()`, and
                # with the same rule: nothing above this line may have a side
                # effect, because the tool is re-entered from the top.
                decision = tool_context.interrupt(
                    name="approve_send",
                    reason={
                        "ticket_id": ticket_id,
                        "risk": classification.risk,
                        "question": "Send this reply?",
                    },
                )
                if decision != "approve":
                    stack._remember(ticket_id, rejected=True)
                    return "rejected by the reviewer"

            draft = stack._recall(ticket_id)["draft"]
            receipt = send_reply(ticket_id, draft["text"], stack=NAME)
            stack._remember(ticket_id, receipt=receipt.idempotency_key)
            return receipt.idempotency_key

        return Agent(
            model=self.model,
            tools=[triage, compose, deliver],
            # Off, or the SDK prints the model's output and every tool call to
            # stdout. The CLI's contract is one JSON object; a framework
            # narrating over it would break every caller that parses the output.
            callback_handler=None,
            system_prompt=(
                "Triage the ticket, compose a reply, then deliver it. "
                "Use one tool at a time."
            ),
            session_manager=FileSessionManager(
                session_id=f"ticket-{ticket_id}", storage_dir=self._session_dir()
            ),
        )

    # --- state -----------------------------------------------------------

    def _session_dir(self) -> str:
        """Where Strands keeps its sessions.

        Next to the other stacks' state files, so the chaos script can point
        every stack at one temporary directory.
        """
        directory = Path(state_db(NAME)).with_suffix("")
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory)

    def _remember(self, ticket_id: str, **fields: Any) -> None:
        self._scratch.setdefault(ticket_id, {}).update(fields)
        self._save(ticket_id)

    def _recall(self, ticket_id: str) -> dict:
        if ticket_id not in self._scratch:
            stored = self._load(ticket_id)
            if stored:
                self._scratch[ticket_id] = stored[2]
        return self._scratch.get(ticket_id, {})

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(state_db(NAME), timeout=10)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                ticket_id    TEXT PRIMARY KEY,
                status       TEXT NOT NULL,
                interrupt_id TEXT NOT NULL,
                context      TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
            """
        )
        return conn

    def _save(
        self, ticket_id: str, status: str | None = None, interrupt_id: str | None = None
    ) -> None:
        """Record the status and the pending interrupt id.

        The *conversation* is Strands' business — this table holds only what the
        two verbs need to find each other. The interrupt id is here because the
        SDK exposes it on a run's result but not through a public accessor on a
        freshly built agent; the session does know it (see
        `test_a_restarted_strands_agent_knows_it_is_parked`), but reading it back
        means touching a private attribute, and a portfolio repository should not
        model that.
        """
        current = self._load(ticket_id)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (ticket_id, status, interrupt_id, context, "
                "updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(ticket_id) DO UPDATE SET status = excluded.status, "
                "interrupt_id = excluded.interrupt_id, context = excluded.context, "
                "updated_at = excluded.updated_at",
                (
                    ticket_id,
                    status if status is not None else (current[0] if current else ""),
                    interrupt_id
                    if interrupt_id is not None
                    else (current[1] if current else ""),
                    json.dumps(self._scratch.get(ticket_id, {}), ensure_ascii=False),
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )

    def _load(self, ticket_id: str) -> tuple[str, str, dict] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, interrupt_id, context FROM runs WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
        return (row[0], row[1], json.loads(row[2])) if row else None

    # --- the two verbs ---------------------------------------------------

    def run(self, ticket_id: str) -> RunOutcome:
        stored = self._load(ticket_id)
        if stored is not None and stored[0]:
            return self._outcome(ticket_id, stored[0], stored[2], resumed=True)

        result = self._agent(ticket_id)(f"Handle support ticket {ticket_id}.")
        interrupts = list(getattr(result, "interrupts", []) or [])
        status = "awaiting_approval" if interrupts else "completed"
        self._remember(ticket_id)
        self._save(
            ticket_id,
            status=status,
            interrupt_id=interrupts[0].id if interrupts else "",
        )
        return self._outcome(ticket_id, status, self._recall(ticket_id))

    def approve(self, ticket_id: str, decision: Decision = "approve") -> RunOutcome:
        stored = self._load(ticket_id)
        if stored is None:
            raise KeyError(f"no run for {ticket_id}; call run first")
        status, interrupt_id, context = stored
        self._scratch[ticket_id] = context
        if status != "awaiting_approval":
            return self._outcome(ticket_id, status, context, resumed=True)

        # A brand new agent over the same session: what a restarted worker does.
        self._agent(ticket_id)(
            [{"interruptResponse": {"interruptId": interrupt_id, "response": decision}}]
        )
        final = "completed" if decision == "approve" else "rejected"
        self._save(ticket_id, status=final, interrupt_id="")
        return self._outcome(ticket_id, final, self._recall(ticket_id), resumed=True)

    def _outcome(
        self, ticket_id: str, status: str, context: dict, *, resumed: bool = False
    ) -> RunOutcome:
        classification = context.get("classification") or {}
        draft = context.get("draft") or {}
        return RunOutcome(
            ticket_id=ticket_id,
            stack=NAME,
            status=status,  # type: ignore[arg-type]
            risk=classification.get("risk", "unknown"),
            receipt=context.get("receipt"),
            draft=draft.get("text"),
            llm_calls=self.client.calls + self.model.turns,
            resumed=resumed,
            detail={"model_turns": self.model.turns},
        )


def _ticket_id_from(messages: list[dict]) -> str:
    """Recover the ticket id from the conversation.

    On resume the prompt is a list of interrupt responses rather than text, so
    the id has to come from the history — which the session has already
    restored by the time the model is called.
    """
    for message in messages:
        for block in message.get("content", []):
            if isinstance(block, dict):
                text = block.get("text", "")
                if "ticket T-" in text:
                    return text.split("ticket ")[1].strip(" .")
                use = block.get("toolUse", {})
                if isinstance(use, dict) and isinstance(use.get("input"), dict):
                    candidate = use["input"].get("ticket_id")
                    if candidate:
                        return str(candidate)
    raise ValueError("no ticket id in the conversation")


def build() -> StrandsStack:
    return StrandsStack()


if __name__ == "__main__":
    raise SystemExit(run_cli(build))
