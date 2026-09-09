# 7. Four stacks, one flow

The same support flow written four times — plain Python, LangGraph,
pydantic-ai, the OpenAI Agents SDK — so that "which framework" stops being an
opinion and becomes a table.

Classify a ticket, fetch knowledge base articles, draft a reply, stop for a
human when the ticket is risky, send. Five steps, and the fourth carries the
whole comparison: steps 1-3 and 5 look nearly identical in every framework;
pausing mid-flow, persisting, and continuing hours later on a human decision
looks completely different in each.

!!! done "What we built here"

    One `core.py` with the business logic, four orchestration modules importing
    it, one parametrized test suite all four pass, a benchmark over 50 tickets
    and a chaos script that kills each stack's worker at two different points.

## The rule that makes it a comparison

**The business logic is written once.** `stacks/core.py` holds classification,
retrieval, drafting, the risk rule and the send; all four stacks import them.
Whatever differs between the four files is orchestration and nothing else.

If the logic were written four times, every number in the benchmark would be
confounded by four slightly different implementations of the same idea — and
that is what most framework comparisons on the internet are measuring without
knowing it.

Two consequences worth stating:

- **No prompt lives in a framework's fields.** Both prompts are files in
  `prompts/`, loaded through aimai-kit's registry. A framework that wants the
  system prompt in a decorator argument gets the rendered text handed to it.
  This is the practical measure of lock-in.
- **The risk threshold is one constant in `core`.** If each stack decided risk
  its own way, the benchmark's `escalated` column would compare four rules
  rather than four orchestrators.

## The four versions, in their own words

=== "plain Python"

    Orchestration is a `match` over a `step` column in SQLite.

    ```python
    def run(self, ticket_id: str) -> RunOutcome:
        step, payload = self._read(ticket_id) or ("", {})
        if step == "drafted":
            return self._after_draft(ticket, payload, resumed=True)
        if step == "classified":
            classification = Classification.model_validate(payload)
        else:
            classification = classify(self.client, ticket, registry=self.registry)
            self._write(ticket_id, "classified", classification.model_dump())
        ...
    ```

    What it demonstrates is how little a durable, resumable, human-in-the-loop
    flow needs when the flow is a straight line: a table with a step column, a
    transaction per step, and an idempotent side effect.

    What it costs is visible too. The resume path is written by hand, one
    branch per step — and those eleven lines are exactly what goes stale when
    someone adds a step and forgets them. There is no history: one row per
    ticket, and no answer to "what did classification return before the human
    corrected it".

=== "LangGraph"

    The state machine is handed to the framework.

    ```python
    def approval_gate(state: SupportState) -> SupportState:
        answer = interrupt({"ticket_id": state["ticket_id"], "question": "Send this reply?"})
        return {"decision": answer if answer in ("approve", "reject") else "reject"}

    graph.add_conditional_edges("compose", needs_approval, {...})
    ```

    You stop writing the step column, the `match`, and the "where was I" logic.
    Resumption is `invoke(None, config)` — no branch per step, no resume path
    to keep in sync with the flow. `get_state_history(config)` gives every
    intermediate state for free.

    You pay a dependency with its own release cadence, a state schema that has
    to be serializable, and a mental model — supersteps, channels, reducers,
    replay — that everyone debugging it has to learn first.

=== "pydantic-ai"

    Typed tools, and a *tool-level* human-in-the-loop.

    ```python
    @agent.tool
    def deliver(ctx: RunContext[SupportDeps], ticket_id: str) -> str:
        if is_risky(ticket, classification) and not ctx.tool_call_approved:
            raise ApprovalRequired
        return send_reply(ticket_id, ctx.deps.draft["text"], stack=NAME).idempotency_key
    ```

    The run ends with `DeferredToolRequests` instead of an answer, and a later
    run continues with `DeferredToolResults(approvals={call_id: True})`.

    **The state is yours to carry.** There is no checkpointer; what survives
    between the two verbs is the message history, and this file serializes it
    with `ModelMessagesTypeAdapter` into a column it chose. More code than
    LangGraph, and in exchange no opinion about where state lives.

    **The gate moves into the tool.** In the two graph versions the risk check
    is a branch *before* the side effect. Here it is a condition *inside* the
    tool that performs it, because that is where the framework's approval
    mechanism lives. Worth knowing before adopting it: an approval you want two
    steps before the side effect has to be modelled as a separate tool.

=== "OpenAI Agents SDK"

    The most control sits with the model. `needs_approval` accepts a callable,
    so the shared risk rule can decide per call:

    ```python
    async def approval_needed(ctx, params, call_id) -> bool:
        return is_risky(load_ticket(params["ticket_id"]), classification)

    @function_tool(needs_approval=approval_needed)
    def deliver(ticket_id: str) -> str: ...
    ```

    State is a `RunState`: `to_json()` out, `RunState.from_json(agent, blob)`
    back. The blob holds items, usage, approvals and a tool-use tracker — the
    whole run, so the SDK can reconstruct the loop for you.

    Two operational notes: `from_json` is async while `Runner.run_sync` is not,
    so a synchronous CLI has to bridge; and **tracing ships to OpenAI by
    default**. This repository calls `set_tracing_disabled(True)` on import,
    deliberately and visibly, because a comparison repo that quietly uploads
    its runs to a vendor is measuring one thing and doing another.

## The benchmark

50 synthetic tickets, same order, same deterministic model, three passes each:
run, approve from a fresh instance, then run everything again to prove a retry
sends nothing twice.

| Stack | completed | escalated | resumed after restart | duplicate sends | llm calls | orchestration lines |
|---|---|---|---|---|---|---|
| plain Python | 50 | 15 | 15 | 0 | 100 | 160 |
| LangGraph | 50 | 15 | 15 | 0 | 100 | **151** |
| pydantic-ai | 50 | 15 | 15 | 0 | **300** | 259 |
| OpenAI Agents SDK | 50 | 15 | 15 | 0 | **300** | 255 |

Two findings worth stating plainly.

**Three times the model calls.** The two agent versions spend one model turn
per tool call on top of the two the business logic makes — the model is
deciding what to do next, and that decision is a request. For a five-step flow
whose shape never varies, 200 extra calls per 50 tickets bought nothing. They
buy something the moment the shape *does* vary, which is
[stage 08](08-dag-orchestrator.md)'s question.

**LangGraph is not more code than writing it yourself.** 151 lines against 160.
The plain version spends its lines on the step column, the `match` and the
hand-written resume path — the code a checkpointer would have written.

!!! note "Why the counters come from the outbox"

    `completed` and `duplicate_sends` are computed from the message store, not
    from what a stack reports about itself. `duplicate_sends` in particular is
    the number of extra rows the customer would have received, which is the
    only definition that matters.

## Chaos: `SIGKILL` at two points

| Stack | Killed while parked → approved? | Paused state | Killed mid-run → resumed? | Model calls repeated |
|---|---|---|---|---|
| plain Python | yes | 461 B | yes | 1 — resumed at the unfinished step |
| LangGraph | yes | 4,953 B | yes | 1 — resumed at the unfinished step |
| pydantic-ai | yes | 4,364 B | yes | 2 — no mid-run state; the run restarted |
| OpenAI Agents SDK | yes | 11,427 B | yes | 2 — no mid-run state; the run restarted |

All four survive being killed **while parked on the gate**, and none sends a
duplicate reply when it comes back. That is the bar, and all four clear it.

They separate on being killed **mid-run**. The two versions with a state store
written per step resume at the step that had not finished; the two agent
versions have nothing between "run started" and "run returned", so they repeat
the work.

!!! warning "The honest answer to \"does it resume\""

    Both frameworks *can* checkpoint mid-run — pydantic-ai through
    `agent.iter()`, the Agents SDK through per-turn `to_state()`. Neither does
    it for you, and this repository did not write it. So: yes, at the
    granularity you were willing to write.

## Decision table — cost and control

| Stack | Orchestration lines | Who decides the next step | Reach for it when |
|---|---|---|---|
| plain Python | 160 | you, entirely | the flow is a straight line, the team is small, and a dependency needs an argument. Also right when the flow will be read more often than changed. |
| LangGraph | 151 | you, in a topology | the flow branches, pauses for humans, or has to be inspectable afterwards. Costs a mental model everyone debugging it has to learn. |
| pydantic-ai | 259 | the model, within typed tools | the codebase is already pydantic-shaped and you want typed tools without adopting a runtime. You carry the state. |
| OpenAI Agents SDK | 255 | the model, mostly | the work genuinely varies per input, so a fixed topology would be a lie. The loop is the vendor's and the state blob will not migrate. |

## Decision table — state, HITL and lock-in

| Stack | State lives in | HITL mechanism | Lock-in |
|---|---|---|---|
| plain Python | a `runs` table you designed | a `step` column read by `approve` | none; your schema, your SQL |
| LangGraph | the checkpointer (SQLite/Postgres) | `interrupt()` + `Command(resume=…)` | moderate: the state schema and topology are LangGraph's, the nodes are not |
| pydantic-ai | a message history **you** serialize | `ApprovalRequired` + `DeferredToolResults` | low: JSON you store where you like |
| OpenAI Agents SDK | a `RunState` blob **you** serialize | `needs_approval` + `state.approve()` | highest: the blob is the SDK's shape. Readable, not portable. |

**Trace destinations**, since nobody reads this until it is a problem: plain
Python has none. LangGraph emits to LangSmith when `LANGCHAIN_TRACING_V2` is
set, otherwise nothing. pydantic-ai emits OpenTelemetry, off unless configured.
The Agents SDK ships traces to OpenAI by default.

## What I would run in production, today

**LangGraph**, for a flow shaped like this one. It costs the same lines as
writing the state machine by hand, and in exchange the resume path is not code
I have to keep in sync with the flow. State history is the other half: the
first time a reviewer asks "what did it say before I corrected it", the plain
version has no answer.

I would not reach for either agent SDK here. The flow's shape does not vary, so
paying three times the model calls for a model to rediscover that shape on
every ticket is spending money to add variance.

The constraint that would change my mind is a team already fluent in one of
them. All four passed every test in this repository; none of the differences
above is worth relearning an ecosystem over.

## Checklist

--8<-- "stacks.md"
