"""A real flow: extract clauses, review them, ask two specialists, synthesise,
deliver — and undo the delivery if anything downstream fails.

    extract ──> scope_gate ──> statute  ─┐
        │           │                    ├──> synthesis ──> deliver ──> archive
        │           └───────> caselaw ···┘                     ╎
        └─────> clause_review ───────────┘              retract ╌╌ compensates

Every node kind appears once, because each answers a different question the
others cannot:

- `extract` is a TASK: one unit of work, no branching.
- `scope_gate` is a GATE: if the document is out of scope, the specialists are
  SKIPPED, not FAILED. Nothing went wrong; the work was not wanted.
- `clause_review` is a FANOUT: the number of clauses is not known until
  `extract` has run, so the work cannot be laid out when the graph is built.
- `synthesis` is a JOIN, and it is where the `optional` edge pays off. It
  REQUIRES the statute specialist and only OPTIONALLY wants case law, so a
  case-law outage produces a degraded opinion rather than no opinion — and the
  opinion says which source it is missing.
- `deliver` has a side effect and `retract` COMPENSATEs it. `archive` exists so
  that there is something *after* the side effect that can fail — which is the
  case a saga is for: the opinion went out, the filing that had to accompany it
  did not, and the recipient has to be told.

The `caselaw` edge being optional is the single most consequential line in the
file. It says, in code, that a due-diligence opinion grounded in statute alone
is worth delivering with a caveat, and that one grounded in case law alone is
not. That is a legal judgement, not an engineering one, and it belongs in the
graph where a reviewer can see and argue with it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence

from ...stacks.core import send_reply, sent_replies
from ..fingerprint import version_from_files
from ..specialists import caselaw_tools, make_specialist, statute_tools
from ..types import Node, NodeKind, NodeResult, RunContext

__all__ = ["DOCUMENT_URI", "build_flow", "clause_topics"]

DOCUMENT_URI = "high_risk.txt"

_CLAUSE_RE = re.compile(r"^(\d+\.\d+)\s+(.+)$", re.MULTILINE)
_TOPICS = {
    "liability": ("liability", "indemnify", "indemnity"),
    "termination": ("terminate", "renew", "notice", "exit fee"),
    "data": ("data", "personal data", "delete", "licence", "license"),
}


def clause_topics(text: str) -> str:
    """Which specialist source a clause belongs to. Unmatched clauses are
    'liability', the most expensive thing to be wrong about."""
    lowered = text.lower()
    for topic, keywords in _TOPICS.items():
        if any(keyword in lowered for keyword in keywords):
            return topic
    return "liability"


async def _extract(context: RunContext) -> NodeResult:
    """Pull numbered clauses out of the document.

    Reuses stage 06's document loader, so the URI confinement and the hash come
    for free rather than being reimplemented here.
    """
    from ...contract.documents import load_document

    document = load_document(DOCUMENT_URI)
    clauses = [
        {
            "clause_id": clause_id,
            "topic": clause_topics(body),
            "text": " ".join(body.split())[:400],
        }
        for clause_id, body in _CLAUSE_RE.findall(document.text)
    ]
    risky = [c for c in clauses if c["topic"] == "liability"]
    return NodeResult(
        output={
            "clauses": clauses,
            # The FANOUT below expands this key. Named `items` by convention so
            # the executor does not have to know anything about this flow.
            "items": [c["clause_id"] for c in risky],
            "document_sha256": document.sha256,
        },
        cost_usd=0.004,
        tokens=len(document.text) // 4,
        note=f"{len(clauses)} clause(s), {len(risky)} on liability",
        evidence=tuple(c["clause_id"] for c in clauses),
    )


async def _scope_gate(context: RunContext) -> NodeResult:
    """Is this document worth a full review?

    Returns `passed`, which the executor reads. A gate that returned an
    exception for "no" would make an ordinary decision look like a failure in
    every report and every alert.
    """
    clauses = context.output("extract").get("clauses", [])
    passed = len(clauses) >= 3
    return NodeResult(
        output={"passed": passed, "clause_count": len(clauses)},
        cost_usd=0.0,
        note="in scope" if passed else "fewer than three clauses; out of scope",
    )


async def _clause_review(context: RunContext) -> NodeResult:
    """One branch per liability clause, run concurrently by the executor."""
    clause_id = str(context.item)
    clauses = {c["clause_id"]: c for c in context.output("extract").get("clauses", [])}
    clause = clauses.get(clause_id, {})
    text = clause.get("text", "")
    severe = any(word in text.lower() for word in ("unlimited", "without limitation"))
    return NodeResult(
        output={"clause_id": clause_id, "severe": severe, "topic": clause.get("topic")},
        cost_usd=0.002,
        tokens=len(text) // 4,
        note=f"{clause_id}: {'severe' if severe else 'ordinary'}",
        evidence=(clause_id,),
    )


async def _synthesis(context: RunContext) -> NodeResult:
    """Merge the specialists, and say out loud what is missing.

    The missing-input list is passed into the write-up rather than logged.
    A synthesis that is not told what it lacks writes with the confidence of one
    that has everything, and that confident paragraph is what a reader acts on.
    """
    review = context.output("clause_review")
    statute = context.output("statute").get("finding", "")
    caselaw = context.output("caselaw").get("finding", "")
    severe = [r for r in review.get("results", []) if r.get("severe")]

    missing = list(context.missing_inputs) + list(context.degraded_inputs)
    caveat = (
        ""
        if not missing
        else (
            "\n\nINCOMPLETE: this opinion was written without "
            + ", ".join(missing)
            + ". Treat the conclusion as provisional in that respect."
        )
    )
    opinion = (
        f"{len(severe)} clause(s) create severe exposure. "
        f"Statute: {statute or '(unavailable)'} "
        f"Case law: {caselaw or '(unavailable)'}"
    ) + caveat

    return NodeResult(
        output={
            "opinion": opinion,
            "severe_clauses": [r["clause_id"] for r in severe],
            "missing_sources": missing,
        },
        cost_usd=0.01,
        tokens=len(opinion) // 4,
        note=f"synthesis over {len(review.get('results', []))} clause review(s)",
        evidence=tuple(r["clause_id"] for r in severe),
        degraded=bool(missing),
    )


async def _deliver(context: RunContext) -> NodeResult:
    """The side effect: send the opinion.

    Keyed by the opinion text, so a retried node delivers once. The runner's
    `max_attempts` makes retries ordinary, and an un-keyed send would make them
    dangerous.
    """
    opinion = context.output("synthesis").get("opinion", "")
    receipt = send_reply(context.seed, opinion, stack="dagrun")
    return NodeResult(
        output={
            "receipt": receipt.idempotency_key,
            "deduplicated": receipt.deduplicated,
        },
        cost_usd=0.0,
        note="opinion delivered" + (" (already sent)" if receipt.deduplicated else ""),
    )


async def _archive(context: RunContext) -> NodeResult:
    """File the delivered opinion in the matter record.

    A cheap node that only exists downstream of a side effect. Without a node
    here, `deliver` would be the last thing in the graph and the compensation
    could only ever fire for `deliver`'s own failure — half the saga.
    """
    receipt = context.output("deliver").get("receipt", "")
    return NodeResult(
        output={"archived": receipt},
        cost_usd=0.0,
        note=f"archived under {receipt[:16]}",
    )


async def _retract(context: RunContext) -> NodeResult:
    """Undo the delivery.

    A retraction is a new message, not a deletion — the recipient already read
    the first one. It is keyed like any other send, so running the compensation
    twice retracts once.
    """
    delivered = context.output("deliver")
    receipt = send_reply(
        context.seed,
        f"RETRACTED: the opinion sent as {delivered.get('receipt', 'unknown')} "
        "was withdrawn because the job did not complete.",
        stack="dagrun",
    )
    return NodeResult(
        output={"retraction": receipt.idempotency_key},
        note="retraction sent" if not receipt.deduplicated else "already retracted",
    )


def build_flow(
    *,
    caselaw_fails: bool = False,
    deliver_fails: bool = False,
    archive_fails: bool = False,
    retract_fails: bool = False,
) -> tuple[Node, ...]:
    """The graph.

    The three failure switches are how the tests reach states that are otherwise
    only reachable by breaking something for real. They belong in the builder
    rather than in a test fixture so that the CLI can demonstrate them too —
    `--fail caselaw` is the fastest way to see a degraded report.
    """
    prompts = version_from_files("prompts/assess_risk@v1.md")

    async def failing(_: RunContext) -> NodeResult:
        raise RuntimeError("the source is unavailable")

    async def failing_deliver(context: RunContext) -> NodeResult:
        await _deliver(context)
        raise RuntimeError("the delivery channel rejected the opinion after sending")

    return (
        Node("extract", NodeKind.TASK, _extract, version=prompts, max_attempts=2),
        Node("scope_gate", NodeKind.GATE, _scope_gate, requires=("extract",)),
        Node(
            "clause_review",
            NodeKind.FANOUT,
            _clause_review,
            requires=("extract", "scope_gate"),
            expands="extract",
        ),
        Node(
            "statute",
            NodeKind.TASK,
            make_specialist("statute", statute_tools()),
            version=prompts,
            requires=("extract", "scope_gate"),
            max_attempts=2,
        ),
        Node(
            "caselaw",
            NodeKind.TASK,
            failing if caselaw_fails else make_specialist("caselaw", caselaw_tools()),
            version=prompts,
            requires=("extract", "scope_gate"),
            max_attempts=2,
        ),
        Node(
            "synthesis",
            NodeKind.JOIN,
            _synthesis,
            requires=("clause_review", "statute"),
            optional=("caselaw",),
        ),
        Node(
            "deliver",
            NodeKind.TASK,
            failing_deliver if deliver_fails else _deliver,
            requires=("synthesis",),
            side_effect=True,
        ),
        Node(
            "archive",
            NodeKind.TASK,
            failing if archive_fails else _archive,
            requires=("deliver",),
        ),
        Node(
            "retract",
            NodeKind.COMPENSATE,
            failing if retract_fails else _retract,
            compensates="deliver",
        ),
    )


def delivered_for(seed: str) -> Sequence:
    """Everything this job actually sent, for tests and for the report."""
    return sent_replies(seed)


def flow_digest(nodes: Sequence[Node]) -> str:
    """A hash of the graph's shape, printed by `--dry-run`.

    Cheap way to answer "is the graph the same one that produced yesterday's
    report" without diffing two reports.
    """
    shape = [
        {
            "id": node.id,
            "kind": node.kind.value,
            "requires": list(node.requires),
            "optional": list(node.optional),
        }
        for node in nodes
    ]
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:12]
