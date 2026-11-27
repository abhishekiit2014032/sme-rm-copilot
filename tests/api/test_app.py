"""Diagram box: API gateway + identity (lane A), the HTTP entry point (src/copilot/app.py).
Proves the FastAPI routes: sign-in helper, /ask and /approve require a bearer token, answers carry a trace that
/trace/{id} returns, and the demo scenario routes work for the UI.
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


def test_dev_token_signs_in_a_known_user_and_rejects_an_unknown_one(client):
    assert client.post("/dev/token", json={"user_id": "rm-1"}).json()["token"].count(".") == 2
    assert client.post("/dev/token", json={"user_id": "nobody"}).status_code == 404


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}])
def test_ask_without_a_bearer_token_is_401(client, headers):
    assert client.post("/ask", json={"text": FEE_QUESTION}, headers=headers).status_code == 401


def test_approve_without_a_bearer_token_is_401(client):
    assert client.post("/approve", json={"approval_token": "x"}).status_code == 401


def test_ask_with_an_invalid_token_is_denied(client):
    body = client.post("/ask", json={"text": FEE_QUESTION}, headers={"Authorization": "Bearer a.b.c"}).json()
    assert body["status"] == "denied"


def test_ask_returns_a_cited_answer_with_its_trace(client):
    body = client.post("/ask", json={"text": FEE_QUESTION}, headers=bearer(client)).json()
    assert body["status"] == "answered" and body["citations"][0]["clause"] == "1.3"
    assert body["trace"]["spans"] and all(s["lane"] and s["component"] for s in body["trace"]["spans"])


def test_trace_endpoint_returns_the_trace_of_an_answer_and_404_for_unknown(client):
    answer = client.post("/ask", json={"action": "prepare_brief", "customer_id": GULF_STAR},
                         headers=bearer(client)).json()
    trace = client.get(f"/trace/{answer['trace']['trace_id']}").json()
    assert trace["request_id"] == answer["request_id"] and len(trace["spans"]) == len(answer["trace"]["spans"])
    assert client.get("/trace/tr-unknown").status_code == 404


def test_ask_rejects_an_unknown_action_with_422(client):
    response = client.post("/ask", json={"action": "commit_to_crm", "customer_id": GULF_STAR},
                           headers=bearer(client))
    assert response.status_code == 422


def test_follow_up_approve_and_replay_over_http(client):
    headers = bearer(client)
    drafted = client.post("/ask", json={"action": "draft_follow_up", "customer_id": GULF_STAR,
                                        "text": FOLLOW_UP_NOTES}, headers=headers).json()
    assert drafted["status"] == "awaiting_approval"
    approval = {"approval_token": drafted["approval_token"]}
    assert client.post("/approve", json=approval, headers=headers).json()["status"] == "committed"
    assert client.post("/approve", json=approval, headers=headers).json()["status"] == "rejected"


def test_other_rms_customer_is_denied_over_http(client):
    body = client.post("/ask", json={"action": "prepare_brief", "customer_id": GULF_STAR},
                       headers=bearer(client, "rm-2")).json()
    assert body["status"] == "denied"


def test_demo_scenario_routes_list_and_run_scenarios(client):
    listed = client.get("/demo/scenarios").json()
    assert len(listed) == 8
    run = client.post("/demo/scenarios/injection").json()
    assert run["steps"][0]["answer"]["status"] == "blocked"
    assert client.post("/demo/scenarios/nope").status_code == 404
