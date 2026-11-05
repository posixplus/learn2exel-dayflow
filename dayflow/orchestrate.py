"""Lesson 85: orchestration, and when one agent is enough.

Lesson 84's single agent reads three sources and writes the brief in one
context. That is the right design for a five-message inbox. It stops being
right when one source gets big: a 120-message inbox puts ~12K tokens of
email bodies into the context that then has to write a four-section brief,
and every turn after the read pays to carry them.

This module is the alternative, written out in plain code so you can see
every decision:

  fan out   three SPECIALISTS run in parallel (asyncio.gather). Each has one
            job, one tool, a fixed output shape, and a cheap model.
  fan in    one SYNTHESISER with no tools turns three short reports into the
            brief. It never sees an email body.

What this buys: context isolation (the synthesiser's context is ~1K tokens no
matter how big the inbox is), parallel wall time, narrower prompts that are
easier to evaluate (lesson 89), and the ability to route each job to the
smallest model that does it well.

What it costs: four system prompts instead of one, four subprocess startups,
a new failure mode (a specialist that returns the wrong shape), and the
honest observation that for the SMALL inbox it is slower and no cheaper.
Run both and read the numbers; that is the lesson.

The SDK also offers subagents the model spawns itself (ClaudeAgentOptions
(agents={...}) plus the Agent tool). We do not use that here, deliberately:
when the model decides whether to delegate, cost and latency become
non-deterministic. Dayflow's fan-out is fixed, so it is testable.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query

from dayflow import clock, prompts
from dayflow.sdk_agent import SERVER, _short, build_server, collect, tool_names
from dayflow.store import StepRow, Store

SPECIALIST_MODEL = os.environ.get("DAYFLOW_SPECIALIST_MODEL", "claude-haiku-5-5")


@dataclass(frozen=True)
class Specialist:
    name: str
    system: str
    tool: str                      # exactly one registry tool
    request: str                   # "Today is {today}. ..." - every new context needs the date (lesson 84)
    max_turns: int = 3


SPECIALISTS = (
    Specialist("inbox", prompts.INBOX_TRIAGE, "read_inbox", "Today is {today}. Triage the inbox.", max_turns=10),  # 120 msgs = 5 pages
    Specialist("calendar", prompts.CALENDAR_READER, "read_calendar", "Today is {today}. Read the calendar."),
    Specialist("tasks", prompts.TASKS_READER, "read_tasks", "Today is {today}. Read the tasks."),
)


@dataclass
class Leg:
    """One specialist's (or the synthesiser's) run, for the cost table."""
    name: str
    model: str
    turns: int
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    cost_usd: float
    seconds: float
    text: str


@dataclass
class OrchResult:
    text: str
    legs: list[Leg] = field(default_factory=list)
    run_id: str | None = None
    session_id: str = ""          # the synthesiser's session; specialists are fire-and-forget

    @property
    def cost_usd(self) -> float:
        return sum(l.cost_usd for l in self.legs)

    @property
    def input_tokens(self) -> int:
        return sum(l.input_tokens for l in self.legs)

    @property
    def cache_read_tokens(self) -> int:
        return sum(l.cache_read_tokens for l in self.legs)

    @property
    def output_tokens(self) -> int:
        return sum(l.output_tokens for l in self.legs)

    @property
    def seconds(self) -> float:
        # Specialists overlap, so wall time is max(specialists) + synthesiser.
        spec = [l.seconds for l in self.legs if l.name != "synthesiser"]
        synth = [l.seconds for l in self.legs if l.name == "synthesiser"]
        return (max(spec) if spec else 0) + sum(synth)


def _leg(name: str, model: str, result: Any, text: str, seconds: float) -> Leg:
    u = result.usage or {}
    return Leg(name, model, result.num_turns, int(u.get("input_tokens", 0)), int(u.get("cache_read_input_tokens", 0)),
               int(u.get("output_tokens", 0)), float(result.total_cost_usd or 0), seconds, text)


async def run_specialist(spec: Specialist, *, today: str, store: Store | None, run_id: str | None, step: dict,
                         verbose: bool, model: str = SPECIALIST_MODEL) -> Leg:
    async def record_step(h: Any, _id: str | None, _ctx: Any) -> dict[str, Any]:
        step["n"] += 1
        if store and run_id:
            store.append_step(run_id, StepRow(
                n=step["n"], stop_reason="tool_use",
                assistant=[{"type": "tool_use", "id": h["tool_use_id"], "name": _short(h["tool_name"]), "input": h["tool_input"],
                            "specialist": spec.name}],
                tool_results=[{"type": "tool_result", "tool_use_id": h["tool_use_id"], "content": json.dumps(h["tool_response"])}],
                input_tokens=0, output_tokens=0))
        return {}

    options = ClaudeAgentOptions(
        system_prompt=spec.system,
        mcp_servers={SERVER: build_server(("read",), names=[spec.tool])},   # exactly one tool
        strict_mcp_config=True, setting_sources=[], tools=[],
        allowed_tools=tool_names(("read",), names=[spec.tool]),
        permission_mode="default",
        max_turns=spec.max_turns, max_budget_usd=0.10, model=model,
        hooks={"PostToolUse": [HookMatcher(matcher=f"mcp__{SERVER}__.*", hooks=[record_step])]},
    )
    t0 = time.perf_counter()
    try:
        parts, result = await collect(query(prompt=spec.request.format(today=today), options=options), verbose=verbose, label=spec.name)
    except RuntimeError as e:
        # A specialist that runs out of turns or budget is a missing source, not a
        # dead brief. Report it in the shape the synthesiser already understands.
        return Leg(spec.name, model, 0, 0, 0, 0, 0.0, time.perf_counter() - t0,
                   f"{spec.name.upper()}_UNAVAILABLE: {e}")
    text = "\n".join(p for p in parts if p.strip()) or (result.result or "")
    return _leg(spec.name, model, result, text, time.perf_counter() - t0)


async def run_brief(*, store: Store | None = None, verbose: bool = False, model: str | None = None,
                    specialist_model: str = SPECIALIST_MODEL) -> OrchResult:
    model = model or os.environ.get("DAYFLOW_MODEL") or "claude-sonnet-5-5"
    today = clock.today()
    run_id = store.create_run(kind="brief-orch", system=prompts.SYNTHESISER,
                              user_msg=f"orchestrated brief for {today}", model=model) if store else None
    step = {"n": 0}
    try:
        # ── fan out ──
        legs = list(await asyncio.gather(*[
            run_specialist(s, today=today, store=store, run_id=run_id, step=step, verbose=verbose, model=specialist_model)
            for s in SPECIALISTS
        ]))
        reports = {l.name: l.text for l in legs}

        # ── fan in: no tools, no MCP, just the three reports ──
        options = ClaudeAgentOptions(
            system_prompt=prompts.SYNTHESISER,
            strict_mcp_config=True, setting_sources=[], tools=[],
            permission_mode="default", max_turns=1, max_budget_usd=0.10, model=model,
        )
        t0 = time.perf_counter()
        parts, result = await collect(query(prompt=prompts.synth_request(today, reports), options=options),
                                      verbose=verbose, label="synth")
        text = "\n".join(p for p in parts if p.strip()) or (result.result or "")
        legs.append(_leg("synthesiser", model, result, text, time.perf_counter() - t0))
    except Exception as e:  # noqa: BLE001
        if store and run_id:
            store.fail_run(run_id, error=f"{type(e).__name__}: {e}")
        raise

    out = OrchResult(text=text, legs=legs, run_id=run_id, session_id=result.session_id)
    if store and run_id:
        store.finish_run(run_id, text=text)
        store.record_sdk_totals(run_id, session_id=out.session_id,
                                input_tokens=out.input_tokens + out.cache_read_tokens, output_tokens=out.output_tokens)
    return out


def cost_table(r: OrchResult) -> str:
    rows = [f"{'leg':<12}{'model':<20}{'turns':>5}{'in':>7}{'cached':>7}{'out':>6}{'$':>9}{'s':>6}"]
    for l in r.legs:
        rows.append(f"{l.name:<12}{l.model:<20}{l.turns:>5}{l.input_tokens:>7}{l.cache_read_tokens:>7}{l.output_tokens:>6}{l.cost_usd:>9.4f}{l.seconds:>6.1f}")
    rows.append(f"{'total':<12}{'':<20}{'':>5}{r.input_tokens:>7}{r.cache_read_tokens:>7}{r.output_tokens:>6}{r.cost_usd:>9.4f}{r.seconds:>6.1f}")
    return "\n".join(rows)
