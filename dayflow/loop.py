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

Lesson 83: every step is committed to the Store BEFORE its tool results are
acted on by the next call, so a crash at any point leaves a resumable run.
`resume(run_id)` rebuilds `messages` from the store and continues.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import anthropic

from dayflow.store import Store, StepRow
from dayflow.tools import definitions, dispatch

DEFAULT_MODEL = os.environ.get("DAYFLOW_MODEL", "claude-sonnet-5-5")


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
    run_id: str | None = None


def _plain(blocks: Any) -> list[dict[str, Any]]:
    """SDK content blocks -> JSON-able dicts. The API accepts these back verbatim."""
    out = []
    for b in blocks:
        if isinstance(b, dict):
            out.append(b)
        elif getattr(b, "type", None) == "text":
            out.append({"type": "text", "text": b.text})
        elif getattr(b, "type", None) == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        else:  # thinking blocks etc.: keep whatever the SDK can serialise
            out.append(b.model_dump() if hasattr(b, "model_dump") else dict(b))
    return out


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
    tools: list[dict[str, Any]] | None = None,
    store: Store | None = None,
    kind: str = "brief",
    crash_after_step: int | None = None,   # test hook: simulate a crash after step N is committed
) -> RunResult:
    """Start a new run. With a store, every step is persisted and the run is resumable."""
    run_id = store.create_run(kind=kind, system=system, user_msg=user, model=model) if store else None
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    return _drive(messages, start_step=1, used_in=0, used_out=0, run_id=run_id, system=system, model=model,
                  max_steps=max_steps, token_budget=token_budget, max_tokens=max_tokens, client=client,
                  verbose=verbose, tools=tools, store=store, crash_after_step=crash_after_step)


def resume(
    run_id: str,
    *,
    store: Store,
    client: anthropic.Anthropic | None = None,
    max_steps: int = 10,
    token_budget: int = 60_000,
    max_tokens: int = 2_048,
    verbose: bool = False,
    tools: list[dict[str, Any]] | None = None,
) -> RunResult:
    """Continue a run from its last committed step. Spends nothing on steps already done."""
    run = store.get_run(run_id)
    if run is None:
        raise LoopError(f"no run {run_id!r}")
    if run["status"] == "done":
        return RunResult(run["final_text"] or "", len(store.steps(run_id)), run["input_tokens"], run["output_tokens"], "end_turn", [], run_id)
    done = store.steps(run_id)
    messages = store.rebuild_messages(run_id)
    if verbose:
        print(f"[resume {run_id}] replaying {len(done)} committed step(s), {run['input_tokens']} in / {run['output_tokens']} out already spent")
    # Re-mark as running in case it was marked failed by a previous attempt.
    with store.tx() as c:
        c.execute("UPDATE runs SET status='running', error=NULL WHERE id=?", (run_id,))
    return _drive(messages, start_step=len(done) + 1, used_in=run["input_tokens"], used_out=run["output_tokens"],
                  run_id=run_id, system=run["system"], model=run["model"], max_steps=max_steps,
                  token_budget=token_budget, max_tokens=max_tokens, client=client, verbose=verbose, tools=tools,
                  store=store, crash_after_step=None)


def _drive(
    messages: list[dict[str, Any]], *, start_step: int, used_in: int, used_out: int, run_id: str | None,
    system: str, model: str, max_steps: int, token_budget: int, max_tokens: int,
    client: anthropic.Anthropic | None, verbose: bool, tools: list[dict[str, Any]] | None,
    store: Store | None, crash_after_step: int | None,
) -> RunResult:
    client = client or anthropic.Anthropic()
    tools = definitions() if tools is None else tools
    tool_log: list[dict[str, Any]] = []

    def fail(msg: str) -> LoopError:
        if store and run_id:
            store.fail_run(run_id, error=msg)
        return LoopError(msg)

    for step in range(start_step, max_steps + 1):
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
        )
        used_in += resp.usage.input_tokens
        used_out += resp.usage.output_tokens
        if verbose:
            print(f"[step {step}] stop={resp.stop_reason} in={resp.usage.input_tokens} out={resp.usage.output_tokens}")

        # Guard 2: token budget. Checked after every call, before acting on it.
        if used_in + used_out > token_budget:
            raise fail(f"token budget exceeded at step {step}: {used_in + used_out} > {token_budget}")

        content = _plain(resp.content)

        if resp.stop_reason == "end_turn":
            text = "".join(b["text"] for b in content if b.get("type") == "text")
            if store and run_id:
                store.append_step(run_id, StepRow(step, "end_turn", content, None, resp.usage.input_tokens, resp.usage.output_tokens))
                store.finish_run(run_id, text=text)
            return RunResult(text, step, used_in, used_out, resp.stop_reason, tool_log, run_id)

        if resp.stop_reason == "max_tokens":
            raise fail(f"model hit max_tokens={max_tokens} at step {step}; raise it or ask for less")

        if resp.stop_reason != "tool_use":
            raise fail(f"unexpected stop_reason {resp.stop_reason!r} at step {step}")

        # The assistant turn goes into history exactly as returned, tool_use blocks and all.
        messages.append({"role": "assistant", "content": content})

        results: list[dict[str, Any]] = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            output = dispatch(block["name"], block["input"])
            tool_log.append({"step": step, "tool": block["name"], "args": block["input"]})
            if verbose:
                print(f"    -> {block['name']}({block['input']}) {len(output)} chars")
            results.append({"type": "tool_result", "tool_use_id": block["id"], "content": output})

        # Tool results always go back in a user turn, one tool_result per tool_use, ids matched.
        messages.append({"role": "user", "content": results})

        # Checkpoint: the step is durable before the next API call can spend anything.
        if store and run_id:
            store.append_step(run_id, StepRow(step, "tool_use", content, results, resp.usage.input_tokens, resp.usage.output_tokens))
        if crash_after_step is not None and step >= crash_after_step:
            raise SystemExit(f"simulated crash after step {step}")

    # Guard 1: max steps.
    raise fail(f"no end_turn after {max_steps} steps; the model is looping")
