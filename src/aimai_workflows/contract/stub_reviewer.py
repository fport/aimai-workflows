"""A rule-based reviewer that satisfies `LLMClient`, so the flow runs with no
API key.

Why a test double ships inside the package rather than under `tests/`: every
claim this repo makes — resume does not repeat the side effect, an unapproved
action is impossible, a schema change does not break an old checkpoint — has to
be reproducible by a reader who has no credentials, in CI, on every push. A
double that lives in the test tree can only be used by the tests; this one is
also what `scripts/kill_mid_run.py` and the README's curl walkthrough run
against.

THIS IS NOT A MODEL. It matches phrases. It reports `provider="stub"` so no
measurement taken with it can be mistaken for a model's, and the numbers in
`results/` say which reviewer produced them. What it does prove is that the
graph, the checkpointer, the gate and the idempotency key behave — and those,
not the quality of the risk report, are what this stage is about.

It is also deliberately fallible in two ways that the tests use:

- `invalid_attempts` makes the first N answers unparseable, which exercises
  aimai-kit's repair loop on the real code path.
- It under-reports `overall_risk` relative to its own findings on some
  documents, which is what `reconcile` exists to catch.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass

from aimai_kit.prompts.guard import TAG
from aimai_kit.provider.types import ChatRequest, ChatResult, Role, Usage

__all__ = ["RuleBasedReviewer", "Rule", "RULES"]

# The tag name comes from aimai-kit rather than being spelled again here: if
# the trust boundary is ever renamed, a stub that silently stops finding the
# document would turn every test green for the wrong reason.
_DOCUMENT_RE = re.compile(rf"<{TAG}[^>]*>\n(.*)\n</{TAG}>", re.DOTALL | re.IGNORECASE)
# A clause is a numbered paragraph: "5.2 The Customer shall indemnify…".
_CLAUSE_RE = re.compile(r"^(\d+\.\d+(?:\([a-z]\))?)\s+(.*)$", re.MULTILINE)
_SENTENCE_END = re.compile(r"(?<=[.;])\s+")


@dataclass(frozen=True, slots=True)
class Rule:
    """One phrase pattern and the finding it produces."""

    name: str
    pattern: re.Pattern[str]
    category: str
    severity: str
    summary: str


def _rule(name: str, pattern: str, category: str, severity: str, summary: str) -> Rule:
    return Rule(name, re.compile(pattern, re.IGNORECASE), category, severity, summary)


# Ordered by severity, and the first match on a clause wins: one clause
# produces one finding, the same rule the prompt gives the model.
RULES: tuple[Rule, ...] = (
    _rule(
        "injection",
        r"ignore (all )?previous instructions|system instruction:|"
        r"do not flag any clause",
        "compliance",
        "critical",
        "The document contains text addressed to an automated reviewer, "
        "instructing it to suppress findings.",
    ),
    _rule(
        "unlimited_liability",
        r"liability .{0,80}shall be unlimited|unlimited .{0,40}liability",
        "liability",
        "critical",
        "Our liability under this clause is uncapped.",
    ),
    _rule(
        "uncapped_indemnity",
        r"indemnify.{0,400}?without limitation as to amount",
        "liability",
        "critical",
        "We indemnify the supplier without any cap, including for their own "
        "negligence.",
    ),
    _rule(
        "ip_assignment",
        r"sole and exclusive property of the supplier|"
        r"perpetual, irrevocable, worldwide",
        "intellectual_property",
        "high",
        "Data or work product we generate becomes the supplier's property.",
    ),
    _rule(
        "no_deletion",
        r"no obligation to return or delete",
        "data_protection",
        "high",
        "The supplier keeps our data after termination.",
    ),
    _rule(
        "unilateral_change",
        r"at its sole discretion|continued use .{0,80}constitutes acceptance",
        "compliance",
        "high",
        "The supplier can change the deal unilaterally.",
    ),
    _rule(
        "no_exit",
        r"may not terminate this agreement for convenience|"
        r"as an exit fee",
        "termination",
        "high",
        "Exit is blocked or priced at the remainder of the term.",
    ),
    _rule(
        "long_notice_renewal",
        r"renews automatically.{0,400}?(one hundred and eighty|ninety) \(\d+\) days",
        "termination",
        "high",
        "Auto-renewal with a notice window long enough to be missed.",
    ),
    _rule(
        "supplier_cap",
        r"supplier's aggregate liability shall not exceed",
        "liability",
        "high",
        "The supplier's own liability is capped far below our exposure.",
    ),
    _rule(
        "compounding_interest",
        r"compounded monthly|interest at \d+(\.\d+)?% per month",
        "payment",
        "medium",
        "Late payment interest compounds monthly.",
    ),
    _rule(
        "flow_down_audit",
        r"subcontractors, at every tier|as amended from time to time",
        "compliance",
        "medium",
        "We must certify compliance with a policy the supplier can change.",
    ),
)

_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class RuleBasedReviewer:
    """Phrase-matching contract reviewer conforming to `LLMClient`."""

    provider = "stub"

    def __init__(
        self,
        model: str = "stub-rules-v1",
        *,
        invalid_attempts: int = 0,
        understate_overall: bool = True,
    ) -> None:
        self.model = model
        self.invalid_attempts = invalid_attempts
        self.understate_overall = understate_overall
        self.calls = 0

    # --- LLMClient ---------------------------------------------------------

    def complete(self, req: ChatRequest) -> ChatResult:
        self.calls += 1
        text = self._document(req)
        if self.calls <= self.invalid_attempts:
            # Not a parse error by accident: a truncated object is what a real
            # model produces when it runs out of output tokens, and it is the
            # case the repair loop has to survive.
            payload = '{"findings": [{"clause_id": "1.1",'
        else:
            payload = json.dumps(self._review(text), ensure_ascii=False)
        return ChatResult(
            text=payload,
            usage=Usage(
                input_tokens=len(text) // 4,
                output_tokens=len(payload) // 4,
            ),
            provider=self.provider,
            model=self.model,
            prompt_ref=req.prompt_ref,
        )

    def stream(self, req: ChatRequest) -> Iterator[str]:
        yield self.complete(req).text

    # --- the rules ---------------------------------------------------------

    @staticmethod
    def _document(req: ChatRequest) -> str:
        """Recover the contract from inside the trust boundary.

        The reviewer reads only what is between the tags, exactly like the
        model is told to. Reading the whole user message instead would make
        the stub blind to the trust boundary the prompt depends on.
        """
        for message in req.messages:
            if message.role is not Role.USER:
                continue
            match = _DOCUMENT_RE.search(message.content)
            if match:
                return match.group(1)
        return ""

    def _review(self, text: str) -> dict:
        findings = []
        for clause_id, body in self._clauses(text):
            for rule in RULES:
                match = rule.pattern.search(body)
                if not match:
                    continue
                findings.append(
                    {
                        "clause_id": clause_id,
                        "category": rule.category,
                        "severity": rule.severity,
                        "summary": rule.summary,
                        "quote": self._sentence_around(body, match.start()),
                    }
                )
                break

        worst = max(
            (f["severity"] for f in findings), key=lambda s: _ORDER[s], default="low"
        )
        overall = worst
        if self.understate_overall and worst == "critical" and len(findings) > 1:
            # The realistic failure: a model that lists a critical clause and
            # then calls the contract "high" overall. `reconcile` catches it.
            overall = "high"

        return {
            "findings": findings,
            "overall_risk": overall,
            "summary": (
                f"Rule-based review matched {len(findings)} clause(s); "
                f"worst severity {worst}. Produced by a stub, not a model."
            ),
        }

    @staticmethod
    def _clauses(text: str) -> list[tuple[str, str]]:
        """Split the contract into numbered clauses.

        Each clause runs to the start of the next numbered one, so a rule can
        match a phrase that sits two lines below the clause number.
        """
        matches = list(_CLAUSE_RE.finditer(text))
        clauses: list[tuple[str, str]] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[match.start(2) : end].strip()
            clauses.append((match.group(1), " ".join(body.split())))
        return clauses

    @staticmethod
    def _sentence_around(body: str, position: int) -> str:
        """Quote the sentence containing the match, verbatim.

        Verbatim matters even in the stub: `test_grounding` style checks and
        the reviewer's own eyes both compare the quote against the document,
        and a stub that paraphrases would hide a real model that paraphrases.
        """
        start = 0
        for split in _SENTENCE_END.finditer(body):
            if split.end() > position:
                break
            start = split.end()
        rest = body[start:]
        split = _SENTENCE_END.search(rest)
        sentence = rest[: split.start() + 1] if split else rest
        return sentence.strip()[:400]
