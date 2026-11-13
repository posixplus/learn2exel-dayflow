"""Lesson 83: steps are durable, runs resume, tasks persist. Fake client, no network."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dayflow import loop
from dayflow.store import Store
from tests.test_loop import FakeClient, _resp, _text, _tool


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_each_step_is_committed_before_the_next_call(store):
    client = FakeClient([
        _resp("tool_use", [_tool("read_tasks", {"include_done": False}, "tu_1")]),
        _resp("tool_use", [_tool("read_calendar", {"range": "today"}, "tu_2")]),
        _resp("end_turn", [_text("brief")]),
    ])
    r = loop.run("sys", "go", client=client, store=store)
    steps = store.steps(r.run_id)
    assert [s.n for s in steps] == [1, 2, 3]
    assert steps[0].tool_results[0]["tool_use_id"] == "tu_1"
    assert steps[2].stop_reason == "end_turn" and steps[2].tool_results is None
    run = store.get_run(r.run_id)
    assert run["status"] == "done" and run["final_text"] == "brief"
    assert run["input_tokens"] == 300 and run["output_tokens"] == 150


def test_rebuilt_messages_match_what_the_loop_sent(store):
    client = FakeClient([
        _resp("tool_use", [_tool("read_tasks", {"include_done": False}, "tu_1")]),
        _resp("end_turn", [_text("ok")]),
    ])
    r = loop.run("sys", "go", client=client, store=store)
    rebuilt = store.rebuild_messages(r.run_id)
    # The second API call saw exactly: user, assistant(tool_use), user(tool_result)
    sent = client.calls[1]["messages"]
    # Lesson 90: the copy sent to the API carries a cache marker on its last block.
    # The stored history must not: a marker is a billing hint, not part of the conversation.
    assert sent[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in str(rebuilt)
    stripped = [{**m, "content": [{k: v for k, v in b.items() if k != "cache_control"} for b in m["content"]]}
                if isinstance(m["content"], list) else m for m in sent]
    assert rebuilt[:3] == stripped


def test_crash_then_resume_spends_only_the_remaining_steps(store):
    # Run 1: crashes after step 2 is committed.
    client1 = FakeClient([
        _resp("tool_use", [_tool("read_tasks", {"include_done": False}, "tu_1")]),
        _resp("tool_use", [_tool("read_calendar", {"range": "today"}, "tu_2")]),
        _resp("end_turn", [_text("never reached")]),
    ])
    with pytest.raises(SystemExit):
        loop.run("sys", "go", client=client1, store=store, crash_after_step=2)
    run_id = store.latest_run()["id"]
    assert store.get_run(run_id)["status"] == "running"
    assert len(store.steps(run_id)) == 2
    assert len(client1.calls) == 2          # the third call never happened

    # Run 2: resume. Only ONE more API call should be made.
    client2 = FakeClient([_resp("end_turn", [_text("brief after resume")])])
    r = loop.resume(run_id, store=store, client=client2)
    assert len(client2.calls) == 1
    assert r.steps == 3 and r.text == "brief after resume"
    # And the one call carried the full rebuilt history, ids intact.
    msgs = client2.calls[0]["messages"]
    assert msgs[1]["content"][0]["id"] == "tu_1"
    assert msgs[4]["content"][0]["tool_use_id"] == "tu_2"
    assert store.get_run(run_id)["status"] == "done"
    assert store.get_run(run_id)["input_tokens"] == 300   # 2 crashed steps + 1 resumed


def test_resume_of_finished_run_costs_nothing(store):
    client = FakeClient([_resp("end_turn", [_text("done")])])
    r = loop.run("sys", "go", client=client, store=store)
    again = loop.resume(r.run_id, store=store, client=FakeClient([]))
    assert again.text == "done"


def test_loop_error_marks_run_failed(store):
    client = FakeClient([_resp("tool_use", [_tool("read_tasks", {"include_done": False})]) for _ in range(3)])
    with pytest.raises(loop.LoopError):
        loop.run("sys", "go", client=client, store=store, max_steps=2)
    run = store.latest_run()
    assert run["status"] == "failed" and "no end_turn" in run["error"]


def test_tasks_persist_across_store_instances(tmp_path):
    s1 = Store(tmp_path / "t.db")
    s1.seed_tasks([{"id": "t1", "title": "seeded", "due": "2026-09-10", "done": False}])
    a = s1.create_task(title="Reply to Priya", due="2026-09-10", idempotency_key="k1")
    s1.close()
    s2 = Store(tmp_path / "t.db")
    titles = [t["title"] for t in s2.list_tasks()]
    assert "Reply to Priya" in titles and "seeded" in titles
    b = s2.create_task(title="Reply to Priya", due="2026-09-10", idempotency_key="k1")
    assert b["created"] is False and b["id"] == a["id"]
    s2.seed_tasks([{"id": "t1", "title": "seeded AGAIN", "due": None, "done": False}])
    assert [t for t in s2.list_tasks() if t["id"] == "t1"][0]["title"] == "seeded"   # seed is idempotent
    s2.close()


# ── compaction ──
from dayflow.compact import compact, estimate_tokens  # noqa: E402


def _exchange(i, ok=True, n=3):
    a = {"role": "assistant", "content": [{"type": "tool_use", "id": f"tu_{i}", "name": "read_inbox", "input": {"limit": 20, "offset": 0}}]}
    payload = {"ok": True, "data": [{"id": f"m{k}", "body": "x" * 400} for k in range(n)]} if ok else {"ok": False, "error": {"code": "quota", "message": "", "retryable": True}}
    u = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tu_{i}", "content": json.dumps(payload)}]}
    return a, u


def test_compact_is_a_noop_under_threshold():
    msgs = [{"role": "user", "content": "task"}]
    for i in range(3):
        msgs.extend(_exchange(i))
    assert compact(msgs, threshold_tokens=1_000_000) is msgs


def test_compact_keeps_task_and_last_pair_and_summarises_the_rest():
    msgs = [{"role": "user", "content": "task"}]
    for i in range(6):
        msgs.extend(_exchange(i, ok=(i != 2)))
    before = estimate_tokens(msgs)
    out = compact(msgs, keep_last=1, threshold_tokens=1)
    assert out[0] == msgs[0]                                   # task intact
    assert out[-2:] == msgs[-2:]                               # last pair intact, ids preserved
    assert out[1]["role"] == "user"
    summary = out[1]["content"][0]["text"]
    assert "compacted" in summary
    assert summary.count("read_inbox(") == 5                   # 5 old exchanges summarised
    assert "error:quota" in summary                            # failures are not forgotten
    assert "(3 items)" in summary
    assert estimate_tokens(out) < before / 3
