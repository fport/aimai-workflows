# Production checklist

Every item here traces back to a failure one of the three stages had to fix.
They are grouped by the stage that produced them, and each block also appears at
the end of its own chapter.

Nothing on this list is about model quality. These are the failures that happen
after the model is good enough — the ones that show up on a Friday, in a
process that is already gone.

## Durable state and human approval

From [stage 06](06-contract-graph.md). Applies to any flow that pauses for a
person or performs a side effect.

--8<-- "durability.md"

## Comparing orchestrators

From [stage 07](07-workflow-stacks.md). Applies to any framework evaluation you
intend to defend.

--8<-- "stacks.md"

## Coordination and partial success

From [stage 08](08-dag-orchestrator.md). Applies to any runner with more than
one path through it.

--8<-- "dag.md"

## The three questions to ask first

Before any of the above, three that decide the shape of everything else:

1. **Where does the state live while a human is deciding?** If the answer is
   "in the process", there is no human-in-the-loop support — there is a demo.
2. **What happens if this dies right now?** Not "if it raises" — if it is
   killed. Everything a framework catches, it catches only when there is a
   process left to catch it.
3. **What does partial success look like in the report?** If the answer is
   "success" or "failure", the report is lying about one of them.
