"""The business logic all four stacks share.

This file is the control in the experiment. Classification, retrieval,
drafting, the risk rule and the send are written once here, and each of the
four stack modules imports them. Whatever differs between the stacks is
therefore orchestration and nothing else — if the business logic were written
four times, every measurement would be confounded by four slightly different
implementations of the same idea.

Two rules keep it honest:

**No prompt lives in a framework's fields.** Both prompts are files in
`prompts/`, loaded through aimai-kit's registry. A framework that wants the
system prompt in a decorator argument or an agent constructor gets the rendered
text handed to it. This is the practical measure of lock-in: moving a flow
between stacks is cheap exactly to the degree that the prompts, the schemas and
the tools do not live inside the framework.

**The send is idempotent and durable.** `send_reply` writes through an
idempotency key derived from the ticket and the draft, to SQLite rather than to
memory, for the same reason as in stage 06: the chaos script kills the process,
and a fake that dies with it cannot prove anything about double sends.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

from aimai_kit.prompts import PromptRegistry, build_request
from aimai_kit.prompts.structured import generate_structured
from aimai_kit.provider.client import LLMClient
from aimai_kit.provider.types import ChatRequest, ChatResult
from pydantic import BaseModel, Field

__all__ = [
    "Article",
    "Classification",
    "CountingClient",
    "Draft",
    "REPO_ROOT",
    "SendReceipt",
    "Ticket",
    "classify",
    "draft_reply",
    "is_risky",
    "knowledge_base",
    "load_ticket",
    "load_tickets",
    "reply_key",
    "reset_outbox",
    "retrieve_articles",
    "send_reply",
    "sent_replies",
]

REPO_ROOT = Path(__file__).resolve().parents[3]

Category = Literal["billing", "technical", "account", "refund", "complaint"]
Urgency = Literal["low", "normal", "high"]
Risk = Literal["low", "medium", "high"]

REFUND_APPROVAL_MINOR = 10_000
"""Refunds at or above 100.00 in minor units need a human.

A number, in one place, in the shared logic — not a sentence in a prompt and
not a rule reimplemented in each stack. The risk decision must be identical
across all four or the comparison measures the rule, not the frameworks.
"""


class Ticket(BaseModel):
    """One support request."""

    ticket_id: str
    customer: str
    plan: Literal["free", "pro", "enterprise"]
    subject: str
    body: str


class Classification(BaseModel):
    """What the model decides about a ticket before anything is drafted."""

    category: Category = Field(description="What the customer is asking about.")
    urgency: Urgency = Field(description="How quickly this needs an answer.")
    risk: Risk = Field(
        description=(
            "How much damage a wrong automated answer would do. Use 'high' for "
            "anything touching money, cancellation, legal threats or data loss."
        )
    )
    refund_amount_minor: int = Field(
        default=0,
        ge=0,
        description=(
            "Refund the customer is asking for, in minor units (cents). 0 when "
            "no refund is requested. Never estimate; read it from the ticket."
        ),
    )
    rationale: str = Field(
        max_length=240, description="One sentence on why this risk level."
    )


class Article(BaseModel):
    """One knowledge base entry."""

    article_id: str
    title: str
    body: str
    keywords: list[str] = Field(default_factory=list)


class Draft(BaseModel):
    """The proposed answer."""

    text: str = Field(description="The reply to the customer, in full.")
    cited_article_ids: list[str] = Field(
        default_factory=list,
        description="Ids of the knowledge base articles the reply relies on.",
    )


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """What the outbox recorded."""

    idempotency_key: str
    ticket_id: str
    text: str
    sent_at: str
    deduplicated: bool = False


@dataclass
class CountingClient:
    """An `LLMClient` that counts calls.

    The `llm_calls` column of the benchmark comes from here rather than from
    each stack's own telemetry, because every framework counts something
    slightly different and the comparison has to count the same thing four
    times: requests that left for a model.
    """

    inner: LLMClient
    calls: int = 0
    by_operation: dict[str, int] = field(default_factory=dict)

    @property
    def provider(self) -> str:
        return self.inner.provider

    @property
    def model(self) -> str:
        return self.inner.model

    def complete(self, req: ChatRequest) -> ChatResult:
        self.calls += 1
        self.by_operation[req.operation] = self.by_operation.get(req.operation, 0) + 1
        return self.inner.complete(req)

    def stream(self, req: ChatRequest) -> Iterator[str]:
        self.calls += 1
        return self.inner.stream(req)


# --- fixtures ---------------------------------------------------------------


@lru_cache(maxsize=1)
def _fixture_dir() -> Path:
    return Path(os.getenv("SUPPORT_FIXTURE_DIR", str(REPO_ROOT / "fixtures")))


@lru_cache(maxsize=1)
def load_tickets() -> tuple[Ticket, ...]:
    raw = json.loads((_fixture_dir() / "tickets.json").read_text(encoding="utf-8"))
    return tuple(Ticket.model_validate(item) for item in raw)


def load_ticket(ticket_id: str) -> Ticket:
    for ticket in load_tickets():
        if ticket.ticket_id == ticket_id:
            return ticket
    raise KeyError(f"no ticket {ticket_id}; try one of T-1000…T-1049")


@lru_cache(maxsize=1)
def knowledge_base() -> tuple[Article, ...]:
    raw = json.loads(
        (_fixture_dir() / "knowledge_base.json").read_text(encoding="utf-8")
    )
    return tuple(Article.model_validate(item) for item in raw)


# --- the five steps ---------------------------------------------------------


def classify(
    client: LLMClient, ticket: Ticket, *, registry: PromptRegistry
) -> Classification:
    """Step 1. The only step whose output the risk rule reads."""
    built = build_request(
        registry,
        "classify_ticket@v1",
        f"Subject: {ticket.subject}\n\n{ticket.body}",
        doc_id=ticket.ticket_id,
        schema=Classification,
        operation="classify",
        question=f"The customer is on the {ticket.plan} plan.",
    )
    return generate_structured(client, built.req, Classification, max_attempts=2).value


def retrieve_articles(
    ticket: Ticket, classification: Classification, *, limit: int = 3
) -> list[Article]:
    """Step 2. Keyword overlap over a fixed JSON file.

    Deliberately not a retrieval system. Retrieval quality is another repo's
    subject, and a real index here would add a dependency that differs across
    the four stacks for reasons that have nothing to do with orchestration.
    """
    haystack = f"{ticket.subject} {ticket.body}".lower()
    scored: list[tuple[int, Article]] = []
    for article in knowledge_base():
        score = sum(1 for keyword in article.keywords if keyword.lower() in haystack)
        if article.article_id.startswith(classification.category[:4]):
            score += 1
        if score:
            scored.append((score, article))
    scored.sort(key=lambda pair: (-pair[0], pair[1].article_id))
    return [article for _, article in scored[:limit]]


def draft_reply(
    client: LLMClient,
    ticket: Ticket,
    classification: Classification,
    articles: list[Article],
    *,
    registry: PromptRegistry,
) -> Draft:
    """Step 3. The second and last model call in the shared logic."""
    context = (
        "\n\n".join(f"[{a.article_id}] {a.title}\n{a.body}" for a in articles)
        or "(no matching articles)"
    )
    document = (
        f"Subject: {ticket.subject}\n\n{ticket.body}\n\n"
        f"--- knowledge base ---\n{context}"
    )
    built = build_request(
        registry,
        "draft_reply@v1",
        document,
        doc_id=ticket.ticket_id,
        schema=Draft,
        operation="draft",
        question=(
            f"Category: {classification.category}. Urgency: "
            f"{classification.urgency}. Plan: {ticket.plan}."
        ),
    )
    return generate_structured(client, built.req, Draft, max_attempts=2).value


def is_risky(ticket: Ticket, classification: Classification) -> bool:
    """Step 4. The gate, as a function of state — never as a prompt instruction.

    Same argument as stage 06, and the reason it lives in the shared module:
    if each stack decided risk its own way, the benchmark's `escalated` column
    would compare four rules rather than four orchestrators.
    """
    return (
        classification.risk == "high"
        or classification.refund_amount_minor >= REFUND_APPROVAL_MINOR
        or (classification.category == "complaint" and ticket.plan == "enterprise")
    )


# --- the outbox -------------------------------------------------------------


def _outbox_path() -> Path:
    return Path(os.getenv("SUPPORT_OUTBOX_DB", ".aimai-outbox.sqlite3"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_outbox_path(), timeout=10)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_replies (
            idempotency_key TEXT PRIMARY KEY,
            ticket_id       TEXT NOT NULL,
            stack           TEXT NOT NULL,
            text            TEXT NOT NULL,
            sent_at         TEXT NOT NULL
        )
        """
    )
    return conn


def reply_key(ticket_id: str, text: str) -> str:
    """The key that makes a resend a no-op.

    Derived from the ticket and the exact text that is being sent — not from
    the stack, not from the run, not from a timestamp. Two stacks producing the
    same reply for the same ticket is the same message to the customer, and the
    benchmark's `duplicate_sends` column is only meaningful if the key says so.
    """
    digest = hashlib.sha256(f"{ticket_id}|{text}".encode()).hexdigest()
    return f"reply-{digest[:24]}"


def send_reply(ticket_id: str, text: str, *, stack: str = "unknown") -> SendReceipt:
    """Step 5. The side effect, once per (ticket, text)."""
    key = reply_key(ticket_id, text)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO sent_replies (idempotency_key, ticket_id, stack, text, "
            "sent_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(idempotency_key) DO NOTHING",
            (key, ticket_id, stack, text, now),
        )
        wrote = cursor.rowcount == 1
        row = conn.execute(
            "SELECT text, sent_at FROM sent_replies WHERE idempotency_key = ?", (key,)
        ).fetchone()
    return SendReceipt(
        idempotency_key=key,
        ticket_id=ticket_id,
        text=row[0],
        sent_at=row[1],
        deduplicated=not wrote,
    )


def sent_replies(ticket_id: str | None = None) -> list[SendReceipt]:
    query = "SELECT idempotency_key, ticket_id, text, sent_at FROM sent_replies"
    params: tuple[str, ...] = ()
    if ticket_id:
        query += " WHERE ticket_id = ?"
        params = (ticket_id,)
    with _connect() as conn:
        return [
            SendReceipt(idempotency_key=r[0], ticket_id=r[1], text=r[2], sent_at=r[3])
            for r in conn.execute(query + " ORDER BY sent_at, idempotency_key", params)
        ]


def reset_outbox() -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM sent_replies")
