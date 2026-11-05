"""Compaction. Lesson 83, part 3.

A long run re-sends its whole history every step. Past a threshold, replace the
oldest tool exchanges with a summary that keeps what the next step needs
(decisions, pending items, ids) and drops the raw payloads.

Two rules that make this safe:
  1. Never compact the first user message (the task) or the most recent
     assistant/user pair (the model needs its own last move intact).
  2. The summary is a *user* turn in history, labelled as such, so the model
     reads it as context, not as something it said.

Dayflow's briefs are 3 steps, so this never fires in practice. It exists so
that lesson 85 (orchestration) and lesson 91 (long-running agents) have a
tested primitive to reach for.
"""
from __future__ import annotations

import json
from typing import Any


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Cheap estimate: ~4 chars per token. Good enough to decide when to compact."""
    return len(json.dumps(messages)) // 4


def _summarise_exchange(assistant: dict[str, Any], results: dict[str, Any]) -> str:
    """Deterministic one-line summary of a tool exchange. No model call: it is
    the *shape* we keep (what was called, whether it worked), not the content."""
    lines = []
    calls = {b["id"]: b for b in assistant["content"] if b.get("type") == "tool_use"}
    for r in results["content"]:
        if r.get("type") != "tool_result":
            continue
        call = calls.get(r["tool_use_id"], {})
        try:
            env = json.loads(r["content"])
            status = "ok" if env.get("ok") else f"error:{env.get('error', {}).get('code')}"
            n = len(env["data"]) if env.get("ok") and isinstance(env.get("data"), list) else None
        except Exception:
            status, n = "?", None
        shape = f" ({n} items)" if n is not None else ""
        lines.append(f"- {call.get('name', '?')}({json.dumps(call.get('input', {}))}) -> {status}{shape}")
    return "\n".join(lines)


def compact(messages: list[dict[str, Any]], *, keep_last: int = 1, threshold_tokens: int = 40_000) -> list[dict[str, Any]]:
    """Return a new message list. Untouched when under threshold.

    Layout in:  [user task] [a1][u1] [a2][u2] ... [aN][uN]
    Layout out: [user task] [user summary of a1..a(N-keep)] [a(N-keep+1)][u..] ... [aN][uN]
    """
    if estimate_tokens(messages) < threshold_tokens:
        return messages
    task, rest = messages[0], messages[1:]
    # rest is alternating assistant/user pairs
    pairs = [(rest[i], rest[i + 1]) for i in range(0, len(rest) - 1, 2)]
    tail = rest[len(pairs) * 2:]           # a dangling assistant turn, if any
    if len(pairs) <= keep_last:
        return messages
    old, recent = pairs[:-keep_last], pairs[-keep_last:]
    summary = "Earlier in this run (compacted):\n" + "\n".join(_summarise_exchange(a, u) for a, u in old)
    out: list[dict[str, Any]] = [task, {"role": "user", "content": [{"type": "text", "text": summary}]}]
    for a, u in recent:
        out.extend([a, u])
    out.extend(tail)
    return out
