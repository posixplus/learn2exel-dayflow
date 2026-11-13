"""Run Dayflow every morning without you. Capstone.

On macOS the right tool is launchd, not cron: a LaunchAgent runs inside your login
session (so it can reach your keychain and show a notification), and a job whose
time passed while the laptop was asleep runs when it wakes.

    dayflow schedule print              show the plist, change nothing
    dayflow schedule install --at 07:30
    dayflow schedule status
    dayflow schedule kick               run it now, through launchd
    dayflow schedule uninstall

Everything that touches the system goes through one `run` callable, so the tests
never call launchctl.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Callable

LABEL = "com.learn2exel.dayflow"
Runner = Callable[[list[str]], subprocess.CompletedProcess]


class ScheduleError(RuntimeError):
    pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "Logs" / "dayflow.log"


def parse_at(at: str) -> tuple[int, int]:
    try:
        hh, mm = at.split(":")
        hour, minute = int(hh), int(mm)
    except ValueError as e:
        raise ScheduleError(f"--at wants HH:MM in 24-hour time, got {at!r}") from e
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"--at wants HH:MM in 24-hour time, got {at!r}")
    return hour, minute


def build_plist(*, at: str, workdir: Path, python: str = sys.executable, mock: bool = False, home: Path | None = None) -> dict:
    """The whole job, as data. Four decisions worth reading:

    ProgramArguments   the exact interpreter that has Dayflow installed, not `python` from
                       a PATH launchd does not share with your shell.
    WorkingDirectory   where .env, dayflow.db and briefs/ live. launchd starts in / otherwise.
    EnvironmentVariables  MOCK only. No secret is written into a plist: it is a plain file.
    RunAtLoad false    installing the schedule must not spend tokens.
    """
    hour, minute = parse_at(at)
    log = str(log_path(home))
    return {
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "dayflow.main", "run"],
        "WorkingDirectory": str(workdir),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "EnvironmentVariables": {"MOCK": "1" if mock else "0", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "RunAtLoad": False,
        "ProcessType": "Background",
    }


def render(plist: dict) -> str:
    return plistlib.dumps(plist, sort_keys=False).decode()


def cron_line(*, at: str, workdir: Path, python: str = sys.executable, mock: bool = False) -> str:
    """For Linux, or anyone who prefers cron. Unlike launchd, cron skips a run the machine slept through."""
    hour, minute = parse_at(at)
    return (f"{minute} {hour} * * * cd {workdir} && MOCK={'1' if mock else '0'} {python} -m dayflow.main run "
            f">> {workdir}/dayflow.log 2>&1")


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _need_macos() -> None:
    if sys.platform != "darwin":
        raise ScheduleError("launchd is macOS only. On Linux use: dayflow schedule print --cron")


def install(*, at: str, workdir: Path, mock: bool = False, run: Runner = _run, home: Path | None = None,
            python: str = sys.executable, check_platform: bool = True) -> Path:
    if check_platform:
        _need_macos()
    path = plist_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    log_path(home).parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(build_plist(at=at, workdir=workdir, python=python, mock=mock, home=home), sort_keys=False))
    run(["launchctl", "bootout", f"{_domain()}/{LABEL}"])          # replace an older copy; failing here is fine
    done = run(["launchctl", "bootstrap", _domain(), str(path)])
    if done.returncode != 0:
        raise ScheduleError(f"launchctl bootstrap failed ({done.returncode}): {(done.stderr or done.stdout).strip()}")
    return path


def uninstall(*, run: Runner = _run, home: Path | None = None, check_platform: bool = True) -> bool:
    if check_platform:
        _need_macos()
    run(["launchctl", "bootout", f"{_domain()}/{LABEL}"])
    path = plist_path(home)
    if path.exists():
        path.unlink()
        return True
    return False


def status(*, run: Runner = _run, home: Path | None = None, check_platform: bool = True) -> dict:
    """installed: the plist exists.  loaded: launchd knows the job.  They can differ, and when
    they do the schedule is not running."""
    path = plist_path(home)
    out: dict = {"installed": path.exists(), "loaded": False, "at": None, "plist": str(path), "log": str(log_path(home))}
    if path.exists():
        when = plistlib.loads(path.read_bytes()).get("StartCalendarInterval", {})
        out["at"] = f"{when.get('Hour', 0):02d}:{when.get('Minute', 0):02d}"
        out["mock"] = plistlib.loads(path.read_bytes()).get("EnvironmentVariables", {}).get("MOCK") == "1"
    if not check_platform or sys.platform == "darwin":
        out["loaded"] = run(["launchctl", "print", f"{_domain()}/{LABEL}"]).returncode == 0
    return out


def kick(*, run: Runner = _run, check_platform: bool = True) -> None:
    """Start the job now, the way launchd will: same environment, same working directory.
    A run that works from your terminal and fails here is the bug this command exists to find."""
    if check_platform:
        _need_macos()
    done = run(["launchctl", "kickstart", f"{_domain()}/{LABEL}"])
    if done.returncode != 0:
        raise ScheduleError(f"launchctl kickstart failed ({done.returncode}): {(done.stderr or done.stdout).strip()}. "
                            "Is the schedule installed? dayflow schedule status")
