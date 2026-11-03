"""Lesson 82: the tool surface is a contract. These tests pin it."""
from __future__ import annotations

import json

import pytest

from dayflow import tools
from dayflow.adapters.fixtures import FixtureTasks


def test_every_tool_is_strict_compatible():
    for d in tools.definitions():
        schema = d["input_schema"]
        assert d["strict"] is True
        assert schema["additionalProperties"] is False
        # strict mode: every declared property must be required
        assert set(schema["required"]) == set(schema["properties"]), d["name"]


def test_read_only_surface_has_no_writes():
    names = {d["name"] for d in tools.definitions(("read",))}
    assert "create_task" not in names
    assert {"read_inbox", "read_calendar", "read_tasks"} <= names


def test_calendar_range_is_an_enum_not_an_integer():
    cal = next(d for d in tools.definitions() if d["name"] == "read_calendar")
    assert cal["input_schema"]["properties"]["range"]["enum"] == ["today", "week"]


def test_success_envelope():
    env = json.loads(tools.dispatch("read_tasks", {"include_done": False}))
    assert env["ok"] is True
    assert isinstance(env["data"], list)


def test_bad_arguments_envelope():
    env = json.loads(tools.dispatch("read_tasks", {"nope": 1}))
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_arguments"
    assert env["error"]["retryable"] is False


def test_create_task_is_idempotent():
    t = FixtureTasks()
    a = t.create_task(title="Reply to Priya", due="2026-09-10", idempotency_key="k1")
    b = t.create_task(title="Reply to Priya", due="2026-09-10", idempotency_key="k1")
    assert a["created"] is True and b["created"] is False
    assert a["id"] == b["id"]
    assert len([x for x in t.list_tasks() if x["title"] == "Reply to Priya"]) == 1


def test_create_task_validates_due():
    t = FixtureTasks()
    with pytest.raises(tools.ToolError) as ei:
        t.create_task(title="x", due="10/09/2026", idempotency_key="k2")
    assert ei.value.code == "invalid_due" and ei.value.retryable is False


def test_tool_error_becomes_envelope_through_dispatch(monkeypatch):
    def bad(**_):
        raise tools.ToolError("quota", "calendar quota exceeded", retryable=True)
    monkeypatch.setattr(tools.REGISTRY["read_calendar"], "handler", bad)
    env = json.loads(tools.dispatch("read_calendar", {"range": "today"}))
    assert env == {"ok": False, "error": {"code": "quota", "message": "calendar quota exceeded", "retryable": True}}


def test_no_numeric_constraints_in_strict_schemas():
    # The API rejects minimum/maximum/pattern/minLength under strict mode.
    banned = {"minimum", "maximum", "pattern", "minLength", "maxLength", "exclusiveMinimum", "exclusiveMaximum"}
    for d in tools.definitions():
        for name, prop in d["input_schema"]["properties"].items():
            assert not (banned & set(prop)), f"{d['name']}.{name} uses an unsupported constraint"


def test_limit_is_clamped_in_handler():
    env = json.loads(tools.dispatch("read_inbox", {"limit": 999}))
    assert env["ok"] is True and len(env["data"]) <= 50
