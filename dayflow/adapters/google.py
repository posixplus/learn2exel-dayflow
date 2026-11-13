"""Real Gmail and Calendar adapters. Lesson 87.

Same method names and return shapes as adapters/fixtures.py, so tools.py, the
loop, the gate and every prompt are untouched. The agent does not know which
adapter it is talking to. That was the point of writing the fixtures first.

Three places where the real API does not look like the fixture, and what the
adapter does about each:

  Paging     Gmail pages with an opaque token, our tool pages with offset.
             The adapter lists message ids once per run and slices that list.
  Bodies     A Gmail message is a MIME tree, base64url encoded. The adapter
             finds the text/plain part, collapses whitespace and cuts it at
             MAX_BODY_CHARS: the model does not need a 40 KB newsletter, and you
             do not want to pay for it.
  Volume     A fixture inbox has five messages. A real one has hundreds. The
             query skips Promotions and Social and a run sees the newest
             MAX_LISTED only, and says so in the result when it was cut.
  Idempotency  drafts.create has no idempotency key. The adapter remembers
             key -> draft id itself (in the store), so a retried or re-approved
             draft_reply cannot create two drafts.

Everything goes through one `http` callable so tests can replace the network.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Callable

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR = "https://www.googleapis.com/calendar/v3/calendars/primary"

# These three were 1500, "in:inbox newer_than:3d" and 200, sized against a five-message
# fixture. The first run on a real inbox read about 200 messages, passed 400,000 input
# tokens by step 6 and was stopped by the token budget every time. A brief needs the gist
# of recent mail a person would read, so: skip the Promotions and Social tabs, keep the
# newest MAX_LISTED, and keep the opening of each body. All three can be overridden.
MAX_BODY_CHARS = int(os.environ.get("DAYFLOW_BODY_CHARS", "400"))       # per message, after decoding
INBOX_QUERY = os.environ.get("DAYFLOW_INBOX_QUERY", "in:inbox newer_than:3d -category:promotions -category:social")
MAX_LISTED = int(os.environ.get("DAYFLOW_INBOX_MAX", "30"))             # newest N messages a run can see

# (method, url, query params, json body) -> parsed JSON
Http = Callable[[str, str, dict[str, Any] | None, dict[str, Any] | None], dict[str, Any]]


class GoogleError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Google API {status}: {message}")
        self.status = status


def bearer_http(get_token: Callable[[], str]) -> Http:
    """An Http that adds the access token. get_token() is called per request, so a
    token refreshed mid-run is picked up without rebuilding the adapters."""
    def http(method: str, url: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Authorization", f"Bearer {get_token()}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read())["error"]["message"]
            except Exception:  # noqa: BLE001 - whatever came back, report the status
                message = e.reason
            raise GoogleError(e.code, str(message)) from e
        return json.loads(raw) if raw else {}
    return http


# ── Gmail ────────────────────────────────────────────────────────────────────
def _header(payload: dict[str, Any], name: str) -> str:
    for h in payload.get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _plain_text(payload: dict[str, Any]) -> str:
    """Depth-first search of the MIME tree for the first text/plain part."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        data = payload["body"]["data"]
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        text = _plain_text(part)
        if text:
            return text
    return ""


def to_message(raw: dict[str, Any]) -> dict[str, Any]:
    """Gmail message resource -> the fixture shape {id, from, subject, date, body}."""
    payload = raw.get("payload", {})
    # Collapse whitespace first: plain-text mail is full of blank lines and indentation
    # that cost tokens and say nothing.
    body = re.sub(r"\s+", " ", _plain_text(payload)).strip() or raw.get("snippet", "")
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + f"... [truncated, {len(body)} chars total]"
    when = datetime.fromtimestamp(int(raw.get("internalDate", "0")) / 1000, tz=timezone.utc)
    return {
        "id": raw["id"],
        "from": parseaddr(_header(payload, "From"))[1] or _header(payload, "From"),
        "subject": _header(payload, "Subject"),
        "date": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "body": body,
    }


class GmailInbox:
    def __init__(self, http: Http, *, drafts: Any = None, query: str = INBOX_QUERY) -> None:
        self._http, self._drafts, self._query = http, drafts, query
        self._ids: list[str] | None = None      # listed once per run, then sliced by offset
        self._more = False                      # the query matched more than MAX_LISTED

    def _list_ids(self) -> list[str]:
        if self._ids is None:
            ids: list[str] = []
            token: str | None = None
            while len(ids) < MAX_LISTED:
                params: dict[str, Any] = {"q": self._query, "maxResults": min(100, MAX_LISTED)}
                if token:
                    params["pageToken"] = token
                page = self._http("GET", f"{GMAIL}/messages", params, None)
                ids += [m["id"] for m in page.get("messages", [])]
                token = page.get("nextPageToken")
                if not token:
                    break
            self._more = bool(token) or len(ids) > MAX_LISTED
            self._ids = ids[:MAX_LISTED]
        return self._ids

    def list_messages(self, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        ids = self._list_ids()
        page = ids[offset:offset + limit]
        messages = [to_message(self._http("GET", f"{GMAIL}/messages/{i}", {"format": "full"}, None)) for i in page]
        nxt = offset + limit
        out: dict[str, Any] = {"messages": messages, "total": len(ids), "next_offset": nxt if nxt < len(ids) else None}
        if self._more:
            # Say so. A brief built on a capped listing must not read as "this is everything".
            out["note"] = (f"Only the newest {len(ids)} matching messages are listed; older ones were not read. "
                           "Tell the user the brief covers the newest mail only.")
        return out

    def sender_of(self, message_id: str) -> str | None:
        """Lesson 88: who a reply to this message would go to. Shown at approval time."""
        try:
            raw = self._http("GET", f"{GMAIL}/messages/{message_id}", {"format": "metadata", "metadataHeaders": ["From"]}, None)
        except GoogleError:
            return None
        return parseaddr(_header(raw.get("payload", {}), "From"))[1] or None

    def create_draft(self, *, message_id: str, body: str, idempotency_key: str) -> dict[str, Any]:
        """A reply DRAFT in the original thread. Nothing is sent: the user reads it in
        Gmail and presses Send themselves."""
        from dayflow.tools import ToolError  # local import avoids a cycle
        if not body.strip():
            raise ToolError("invalid_body", "body must not be empty")
        if self._drafts is not None:
            known = self._drafts.get_draft(idempotency_key)
            if known:
                return {**known, "created": False}
        try:
            original = self._http("GET", f"{GMAIL}/messages/{message_id}",
                                  {"format": "metadata", "metadataHeaders": ["From", "Subject", "Message-ID"]}, None)
        except GoogleError as e:
            if e.status == 404:
                raise ToolError("unknown_message", f"no message with id {message_id!r}") from e
            raise
        payload = original.get("payload", {})
        subject = _header(payload, "Subject")
        mail = EmailMessage()
        mail["To"] = _header(payload, "From")
        mail["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if _header(payload, "Message-ID"):
            mail["In-Reply-To"] = mail["References"] = _header(payload, "Message-ID")
        mail.set_content(body)
        raw = base64.urlsafe_b64encode(mail.as_bytes()).decode("ascii")
        made = self._http("POST", f"{GMAIL}/drafts", None, {"message": {"raw": raw, "threadId": original.get("threadId")}})
        draft = {"draft_id": made["id"], "to": parseaddr(mail["To"])[1], "subject": mail["Subject"]}
        if self._drafts is not None:
            self._drafts.save_draft(idempotency_key, draft)
        return {**draft, "created": True}


# ── Calendar ─────────────────────────────────────────────────────────────────
def to_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Calendar event -> {id, title, start, end, attendees}. All-day events carry
    a `date` and no `dateTime`; pass that through so the model can say 'all day'."""
    def when(slot: dict[str, Any]) -> str:
        return slot.get("dateTime") or slot.get("date", "")
    return {
        "id": raw["id"],
        "title": raw.get("summary", "(no title)"),
        "start": when(raw.get("start", {})),
        "end": when(raw.get("end", {})),
        "attendees": [a["email"] for a in raw.get("attendees", []) if not a.get("self")],
    }


class GoogleCalendar:
    def __init__(self, http: Http, *, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._http, self._now = http, now

    def list_events(self, days: int = 1) -> list[dict[str, Any]]:
        start = self._now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)   # local midnight today
        out: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "timeMin": start.isoformat(), "timeMax": (start + timedelta(days=days)).isoformat(),
                "singleEvents": "true",          # expand recurring events into instances
                "orderBy": "startTime", "maxResults": 100,
            }
            if token:
                params["pageToken"] = token
            page = self._http("GET", f"{CALENDAR}/events", params, None)
            out += [to_event(e) for e in page.get("items", []) if e.get("status") != "cancelled"]
            token = page.get("nextPageToken")
            if not token:
                return out
