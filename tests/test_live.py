"""Live tests: run the real loop against the real API.

Skipped unless DAYFLOW_LIVE=1 and a key is present. Each test has a token
budget so a regression that makes the model loop is bounded in cost. These
are the tests that would have caught a dead model ID, a schema the API
rejects, or a prompt change that stops the brief from calling the tools.
"""
from __future__ import annotations

import json
import os

import pytest

from dayflow import clock, loop, prompts
from dayflow.tools import definitions

live = pytest.mark.skipif(
    os.environ.get("DAYFLOW_LIVE") != "1" or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="set DAYFLOW_LIVE=1 and ANTHROPIC_API_KEY to run live tests",
)


@live
def test_model_id_is_valid_and_schemas_are_accepted():
    """The cheapest live test: one call, strict schemas included, no tools needed."""
    r = loop.run("Reply with the single word OK.", "ping", max_steps=1, token_budget=5_000, max_tokens=16)
    assert "OK" in r.text.upper()


@live
def test_brief_reads_all_three_sources_and_ends():
    r = loop.run(prompts.MORNING_BRIEF, prompts.brief_request(clock.today()),
                 max_steps=6, token_budget=30_000, tools=definitions(("read",)))
    called = {c["tool"] for c in r.tool_calls}
    assert {"read_inbox", "read_calendar", "read_tasks"} <= called
    assert r.stop_reason == "end_turn"
    assert "## One thing" in r.text


@live
def test_read_only_surface_cannot_write():
    r = loop.run(prompts.MORNING_BRIEF, prompts.brief_request(clock.today()),
                 max_steps=6, token_budget=30_000, tools=definitions(("read",)))
    assert not any(c["tool"] == "create_task" for c in r.tool_calls)


def _section(text: str, heading: str) -> str:
    """Return the body of a '## heading' section, or '' if absent."""
    marker = f"## {heading}"
    if marker not in text:
        return ""
    body = text.split(marker, 1)[1]
    return body.split("\n## ", 1)[0]


@live
def test_brief_does_not_ask_for_a_reply_to_the_promo():
    """Grade the behaviour, not the wording. The first version of this test
    asserted 'megastore' never appeared anywhere; the model then wrote
    '(skipped: megastore flash-sale promo)' and the test failed on a correct
    brief. The claim we care about is narrower: the promo is not presented
    as something the user should answer."""
    r = loop.run(prompts.MORNING_BRIEF, prompts.brief_request(clock.today()),
                 max_steps=6, token_budget=30_000, tools=definitions(("read",)))
    reply_items = [ln for ln in _section(r.text, "Needs a reply").splitlines() if ln.lstrip().startswith(("-", "*", "1", "2", "3"))]
    assert reply_items, "expected at least one reply item"
    assert not any("megastore" in ln.lower() or "flash sale" in ln.lower() for ln in reply_items if "skipped" not in ln.lower())


@live
def test_unattended_brief_proposes_but_never_writes(tmp_path):
    """Lesson 86: with nobody to ask, a full read+write surface must still leave
    the task list untouched. Anything the model wanted to do is a pending row."""
    from dayflow import permissions, tools
    from dayflow.store import Store
    store = Store(tmp_path / "live.db")
    previous = tools.tasks
    tools.use_store(store)
    try:
        before = store.list_tasks()
        r = loop.run(prompts.MORNING_BRIEF, prompts.brief_request(clock.today()), max_steps=6, token_budget=40_000,
                     store=store, gate=permissions.gate_for("queue", store=store))
        assert store.list_tasks() == before
        pending = store.list_approvals(run_id=r.run_id)
        for name in ("create_task", "draft_reply"):               # the limit is per write tool (lesson 87)
            proposed = [c for c in r.tool_calls if c["tool"] == name]
            assert len([p for p in pending if p["tool"] == name]) == min(len(proposed), 2)
        if pending:
            assert "approval" in r.text.lower() and pending[0]["id"] in r.text
    finally:
        tools.tasks = previous
        store.close()
