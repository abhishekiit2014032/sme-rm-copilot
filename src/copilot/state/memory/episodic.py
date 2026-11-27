"""Diagram box: Session + memory store (lane C), episodic memory.
Holds RM-approved meeting summaries per customer for 90 days; only approved text is ever stored.
Mocked: seeded from customers.json; production uses the per-RM, encrypted document store.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel

RETENTION = timedelta(days=90)


class Episode(BaseModel):
    customer_id: str
    rm_id: str
    date: str
    summary: str


class EpisodicMemory:
    def __init__(self, dataset: dict[str, Any], today: date) -> None:
        self._today = today
        self._episodes = [
            Episode(customer_id=c["customer_id"], rm_id=c["rm_id"], **e)
            for c in dataset["customers"] for e in c.get("episodic_memory", [])
        ]

    def add_approved(self, customer_id: str, rm_id: str, summary: str) -> Episode:
        episode = Episode(customer_id=customer_id, rm_id=rm_id, date=self._today.isoformat(), summary=summary)
        self._episodes.append(episode)
        return episode

    def recall(self, customer_id: str, rm_id: str) -> list[Episode]:
        """Episodes for this customer written by this RM, newest first, within the retention period."""
        cutoff = self._today - RETENTION
        found = [e for e in self._episodes if e.customer_id == customer_id and e.rm_id == rm_id
                 and date.fromisoformat(e.date) >= cutoff]
        return sorted(found, key=lambda e: e.date, reverse=True)
