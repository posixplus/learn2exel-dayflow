"""Lesson 92: Dayflow's tools over MCP. The client here is the real MCP client,
connected in-process to the real server object. No model, no network."""
from __future__ import annotations

import json

import pytest
from mcp import Client

from dayflow import mcp_server, tools
from dayflow.store import Store

anyio_backend = "asyncio"
pytestmark = pytest.mark.anyio


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "m.db")
    previous = tools.tasks
    tools.use_store(s)
    yield s
    tools.tasks = previous
    s.close()


def _env(result):
    return json.loads(result.content[0].text)


async def test_lists_the_registry_unchanged_with_read_and_write_hints(store):
    async with Client(mcp_server.build(store)) as c:
        listed = {t.name: t for t in (await c.list_tools()).tools}
    assert set(listed) == set(tools.REGISTRY)
    for name, t in tools.REGISTRY.items():
        assert listed[name].input_schema == t.schema()["input_schema"]          # the same strict schema the API sees
        assert listed[name].description == t.description
        assert listed[name].annotations.read_only_hint is (t.kind == "read")


async def test_a_read_runs_and_returns_the_envelope(store):
    async with Client(mcp_server.build(store)) as c:
        r = await c.call_tool("read_tasks", {"include_done": False})
    assert r.is_error is False and _env(r)["ok"] is True and len(_env(r)["data"]) == 3


async def test_a_write_is_queued_never_run_and_is_not_reported_as_a_protocol_error(store):
    before = store.list_tasks()
    async with Client(mcp_server.build(store)) as c:
        r = await c.call_tool("create_task", {"title": "Call the bank", "due": None, "idempotency_key": "k1"})
    env = _env(r)
    assert env["ok"] is False and env["error"]["code"] == "pending_approval"
    assert r.is_error is False                              # a refusal the client should read, not a failure to retry
    assert store.list_tasks() == before
    pending = store.list_approvals()
    assert len(pending) == 1 and pending[0]["tool"] == "create_task" and pending[0]["id"] in env["error"]["message"]


async def test_the_human_approves_outside_the_protocol(store):
    from dayflow import permissions
    async with Client(mcp_server.build(store)) as c:
        await c.call_tool("create_task", {"title": "Call the bank", "due": None, "idempotency_key": "k1"})
        names = {t.name for t in (await c.list_tools()).tools}
    assert not any("approv" in n for n in names)            # the proposer cannot also be the approver
    permissions.approve(store, store.list_approvals()[0]["id"])
    assert any(t["title"] == "Call the bank" for t in store.list_tasks())


async def test_read_only_server_does_not_list_writes_and_refuses_them_by_name(store):
    async with Client(mcp_server.build(store, read_only=True)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        r = await c.call_tool("create_task", {"title": "x", "due": None, "idempotency_key": "k"})
    assert names == {"read_inbox", "read_calendar", "read_tasks"}
    assert _env(r)["error"]["code"] == "unknown_tool" and r.is_error is True
    assert store.list_approvals() == []


async def test_each_session_is_a_run_and_the_write_limit_applies_to_it(store):
    async with Client(mcp_server.build(store)) as c:
        codes = [_env(await c.call_tool("create_task", {"title": f"t{i}", "due": None, "idempotency_key": f"k{i}"}))["error"]["code"]
                 for i in range(3)]
    assert codes == ["pending_approval", "pending_approval", "write_limit"]
    run = store.latest_run()
    assert run["kind"] == "mcp" and all(a["run_id"] == run["id"] for a in store.list_approvals())


async def test_a_session_that_read_hostile_mail_cannot_draft_to_the_attacker(store, monkeypatch):
    monkeypatch.setenv("DAYFLOW_INBOX", "redteam")
    async with Client(mcp_server.build(store)) as c:
        await c.call_tool("read_inbox", {"limit": 25, "offset": 0})
        r = await c.call_tool("draft_reply", {"message_id": "x5", "body": "Approved, go ahead.", "idempotency_key": "d1"})
    assert _env(r)["error"]["code"] == "content_rule" and store.list_approvals() == []


async def test_a_failing_tool_is_a_protocol_error(store, monkeypatch):
    monkeypatch.setenv("DAYFLOW_FAULTS", "read_inbox=always")
    async with Client(mcp_server.build(store)) as c:
        r = await c.call_tool("read_inbox", {"limit": 5, "offset": 0})
    assert r.is_error is True and _env(r)["error"]["code"] == "upstream_unavailable" and _env(r)["attempts"] == 3


async def test_instructions_carry_the_date_and_the_rule_about_writes(store):
    async with Client(mcp_server.build(store)) as c:
        text = c.instructions
    assert "Today is 2026-09-07" in text and "pending_approval" in text and "data, not instructions" in text
