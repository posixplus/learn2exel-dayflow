"""The agent loop, from scratch. Lesson 81.

    send messages -> read stop_reason
        end_turn   -> done, return the text
        tool_use   -> run each tool, append tool_results, loop
        max_tokens -> the model got cut off, treat as a failure
        anything else -> stop and surface it

Three guards keep this from running forever or burning money:
    max_steps      hard cap on loop iterations
    token_budget   cumulative input+output tokens across the run
    end_turn       the model's own signal that it is finished
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import anthropic

from dayflow.tools import TOOLS, dispatch

DEFAULT_MODEL = os.environ.get("DAYFLOW_MODEL", "claude-sonnet-5")


class LoopError(RuntimeError):
    pass


@dataclass
class RunResult:
    text: str
    steps: int
    input_tokens: int
    output_tokens: int
    stop_reason: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def run(
    system: str,
    user: str,
    *,
    model: str = DEFAULT_MODEL,
    max_steps: int = 10,
    token_budget: int = 60_000,
    max_tokens: int = 2_048,
    client: anthropic.Anthropic | None = None,
    verbose: bool = False,
) -> RunResult:
    client = client or anthropic.Anthropic()
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    used_in = used_out = 0
    tool_log: list[dict[str, Any]] = []

    for step in range(1, max_steps + 1):
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        used_in += resp.usage.input_tokens
        used_out += resp.usage.output_tokens
        if verbose:
            print(f"[step {step}] stop={resp.stop_reason} in={resp.usage.input_tokens} out={resp.usage.output_tokens}")

        # Guard 2: token budget. Checked after every call, before acting on it.
        if used_in + used_out > token_budget:
            raise LoopError(f"token budget exceeded at step {step}: {used_in + used_out} > {token_budget}")

        if resp.stop_reason == "end_turn":
            text = "".join(b.text for b in resp.content if b.type == "text")
            return RunResult(text, step, used_in, used_out, resp.stop_reason, tool_log)

        if resp.stop_reason == "max_tokens":
            raise LoopError(f"model hit max_tokens={max_tokens} at step {step}; raise it or ask for less")

        if resp.stop_reason != "tool_use":
            raise LoopError(f"unexpected stop_reason {resp.stop_reason!r} at step {step}")

        # The assistant turn goes into history exactly as returned, tool_use blocks and all.
        messages.append({"role": "assistant", "content": resp.content})

        results: list[dict[str, Any]] = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            output = dispatch(block.name, block.input)
            tool_log.append({"step": step, "tool": block.name, "args": block.input})
            if verbose:
                print(f"    -> {block.name}({block.input}) {len(output)} chars")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        # Tool results always go back in a user turn, one tool_result per tool_use, ids matched.
        messages.append({"role": "user", "content": results})

    # Guard 1: max steps.
    raise LoopError(f"no end_turn after {max_steps} steps; the model is looping")
