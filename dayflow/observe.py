"""Observability and cost. Lesson 90.

Three questions you cannot answer about an agent without this file:
    what did that run actually do, and where did the time go?   trace()
    what does a run cost, and what is that made of?             cost(), trace()
    is it getting slower, dearer or flakier?                    stats()

Everything here reads two tables the loop already writes: runs (lesson 83) and
events (this lesson: one row per model call and per tool call, with wall time,
tokens split into uncached / cache-read / cache-write, and the outcome).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dayflow.store import Store


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""
    input: float
    output: float
    cache_read: float
    cache_write: float


# Standard-tier list prices as published in the course's model registry (ai-learn2exel
# src/data/models.ts), Nov 2026. Cache writes are billed at 1.25x input (5-minute cache).
# Prices change: check the vendor's pricing page before trusting a dollar figure from here.
# Not modelled: Haiku 5.5's higher tier for prompts over 100k tokens (Dayflow's are ~11k).
PRICES: dict[str, Price] = {
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.10, 2.50),
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, 0.125),
    "claude-opus-5-5": Price(4.00, 20.00, 0.20, 5.00),
}


def cost(model: str, *, input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0, cache_write: int = 0) -> float | None:
    """Dollars for one call or one run. None for a model with no price: an unknown
    cost must never print as $0.00."""
    p = PRICES.get(model)
    if p is None:
        return None
    return (input_tokens * p.input + output_tokens * p.output + cache_read * p.cache_read + cache_write * p.cache_write) / 1_000_000


def _money(x: float | None) -> str:
    return "   n/a" if x is None else f"${x:.4f}"


def run_cost(store: Store, run_id: str) -> dict[str, Any]:
    rows = [e for e in store.events(run_id) if e["kind"] == "model"]
    total: float | None = 0.0
    for e in rows:
        c = cost(e["name"], input_tokens=e["input_tokens"], output_tokens=e["output_tokens"],
                 cache_read=e["cache_read"], cache_write=e["cache_write"])
        total = None if c is None or total is None else total + c
    return {"usd": total, "uncached": sum(e["input_tokens"] for e in rows), "cache_read": sum(e["cache_read"] for e in rows),
            "cache_write": sum(e["cache_write"] for e in rows), "output": sum(e["output_tokens"] for e in rows),
            "model_ms": sum(e["ms"] for e in rows), "calls": len(rows)}


def trace(store: Store, run_id: str) -> str:
    """One run as a waterfall: each model call, then the tool calls it asked for."""
    run = store.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    events = store.events(run_id)
    if not events:
        return f"run {run_id}: no events recorded (it predates lesson 90, or was driven by the SDK)"
    lines = [f"run {run_id} · {run['kind']} · {run['status']} · {run['created_at']}",
             f"{'step':<5}{'what':<34}{'ms':>7}{'in':>7}{'cached':>8}{'write':>7}{'out':>6}{'cost':>10}  outcome"]
    for e in events:
        if e["kind"] == "model":
            c = cost(e["name"], input_tokens=e["input_tokens"], output_tokens=e["output_tokens"],
                     cache_read=e["cache_read"], cache_write=e["cache_write"])
            lines.append(f"{e['step']:<5}{e['name']:<34}{e['ms']:>7}{e['input_tokens']:>7}{e['cache_read']:>8}{e['cache_write']:>7}"
                         f"{e['output_tokens']:>6}{_money(c):>10}  {e['code']}")
        else:
            outcome = "ok" if e["ok"] else (e["code"] or "error")
            lines.append(f"{'':<5}{'  -> ' + e['name']:<34}{e['ms']:>7}{'':>7}{'':>8}{'':>7}{'':>6}{'':>10}  {outcome}")
    t = run_cost(store, run_id)
    tool_ms = sum(e["ms"] for e in events if e["kind"] == "tool")
    lines.append(f"{'':<5}{'total':<34}{t['model_ms'] + tool_ms:>7}{t['uncached']:>7}{t['cache_read']:>8}{t['cache_write']:>7}{t['output']:>6}{_money(t['usd']):>10}")
    lines.append(f"model time {t['model_ms']} ms ({t['model_ms'] * 100 // max(1, t['model_ms'] + tool_ms)}%) · tool time {tool_ms} ms")
    if run["error"]:
        lines.append(f"error: {run['error']}")
    return "\n".join(lines)


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank. With 5 runs the p95 is simply the worst one, which is the honest answer."""
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(p / 100 * len(s) + 0.5) - 1))]


def stats(store: Store, *, last: int = 50) -> str:
    """The last N runs, grouped by kind and model: how many, how many failed, how long, how much."""
    runs = store._conn.execute("SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (last,)).fetchall()
    if not runs:
        return "no runs recorded"
    groups: dict[tuple[str, str], list[Any]] = {}
    for r in runs:
        groups.setdefault((r["kind"], r["model"]), []).append(r)
    lines = [f"last {len(runs)} run(s)",
             f"{'kind':<10}{'model':<22}{'runs':>5}{'failed':>7}{'p50 s':>7}{'p95 s':>7}{'in/run':>8}{'cached%':>8}{'out/run':>8}{'$/run':>9}{'$ total':>9}"]
    reasons: dict[str, int] = {}
    for (kind, model), rs in sorted(groups.items()):
        costs, secs, ins, outs, cached = [], [], [], [], []
        for r in rs:
            t = run_cost(store, r["id"])
            if t["calls"]:
                costs.append(t["usd"])
                secs.append(t["model_ms"] / 1000)
                ins.append(t["uncached"] + t["cache_read"] + t["cache_write"])
                cached.append(t["cache_read"])
                outs.append(t["output"])
            if r["status"] == "failed":
                key = (r["error"] or "unknown").split(":")[0].split(" at step")[0][:48]
                reasons[key] = reasons.get(key, 0) + 1
        n = len(costs)
        known = [c for c in costs if c is not None]
        per_run = _money(sum(known) / len(known)) if known and len(known) == n else "   n/a"
        total = _money(sum(known)) if known and len(known) == n else "   n/a"
        lines.append(f"{kind:<10}{model:<22}{len(rs):>5}{sum(r['status'] == 'failed' for r in rs):>7}"
                     f"{percentile(secs, 50):>7.1f}{percentile(secs, 95):>7.1f}"
                     f"{(sum(ins) // n if n else 0):>8}{(sum(cached) * 100 // max(1, sum(ins))):>7}%{(sum(outs) // n if n else 0):>8}{per_run:>9}{total:>9}")
    running = sum(r["status"] == "running" for r in runs)
    if running:
        lines.append(f"{running} run(s) still marked running: crashed or in progress (`dayflow resume`)")
    for why, k in sorted(reasons.items(), key=lambda kv: -kv[1]):
        lines.append(f"failed x{k}: {why}")
    return "\n".join(lines)
