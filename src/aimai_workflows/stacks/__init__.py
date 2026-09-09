"""Stage 07 — one support flow, four orchestrators, one measurement set.

The business logic lives once in `core.py`; each stack module is orchestration
only. That is what makes the comparison a comparison rather than four programs
that happen to do similar things.

    from aimai_workflows.stacks.plain import PlainStack

    stack = PlainStack()
    stack.run("T-1000")            # -> awaiting_approval, nothing sent
    stack.approve("T-1000")        # -> completed, exactly one reply

    python -m aimai_workflows.stacks.langgraph_stack run T-1000
    uv run stack-bench             # results/bench.md
    python -m aimai_workflows.stacks.chaos   # results/chaos.md

The four modules are deliberately NOT imported here. Two of them pull in a
third-party framework, and a reader who wants only the plain version should not
have to install both. `bench.stack_factories()` imports them on demand.
"""

from .contract import Decision, RunOutcome, SupportStack
from .core import (
    Article,
    Classification,
    CountingClient,
    Draft,
    Ticket,
    classify,
    draft_reply,
    is_risky,
    knowledge_base,
    load_ticket,
    load_tickets,
    reply_key,
    reset_outbox,
    retrieve_articles,
    send_reply,
    sent_replies,
)
from .stub_support import RuleBasedSupportModel

__all__ = [
    "Article",
    "Classification",
    "CountingClient",
    "Decision",
    "Draft",
    "RuleBasedSupportModel",
    "RunOutcome",
    "SupportStack",
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
