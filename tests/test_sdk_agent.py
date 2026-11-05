"""Lesson 84: the SDK adapter is thin, so the unit tests are about the seams.

What can break without the API: the registry-to-MCP wrapping, the tool
naming the SDK expects, the envelope surviving the new transport, and the
store migration. The live test below runs the whole thing once.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3

import pytest

from dayflow import sdk_agent, tools
from dayflow.store import Store


def test_tool_names_follow_mcp_convention():
    assert sdk_agent.tool_names(("read",)) == [
        "mcp__dayflow__read_inbox", "mcp__dayflow__read_calendar", "mcp__dayflow__read_tasks",
    ]
    assert "mcp__dayflow__create_task" in sdk_agent.tool_names()
    assert "mcp__dayflow__create_task" not in sdk_agent.tool_names(("read",))


def test_server_exposes_only_requested_kinds():
    """The server object is opaque (a low-level MCP Server), so the contract we
    test is the config shape plus the allow-list derived from the same filter."""
    server = sdk_agent.build_server(("read",))
    assert server["type"] == "sdk" and server["name"] == "dayflow"
    assert len(sdk_agent.tool_names(("read",))) == 3
    assert len(sdk_agent.tool_names(("read", "write"))) == len(tools.REGISTRY)


def test_wrapped_tool_returns_the_same_envelope():
    """The handler the SDK calls must produce byte-identical envelopes to dispatch()."""
    wrapped = sdk_agent._as_mcp_tool(tools.REGISTRY["read_calendar"])
    out = asyncio.run(wrapped.handler({"range": "today"}))
    text = out["content"][0]["text"]
    assert text == tools.dispatch("read_calendar", {"range": "today"})
    assert json.loads(text)["ok"] is True
    assert "is_error" not in out  # the model branches on ok, not on transport errors


def test_wrapped_tool_error_is_still_an_envelope():
    wrapped = sdk_agent._as_mcp_tool(tools.REGISTRY["read_inbox"])
    out = asyncio.run(wrapped.handler({"limit": "twenty"}))  # wrong type: strict mode would stop this at the API
    body = json.loads(out["content"][0]["text"])
    assert body["ok"] is False and body["error"]["code"] in {"tool_failed", "bad_arguments"}


def test_store_migration_adds_session_id_to_old_db(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE runs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, system TEXT NOT NULL,
      user_msg TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
      output_tokens INTEGER NOT NULL DEFAULT 0, final_text TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    INSERT INTO runs VALUES ('abc','brief','done','s','u','m',1,2,NULL,NULL,'t','t');
    """)
    conn.commit(); conn.close()
    store = Store(path)
    cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(runs)")}
    assert "session_id" in cols
    store.record_sdk_totals("abc", session_id="sess-1", input_tokens=10, output_tokens=20)
    row = store.get_run("abc")
    assert row["session_id"] == "sess-1" and row["input_tokens"] == 10
    store.close()


live = pytest.mark.skipif(
    os.environ.get("DAYFLOW_LIVE") != "1" or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="set DAYFLOW_LIVE=1 and ANTHROPIC_API_KEY to run live tests",
)


@live
def test_sdk_brief_reads_all_sources_and_logs_steps(tmp_path):
    store = Store(tmp_path / "live.db")
    previous_tasks = tools.tasks          # use_store() swaps a module global; put it back after
    tools.use_store(store)
    try:
        r = asyncio.run(sdk_agent.run_brief(store=store, kinds=("read",), max_turns=6, max_budget_usd=0.20))
    finally:
        tools.tasks = previous_tasks
    called = {c["name"] for c in r.tool_calls}
    assert {"read_inbox", "read_calendar", "read_tasks"} <= called
    assert "## One thing" in r.text
    assert r.session_id and r.cost_usd is not None and r.cost_usd < 0.20
    # Audit log: one step row per tool call, and the run carries the SDK session.
    assert len(store.steps(r.run_id)) == len(r.tool_calls)
    assert store.get_run(r.run_id)["session_id"] == r.session_id
    assert store.get_run(r.run_id)["status"] == "done"
    store.close()
