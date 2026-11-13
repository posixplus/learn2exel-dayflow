"""Fault injection for the fixture adapters. Lesson 91. Off unless DAYFLOW_FAULTS is set.

    DAYFLOW_FAULTS="read_inbox=2"            the first 2 calls fail with a retryable error
    DAYFLOW_FAULTS="read_inbox=always"       every call fails with a retryable error (an outage)
    DAYFLOW_FAULTS="read_calendar=auth"      every call fails with a NON-retryable error
    DAYFLOW_FAULTS="read_inbox=2,read_tasks=always"

You cannot wait for Gmail to have a bad day to find out what your agent does on one.
"""
from __future__ import annotations

import os

_calls: dict[str, int] = {}


def reset() -> None:
    _calls.clear()


def _plan() -> dict[str, str]:
    spec = os.environ.get("DAYFLOW_FAULTS", "")
    return dict(part.split("=", 1) for part in spec.split(",") if "=" in part)


def maybe_fail(tool: str) -> None:
    """Called by dispatch() before the handler. Raises ToolError when the plan says so."""
    mode = _plan().get(tool)
    if mode is None:
        return
    from dayflow.tools import ToolError
    _calls[tool] = _calls.get(tool, 0) + 1
    if mode == "auth":
        raise ToolError("auth", f"{tool}: injected fault: access was refused (401)", retryable=False)
    if mode == "always" or (mode.isdigit() and _calls[tool] <= int(mode)):
        raise ToolError("upstream_unavailable", f"{tool}: injected fault: upstream returned 503", retryable=True)
