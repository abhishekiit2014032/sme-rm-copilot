"""Shared fixtures: a fresh copilot per test (it builds in a few milliseconds), so no state leaks between tests.
Helpers and fictional ids live in helpers.py.
"""
from __future__ import annotations

import pytest

from copilot.operations.flags import Flags
from copilot.wiring import Copilot, build_copilot
from helpers import SpyLlm


@pytest.fixture
def copilot() -> Copilot:
    """The whole system with default flags (semantic cache off), independent of COPILOT_FLAG_* variables."""
    return build_copilot(flags=Flags())


@pytest.fixture
def cached_copilot() -> Copilot:
    return build_copilot(flags=Flags(semantic_cache=True))


@pytest.fixture
def spy_llm() -> SpyLlm:
    return SpyLlm()


@pytest.fixture
def spied_copilot(spy_llm: SpyLlm) -> Copilot:
    return build_copilot(flags=Flags(), llm=spy_llm)
