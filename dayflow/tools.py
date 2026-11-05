"""Tool definitions and dispatch. Lesson 82: tools are an API surface.

Three rules, each enforced here rather than hoped for in the prompt:

1. Schemas are strict. Every object has additionalProperties=False, every
   property is required (optional values use a null type union), and the API
   is told strict=True so it guarantees the arguments match.
2. Results follow one envelope: {"ok": true, "data": ...} or
   {"ok": false, "error": {"code", "message", "retryable"}}. The model can
   branch on `ok` and `retryable` instead of parsing prose.
3. Tools declare a kind: READ or WRITE. Writes carry an idempotency key so a
   retried call cannot act twice. Lesson 86 gates WRITE tools behind approval;
   the split is made now so that gate has something to hold on to.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from dayflow.adapters.fixtures import FixtureCalendar, FixtureInbox, FixtureTasks, _load

Kind = Literal["read", "write"]


class ToolError(Exception):
    """Raised by a handler to return a structured error to the model."""
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass
class Tool:
    name: str
    description: str
    kind: Kind
    properties: dict[str, Any]
    handler: Callable[..., Any]
    # Lesson 82 keeps the registry flat; lesson 92 exposes it over MCP unchanged.

    def schema(self) -> dict[str, Any]:
        """The definition the API sees. Strict mode needs every property required."""
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": self.properties,
                "required": list(self.properties),
                "additionalProperties": False,
            },
        }


# One place to swap adapters later (lesson 87).
inbox = FixtureInbox()
calendar = FixtureCalendar()
# Tasks: in-memory by default (tests), or the SQLite store once use_store() is called.
tasks: Any = FixtureTasks()


def use_store(store: Any) -> None:
    """Point the task tools at a persistent store (lesson 83). Seeds fixtures once."""
    global tasks
    store.seed_tasks(_load("tasks"))
    tasks = store

# ── Read tools ───────────────────────────────────────────────────────────────
READ_INBOX = Tool(
    name="read_inbox",
    kind="read",
    description=(
        "List recent inbox messages, newest first. Returns id, from, subject, date, body. "
        "Promotions and automated reminders are included; the caller decides what matters."
    ),
    properties={
        # Strict mode does not accept minimum/maximum on numbers. State the
        # bound in the description and enforce it in the handler.
        "limit": {"type": "integer",
                  "description": "How many messages to return, 1 to 50. 20 is enough for a daily brief."},
    },
    handler=lambda limit: inbox.list_messages(limit=max(1, min(50, limit))),
)

READ_CALENDAR = Tool(
    name="read_calendar",
    kind="read",
    description="List calendar events. Use 'today' for the day's schedule, 'week' for deadlines and planning.",
    properties={
        # An enum beats an integer here: the model does not have to guess how
        # many days a 'week' is, and the schema cannot be called with days=400.
        "range": {"type": "string", "enum": ["today", "week"]},
    },
    handler=lambda range: calendar.list_events(days=1 if range == "today" else 7),
)

READ_TASKS = Tool(
    name="read_tasks",
    kind="read",
    description="List tasks with due dates. Completed tasks are excluded unless include_done is true.",
    properties={
        "include_done": {"type": "boolean"},
    },
    handler=lambda include_done: tasks.list_tasks(include_done=include_done),
)

# ── Write tools ──────────────────────────────────────────────────────────────
CREATE_TASK = Tool(
    name="create_task",
    kind="write",
    description=(
        "Create a task. Only call this when the brief identifies a concrete action the user "
        "has not already tracked. Use the same idempotency_key if you retry."
    ),
    properties={
        "title": {"type": "string", "description": "Imperative, under 80 characters"},
        "due": {"type": ["string", "null"], "description": "YYYY-MM-DD, or null if no date"},
        "idempotency_key": {"type": "string",
                            "description": "Stable key for this intent, e.g. 'reply-priya-vendor-2026-09-07'"},
    },
    handler=lambda title, due, idempotency_key: tasks.create_task(title=title, due=due, idempotency_key=idempotency_key),
)

REGISTRY: dict[str, Tool] = {t.name: t for t in (READ_INBOX, READ_CALENDAR, READ_TASKS, CREATE_TASK)}


def definitions(kinds: tuple[Kind, ...] = ("read", "write")) -> list[dict[str, Any]]:
    """Schemas for the API. Pass kinds=('read',) to hand the model a read-only surface."""
    return [t.schema() for t in REGISTRY.values() if t.kind in kinds]


# Kept for lesson-81 callers; lesson 82 code should use definitions().
TOOLS = definitions()


# ── Dispatch and the result envelope ─────────────────────────────────────────
def _ok(data: Any) -> str:
    return json.dumps({"ok": True, "data": data})


def _err(code: str, message: str, *, retryable: bool) -> str:
    return json.dumps({"ok": False, "error": {"code": code, "message": message, "retryable": retryable}})


def dispatch(name: str, args: dict[str, Any]) -> str:
    """Run a tool and return a JSON envelope. Never raises: a failing tool is
    information the model needs, not a reason to kill the run."""
    tool = REGISTRY.get(name)
    if tool is None:
        # Strict mode makes this unreachable from the API; keep it for direct callers.
        return _err("unknown_tool", f"no tool named {name!r}", retryable=False)
    try:
        return _ok(tool.handler(**args))
    except TypeError as e:
        return _err("bad_arguments", str(e), retryable=False)
    except ToolError as e:
        return _err(e.code, e.message, retryable=e.retryable)
    except Exception as e:  # noqa: BLE001 - lesson 91 adds retries on retryable=True
        return _err("tool_failed", f"{type(e).__name__}: {e}", retryable=True)
