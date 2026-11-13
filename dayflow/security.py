"""Injection, exfiltration and least privilege. Lesson 88.

Everything Dayflow reads was written by someone else. An email body is not a
request from the user; it is text from a stranger that happens to sit in the
user's inbox, and the model reads it in the same context as its instructions.

In testing for this lesson the model refused every planted instruction, on both
Sonnet and Haiku. That is good news and it is not a security property: it is
the behaviour of one model on ten messages. So this module is written for the
other case. Assume the model does what the email says. What can still happen?

Three kinds of control live here, in order of how much they can be trusted:

  1. Structural rules on write arguments (deterministic, cannot be argued with):
     no links in a write, drafts stay short, a draft may not carry text copied
     from a DIFFERENT message than the one it answers.
  2. Taint: once a run has read a message that looks like an injection, nothing
     in that run is auto-approved, whatever the approval mode.
  3. Detection (scan): pattern matching for instruction-shaped text. Cheap,
     useful as a signal for 2 and for the human, and trivially evaded. Never
     the only thing between an email and an action.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_DRAFT_CHARS = 600          # a reply that acknowledges does not need more
MIN_COPIED_CHARS = 40          # a shared run this long is a quote, not a coincidence

URL = re.compile(r"(?:https?://|www\.)[^\s\"'<>)]+", re.I)

# name -> pattern. Deliberately few and readable. Extend when the red-team set grows.
SIGNS: dict[str, re.Pattern[str]] = {
    "overrides_instructions": re.compile(r"ignore\s+(?:all\s+|your\s+)?(?:previous|prior|above)\s+instructions", re.I),
    "addresses_assistant": re.compile(r"\b(?:ai\s+)?assistants?\b|\bto\s+ai\b|\bllm\b", re.I),
    "claims_authority": re.compile(r"\bsystem\s+(?:notice|prompt|message)\b|pre-?approved", re.I),
    "asks_for_secrecy": re.compile(r"do\s+not\s+(?:mention|tell|reveal|include)\b", re.I),
    "hidden_text": re.compile(r"<!--.*?-->", re.S),
    "imitates_tool_result": re.compile(r'"ok"\s*:\s*(?:true|false)', re.I),
}


def scan(text: str) -> list[str]:
    """Names of the signs found in `text`. An empty list means 'nothing matched',
    which is a much weaker statement than 'safe'."""
    return [name for name, pattern in SIGNS.items() if pattern.search(text or "")]


def urls(text: str) -> list[str]:
    return [m.group(0).rstrip(".,;") for m in URL.finditer(text or "")]


@dataclass
class RunState:
    """What this run has read. Reset at the start of every run."""
    bodies: dict[str, str] = field(default_factory=dict)        # message id -> body
    flagged: dict[str, list[str]] = field(default_factory=dict) # message id -> signs
    flagged_urls: set[str] = field(default_factory=set)

    @property
    def tainted(self) -> bool:
        return bool(self.flagged)

    def observe(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Record what was read and annotate suspicious messages. The annotation
        travels to the model as data: `flags` on the message, nothing hidden."""
        out = []
        for m in messages:
            body = m.get("body", "")
            self.bodies[m["id"]] = body
            signs = scan(f"{m.get('subject', '')}\n{body}")
            if signs:
                self.flagged[m["id"]] = signs
                self.flagged_urls.update(urls(body))
                m = {**m, "flags": signs}
            out.append(m)
        return out


RUN = RunState()


def reset() -> RunState:
    global RUN
    RUN = RunState()
    return RUN


# ── Rules on what a write may contain ────────────────────────────────────────
def copied_from_other_message(body: str, *, answering: str | None, state: RunState | None = None) -> str | None:
    """Id of a message, other than the one being answered, that `body` quotes at
    length. This is the exfiltration check: a reply to A should not carry B's text."""
    state = state or RUN
    squashed = " ".join(body.split())
    for mid, other in state.bodies.items():
        if mid == answering:
            continue
        other = " ".join(other.split())
        for i in range(0, max(0, len(other) - MIN_COPIED_CHARS + 1), 8):
            if other[i:i + MIN_COPIED_CHARS] in squashed:
                return mid
    return None


def content_violation(tool: str, args: dict[str, Any], state: RunState | None = None) -> str | None:
    """Why this write must not run, or None. Deterministic: same input, same answer."""
    state = state or RUN
    text = " ".join(str(v) for v in args.values() if isinstance(v, str))
    if urls(text):
        return "it contains a link"
    if tool == "draft_reply":
        body = args.get("body", "")
        if len(body) > MAX_DRAFT_CHARS:
            return f"the draft is {len(body)} characters; the limit is {MAX_DRAFT_CHARS}"
        source = copied_from_other_message(body, answering=args.get("message_id"), state=state)
        if source:
            return f"the draft quotes message {source}, which is not the message it answers"
        if args.get("message_id") in state.flagged:
            return f"message {args['message_id']} was flagged ({', '.join(state.flagged[args['message_id']])})"
    return None


def flagged_links_in(text: str, state: RunState | None = None) -> list[str]:
    """Links in the final brief that came from a flagged message. The model is
    allowed to describe a phishing mail; it should not hand the user its link."""
    state = state or RUN
    return [u for u in urls(text) if u in state.flagged_urls]
