# 8. Writing the coordination layer

Stages 06 and 07 used frameworks. This one writes the coordination layer by
hand, which is the only way to see what those frameworks were doing for you.

Five node kinds, nine states, a written state transition table, hard and soft
dependencies, fingerprint-based reruns and a SQLite result store. On top of it
one real flow, and a fifteen-line LangGraph supervisor for comparison.

!!! done "What we built here"

    A runner whose whole test suite passes without a model — which is the most
    valuable property of the code, because the thing being proved is
    coordination and a coordination test that needs an API key gets run once.

## The flow

```text
extract ──> scope_gate ──> statute  ─┐
    │           │                    ├──> synthesis ──> deliver ──> archive
    │           └───────> caselaw ···┘                     ╎
    └─────> clause_review ───────────┘              retract ╌╌ compensates
```

Every node kind earns its place:

| Kind | Node | Why it is not a TASK |
|---|---|---|
| `TASK` | `extract`, `statute`, `caselaw` | one unit of work, no branching |
| `GATE` | `scope_gate` | an out-of-scope document should **skip** the specialists, not fail them |
| `FANOUT` | `clause_review` | the number of clauses is unknown until `extract` has run, so the work cannot be laid out when the graph is built |
| `JOIN` | `synthesis` | the merge has to see *which* inputs degraded; a task receiving a dict cannot tell a missing input from an empty one |
| `COMPENSATE` | `retract` | never scheduled by dependencies — it waits for a failure |

`archive` exists so that something can fail *after* the side effect, which is
the case a saga is for: the opinion went out, the filing that had to accompany
it did not, and the recipient has to be told.

## Why this work is not one agent

One agent with five tools would be shorter, and it would lose four things.

**Partial success has nowhere to live.** When the case-law source is down, an
agent either gives up or quietly writes around the gap. The graph has a state
for it, propagates it, and puts it in the opinion.

**Reruns cost the same as first runs.** An agent's transcript is not
addressable work. Here `(seed, node_id, fingerprint)` means a rerun of an
unchanged job costs nothing.

**Independent work is actually independent.** The two specialists have separate
tool registries and separate contexts, so neither can be talked into the
other's conclusion, and they run in the same superstep. One agent doing both in
one context is serial and cross-contaminated.

**The side effect is undoable.** An agent that sent an email cannot un-send it,
because nothing recorded that sending was a step.

!!! note "The honest counter-argument"

    For a flow with three steps and no side effects, all of this is overhead
    and one agent is right. Multi-agent is usually unnecessary; the value is in
    being able to say why it was necessary *here*.

## The state transition table

Nine states, and `test_dag_execute.py` asserts the executor against this table.
Where the two disagree, the table is lying.

| From | To | When |
|---|---|---|
| `PENDING` | `READY` | every dependency reached a terminal state |
| `PENDING` | `SKIPPED` | a `requires` dependency is `FAILED` or `SKIPPED` |
| `PENDING` | `SKIPPED` | a `GATE` dependency returned `passed: false` |
| `READY` | `RUNNING` | the scheduler picked it up in this superstep |
| `RUNNING` | `SUCCEEDED` | returned, with every input complete |
| `RUNNING` | `DEGRADED` | returned, but an `optional` input was missing or degraded — or the node declared itself degraded |
| `RUNNING` | `READY` | raised or timed out, and attempts remain |
| `RUNNING` | `FAILED` | out of attempts, or over the node's cost ceiling |
| `READY` | `SKIPPED` | the job's cost ceiling was reached before it ran |
| `SUCCEEDED` / `DEGRADED` / `FAILED` | `COMPENSATING` | something downstream failed, or the node itself failed |
| `COMPENSATING` | `COMPENSATED` | the compensation returned |
| `COMPENSATING` | `FAILED` (job `needs_human`) | the compensation raised. No second attempt. |

`SKIPPED` is counted separately from `FAILED` in every report. Conflating them
makes a job that correctly declined to do work look like a job that broke.

## `requires` and `optional` are different edges

```python
Node(
    "synthesis",
    NodeKind.JOIN,
    _synthesis,
    requires=("clause_review", "statute"),
    optional=("caselaw",),          # (1)
)
```

1.  The single most consequential line in the flow.

A hard dependency that fails takes its dependants with it — `SKIPPED`, because
nothing broke. A soft dependency that fails leaves its dependants runnable and
`DEGRADED`, and the mark spreads downstream.

Without the distinction you get one of two bad flows: everything is required,
so one flaky lookup kills the job; or nothing is, so a synthesis quietly
reports a conclusion drawn from half its evidence.

That `optional` says, in code, that a due-diligence opinion grounded in statute
alone is worth delivering with a caveat, and that one grounded in case law
alone is not. **That is a legal judgement, not an engineering one**, and it
belongs in the graph where a reviewer can see and argue with it.

## Where `DEGRADED` shows up

Everywhere, before the result. The state propagates, `missing_data_section` is
emitted whether or not anything is missing — an explicit "nothing" is a
different statement from an absent section — and, the part that matters, the
missing input list is passed **into** the synthesis rather than logged:

```python
missing = list(context.missing_inputs) + list(context.degraded_inputs)
caveat = "" if not missing else (
    "\n\nINCOMPLETE: this opinion was written without " + ", ".join(missing)
    + ". Treat the conclusion as provisional in that respect."
)
```

A synthesis that is not told what it lacks writes with the confidence of one
that has everything, and that confident paragraph is what a reader acts on.

```python
def test_a_failed_optional_specialist_degrades_the_opinion():
    report = run_flow(caselaw_fails=True)
    opinion = report.records["synthesis"].result.output["opinion"]

    assert "INCOMPLETE" in opinion and "caselaw" in opinion
    assert len(sent_replies(SEED)) == 1     # a degraded opinion is still delivered
```

## What is in the fingerprint

A fingerprint answers one question: *would running this node again produce the
same thing?* Everything that changes the answer goes in; everything else stays
out, because each unnecessary ingredient throws away a cache hit that was
correct.

=== "In"

    - the node id and its `version` — derived from the prompt file's hash, not
      hand-maintained. An integer someone forgets to bump is a cache serving
      answers from a prompt that no longer exists.
    - the seed. Two documents share nothing.
    - the outputs of every dependency, hard and soft, in sorted order.
    - the fan-out item, when there is one.

=== "Out"

    - the wall clock. Including it is the same as having no cache.
    - the attempt number. A retry of the same work is the same work.
    - cost and token counts — they are *results*, not inputs.
    - the state of unrelated nodes. Tempting because it feels safer, and wrong:
      it couples branches so any change anywhere reruns everything.
    - **execution policy**: `max_attempts`, `timeout_s`, `cost_ceiling_usd`.

The last one is the interesting call, and the argument is concrete: if policy
were in the key, raising a timeout to get one flaky node through would rerun
the entire graph, including the expensive nodes that had already succeeded.

```python
def test_changing_one_node_version_reruns_it_and_its_subtree():
    ...
    assert calls == ["b", "c"], "a is upstream of the change and must not rerun"
```

!!! danger "Cache the successes, never the failures"

    `ResultStore.put` records every terminal state for the report; `get` only
    ever returns `SUCCEEDED` and `DEGRADED`. A cached transient failure is a
    permanent one.

## Compensation fires on two triggers

```python
def _needs_compensating(target_id, nodes, records) -> str:
    if records[target_id].state is NodeState.FAILED:
        return f"{target_id} failed after it may already have taken effect"
    ...
    failed_below = [n for n in _downstream_of(target_id, nodes) if failed(n)]
```

1. **Something downstream failed.** The side effect happened and the work it
   was part of did not complete — the classic saga case.
2. **The node itself failed.** A send that raised is not a send that did not
   happen: the message may have gone out and the acknowledgement been lost.
   Compensating is safe because the compensation is idempotent; assuming
   nothing happened is not.

Deliberately **not** a trigger: a failure elsewhere in the graph that this node
neither depends on nor feeds. An optional case-law lookup timing out has no
bearing on whether the opinion should have been sent, and retracting it because
an unrelated branch failed would be worse than the outage.

```python
def test_an_unrelated_failure_does_not_retract_a_good_delivery():
    report = run_flow(caselaw_fails=True)

    assert report.records["caselaw"].state is NodeState.FAILED
    assert report.records["retract"].state is NodeState.SKIPPED
    assert len(sent_replies(SEED)) == 1
```

A failed compensation escalates instead of retrying. Two automatic recovery
layers turn one bad state into two, and the second is always the one nobody has
a runbook for: `needs_human`, exit code 3, operator queue.

## Static graph or supervisor?

**If the shape of the work is fixed, use the graph. If it is not, use a
supervisor.** The due-diligence flow always extracts, always reviews clauses
and always asks both specialists — so the graph is right, and
`dagrun/supervisor.py` exists to make the comparison concrete.

The supervisor is fifteen lines of routing and three constraints, each of which
is a failure that appears immediately without it:

1. **Completed specialists leave the candidate list.** Otherwise the model asks
   for the same expert forever, because its last answer was useful.
2. **A hard step ceiling in code**, not a sentence in a prompt. This is what
   keeps a bad routing decision from becoming an unbounded bill.
3. **An unknown name falls back** to the first outstanding specialist rather
   than raising. A supervisor that crashes on a hallucinated name turns a
   recoverable routing mistake into a failed job.

```python
def test_a_hallucinated_specialist_falls_back_rather_than_raising():
    out = build_supervisor(chooser=lambda state, outstanding: "notary-expert").invoke(...)

    assert out["visited"] == ["statute", "caselaw"]
```

Not `langgraph-supervisor`: it is still in its 0.0.x band, and those three
constraints are the entire value of the file. A dependency that owns the loop
owns the constraints too.

The parallel specialists write to one state key, so the reducer has to be
order-independent — the same rule as stage 06's findings reducer, for the same
reason.

## The test that has no equivalent elsewhere

```python
@pytest.mark.parametrize(("node_id", "dropped"), [...])
def test_every_required_edge_is_real(node_id: str, dropped: str) -> None:
    """Remove a hard dependency; the node's output must change."""
```

A `requires` edge that does not change the answer is not a dependency — it is a
serialisation of work that could have run in parallel. Nothing else in a
codebase catches that: not a type error, not a test failure, not a performance
regression anyone can point at. The test drops one hard edge at a time and
asserts the node that declared it produces a different result without it.

## Reading the run report

```bash
uv run dagrun --seed SZL-2026-0431 --fail archive
```

```text
job SZL-2026-0431
  outcome        failed
  supersteps     7
  cost           $0.0780  (spent this run $0.0000)
  cache hits     19
  reason         completed

  node                 state         attempts  cost      cached  note
  --------------------------------------------------------------------
  archive              failed        1         $0.0000   no      RuntimeError: the source…
  deliver              compensated   1         $0.0000   yes     opinion delivered
  retract              compensated   1         $0.0000   no      archive failed downstream…
  ...
```

Two columns are worth pointing at. `cost` and `spent this run` are different
numbers: the first is what the result cost to produce across every run that
contributed to it, the second is what this run paid. The gap is what the store
saved.

The exit code is a number a shell can branch on: `0` clean, `1` degraded, `2`
failed, `3` needs a human. A job that degraded is not a job that failed, and a
pipeline treating them the same will either page too often or not enough.

## Checklist

--8<-- "dag.md"
