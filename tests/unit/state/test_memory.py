"""Diagram box: Session + memory store (lane C).
Proves layered memory: the checkpointer saves and resumes per-RM state, session memory keeps recent turns for
24 h, episodic memory is RM-scoped with 90-day retention, and procedural memory versions every prompt.
"""
from __future__ import annotations

from datetime import date

from copilot.contracts import Trace
from copilot.state.memory.checkpointer import Checkpointer
from copilot.state.memory.episodic import EpisodicMemory
from copilot.state.memory.session import MAX_TURNS, SESSION_TTL_S, SessionMemory
from helpers import GULF_STAR, ask, draft_follow_up
from mocks.data import BUSINESS_DATE, load_json


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def test_checkpoint_resumes_only_for_its_owner_and_only_once():
    checkpoints = Checkpointer()
    checkpoints.save("thread-1", "await_approval", "rm-1", {"drafts": ["d1"]}, Trace())
    assert checkpoints.load("thread-1", "rm-2", Trace()) is None
    assert checkpoints.load("thread-1", "rm-1", Trace()).state == {"drafts": ["d1"]}
    checkpoints.complete("thread-1")
    assert checkpoints.load("thread-1", "rm-1", Trace()) is None


def test_session_keeps_the_last_turns_per_rm_for_24_hours():
    clock = Clock()
    session = SessionMemory(clock=clock)
    for i in range(MAX_TURNS + 3):
        session.add_turn("rm-1", f"turn {i}", "answered", Trace())
    turns = session.recent("rm-1")
    assert len(turns) == MAX_TURNS and turns[-1].text == f"turn {MAX_TURNS + 2}" and session.recent("rm-2") == []
    clock.now = SESSION_TTL_S + 1
    assert session.recent("rm-1") == []


def test_episodic_memory_is_rm_scoped_and_expires_after_90_days():
    dataset = load_json("customers.json")
    memory = EpisodicMemory(dataset, BUSINESS_DATE)
    assert len(memory.recall(GULF_STAR, "rm-1")) == 1 and memory.recall(GULF_STAR, "rm-2") == []
    assert EpisodicMemory(dataset, date(2027, 3, 1)).recall(GULF_STAR, "rm-1") == []


def test_approved_note_is_remembered_and_every_model_call_names_its_prompt_version(copilot):
    drafted = draft_follow_up(copilot)
    assert [s.attrs["prompt_version"] for s in drafted.trace.spans if "prompt_version" in s.attrs] == \
        ["followup.extract@1.2.0"]
    copilot.pipeline.approve(copilot.token_for("rm-1"), drafted.approval_token)
    brief = ask(copilot, "rm-1", action="prepare_brief", customer_id=GULF_STAR)
    assert "audited financials" in brief.text.split("### Last approved meeting summary")[1]
