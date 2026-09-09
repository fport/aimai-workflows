- [ ] **The business logic is written once** and imported by every version being
      compared. Four implementations of the same idea confound every number in
      the table.
- [ ] **No prompt lives inside a framework's fields.** The registry renders it;
      the framework receives text. This is the practical measure of lock-in.
- [ ] **The risk rule is one constant, shared.** Otherwise the "escalated"
      column compares rules rather than orchestrators.
- [ ] **Duplicate sends are counted from the outbox**, not from what a stack
      reports about itself. The number that matters is the one the customer
      would have received.
- [ ] **Approvals are answered by a different instance** than the one that
      started the run. A stack that can only be approved by the object that
      started it has no human-in-the-loop support outside a demo.
- [ ] **The benchmark's model is deterministic.** A real model makes every
      column noisy for reasons unrelated to the framework.
- [ ] **Model calls are counted the same way for everyone**: requests that left
      for a model, not turns, spans or whatever each framework's telemetry
      happens to call them.
- [ ] **Where each framework's trace goes is written down.** At least one ships
      to a vendor by default.
- [ ] **Mid-run durability is stated at the granularity you actually wrote**,
      not the granularity the framework could support.
- [ ] **The decision table says when *not* to use each option.** A comparison
      with a winner and no constraints is an advertisement.
