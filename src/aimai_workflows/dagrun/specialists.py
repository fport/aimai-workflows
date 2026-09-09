"""The thin layer between a DAG node and an aimai-kit agent.

Each specialist is a bounded agent with its own tools and its own context. The
isolation is the point: the statute specialist never sees the case-law
specialist's transcript, so neither can be talked into the other's conclusion,
and the synthesis node is the only place their findings meet — as
`NodeResult.output`, not as prose.

The wrapper's whole job is turning an agent run into a `NodeResult`, which
means three translations the executor depends on:

- a run that stopped on a budget rather than an answer is DEGRADED, not
  failed — a partial answer from a specialist is exactly the case `optional`
  edges exist for;
- cost and tokens come from the agent's own accounting, so the job's budget is
  the sum of real spend rather than an estimate;
- the tool results become `evidence`, because the synthesis node's citations
  have to be checkable by a human who does not trust either agent.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from aimai_kit.agent import Agent, Budgets, RunResult, ScriptedClient, Thread
from aimai_kit.provider.client import LLMClient
from aimai_kit.provider.types import Role
from aimai_kit.tools import CallContext, ToolExecutor
from aimai_kit.tools.decorator import tool
from aimai_kit.tools.registry import ToolRegistry

from .types import NodeResult, RunContext

__all__ = ["SPECIALIST_SCRIPTS", "make_specialist", "statute_tools", "caselaw_tools"]


# --- the corpus ------------------------------------------------------------
#
# Fake, small, and in this file on purpose. Retrieval quality is another repo's
# subject; what this stage needs is two specialists with genuinely different
# sources, so that removing one from the synthesis changes the answer.

_STATUTES = {
    "termination": [
        (
            "TCO-347",
            "A fixed-term contract ends at the end of its term without notice; "
            "early termination requires just cause.",
        ),
        (
            "TTK-18",
            "A merchant is expected to act as a prudent businessperson in all "
            "commercial dealings.",
        ),
    ],
    "liability": [
        (
            "TBK-115",
            "An agreement excluding liability for gross negligence or wilful "
            "misconduct in advance is void.",
        ),
    ],
    "data": [
        (
            "KVKK-12",
            "The data controller must take technical and administrative measures "
            "appropriate to the risk.",
        ),
    ],
}

_CASES = {
    "liability": [
        (
            "11-HD-2019/4412",
            "An unlimited liability clause imposed on the weaker party was held "
            "unenforceable where the cap was one-sided.",
        ),
    ],
    "termination": [
        (
            "19-HD-2021/1180",
            "A 180-day non-renewal notice was found excessive for a 12-month "
            "term and reduced to 60 days.",
        ),
    ],
}


@tool
def search_statutes(topic: str) -> str:
    """Find statutory provisions on a topic.

    Args:
        topic: one of termination, liability, data.
    """
    hits = _STATUTES.get(topic.lower().strip(), [])
    return json.dumps(
        [{"reference": reference, "text": text} for reference, text in hits]
    )


@tool
def search_cases(topic: str) -> str:
    """Find decided cases on a topic.

    Args:
        topic: one of termination, liability, data.
    """
    hits = _CASES.get(topic.lower().strip(), [])
    return json.dumps(
        [{"reference": reference, "summary": summary} for reference, summary in hits]
    )


def statute_tools() -> ToolRegistry:
    """The statute specialist sees statutes and nothing else."""
    return ToolRegistry([search_statutes])


def caselaw_tools() -> ToolRegistry:
    """The case-law specialist sees cases and nothing else.

    Two registries rather than one with an allowlist: a specialist that *could*
    reach the other's source will, the first time its own search comes up empty,
    and then the two nodes are no longer independent evidence.
    """
    return ToolRegistry([search_cases])


SPECIALIST_SCRIPTS = {
    "statute": [
        [("search_statutes", '{"topic": "liability"}')],
        [("search_statutes", '{"topic": "termination"}')],
        "TBK-115 voids an advance exclusion of liability for gross negligence, "
        "so the uncapped clause is unenforceable to that extent. TCO-347 leaves "
        "the fixed term intact.",
    ],
    "caselaw": [
        [("search_cases", '{"topic": "liability"}')],
        "11-HD-2019/4412 refused to enforce a one-sided unlimited liability "
        "clause; the facts here are close.",
    ],
}
"""Deterministic transcripts for the two specialists.

The same reasoning as every other stub in this repo: the claim being made is
about coordination — that a failed optional specialist degrades the synthesis
rather than killing it — and that claim has to be checkable without a key and
without variance.
"""


def make_specialist(
    name: str,
    registry: ToolRegistry,
    *,
    client: LLMClient | None = None,
    topic_from: str = "extract",
    max_steps: int = 6,
    cost_per_run_usd: float = 0.02,
) -> Callable:
    """Build a node body that runs one bounded specialist agent."""
    script = SPECIALIST_SCRIPTS.get(name, ["no findings"])

    async def run(context: RunContext) -> NodeResult:
        agent = Agent(
            client or ScriptedClient(script=list(script)),
            ToolExecutor(registry),
            budgets=Budgets(max_steps=max_steps, max_seconds=30),
        )
        upstream = context.output(topic_from)
        thread = Thread()
        result: RunResult = agent.run(
            thread,
            (
                f"You are the {name} specialist. Review these clauses and report "
                f"what the {name} sources say about them: "
                f"{json.dumps(upstream.get('clauses', []))}"
            ),
            ctx=CallContext(user_id="dagrun", tenant_id=context.seed),
        )
        evidence = _evidence(thread)
        return NodeResult(
            output={"finding": result.answer, "topic_count": len(evidence)},
            cost_usd=cost_per_run_usd,
            tokens=thread.spend.tokens if hasattr(thread.spend, "tokens") else 0,
            note=f"{name}: stopped on {result.stop_reason}",
            evidence=evidence,
            # A specialist that ran out of budget answered from part of its
            # sources. That is a usable answer and a degraded one, and saying so
            # here is what lets the synthesis say it too.
            degraded=not result.ok,
        )

    return run


def _evidence(thread: Thread) -> tuple[str, ...]:
    """Every reference the agent's tools actually returned.

    Read off the tool results rather than out of the final answer. An agent that
    cites a case its search never returned is the failure this field exists to
    catch, and taking the citations from the answer would launder exactly that
    mistake into the report.

    The transcript's TOOL messages are the source. `Thread.tool_results` looks
    like the obvious place and is not: aimai-kit caches only side-effecting
    calls there, so a read-only search — which is every tool a specialist has —
    never appears in it.
    """
    references: list[str] = []
    for message in thread.messages:
        if message.role is not Role.TOOL:
            continue
        try:
            envelope = json.loads(message.content)
            payload = json.loads(envelope.get("content", "[]"))
        except (ValueError, TypeError, AttributeError):
            continue
        for item in payload if isinstance(payload, list) else []:
            if isinstance(item, dict) and "reference" in item:
                references.append(str(item["reference"]))
    return tuple(dict.fromkeys(references))
