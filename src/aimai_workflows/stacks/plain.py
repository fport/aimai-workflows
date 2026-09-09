"""Version 1 of 4 — plain Python, no framework.

Orchestration here is a `match` over a `step` column in SQLite. That is the
whole mechanism: `run` advances the ticket until it either sends a reply or
writes `awaiting_approval`, and `approve` picks it up from that row in whatever
process happens to be running.

What this version demonstrates is how little a durable, resumable,
human-in-the-loop flow actually needs when the flow is a straight line: a table
with a step column, a transaction per step, and an idempotent side effect. Every
framework in this comparison is buying you something on top of this — the
question the repo asks is what, and at what price.

What it costs is visible too. Every state transition is written by hand;
there is no history to inspect, no way to ask "what did step 2 return" after
the fact, and adding a branch means editing the `match` in two places. At four
steps that is cheap. At fourteen it is where the bugs live.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from aimai_kit.prompts import PromptRegistry

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

__all__ = ["PlainStack", "build"]

NAME = "plain"


class PlainStack:
    """The reference implementation: a state machine you can read in one sitting."""

    name = NAME

    def __init__(self, client=None, registry: PromptRegistry | None = None) -> None:
        self.client = CountingClient(client or RuleBasedSupportModel())
        self.registry = registry or PromptRegistry("prompts")
        self._connect().close()

    # --- state -----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(state_db(NAME), timeout=10)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                ticket_id  TEXT PRIMARY KEY,
                step       TEXT NOT NULL,
                payload    TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        return conn

    def _write(self, ticket_id: str, step: str, payload: dict) -> None:
        """One row per ticket, replaced at every step.

        History is not kept. That is the honest version of this design — a
        checkpointer gives you `get_state_history` for free and this does not,
        and pretending otherwise by adding an append-only table would be
        rebuilding the framework this version exists to be compared against.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (ticket_id, step, payload, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(ticket_id) DO UPDATE SET "
                "step = excluded.step, payload = excluded.payload, "
                "updated_at = excluded.updated_at",
                (
                    ticket_id,
                    step,
                    json.dumps(payload, ensure_ascii=False),
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )

    def _read(self, ticket_id: str) -> tuple[str, dict] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT step, payload FROM runs WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    # --- the two verbs ---------------------------------------------------

    def run(self, ticket_id: str) -> RunOutcome:
        existing = self._read(ticket_id)
        if existing and existing[0] in ("awaiting_approval", "sent", "rejected"):
            # Re-running a ticket that is already parked or finished is a retry,
            # not a new run. Same reasoning as stage 06's derived thread id.
            return self._outcome_from_state(ticket_id, *existing, resumed=True)

        ticket = load_ticket(ticket_id)

        # Resuming mid-run is written out by hand, one branch per step. This is
        # the code a checkpointer would have written for us, and the reason the
        # `orchestration lines` column in the benchmark is not the whole story:
        # these eleven lines are also the ones that go stale when a step is
        # added and nobody updates the resume path.
        step, payload = existing if existing else ("", {})
        if step == "drafted":
            return self._after_draft(ticket, payload, resumed=True)
        if step == "classified":
            classification = Classification.model_validate(payload)
        else:
            classification = classify(self.client, ticket, registry=self.registry)
            self._write(ticket_id, "classified", classification.model_dump())

        articles = retrieve_articles(ticket, classification)
        draft = draft_reply(
            self.client, ticket, classification, articles, registry=self.registry
        )
        payload = {
            "classification": classification.model_dump(),
            "draft": draft.model_dump(),
        }
        self._write(ticket_id, "drafted", payload)
        return self._after_draft(ticket, payload, resumed=bool(step))

    def _after_draft(self, ticket, payload: dict, *, resumed: bool) -> RunOutcome:
        ticket_id = ticket.ticket_id
        classification = Classification.model_validate(payload["classification"])
        draft = Draft.model_validate(payload["draft"])

        if is_risky(ticket, classification):
            self._write(ticket_id, "awaiting_approval", payload)
            return RunOutcome(
                ticket_id=ticket_id,
                stack=NAME,
                status="awaiting_approval",
                risk=classification.risk,
                draft=draft.text,
                llm_calls=self.client.calls,
                resumed=resumed,
            )

        return self._send(ticket_id, payload, resumed=resumed)

    def approve(self, ticket_id: str, decision: Decision = "approve") -> RunOutcome:
        state = self._read(ticket_id)
        if state is None:
            raise KeyError(f"no run for {ticket_id}; call run first")
        step, payload = state
        if step == "sent":
            return self._outcome_from_state(ticket_id, step, payload, resumed=True)
        if step != "awaiting_approval":
            raise ValueError(f"{ticket_id} is at step {step!r}, not awaiting approval")

        if decision == "reject":
            self._write(ticket_id, "rejected", payload)
            return RunOutcome(
                ticket_id=ticket_id,
                stack=NAME,
                status="rejected",
                risk=payload["classification"]["risk"],
                llm_calls=self.client.calls,
                resumed=True,
            )
        return self._send(ticket_id, payload, resumed=True)

    # --- helpers ---------------------------------------------------------

    def _send(
        self, ticket_id: str, payload: dict, *, resumed: bool = False
    ) -> RunOutcome:
        draft = Draft.model_validate(payload["draft"])
        receipt = send_reply(ticket_id, draft.text, stack=NAME)
        self._write(ticket_id, "sent", {**payload, "receipt": receipt.idempotency_key})
        return RunOutcome(
            ticket_id=ticket_id,
            stack=NAME,
            status="completed",
            risk=payload["classification"]["risk"],
            receipt=receipt.idempotency_key,
            draft=draft.text,
            llm_calls=self.client.calls,
            resumed=resumed,
            detail={"deduplicated": receipt.deduplicated},
        )

    def _outcome_from_state(
        self, ticket_id: str, step: str, payload: dict, *, resumed: bool = False
    ) -> RunOutcome:
        status = {
            "awaiting_approval": "awaiting_approval",
            "sent": "completed",
            "rejected": "rejected",
        }.get(step, "unknown")
        classification = payload.get("classification", {})
        return RunOutcome(
            ticket_id=ticket_id,
            stack=NAME,
            status=status,  # type: ignore[arg-type]
            risk=Classification.model_validate(classification).risk
            if classification
            else "unknown",
            receipt=payload.get("receipt"),
            draft=(payload.get("draft") or {}).get("text"),
            llm_calls=self.client.calls,
            resumed=resumed,
        )


def build() -> PlainStack:
    return PlainStack()


if __name__ == "__main__":
    raise SystemExit(run_cli(build))
