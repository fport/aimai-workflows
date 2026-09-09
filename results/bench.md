# Stack benchmark

50 synthetic tickets through five orchestrators, same order, same
deterministic model (`RuleBasedSupportModel`, a stub — not an LLM).
Each stack is run three times over the fixture set: once to triage and
escalate, once from a fresh instance to approve, and once more to prove
a retry sends nothing twice.

| Stack | completed | escalated | resumed after restart | duplicate sends | llm calls | orchestration lines | seconds |
|---|---|---|---|---|---|---|---|
| plain | 50 | 15 | 15 | 0 | 100 | 160 | 0.20 |
| langgraph | 50 | 15 | 15 | 0 | 100 | 151 | 0.20 |
| pydantic-ai | 50 | 15 | 15 | 0 | 300 | 259 | 0.42 |
| openai-agents | 50 | 15 | 15 | 0 | 300 | 255 | 0.40 |
| strands | 50 | 15 | 15 | 0 | 300 | 255 | 0.51 |

`completed` counts tickets with at least one reply in the outbox;
`duplicate_sends` counts extra rows the customer would have received.
Both are read from the outbox rather than from what a stack says about
itself.
