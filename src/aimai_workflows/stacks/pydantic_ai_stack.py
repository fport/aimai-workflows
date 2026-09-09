"""Version 3 of 4 — pydantic-ai.

The type-centric version. The agent is typed on its dependencies and its
output, tools are plain functions with validated signatures, and the
human-in-the-loop mechanism is a *tool-level* one: `send_reply` raises
`ApprovalRequired`, the run ends with `DeferredToolRequests` instead of an
answer, and a later run continues with `DeferredToolResults`.

Two things follow from that design and they are the finding of this version:

**The state is yours to carry.** pydantic-ai has no checkpointer. What has to
survive between the two verbs is the message history, and it is this file's job
to serialize it (`ModelMessagesTypeAdapter`) and put it somewhere durable. That
is more code than LangGraph and, in exchange, no opinion about where state
lives — the history is JSON in a column you chose, readable without the
framework.

**The gate moves into the tool.** In `plain.py` and `langgraph_stack.py` the
risk check is a branch before the side effect. Here it is a condition inside
the tool that performs it, because that is where the framework's approval
mechanism lives. The rule itself is still `core.is_risky` — the same function,
reading the same state — but the *place* it is enforced is chosen by the
framework, not by us. Worth knowing before adopting it: an approval you want to
happen two steps before the side effect has to be modelled as a separate tool.

The model here decides the sequence of tool calls, which is the other
difference the benchmark measures: this version spends four model turns where
the two graph versions spend none, on top of the two calls the shared logic
makes.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from aimai_kit.prompts import PromptRegistry
from pydantic_ai import (
    Agent,
    ApprovalRequired,
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

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

__all__ = ["PydanticAIStack", "build"]

NAME = "pydantic-ai"


@dataclass
class SupportDeps:
    """Everything the tools need that is not a tool argument."""

    client: CountingClient
    registry: PromptRegistry
    classification: dict | None = None
    draft: dict | None = None


class PydanticAIStack:
    """The support flow as a typed agent with a deferred approval tool."""

    name = NAME

    def __init__(self, client=None, registry: PromptRegistry | None = None) -> None:
        self.client = CountingClient(client or RuleBasedSupportModel())
        self.registry = registry or PromptRegistry("prompts")
        self.model_turns = 0
        self.agent = self._build_agent()
        self._connect().close()

    # --- the agent -------------------------------------------------------

    def _build_agent(self) -> Agent[SupportDeps, object]:
        def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            """A scripted model, so the comparison measures orchestration.

            A real model would make every column in the benchmark noisy for
            reasons unrelated to the framework. This function plays the part a
            model plays — it decides which tool to call next — deterministically,
            by looking at which tools have already returned.
            """
            self.model_turns += 1
            called = {
                part.tool_name
                for message in messages
                for part in getattr(message, "parts", [])
                if isinstance(part, ToolCallPart)
            }
            ticket_id = _ticket_id_from(messages)
            if "triage" not in called:
                return ModelResponse(
                    parts=[ToolCallPart("triage", {"ticket_id": ticket_id})]
                )
            if "compose" not in called:
                return ModelResponse(
                    parts=[ToolCallPart("compose", {"ticket_id": ticket_id})]
                )
            if "deliver" not in called:
                return ModelResponse(
                    parts=[ToolCallPart("deliver", {"ticket_id": ticket_id})]
                )
            return ModelResponse(parts=[TextPart("handled")])

        agent = Agent(
            FunctionModel(script),
            deps_type=SupportDeps,
            output_type=[str, DeferredToolRequests],
        )

        @agent.tool
        def triage(ctx: RunContext[SupportDeps], ticket_id: str) -> str:
            ticket = load_ticket(ticket_id)
            classification = classify(
                ctx.deps.client, ticket, registry=ctx.deps.registry
            )
            ctx.deps.classification = classification.model_dump()
            return classification.model_dump_json()

        @agent.tool
        def compose(ctx: RunContext[SupportDeps], ticket_id: str) -> str:
            ticket = load_ticket(ticket_id)
            classification = Classification.model_validate(ctx.deps.classification)
            articles = retrieve_articles(ticket, classification)
            draft = draft_reply(
                ctx.deps.client,
                ticket,
                classification,
                articles,
                registry=ctx.deps.registry,
            )
            ctx.deps.draft = draft.model_dump()
            return draft.model_dump_json()

        @agent.tool
        def deliver(ctx: RunContext[SupportDeps], ticket_id: str) -> str:
            """The side effect, gated by the framework's approval mechanism.

            `ctx.tool_call_approved` is False on the first pass and True when the
            call arrives through `DeferredToolResults`. The risk rule is still
            `core.is_risky`; only the place it is checked belongs to the
            framework.
            """
            ticket = load_ticket(ticket_id)
            classification = Classification.model_validate(ctx.deps.classification)
            if is_risky(ticket, classification) and not ctx.tool_call_approved:
                raise ApprovalRequired
            receipt = send_reply(ticket_id, ctx.deps.draft["text"], stack=NAME)
            return receipt.idempotency_key

        return agent

    # --- state -----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(state_db(NAME), timeout=10)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                ticket_id  TEXT PRIMARY KEY,
                status     TEXT NOT NULL,
                history    TEXT NOT NULL,
                context    TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        return conn

    def _save(
        self,
        ticket_id: str,
        status: str,
        messages: list[ModelMessage],
        deps: SupportDeps,
    ) -> None:
        """Persist the message history — the framework's entire memory of the run.

        Stored as JSON rather than pickled, so a paused run stays readable (and
        migratable) without importing pydantic-ai.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (ticket_id, status, history, context, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(ticket_id) DO UPDATE SET "
                "status = excluded.status, history = excluded.history, "
                "context = excluded.context, updated_at = excluded.updated_at",
                (
                    ticket_id,
                    status,
                    ModelMessagesTypeAdapter.dump_json(messages).decode(),
                    json.dumps(
                        {
                            "classification": deps.classification,
                            "draft": deps.draft,
                        },
                        ensure_ascii=False,
                    ),
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )

    def _load(self, ticket_id: str) -> tuple[str, list[ModelMessage], dict] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, history, context FROM runs WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
        if row is None:
            return None
        return (
            row[0],
            ModelMessagesTypeAdapter.validate_json(row[1]),
            json.loads(row[2]),
        )

    # --- the two verbs ---------------------------------------------------

    def run(self, ticket_id: str) -> RunOutcome:
        stored = self._load(ticket_id)
        if stored is not None:
            return self._outcome(ticket_id, *stored, resumed=True)

        deps = SupportDeps(self.client, self.registry)
        result = self.agent.run_sync(f"Handle support ticket {ticket_id}.", deps=deps)
        status = (
            "awaiting_approval"
            if isinstance(result.output, DeferredToolRequests)
            else "completed"
        )
        self._save(ticket_id, status, result.all_messages(), deps)
        return self._outcome(
            ticket_id,
            status,
            result.all_messages(),
            {"classification": deps.classification, "draft": deps.draft},
        )

    def approve(self, ticket_id: str, decision: Decision = "approve") -> RunOutcome:
        stored = self._load(ticket_id)
        if stored is None:
            raise KeyError(f"no run for {ticket_id}; call run first")
        status, history, context = stored
        if status != "awaiting_approval":
            return self._outcome(ticket_id, status, history, context, resumed=True)

        pending = _pending_approvals(history)
        if decision == "reject":
            self._save(
                ticket_id,
                "rejected",
                history,
                SupportDeps(self.client, self.registry, **context),
            )
            return self._outcome(ticket_id, "rejected", history, context, resumed=True)

        deps = SupportDeps(self.client, self.registry, **context)
        results = DeferredToolResults(approvals=dict.fromkeys(pending, True))
        result = self.agent.run_sync(
            message_history=history, deferred_tool_results=results, deps=deps
        )
        self._save(ticket_id, "completed", result.all_messages(), deps)
        return self._outcome(
            ticket_id,
            "completed",
            result.all_messages(),
            {"classification": deps.classification, "draft": deps.draft},
            resumed=True,
        )

    def _outcome(
        self,
        ticket_id: str,
        status: str,
        history: list[ModelMessage],
        context: dict,
        *,
        resumed: bool = False,
    ) -> RunOutcome:
        classification = context.get("classification") or {}
        draft = context.get("draft") or {}
        return RunOutcome(
            ticket_id=ticket_id,
            stack=NAME,
            status=status,  # type: ignore[arg-type]
            risk=classification.get("risk", "unknown"),
            receipt=_receipt_from(history),
            draft=draft.get("text"),
            llm_calls=self.client.calls + self.model_turns,
            resumed=resumed,
            detail={
                "model_turns": self.model_turns,
                "history_messages": len(history),
            },
        )


def _ticket_id_from(messages: list[ModelMessage]) -> str:
    """Recover the ticket id from the first user message.

    The scripted model needs it to build tool calls, and on resume the prompt is
    not passed again — the history is all there is.
    """
    for message in messages:
        for part in getattr(message, "parts", []):
            content = getattr(part, "content", "")
            if isinstance(content, str) and "ticket T-" in content:
                return content.split("ticket ")[1].strip(" .")
    raise ValueError("no ticket id in the message history")


def _pending_approvals(messages: list[ModelMessage]) -> list[str]:
    """Tool call ids waiting on a human, newest run last."""
    return [
        part.tool_call_id
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolCallPart) and part.tool_name == "deliver"
    ]


def _receipt_from(messages: list[ModelMessage]) -> str | None:
    for message in reversed(messages):
        for part in getattr(message, "parts", []):
            content = getattr(part, "content", None)
            if isinstance(content, str) and content.startswith("reply-"):
                return content
    return None


def build() -> PydanticAIStack:
    return PydanticAIStack()


if __name__ == "__main__":
    raise SystemExit(run_cli(build))
