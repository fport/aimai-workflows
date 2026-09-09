"""The two endpoints, driven end to end against an in-memory checkpointer.

`TestClient` runs the real lifespan, so these tests cover the same code the
service runs — including the part where the graph is built once and every
request reads its state back out of the checkpointer.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aimai_workflows.contract import RuleBasedReviewer, crm_notes
from aimai_workflows.contract.api import create_app
from aimai_workflows.contract.checkpointer import in_memory_checkpointer


@pytest.fixture
def client(registry, monkeypatch) -> TestClient:
    monkeypatch.chdir(registry.directory.parent)
    app = create_app(RuleBasedReviewer(), in_memory_checkpointer())
    with TestClient(app) as test_client:
        yield test_client


def test_a_risky_contract_comes_back_awaiting_approval(client) -> None:
    response = client.post(
        "/reviews", json={"contract_no": "API-1", "document_uri": "high_risk.txt"}
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "awaiting_approval"
    assert body["next_nodes"] == ["human_gate"]
    assert body["overall_risk"] == "critical"
    assert body["findings"], "the screen needs the findings to render"
    assert body["question"]
    assert crm_notes() == []


def test_a_low_risk_contract_completes_in_one_call(client) -> None:
    response = client.post(
        "/reviews", json={"contract_no": "API-2", "document_uri": "low_risk.txt"}
    )
    body = response.json()

    assert body["status"] == "completed"
    assert body["action_receipt"]
    assert len(crm_notes("API-2")) == 1


def test_approval_writes_exactly_one_note(client) -> None:
    client.post(
        "/reviews", json={"contract_no": "API-3", "document_uri": "high_risk.txt"}
    )
    response = client.post(
        "/reviews/API-3/decision", json={"decision": "approve", "note": "reviewed"}
    )
    body = response.json()

    assert body["status"] == "completed"
    assert body["decision"] == "approve"
    assert len(crm_notes("API-3")) == 1


def test_a_second_decision_is_refused_rather_than_re_run(client) -> None:
    """The double-clicked approve button.

    409 rather than 200: the caller asked for something that cannot happen, and
    answering "fine" would teach the screen that repeated approvals are normal.
    """
    client.post(
        "/reviews", json={"contract_no": "API-4", "document_uri": "high_risk.txt"}
    )
    client.post("/reviews/API-4/decision", json={"decision": "approve"})
    second = client.post("/reviews/API-4/decision", json={"decision": "approve"})

    assert second.status_code == 409
    assert "already finished" in second.json()["detail"]
    assert len(crm_notes("API-4")) == 1


def test_a_repeated_start_rejoins_the_paused_review(client) -> None:
    """A retried webhook must not fork the review."""
    first = client.post(
        "/reviews", json={"contract_no": "API-5", "document_uri": "high_risk.txt"}
    ).json()
    second = client.post(
        "/reviews", json={"contract_no": "API-5", "document_uri": "high_risk.txt"}
    ).json()

    assert first["status"] == second["status"] == "awaiting_approval"
    assert first["text_sha256"] == second["text_sha256"]
    assert crm_notes() == []


def test_deciding_an_unknown_contract_is_a_404(client) -> None:
    response = client.post("/reviews/NOPE/decision", json={"decision": "approve"})

    assert response.status_code == 404
    assert "no review for contract" in response.json()["detail"]


def test_reading_an_unknown_contract_is_a_404(client) -> None:
    assert client.get("/reviews/NOPE").status_code == 404


def test_an_invalid_decision_never_reaches_the_graph(client) -> None:
    """Validation at the edge, so the "unrecognised decision" path in the node
    is a second line of defence rather than the only one."""
    client.post(
        "/reviews", json={"contract_no": "API-6", "document_uri": "high_risk.txt"}
    )
    response = client.post("/reviews/API-6/decision", json={"decision": "maybe"})

    assert response.status_code == 422
    assert client.get("/reviews/API-6").json()["status"] == "awaiting_approval"


def test_corrections_posted_by_the_screen_are_applied(client) -> None:
    client.post(
        "/reviews", json={"contract_no": "API-7", "document_uri": "high_risk.txt"}
    )
    body = client.post(
        "/reviews/API-7/decision",
        json={
            "decision": "edit",
            "note": "cap agreed",
            "findings": [
                {
                    "clause_id": "5.1",
                    "category": "liability",
                    "severity": "medium",
                    "summary": "capped by side letter",
                    "quote": "The Customer's aggregate liability",
                }
            ],
        },
    ).json()

    corrected = [f for f in body["findings"] if f["clause_id"] == "5.1"]
    assert corrected[0]["severity"] == "medium"
    assert body["decision"] == "edit"


def test_the_approval_screen_is_served(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Contract review" in response.text
