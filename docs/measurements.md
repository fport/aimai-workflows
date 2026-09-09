# Measurements

Every table on this page is produced by a script in the repository and every
one of them runs with no API key, no database and no network. The commands are
in each section, and CI regenerates the pinned ones on every push — a change
that quietly invalidates a published number fails the build rather than being
noticed by a reader.

!!! warning "Read this before quoting any number here"

    **The models are stubs.** The contract reviewer matches phrases, the
    support model triages with regexes, the specialists replay scripted
    transcripts. They do real work and none of it is a model's work.

    That is deliberate. The claims are about coordination — a killed worker
    resumes, an unapproved action is impossible, a rerun costs nothing — and a
    claim like that has to be checkable in CI, on every push, without
    credentials and without variance. Two rows below are **simulated** rather
    than measured, and they are labelled everywhere they appear, including in
    the script that produces them.

    A table that quietly presents an invented p95 as an observation is worth
    less than no table.

---

## 06 — durability

```bash
uv run python scripts/measure.py --runs 100            # SQLite
uv run python scripts/measure.py --postgres --runs 100 # a real database
```

### The three `durability` modes

The same review — start, pause at the gate, approve — under each mode.

| Mode | End to end (s) | Checkpoint writes | Supersteps | After `SIGKILL` during the assessment |
|---|---|---|---|---|
| `exit` | 0.083 | 2 | 2 | nothing survived; the review restarts |
| `async` | 0.006 | 6 | 6 | resumes at `assess_risk` |
| `sync` | 0.018 | 6 | 6 | resumes at `assess_risk` |

An exception is the wrong instrument for this comparison: LangGraph catches it
and persists what it has, so all three modes look identical. `SIGKILL` is the
honest test, and it is also the failure that actually happens.

### 100 reviews

| Metric | How it is measured | SQLite | PostgreSQL |
|---|---|---|---|
| Resume success rate | completed `Command(resume=…)` / reviews | 1.000 | 1.000 |
| Replay rate | reviews whose assessment node ran twice | 0.000 | 0.000 |
| Mean state size | serialized state per thread | 3,402 B | 3,402 B |
| Largest state | same, worst thread | 4,963 B | 4,963 B |
| Checkpoint write latency p95 | per `put`, `sync` mode | 0.231 ms | 1.009 ms |
| Checkpoint writes | total over the run | 566 | 566 |
| Time per review | wall clock / reviews | 3.8 ms | 9.4 ms |
| Approval wait p50 | **SIMULATED**, not observed | 2.91 h | — |
| Approval wait p95 | **SIMULATED**, not observed | 18.07 h | — |
| Human disagreement rate | **SIMULATED**, not observed | 0.240 | — |

The three simulated rows need a human answering real emails over real weeks.
They come from a lognormal draw with a fixed seed and are marked as such in
`results/measurements.md`, in `scripts/measure.py`, and here.

### What 100 reviews leave in Postgres

| Table | Size | Rows |
|---|---|---|
| `checkpoint_writes` | 1,112 KiB | 1,856 |
| `checkpoints` | 832 KiB | 596 |
| `checkpoint_blobs` | 344 KiB | 211 |

Roughly 23 KiB per review. The pending-writes table being the largest is not
obvious until you see it: every superstep records the writes it intends before
it commits them, and an interrupted flow keeps those rows for as long as the
approval takes. The shape matters more than the size — it grows with
supersteps, not with document size, so retention belongs in the plan.
`delete_thread(thread_id)` is the tool.

### The crash script

```bash
uv run python scripts/kill_mid_run.py
```

```json
{
  "seconds_to_gate": 0.379,
  "worker_returncode": -9,
  "next_before_resume": ["human_gate"],
  "findings_recovered": 13,
  "notes_before_resume": 0,
  "notes_after_resume": 1,
  "passed": true
}
```

---

## 07 — four stacks

```bash
uv run stack-bench                              # results/bench.md
uv run python -m aimai_workflows.stacks.chaos   # results/chaos.md
```

### 50 tickets, three passes each

| Stack | completed | escalated | resumed after restart | duplicate sends | llm calls | orchestration lines | seconds |
|---|---|---|---|---|---|---|---|
| plain Python | 50 | 15 | 15 | 0 | 100 | 160 | 0.25 |
| LangGraph | 50 | 15 | 15 | 0 | 100 | 151 | 0.24 |
| pydantic-ai | 50 | 15 | 15 | 0 | 300 | 259 | 0.66 |
| OpenAI Agents SDK | 50 | 15 | 15 | 0 | 300 | 255 | 0.46 |

`completed` counts tickets with at least one reply in the outbox;
`duplicate_sends` counts extra rows the customer would have received. Both are
read from the message store rather than from what a stack says about itself.

`orchestration lines` excludes docstrings, comments and blank lines — counting
comment lines would reward the version with the most explaining to do.

### `SIGKILL` at two points

| Stack | Killed while parked → approved? | Replies sent | Paused state | Killed mid-run → resumed? | Model calls repeated |
|---|---|---|---|---|---|
| plain Python | yes | 1 | 461 B | yes | 1 |
| LangGraph | yes | 1 | 4,953 B | yes | 1 |
| pydantic-ai | yes | 1 | 4,364 B | yes | 2 |
| OpenAI Agents SDK | yes | 1 | 11,427 B | yes | 2 |

`Paused state` is every text and blob column each stack wrote for one paused
ticket — what it costs to keep a run waiting for a human. Twenty-five times the
storage at the far end: irrelevant at 50 tickets, a conversation at 50,000 open
approvals.

`Model calls repeated` counts calls to the shared business logic in the process
that took over. One means the work before the kill was reused; two means the
flow started from the beginning.

---

## 08 — the DAG runner

```bash
uv run python scripts/measure_dag.py --jobs 100   # results/dagrun.md
```

100 jobs with a deterministic failure mix: roughly one in six loses case law,
one in twenty-five loses the filing after delivery, one in a hundred cannot
retract.

| Node | succeeded | degraded | failed | skipped | compensated |
|---|---|---|---|---|---|
| `extract` | 100 | 0 | 0 | 0 | 0 |
| `scope_gate` | 100 | 0 | 0 | 0 | 0 |
| `clause_review` | 100 | 0 | 0 | 0 | 0 |
| `statute` | 100 | 0 | 0 | 0 | 0 |
| `caselaw` | 87 | 0 | 13 | 0 | 0 |
| `synthesis` | 87 | 13 | 0 | 0 | 0 |
| `deliver` | 80 | 13 | 0 | 0 | 7 |
| `archive` | 77 | 13 | 10 | 0 | 0 |

| Metric | How it is measured | Value |
|---|---|---|
| Clean jobs | every sink node succeeded | 77/100 |
| Degraded jobs | delivered on partial evidence | 13/100 |
| Failed jobs | a sink node failed or was skipped | 7/100 |
| Needs a human | a compensation failed | 3/100 |
| Rerun saving | 1 − (spend on rerun / cost of result) | 100% |
| Cost per job, median | | $0.0780 |
| Cost per job, p90 | the number a budget is set from | $0.0780 |
| Cost per job, max | one job, worst case | $0.0780 |

!!! note "Two things this table refuses to report"

    **No mean cost.** A supervisor loop or a retry storm leaves the mean
    untouched and multiplies the maximum, so a table with a mean and no tail
    hides the only number that matters when the bill arrives.

    **No aggregate success rate.** "94% of jobs succeeded" is compatible with
    the case-law specialist being down all week — every job degraded, none
    failed, and the aggregate looks fine. Per-node rates make an outage
    visible.

Two caveats to apply to the cost rows: the specialists are scripted agents
rather than models, which is why p90 equals the max — the cost spread of a real
model is not in this table. And the 13% degradation rate is the failure mix the
script injects, not an observation about any real service.

---

## What CI pins, and what it does not

`results/durability.json`, `results/bench.json` and `results/dagrun.json` hold
only **deterministic** facts: checkpoint write counts, supersteps, replay
rates, orchestration line counts, duplicate-send counts, per-node state counts.
CI regenerates them and fails on a diff, so a refactor that doubles the writes
per run or makes a resume re-run an assessment is caught by a diff rather than
by a reader.

The Markdown tables hold timings as well. Those differ between machines, so
they are committed but not diffed.
