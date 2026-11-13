"""Capstone: the unattended run, the schedule, the keychain, doctor, health, backup, prune.

Nothing here calls launchctl, osascript, a keychain or the network. Each of those sits
behind a callable the test replaces, which is also why the same code can be checked on Linux.
"""
from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest

from dayflow import loop, main, oauth, schedule, ship, tools, vault
from dayflow.store import Store

NOW = datetime(2026, 9, 7, 11, 30, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MOCK", "1")
    monkeypatch.delenv("DAYFLOW_BRIEF_DIR", raising=False)
    monkeypatch.delenv("DAYFLOW_BACKUP_DIR", raising=False)
    s = Store(tmp_path / "d.db")
    previous = tools.tasks
    tools.use_store(s)
    yield s
    tools.tasks = previous
    s.close()


def _resp(stop, content, i=100, o=50):
    return SimpleNamespace(stop_reason=stop, content=content, usage=SimpleNamespace(input_tokens=i, output_tokens=o))


def _use(name, args, n=1):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=f"t{n}")


READ = _resp("tool_use", [_use("read_tasks", {"include_done": False})])
WRITE = _resp("tool_use", [_use("create_task", {"title": "Call the bank", "due": None, "idempotency_key": "k1"})])
DONE = _resp("end_turn", [SimpleNamespace(type="text", text="## Today\n- Staff sync\n\n## One thing\nSend the newsletter.")])


class Client:
    def __init__(self, *script):
        self._script, self.calls = list(script), 0

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Notes(list):
    def __call__(self, title, message):
        self.append((title, message))


def _go(store, client, **kw):
    notes = Notes()
    out = ship.run_scheduled(store, client=client, notifier=notes, now=lambda: NOW, **kw)
    return out, notes


# ── the scheduled run ────────────────────────────────────────────────────────

def test_a_run_writes_todays_brief_and_says_so(store, tmp_path):
    out, notes = _go(store, Client(READ, DONE))
    assert out.code == ship.OK and out.path == Path("briefs/2026-09-07.md")
    text = out.path.read_text()
    assert "## One thing" in text and f"run {out.run_id}" in text and "$0." in text
    assert notes == [("Dayflow", "Brief ready: 2026-09-07.md")]
    assert not list(Path("briefs").glob("*.tmp")) and not Path("briefs/.dayflow.lock").exists()


def test_writes_are_queued_never_made_and_the_file_lists_the_commands(store):
    before = store.list_tasks()
    out, notes = _go(store, Client(WRITE, DONE))
    assert store.list_tasks() == before                              # nobody was there to say yes
    pending = store.list_approvals()
    assert len(pending) == 1
    text = out.path.read_text()
    assert "1 write(s) waiting for your approval" in text and f"dayflow approve {pending[0]['id']}" in text
    assert notes[0][1] == "Brief ready: 2026-09-07.md · 1 waiting for approval"


def test_a_second_run_the_same_day_does_nothing_and_spends_nothing(store):
    _go(store, Client(READ, DONE))
    client = Client(READ, DONE)
    out, notes = _go(store, client)
    assert out.code == ship.SKIPPED and client.calls == 0 and notes == []


def test_force_runs_again(store):
    _go(store, Client(READ, DONE))
    client = Client(DONE)
    out, _ = _go(store, client, force=True)
    assert out.code == ship.OK and client.calls == 1


def test_a_held_lock_skips_and_a_stale_one_is_taken_over(store):
    Path("briefs").mkdir()
    lock = Path("briefs/.dayflow.lock")
    lock.write_text("123")
    out, _ = _go(store, Client(DONE))
    assert out.code == ship.SKIPPED and "lock" in out.message
    old = NOW.timestamp() - ship.LOCK_STALE_S - 1
    os.utime(lock, (old, old))
    assert ship.Lock(lock, now=lambda: NOW.timestamp()).acquire() is True


def test_a_failed_run_leaves_no_brief_and_notifies_with_the_reason(store):
    out, notes = _go(store, Client(*[READ] * 12))                     # never finishes: max_steps
    assert out.code == ship.FAILED and not Path("briefs/2026-09-07.md").exists()
    assert notes[0][0] == "Dayflow failed" and "dayflow doctor" in notes[0][1]
    assert not Path("briefs/.dayflow.lock").exists()                  # the lock is released on failure too


def test_after_an_api_outage_the_next_run_resumes_and_pays_for_one_call(store):
    outage = anthropic.APIConnectionError.__new__(anthropic.APIConnectionError)
    out, notes = _go(store, Client(READ, outage))
    assert out.code == ship.FAILED and "api unavailable" in out.message
    failed = store.latest_run()
    later = Client(DONE)
    out2 = ship.run_scheduled(store, client=later, notifier=Notes(), now=lambda: datetime.fromisoformat(failed["created_at"]))
    assert out2.code == ship.OK and later.calls == 1 and out2.run_id == failed["id"]
    assert store.get_run(failed["id"])["status"] == "done"


def test_notify_never_raises_and_escapes_quotes(monkeypatch):
    monkeypatch.setattr(ship.sys, "platform", "darwin")
    seen = []
    ship.notify("Dayflow", 'He said "hi" \\ bye', run=seen.append)
    assert seen[0][:2] == ["osascript", "-e"] and '\\"hi\\"' in seen[0][2] and "\\\\ bye" in seen[0][2]
    ship.notify("Dayflow", "x", run=lambda c: (_ for _ in ()).throw(OSError("no osascript")))     # swallowed


# ── health ───────────────────────────────────────────────────────────────────

def _run_row(store, *, status="done", days_ago=0.0, error=None, in_tok=10_000, out_tok=2_000, model=loop.DEFAULT_MODEL):
    rid = store.create_run(kind="brief", system="s", user_msg="u", model=model)
    when = (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")
    store._conn.execute("UPDATE runs SET status=?, error=?, created_at=?, updated_at=? WHERE id=?", (status, error, when, when, rid))
    store.record_event(rid, step=1, kind="model", name=model, ms=1000, input_tokens=in_tok, output_tokens=out_tok)
    return rid


def test_health_is_silent_when_things_are_fine(store):
    for d in (3, 2, 1, 0):
        _run_row(store, days_ago=d)
    assert ship.health(store, now=NOW) == []


def test_two_failures_in_a_week_are_reported_with_the_latest_reason(store):
    _run_row(store, days_ago=2)
    _run_row(store, status="failed", days_ago=1, error="token budget exceeded at step 3: 149696 > 60000")
    _run_row(store, status="failed", days_ago=0, error="api unavailable at step 2 after 4 retries (APIConnectionError); nothing was lost")
    alerts = ship.health(store, now=NOW)
    assert alerts[0].startswith("2 of the last 3 briefs failed") and "api unavailable at step 2" in alerts[0]


def test_one_failure_is_not_an_alert(store):
    _run_row(store, days_ago=1)
    _run_row(store, status="failed", days_ago=0, error="x")
    assert ship.health(store, now=NOW) == []


def test_a_run_stuck_running_is_reported_as_crashed(store):
    rid = _run_row(store, status="running", days_ago=0.2)
    assert f"dayflow resume {rid}" in ship.health(store, now=NOW)[0]


def test_cost_above_the_limit_and_cost_above_twice_the_median(store):
    for d in (5, 4, 3, 2):
        _run_row(store, days_ago=d)                                   # $0.04 each
    _run_row(store, days_ago=0, in_tok=30_000, out_tok=5_000)         # $0.11: under the absolute limit, over 2x median
    assert "more than twice the 7-day median $0.0400" in ship.health(store, now=NOW)[0]
    _run_row(store, days_ago=0, in_tok=60_000, out_tok=10_000)        # $0.22
    assert "above the $0.15 alert" in ship.health(store, now=NOW)[0]


def test_runs_with_no_recorded_cost_do_not_drag_the_median_to_zero(store):
    """From the first real health report: 'more than twice the 7-day median $0.0000'."""
    for d in (5, 4, 3, 2):
        rid = store.create_run(kind="brief", system="s", user_msg="u", model=loop.DEFAULT_MODEL)      # no events: pre-lesson-90
        when = (NOW - timedelta(days=d)).isoformat(timespec="seconds")
        store._conn.execute("UPDATE runs SET status='done', created_at=?, updated_at=? WHERE id=?", (when, when, rid))
    _run_row(store, days_ago=0, in_tok=5_000, out_tok=100)             # an ordinary, cheap run
    assert ship.health(store, now=NOW) == []


def test_an_unpriced_model_raises_no_cost_alert_and_does_not_crash(store):
    _run_row(store, model="some-new-model", in_tok=10**7)
    assert ship.health(store, now=NOW) == []


def test_expired_approvals_are_reported(store):
    aid = store.record_approval(run_id=None, tool="create_task", args={}, status="pending", decided_by=None, reason="", ttl_hours=1)
    assert ship.health(store, now=NOW + timedelta(days=400)) == ["1 approval(s) expired unanswered. `dayflow approvals`"]
    assert aid


def test_an_old_google_sign_in_is_flagged_before_it_expires(store, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK", "0")
    path = tmp_path / "tok.json"
    monkeypatch.setattr(oauth, "DEFAULT_TOKEN_PATH", path)
    monkeypatch.setattr(oauth.load_token, "__defaults__", (path,))
    oauth.save_token(oauth.Token("a", "r", 0, ["s"], obtained_at=NOW.timestamp() - 6.5 * 86400), path)
    assert "over 6 days old" in ship.health(store, now=NOW)[0]


# ── backup and prune ─────────────────────────────────────────────────────────

def test_backup_is_a_real_database_owner_only_and_old_ones_are_dropped(store, tmp_path):
    _run_row(store)
    out = tmp_path / "bk"
    for day in ("20260901", "20260902", "20260903"):
        dest = ship.backup(store, keep=2, out_dir=out, today=day)
    assert [p.name for p in sorted(out.glob("*.db"))] == ["dayflow-20260902.db", "dayflow-20260903.db"]
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    copy = sqlite3.connect(dest)
    assert copy.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1 and copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    copy.close()


def test_prune_forgets_old_content_and_keeps_the_numbers(store):
    out, _ = _go(store, Client(READ, DONE))
    old = out.run_id
    long_ago = (NOW - timedelta(days=45)).isoformat(timespec="seconds")
    store._conn.execute("UPDATE runs SET created_at = ? WHERE id = ?", (long_ago, old))
    recent = _run_row(store, days_ago=1)
    done = ship.prune(store, days=30, now=NOW)
    assert done["runs"] == 1 and done["steps"] == 2
    assert store.steps(old) == [] and store.get_run(old)["final_text"] is None
    assert store.get_run(old)["input_tokens"] > 0 and len(store.events(old)) > 0        # stats and cost history survive
    assert store.get_run(recent) is not None


# ── doctor ───────────────────────────────────────────────────────────────────

NOT_INSTALLED = lambda: {"installed": False, "loaded": False, "at": None, "plist": "p", "log": "l"}      # noqa: E731
LOADED = lambda: {"installed": True, "loaded": True, "at": "07:30", "plist": "p", "log": "l", "mock": True}            # noqa: E731
ORPHAN = lambda: {"installed": True, "loaded": False, "at": "07:30", "plist": "p", "log": "l", "mock": True}           # noqa: E731


def _levels(report):
    return {c.name: c.level for c in report.checks}


def test_doctor_fails_without_an_api_key_and_never_prints_one(store, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(vault, "available", lambda: False)
    r = ship.doctor(store, schedule_status=NOT_INSTALLED)
    assert _levels(r)["api key"] == "FAIL" and not r.ok and "NOT ready: 1 problem(s)" in r.render()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-VALUE")
    r = ship.doctor(store, schedule_status=LOADED)
    assert r.ok and "SECRET" not in r.render() and "ready" in r.render()


def test_doctor_tells_installed_from_loaded(store, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert _levels(ship.doctor(store, schedule_status=NOT_INSTALLED))["schedule"] == "warn"
    assert _levels(ship.doctor(store, schedule_status=LOADED))["schedule"] == "ok"
    r = ship.doctor(store, schedule_status=ORPHAN)
    assert _levels(r)["schedule"] == "FAIL" and "launchd has not loaded it" in r.render()


def test_doctor_in_real_mode_needs_a_google_sign_in(store, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("MOCK", "0")
    path = tmp_path / "none.json"
    monkeypatch.setattr(oauth.load_token, "__defaults__", (path,))
    r = ship.doctor(store, schedule_status=LOADED)
    assert _levels(r)["google token"] == "FAIL" and "dayflow auth login" in r.render()
    oauth.save_token(oauth.Token("a", "r", 0, [oauth.SCOPES_READ[0]], obtained_at=1.0), path)
    r = ship.doctor(store, schedule_status=LOADED)
    assert _levels(r)["google token"] == "ok" and _levels(r)["token storage"] == "warn"


def test_doctor_checks_google_when_the_schedule_runs_real_even_from_a_mock_shell(store, tmp_path, monkeypatch):
    """From the first real doctor run: the shell had MOCK=1, launchd had MOCK=0, and Google was never looked at."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("MOCK", "1")
    monkeypatch.setattr(oauth.load_token, "__defaults__", (tmp_path / "none.json",))
    real_schedule = lambda: {"installed": True, "loaded": True, "at": "07:30", "plist": "p", "log": "l", "mock": False}   # noqa: E731
    r = ship.doctor(store, schedule_status=real_schedule)
    assert _levels(r)["google token"] == "FAIL" and "the schedule runs MOCK=0" in r.render() and not r.ok
    mock_schedule = lambda: {**real_schedule(), "mock": True}           # noqa: E731
    assert "google token" not in _levels(ship.doctor(store, schedule_status=mock_schedule))


def test_doctor_live_makes_one_small_call_and_reports_a_failure(store, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    ok = Client(_resp("end_turn", []))
    assert _levels(ship.doctor(store, live=True, client=ok, schedule_status=LOADED))["api call"] == "ok" and ok.calls == 1
    bad = Client(RuntimeError("401 invalid x-api-key"))
    r = ship.doctor(store, live=True, client=bad, schedule_status=LOADED)
    assert _levels(r)["api call"] == "FAIL" and not r.ok


# ── schedule ─────────────────────────────────────────────────────────────────

class Launchctl:
    def __init__(self, fail=()):
        self.calls, self.fail = [], fail

    def __call__(self, cmd):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 5 if cmd[1] in self.fail else 0, "", "Bootstrap failed: 5: Input/output error")


def test_the_plist_names_the_interpreter_the_folder_and_the_time_and_holds_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET")
    p = schedule.build_plist(at="07:05", workdir=tmp_path, python="/opt/py/bin/python", home=tmp_path)
    assert p["ProgramArguments"] == ["/opt/py/bin/python", "-m", "dayflow.main", "run"]
    assert p["WorkingDirectory"] == str(tmp_path) and p["StartCalendarInterval"] == {"Hour": 7, "Minute": 5}
    assert p["EnvironmentVariables"]["MOCK"] == "0" and p["RunAtLoad"] is False
    assert "SECRET" not in schedule.render(p) and plistlib.loads(schedule.render(p).encode()) == p


@pytest.mark.parametrize("bad", ["7", "25:00", "07:60", "seven", "07:30:00", ""])
def test_a_bad_time_is_refused(bad, tmp_path):
    with pytest.raises(schedule.ScheduleError, match="HH:MM"):
        schedule.build_plist(at=bad, workdir=tmp_path)


def test_install_writes_the_plist_then_replaces_and_loads_it(tmp_path):
    ctl = Launchctl()
    path = schedule.install(at="07:30", workdir=tmp_path, run=ctl, home=tmp_path, check_platform=False)
    assert path == tmp_path / "Library/LaunchAgents/com.learn2exel.dayflow.plist" and path.exists()
    domain = f"gui/{os.getuid()}"
    assert ctl.calls == [["launchctl", "bootout", f"{domain}/com.learn2exel.dayflow"], ["launchctl", "bootstrap", domain, str(path)]]
    st = schedule.status(run=ctl, home=tmp_path, check_platform=False)
    assert st["installed"] and st["loaded"] and st["at"] == "07:30" and st["mock"] is False


def test_a_failed_bootstrap_is_an_error_with_launchctls_own_words(tmp_path):
    with pytest.raises(schedule.ScheduleError, match="bootstrap failed \\(5\\).*Input/output error"):
        schedule.install(at="07:30", workdir=tmp_path, run=Launchctl(fail=("bootstrap",)), home=tmp_path, check_platform=False)


def test_installed_but_not_loaded_is_visible_in_status(tmp_path):
    schedule.install(at="06:00", workdir=tmp_path, run=Launchctl(), home=tmp_path, check_platform=False)
    st = schedule.status(run=Launchctl(fail=("print",)), home=tmp_path, check_platform=False)
    assert st["installed"] is True and st["loaded"] is False


def test_uninstall_unloads_and_removes(tmp_path):
    schedule.install(at="06:00", workdir=tmp_path, run=Launchctl(), home=tmp_path, check_platform=False)
    ctl = Launchctl()
    assert schedule.uninstall(run=ctl, home=tmp_path, check_platform=False) is True
    assert ctl.calls[0][:2] == ["launchctl", "bootout"] and not schedule.plist_path(tmp_path).exists()
    assert schedule.uninstall(run=Launchctl(), home=tmp_path, check_platform=False) is False


def test_off_macos_install_refuses_and_points_at_cron(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.sys, "platform", "linux")
    with pytest.raises(schedule.ScheduleError, match="schedule print --cron"):
        schedule.install(at="07:30", workdir=tmp_path, run=Launchctl(), home=tmp_path)
    assert schedule.cron_line(at="07:30", workdir=tmp_path, python="/p").startswith(f"30 7 * * * cd {tmp_path} && MOCK=0 /p -m dayflow.main run")


# ── keychain ─────────────────────────────────────────────────────────────────

class FakeKeyring:
    def __init__(self):
        self.items = {}

    def get_password(self, service, name):
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        self.items[(service, name)] = value

    def delete_password(self, service, name):
        del self.items[(service, name)]


@pytest.fixture
def keyring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(vault, "_backend", lambda backend=None: backend or fake)
    monkeypatch.setattr(vault, "available", lambda: True)
    return fake


def test_vault_round_trip_and_delete(keyring):
    assert vault.get("x") is None and vault.delete("x") is False
    vault.put("x", "v")
    assert vault.get("x") == "v" and keyring.items == {("dayflow", "x"): "v"}
    assert vault.delete("x") is True and vault.get("x") is None


def test_a_broken_keychain_is_a_vault_error_not_a_traceback():
    class Locked:
        def get_password(self, *a):
            raise RuntimeError("keychain is locked")
    with pytest.raises(vault.VaultError, match="could not read 'x'.*locked"):
        vault.get("x", backend=Locked())


def test_without_the_package_the_error_says_what_to_install(monkeypatch):
    import builtins
    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", lambda name, *a, **k: (_ for _ in ()).throw(ImportError()) if name == "keyring" else real(name, *a, **k))
    with pytest.raises(vault.VaultError, match="pip install keyring"):
        vault.get("x")
    assert vault.available() is False


def test_the_token_lives_in_the_keychain_when_asked_and_no_file_is_written(keyring, tmp_path, monkeypatch):
    monkeypatch.setenv("DAYFLOW_TOKEN_STORE", "keychain")
    path = tmp_path / "google_token.json"
    monkeypatch.setattr(oauth, "DEFAULT_TOKEN_PATH", path)
    token = oauth.Token("a", "r", 123.0, ["s"], obtained_at=5.0)
    oauth.save_token(token, path)
    assert not path.exists() and json.loads(keyring.items[("dayflow", "google-token")])["refresh_token"] == "r"
    assert oauth.load_token(path) == token and "keychain" in oauth.token_location(path)
    assert oauth.delete_token(path) is True and oauth.load_token(path) is None


def test_an_explicit_path_is_always_a_file_even_in_keychain_mode(keyring, tmp_path, monkeypatch):
    monkeypatch.setenv("DAYFLOW_TOKEN_STORE", "keychain")
    other = tmp_path / "elsewhere.json"
    oauth.save_token(oauth.Token("a", "r", 1.0), other)
    assert other.exists() and keyring.items == {}


def test_migrate_copies_verifies_then_deletes_the_file(keyring, tmp_path, monkeypatch, capsys):
    path = tmp_path / "google_token.json"
    monkeypatch.setattr(oauth, "DEFAULT_TOKEN_PATH", path)
    for fn in (oauth.save_token, oauth.load_token, oauth.delete_token, oauth.token_location):
        monkeypatch.setattr(fn, "__defaults__", (path,))
    monkeypatch.setenv("DAYFLOW_TOKEN_STORE", "file")
    token = oauth.Token("a", "r", 1.0, ["s"], obtained_at=9.0)
    oauth.save_token(token)
    assert main._migrate_token("keychain") == 0
    assert not path.exists() and json.loads(keyring.items[("dayflow", "google-token")])["obtained_at"] == 9.0
    assert "DAYFLOW_TOKEN_STORE=keychain" in capsys.readouterr().out
    assert main._migrate_token("keychain") == 1                       # nothing left in the file to move


def test_the_api_key_is_read_from_the_keychain_only_when_the_environment_has_none(keyring, monkeypatch):
    vault.put(vault.ANTHROPIC_KEY, "from-keychain")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    main._api_key_from_keychain()
    assert os.environ["ANTHROPIC_API_KEY"] == "from-env"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    main._api_key_from_keychain()
    assert os.environ["ANTHROPIC_API_KEY"] == "from-keychain"
    monkeypatch.delenv("ANTHROPIC_API_KEY")


# ── token age ────────────────────────────────────────────────────────────────

def test_a_refresh_does_not_reset_the_consent_clock():
    first = oauth._token_from({"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "s"}, now=1000.0)
    assert first.obtained_at == 1000.0
    again = oauth._token_from({"access_token": "b", "expires_in": 3600}, previous=first, now=999_999.0)
    assert again.obtained_at == 1000.0 and again.refresh_token == "r"


def test_a_token_saved_before_the_capstone_still_loads(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": 1.0, "scopes": ["s"]}))
    os.chmod(path, 0o600)
    assert oauth.load_token(path).obtained_at == 0.0
