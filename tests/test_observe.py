"""Lesson 90: cost arithmetic, the events the loop records, trace and stats."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dayflow import loop, observe, tools
from dayflow.permissions import Gate, Policy
from dayflow.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "o.db")
    previous = tools.tasks
    tools.use_store(s)
    yield s
    tools.tasks = previous
    s.close()


def _usage(i, o, read=0, write=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=read, cache_creation_input_tokens=write)


class Client:
    def __init__(self, script):
        self._script, self.calls = list(script), []

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.calls.append(kw)
        return self._script.pop(0)


def _two_step(read=0, write=0):
    return Client([
        SimpleNamespace(stop_reason="tool_use", usage=_usage(100, 50, 0, write),
                        content=[SimpleNamespace(type="tool_use", name="read_tasks", input={"include_done": False}, id="t1"),
                                 SimpleNamespace(type="tool_use", name="create_task", input={"title": "x", "due": None, "idempotency_key": "k"}, id="t2")]),
        SimpleNamespace(stop_reason="end_turn", usage=_usage(40, 20, read, 0), content=[SimpleNamespace(type="text", text="done")]),
    ])


def test_cost_adds_the_four_token_kinds_at_their_own_prices():
    c = observe.cost("claude-sonnet-5-5", input_tokens=1_000_000, output_tokens=1_000_000, cache_read=1_000_000, cache_write=1_000_000)
    assert c == pytest.approx(2.00 + 10.00 + 0.10 + 2.50)
    assert observe.cost("claude-haiku-5-5", input_tokens=10_000, output_tokens=2_000) == pytest.approx(0.002)


def test_unknown_model_has_no_cost_rather_than_a_zero_cost():
    assert observe.cost("some-new-model", input_tokens=1000) is None


def test_a_cached_token_costs_a_twentieth_of_an_uncached_one_and_a_write_costs_more():
    p = observe.PRICES["claude-sonnet-5-5"]
    assert p.cache_read * 20 == pytest.approx(p.input) and p.cache_write > p.input


def test_loop_records_one_event_per_model_call_and_per_tool_call(store):
    r = loop.run("sys", "go", client=_two_step(), store=store, gate=Gate(Policy("ask"), store=store))
    ev = store.events(r.run_id)
    assert [(e["step"], e["kind"], e["name"]) for e in ev] == [
        (1, "model", loop.DEFAULT_MODEL), (1, "tool", "read_tasks"), (1, "tool", "create_task"), (2, "model", loop.DEFAULT_MODEL)]
    assert ev[1]["ok"] == 1 and ev[2]["ok"] == 0 and ev[2]["code"] == "pending_approval"     # the refusal is in the trace
    assert ev[0]["code"] == "tool_use" and ev[3]["code"] == "end_turn" and all(e["ms"] >= 0 for e in ev)


def test_cached_tokens_count_toward_the_token_budget(store):
    """Caching makes tokens cheaper, not free, and not invisible. A run that read
    50k tokens from cache still read 50k tokens."""
    with pytest.raises(loop.LoopError, match="token budget"):
        loop.run("sys", "go", client=_two_step(read=50_000), store=store, token_budget=10_000)


def test_run_totals_include_cached_input_and_cost_prices_it_separately(store):
    r = loop.run("sys", "go", client=_two_step(read=1000, write=500), store=store)
    assert r.input_tokens == 100 + 500 + 40 + 1000
    t = observe.run_cost(store, r.run_id)
    assert (t["uncached"], t["cache_read"], t["cache_write"], t["output"]) == (140, 1000, 500, 70)
    assert t["usd"] == pytest.approx((140 * 2 + 70 * 10 + 1000 * 0.10 + 500 * 2.50) / 1e6)


def test_system_prompt_and_last_block_carry_cache_markers_unless_disabled(store, monkeypatch):
    c = _two_step()
    loop.run("sys", "go", client=c, store=store)
    assert c.calls[0]["system"] == [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    assert c.calls[0]["messages"][0] == {"role": "user", "content": "go"}                   # a plain string: nothing to mark
    assert c.calls[1]["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    monkeypatch.setenv("DAYFLOW_CACHE", "0")
    c2 = _two_step()
    loop.run("sys", "go", client=c2, store=store)
    assert c2.calls[0]["system"] == "sys" and "cache_control" not in str(c2.calls[1]["messages"])


def test_trace_shows_each_call_and_a_total(store):
    r = loop.run("sys", "go", client=_two_step(read=1000), store=store)
    out = observe.trace(store, r.run_id)
    assert "-> read_tasks" in out and "pending_approval" in out and "end_turn" in out
    assert out.splitlines()[-2].split()[0] == "total" and "$0." in out


def test_trace_says_so_when_a_run_has_no_events(store):
    run_id = store.create_run(kind="brief", system="s", user_msg="u", model="m")
    assert "no events recorded" in observe.trace(store, run_id)


def test_percentile_is_nearest_rank():
    assert observe.percentile([1, 2, 3, 4, 5], 50) == 3
    assert observe.percentile([1, 2, 3, 4, 5], 95) == 5          # with five runs, p95 is the worst run
    assert observe.percentile([], 95) == 0.0


def test_stats_counts_failures_and_names_the_reason(store):
    loop.run("sys", "go", client=_two_step(), store=store)
    with pytest.raises(loop.LoopError):
        loop.run("sys", "go", client=_two_step(read=90_000), store=store, token_budget=10_000)
    out = observe.stats(store)
    row = next(l for l in out.splitlines() if l.startswith("brief"))
    assert row.split()[2:4] == ["2", "1"]
    assert "failed x1: token budget exceeded" in out


def test_stats_prints_na_not_zero_for_a_model_without_a_price(store):
    loop.run("sys", "go", client=_two_step(), store=store, model="unpriced-model")
    assert "n/a" in next(l for l in observe.stats(store).splitlines() if "unpriced-model" in l)
