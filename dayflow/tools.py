"""Tool definitions and dispatch.

Lesson 81 ships read-only tools. Lesson 82 refactors these into a proper
API surface; lesson 86 adds the write tools behind approval gates.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from dayflow.adapters.fixtures import FixtureCalendar, FixtureInbox, FixtureTasks

# One place to swap adapters later (lesson 87).
inbox = FixtureInbox()
calendar = FixtureCalendar()
tasks = FixtureTasks()

TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_inbox",
        "description": "List recent inbox messages, newest first. Returns id, from, subject, date, body.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
        },
    },
    {
        "name": "read_calendar",
        "description": "List calendar events for the next N days, starting today. Returns id, title, start, end, attendees.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 14, "default": 1},
            },
        },
    },
    {
        "name": "read_tasks",
        "description": "List open tasks with due dates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "include_done": {"type": "boolean", "default": False},
            },
        },
    },
]

_HANDLERS: dict[str, Callable[..., Any]] = {
    "read_inbox": lambda limit=20: inbox.list_messages(limit=limit),
    "read_calendar": lambda days=1: calendar.list_events(days=days),
    "read_tasks": lambda include_done=False: tasks.list_tasks(include_done=include_done),
}


def dispatch(name: str, args: dict[str, Any]) -> str:
    """Run a tool and return its result as a JSON string.

    Results are data, not prose. The model reads JSON far more reliably than
    a paragraph describing the JSON.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        return json.dumps(handler(**args))
    except TypeError as e:
        # Bad arguments. Tell the model so it can correct itself.
        return json.dumps({"error": f"bad arguments for {name}: {e}"})
