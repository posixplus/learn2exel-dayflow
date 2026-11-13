"""Lesson 84: the same Dayflow, driven by the Claude Agent SDK.

Lessons 81-83 built the loop by hand so you would know what every line of it
costs. This lesson deletes that loop (for one code path) and keeps everything
that was actually ours: the tool registry, the result envelope, the store.

What the SDK takes over:
  - the request/response loop and stop_reason handling (loop.py)
  - permission checks before every tool call. Lesson 86 plugs Dayflow's own
    Gate into that check with a PreToolUse hook and takes the write tool off
    the auto-approved list.
  - session persistence and `resume` (it owns the transcript now)
  - compaction when the context fills (compact.py becomes a teaching artifact)

What stays ours, untouched:
  - tools.REGISTRY: each Tool is wrapped as an in-process MCP tool. The
    handler, the strict schema and the {"ok": ...} envelope are the same
    bytes the raw loop sent. The transport changed; the contract did not.
  - store.Store: still records one row per tool step via a PostToolUse hook,
    but as an AUDIT LOG. It is no longer the replay source for resume. Two
    sources of truth for "where was I" is one too many; the SDK session wins,
    and we record its session_id on the run so `dayflow resume` can hand it back.

Trade-offs, stated once so you can weigh them:
  - The SDK runs Claude Code as a subprocess. Startup adds ~1-2 s per run.
  - You no longer see raw `usage` per step; ResultMessage gives totals.
  - The default tool surface is Claude Code's (Read, Bash, ...). Dayflow
    passes tools=[] so the model sees ONLY the registry. Forgetting that line
    turns a calendar assistant into something that can run shell commands.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

from claude_agent_sdk import (
    AssistantMessage,
    ResultError,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from dayflow import clock, prompts
from dayflow.permissions import Gate, Policy
from dayflow.store import StepRow, Store
from dayflow.tools import REGISTRY, Kind, Tool, dispatch

SERVER = "dayflow"            # tools are addressed as mcp__dayflow__<name>


# ── Registry → in-process MCP server ─────────────────────────────────────────
def _as_mcp_tool(t: Tool):
    """Wrap one registry Tool. dispatch() still runs the handler and still
    returns the envelope, so error handling is identical to the raw loop."""
    schema = {
        "type": "object",
        "properties": t.properties,
        "required": list(t.properties),
        "additionalProperties": False,
    }

    @tool(t.name, t.description, schema)
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        envelope = dispatch(t.name, args)
        # The envelope carries ok/error; we do NOT also set is_error. The model
        # branches on `ok`, exactly as the prompt tells it to (lesson 82).
        return {"content": [{"type": "text", "text": envelope}]}

    return handler


def _select(kinds: tuple[Kind, ...], names: Iterable[str] | None) -> list[Tool]:
    """Registry subset: by kind (lesson 82) and, since lesson 85, by explicit name
    so a specialist can be handed exactly one tool."""
    wanted = set(names) if names is not None else None
    return [t for t in REGISTRY.values() if t.kind in kinds and (wanted is None or t.name in wanted)]


def build_server(kinds: tuple[Kind, ...] = ("read", "write"), names: Iterable[str] | None = None):
    return create_sdk_mcp_server(
        name=SERVER,
        version="0.88.0",
        tools=[_as_mcp_tool(t) for t in _select(kinds, names)],
    )


def tool_names(kinds: tuple[Kind, ...] = ("read", "write"), names: Iterable[str] | None = None) -> list[str]:
    return [f"mcp__{SERVER}__{t.name}" for t in _select(kinds, names)]


def preapproved_tools(kinds: tuple[Kind, ...] = ("read", "write")) -> list[str]:
    """Lesson 86: what the harness may run without asking anyone. Reads only.
    A write is never on this list, whatever `kinds` says, so it needs the
    PreToolUse hook to say allow. If the hook is missing or crashes, the write
    fails closed."""
    return tool_names(tuple(k for k in kinds if k == "read"))


def _short(name: str) -> str:
    return name.removeprefix(f"mcp__{SERVER}__")


# ── Result ───────────────────────────────────────────────────────────────────
@dataclass
class SdkResult:
    text: str
    session_id: str
    num_turns: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float | None = None
    run_id: str | None = None


async def collect(stream, *, verbose: bool = False, label: str = "") -> tuple[list[str], ResultMessage]:
    """Drain one query() stream. Returns the assistant text parts and the final
    ResultMessage. Shared by the single agent (84) and every specialist (85)."""
    text_parts: list[str] = []
    result: ResultMessage | None = None
    tag = f"[{label}] " if label else ""
    try:
        async for msg in stream:
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock) and verbose:
                        print(f"  {tag}→ {_short(block.name)}")
            elif isinstance(msg, ResultMessage):
                result = msg
    except ResultError as e:
        # max_turns, max_budget_usd and execution errors arrive as an exception in
        # this SDK version, not as a ResultMessage with is_error. Normalise.
        raise RuntimeError(f"{tag}{e}") from e
    if result is None:
        raise RuntimeError("SDK stream ended without a ResultMessage")
    return text_parts, result


# ── The run ──────────────────────────────────────────────────────────────────
async def run_brief(
    *,
    store: Store | None = None,
    kinds: tuple[Kind, ...] = ("read", "write"),
    verbose: bool = False,
    max_turns: int = 8,
    max_budget_usd: float = 0.25,
    model: str | None = None,
    resume: str | None = None,
    gate: Gate | None = None,
) -> SdkResult:
    model = model or os.environ.get("DAYFLOW_MODEL") or "claude-sonnet-5-5"
    user_msg = prompts.brief_request(clock.today())
    run_id = store.create_run(kind="brief-sdk", system=prompts.MORNING_BRIEF, user_msg=user_msg, model=model) if store else None
    tool_calls: list[dict[str, Any]] = []
    step = {"n": 0}
    # Lesson 86: the same Gate the raw loop uses. Default is the safe one (queue).
    gate = gate or Gate(Policy(), store=store)
    gate.run_id, gate.store = gate.run_id or run_id, gate.store or store

    async def ask_gate(hook_input: Any, _tool_use_id: str | None, _ctx: Any) -> dict[str, Any]:
        """PreToolUse: runs before every Dayflow tool call and returns a decision.
        'allow' lets the SDK run the tool. 'deny' stops it, and the reason is what
        the model gets back in place of a result, so the reason is our envelope."""
        name = _short(hook_input["tool_name"])
        outcome = await gate.acheck(name, hook_input["tool_input"])
        if verbose and not outcome.allowed:
            print(f"  ✋ {name}: {outcome.code}" + (f" ({outcome.approval_id})" if outcome.approval_id else ""))
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if outcome.allowed else "deny",
            "permissionDecisionReason": "allowed by the Dayflow gate" if outcome.allowed else outcome.envelope,
        }}

    async def record_step(hook_input: Any, _tool_use_id: str | None, _ctx: Any) -> dict[str, Any]:
        """PostToolUse: one row per tool call, same table lesson 83 wrote.
        Token counts per step are not exposed here; totals land on the run."""
        step["n"] += 1
        name = _short(hook_input["tool_name"])
        tool_calls.append({"name": name, "input": hook_input["tool_input"]})
        if verbose:
            print(f"  ↳ {name}({json.dumps(hook_input['tool_input'])})")
        if store and run_id:
            store.append_step(run_id, StepRow(
                n=step["n"],
                stop_reason="tool_use",
                assistant=[{"type": "tool_use", "id": hook_input["tool_use_id"], "name": name, "input": hook_input["tool_input"]}],
                tool_results=[{"type": "tool_result", "tool_use_id": hook_input["tool_use_id"],
                               "content": json.dumps(hook_input["tool_response"])}],
                input_tokens=0, output_tokens=0,
            ))
        return {}

    options = ClaudeAgentOptions(
        system_prompt=prompts.MORNING_BRIEF,          # ours, not the Claude Code preset
        mcp_servers={SERVER: build_server(kinds)},
        strict_mcp_config=True,                       # ignore ~/.claude.json and .mcp.json
        setting_sources=[],                           # ignore the developer's own Claude Code settings
        tools=[],                                     # no Read/Bash/Edit: the registry is the whole surface
        # Lesson 86: only READ tools are pre-approved. A write is not on this list,
        # so the harness itself refuses it unless the PreToolUse hook says allow.
        # Two locks: lose the hook and writes fail closed, they do not run.
        allowed_tools=preapproved_tools(kinds),
        permission_mode="default",
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        model=model,
        resume=resume,
        hooks={
            "PreToolUse": [HookMatcher(matcher=f"mcp__{SERVER}__.*", hooks=[ask_gate])],
            "PostToolUse": [HookMatcher(matcher=f"mcp__{SERVER}__.*", hooks=[record_step])],
        },
    )

    try:
        text_parts, result = await collect(query(prompt=user_msg, options=options), verbose=verbose)
    except Exception as e:  # noqa: BLE001
        if store and run_id:
            store.fail_run(run_id, error=f"{type(e).__name__}: {e}")
        raise

    if result.is_error or result.subtype != "success":
        msg = f"{result.subtype}: {'; '.join(result.errors or [])}"
        if store and run_id:
            store.fail_run(run_id, error=msg)
        raise RuntimeError(msg)

    # The final assistant text is the brief. result.result is the same text
    # in most cases; prefer the blocks we saw so streaming and non-streaming agree.
    text = "\n".join(p for p in text_parts if p.strip()) or (result.result or "")
    usage = result.usage or {}
    out = SdkResult(
        text=text,
        session_id=result.session_id,
        num_turns=result.num_turns,
        tool_calls=tool_calls,
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
        cost_usd=result.total_cost_usd,
        run_id=run_id,
    )
    if store and run_id:
        store.finish_run(run_id, text=text)
        store.record_sdk_totals(run_id, session_id=out.session_id,
                                input_tokens=out.input_tokens + out.cache_read_tokens,
                                output_tokens=out.output_tokens)
    return out
