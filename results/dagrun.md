# DAG runner over 100 jobs

Deterministic failure mix (fixed seed); the specialists are scripted
agents, not models. Regenerate with `uv run python scripts/measure_dag.py`.

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
| Degraded jobs | delivered on partial evidence | 13/100 (13%) |
| Failed jobs | a sink node failed or was skipped | 7/100 |
| Needs a human | a compensation failed | 3/100 |
| Rerun saving | 1 − (spend on rerun / cost of result) | 100.0% |
| Cost per job, median | | $0.0780 |
| Cost per job, p90 | the number a budget is set from | $0.0780 |
| Cost per job, max | one job, worst case | $0.0780 |

No mean cost, on purpose: a retry storm leaves the mean untouched and
multiplies the maximum.
