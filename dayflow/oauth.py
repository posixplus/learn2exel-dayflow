"""Google OAuth for an installed app, from scratch. Lesson 87.

Lesson 81 wrote the agent loop by hand so you would know what a framework does
for you. This is the same exercise for OAuth: about 150 lines of standard
library, so that every step of "Sign in with Google" is something you have read.

The flow (authorization code + PKCE, loopback redirect):

    1. Make a random `verifier`. Send its SHA-256 hash (the `challenge`) to Google
       with the consent request. Only this process knows the verifier.
    2. Open the browser at Google's consent page. Listen on 127.0.0.1:<random port>.
    3. The user approves. Google redirects the browser to our local port with a
       one-time `code` and the `state` we sent. Wrong state: stop.
    4. POST the code AND the verifier to the token endpoint. Google checks the
       verifier against the challenge from step 1, so a stolen code is useless.
    5. Store the tokens in a file only this user can read.

Access tokens last about an hour. The refresh token gets a new one without the
browser. Dayflow never sees the Google password, and the user can cut it off at
myaccount.google.com/permissions at any time without telling us.

What this module will not do: ask for more than it needs. SCOPES_READ is the
default. The compose scope is requested only by `dayflow auth login --drafts`.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Least privilege, spelled out. Read-only for mail and calendar is all a brief needs.
SCOPES_READ = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
)
# Needed to create a draft. Google has no "drafts only" scope: this one can also
# SEND. Dayflow has no send tool, but the token could. Lesson 88 starts there.
SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"

DEFAULT_TOKEN_PATH = Path(os.environ.get("DAYFLOW_TOKEN_PATH", "~/.config/dayflow/google_token.json")).expanduser()

# (url, form fields) -> parsed JSON. Injectable so tests never touch the network.
Poster = Callable[[str, dict[str, str]], dict[str, Any]]


class OAuthError(RuntimeError):
    pass


@dataclass
class Token:
    access_token: str
    refresh_token: str | None
    expires_at: float                 # unix seconds
    scopes: list[str] = field(default_factory=list)
    obtained_at: float = 0.0          # capstone: when the user last consented. 0 for tokens saved before this field existed.

    def expired(self, *, now: float | None = None, skew: float = 60) -> bool:
        """True a minute early, so a token cannot die between the check and the call."""
        return (now if now is not None else time.time()) >= self.expires_at - skew

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True)
class Client:
    """The OAuth client you create in Google Cloud Console (type: Desktop app).
    For an installed app the 'secret' is not secret: it ships with the program.
    PKCE is what protects the flow."""
    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls) -> "Client":
        cid, sec = os.environ.get("GOOGLE_CLIENT_ID"), os.environ.get("GOOGLE_CLIENT_SECRET")
        if not cid or not sec:
            raise OAuthError("set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env (see the lesson 87 setup steps)")
        return cls(cid, sec)


# ── PKCE ─────────────────────────────────────────────────────────────────────
def make_pkce() -> tuple[str, str]:
    """(verifier, challenge). challenge = base64url(sha256(verifier)), no padding."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def consent_url(client: Client, *, redirect_uri: str, scopes: tuple[str, ...], state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",        # ask for a refresh token
        "prompt": "consent",             # ...and get one even on a repeat login
    })


# ── Token endpoint ───────────────────────────────────────────────────────────
def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            detail = json.loads(body)
        except ValueError:
            detail = {"error": body[:200].decode(errors="replace")}
        raise OAuthError(f"{detail.get('error', 'http_error')}: {detail.get('error_description', e.reason)}") from e
    return json.loads(body) if body else {}


def _token_from(payload: dict[str, Any], *, previous: Token | None = None, now: float | None = None) -> Token:
    return Token(
        access_token=payload["access_token"],
        # A refresh response usually omits refresh_token. Keep the one we have.
        refresh_token=payload.get("refresh_token") or (previous.refresh_token if previous else None),
        expires_at=(now if now is not None else time.time()) + float(payload.get("expires_in", 3600)),
        scopes=payload["scope"].split() if payload.get("scope") else (previous.scopes if previous else []),
        # A refresh is not a new consent: Google's 7-day clock for apps in Testing keeps running.
        obtained_at=previous.obtained_at if previous else (now if now is not None else time.time()),
    )


def exchange_code(client: Client, *, code: str, verifier: str, redirect_uri: str, post: Poster = _post_form) -> Token:
    return _token_from(post(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "redirect_uri": redirect_uri,
    }))


def refresh(client: Client, token: Token, *, post: Poster = _post_form) -> Token:
    if not token.refresh_token:
        raise OAuthError("no refresh token stored; run `dayflow auth login` again")
    try:
        payload = post(TOKEN_URL, {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
        })
    except OAuthError as e:
        if "invalid_grant" in str(e):
            # Revoked by the user, expired (7 days for an app in Testing), or the
            # password changed. Nothing we can retry: the user has to consent again.
            raise OAuthError("Google rejected the refresh token (revoked or expired). Run `dayflow auth login`.") from e
        raise
    return _token_from(payload, previous=token)


def revoke(token: Token, *, post: Poster = _post_form) -> None:
    """Tell Google to forget the grant. Revoking the refresh token kills the access token too."""
    post(REVOKE_URL, {"token": token.refresh_token or token.access_token})


# ── Token storage ────────────────────────────────────────────────────────────
def token_store() -> str:
    """'file' (lesson 87, the default) or 'keychain' (capstone). Read at call time so
    .env and tests can change it."""
    return os.environ.get("DAYFLOW_TOKEN_STORE", "file").lower()


def _in_keychain(path: Path) -> bool:
    # Only the default location moves to the keychain. An explicit path is always a file,
    # which is what every test and DAYFLOW_TOKEN_PATH user expects.
    return token_store() == "keychain" and path == DEFAULT_TOKEN_PATH


def token_location(path: Path = DEFAULT_TOKEN_PATH) -> str:
    return "the system keychain (service dayflow, item google-token)" if _in_keychain(path) else f"{path} (mode 600)"


def save_token(token: Token, path: Path = DEFAULT_TOKEN_PATH) -> None:
    """Owner read/write only, set BEFORE the secret is written into the file."""
    if _in_keychain(path):
        from dayflow import vault
        try:
            vault.put(vault.GOOGLE_TOKEN, json.dumps(asdict(token)))
        except vault.VaultError as e:
            raise OAuthError(str(e)) from e
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(token), f)
    os.chmod(path, 0o600)            # also fixes a file that already existed with wider permissions


def load_token(path: Path = DEFAULT_TOKEN_PATH) -> Token | None:
    if _in_keychain(path):
        from dayflow import vault
        try:
            raw = vault.get(vault.GOOGLE_TOKEN)
        except vault.VaultError as e:
            raise OAuthError(str(e)) from e
        return Token(**json.loads(raw)) if raw else None
    if not path.exists():
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if os.name == "posix" and mode & 0o077:
        raise OAuthError(f"{path} is readable by other users (mode {mode:o}). Fix with: chmod 600 {path}")
    return Token(**json.loads(path.read_text()))


def delete_token(path: Path = DEFAULT_TOKEN_PATH) -> bool:
    if _in_keychain(path):
        from dayflow import vault
        try:
            return vault.delete(vault.GOOGLE_TOKEN)
        except vault.VaultError as e:
            raise OAuthError(str(e)) from e
    if path.exists():
        path.unlink()
        return True
    return False


def valid_token(client: Client, *, path: Path = DEFAULT_TOKEN_PATH, post: Poster = _post_form) -> Token:
    """The one call the rest of Dayflow makes: a token that works right now."""
    token = load_token(path)
    if token is None:
        raise OAuthError("not signed in to Google. Run `dayflow auth login`.")
    if token.expired():
        token = refresh(client, token, post=post)
        save_token(token, path)
    return token


# ── The browser round trip ───────────────────────────────────────────────────
def login(client: Client, *, scopes: tuple[str, ...] = SCOPES_READ, open_browser: Callable[[str], Any] = webbrowser.open,
          post: Poster = _post_form, timeout: float = 300, path: Path = DEFAULT_TOKEN_PATH) -> Token:
    """Run the whole flow and store the token. Blocks until the user finishes in the browser."""
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(24)
    got: dict[str, str] = {}
    done = threading.Event()

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h3>Dayflow is connected. You can close this tab.</h3>" if "code" in got
                             else b"<h3>Dayflow was not connected. Check the terminal.</h3>")
            done.set()

        def log_message(self, *_args: Any) -> None:   # keep the code out of the terminal scrollback
            pass

    # Bind to the loopback address only, on a port the OS picks. Nothing off this machine can reach it.
    server = http.server.HTTPServer(("127.0.0.1", 0), Callback)
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = consent_url(client, redirect_uri=redirect_uri, scopes=scopes, state=state, challenge=challenge)
        print(f"Opening your browser to sign in. If it does not open, paste this URL:\n{url}\n")
        open_browser(url)
        if not done.wait(timeout):
            raise OAuthError(f"no response from the browser after {int(timeout)} s")
    finally:
        server.shutdown()
        server.server_close()

    if got.get("state") != state:
        # Someone else's redirect reached our port, or the response was tampered with.
        raise OAuthError("state mismatch: the response did not come from the request we started")
    if "error" in got:
        raise OAuthError(f"consent was not given: {got['error']}")
    if "code" not in got:
        raise OAuthError("Google returned no authorization code")

    token = exchange_code(client, code=got["code"], verifier=verifier, redirect_uri=redirect_uri, post=post)
    missing = [s for s in scopes if not token.allows(s)]
    if missing:
        # The consent screen lets the user untick individual scopes. Believe the response, not the request.
        raise OAuthError("you did not grant: " + ", ".join(s.rsplit('/', 1)[-1] for s in missing))
    save_token(token, path)
    return token
