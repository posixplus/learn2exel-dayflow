"""Lesson 87: OAuth and the Google adapters, tested without Google.

The network is two injectable callables: `post` for the token endpoint and
`http` for the APIs. The loopback login test is real end to end on this
machine: a real local server, a real redirect, a fake Google.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import pytest

from dayflow import oauth, tools
from dayflow.adapters import google
from dayflow.store import Store

CLIENT = oauth.Client("cid.apps.googleusercontent.com", "not-really-secret")


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# ── PKCE and the consent URL ─────────────────────────────────────────────────
def test_pkce_challenge_is_the_sha256_of_the_verifier():
    verifier, challenge = oauth.make_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected and "=" not in challenge
    assert oauth.make_pkce()[0] != verifier                      # fresh every time


def test_consent_url_asks_for_exactly_the_scopes_given_and_offline_access():
    url = oauth.consent_url(CLIENT, redirect_uri="http://127.0.0.1:1", scopes=oauth.SCOPES_READ, state="s", challenge="c")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["scope"][0].split() == list(oauth.SCOPES_READ)
    assert "compose" not in q["scope"][0]                        # least privilege is the default
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"] and q["response_type"] == ["code"]


# ── Tokens ───────────────────────────────────────────────────────────────────
def test_token_is_treated_as_expired_a_minute_early():
    t = oauth.Token("a", "r", expires_at=1000.0)
    assert t.expired(now=939.0) is False and t.expired(now=941.0) is True


def test_refresh_keeps_the_old_refresh_token_and_scopes():
    old = oauth.Token("old", "refresh-1", 0.0, ["s1"])
    new = oauth.refresh(CLIENT, old, post=lambda url, f: {"access_token": "new", "expires_in": 3600})
    assert new.access_token == "new" and new.refresh_token == "refresh-1" and new.scopes == ["s1"]


def test_revoked_refresh_token_tells_the_user_to_log_in_again():
    def post(url, fields):
        raise oauth.OAuthError("invalid_grant: Token has been expired or revoked.")
    with pytest.raises(oauth.OAuthError, match="dayflow auth login"):
        oauth.refresh(CLIENT, oauth.Token("a", "r", 0.0), post=post)


def test_token_file_is_owner_only_and_a_loose_one_is_refused(tmp_path):
    path = tmp_path / "cfg" / "token.json"
    oauth.save_token(oauth.Token("a", "r", 1.0, ["s"]), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert oauth.load_token(path).refresh_token == "r"
    os.chmod(path, 0o644)
    with pytest.raises(oauth.OAuthError, match="chmod 600"):
        oauth.load_token(path)


def test_valid_token_refreshes_and_saves_when_expired(tmp_path):
    path = tmp_path / "token.json"
    oauth.save_token(oauth.Token("stale", "r", 0.0, ["s"]), path)
    calls = []
    def post(url, fields):
        calls.append(fields["grant_type"])
        return {"access_token": "fresh", "expires_in": 3600}
    assert oauth.valid_token(CLIENT, path=path, post=post).access_token == "fresh"
    assert oauth.valid_token(CLIENT, path=path, post=post).access_token == "fresh"   # second call: no refresh
    assert calls == ["refresh_token"]


def test_not_signed_in_is_a_clear_error(tmp_path):
    with pytest.raises(oauth.OAuthError, match="not signed in"):
        oauth.valid_token(CLIENT, path=tmp_path / "missing.json")


# ── The loopback round trip, for real, against a fake Google ─────────────────
def _fake_google(*, tamper_state=False, deny=False, grant=None):
    """Returns (open_browser, post). open_browser plays the user's browser: it reads the
    consent URL and calls our local redirect the way Google would."""
    seen = {}
    def open_browser(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        seen["challenge"], seen["scope"] = q["code_challenge"][0], q["scope"][0]
        back = {"state": "evil" if tamper_state else q["state"][0]}
        back.update({"error": "access_denied"} if deny else {"code": "one-time-code"})
        urllib.request.urlopen(q["redirect_uri"][0] + "/?" + urllib.parse.urlencode(back), timeout=5).read()
    def post(url, fields):
        assert fields["code"] == "one-time-code"
        # What Google does with PKCE: hash the verifier, compare with the challenge it was sent first.
        digest = base64.urlsafe_b64encode(hashlib.sha256(fields["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        assert digest == seen["challenge"]
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3599, "scope": grant or seen["scope"]}
    return open_browser, post


def test_login_round_trip_stores_a_token(tmp_path, capsys):
    browser, post = _fake_google()
    token = oauth.login(CLIENT, open_browser=browser, post=post, timeout=5, path=tmp_path / "t.json")
    assert token.refresh_token == "rt" and token.allows(oauth.SCOPES_READ[0])
    assert oauth.load_token(tmp_path / "t.json").access_token == "at"


def test_login_rejects_a_response_with_the_wrong_state(tmp_path, capsys):
    browser, post = _fake_google(tamper_state=True)
    with pytest.raises(oauth.OAuthError, match="state mismatch"):
        oauth.login(CLIENT, open_browser=browser, post=post, timeout=5, path=tmp_path / "t.json")
    assert not (tmp_path / "t.json").exists()


def test_login_reports_a_refused_consent(tmp_path, capsys):
    browser, post = _fake_google(deny=True)
    with pytest.raises(oauth.OAuthError, match="access_denied"):
        oauth.login(CLIENT, open_browser=browser, post=post, timeout=5, path=tmp_path / "t.json")


def test_login_believes_the_granted_scopes_not_the_requested_ones(tmp_path, capsys):
    """The consent screen has a checkbox per scope. The user unticked Calendar."""
    browser, post = _fake_google(grant=oauth.SCOPES_READ[0])
    with pytest.raises(oauth.OAuthError, match="calendar.readonly"):
        oauth.login(CLIENT, open_browser=browser, post=post, timeout=5, path=tmp_path / "t.json")


# ── Gmail adapter ────────────────────────────────────────────────────────────
def _gmail_message(i, *, body="Hello there", html_only=False):
    parts = [{"mimeType": "text/html", "body": {"data": _b64(f"<p>{body}</p>")}}]
    if not html_only:
        parts.insert(0, {"mimeType": "text/plain", "body": {"data": _b64(body)}})
    return {"id": f"g{i}", "threadId": f"th{i}", "snippet": "snippet text", "internalDate": "1788764400000",
            "payload": {"mimeType": "multipart/alternative", "parts": parts, "headers": [
                {"name": "From", "value": f"Priya N <priya{i}@example.com>"}, {"name": "Subject", "value": f"Subject {i}"},
                {"name": "Message-ID", "value": f"<mid{i}@example.com>"}]}}


class FakeGoogle:
    def __init__(self, n=5, **kw):
        self.messages = {f"g{i}": _gmail_message(i, **kw) for i in range(n)}
        self.calls, self.drafts = [], []

    def __call__(self, method, url, params=None, body=None):
        self.calls.append((method, url.rsplit("/users/me", 1)[-1] if "/users/me" in url else url, params))
        if url.endswith("/messages"):
            ids = list(self.messages)
            start = int((params or {}).get("pageToken", 0))
            page = ids[start:start + 2]                               # tiny pages, to force the token loop
            out = {"messages": [{"id": i} for i in page]}
            if start + 2 < len(ids):
                out["nextPageToken"] = str(start + 2)
            return out
        if "/messages/" in url:
            mid = url.rsplit("/", 1)[-1]
            if mid not in self.messages:
                raise google.GoogleError(404, "Requested entity was not found.")
            return self.messages[mid]
        if url.endswith("/drafts"):
            self.drafts.append(body)
            return {"id": f"r{len(self.drafts)}"}
        raise AssertionError(url)


def test_gmail_messages_come_back_in_the_fixture_shape():
    out = google.GmailInbox(FakeGoogle(3)).list_messages(limit=10, offset=0)
    assert set(out) == {"messages", "total", "next_offset"} and out["total"] == 3 and out["next_offset"] is None
    m = out["messages"][0]
    assert set(m) == {"id", "from", "subject", "date", "body"}
    assert m["from"] == "priya0@example.com" and m["body"] == "Hello there" and m["date"].endswith("Z")


def test_offset_paging_is_built_on_one_listing_of_ids():
    fake = FakeGoogle(5)
    box = google.GmailInbox(fake)
    first = box.list_messages(limit=2, offset=0)
    second = box.list_messages(limit=2, offset=first["next_offset"])
    third = box.list_messages(limit=2, offset=second["next_offset"])
    assert [m["id"] for p in (first, second, third) for m in p["messages"]] == ["g0", "g1", "g2", "g3", "g4"]
    assert third["next_offset"] is None
    assert sum(1 for c in fake.calls if c[1] == "/messages") == 3     # the id list was walked once, not once per page


def test_long_bodies_are_cut_and_say_so():
    out = google.GmailInbox(FakeGoogle(1, body="x" * 5000)).list_messages(limit=1)
    body = out["messages"][0]["body"]
    assert len(body) < google.MAX_BODY_CHARS + 60 and "truncated, 5000 chars total" in body


def test_a_big_inbox_is_capped_and_the_result_says_so(monkeypatch):
    """The first real run read ~200 messages and blew a 400,000-token budget."""
    monkeypatch.setattr(google, "MAX_LISTED", 4)
    fake = FakeGoogle(9)
    box = google.GmailInbox(fake)
    out = box.list_messages(limit=50, offset=0)
    assert out["total"] == 4 and len(out["messages"]) == 4 and out["next_offset"] is None
    assert "newest 4" in out["note"] and "not read" in out["note"]
    assert sum(1 for c in fake.calls if "/messages/" in c[1]) == 4          # bodies beyond the cap are never fetched


def test_a_small_inbox_carries_no_note():
    assert "note" not in google.GmailInbox(FakeGoogle(3)).list_messages(limit=10, offset=0)


def test_default_query_skips_promotions_and_social_and_caps_are_small():
    assert "-category:promotions" in google.INBOX_QUERY and "-category:social" in google.INBOX_QUERY
    assert google.MAX_LISTED <= 50 and google.MAX_BODY_CHARS <= 600
    # worst case the model can be handed in one run, in characters: well under the 60k-token budget
    assert google.MAX_LISTED * (google.MAX_BODY_CHARS + 200) < 40_000


def test_whitespace_in_bodies_is_collapsed_before_the_cut():
    out = google.GmailInbox(FakeGoogle(1, body="Hi\n\n\n   there,\r\n\tsee   you")).list_messages(limit=1)
    assert out["messages"][0]["body"] == "Hi there, see you"


def test_html_only_mail_falls_back_to_the_snippet():
    out = google.GmailInbox(FakeGoogle(1, html_only=True)).list_messages(limit=1)
    assert out["messages"][0]["body"] == "snippet text"


def test_draft_is_a_reply_in_the_same_thread_and_is_not_sent():
    fake = FakeGoogle(2)
    d = google.GmailInbox(fake).create_draft(message_id="g1", body="Thanks, will reply by Thursday.", idempotency_key="k1")
    assert d == {"draft_id": "r1", "to": "priya1@example.com", "subject": "Re: Subject 1", "created": True}
    sent = fake.drafts[0]["message"]
    assert sent["threadId"] == "th1"
    raw = base64.urlsafe_b64decode(sent["raw"]).decode()
    assert "In-Reply-To: <mid1@example.com>" in raw and "Thanks, will reply by Thursday." in raw
    assert not any("send" in c[1] for c in fake.calls)                # drafts.create only; nothing hit a send endpoint


def test_draft_idempotency_is_ours_because_gmail_has_none(tmp_path):
    store, fake = Store(tmp_path / "d.db"), FakeGoogle(2)
    box = google.GmailInbox(fake, drafts=store)
    first = box.create_draft(message_id="g0", body="hi", idempotency_key="same")
    again = google.GmailInbox(fake, drafts=store).create_draft(message_id="g0", body="hi", idempotency_key="same")
    assert first["created"] is True and again["created"] is False and again["draft_id"] == first["draft_id"]
    assert len(fake.drafts) == 1
    store.close()


def test_draft_to_an_unknown_message_is_a_tool_error_not_a_crash():
    with pytest.raises(tools.ToolError) as e:
        google.GmailInbox(FakeGoogle(1)).create_draft(message_id="nope", body="hi", idempotency_key="k")
    assert e.value.code == "unknown_message"


# ── Calendar adapter ─────────────────────────────────────────────────────────
def test_calendar_events_map_timed_and_all_day_and_skip_cancelled():
    def http(method, url, params=None, body=None):
        assert params["singleEvents"] == "true" and params["orderBy"] == "startTime"
        return {"items": [
            {"id": "1", "summary": "Standup", "start": {"dateTime": "2026-11-16T09:00:00-05:00"}, "end": {"dateTime": "2026-11-16T09:15:00-05:00"},
             "attendees": [{"email": "me@example.com", "self": True}, {"email": "sam@example.com"}]},
            {"id": "2", "summary": "Offsite", "start": {"date": "2026-11-16"}, "end": {"date": "2026-11-17"}},
            {"id": "3", "status": "cancelled", "start": {}, "end": {}},
            {"id": "4", "start": {"dateTime": "2026-11-16T10:00:00-05:00"}, "end": {"dateTime": "2026-11-16T11:00:00-05:00"}},
        ]}
    events = google.GoogleCalendar(http, now=lambda: datetime(2026, 11, 16, 14, 0, tzinfo=timezone.utc)).list_events(days=1)
    assert [e["id"] for e in events] == ["1", "2", "4"]
    assert events[0]["attendees"] == ["sam@example.com"]              # the user is not their own attendee
    assert events[1]["start"] == "2026-11-16" and events[2]["title"] == "(no title)"


# ── The tool surface ─────────────────────────────────────────────────────────
def test_real_adapters_sit_behind_the_same_tools_and_envelope():
    previous = (tools.inbox, tools.calendar, tools.BACKEND)
    try:
        tools.use_google(FakeGoogle(2))
        assert tools.BACKEND == "google"
        body = json.loads(tools.dispatch("read_inbox", {"limit": 5, "offset": 0}))
        assert body["ok"] is True and body["data"]["total"] == 2
        bad = json.loads(tools.dispatch("draft_reply", {"message_id": "nope", "body": "x", "idempotency_key": "k"}))
        assert bad["ok"] is False and bad["error"]["code"] == "unknown_message"
    finally:
        tools.inbox, tools.calendar, tools.BACKEND = previous


@pytest.fixture
def on_google(tmp_path):
    """Tools pointed at a fake Google, with a real store, restored afterwards."""
    from dayflow.store import Store
    previous = (tools.inbox, tools.calendar, tools.BACKEND, tools.tasks)
    store = Store(tmp_path / "g.db")
    tools.use_store(store)
    fake = FakeGoogle(2)
    tools.use_google(fake, drafts=store)
    yield store, fake
    tools.inbox, tools.calendar, tools.BACKEND, tools.tasks = previous
    store.close()


def _queue_draft(store):
    from dayflow import permissions
    gate = permissions.gate_for("queue", store=store)
    gate.call("draft_reply", {"message_id": "g0", "body": "Thanks, will do.", "idempotency_key": "k1"})
    return store.list_approvals()[0]


def test_a_proposal_remembers_the_backend_it_was_made_against(on_google):
    store, _ = on_google
    assert _queue_draft(store)["backend"] == "google"


def test_a_google_proposal_cannot_be_approved_against_fixtures(on_google):
    """What happened on the first real run: `dayflow approve` without MOCK=0 looked up a
    real Gmail id in the fixture inbox. Now it refuses, says why, and stays pending."""
    from dayflow import permissions
    store, fake = on_google
    row = _queue_draft(store)
    tools.BACKEND = "fixtures"                                   # the next command forgot MOCK=0
    with pytest.raises(permissions.ApprovalError, match=r"proposed against google.*running against fixtures.*MOCK=0 dayflow approve"):
        permissions.approve(store, row["id"])
    assert store.get_approval(row["id"])["status"] == "pending" and fake.drafts == []
    tools.BACKEND = "google"                                     # run it in the right world
    out = permissions.approve(store, row["id"])
    assert out["data"]["created"] is True and len(fake.drafts) == 1


def test_a_fixture_proposal_cannot_be_approved_against_google(on_google):
    from dayflow import permissions
    store, fake = on_google
    tools.BACKEND = "fixtures"
    row = _queue_draft(store)
    tools.BACKEND = "google"
    with pytest.raises(permissions.ApprovalError, match="MOCK=1 dayflow approve"):
        permissions.approve(store, row["id"])
    assert fake.drafts == []


def test_a_proposal_from_before_the_column_existed_is_not_blocked(on_google):
    from dayflow import permissions
    store, fake = on_google
    row = _queue_draft(store)
    store._conn.execute("UPDATE approvals SET backend = NULL WHERE id = ?", (row["id"],))
    assert permissions.approve(store, row["id"])["data"]["created"] is True


def test_draft_reply_is_a_write_tool_so_the_gate_holds_it():
    from dayflow.permissions import Gate, Policy, QueueApprover
    assert tools.REGISTRY["draft_reply"].kind == "write"
    out = Gate(Policy(), QueueApprover()).check("draft_reply", {"message_id": "m1", "body": "x", "idempotency_key": "k"})
    assert out.allowed is False and out.code == "pending_approval"


def test_fixture_draft_has_the_same_shape_as_the_real_one():
    body = json.loads(tools.dispatch("draft_reply", {"message_id": "m1", "body": "Thanks Priya.", "idempotency_key": "fx-1"}))
    assert set(body["data"]) == {"draft_id", "to", "subject", "created"}
    assert body["data"]["to"] == "priya@acme-consulting.com" and body["data"]["subject"].startswith("Re: ")
