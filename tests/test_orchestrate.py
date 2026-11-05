"""Lesson 85: the orchestration is fixed code, so most of it is testable without a model.

What is checked here: every specialist has exactly one real tool and a dated
request; the reports → synthesiser contract; the failure path (a specialist
that dies becomes an *_UNAVAILABLE report, not a dead brief); and the cost
table arithmetic. The live test runs the real thing once on the small inbox.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from dayflow import orchestrate, prompts, tools
from dayflow.orchestrate import Leg, OrchResult, Specialist
from dayflow.sdk_agent import tool_names


def test_each_specialist_has_exactly_one_registry_tool_and_a_date():
    for s in orchestrate.SPECIALISTS:
        assert s.tool in tools.REGISTRY and tools.REGISTRY[s.tool].kind == "read"
        assert tool_names(("read",), names=[s.tool]) == [f"mcp__dayflow__{s.tool}"]
        assert "{today}" in s.request, "every new context needs the date (lesson 84)"
        assert s.tool in s.system, "the system prompt must name the one tool it may call"


def test_inbox_specialist_has_turns_for_five_pages():
    inbox = next(s for s in orchestrate.SPECIALISTS if s.name == "inbox")
    assert inbox.max_turns >= 6          # 120 messages / 25 per page = 5 reads + 1 answer


def test_synth_request_carries_every_report_labelled():
    req = prompts.synth_request("2026-09-07", {"inbox": "NEEDS_REPLY:\n- a | b | c | d", "calendar": "TODAY:\n- x", "tasks": "TASKS_UNAVAILABLE: boom"})
    assert req.startswith("Today is 2026-09-07.")
    for label in ("=== INBOX REPORT ===", "=== CALENDAR REPORT ===", "=== TASKS REPORT ==="):
        assert label in req
    assert req.rstrip().endswith("Write the brief.")


def test_specialist_failure_becomes_unavailable_report(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("[inbox] Reached maximum number of turns (3)")
    monkeypatch.setattr(orchestrate, "collect", boom)
    spec = Specialist("inbox", prompts.INBOX_TRIAGE, "read_inbox", "Today is {today}. Triage.")
    leg = asyncio.run(orchestrate.run_specialist(spec, today="2026-09-07", store=None, run_id=None, step={"n": 0}, verbose=False))
    assert leg.text.startswith("INBOX_UNAVAILABLE:") and leg.cost_usd == 0 and leg.turns == 0


def test_totals_and_wall_time():
    r = OrchResult(text="", legs=[
        Leg("inbox", "h", 6, 10, 5000, 800, 0.0075, 30.0, ""),
        Leg("calendar", "h", 2, 4, 2000, 600, 0.0007, 6.0, ""),
        Leg("tasks", "h", 2, 4, 2000, 200, 0.0003, 3.0, ""),
        Leg("synthesiser", "s", 1, 2, 0, 800, 0.0136, 8.0, ""),
    ])
    assert round(r.cost_usd, 4) == 0.0221
    assert r.input_tokens == 20 and r.cache_read_tokens == 9000 and r.output_tokens == 2400
    assert r.seconds == 38.0                 # max(30, 6, 3) + 8: specialists overlap
    table = orchestrate.cost_table(r)
    assert table.splitlines()[0].startswith("leg") and "total" in table.splitlines()[-1]


live = pytest.mark.skipif(
    os.environ.get("DAYFLOW_LIVE") != "1" or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="set DAYFLOW_LIVE=1 and ANTHROPIC_API_KEY to run live tests",
)


@live
def test_orchestrated_brief_small_inbox():
    r = asyncio.run(orchestrate.run_brief(store=None))
    names = [l.name for l in r.legs]
    assert names == ["inbox", "calendar", "tasks", "synthesiser"]
    inbox = next(l for l in r.legs if l.name == "inbox")
    assert "NEEDS_REPLY:" in inbox.text and "priya" in inbox.text.lower()
    assert "## One thing" in r.text
    assert r.cost_usd < 0.06
