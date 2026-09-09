"""Wiring the four nodes into a graph.

The graph is static and small enough to hold in your head. That is a choice:
dynamic routing, supervisors and agents deciding the next node belong to stage
08, where the point is the coordination layer. Here the point is durability,
and a topology you can read is what makes the guarantee checkable.

Two edges carry the argument of the whole stage:

    assess_risk --[risk >= high]--> human_gate --> execute_action
    assess_risk --[risk <  high]--> execute_action

The threshold is a conditional edge, not a sentence in the prompt. A prompt
instruction is a request; an edge is a branch. Text inside the contract can
argue with the first — `fixtures/injected.txt` tries exactly that — and cannot
touch the second.
"""

from __future__ import annotations

from aimai_kit.prompts import PromptRegistry
from aimai_kit.provider.client import LLMClient
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .nodes import build_nodes, route_after_assessment, route_after_gate
from .state import ContractState

__all__ = ["build_graph", "compile_graph", "thread_config", "thread_id_for"]


def build_graph(
    client: LLMClient,
    *,
    registry: PromptRegistry | None = None,
    prompt_key: str | None = None,
) -> StateGraph:
    """Build the uncompiled graph.

    Uncompiled on purpose: compiling binds a checkpointer, and the same
    topology is compiled against an in-memory saver in tests, a SQLite file in
    the crash script and Postgres in the service. A builder that returned a
    compiled graph would force the checkpointer choice on every caller.
    """
    nodes = build_nodes(client, registry=registry, prompt_key=prompt_key)
    graph = StateGraph(ContractState)

    for name, fn in nodes.items():
        graph.add_node(name, fn)

    graph.add_edge(START, "fetch_document")
    graph.add_edge("fetch_document", "assess_risk")
    graph.add_conditional_edges(
        "assess_risk",
        route_after_assessment,
        {"human_gate": "human_gate", "execute_action": "execute_action"},
    )
    graph.add_conditional_edges(
        "human_gate",
        route_after_gate,
        {"execute_action": "execute_action", "end": END},
    )
    graph.add_edge("execute_action", END)
    return graph


def compile_graph(
    client: LLMClient,
    checkpointer,
    *,
    registry: PromptRegistry | None = None,
    prompt_key: str | None = None,
) -> CompiledStateGraph:
    """Compile the graph against a checkpointer.

    A checkpointer is required rather than optional. Without one `interrupt()`
    has nowhere to store the paused state, and the flow would raise at the gate
    — at runtime, on the first risky contract, in production. Requiring it in
    the signature moves that failure to import time.
    """
    if checkpointer is None:
        raise ValueError(
            "this graph interrupts for human approval and cannot run without a "
            "checkpointer; pass InMemorySaver() in tests"
        )
    return build_graph(client, registry=registry, prompt_key=prompt_key).compile(
        checkpointer=checkpointer
    )


def thread_id_for(contract_no: str) -> str:
    """Derive the thread id from the contract, never from a fresh UUID.

    The thread id is the identity of the review, and it has to be recoverable
    from the outside world. A reviewer who opens yesterday's approval email,
    an operator resuming a run after an incident, and a retried webhook all
    know the contract number and none of them know a UUID the service invented.
    Deriving it also makes a second `POST /reviews` for the same contract
    resume the existing review rather than starting a rival one.
    """
    return f"contract-{contract_no}"


def thread_config(contract_no: str) -> dict:
    """The config every invoke and every state read has to be given."""
    return {"configurable": {"thread_id": thread_id_for(contract_no)}}
