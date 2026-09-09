"""The published diagram is the graph, not a picture of it.

`report.mermaid()` renders the flow from the node list the runner validates.
The documentation embeds that rendering. A diagram maintained by hand goes
stale the first time an edge changes and nobody notices until a reader is
misled — so it is generated, and this test pins that the committed copy still
matches.

Regenerate with:

    uv run dagrun --mermaid
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aimai_workflows.dagrun.flows import build_flow
from aimai_workflows.dagrun.report import mermaid
from aimai_workflows.dagrun.validate import validate

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGES_WITH_THE_DAG = (
    "README.md",
    "docs/08-dag-orchestrator.md",
    "docs/08-dag-orchestrator.tr.md",
)
_BLOCK = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)


def blocks(path: Path) -> list[str]:
    return [match.strip() for match in _BLOCK.findall(path.read_text(encoding="utf-8"))]


@pytest.mark.parametrize("page", PAGES_WITH_THE_DAG)
def test_the_published_dag_matches_the_graph(page: str) -> None:
    rendered = mermaid(validate(build_flow())).strip()
    found = blocks(REPO_ROOT / page)

    assert rendered in found, (
        f"{page} shows a DAG that is not the one `build_flow()` produces. "
        "Regenerate it with `uv run dagrun --mermaid`."
    )


@pytest.mark.parametrize(
    "page",
    [
        "README.md",
        "docs/06-contract-graph.md",
        "docs/06-contract-graph.tr.md",
        "docs/08-dag-orchestrator.md",
        "docs/08-dag-orchestrator.tr.md",
    ],
)
def test_every_diagram_declares_a_direction(page: str) -> None:
    """A Mermaid block whose first line is not a graph declaration renders as an
    error box on the published site, which is worse than no diagram."""
    found = blocks(REPO_ROOT / page)

    assert found, f"{page} was expected to carry a diagram"
    for block in found:
        assert block.splitlines()[0].startswith(("graph ", "flowchart ")), block[:60]
