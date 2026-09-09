---
hide:
  - navigation
---

<div class="aimai-hero" markdown>
![aimai-workflows](assets/header.png)
</div>

<div class="aimai-badges" markdown>
[![CI](https://github.com/fport/aimai-workflows/actions/workflows/ci.yml/badge.svg)](https://github.com/fport/aimai-workflows/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-8FE64A)](https://github.com/fport/aimai-workflows/blob/main/pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-8FE64A)](https://github.com/fport/aimai-workflows/blob/main/LICENSE)
</div>

# What this is

Three stages of agent orchestration, each answering a question the previous one
raised. Built on [aimai-kit](https://github.com/fport/aimai-kit), so the
provider, prompt, tool and agent layers come from there and this repository can
be about orchestration alone.

```bash
git clone https://github.com/fport/aimai-workflows && cd aimai-workflows
uv sync --all-extras --group dev
uv run pytest        # 167 tests: no API key, no database, no network
```

An LLM call takes seconds. An agent run takes minutes. A human approval takes
days. Everything in this repository follows from that gap.

---

## The three stages

<div class="grid cards" markdown>

-   **[06. Durable state and a human gate](06-contract-graph.md)**

    A four-node LangGraph flow that reviews a supplier contract, stops for a
    human when the risk is high, and writes exactly one CRM note.

    *The trap it closes:* `interrupt()` replays its node from the first line.
    A side effect written above that call fires once per resume — and the
    resume is the whole point of the design.

-   **[07. Five stacks, one flow](07-workflow-stacks.md)**

    The same support flow in plain Python, LangGraph, pydantic-ai, the OpenAI
    Agents SDK and Strands Agents, with the business logic written once and
    imported by all five.

    *The trap it closes:* comparing frameworks by writing five programs. Every
    difference is then an implementation difference, and the table measures
    nothing.

-   **[08. Writing the coordination layer](08-dag-orchestrator.md)**

    A DAG runner with no framework underneath it: five node kinds, nine
    states, hard and soft dependencies, fingerprint-based reruns, saga
    compensation.

    *The trap it closes:* treating partial success as either success or
    failure. `DEGRADED` is a state, it propagates, and it reaches the reader.

</div>

---

## Sixty seconds

=== "A review that pauses for days"

    ```python
    from aimai_workflows.contract import RuleBasedReviewer, compile_graph, thread_config
    from aimai_workflows.contract.checkpointer import postgres_checkpointer
    from langgraph.types import Command

    with postgres_checkpointer() as saver:
        app = compile_graph(RuleBasedReviewer(), saver)
        config = thread_config("SUP-2025-0042")

        out = app.invoke(
            {"contract_no": "SUP-2025-0042", "document_uri": "high_risk.txt"},
            config,
            durability="sync",
        )
        out["__interrupt__"]      # parked on the gate; the worker can now die

        # …hours later, in another process
        app.invoke(Command(resume="approve"), config, durability="sync")
    ```

=== "Two verbs, four stacks"

    ```python
    from aimai_workflows.stacks.plain import PlainStack
    from aimai_workflows.stacks.langgraph_stack import LangGraphStack

    for stack in (PlainStack(), LangGraphStack()):
        outcome = stack.run("T-1000")
        assert outcome.status == "awaiting_approval"   # a EUR 120 refund
        assert stack.approve("T-1000").status == "completed"
    ```

    ```bash
    python -m aimai_workflows.stacks.plain run T-1000
    uv run stack-bench            # results/bench.md
    ```

=== "A graph that knows what it lost"

    ```python
    from aimai_workflows.dagrun import NodeState, execute, open_store, validate
    from aimai_workflows.dagrun.flows import build_flow

    nodes = validate(build_flow(caselaw_fails=True))
    with open_store(":memory:") as store:
        report = await execute(nodes, "SZL-2026-0431", store)

    report.by_state(NodeState.DEGRADED)      # ['deliver', 'synthesis']
    report.records["synthesis"].result.output["opinion"]
    # "…INCOMPLETE: this opinion was written without caselaw."
    ```

    ```bash
    uv run dagrun --seed SZL-2026-0431 --dry-run
    ```

---

## What the numbers say

Every figure is reproducible from the repository with no credentials. Full
tables and their caveats are in **[Measurements](measurements.md)**.

| Experiment | Finding |
|---|---|
| `durability` modes under `SIGKILL` | `exit` loses the whole run; `sync` resumes at the unfinished node, for 0.23 ms per write |
| Side effect inside the gate | Two CRM notes for one approval — kept as an executable counter-example |
| Five stacks, 50 tickets | All three agent SDKs spend 3× the model calls; LangGraph costs fewer lines than writing the state machine by hand |
| Paused state per stack | 461 B hand-written, 4.9 KB LangGraph, 4.4 KB pydantic-ai, 6.0 KB Strands, 11.4 KB Agents SDK |
| `SIGKILL` mid-run | Strands repeats nothing; the graph versions repeat one call; the other two agent SDKs restart |
| DAG rerun | 100% of a rerun's spend served from the store |
| Degradation | 13 of 100 jobs delivered on partial evidence — and said so in the opinion |

!!! warning "The models behind these numbers are stubs"

    Not providers. The contract reviewer matches phrases, the support model
    triages with regexes, the specialists replay scripted transcripts. They do
    real work, and none of it is a model's work.

    That is deliberate: the claims here are about *coordination* — that a
    killed worker resumes, that an unapproved action is impossible, that a
    rerun costs nothing — and a claim like that has to be checkable in CI, on
    every push, without credentials and without variance. Two rows of the
    measurement tables are simulated rather than measured, and they say so
    wherever they appear.

---

## Before you ship any of this

Each stage explains why it looks the way it does. The
**[production checklist](checklist.md)** turns that into something to run
through before a release — every item tracing back to a failure one of the
three stages had to fix.
