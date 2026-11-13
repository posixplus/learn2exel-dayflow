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

Lesson 86: tool calls no longer go straight to dispatch(). They go through a
Gate (permissions.py), which lets reads through and stops writes until a rule
or a person says yes. The loop does not know which; it gets an envelope back
either way, and a refused write is just another ok=false result for the model.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from dayflow.permissions import Gate, Policy
from dayflow.store import Store, StepRow
from dayflow.tools import definitions

API_RETRIES = int(os.environ.get("DAYFLOW_API_RETRIES", "4"))
API_TIMEOUT_S = float(os.environ.get("DAYFLOW_API_TIMEOUT", "120"))
# Worth trying again later. Auth and bad-request errors are not in this list on purpose.
TRANSIENT_API_ERRORS = (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError)
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


def _cached_system(system: str) -> list[dict[str, Any]]:
    """Lesson 90: a cache breakpoint after the system prompt. Tools come before the
    system prompt in the cached prefix, so this one marker covers both."""
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def _cached_tail(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A second breakpoint on the last block of the last message, so the next step
    reads everything up to here (the inbox it already paid for) from the cache.
    Returns a copy: the stored history must not carry cache markers."""
    if not messages or not isinstance(messages[-1]["content"], list) or not messages[-1]["content"]:
        return messages
    last = dict(messages[-1])
    blocks = [dict(b) for b in last["content"]]
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    last["content"] = blocks
    return messages[:-1] + [last]


def _outcome(output: str) -> dict[str, Any]:
    """Lesson 89: what happened to a call, for graders. ok plus the refusal code, if any."""
    try:
        env = json.loads(output)
    except (TypeError, ValueError):
        return {"ok": None, "code": None, "attempts": 1}
    return {"ok": env.get("ok"), "code": (env.get("error") or {}).get("code"), "attempts": env.get("attempts", 1)}


def run(
    system: str,
    user: str,
    *,
    model: str = DEFAULT_MODEL,
    max_steps: int = 10,
    token_budget: int = 60_000,
    max_tokens: int = 4_096,
    client: anthropic.Anthropic | None = None,
    verbose: bool = False,
    tools: list[dict[str, Any]] | None = None,
    store: Store | None = None,
    kind: str = "brief",
    crash_after_step: int | None = None,   # test hook: simulate a crash after step N is committed
    gate: Gate | None = None,              # lesson 86: who decides whether a write runs
) -> RunResult:
    """Start a new run. With a store, every step is persisted and the run is resumable."""
    run_id = store.create_run(kind=kind, system=system, user_msg=user, model=model) if store else None
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    return _drive(messages, start_step=1, used_in=0, used_out=0, run_id=run_id, system=system, model=model,
                  max_steps=max_steps, token_budget=token_budget, max_tokens=max_tokens, client=client,
                  verbose=verbose, tools=tools, store=store, crash_after_step=crash_after_step, gate=gate)


def resume(
    run_id: str,
    *,
    store: Store,
    client: anthropic.Anthropic | None = None,
    max_steps: int = 10,
    token_budget: int = 60_000,
    max_tokens: int = 4_096,
    verbose: bool = False,
    tools: list[dict[str, Any]] | None = None,
    gate: Gate | None = None,
) -> RunResult:
    """Continue a run from its last committed step. Spends nothing on steps already done."""
    run = store.get_run(run_id)
    if run is None:
        raise LoopError(f"no run {run_id!r}")
    if run["kind"].endswith("-sdk"):
        # Lesson 84: the SDK session owns that run's history. Our steps rows are
        # an audit log, not a replayable transcript. Refuse rather than fake it.
        raise LoopError(f"run {run_id} was driven by the Agent SDK (session {run['session_id']}); "
                        "resume it through the SDK with --engine sdk (lesson 91), not the raw loop")
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
                  store=store, crash_after_step=None, gate=gate)


def _drive(
    messages: list[dict[str, Any]], *, start_step: int, used_in: int, used_out: int, run_id: str | None,
    system: str, model: str, max_steps: int, token_budget: int, max_tokens: int,
    client: anthropic.Anthropic | None, verbose: bool, tools: list[dict[str, Any]] | None,
    store: Store | None, crash_after_step: int | None, gate: Gate | None = None,
) -> RunResult:
    # Lesson 91: say what the SDK does instead of inheriting it. It already retries 429,
    # 5xx and connection errors with backoff; a second retry loop of ours on top would
    # multiply the waits. What we add is a ceiling on one call and a clean failure after.
    client = client or anthropic.Anthropic(max_retries=API_RETRIES, timeout=API_TIMEOUT_S)
    tools = definitions() if tools is None else tools
    # No gate passed means the safe default: writes are queued, never run unasked.
    # A caller that wants the old behaviour has to say Policy("allow") out loud.
    gate = gate or Gate(Policy(), store=store)
    gate.run_id, gate.store = gate.run_id or run_id, gate.store or store
    tool_log: list[dict[str, Any]] = []

    def fail(msg: str) -> LoopError:
        if store and run_id:
            store.fail_run(run_id, error=msg)
        return LoopError(msg)

    cache = os.environ.get("DAYFLOW_CACHE", "1") == "1"
    for step in range(start_step, max_steps + 1):
        t0 = time.perf_counter()
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=_cached_system(system) if cache else system,
                tools=tools,
                messages=_cached_tail(messages) if cache else messages,
            )
        except TRANSIENT_API_ERRORS as e:
            # The SDK's retries are spent. Steps 1..step-1 are committed, so nothing is lost:
            # mark the run failed with a reason and let `dayflow resume` pick it up at this step.
            raise fail(f"api unavailable at step {step} after {API_RETRIES} retries ({type(e).__name__}); "
                       f"nothing was lost, continue with `dayflow resume`") from e
        ms = int((time.perf_counter() - t0) * 1000)
        # Lesson 90: with prompt caching, input_tokens is only the UNCACHED part. The
        # budget must count every token the model read, or caching would quietly raise it.
        c_read = int(getattr(resp.usage, "cache_read_input_tokens", 0) or 0)
        c_write = int(getattr(resp.usage, "cache_creation_input_tokens", 0) or 0)
        step_in = resp.usage.input_tokens + c_read + c_write
        used_in += step_in
        used_out += resp.usage.output_tokens
        if store and run_id:
            store.record_event(run_id, step=step, kind="model", name=model, ms=ms, input_tokens=resp.usage.input_tokens,
                               output_tokens=resp.usage.output_tokens, cache_read=c_read, cache_write=c_write,
                               code=resp.stop_reason)
        if verbose:
            print(f"[step {step}] stop={resp.stop_reason} in={step_in} (cached {c_read}) out={resp.usage.output_tokens} {ms}ms")

        # Guard 2: token budget. Checked after every call, before acting on it.
        if used_in + used_out > token_budget:
            raise fail(f"token budget exceeded at step {step}: {used_in + used_out} > {token_budget}")

        content = _plain(resp.content)

        if resp.stop_reason == "end_turn":
            text = "".join(b["text"] for b in content if b.get("type") == "text")
            if store and run_id:
                store.append_step(run_id, StepRow(step, "end_turn", content, None, step_in, resp.usage.output_tokens))
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
            t1 = time.perf_counter()
            output = gate.call(block["name"], block["input"])     # was dispatch(); the gate calls it if allowed
            outcome = _outcome(output)
            tool_log.append({"step": step, "tool": block["name"], "args": block["input"], **outcome})
            if store and run_id:
                store.record_event(run_id, step=step, kind="tool", name=block["name"],
                                   ms=int((time.perf_counter() - t1) * 1000), ok=outcome["ok"], code=outcome["code"],
                                   attempts=outcome["attempts"])
            if verbose:
                print(f"    -> {block['name']}({block['input']}) {len(output)} chars")
            results.append({"type": "tool_result", "tool_use_id": block["id"], "content": output})

        # Tool results always go back in a user turn, one tool_result per tool_use, ids matched.
        messages.append({"role": "user", "content": results})

        # Checkpoint: the step is durable before the next API call can spend anything.
        if store and run_id:
            store.append_step(run_id, StepRow(step, "tool_use", content, results, step_in, resp.usage.output_tokens))
        if crash_after_step is not None and step >= crash_after_step:
            raise SystemExit(f"simulated crash after step {step}")

    # Guard 1: max steps.
    raise fail(f"no end_turn after {max_steps} steps; the model is looping")
