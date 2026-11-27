"""Diagram box: Session + memory store (lane C).
Proves layered memory: the checkpointer saves and resumes per-RM state, session memory keeps recent masked
turns for 24 h, episodic memory is RM-scoped with 90-day retention, and procedural memory versions prompts.
"""
from __future__ import annotations

from datetime import date

from copilot.contracts import Trace
from copilot.state.memory.checkpointer import Checkpointer
from copilot.state.memory.episodic import EpisodicMemory
from copilot.state.memory.procedural import PromptRegistry
from copilot.state.memory.session import MAX_TURNS, SESSION_TTL_S, SessionMemory
from helpers import GULF_STAR, ask, draft_follow_up
from mocks.data import BUSINESS_DATE, load_json


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


# ---- checkpointer ---------------------------------------------------------------------------------
def test_checkpoint_saved_at_pause_can_be_resumed_by_its_owner():
    checkpoints = Checkpointer()
    checkpoints.save("thread-1", "await_approval", "rm-1", {"drafts": ["d1"]}, Trace())
    loaded = checkpoints.load("thread-1", "rm-1", Trace())
    assert loaded.node == "await_approval" and loaded.state == {"drafts": ["d1"]}


def test_checkpoint_is_invisible_to_another_rm():
    checkpoints = Checkpointer()
    checkpoints.save("thread-1", "await_approval", "rm-1", {}, Trace())
    assert checkpoints.load("thread-1", "rm-2", Trace()) is None


def test_completed_checkpoint_cannot_be_resumed_again():
    checkpoints = Checkpointer()
    checkpoints.save("thread-1", "await_approval", "rm-1", {}, Trace())
    checkpoints.complete("thread-1")
    assert checkpoints.load("thread-1", "rm-1", Trace()) is None


def test_follow_up_pauses_at_a_checkpoint_in_the_trace(copilot):
    names = [s.name for s in draft_follow_up(copilot).trace.spans]
    assert "checkpoint saved: await_approval" in names


# ---- session ---------------------------------------------------------------------------------------
def test_session_keeps_recent_turns_per_rm_and_expires_after_24_hours():
    clock = Clock()
    session = SessionMemory(clock=clock)
    session.add_turn("rm-1", "first", "answered", Trace())
    assert [t.text for t in session.recent("rm-1")] == ["first"] and session.recent("rm-2") == []
    clock.now = SESSION_TTL_S + 1
    assert session.recent("rm-1") == []


def test_session_keeps_only_the_last_turns():
    session = SessionMemory()
    for i in range(MAX_TURNS + 3):
        session.add_turn("rm-1", f"turn {i}", "answered", Trace())
    turns = session.recent("rm-1")
    assert len(turns) == MAX_TURNS and turns[-1].text == f"turn {MAX_TURNS + 2}"


# ---- episodic --------------------------------------------------------------------------------------
def test_episodic_memory_is_scoped_to_the_rm_who_approved_it():
    memory = EpisodicMemory(load_json("customers.json"), BUSINESS_DATE)
    assert len(memory.recall(GULF_STAR, "rm-1")) == 1
    assert memory.recall(GULF_STAR, "rm-2") == []


def test_episodes_older_than_90_days_are_not_recalled():
    memory = EpisodicMemory(load_json("customers.json"), date(2027, 3, 1))
    assert memory.recall(GULF_STAR, "rm-1") == []


def test_approved_note_becomes_an_episode_after_commit(copilot):
    token = draft_follow_up(copilot).approval_token
    copilot.pipeline.approve(copilot.token_for("rm-1"), token)
    brief = ask(copilot, "rm-1", action="prepare_brief", customer_id=GULF_STAR)
    assert "audited financials" in brief.text.split("### Last approved meeting summary")[1]


# ---- procedural ------------------------------------------------------------------------------------
def test_every_prompt_is_versioned_and_renders_its_variables():
    registry = PromptRegistry()
    rendered, template = registry.render("followup.extract", {"notes": "send financials", "today": "2026-11-27"})
    assert template.version == "1.2.0" and rendered.version == "1.2.0"
    assert "send financials" in rendered.user and "2026-11-27" in rendered.user


def test_model_spans_record_the_prompt_version_used(copilot):
    trace = draft_follow_up(copilot).trace
    versions = [s.attrs["prompt_version"] for s in trace.spans if "prompt_version" in s.attrs]
    assert versions == ["followup.extract@1.2.0"]
