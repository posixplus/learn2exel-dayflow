"""Fixture-backed adapters. Deterministic, free, and safe for red-team tests later.

Every adapter exposes the same methods a real adapter will (lesson 87),
so the agent never knows which one it is talking to.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _load(name: str) -> list[dict]:
    with open(FIXTURES / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


class FixtureInbox:
    def list_messages(self, limit: int = 20, offset: int = 0) -> dict:
        """One page, newest first. Shape: {messages, total, next_offset} (lesson 85)."""
        # Lesson 85: DAYFLOW_INBOX=large swaps in 120 messages (fixtures/gen_inbox_large.py).
        msgs = _load("inbox_large" if os.environ.get("DAYFLOW_INBOX") == "large" else "inbox")
        msgs = sorted(msgs, key=lambda m: m["date"], reverse=True)
        page = msgs[offset:offset + limit]
        nxt = offset + limit
        return {"messages": page, "total": len(msgs), "next_offset": nxt if nxt < len(msgs) else None}


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
    """Tasks live in memory for the run. Lesson 83 persists them.

    Writes are idempotent: the same key returns the same task and creates
    nothing. This is what lets lesson 91 retry a timed-out write safely.
    """
    def __init__(self) -> None:
        self._tasks: list[dict] = _load("tasks")
        self._by_key: dict[str, dict] = {}

    def list_tasks(self, include_done: bool = False) -> list[dict]:
        return [t for t in self._tasks if include_done or not t["done"]]

    def create_task(self, *, title: str, due: str | None, idempotency_key: str) -> dict:
        from dayflow.tools import ToolError  # local import avoids a cycle
        if not title.strip():
            raise ToolError("invalid_title", "title must not be empty")
        if due is not None and not (len(due) == 10 and due[4] == "-" and due[7] == "-"):
            raise ToolError("invalid_due", f"due must be YYYY-MM-DD, got {due!r}")
        if idempotency_key in self._by_key:
            return {**self._by_key[idempotency_key], "created": False}
        task = {"id": f"t{len(self._tasks) + 1}", "title": title.strip(), "due": due, "done": False}
        self._tasks.append(task)
        self._by_key[idempotency_key] = task
        return {**task, "created": True}
