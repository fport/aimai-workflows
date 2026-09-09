"""What makes a node's result reusable, and what invalidates it.

A fingerprint answers one question: *would running this node again produce the
same thing?* Everything that changes the answer goes in; everything else stays
out, because each unnecessary ingredient throws away a cache hit that was
correct.

IN:
  - the node id and its `version`. The version is derived from the prompt file
    (`version_from_files`), not hand-maintained: an integer someone forgets to
    bump is a cache serving answers from a prompt that no longer exists.
  - the seed. Two jobs over different documents share nothing.
  - the outputs of every dependency, hard and soft, in sorted order. If an
    upstream answer changed, this node's inputs changed.
  - the fanout item, when there is one.

OUT, and each of these was tempting:
  - the wall clock. Including it is the same as having no cache.
  - the attempt number. A retry of the same work is the same work; including it
    would make every retry a cache miss, which is the opposite of the point.
  - cost and token counts. They are *results*, not inputs; a cheaper run of the
    same node is still the same node.
  - the state of nodes this one does not depend on. Tempting because it feels
    safer, and wrong: it couples unrelated branches so that any change anywhere
    reruns everything.
  - `max_attempts`, `timeout_s`, `cost_ceiling_usd`. Execution policy, not
    behaviour. Raising a timeout should not invalidate results.

The last one is the interesting call, and the argument against it is concrete:
if policy were in the fingerprint, then raising a timeout to get one flaky node
through would rerun the entire graph, including the expensive nodes that had
already succeeded.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path

from .types import Node, NodeResult

__all__ = ["fingerprint", "version_from_files"]


def _canonical(value: object) -> str:
    """Stable text for arbitrary output.

    `sort_keys=True` matters more than it looks: without it, two runs producing
    the same dictionary in a different insertion order would fingerprint
    differently, and the cache would never hit.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def fingerprint(
    node: Node,
    inputs: Mapping[str, NodeResult],
    seed: str,
    *,
    item: object = None,
) -> str:
    """The cache key for one node's execution."""
    parts = [
        f"id={node.id}",
        f"version={node.version}",
        f"seed={seed}",
    ]
    for dependency in sorted(set(node.dependencies)):
        result = inputs.get(dependency)
        # A missing dependency is part of the identity: the same node run with
        # and without its optional input produced different answers, and the
        # cache must not confuse them.
        parts.append(
            f"in[{dependency}]="
            + (_canonical(result.output) if result else "<missing>")
        )
    if item is not None:
        parts.append(f"item={_canonical(item)}")

    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


def version_from_files(*paths: str | Path) -> str:
    """Derive a node's `version` from the files that decide its behaviour.

    Usually the prompt. Passing the model name or a schema file is equally
    valid — the rule is that anything whose edit should invalidate the cache
    belongs here, and nothing else does.
    """
    digest = hashlib.sha256()
    for path in sorted(str(p) for p in paths):
        content = Path(path).read_bytes() if Path(path).exists() else b""
        digest.update(path.encode("utf-8"))
        digest.update(content)
    return "v" + digest.hexdigest()[:12]


def subtree(node_ids: Iterable[str], nodes: Iterable[Node]) -> set[str]:
    """Every node downstream of the given ones, inclusive.

    Used to explain a rerun: when one node's version changes, this is exactly
    the set that has to run again, and the report says so rather than leaving
    the reader to work out why eleven nodes reran.
    """
    nodes = tuple(nodes)
    affected = set(node_ids)
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node.id in affected:
                continue
            if affected & set(node.dependencies):
                affected.add(node.id)
                changed = True
    return affected
