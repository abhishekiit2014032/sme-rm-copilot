"""Diagram box: API gateway + identity (lane A), the HTTP entry point.
FastAPI app: POST /ask, POST /approve, GET /trace/{id}; serves the demo UI and runs recorded demo scenarios.
Mocked: sign-in uses the mock identity provider (POST /dev/token is a development-only helper).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from copilot.contracts import Answer, AskRequest, Trace
from copilot.wiring import build_copilot
from demo.scenarios import SCENARIOS, ScenarioRun, run_scenario

UI_FILE = Path(__file__).resolve().parents[2] / "demo" / "ui" / "index.html"

app = FastAPI(title="SME RM Copilot (prototype)", version="0.1.0")
copilot = build_copilot()


class ApproveBody(BaseModel):
    approval_token: str


class TokenBody(BaseModel):
    user_id: str


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization.split(" ", 1)[1]


@app.post("/ask", response_model=Answer)
def ask(body: AskRequest, authorization: str | None = Header(default=None)) -> Answer:
    return copilot.pipeline.ask(_bearer(authorization), body)


@app.post("/approve", response_model=Answer)
def approve(body: ApproveBody, authorization: str | None = Header(default=None)) -> Answer:
    return copilot.pipeline.approve(_bearer(authorization), body.approval_token)


@app.get("/trace/{trace_id}", response_model=Trace)
def trace(trace_id: str) -> Trace:
    found = copilot.tracer.get(trace_id)
    if found is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return found


@app.post("/dev/token")
def dev_token(body: TokenBody) -> dict[str, str]:
    """Development only: sign in a fictional user (rm-1, rm-2) at the mock identity provider."""
    try:
        return {"token": copilot.token_for(body.user_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown user") from exc


@app.get("/demo/scenarios")
def list_scenarios() -> list[dict[str, str]]:
    return [{"id": s.id, "title": s.title, "summary": s.summary} for s in SCENARIOS]


@app.post("/demo/scenarios/{scenario_id}", response_model=ScenarioRun)
def run_demo_scenario(scenario_id: str) -> ScenarioRun:
    if scenario_id not in {s.id for s in SCENARIOS}:
        raise HTTPException(status_code=404, detail="unknown scenario")
    return run_scenario(scenario_id)


@app.get("/")
def ui() -> FileResponse:
    return FileResponse(UI_FILE)
