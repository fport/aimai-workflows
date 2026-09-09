"""Shared fixtures.

Two environment variables are redirected for every test: the fake CRM's
database and the document root. Both default to paths relative to the working
directory, which is convenient for a local run and would make the test suite
depend on where it was started from — and, worse, would let a test leave notes
in the file a developer's own run reads.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from aimai_kit.prompts import PromptRegistry

from aimai_workflows.contract import RuleBasedReviewer, compile_graph, reset_crm
from aimai_workflows.contract.checkpointer import in_memory_checkpointer
from aimai_workflows.stacks.core import reset_outbox

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "fixtures"


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every durable store at somewhere disposable.

    Both stages write to SQLite on purpose — the crash tests need a store that
    outlives a killed process — so both need redirecting, or a test run would
    leave notes in the file a developer's own run reads.
    """
    monkeypatch.setenv("CONTRACT_CRM_DB", str(tmp_path / "crm.sqlite3"))
    monkeypatch.setenv("CONTRACT_DOCUMENT_ROOT", str(FIXTURES))
    monkeypatch.setenv("SUPPORT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SUPPORT_OUTBOX_DB", str(tmp_path / "outbox.sqlite3"))
    reset_crm()
    reset_outbox()


@pytest.fixture
def registry() -> PromptRegistry:
    return PromptRegistry(REPO_ROOT / "prompts")


@pytest.fixture
def reviewer() -> RuleBasedReviewer:
    return RuleBasedReviewer()


@pytest.fixture
def graph(reviewer: RuleBasedReviewer, registry: PromptRegistry):
    """A compiled graph over an in-memory checkpointer."""
    return compile_graph(reviewer, in_memory_checkpointer(), registry=registry)


@pytest.fixture
def document_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A writable copy of the fixtures, for tests that mutate a document."""
    root = tmp_path / "documents"
    root.mkdir()
    for source in FIXTURES.glob("*.txt"):
        (root / source.name).write_bytes(source.read_bytes())
    monkeypatch.setenv("CONTRACT_DOCUMENT_ROOT", str(root))
    yield root


@pytest.fixture
def sqlite_path(tmp_path: Path) -> str:
    return str(tmp_path / "checkpoints.sqlite3")


def new_graph(registry: PromptRegistry, checkpointer, **kwargs):
    """Compile a fresh graph object over an existing checkpointer.

    Used by the resume tests: throwing the graph away and building another one
    is how a redeploy looks from the checkpointer's side.
    """
    return compile_graph(RuleBasedReviewer(**kwargs), checkpointer, registry=registry)


@pytest.fixture(
    params=["plain", "langgraph", "pydantic-ai", "openai-agents", "strands"]
)
def stack(request, registry):
    """Every stack, one at a time, behind the same two verbs.

    Parametrized rather than four near-identical test modules: the contract is
    the claim, and a suite that lets one stack have its own tests would let it
    have its own behaviour.
    """
    from aimai_workflows.stacks.bench import stack_factories

    return stack_factories()[request.param](None, registry)


os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
"""Fail rather than warn if the state carries a type the serializer allowlist
does not name. See `checkpointer.ALLOWED_STATE_TYPES`."""
