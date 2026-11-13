"""CLI entrypoint.  `dayflow brief` runs the morning brief."""
from __future__ import annotations

import argparse
import json
import os
import sys

import anthropic
from dotenv import load_dotenv

from dayflow import clock, loop, permissions, prompts
from dayflow.tools import definitions, use_store
from dayflow.store import Store


def _pending_note(store: Store, run_id: str | None) -> None:
    """Tell the human, outside the model's text, what is waiting on them. The brief
    may mention it too; this line does not depend on the model remembering to."""
    pending = store.list_approvals(status="pending", run_id=run_id) if run_id else []
    if pending:
        ids = " ".join(row["id"] for row in pending)
        print(f"\n{len(pending)} write(s) waiting for approval: `dayflow approvals`, then `dayflow approve <id>` or `dayflow deny <id>`  [{ids}]",
              file=sys.stderr)


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
    b.add_argument("--approve", choices=["ask", "queue", "auto"], default=None,
                   help="lesson 86: how writes are decided. ask = at the terminal; queue = record and wait for "
                        "`dayflow approve`; auto = run without asking. Default: ask on a terminal, queue otherwise")
    r = sub.add_parser("resume", help="Continue the latest unfinished run (or a given id)")
    r.add_argument("run_id", nargs="?", default=None)
    r.add_argument("--verbose", "-v", action="store_true")
    sub.add_parser("runs", help="List recent runs")
    ap = sub.add_parser("approvals", help="List write proposals (lesson 86)")
    ap.add_argument("--all", action="store_true", help="Include decided ones: the audit trail")
    ok = sub.add_parser("approve", help="Run a queued proposal exactly as recorded")
    ok.add_argument("approval_id")
    no = sub.add_parser("deny", help="Reject a queued proposal")
    no.add_argument("approval_id")
    no.add_argument("-m", "--reason", default="")
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

    if args.cmd == "approvals":
        rows = store.list_approvals(status=None if args.all else "pending")
        if not rows:
            print("no pending approvals" if not args.all else "no approvals recorded")
            return 0
        for row in rows:
            stale = "  EXPIRED" if row["status"] == "pending" and store.is_expired(row) else ""
            who = f" by {row['decided_by']}" if row["decided_by"] else ""
            print(f"{row['id']}  {row['status']}{who}{stale}  {row['tool']}  run {row['run_id']}  {row['created_at']}")
            for k, v in json.loads(row["args"]).items():
                print(f"    {k}: {v!r}")
            if args.all and row["reason"]:
                print(f"    reason: {row['reason']}")
        return 0

    if args.cmd in ("approve", "deny"):
        try:
            if args.cmd == "approve":
                result = permissions.approve(store, args.approval_id)
                print(f"approved {args.approval_id}: {result['data']}")
            else:
                permissions.deny(store, args.approval_id, args.reason)
                print(f"denied {args.approval_id}")
        except permissions.ApprovalError as e:
            print(f"{e}", file=sys.stderr)
            return 1
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
            result = loop.resume(run_id, store=store, verbose=args.verbose,
                                 gate=permissions.gate_for("default", store=store, run_id=run_id))
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
                gate=permissions.gate_for(args.approve, store=store),
            ))
        except RuntimeError as e:
            print(f"sdk run stopped: {e}", file=sys.stderr)
            return 1
        print(result.text)
        _pending_note(store, result.run_id)
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
                gate=permissions.gate_for(args.approve, store=store),
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
        _pending_note(store, result.run_id)
        print(
            f"\n[{result.steps} steps · {len(result.tool_calls)} tool calls · "
            f"{result.input_tokens} in / {result.output_tokens} out · run {result.run_id}]",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
