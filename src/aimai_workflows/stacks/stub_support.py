"""A rule-based support model, so all four stacks run with no API key.

Same reasoning as stage 06's reviewer, with one addition specific to this
stage: the benchmark compares four orchestrators, and a real model would make
every column noisy for reasons that have nothing to do with orchestration.
Two runs of the same stack would differ, and a difference between stacks could
always be the model having a better morning. With a deterministic client, every
difference in the table is the framework.

It reports `provider="stub"`, and `results/bench.md` says so on every table.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

from aimai_kit.prompts.guard import TAG
from aimai_kit.provider.types import ChatRequest, ChatResult, Role, Usage

__all__ = ["RuleBasedSupportModel"]

_DOCUMENT_RE = re.compile(rf"<{TAG}[^>]*>\n(.*)\n</{TAG}>", re.DOTALL | re.IGNORECASE)
_AMOUNT_RE = re.compile(r"EUR\s*([\d.,]+)", re.IGNORECASE)
_ARTICLE_RE = re.compile(r"^\[([a-z]{4}-\d{2})\]\s*(.+)$", re.MULTILINE)

# Irreversible or legal: high regardless of any amount mentioned.
_CRITICAL = ("lawyer", "cannot get them back", "sla breach", "disaster", "cancelling")
# A refund with no amount stated: also high, because the amount is exactly what
# would decide it and we do not have it.
_UNQUANTIFIED_MONEY = ("refund", "charged twice", "money back")
_MEDIUM = ("declined", "recovery codes", "still charged", "patience", "holding up")

APPROVAL_THRESHOLD_MINOR = 10_000
"""Mirrors `core.REFUND_APPROVAL_MINOR`.

Deliberately duplicated rather than imported: this is the MODEL's opinion of
risk, and `core.is_risky` is the SYSTEM's rule. Importing one into the other
would hide the case the fixtures are built around — a model that calls a EUR 95
refund 'medium' and a system that escalates anyway when the number crosses the
line."""

_CATEGORIES = (
    ("refund", ("refund", "charged twice", "money back", "duplicate")),
    ("complaint", ("sla", "outage", "unacceptable", "disaster", "lawyer")),
    ("billing", ("invoice", "billing", "card", "charge", "seat")),
    ("account", ("password", "2fa", "recovery codes", "sign in", "invite")),
    ("technical", ("webhook", "import", "rate limit", "429", "api")),
)


def _amount_minor(text: str) -> int:
    match = _AMOUNT_RE.search(text)
    if not match:
        return 0
    raw = match.group(1).replace(",", "").rstrip(".")
    try:
        return int(round(float(raw) * 100))
    except ValueError:
        return 0


class RuleBasedSupportModel:
    """Phrase-matching triage and drafting, conforming to `LLMClient`."""

    provider = "stub"

    def __init__(self, model: str = "stub-support-v1") -> None:
        self.model = model
        self.calls = 0

    def complete(self, req: ChatRequest) -> ChatResult:
        self.calls += 1
        document = self._document(req)
        if req.operation == "draft":
            payload = json.dumps(self._draft(document), ensure_ascii=False)
        else:
            payload = json.dumps(self._classify(document), ensure_ascii=False)
        return ChatResult(
            text=payload,
            usage=Usage(
                input_tokens=len(document) // 4, output_tokens=len(payload) // 4
            ),
            provider=self.provider,
            model=self.model,
            prompt_ref=req.prompt_ref,
        )

    def stream(self, req: ChatRequest) -> Iterator[str]:
        yield self.complete(req).text

    @staticmethod
    def _document(req: ChatRequest) -> str:
        for message in req.messages:
            if message.role is Role.USER:
                match = _DOCUMENT_RE.search(message.content)
                if match:
                    return match.group(1)
        return ""

    def _classify(self, text: str) -> dict:
        lowered = text.lower()
        amount = _amount_minor(text)

        category = "technical"
        for name, keywords in _CATEGORIES:
            if any(k in lowered for k in keywords):
                category = name
                break

        if any(k in lowered for k in _CRITICAL):
            risk, reason = "high", "the loss is irreversible or legal action is named"
        elif amount >= APPROVAL_THRESHOLD_MINOR:
            risk, reason = (
                "high",
                "the refund asked for is above what support may settle",
            )
        elif amount:
            risk, reason = "medium", "a refund is asked for, but a small one"
        elif any(k in lowered for k in _UNQUANTIFIED_MONEY):
            risk, reason = "high", "money is at stake and no amount is stated"
        elif any(k in lowered for k in _MEDIUM):
            risk, reason = "medium", "the customer is unhappy but nothing is lost"
        else:
            risk, reason = "low", "a question the knowledge base answers"

        # The ticket that tells the triage system what to decide. It is
        # reported, not obeyed — and the risk stays whatever the content says.
        if "system:" in lowered or "no approval is required" in lowered:
            reason = "the ticket contains instructions addressed to the triage system"

        urgency = "high" if risk == "high" else "normal" if risk == "medium" else "low"
        return {
            "category": category,
            "urgency": urgency,
            "risk": risk,
            "refund_amount_minor": amount,
            "rationale": reason,
        }

    @staticmethod
    def _draft(text: str) -> dict:
        articles = _ARTICLE_RE.findall(text)
        if not articles:
            return {
                "text": (
                    "I do not have a documented answer for this, so I am handing "
                    "your ticket to a colleague who can look into it directly."
                ),
                "cited_article_ids": [],
            }
        first_id, first_title = articles[0]
        return {
            "text": (
                f"Thanks for writing in. The short answer is covered by our "
                f"guidance on {first_title.lower()}. If that does not resolve it, "
                f"reply here and a colleague will pick it up."
            ),
            "cited_article_ids": [article_id for article_id, _ in articles],
        }
