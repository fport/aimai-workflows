- [ ] **The gate is a branch in the topology, not a sentence in a prompt.** A
      prompt instruction is a request; text inside the document under review can
      argue with it.
- [ ] **No side effect above an `interrupt()` call.** The node is replayed from
      its first line on every resume, so anything above that line fires once per
      resume.
- [ ] **The idempotency key comes from the facts**, never from a UUID, an
      attempt number or a clock. A key with a timestamp in it silently disables
      the deduplication it exists for.
- [ ] **The side effect's sink is `INSERT … ON CONFLICT DO NOTHING`,** not
      check-then-write. Two workers resuming the same thread at once is exactly
      the case the key is for, and check-then-write races.
- [ ] **`durability` is chosen per run.** `exit` for a batch that can restart,
      `sync` for anything a human is waiting on. Measure the difference with
      `SIGKILL`, not with an exception.
- [ ] **The thread id is derived from a business key.** Retried webhooks,
      operators after an incident and reviewers opening yesterday's email all
      know the business key and none of them knows a UUID.
- [ ] **The state holds references, not payloads.** A document carried in the
      state is written on every superstep and sits in durable storage for as
      long as the approval takes.
- [ ] **What was assessed is verified before it is acted on.** A gate lasts
      days; the file behind the URI can be replaced in that time.
- [ ] **Every state key is optional.** A deploy that adds a required key strands
      every paused run.
- [ ] **The serializer has an allowlist.** Reading a checkpoint constructs
      whatever type the bytes name.
- [ ] **The checkpointer is opened once per process**, in the application's
      lifespan — not per request.
- [ ] **A killed worker is tested, not assumed.** `SIGTERM` lets a shutdown hook
      run; `SIGKILL` is the failure that actually happens.
