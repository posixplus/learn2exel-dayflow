"""Permissions and the human in the loop. Lesson 86.

Until now every tool the model asked for ran. For reads that is fine. For
writes it means the model's judgement is the only thing between an email it
misread and an action in your name. This module puts three things in between:

    Policy     pure code, no I/O. Looks at the tool and the run so far and
               answers allow / deny / ask. Deny rules are checked first.
    Approver   resolves an "ask". Two ship here:
                 TtyApprover    a person is at the terminal: show the exact
                                call, wait for y / n / a.
                 QueueApprover  nobody is there (a 6 a.m. scheduled brief):
                                record the proposal, do NOT run it, let the
                                run finish. `dayflow approve <id>` runs it later.
    Gate       ties the two to the store and to dispatch(). Both engines call
               the same Gate, so the raw loop and the Agent SDK cannot drift.

Four rules the code enforces rather than the prompt:

1. The default is ask. A write runs only if a rule or a person said yes.
2. What is approved is what runs. A queued proposal stores the exact arguments;
   approving it replays those bytes through dispatch(). The model is not asked
   again, so it cannot change its mind between the question and the action.
3. Approvals go stale. The world moves on; a proposal older than its TTL is
   refused and has to be proposed again.
4. Every write decision leaves a row: who decided (policy or human), what, why.

A refused or queued write comes back to the model in the lesson 82 envelope
with ok=false, so it can never report something as done that was not done.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol, TextIO

from dayflow.store import Store
from dayflow.tools import REGISTRY, Tool, dispatch

Behavior = Literal["allow", "deny", "ask"]
WritesMode = Literal["ask", "allow", "deny"]

# Error codes the model can receive from the gate. None of them is retryable:
# the answer to "the human said no" is never "ask again in a loop".
PENDING = "pending_approval"
DENIED = "denied_by_user"
READ_ONLY = "read_only"
WRITE_LIMIT = "write_limit"

DEFAULT_TTL_HOURS = float(os.environ.get("DAYFLOW_APPROVAL_TTL_HOURS", "24"))


# ── Policy: allow / deny / ask, decided in code ──────────────────────────────
@dataclass(frozen=True)
class Decision:
    behavior: Behavior
    reason: str          # shown to the human and written to the audit row
    code: str = ""       # error code for the model when behavior == "deny"


@dataclass(frozen=True)
class Policy:
    """How this run treats tools. Frozen: a run's policy does not change under it."""
    writes: WritesMode = "ask"
    max_writes_per_run: int = 2      # was a sentence in the prompt until this lesson

    def decide(self, tool: Tool, args: dict[str, Any], *, writes_so_far: int) -> Decision:
        if tool.kind == "read":
            return Decision("allow", "read tools do not change anything")
        # Deny rules first. An allow rule must never be able to skip a limit.
        if self.writes == "deny":
            return Decision("deny", "this run is read-only", READ_ONLY)
        if writes_so_far >= self.max_writes_per_run:
            return Decision("deny", f"this run already proposed {writes_so_far} writes; the limit is {self.max_writes_per_run}", WRITE_LIMIT)
        if self.writes == "allow":
            return Decision("allow", "writes were pre-approved for this run (--approve auto)")
        return Decision("ask", f"{tool.name} is a write tool")


# ── Approvers: who answers an "ask" ──────────────────────────────────────────
@dataclass(frozen=True)
class Request:
    tool: str
    args: dict[str, Any]
    reason: str
    run_id: str | None = None


@dataclass(frozen=True)
class Verdict:
    approved: bool | None        # True, False, or None = nobody here to ask; queue it
    reason: str = ""


class Approver(Protocol):
    def resolve(self, req: Request) -> Verdict: ...


def render(req: Request) -> str:
    """What the human sees. The exact arguments, not a summary of them: a person
    cannot approve what they were not shown."""
    lines = [f"Dayflow wants to run {req.tool}  ({req.reason})"]
    for k, v in req.args.items():
        lines.append(f"    {k}: {json.dumps(v, ensure_ascii=False)}")
    return "\n".join(lines)


class TtyApprover:
    """Ask the person at the terminal. 'a' approves this tool for the rest of the
    run, which is the honest version of 'stop asking me'."""
    def __init__(self, ask: Callable[[str], str] = input, out: TextIO | None = None) -> None:
        self._ask, self._out = ask, out
        self._always: set[str] = set()

    def resolve(self, req: Request) -> Verdict:
        if req.tool in self._always:
            return Verdict(True, "approved for the rest of this run")
        print("\n" + render(req), file=self._out or sys.stderr)
        while True:
            try:
                answer = self._ask("  approve? [y]es / [n]o / [a]lways this run: ").strip().lower()
            except EOFError:           # stdin closed under us: that is not a yes
                return Verdict(False, "no answer (input closed)")
            if answer in ("y", "yes"):
                return Verdict(True, "approved at the terminal")
            if answer in ("a", "always"):
                self._always.add(req.tool)
                return Verdict(True, "approved at the terminal for the rest of this run")
            if answer in ("n", "no", ""):   # Enter means no. The safe answer is the default.
                try:
                    why = self._ask("  reason (optional, the agent will see it): ").strip()
                except EOFError:
                    why = ""
                return Verdict(False, why or "declined at the terminal")


class QueueApprover:
    """Nobody is watching. Do not block, do not guess: queue it."""
    def resolve(self, req: Request) -> Verdict:
        return Verdict(None, "queued; no human was present")


# ── Gate: policy + approver + audit + dispatch ───────────────────────────────
@dataclass(frozen=True)
class Outcome:
    allowed: bool
    envelope: str | None = None      # what to hand the model when not allowed
    approval_id: str | None = None
    code: str = ""


def _refusal(code: str, message: str) -> str:
    return json.dumps({"ok": False, "error": {"code": code, "message": message, "retryable": False}})


@dataclass
class Gate:
    policy: Policy = field(default_factory=Policy)
    approver: Approver = field(default_factory=QueueApprover)
    store: Store | None = None
    run_id: str | None = None
    ttl_hours: float = DEFAULT_TTL_HOURS
    writes: int = 0                  # writes allowed or queued so far in this run

    def check(self, name: str, args: dict[str, Any]) -> Outcome:
        """Decide one call. Does not run the tool: the SDK engine runs it itself
        after a PreToolUse 'allow', the raw loop goes through call()."""
        decision = self._decide(name, args)
        if decision is None:
            return Outcome(True)
        verdict = self.approver.resolve(self._request(name, args, decision)) if decision.behavior == "ask" else None
        return self._settle(name, args, decision, verdict)

    async def acheck(self, name: str, args: dict[str, Any]) -> Outcome:
        """check() for async callers (the SDK hook). Only the approver goes to a
        thread, because a person thinking must not freeze the event loop. The store
        stays on this thread: a SQLite connection belongs to the thread that opened it."""
        decision = self._decide(name, args)
        if decision is None:
            return Outcome(True)
        verdict = None
        if decision.behavior == "ask":
            verdict = await asyncio.to_thread(self.approver.resolve, self._request(name, args, decision))
        return self._settle(name, args, decision, verdict)

    def _decide(self, name: str, args: dict[str, Any]) -> Decision | None:
        tool = REGISTRY.get(name)
        if tool is None or tool.kind == "read":
            return None              # unknown names fall through to dispatch's own error envelope
        return self.policy.decide(tool, args, writes_so_far=self.writes)

    def _request(self, name: str, args: dict[str, Any], decision: Decision) -> Request:
        return Request(name, args, decision.reason, self.run_id)

    def _settle(self, name: str, args: dict[str, Any], decision: Decision, verdict: Verdict | None) -> Outcome:
        if decision.behavior == "deny":
            self._audit(name, args, "denied", "policy", decision.reason)
            return Outcome(False, _refusal(decision.code, f"Not executed: {decision.reason}. Do not retry."), code=decision.code)

        if decision.behavior == "allow":
            self.writes += 1
            self._audit(name, args, "allowed", "policy", decision.reason)
            return Outcome(True)

        assert verdict is not None
        if verdict.approved is True:
            self.writes += 1
            self._audit(name, args, "allowed", "human", verdict.reason)
            return Outcome(True)
        if verdict.approved is False:
            self._audit(name, args, "denied", "human", verdict.reason)
            return Outcome(False, _refusal(DENIED, f"Not executed. The user declined: {verdict.reason}. Do not retry; say in the brief that it was declined."), code=DENIED)

        # Queued. It counts toward the write limit: a run cannot stack up proposals either.
        self.writes += 1
        approval_id = self._audit(name, args, "pending", None, verdict.reason)
        ref = f" as {approval_id}" if approval_id else ""
        return Outcome(False, _refusal(PENDING, f"Not executed. Queued for the user's approval{ref}. Do not retry. "
                                                "List it in the brief under 'Waiting for your approval'."),
                       approval_id=approval_id, code=PENDING)

    def call(self, name: str, args: dict[str, Any]) -> str:
        """check() then dispatch(). Always returns an envelope, like dispatch()."""
        outcome = self.check(name, args)
        return dispatch(name, args) if outcome.allowed else (outcome.envelope or _refusal("blocked", "Not executed."))

    def _audit(self, tool: str, args: dict[str, Any], status: str, decided_by: str | None, reason: str) -> str | None:
        if self.store is None:
            return None
        return self.store.record_approval(run_id=self.run_id, tool=tool, args=args, status=status,
                                          decided_by=decided_by, reason=reason, ttl_hours=self.ttl_hours)


# ── Acting on a queued proposal, later, without the model ────────────────────
class ApprovalError(RuntimeError):
    pass


def approve(store: Store, approval_id: str) -> dict[str, Any]:
    """Run a queued proposal exactly as it was recorded. Safe to call twice: the
    second call returns the stored result, and the write's own idempotency key
    (lesson 82) covers a crash between running the tool and recording that it ran."""
    row = store.get_approval(approval_id)
    if row is None:
        raise ApprovalError(f"no approval {approval_id!r}")
    if row["status"] == "approved":
        return json.loads(row["result"])
    if row["status"] != "pending":
        raise ApprovalError(f"approval {approval_id} is {row['status']}, not pending")
    if store.is_expired(row):
        store.decide_approval(approval_id, status="expired", decided_by="policy", reason="older than its TTL")
        raise ApprovalError(f"approval {approval_id} expired at {row['expires_at']}; run the brief again to get a fresh proposal")
    envelope = dispatch(row["tool"], json.loads(row["args"]))
    result = json.loads(envelope)
    if not result["ok"]:
        # The human said yes and the tool said no. Leave it pending so it can be retried.
        raise ApprovalError(f"approved, but {row['tool']} failed: {result['error']['code']}: {result['error']['message']}")
    store.decide_approval(approval_id, status="approved", decided_by="human", reason="approved from the queue", result=envelope)
    return result


def deny(store: Store, approval_id: str, reason: str = "") -> None:
    row = store.get_approval(approval_id)
    if row is None:
        raise ApprovalError(f"no approval {approval_id!r}")
    if row["status"] != "pending":
        raise ApprovalError(f"approval {approval_id} is {row['status']}, not pending")
    store.decide_approval(approval_id, status="denied", decided_by="human", reason=reason or "denied from the queue")


def gate_for(mode: str, *, store: Store | None, run_id: str | None = None, interactive: bool | None = None) -> Gate:
    """Build the gate for a CLI mode.

    ask    a person decides each write, at the terminal
    queue  writes are recorded and wait for `dayflow approve`
    auto   writes run without asking (the behaviour before this lesson)
    deny   no writes at all
    None/'default'  ask if a person is at the terminal, queue if not. A scheduled
           run must never sit waiting on input() that nobody will answer.
    """
    if mode in (None, "default"):
        if interactive is None:
            interactive = sys.stdin.isatty()
        mode = "ask" if interactive else "queue"
    if mode == "ask":
        return Gate(Policy("ask"), TtyApprover(), store, run_id)
    if mode == "queue":
        return Gate(Policy("ask"), QueueApprover(), store, run_id)
    if mode == "auto":
        return Gate(Policy("allow"), QueueApprover(), store, run_id)
    if mode == "deny":
        return Gate(Policy("deny"), QueueApprover(), store, run_id)
    raise ValueError(f"unknown approval mode {mode!r}")
