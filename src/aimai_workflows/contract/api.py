"""The HTTP service around the flow.

Two endpoints do the work — one starts a review, one answers the gate — plus a
read endpoint the approval screen polls and the screen itself. Everything
interesting is in how they treat the graph:

**The service is stateless; the graph is not.** No dictionary of in-flight
reviews, no background task holding a paused run, no queue. A review that is
waiting for a human exists only as rows in Postgres, and the process can be
restarted, scaled to four replicas or redeployed mid-approval without any of
them noticing. That is the whole reason for a checkpointer, and keeping a
single in-memory handle would quietly give it up.

**The thread id is derived, not invented.** `POST /reviews` for a contract that
is already under review resumes that review instead of starting a second one.
Retried webhooks and double-clicked buttons are the normal case, not the edge
case.

**The decision endpoint is the only writer of the gate.** It refuses a decision
for a review that is not paused (409) rather than silently starting a new run,
because "approve" arriving twice must not mean two CRM notes — and the second
one has no interrupt to resume.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from aimai_kit.provider.client import LLMClient
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from langgraph.types import Command
from pydantic import BaseModel, Field

from .checkpointer import postgres_checkpointer, sqlite_checkpointer
from .graph import compile_graph, thread_config
from .state import Finding
from .stub_reviewer import RuleBasedReviewer

__all__ = ["DecisionRequest", "ReviewRequest", "ReviewStatus", "create_app"]

_WEB_DIR = Path(__file__).resolve().parents[3] / "web"


class ReviewRequest(BaseModel):
    """Start a review."""

    contract_no: str = Field(min_length=1, max_length=64)
    document_uri: str = Field(min_length=1)


class DecisionRequest(BaseModel):
    """Answer the gate.

    `findings` is only read for `edit`. Sending corrections with `approve` is a
    request the reviewer did not make explicit, and guessing which they meant
    is how an approval quietly rewrites a report.
    """

    decision: Literal["approve", "edit", "reject"]
    note: str = ""
    findings: list[Finding] = Field(default_factory=list)


class ReviewStatus(BaseModel):
    """What a review looks like from outside.

    `status` is derived from the graph's own state on every read rather than
    stored: a status column and a checkpoint that disagree is a class of bug
    this service cannot have if it never writes one.
    """

    contract_no: str
    status: Literal["awaiting_approval", "completed", "not_found"]
    overall_risk: str | None = None
    summary: str | None = None
    text_sha256: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    decision: str | None = None
    action_receipt: str | None = None
    question: str | None = None
    next_nodes: list[str] = Field(default_factory=list)


def _status_from(state: Any, contract_no: str) -> ReviewStatus:
    values = state.values or {}
    interrupts = [i for task in state.tasks for i in task.interrupts]
    question = interrupts[0].value.get("question") if interrupts else None
    return ReviewStatus(
        contract_no=contract_no,
        status="awaiting_approval" if state.next else "completed",
        overall_risk=values.get("overall_risk"),
        summary=values.get("summary"),
        text_sha256=values.get("text_sha256"),
        findings=values.get("findings", []),
        decision=values.get("decision"),
        action_receipt=values.get("action_receipt"),
        question=question,
        next_nodes=list(state.next),
    )


def create_app(
    client: LLMClient | None = None,
    checkpointer: Any = None,
    *,
    durability: str = "sync",
) -> FastAPI:
    """Build the service.

    Both dependencies are injectable so the tests drive the real endpoints with
    an in-memory saver and the rule-based reviewer. A service whose tests can
    only reach it through a running Postgres gets tested once and then never
    again.
    """
    stack = ExitStack()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Open the checkpointer once, for the life of the process."""
        saver = checkpointer
        if saver is None:
            backend = os.getenv("CONTRACT_CHECKPOINTER", "postgres")
            if backend == "sqlite":
                saver = stack.enter_context(
                    sqlite_checkpointer(
                        os.getenv("CONTRACT_DB_PATH", "contract.sqlite3")
                    )
                )
            else:
                saver = stack.enter_context(postgres_checkpointer())
        app.state.graph = compile_graph(client or RuleBasedReviewer(), saver)
        try:
            yield
        finally:
            stack.close()

    app = FastAPI(
        title="contract-graph",
        summary="A durable contract review flow with a human approval gate",
        lifespan=lifespan,
    )

    @app.post("/reviews", response_model=ReviewStatus)
    def start_review(request: ReviewRequest) -> ReviewStatus:
        graph = app.state.graph
        config = thread_config(request.contract_no)

        existing = graph.get_state(config)
        if existing.next:
            # Already paused for a human: report where it is instead of
            # starting a rival run over the same contract.
            return _status_from(existing, request.contract_no)

        graph.invoke(
            {
                "contract_no": request.contract_no,
                "document_uri": request.document_uri,
            },
            config,
            durability=durability,
        )
        return _status_from(graph.get_state(config), request.contract_no)

    @app.post("/reviews/{contract_no}/decision", response_model=ReviewStatus)
    def decide(contract_no: str, request: DecisionRequest) -> ReviewStatus:
        graph = app.state.graph
        config = thread_config(contract_no)
        state = graph.get_state(config)

        if not state.created_at:
            raise HTTPException(404, f"no review for contract {contract_no}")
        if not state.next:
            raise HTTPException(
                409,
                f"review for {contract_no} is already finished "
                f"(decision: {(state.values or {}).get('decision', 'none')})",
            )

        resume: dict[str, Any] = {
            "decision": request.decision,
            "note": request.note,
        }
        if request.decision == "edit":
            resume["findings"] = [f.model_dump() for f in request.findings]

        graph.invoke(Command(resume=resume), config, durability=durability)
        return _status_from(graph.get_state(config), contract_no)

    @app.get("/reviews/{contract_no}", response_model=ReviewStatus)
    def read_review(contract_no: str) -> ReviewStatus:
        state = app.state.graph.get_state(thread_config(contract_no))
        if not state.created_at:
            raise HTTPException(404, f"no review for contract {contract_no}")
        return _status_from(state, contract_no)

    @app.get("/", response_class=HTMLResponse)
    def review_screen() -> str:
        return (_WEB_DIR / "review.html").read_text(encoding="utf-8")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
"""The ASGI application. `uvicorn aimai_workflows.contract.api:app`."""
