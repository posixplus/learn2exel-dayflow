"""CLI entrypoint.  `dayflow brief` runs the morning brief."""
from __future__ import annotations

import argparse
import os
import sys

import anthropic
from dotenv import load_dotenv

from dayflow import clock, loop, prompts
from dayflow.tools import definitions, use_store
from dayflow.store import Store


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="dayflow")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("brief", help="Generate the morning brief")
    b.add_argument("--verbose", "-v", action="store_true")
    b.add_argument("--max-steps", type=int, default=10)
    b.add_argument("--token-budget", type=int, default=60_000)
    b.add_argument("--read-only", action="store_true", help="Hand the model read tools only")
    b.add_argument("--crash-after", type=int, default=None, metavar="N", help="Simulate a crash after step N (lesson 83)")
    b.add_argument("--engine", choices=["raw", "sdk", "orch"], default="raw",
                   help="raw = the hand-written loop (81-83); sdk = Claude Agent SDK (84); orch = specialists + synthesiser (85)")
    r = sub.add_parser("resume", help="Continue the latest unfinished run (or a given id)")
    r.add_argument("run_id", nargs="?", default=None)
    r.add_argument("--verbose", "-v", action="store_true")
    sub.add_parser("runs", help="List recent runs")
    args = parser.parse_args(argv)

    store = Store()
    use_store(store)

    if os.environ.get("MOCK", "1") != "1":
        print("Real adapters arrive in lesson 87. Set MOCK=1 for now.", file=sys.stderr)
        return 2

    if args.cmd == "runs":
        rows = store._conn.execute("SELECT id, status, created_at, input_tokens, output_tokens, (SELECT COUNT(*) FROM steps WHERE run_id = runs.id) AS n FROM runs ORDER BY created_at DESC LIMIT 10").fetchall()
        for row in rows:
            print(f"{row['id']}  {row['status']:<8} {row['created_at']}  steps={row['n']}  tokens={row['input_tokens']}/{row['output_tokens']}")
        return 0

    if args.cmd == "resume":
        if args.run_id:
            run_id = args.run_id
        else:
            latest = store.latest_run(status="running") or store.latest_run(status="failed")
            run_id = latest["id"] if latest else None
        if not run_id:
            print("nothing to resume", file=sys.stderr)
            return 1
        try:
            result = loop.resume(run_id, store=store, verbose=args.verbose)
        except loop.LoopError as e:
            print(f"loop stopped: {e}", file=sys.stderr)
            return 1
        print(result.text)
        print(f"\n[resumed {run_id} · finished at step {result.steps} · {result.input_tokens} in / {result.output_tokens} out total]", file=sys.stderr)
        return 0

    if args.cmd == "brief" and args.engine == "orch":
        import asyncio
        import time
        from dayflow import orchestrate
        t0 = time.perf_counter()
        try:
            result = asyncio.run(orchestrate.run_brief(store=store, verbose=args.verbose))
        except RuntimeError as e:
            print(f"orchestration stopped: {e}", file=sys.stderr)
            return 1
        print(result.text)
        print(f"\n{orchestrate.cost_table(result)}", file=sys.stderr)
        print(f"[orch · {len(result.legs)} legs · ${result.cost_usd:.4f} · {time.perf_counter() - t0:.1f}s wall · run {result.run_id}]", file=sys.stderr)
        return 0

    if args.cmd == "brief" and args.engine == "sdk":
        import asyncio
        import time
        from dayflow import sdk_agent
        t0 = time.perf_counter()
        if args.crash_after is not None:
            print("--crash-after is a raw-loop flag; the SDK owns checkpoints now (resume with --engine sdk later)", file=sys.stderr)
        try:
            result = asyncio.run(sdk_agent.run_brief(
                store=store,
                kinds=("read",) if args.read_only else ("read", "write"),
                verbose=args.verbose,
                max_turns=args.max_steps,
            ))
        except RuntimeError as e:
            print(f"sdk run stopped: {e}", file=sys.stderr)
            return 1
        print(result.text)
        cost = f" · ${result.cost_usd:.4f}" if result.cost_usd is not None else ""
        print(
            f"\n[sdk · {result.num_turns} turns · {len(result.tool_calls)} tool calls · "
            f"{result.input_tokens} in (+{result.cache_read_tokens} cached) / {result.output_tokens} out{cost} · "
            f"{time.perf_counter() - t0:.1f}s wall · run {result.run_id} · session {result.session_id}]",
            file=sys.stderr,
        )
        return 0

    if args.cmd == "brief":
        try:
            result = loop.run(
                prompts.MORNING_BRIEF,
                prompts.brief_request(clock.today()),
                max_steps=args.max_steps,
                token_budget=args.token_budget,
                verbose=args.verbose,
                tools=definitions(("read",)) if args.read_only else None,
                store=store,
                crash_after_step=args.crash_after,
            )
        except loop.LoopError as e:
            print(f"loop stopped: {e}", file=sys.stderr)
            return 1
        except anthropic.AuthenticationError:
            print("auth failed: check ANTHROPIC_API_KEY in .env", file=sys.stderr)
            return 3
        except anthropic.BadRequestError as e:
            # Billing, bad model id, malformed request. Show the message, not the traceback.
            print(f"request rejected: {e.body.get('error', {}).get('message', e)}", file=sys.stderr)
            return 3
        except anthropic.APIConnectionError:
            print("could not reach api.anthropic.com: check your network", file=sys.stderr)
            return 3
        print(result.text)
        print(
            f"\n[{result.steps} steps · {len(result.tool_calls)} tool calls · "
            f"{result.input_tokens} in / {result.output_tokens} out · run {result.run_id}]",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
