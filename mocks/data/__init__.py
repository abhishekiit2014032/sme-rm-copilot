"""Diagram box: Systems of record and the policy library (lanes B and C), the fictional data.
Loads customers, product rules and policy documents, and fixes the mock world's business date.
Everything here is fictional; no real bank, customer or person.
"""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent
POLICIES_DIR = DATA_DIR / "policies"

# The mock world's business date: system as-of times, meetings and policy dates are set around it,
# so demos and tests give the same result on any day.
BUSINESS_DATE = date(2026, 11, 27)


@lru_cache(maxsize=1)
def _load(name: str) -> str:
    return (DATA_DIR / name).read_text(encoding="utf-8")


def load_json(name: str) -> dict[str, Any]:
    """Return a fresh copy each time so tests and scenarios never share mutable state."""
    return json.loads(_load(name))
