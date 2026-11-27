"""Diagram box: API gateway + identity (lane A), the HTTP entry point (src/copilot/app.py).
Proves the FastAPI routes: the sign-in helper, /ask and /approve require a bearer token (401 without one),
answers carry a trace that /trace/{id} returns, and the follow-up approval works over HTTP.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import copilot.app as app_module
from copilot.operations.flags import Flags
from copilot.wiring import build_copilot
from helpers import FEE_QUESTION, FOLLOW_UP_NOTES, GULF_STAR


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """A fresh copilot behind the app for each test, so rate limits and nonces never carry over."""
    monkeypatch.setattr(app_module, "copilot", build_copilot(flags=Flags()))
    return TestClient(app_module.app)


def bearer(client: TestClient, user: str = "rm-1") -> dict[str, str]:
    token = client.post("/dev/token", json={"user_id": user}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_dev_token_signs_in_known_users_only(client):
    assert client.post("/dev/token", json={"user_id": "rm-1"}).json()["token"].count(".") == 2
    assert client.post("/dev/token", json={"user_id": "nobody"}).status_code == 404


def test_ask_and_approve_return_401_without_a_bearer_token(client):
    for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}):
        assert client.post("/ask", json={"text": FEE_QUESTION}, headers=headers).status_code == 401
    assert client.post("/approve", json={"approval_token": "x"}).status_code == 401
    invalid = client.post("/ask", json={"text": FEE_QUESTION}, headers={"Authorization": "Bearer a.b.c"})
    assert invalid.json()["status"] == "denied"


def test_ask_returns_a_cited_answer_whose_trace_is_served_by_id(client):
    answer = client.post("/ask", json={"text": FEE_QUESTION}, headers=bearer(client)).json()
    assert answer["status"] == "answered" and answer["citations"][0]["clause"] == "1.3"
    trace = client.get(f"/trace/{answer['trace']['trace_id']}").json()
    assert trace["request_id"] == answer["request_id"]
    assert trace["spans"] and all(s["lane"] and s["component"] for s in trace["spans"])
    assert client.get("/trace/tr-unknown").status_code == 404


def test_follow_up_approve_and_replay_over_http(client):
    headers = bearer(client)
    drafted = client.post("/ask", json={"action": "draft_follow_up", "customer_id": GULF_STAR,
                                        "text": FOLLOW_UP_NOTES}, headers=headers).json()
    assert drafted["status"] == "awaiting_approval"
    approval = {"approval_token": drafted["approval_token"]}
    assert client.post("/approve", json=approval, headers=bearer(client, "rm-2")).json()["status"] == "rejected"
    assert client.post("/approve", json=approval, headers=headers).json()["status"] == "committed"
    assert client.post("/approve", json=approval, headers=headers).json()["status"] == "rejected"


def test_unknown_action_is_rejected_by_the_contract_with_422(client):
    response = client.post("/ask", json={"action": "commit_to_crm", "customer_id": GULF_STAR},
                           headers=bearer(client))
    assert response.status_code == 422
