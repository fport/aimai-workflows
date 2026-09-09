"""Version 4 of 4 — OpenAI Agents SDK.

The version where the most control sits with the model. The agent is given
tools and a goal; which tool runs next is a model decision, and the SDK's job is
to keep that loop, its approvals and its serialized state straight.

The human-in-the-loop mechanism is `needs_approval` on the tool. It accepts a
callable, so the risk rule from `core` can decide per call rather than marking
every send as approval-worthy — which matters, because the alternative
(`needs_approval=True`) would stop all 50 tickets instead of the 15 that need a
person.

State is a `RunState`: `to_json()` on the way out, `RunState.from_json(agent,
blob)` on the way back. Like pydantic-ai, there is no checkpointer and this
file chooses where the blob lives; unlike pydantic-ai, the blob contains the
whole run — items, usage, approvals and the tool-use tracker. For the same
paused ticket that costs 11.4 KB against pydantic-ai's 4.4 KB and 461 bytes for
the hand-written version (`results/chaos.md`). That is the price of the SDK
reconstructing the loop for you.

Two operational notes, since the repo is about operations:

- **Tracing goes to OpenAI by default.** With no `OPENAI_API_KEY` the exporter
  logs and gives up; in production it ships run data to a third party unless
  `set_tracing_disabled(True)` or a custom processor says otherwise. This file
  disables it, deliberately and visibly.
- **`from_json` is async** while `Runner.run_sync` is not, so a synchronous
  CLI has to bridge. Small, but the kind of thing that decides whether a
  framework fits an existing codebase.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from agents import Agent, Runner, RunState, function_tool, set_tracing_disabled
from agents.items import ModelResponse, TResponseInputItem
from agents.models.interface import Model
from agents.usage import Usage
from aimai_kit.prompts import PromptRegistry
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

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

__all__ = ["OpenAIAgentsStack", "ScriptedModel", "build"]

NAME = "openai-agents"

# Off by default here. A comparison repo that quietly uploads its runs to a
# vendor while measuring "operational behaviour" would be measuring the wrong
# thing and doing the wrong thing.
set_tracing_disabled(True)

_TOOL_ORDER = ("triage", "compose", "deliver")


class ScriptedModel(Model):
    """A deterministic stand-in for the model's control decisions.

    It plays exactly the part a model plays in this SDK — choosing the next tool
    — by looking at which tools have already been called. Determinism is what
    makes the benchmark measure the framework rather than the weather.
    """

    def __init__(self) -> None:
        self.turns = 0

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: Any,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any = None,
        **kwargs: Any,
    ) -> ModelResponse:
        self.turns += 1
        ticket_id = _ticket_id_from(input)
        called = _called_tools(input)
        for tool_name in _TOOL_ORDER:
            if tool_name not in called:
                call_id = f"call_{tool_name}_{ticket_id}"
                return ModelResponse(
                    output=[
                        ResponseFunctionToolCall(
                            id=call_id,
                            call_id=call_id,
                            name=tool_name,
                            arguments=json.dumps({"ticket_id": ticket_id}),
                            type="function_call",
                        )
                    ],
                    usage=Usage(),
                    response_id=f"resp_{self.turns}",
                )
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id=f"msg_{self.turns}",
                    role="assistant",
                    status="completed",
                    type="message",
                    content=[
                        ResponseOutputText(
                            annotations=[], text="handled", type="output_text"
                        )
                    ],
                )
            ],
            usage=Usage(),
            response_id=f"resp_{self.turns}",
        )

    def stream_response(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError("this comparison never streams")


class OpenAIAgentsStack:
    """The support flow as an agent whose tool order the model decides."""

    name = NAME

    def __init__(self, client=None, registry: PromptRegistry | None = None) -> None:
        self.client = CountingClient(client or RuleBasedSupportModel())
        self.registry = registry or PromptRegistry("prompts")
        self.model = ScriptedModel()
        self._scratch: dict[str, dict] = {}
        self.agent = self._build_agent()
        self._connect().close()

    def _build_agent(self) -> Agent:
        stack = self

        @function_tool
        def triage(ticket_id: str) -> str:
            """Classify a support ticket."""
            ticket = load_ticket(ticket_id)
            classification = classify(stack.client, ticket, registry=stack.registry)
            stack._remember(ticket_id, classification=classification.model_dump())
            return classification.model_dump_json()

        @function_tool
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

        async def approval_needed(
            ctx: Any, params: dict[str, Any], call_id: str
        ) -> bool:
            """The gate, as the SDK wants it: a predicate on the call.

            `core.is_risky` again — the rule is shared, only the hook is the
            framework's. Returning `True` unconditionally would be simpler and
            would stop all 50 tickets.
            """
            ticket_id = params.get("ticket_id", "")
            remembered = stack._recall(ticket_id).get("classification")
            if not remembered:
                return True
            return is_risky(
                load_ticket(ticket_id), Classification.model_validate(remembered)
            )

        @function_tool(needs_approval=approval_needed)
        def deliver(ticket_id: str) -> str:
            """Send the drafted reply to the customer."""
            draft = stack._recall(ticket_id)["draft"]
            receipt = send_reply(ticket_id, draft["text"], stack=NAME)
            stack._remember(ticket_id, receipt=receipt.idempotency_key)
            return receipt.idempotency_key

        return Agent(
            name="support",
            instructions=(
                "Triage the ticket, compose a reply, then deliver it. "
                "Use one tool at a time."
            ),
            model=self.model,
            tools=[triage, compose, deliver],
        )

    # --- state -----------------------------------------------------------

    def _remember(self, ticket_id: str, **fields: Any) -> None:
        self._scratch.setdefault(ticket_id, {}).update(fields)

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
                ticket_id  TEXT PRIMARY KEY,
                status     TEXT NOT NULL,
                run_state  TEXT NOT NULL,
                context    TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        return conn

    def _save(self, ticket_id: str, status: str, run_state: dict | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (ticket_id, status, run_state, context, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(ticket_id) DO UPDATE SET "
                "status = excluded.status, run_state = excluded.run_state, "
                "context = excluded.context, updated_at = excluded.updated_at",
                (
                    ticket_id,
                    status,
                    json.dumps(run_state or {}),
                    json.dumps(self._recall(ticket_id), ensure_ascii=False),
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )

    def _load(self, ticket_id: str) -> tuple[str, dict, dict] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, run_state, context FROM runs WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
        if row is None:
            return None
        return row[0], json.loads(row[1]), json.loads(row[2])

    # --- the two verbs ---------------------------------------------------

    def run(self, ticket_id: str) -> RunOutcome:
        stored = self._load(ticket_id)
        if stored is not None:
            return self._outcome(ticket_id, stored[0], stored[2], resumed=True)

        result = Runner.run_sync(self.agent, f"Handle support ticket {ticket_id}.")
        state = result.to_state()
        interruptions = state.get_interruptions()
        status = "awaiting_approval" if interruptions else "completed"
        self._save(ticket_id, status, state.to_json() if interruptions else None)
        return self._outcome(ticket_id, status, self._recall(ticket_id))

    def approve(self, ticket_id: str, decision: Decision = "approve") -> RunOutcome:
        stored = self._load(ticket_id)
        if stored is None:
            raise KeyError(f"no run for {ticket_id}; call run first")
        status, blob, context = stored
        self._scratch[ticket_id] = context
        if status != "awaiting_approval":
            return self._outcome(ticket_id, status, context, resumed=True)

        if decision == "reject":
            self._save(ticket_id, "rejected", blob)
            return self._outcome(ticket_id, "rejected", context, resumed=True)

        # `from_json` is async; `run_sync` is not. The bridge is here rather
        # than in the CLI so every caller gets the same behaviour.
        state = asyncio.run(RunState.from_json(self.agent, blob))
        for interruption in state.get_interruptions():
            state.approve(interruption)
        Runner.run_sync(self.agent, state)
        self._save(ticket_id, "completed", None)
        return self._outcome(
            ticket_id, "completed", self._recall(ticket_id), resumed=True
        )

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


def _ticket_id_from(input_items: str | list[TResponseInputItem]) -> str:
    if isinstance(input_items, str):
        return input_items.split("ticket ")[1].strip(" .")
    for item in input_items:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, str) and "ticket T-" in content:
            return content.split("ticket ")[1].strip(" .")
    raise ValueError("no ticket id in the agent input")


def _called_tools(input_items: str | list[TResponseInputItem]) -> set[str]:
    if isinstance(input_items, str):
        return set()
    called = set()
    for item in input_items:
        if not isinstance(item, dict):
            item = item.model_dump() if hasattr(item, "model_dump") else {}
        if item.get("type") == "function_call" and item.get("name"):
            called.add(item["name"])
    return called


def build() -> OpenAIAgentsStack:
    return OpenAIAgentsStack()


if __name__ == "__main__":
    raise SystemExit(run_cli(build))
