"""The shared business logic, tested once so the stacks can be compared.

If these tests were per-stack, a difference in the benchmark could always be a
difference in someone's copy of the risk rule.
"""

from __future__ import annotations

from aimai_kit.prompts import PromptRegistry

from aimai_workflows.stacks.core import (
    REFUND_APPROVAL_MINOR,
    Classification,
    CountingClient,
    classify,
    draft_reply,
    is_risky,
    knowledge_base,
    load_ticket,
    load_tickets,
    retrieve_articles,
)
from aimai_workflows.stacks.stub_support import RuleBasedSupportModel


def classification(**overrides) -> Classification:
    base = {
        "category": "billing",
        "urgency": "normal",
        "risk": "low",
        "refund_amount_minor": 0,
        "rationale": "r",
    }
    return Classification.model_validate(base | overrides)


def test_the_fixture_set_has_the_shape_the_benchmark_assumes() -> None:
    """Fifteen tickets over the gate, and the rest under it.

    The benchmark's `escalated` column is only meaningful against a known
    fixture set; this pins it so a regenerated fixture file that quietly
    changes the mix fails here.
    """
    tickets = load_tickets()
    model = CountingClient(RuleBasedSupportModel())
    registry = PromptRegistry("prompts")

    risky = [t for t in tickets if is_risky(t, classify(model, t, registry=registry))]

    assert len(tickets) == 50
    assert len(risky) == 15


def test_high_risk_escalates() -> None:
    assert is_risky(load_ticket("T-1030"), classification(risk="high"))


def test_a_refund_at_the_threshold_escalates() -> None:
    """At, not above: a rule written with `>` lets the exact amount through."""
    ticket = load_ticket("T-1030")

    assert is_risky(ticket, classification(refund_amount_minor=REFUND_APPROVAL_MINOR))
    assert not is_risky(
        ticket, classification(refund_amount_minor=REFUND_APPROVAL_MINOR - 1)
    )


def test_an_enterprise_complaint_escalates_even_when_calm() -> None:
    enterprise = next(t for t in load_tickets() if t.plan == "enterprise")

    assert is_risky(enterprise, classification(category="complaint", risk="low"))


def test_retrieval_returns_articles_that_share_words_with_the_ticket() -> None:
    ticket = load_ticket("T-1030")
    found = retrieve_articles(
        ticket,
        classify(
            CountingClient(RuleBasedSupportModel()),
            ticket,
            registry=PromptRegistry("prompts"),
        ),
    )

    assert found
    assert all(a.article_id in {x.article_id for x in knowledge_base()} for a in found)


def test_a_draft_cites_the_articles_it_used() -> None:
    registry = PromptRegistry("prompts")
    model = CountingClient(RuleBasedSupportModel())
    ticket = load_ticket("T-1030")
    found = classify(model, ticket, registry=registry)
    articles = retrieve_articles(ticket, found)

    draft = draft_reply(model, ticket, found, articles, registry=registry)

    assert draft.text
    assert set(draft.cited_article_ids) <= {a.article_id for a in articles}


def test_the_counting_client_counts_by_operation() -> None:
    """The benchmark's `llm_calls` column comes from here."""
    registry = PromptRegistry("prompts")
    model = CountingClient(RuleBasedSupportModel())
    ticket = load_ticket("T-1030")
    found = classify(model, ticket, registry=registry)
    draft_reply(
        model, ticket, found, retrieve_articles(ticket, found), registry=registry
    )

    assert model.calls == 2
    assert model.by_operation == {"classify": 1, "draft": 1}


def test_the_model_reads_the_amount_rather_than_estimating_it() -> None:
    """A EUR 95 ticket must classify as a small refund, not a high risk one."""
    registry = PromptRegistry("prompts")
    model = CountingClient(RuleBasedSupportModel())
    borderline = next(t for t in load_tickets() if "EUR 95" in t.body)

    found = classify(model, borderline, registry=registry)

    assert found.refund_amount_minor == 9500
    assert found.risk == "medium"
