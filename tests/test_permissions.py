"""Lesson 86: the gate is plain code, so almost all of it is tested without a model.

The questions these tests answer are the ones a reviewer should ask of any
human-in-the-loop design: what happens by default, what happens when nobody is
there, can an allow rule skip a limit, does approving run what was shown, and
what happens when the approval machinery itself breaks.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dayflow import loop, permissions, sdk_agent, tools
from dayflow.permissions import Gate, Policy, QueueApprover, TtyApprover
from dayflow.store import Store

WRITE = {"title": "Reply to Priya", "due": "2026-09-10", "idempotency_key": "reply-priya"}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    previous = tools.tasks            # use_store() swaps a module global; put it back after
    tools.use_store(s)
    yield s
    tools.tasks = previous
    s.close()


def _titles(store):
    return [t["title"] for t in store.list_tasks()]


def _answers(*replies):
    it = iter(replies)
    return lambda _prompt: next(it)


# ── Policy ───────────────────────────────────────────────────────────────────
def test_reads_are_allowed_and_writes_ask_by_default():
    p = Policy()
    assert p.decide(tools.READ_INBOX, {}, writes_so_far=0).behavior == "allow"
    assert p.decide(tools.CREATE_TASK, WRITE, writes_so_far=0).behavior == "ask"


def test_deny_rules_are_checked_before_allow_rules():
    """--approve auto must not be a way around the per-run write limit."""
    p = Policy("allow", max_writes_per_run=2)
    assert p.decide(tools.CREATE_TASK, WRITE, writes_so_far=1).behavior == "allow"
    d = p.decide(tools.CREATE_TASK, WRITE, writes_so_far=2)
    assert d.behavior == "deny" and d.code == permissions.WRITE_LIMIT


def test_read_only_policy_denies_every_write():
    d = Policy("deny").decide(tools.CREATE_TASK, WRITE, writes_so_far=0)
    assert d.behavior == "deny" and d.code == permissions.READ_ONLY


# ── Gate + queue: nobody is there ────────────────────────────────────────────
def test_queued_write_is_not_executed_and_tells_the_model_so(store):
    gate = Gate(Policy(), QueueApprover(), store, run_id="r1")
    before = _titles(store)
    body = json.loads(gate.call("create_task", WRITE))
    assert body["ok"] is False and body["error"]["code"] == permissions.PENDING
    assert body["error"]["retryable"] is False
    assert _titles(store) == before                       # nothing was written
    (row,) = store.list_approvals()
    assert row["id"] in body["error"]["message"]          # the model can cite the id
    assert json.loads(row["args"]) == WRITE and row["decided_by"] is None


def test_queued_proposals_count_toward_the_write_limit(store):
    gate = Gate(Policy(max_writes_per_run=2), QueueApprover(), store)
    codes = [json.loads(gate.call("create_task", {**WRITE, "idempotency_key": f"k{i}"}))["error"]["code"] for i in range(3)]
    assert codes == [permissions.PENDING, permissions.PENDING, permissions.WRITE_LIMIT]
    assert len(store.list_approvals()) == 2


def test_reads_pass_through_untouched_and_leave_no_audit_row(store):
    gate = Gate(Policy("deny"), QueueApprover(), store)
    assert json.loads(gate.call("read_tasks", {"include_done": False}))["ok"] is True
    assert store.list_approvals(status=None) == []


# ── Gate + terminal: a person answers ────────────────────────────────────────
def test_yes_at_the_terminal_runs_the_write(store):
    gate = Gate(Policy(), TtyApprover(ask=_answers("y"), out=open("/dev/null", "w")), store)
    body = json.loads(gate.call("create_task", WRITE))
    assert body["ok"] is True and body["data"]["created"] is True
    (row,) = store.list_approvals(status="allowed")
    assert row["decided_by"] == "human"


def test_no_with_a_reason_reaches_the_model(store):
    gate = Gate(Policy(), TtyApprover(ask=_answers("n", "already handled"), out=open("/dev/null", "w")), store)
    body = json.loads(gate.call("create_task", WRITE))
    assert body["error"]["code"] == permissions.DENIED and "already handled" in body["error"]["message"]
    assert "Reply to Priya" not in _titles(store)


def test_enter_and_closed_stdin_both_mean_no():
    """The default answer is the safe one. So is no answer at all."""
    def closed(_prompt):
        raise EOFError
    for ask in (_answers("", ""), closed):
        gate = Gate(Policy(), TtyApprover(ask=ask, out=open("/dev/null", "w")))
        assert gate.check("create_task", WRITE).allowed is False


def test_always_approves_the_rest_of_the_run_but_not_past_the_limit():
    gate = Gate(Policy(max_writes_per_run=2), TtyApprover(ask=_answers("a"), out=open("/dev/null", "w")))
    assert gate.check("create_task", WRITE).allowed is True
    assert gate.check("create_task", WRITE).allowed is True       # no second question: _answers would raise
    assert gate.check("create_task", WRITE).code == permissions.WRITE_LIMIT


def test_the_prompt_shows_the_exact_arguments():
    text = permissions.render(permissions.Request("create_task", WRITE, "create_task is a write tool"))
    for value in WRITE.values():
        assert value in text


# ── Acting on the queue later ────────────────────────────────────────────────
def _queue_one(store, **over):
    gate = Gate(Policy(), QueueApprover(), store, run_id="r1", **over)
    return gate.check("create_task", WRITE).approval_id


def test_approve_runs_exactly_what_was_recorded(store):
    approval_id = _queue_one(store)
    result = permissions.approve(store, approval_id)
    assert result["data"]["title"] == WRITE["title"] and result["data"]["created"] is True
    row = store.get_approval(approval_id)
    assert row["status"] == "approved" and row["decided_by"] == "human"


def test_approving_twice_writes_once(store):
    approval_id = _queue_one(store)
    first = permissions.approve(store, approval_id)
    second = permissions.approve(store, approval_id)
    assert first == second
    assert _titles(store).count(WRITE["title"]) == 1


def test_a_stale_approval_is_refused(store):
    approval_id = _queue_one(store, ttl_hours=-1)          # already past its expiry
    with pytest.raises(permissions.ApprovalError, match="expired"):
        permissions.approve(store, approval_id)
    assert store.get_approval(approval_id)["status"] == "expired"
    assert WRITE["title"] not in _titles(store)


def test_expiry_is_a_real_clock_comparison(store):
    row = store.get_approval(_queue_one(store, ttl_hours=1))
    now = datetime.now(timezone.utc)
    assert store.is_expired(row, now=now) is False
    assert store.is_expired(row, now=now + timedelta(hours=2)) is True


def test_a_denied_proposal_cannot_be_approved_afterwards(store):
    approval_id = _queue_one(store)
    permissions.deny(store, approval_id, "not needed")
    with pytest.raises(permissions.ApprovalError, match="denied"):
        permissions.approve(store, approval_id)
    assert store.get_approval(approval_id)["reason"] == "not needed"


def test_approved_but_failing_tool_stays_pending(store, monkeypatch):
    """Yes from the human plus an error from the tool is not 'approved'."""
    approval_id = _queue_one(store)
    def boom(**_kw):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(tools.REGISTRY["create_task"], "handler", boom)
    with pytest.raises(permissions.ApprovalError, match="tool_failed"):
        permissions.approve(store, approval_id)
    assert store.get_approval(approval_id)["status"] == "pending"


# ── The loop: safe by default ────────────────────────────────────────────────
def _resp(stop, content):
    return SimpleNamespace(stop_reason=stop, content=content, usage=SimpleNamespace(input_tokens=10, output_tokens=5))


class FakeClient:
    def __init__(self, responses):
        self._responses, self.calls = list(responses), []

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.calls.append(kw)
        return self._responses.pop(0)


def test_loop_without_a_gate_queues_writes(store):
    """A caller that forgets the gate gets the safe behaviour, not the old one."""
    client = FakeClient([
        _resp("tool_use", [SimpleNamespace(type="tool_use", name="create_task", input=WRITE, id="tu_1")]),
        _resp("end_turn", [SimpleNamespace(type="text", text="done")]),
    ])
    r = loop.run("sys", "go", client=client, store=store)
    sent_back = json.loads(client.calls[1]["messages"][-1]["content"][0]["content"])
    assert sent_back["error"]["code"] == permissions.PENDING
    assert WRITE["title"] not in _titles(store)
    (row,) = store.list_approvals()
    assert row["run_id"] == r.run_id                       # the proposal is tied to the run that made it


def test_loop_with_auto_gate_behaves_as_before_lesson_86(store):
    client = FakeClient([
        _resp("tool_use", [SimpleNamespace(type="tool_use", name="create_task", input=WRITE, id="tu_1")]),
        _resp("end_turn", [SimpleNamespace(type="text", text="done")]),
    ])
    loop.run("sys", "go", client=client, store=store, gate=permissions.gate_for("auto", store=store))
    assert WRITE["title"] in _titles(store)
    assert store.list_approvals(status="allowed")[0]["decided_by"] == "policy"


# ── The SDK engine ───────────────────────────────────────────────────────────
def test_sdk_preapproves_reads_only_whatever_kinds_it_is_given():
    """The second lock: with the write off the allow-list, a missing or crashing
    hook means the write does not run."""
    assert sdk_agent.preapproved_tools(("read", "write")) == sdk_agent.tool_names(("read",))
    assert "mcp__dayflow__create_task" not in sdk_agent.preapproved_tools()


def test_async_check_keeps_the_store_on_its_own_thread(store):
    """Regression: the first SDK hook ran the whole check in a worker thread and
    SQLite refused the connection. Only the approver may leave the loop thread."""
    gate = Gate(Policy(), TtyApprover(ask=_answers("y"), out=open("/dev/null", "w")), store)
    outcome = asyncio.run(gate.acheck("create_task", WRITE))
    assert outcome.allowed is True
    assert len(store.list_approvals(status="allowed")) == 1


def test_gate_for_never_waits_on_a_terminal_that_is_not_there(store):
    assert isinstance(permissions.gate_for("default", store=store, interactive=False).approver, QueueApprover)
    assert isinstance(permissions.gate_for("default", store=store, interactive=True).approver, TtyApprover)
    with pytest.raises(ValueError):
        permissions.gate_for("yolo", store=store)


def test_the_write_limit_is_per_tool(store):
    """Lesson 87 added draft_reply. Two drafts must not use up the two tasks."""
    gate = Gate(Policy(max_writes_per_run=2), QueueApprover(), store)
    draft = {"message_id": "m1", "body": "Thanks.", "idempotency_key": "d"}
    codes = [gate.check("draft_reply", draft).code, gate.check("draft_reply", draft).code, gate.check("draft_reply", draft).code,
             gate.check("create_task", WRITE).code, gate.check("create_task", WRITE).code, gate.check("create_task", WRITE).code]
    assert codes == [permissions.PENDING] * 2 + [permissions.WRITE_LIMIT] + [permissions.PENDING] * 2 + [permissions.WRITE_LIMIT]
