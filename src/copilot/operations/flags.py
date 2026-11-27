"""Diagram box: Release + recovery (lane D), feature flags and kill switches.
Flags switch features on or off without a deploy; every check is recorded in the trace.
Mocked: flags come from defaults and COPILOT_FLAG_* environment variables instead of a config service.
"""
from __future__ import annotations

import os

from pydantic import BaseModel

from copilot.contracts.trace import Component, Trace


class Flags(BaseModel):
    copilot_enabled: bool = True     # global kill switch
    planner_path: bool = True        # kill switch for the LLM planner (fast path still works)
    crm_writes: bool = True          # kill switch for committing approved follow-ups
    semantic_cache: bool = False     # off in the pilot, as in the approved design

    @classmethod
    def from_env(cls) -> "Flags":
        values = {}
        for name in cls.model_fields:
            raw = os.environ.get(f"COPILOT_FLAG_{name.upper()}")
            if raw is not None:
                values[name] = raw.strip().lower() in {"1", "true", "on", "yes"}
        return cls(**values)

    def check(self, name: str, trace: Trace) -> bool:
        enabled = bool(getattr(self, name))
        trace.event(Component.RELEASE, f"flag {name}", detail="on" if enabled else "off",
                    status="ok" if enabled else "off")
        return enabled
