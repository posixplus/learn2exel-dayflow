"""CLI entrypoint.  `dayflow brief` runs the morning brief."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import sys

import anthropic
from dotenv import load_dotenv

from dayflow import clock, loop, oauth, permissions, prompts, tools
from dayflow.tools import definitions, use_store
from dayflow.store import Store


def _migrate_token(to: str) -> int:
    """Move the Google token between the file (lesson 87) and the keychain (capstone).
    Order matters: write the new copy, read it back, and only then delete the old one."""
    src = "file" if to == "keychain" else "keychain"
    os.environ["DAYFLOW_TOKEN_STORE"] = src
    token = oauth.load_token()
    if token is None:
        print(f"no token in the {src}; nothing to move. Sign in with: DAYFLOW_TOKEN_STORE={to} dayflow auth login", file=sys.stderr)
        return 1
    os.environ["DAYFLOW_TOKEN_STORE"] = to
    oauth.save_token(token)
    if oauth.load_token() != token:
        print(f"the copy in the {to} did not read back identically; the {src} copy was left in place", file=sys.stderr)
        return 3
    os.environ["DAYFLOW_TOKEN_STORE"] = src
    oauth.delete_token()
    os.environ["DAYFLOW_TOKEN_STORE"] = to
    print(f"Google token moved to {oauth.token_location()}; the {src} copy was deleted.")
    print(f"Now add this line to .env so every command looks there:\n  DAYFLOW_TOKEN_STORE={to}")
    return 0


def _use_real_google(store: Store) -> None:
    """Swap the fixture adapters for Gmail and Calendar. The tool surface follows the
    token: if the user did not grant the compose scope, draft_reply is not offered at
    all. A tool the token cannot back should not exist as far as the model is concerned."""
    from dayflow.adapters.google import bearer_http
    client = oauth.Client.from_env()
    token = oauth.valid_token(client)                     # raises if not signed in; refreshes if expired
    tools.use_google(bearer_http(lambda: oauth.valid_token(client).access_token), drafts=store)
    if not token.allows(oauth.SCOPE_COMPOSE):
        tools.REGISTRY.pop("draft_reply", None)


def _api_key_from_keychain() -> None:
    """Capstone: if the key is not in the environment or .env, look in the keychain. Silent
    when the keyring package is absent or the item is not there: .env stays the default."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    from dayflow import vault
    if not vault.available():
        return
    try:
        key = vault.get(vault.ANTHROPIC_KEY)
    except vault.VaultError:
        return
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key


def _secret(args: argparse.Namespace) -> int:
    import getpass
    from dayflow import vault
    try:
        if args.action == "set":
            value = getpass.getpass(f"{args.name} (input hidden): ").strip()     # never argv: that lands in shell history and `ps`
            if not value:
                print("nothing entered; nothing stored", file=sys.stderr)
                return 1
            vault.put(args.name, value)
            print(f"{args.name} stored in the system keychain. You can now remove it from .env.")
        elif args.action == "status":
            print(f"{args.name}: {'in the keychain' if vault.get(args.name) else 'not in the keychain'}"
                  f"{'; also set in the environment or .env' if os.environ.get('ANTHROPIC_API_KEY') and not vault.get(args.name) else ''}")
        else:
            print(f"{args.name}: {'deleted from the keychain' if vault.delete(args.name) else 'was not in the keychain'}")
    except vault.VaultError as e:
        print(f"keychain: {e}", file=sys.stderr)
        return 3
    return 0


def _schedule(args: argparse.Namespace) -> int:
    from pathlib import Path
    from dayflow import schedule
    workdir = Path.cwd()
    try:
        if args.action == "print":
            print(schedule.cron_line(at=args.at, workdir=workdir, mock=args.mock) if args.cron
                  else schedule.render(schedule.build_plist(at=args.at, workdir=workdir, mock=args.mock)))
        elif args.action == "install":
            path = schedule.install(at=args.at, workdir=workdir, mock=args.mock)
            print(f"installed: {path}\nruns `dayflow run` daily at {args.at} in {workdir} ({'MOCK=1, fixtures' if args.mock else 'MOCK=0, real Google'})"
                  f"\nlog: {schedule.log_path()}\ntry it now: dayflow schedule kick")
        elif args.action == "status":
            st = schedule.status()
            if not st["installed"]:
                print("not installed")
                return 1
            print(f"installed: {st['plist']}\ndaily at {st['at']}{' (MOCK=1)' if st.get('mock') else ''}\n"
                  f"loaded by launchd: {'yes' if st['loaded'] else 'NO: run `dayflow schedule install` again'}\nlog: {st['log']}")
            return 0 if st["loaded"] else 1
        elif args.action == "kick":
            schedule.kick()
            print(f"started through launchd. Watch: tail -f {schedule.log_path()}")
        else:
            print("uninstalled" if schedule.uninstall() else "was not installed")
    except schedule.ScheduleError as e:
        print(f"schedule: {e}", file=sys.stderr)
        return 1
    return 0


def _auth(args: argparse.Namespace) -> int:
    try:
        if args.action == "migrate":
            return _migrate_token(args.to)
        if args.action == "login":
            scopes = oauth.SCOPES_READ + ((oauth.SCOPE_COMPOSE,) if args.drafts else ())
            token = oauth.login(oauth.Client.from_env(), scopes=scopes)
            print("signed in. granted: " + ", ".join(s.rsplit("/", 1)[-1] for s in token.scopes))
            print(f"token stored at {oauth.token_location()}")
        elif args.action == "status":
            token = oauth.load_token()
            if token is None:
                print("not signed in")
                return 1
            state = "expired, will refresh on next use" if token.expired() else "valid"
            print(f"signed in. access token {state}. refresh token: {'yes' if token.refresh_token else 'NO'}")
            print("granted: " + ", ".join(s.rsplit("/", 1)[-1] for s in token.scopes))
            print("draft_reply: " + ("available" if token.allows(oauth.SCOPE_COMPOSE) else "not offered (run `dayflow auth login --drafts` to add it)"))
        else:
            token = oauth.load_token()
            if token is None:
                print("not signed in")
                return 0
            try:
                oauth.revoke(token)                       # tell Google first...
                print("revoked at Google")
            except oauth.OAuthError as e:
                print(f"could not revoke at Google ({e}); remove access at https://myaccount.google.com/permissions", file=sys.stderr)
            oauth.delete_token()                          # ...then forget it locally either way
            print("local token deleted")
    except oauth.OAuthError as e:
        print(f"google: {e}", file=sys.stderr)
        return 3
    return 0


def _link_note(text: str) -> None:
    """Lesson 88: checked in code, after the model is done. A phishing link the model
    was talked into repeating is caught here, not by asking the model to be careful."""
    from dayflow import security
    bad = security.flagged_links_in(text)
    if bad:
        print(f"\nWARNING: the brief repeats {len(bad)} link(s) from mail flagged as a possible injection. Do not open: "
              + " ".join(bad), file=sys.stderr)


def _pending_note(store: Store, run_id: str | None) -> None:
    """Tell the human, outside the model's text, what is waiting on them. The brief
    may mention it too; this line does not depend on the model remembering to."""
    pending = store.list_approvals(status="pending", run_id=run_id) if run_id else []
    if pending:
        ids = " ".join(row["id"] for row in pending)
        print(f"\n{len(pending)} write(s) waiting for approval: `dayflow approvals`, then `dayflow approve <id>` or `dayflow deny <id>`  [{ids}]",
              file=sys.stderr)


def _eval(args: argparse.Namespace) -> int:
    """Lesson 89. Costs real tokens: about 13k in / 2k out per trial on the fixtures."""
    from dayflow import evals
    try:
        if args.calibrate:
            c = evals.calibrate()
            for r in c["rows"]:
                votes = " ".join("pass" if v else "fail" for v in r["votes"])
                print(f"  {'agree   ' if r['agree'] else 'DISAGREE'} {r['id']:<3} label={'pass' if r['label'] else 'fail'}  judge: {votes}{'' if r['stable'] else '   <- disagrees with itself'}")
            print(f"judge {c['model']}, {c['repeats']} votes each: {c['agree']}/{c['n']} match the hand labels every time, {c['stable']}/{c['n']} stable")
            return 0 if c["agree"] == c["n"] else 1
        all_cases = evals.suites(with_judge=not args.no_judge)
        cases = list(all_cases.values()) if args.suite == "all" else [all_cases[args.suite]]
        kept: list | None = [] if args.traces else None
        if args.regrade:
            with open(args.regrade, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
            print(evals.render(evals.regrade(rows, all_cases, model=f"saved traces ({args.regrade})")))
            return 0
        report = evals.run_suite(
            cases, n=args.n, keep=kept, model=args.model or loop.DEFAULT_MODEL,
            on_trial=lambda cid, i, t: print(f"  {cid} trial {i}/{args.n}: " + (t.error or f"{len(t.tool_calls)} tool calls, {t.input_tokens} in / {t.output_tokens} out"), file=sys.stderr))
    except anthropic.AuthenticationError:
        print("auth failed: check ANTHROPIC_API_KEY in .env", file=sys.stderr)
        return 3
    print(evals.render(report))
    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            moved = evals.compare(json.load(f), report)
        print("\nvs " + args.compare + ":\n" + ("\n".join("  " + m for m in moved) if moved else "  no check moved"))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
    if args.traces:
        with open(args.traces, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(t) + "\n" for t in kept or [])
    return 0 if report["ok"] else 1


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
    au = sub.add_parser("auth", help="Google sign-in for MOCK=0 (lesson 87)")
    au.add_argument("action", choices=["login", "status", "logout", "migrate"])
    au.add_argument("--to", choices=["keychain", "file"], default="keychain", help="migrate: where the Google token should live")
    au.add_argument("--drafts", action="store_true", help="login: also ask for permission to create drafts (gmail.compose)")
    rn = sub.add_parser("run", help="One unattended run: writes briefs/<date>.md and notifies (capstone)")
    rn.add_argument("--force", action="store_true", help="run even if today's brief already exists")
    dr = sub.add_parser("doctor", help="Check everything an unattended run depends on (capstone)")
    dr.add_argument("--live", action="store_true", help="also make one small API call and one Google token refresh")
    sub.add_parser("health", help="Recent failures, cost and stale state, as plain sentences (capstone)")
    bk = sub.add_parser("backup", help="Copy the database to backups/ (capstone)")
    bk.add_argument("--keep", type=int, default=7)
    pr = sub.add_parser("prune", help="Forget the content of old runs; keep the numbers (capstone)")
    pr.add_argument("--days", type=int, default=30)
    sc = sub.add_parser("schedule", help="Run Dayflow daily with launchd (capstone)")
    sc.add_argument("action", choices=["install", "status", "uninstall", "print", "kick"])
    sc.add_argument("--at", default="07:30", help="HH:MM, 24-hour, local time")
    sc.add_argument("--mock", action="store_true", help="schedule fixture runs (MOCK=1), for a dry run of the schedule itself")
    sc.add_argument("--cron", action="store_true", help="print: show a crontab line instead of a launchd plist")
    se = sub.add_parser("secret", help="Keep the API key in the system keychain (capstone)")
    se.add_argument("action", choices=["set", "status", "delete"])
    se.add_argument("name", nargs="?", default="anthropic-api-key", choices=["anthropic-api-key"])
    mc = sub.add_parser("mcp", help="Serve Dayflow's tools over MCP on stdio (lesson 92)")
    mc.add_argument("--read-only", action="store_true", help="expose the read tools only")
    tr = sub.add_parser("trace", help="Show one run step by step: time, tokens, cost (lesson 90)")
    tr.add_argument("run_id", nargs="?", default=None, help="default: the latest run")
    st = sub.add_parser("stats", help="Recent runs: failures, latency, tokens, cost (lesson 90)")
    st.add_argument("--last", type=int, default=50)
    ev = sub.add_parser("eval", help="Run the eval suites against the real model (lesson 89)")
    ev.add_argument("--suite", choices=["clean", "redteam", "degraded", "all"], default="all")
    ev.add_argument("-n", type=int, default=3, help="trials per case (default 3; use 10+ before believing a rate)")
    ev.add_argument("--model", default=None, help="model under test (default: DAYFLOW_MODEL)")
    ev.add_argument("--no-judge", action="store_true", help="code graders only; no model-graded check")
    ev.add_argument("--out", default=None, metavar="FILE", help="write the report as JSON")
    ev.add_argument("--compare", default=None, metavar="FILE", help="diff against a saved report")
    ev.add_argument("--traces", default=None, metavar="FILE", help="write every trial (text, tool calls, failed checks) as JSON lines")
    ev.add_argument("--regrade", default=None, metavar="FILE", help="re-apply the graders to saved traces; runs no agent")
    ev.add_argument("--calibrate", action="store_true", help="check the judge against hand labels and exit")
    args = parser.parse_args(argv)

    _api_key_from_keychain()

    if args.cmd == "eval":
        return _eval(args)

    if args.cmd == "auth":
        return _auth(args)

    if args.cmd == "secret":
        return _secret(args)

    if args.cmd == "schedule":
        return _schedule(args)

    store = Store()
    use_store(store)

    if os.environ.get("MOCK", "1") != "1" and args.cmd in ("brief", "resume", "approve", "mcp", "run"):
        # Lesson 87: real Gmail and Calendar. Same tools; different adapters behind them.
        try:
            _use_real_google(store)
        except oauth.OAuthError as e:
            print(f"google: {e}", file=sys.stderr)
            if args.cmd == "run":                     # nobody is reading stderr at 07:30
                from dayflow import ship
                ship.notify("Dayflow failed", f"Google: {str(e)[:160]}")
            return 3

    if args.cmd in ("run", "doctor", "health", "backup", "prune"):
        from dayflow import ship
        if args.cmd == "run":
            out = ship.run_scheduled(store, force=args.force)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"{stamp} dayflow run: {('ok', 'FAILED', 'skipped')[out.code]}: {out.message}")
            return out.code
        if args.cmd == "doctor":
            report = ship.doctor(store, live=args.live)
            print(report.render())
            return 0 if report.ok else 1
        if args.cmd == "health":
            alerts = ship.health(store)
            print("\n".join(f"- {a}" for a in alerts) if alerts else "nothing to report")
            return 1 if alerts else 0
        if args.cmd == "backup":
            print(f"backed up to {ship.backup(store, keep=args.keep)}")
            return 0
        done = ship.prune(store, days=args.days)
        print(f"pruned content of {done['runs']} run(s) older than {args.days} days: {done['steps']} step(s), {done['approvals']} decided approval(s). Numbers kept.")
        return 0

    if args.cmd == "mcp":
        import anyio
        from dayflow import mcp_server
        anyio.run(lambda: mcp_server.serve_stdio(store, read_only=args.read_only))
        return 0

    if args.cmd in ("trace", "stats"):
        from dayflow import observe
        if args.cmd == "stats":
            print(observe.stats(store, last=args.last))
            return 0
        latest = store.latest_run()
        run_id = args.run_id or (latest["id"] if latest else None)
        if not run_id or store.get_run(run_id) is None:
            print("no such run" if args.run_id else "no runs recorded", file=sys.stderr)
            return 1
        print(observe.trace(store, run_id))
        return 0

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
            where = f"  [{row['backend']}]" if row["backend"] else ""
            print(f"{row['id']}  {row['status']}{who}{stale}  {row['tool']}{where}  run {row['run_id']}  {row['created_at']}")
            for k, v in json.loads(row["args"]).items():
                print(f"    {k}: {v!r}")
            if row["reason"] and (args.all or row["status"] == "pending"):
                print(f"    note: {row['reason']}")
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
        _link_note(result.text)
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
        _link_note(result.text)
        _pending_note(store, result.run_id)
        from dayflow import observe
        spent = observe.run_cost(store, result.run_id)
        price = f" · ${spent['usd']:.4f}" if spent["usd"] is not None else " · cost n/a (no price for this model)"
        print(
            f"\n[{result.steps} steps · {len(result.tool_calls)} tool calls · "
            f"{result.input_tokens} in ({spent['cache_read']} cached) / {result.output_tokens} out{price} · "
            f"run {result.run_id} · `dayflow trace` for the breakdown]",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
