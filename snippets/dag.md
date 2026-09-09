- [ ] **The graph is validated before anything runs**: duplicate ids, dangling
      references, cycles, compensation targets, fan-out sources. All free at
      startup, all expensive at runtime.
- [ ] **`SKIPPED` is counted separately from `FAILED`.** A job that correctly
      declined to do work is not a job that broke.
- [ ] **Hard and soft dependencies are distinguished**, and which one each edge
      is has been decided deliberately — that decision is usually a domain
      judgement, not an engineering one.
- [ ] **Degradation propagates and reaches the reader.** The missing-input list
      goes *into* the synthesis, not just into a log.
- [ ] **The fingerprint excludes execution policy.** Raising a timeout must not
      rerun a graph's worth of succeeded work.
- [ ] **Node versions are derived from the prompt**, not hand-maintained. An
      integer nobody bumps is a cache serving answers from a prompt that no
      longer exists.
- [ ] **Failures are recorded but never served from the cache.** A cached
      transient failure is a permanent one.
- [ ] **A budget stops the job, not just the node.** A runner that keeps
      scheduling after the ceiling spends it several times over.
- [ ] **Compensation fires when the side effect's own node failed**, not only
      when something downstream did. A send that raised is not a send that did
      not happen.
- [ ] **A failed compensation escalates instead of retrying.** Two automatic
      recovery layers turn one bad state into two.
- [ ] **Every `requires` edge has been shown to change the answer.** An edge
      that does not is costing parallelism and buying nothing.
- [ ] **The supervisor's constraints are in code**, not in its prompt:
      completed candidates removed, a hard step ceiling, a deterministic
      fallback for an unknown name.
- [ ] **Reports show p90 and max, never a mean.** A retry storm leaves the mean
      untouched and multiplies the maximum.
- [ ] **Per-node rates, not one success rate.** "94% of jobs succeeded" is
      compatible with one specialist being down all week.
