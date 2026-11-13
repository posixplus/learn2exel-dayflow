"""Running Dayflow unattended, and looking after it. Capstone.

Lessons 81 to 92 built an agent you run by typing a command and reading the result.
A scheduled agent has neither of those: nobody starts it and nobody is watching when
it finishes. This file is what changes because of that.

    run_scheduled   one unattended run: lock, once a day, resume, file, notify
    doctor          before you trust it: is everything it needs in place?
    health          after it has been running: is it failing, slow or expensive?
    backup, prune   the database holds your mail. Keep a copy; do not keep it forever.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from dayflow import clock, loop, observe, oauth, permissions, prompts
from dayflow.store import Store

Notifier = Callable[[str, str], None]
OK, FAILED, SKIPPED = 0, 1, 2
LOCK_STALE_S = 30 * 60


# ── notification ─────────────────────────────────────────────────────────────
def notify(title: str, message: str, *, run: Callable[[list[str]], Any] = lambda c: subprocess.run(c, capture_output=True, timeout=10)) -> None:
    """A macOS notification, or a line on stderr anywhere else. Never raises: failing to
    announce a brief must not fail the brief."""
    print(f"[{title}] {message}", file=sys.stderr)
    if sys.platform != "darwin":
        return
    def q(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        run(["osascript", "-e", f'display notification "{q(message)}" with title "{q(title)}"'])
    except Exception:  # noqa: BLE001
        pass


# ── the scheduled run ────────────────────────────────────────────────────────
@dataclass
class Outcome:
    code: int
    message: str
    path: Path | None = None
    run_id: str | None = None


def brief_dir() -> Path:
    return Path(os.environ.get("DAYFLOW_BRIEF_DIR", "briefs")).expanduser()


class Lock:
    """One run at a time. launchd can fire a missed job on wake while a manual run is going;
    two runs would both pay, and both propose the same writes."""
    def __init__(self, path: Path, *, now: Callable[[], float] = time.time) -> None:
        self.path, self._now, self.held = path, now, False

    def acquire(self) -> bool:
        if self.path.exists() and self._now() - self.path.stat().st_mtime > LOCK_STALE_S:
            self.path.unlink()                       # the holder died without cleaning up
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        self.held = True
        return True

    def release(self) -> None:
        if self.held and self.path.exists():
            self.path.unlink()
        self.held = False


def _resumable_today(store: Store, today_utc: str) -> str | None:
    """Lesson 91 left a run that died on an API outage 'failed' and resumable. An unattended
    job should pick that up on its next start, not pay for steps 1 and 2 again."""
    run = store.latest_run()
    if (run and run["kind"] == "brief" and run["status"] == "failed" and run["created_at"][:10] == today_utc
            and "dayflow resume" in (run["error"] or "")):
        return run["id"]
    return None


def _document(text: str, *, store: Store, run_id: str, alerts: list[str]) -> str:
    spent = observe.run_cost(store, run_id)
    run = store.get_run(run_id)
    pending = store.list_approvals(status="pending", run_id=run_id)
    parts = [text.rstrip(), ""]
    if pending:
        parts += ["---", f"**{len(pending)} write(s) waiting for your approval.** Nothing was changed.", ""]
        parts += [f"- `{'MOCK=0 ' if p['backend'] == 'google' else ''}dayflow approve {p['id']}`  ({p['tool']})" for p in pending]
        parts += ["", "See them with `dayflow approvals`.", ""]
    if alerts:
        parts += ["---", "**Dayflow health**", ""] + [f"- {a}" for a in alerts] + [""]
    cost = f"${spent['usd']:.4f}" if spent["usd"] is not None else "cost n/a"
    parts += ["---", f"<sub>run {run_id} · {run['model']} · {run['input_tokens']} in / {run['output_tokens']} out · {cost} · "
                     f"`dayflow trace {run_id}`</sub>", ""]
    return "\n".join(parts)


def run_scheduled(store: Store, *, out_dir: Path | None = None, force: bool = False, notifier: Notifier = notify,
                  client: Any = None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> Outcome:
    """One unattended run. Exit codes: 0 a brief was written, 1 it failed, 2 nothing to do."""
    out_dir = out_dir or brief_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    today = clock.today()
    target = out_dir / f"{today}.md"
    if target.exists() and not force:
        return Outcome(SKIPPED, f"today's brief already exists: {target}", target)

    lock = Lock(out_dir / ".dayflow.lock")
    if not lock.acquire():
        return Outcome(SKIPPED, "another Dayflow run holds the lock")
    try:
        try:
            backup(store, keep=7)
        except Exception as e:  # noqa: BLE001 - a failed backup is reported by health(), it does not stop the brief
            print(f"backup failed: {type(e).__name__}: {e}", file=sys.stderr)
        gate_for = lambda rid=None: permissions.gate_for("queue", store=store, run_id=rid)   # noqa: E731 - nobody is at a terminal
        try:
            resumable = _resumable_today(store, now().strftime("%Y-%m-%d"))
            if resumable:
                result = loop.resume(resumable, store=store, client=client, gate=gate_for(resumable))
            else:
                result = loop.run(prompts.MORNING_BRIEF, prompts.brief_request(today), store=store, client=client,
                                  gate=gate_for())
        except Exception as e:  # noqa: BLE001 - unattended: every failure becomes a message someone will see
            reason = str(e) if isinstance(e, (loop.LoopError, oauth.OAuthError)) else f"{type(e).__name__}: {e}"
            if "authentication" in reason.lower() or "api_key" in reason.lower():
                reason = "no working ANTHROPIC_API_KEY (not set, or rejected)"   # the SDK's wording helps nobody at 07:30
            notifier("Dayflow failed", f"{reason[:180]}. Run: dayflow doctor")
            return Outcome(FAILED, reason)

        alerts = health(store, now=now())
        tmp = target.with_suffix(".md.tmp")
        tmp.write_text(_document(result.text, store=store, run_id=result.run_id, alerts=alerts), encoding="utf-8")
        os.replace(tmp, target)                      # never leave half a brief where a reader will find it
        waiting = len(store.list_approvals(status="pending", run_id=result.run_id))
        bits = [f"Brief ready: {target.name}"] + ([f"{waiting} waiting for approval"] if waiting else []) \
            + ([f"{len(alerts)} health alert(s)"] if alerts else [])
        notifier("Dayflow", " · ".join(bits))
        return Outcome(OK, f"wrote {target}", target, result.run_id)
    finally:
        lock.release()


# ── health: Day 2 ────────────────────────────────────────────────────────────
def health(store: Store, *, now: datetime | None = None, cost_alert: float | None = None) -> list[str]:
    """What a person would want to be told, as plain sentences. Empty list: nothing to say.
    Deliberately few rules, each one something you would act on."""
    now = now or datetime.now(timezone.utc)
    cost_alert = cost_alert if cost_alert is not None else float(os.environ.get("DAYFLOW_COST_ALERT", "0.15"))
    alerts: list[str] = []
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
    runs = store._conn.execute("SELECT * FROM runs WHERE kind = 'brief' AND created_at >= ? ORDER BY created_at", (week_ago,)).fetchall()

    failed = [r for r in runs if r["status"] == "failed"]
    if len(failed) >= 2:
        why = (failed[-1]["error"] or "unknown").split(";")[0][:90]
        alerts.append(f"{len(failed)} of the last {len(runs)} briefs failed in 7 days. Latest: {why}")

    stuck = [r for r in runs if r["status"] == "running" and now - datetime.fromisoformat(r["updated_at"]) > timedelta(hours=1)]
    if stuck:
        alerts.append(f"{len(stuck)} run(s) still marked running after an hour: crashed. `dayflow resume {stuck[-1]['id']}`")

    done = [r for r in runs if r["status"] == "done"]
    older = store._conn.execute("SELECT COUNT(*) FROM runs WHERE kind = 'brief' AND status = 'done' AND created_at < ?", (week_ago,)).fetchone()[0]
    if runs and not done and older:
        alerts.append("no brief has succeeded in 7 days")

    costs = [c for c in (observe.run_cost(store, r["id"])["usd"] for r in done) if c is not None]
    if costs:
        last, earlier = costs[-1], costs[:-1]
        if last > cost_alert:
            alerts.append(f"the last brief cost ${last:.4f}, above the ${cost_alert:.2f} alert (DAYFLOW_COST_ALERT)")
        elif len(earlier) >= 4 and last > 2 * statistics.median(earlier):
            alerts.append(f"the last brief cost ${last:.4f}, more than twice the 7-day median ${statistics.median(earlier):.4f}")

    expired = [a for a in store.list_approvals(status="pending") if store.is_expired(a, now=now)]
    if expired:
        alerts.append(f"{len(expired)} approval(s) expired unanswered. `dayflow approvals`")

    if os.environ.get("MOCK", "1") != "1":
        try:
            token = oauth.load_token()
        except oauth.OAuthError as e:
            token = None
            alerts.append(f"Google token unreadable: {e}")
        if token and token.obtained_at and now.timestamp() - token.obtained_at > 6 * 86400:
            alerts.append("Google sign-in is over 6 days old. Apps in Testing mode are signed out after 7: `dayflow auth login`")
    return alerts


# ── backup and retention ─────────────────────────────────────────────────────
def backup_dir() -> Path:
    return Path(os.environ.get("DAYFLOW_BACKUP_DIR", "backups")).expanduser()


def backup(store: Store, *, keep: int = 7, out_dir: Path | None = None, today: str | None = None) -> Path:
    """A consistent copy using SQLite's online backup API (safe while the database is in use;
    copying the file is not). One file per day, newest `keep` kept, mode 600: it holds your mail."""
    out_dir = out_dir or backup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"dayflow-{today or datetime.now().strftime('%Y%m%d')}.db"
    tmp = dest.with_suffix(".db.tmp")
    if tmp.exists():
        tmp.unlink()
    target = sqlite3.connect(tmp)
    try:
        store._conn.backup(target)
    finally:
        target.close()
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    for old in sorted(out_dir.glob("dayflow-*.db"))[:-keep]:
        old.unlink()
    return dest


def prune(store: Store, *, days: int = 30, now: datetime | None = None) -> dict[str, int]:
    """Forget the CONTENT of runs older than `days`: the step transcripts (which contain the
    mail the model read) and the brief text. The run rows, token counts and events stay, so
    stats and cost history survive. Pruned runs cannot be resumed or replayed."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat(timespec="seconds")
    with store.tx() as c:
        old = [r["id"] for r in c.execute("SELECT id FROM runs WHERE created_at < ?", (cutoff,))]
        steps = 0
        for rid in old:
            steps += c.execute("DELETE FROM steps WHERE run_id = ?", (rid,)).rowcount
            c.execute("UPDATE runs SET final_text = NULL, system = '', user_msg = '' WHERE id = ?", (rid,))
        approvals = c.execute("DELETE FROM approvals WHERE status != 'pending' AND created_at < ?", (cutoff,)).rowcount
    store._conn.execute("VACUUM")                    # deleted rows stay readable in the file until this
    return {"runs": len(old), "steps": steps, "approvals": approvals}


# ── doctor: preflight ────────────────────────────────────────────────────────
@dataclass
class Check:
    level: str            # ok | warn | FAIL
    name: str
    detail: str
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, level: str, name: str, detail: str, fix: str = "") -> None:
        self.checks.append(Check(level, name, detail, fix))

    @property
    def ok(self) -> bool:
        return not any(c.level == "FAIL" for c in self.checks)

    def render(self) -> str:
        lines = []
        for c in self.checks:
            lines.append(f"  {c.level:<4} {c.name:<16} {c.detail}")
            if c.fix and c.level != "ok":
                lines.append(f"       {'':<16} fix: {c.fix}")
        fails = sum(c.level == "FAIL" for c in self.checks)
        warns = sum(c.level == "warn" for c in self.checks)
        lines.append("\nready" + (f", {warns} warning(s)" if warns else "") if not fails else f"\nNOT ready: {fails} problem(s)")
        return "\n".join(lines)


def doctor(store: Store, *, live: bool = False, client: Any = None, schedule_status: Callable[[], dict] | None = None) -> Report:
    """Everything an unattended run depends on, checked before 07:30 instead of at 07:30.
    Never prints a secret: only whether one is present and where it came from."""
    from dayflow import schedule, vault
    r = Report()
    mock = os.environ.get("MOCK", "1") == "1"

    v = sys.version_info
    r.add("ok" if v >= (3, 11) else "FAIL", "python", f"{v.major}.{v.minor}.{v.micro} at {sys.executable}", "install Python 3.11 or newer")

    key_env = bool(os.environ.get("ANTHROPIC_API_KEY"))
    key_vault = False
    if not key_env and vault.available():
        try:
            key_vault = bool(vault.get(vault.ANTHROPIC_KEY))
        except vault.VaultError:
            key_vault = False
    if key_env or key_vault:
        r.add("ok", "api key", "present, from " + ("the environment or .env" if key_env else "the keychain"))
    else:
        r.add("FAIL", "api key", "ANTHROPIC_API_KEY is not set", "put it in .env, or: dayflow secret set anthropic-api-key")

    model = loop.DEFAULT_MODEL
    r.add("ok" if model in observe.PRICES else "warn", "model", model + ("" if model in observe.PRICES else ": no price in observe.PRICES"),
          "add a Price for it, or cost will show as n/a and cost alerts will not fire")

    if live and (key_env or key_vault):
        try:
            import anthropic
            c = client or anthropic.Anthropic(max_retries=1, timeout=30)
            c.messages.create(model=model, max_tokens=64, messages=[{"role": "user", "content": "Reply with the word ok."}])
            r.add("ok", "api call", "one small request succeeded")
        except Exception as e:  # noqa: BLE001
            r.add("FAIL", "api call", f"{type(e).__name__}: {str(e)[:120]}", "check the key, billing and your network")

    r.add("ok", "mode", "MOCK=1: fixtures, no Google access" if mock else "MOCK=0: real Gmail and Calendar")
    if not mock:
        store_kind = oauth.token_store()
        try:
            token = oauth.load_token()
        except oauth.OAuthError as e:
            token = None
            r.add("FAIL", "google token", str(e), "dayflow auth login")
        else:
            if token is None:
                r.add("FAIL", "google token", "not signed in", "dayflow auth login")
            else:
                scopes = ", ".join(s.rsplit("/", 1)[-1] for s in token.scopes)
                r.add("ok" if token.refresh_token else "FAIL", "google token",
                      f"in the {store_kind}; refresh token {'yes' if token.refresh_token else 'NO'}; scopes: {scopes}", "dayflow auth login")
                if store_kind == "file":
                    r.add("warn", "token storage", "a file at mode 600: the minimum", "pip install keyring && dayflow auth migrate --to keychain")
                if token.obtained_at and time.time() - token.obtained_at > 6 * 86400:
                    r.add("warn", "google sign-in", "over 6 days old; Testing-mode apps are signed out after 7", "dayflow auth login")
                if live and token.refresh_token:
                    try:
                        oauth.refresh(oauth.Client.from_env(), token)
                        r.add("ok", "google refresh", "Google issued a fresh access token")
                    except oauth.OAuthError as e:
                        r.add("FAIL", "google refresh", str(e), "dayflow auth login")

    try:
        verdict = store._conn.execute("PRAGMA integrity_check").fetchone()[0]
        store._conn.execute("CREATE TABLE IF NOT EXISTS _doctor (x)"); store._conn.execute("DROP TABLE _doctor")
        r.add("ok" if verdict == "ok" else "FAIL", "database", f"{store.path}: integrity {verdict}, writable", "restore from backups/ (see docs/RUNBOOK.md)")
    except sqlite3.Error as e:
        r.add("FAIL", "database", f"{store.path}: {e}", "check the file's permissions, or restore from backups/")

    for label, d in (("brief folder", brief_dir()), ("backup folder", backup_dir())):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write-test"; probe.write_text("x"); probe.unlink()
            r.add("ok", label, str(d.resolve()))
        except OSError as e:
            r.add("FAIL", label, f"{d}: {e}", "create it or point DAYFLOW_BRIEF_DIR / DAYFLOW_BACKUP_DIR somewhere writable")

    free = shutil.disk_usage(Path.cwd()).free / 1e9
    r.add("ok" if free > 1 else "warn", "disk", f"{free:.0f} GB free", "free some space; SQLite fails badly on a full disk")

    st = (schedule_status or schedule.status)()
    if st["installed"] and st["loaded"]:
        r.add("ok", "schedule", f"daily at {st['at']}{' (MOCK=1)' if st.get('mock') else ''}; log {st['log']}")
    elif st["installed"]:
        r.add("FAIL", "schedule", "the plist exists but launchd has not loaded it", "dayflow schedule install --at " + (st["at"] or "07:30"))
    else:
        r.add("warn", "schedule", "not installed: Dayflow only runs when you type a command", "dayflow schedule install --at 07:30")

    for a in health(store):
        r.add("warn", "health", a)
    return r
