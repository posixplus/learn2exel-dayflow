"""Fixture-backed adapters. Deterministic, free, and safe for red-team tests later.

Every adapter exposes the same methods a real adapter will (lesson 87),
so the agent never knows which one it is talking to.
"""
from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _load(name: str) -> list[dict]:
    with open(FIXTURES / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


class FixtureInbox:
    def list_messages(self, limit: int = 20, unread_only: bool = True) -> list[dict]:
        msgs = _load("inbox")
        return sorted(msgs, key=lambda m: m["date"], reverse=True)[:limit]


class FixtureCalendar:
    def list_events(self, days: int = 1) -> list[dict]:
        # Fixtures are a static week; `days` is honoured relative to the first event.
        events = sorted(_load("calendar"), key=lambda e: e["start"])
        if not events:
            return []
        start_day = events[0]["start"][:10]
        keep = []
        for e in events:
            day_offset = (int(e["start"][8:10]) - int(start_day[8:10]))
            if day_offset < days:
                keep.append(e)
        return keep


class FixtureTasks:
    def list_tasks(self, include_done: bool = False) -> list[dict]:
        tasks = _load("tasks")
        return [t for t in tasks if include_done or not t["done"]]
