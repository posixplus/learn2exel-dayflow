"""Lesson 91: retries, error classification, fault injection, and failing cleanly."""
from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import pytest

from dayflow import faults, loop, observe, retry, tools
from dayflow.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "r.db")
    previous = tools.tasks
    tools.use_store(s)
    yield s
    tools.tasks = previous
    s.close()


# --- retry.call ---------------------------------------------------------------

def _flaky(fail_times, exc=TimeoutError("slow")):
    state = {"n": 0}
    def fn():
        state["n"] += 1
        if state["n"] <= fail_times:
            raise exc
        return "ok"
    return fn, state


def test_retry_returns_the_result_and_how_many_tries_it_took():
    fn, _ = _flaky(2)
    slept = []
    assert retry.call(fn, policy=retry.Backoff(attempts=3), retry_if=lambda e: True, sleep=slept.append) == ("ok", 3)
    assert len(slept) == 2


def test_retry_stops_at_the_attempt_limit_and_reraises_with_the_count():
    fn, state = _flaky(99)
    with pytest.raises(TimeoutError) as e:
        retry.call(fn, policy=retry.Backoff(attempts=3), retry_if=lambda e: True, sleep=lambda s: None)
    assert state["n"] == 3 and e.value.attempts == 3


def test_a_non_retryable_error_is_raised_at_once_without_sleeping():
    fn, state = _flaky(99, exc=PermissionError("401"))
    slept = []
    with pytest.raises(PermissionError):
        retry.call(fn, policy=retry.Backoff(attempts=5), retry_if=lambda e: not isinstance(e, PermissionError), sleep=slept.append)
    assert state["n"] == 1 and slept == []


def test_backoff_grows_is_capped_and_is_jittered():
    p = retry.Backoff(attempts=6, base=0.5, cap=4.0)
    assert [p.wait(i, rng=lambda: 1.0) for i in range(5)] == [0.5, 1.0, 2.0, 4.0, 4.0]      # ceilings: double, then cap
    assert p.wait(3, rng=lambda: 0.25) == 1.0                                               # the wait is a draw below the ceiling
    assert p.worst_case() == 0.5 + 1.0 + 2.0 + 4.0 + 4.0


# --- dispatch: what gets retried ----------------------------------------------

def test_a_read_that_fails_twice_then_works_reports_ok_with_attempts(monkeypatch):
    monkeypatch.setenv("DAYFLOW_FAULTS", "read_inbox=2")
    env = json.loads(tools.dispatch("read_inbox", {"limit": 5, "offset": 0}))
    assert env["ok"] is True and env["attempts"] == 3 and len(env["data"]["messages"]) == 5


def test_an_outage_is_reported_after_three_tries_and_stays_retryable(monkeypatch):
    monkeypatch.setenv("DAYFLOW_FAULTS", "read_inbox=always")
    env = json.loads(tools.dispatch("read_inbox", {"limit": 5, "offset": 0}))
    assert env == {"ok": False, "attempts": 3,
                   "error": {"code": "upstream_unavailable", "message": "read_inbox: injected fault: upstream returned 503", "retryable": True}}


def test_an_auth_failure_is_not_retried(monkeypatch):
    monkeypatch.setenv("DAYFLOW_FAULTS", "read_calendar=auth")
    env = json.loads(tools.dispatch("read_calendar", {"range": "today"}))
    assert env["error"]["code"] == "auth" and env["error"]["retryable"] is False and "attempts" not in env


def test_a_write_is_never_retried_by_dispatch(monkeypatch):
    calls = []
    def flaky_write(**kw):
        calls.append(kw)
        raise tools.ToolError("upstream_unavailable", "503", retryable=True)
    monkeypatch.setattr(tools.REGISTRY["create_task"], "handler", flaky_write)
    env = json.loads(tools.dispatch("create_task", {"title": "x", "due": None, "idempotency_key": "k"}))
    assert len(calls) == 1                                    # one try. It may have happened; do not guess.
    assert env["error"]["retryable"] is True and "attempts" not in env


@pytest.mark.parametrize("exc,code,retryable", [
    (SimpleNamespace(status=401), "auth", False),
    (SimpleNamespace(status=403), "auth", False),
    (SimpleNamespace(status=404), "upstream_rejected", False),
    (SimpleNamespace(status=429), "upstream_unavailable", True),
    (SimpleNamespace(status=503), "upstream_unavailable", True),
    (TimeoutError("read timed out"), "upstream_unavailable", True),
    (ConnectionResetError("reset"), "upstream_unavailable", True),
    (KeyError("subject"), "tool_failed", False),              # a bug in the adapter: retrying repeats it
])
def test_classification_decides_whether_trying_again_could_help(exc, code, retryable):
    if isinstance(exc, SimpleNamespace):
        class Upstream(RuntimeError):
            status = exc.status
        exc = Upstream("x")
    e = tools._classify(exc)
    assert (e.code, e.retryable) == (code, retryable)


def test_fault_plan_parses_several_tools_and_counts_per_tool(monkeypatch):
    monkeypatch.setenv("DAYFLOW_FAULTS", "read_inbox=1,read_tasks=always")
    with pytest.raises(tools.ToolError):
        faults.maybe_fail("read_inbox")
    faults.maybe_fail("read_inbox")                           # second call passes
    faults.maybe_fail("read_calendar")                        # not in the plan
    with pytest.raises(tools.ToolError):
        faults.maybe_fail("read_tasks")


# --- the loop: retries show in the trace; an API outage fails cleanly ----------

def _resp(stop, content):
    return SimpleNamespace(stop_reason=stop, content=content, usage=SimpleNamespace(input_tokens=10, output_tokens=5))


class Client:
    def __init__(self, script):
        self._script, self.calls = list(script), 0

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _api_error(cls):
    """An SDK exception without going through its constructor, which wants a request object
    from whichever HTTP library this SDK version uses. The loop only looks at the type."""
    return cls.__new__(cls)


READ = _resp("tool_use", [SimpleNamespace(type="tool_use", name="read_inbox", input={"limit": 5, "offset": 0}, id="t1")])
DONE = _resp("end_turn", [SimpleNamespace(type="text", text="brief")])


def test_retries_are_visible_in_the_trace(store, monkeypatch):
    monkeypatch.setenv("DAYFLOW_FAULTS", "read_inbox=2")
    r = loop.run("sys", "go", client=Client([READ, DONE]), store=store)
    assert r.tool_calls[0]["ok"] is True and r.tool_calls[0]["attempts"] == 3
    assert "-> read_inbox" in observe.trace(store, r.run_id) and "ok (after 3 attempts)" in observe.trace(store, r.run_id)


def test_an_api_outage_fails_the_run_with_a_reason_and_resume_finishes_it(store):
    outage = _api_error(anthropic.APIConnectionError)
    with pytest.raises(loop.LoopError, match="api unavailable at step 2.*dayflow resume"):
        loop.run("sys", "go", client=Client([READ, outage]), store=store)
    run = store.latest_run()
    assert run["status"] == "failed" and "api unavailable" in run["error"]
    assert len(store.steps(run["id"])) == 1                   # step 1 is safe on disk

    again = Client([DONE])
    r = loop.resume(run["id"], store=store, client=again)
    assert r.text == "brief" and again.calls == 1             # step 1 was not paid for twice
    assert store.get_run(run["id"])["status"] == "done"


def test_an_auth_error_is_not_dressed_up_as_an_outage(store):
    bad_key = _api_error(anthropic.AuthenticationError)
    with pytest.raises(anthropic.AuthenticationError):
        loop.run("sys", "go", client=Client([bad_key]), store=store)
