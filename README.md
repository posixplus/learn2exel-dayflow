# Dayflow

The personal ops agent built across **Learn2Exel Level 10: Agentic AI Engineer**.

One application, twelve lessons. Each lesson adds a layer: the raw loop, tool design, state,
the Claude Agent SDK, orchestration, permissions, real Google OAuth, security, evals,
observability, reliability, and MCP. The capstone deploys it and runs Day 2.

Python is the primary implementation (`dayflow/`). A TypeScript mirror lives in `ts/`.

## Lesson tags

Every lesson has a git tag with the exact starting state:

```
git checkout lesson-81   # the raw loop
git checkout lesson-82   # tool design
...
git checkout lesson-92   # MCP server
git checkout main        # capstone / latest
```

## Quick start (Python)

```
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # add your ANTHROPIC_API_KEY
pytest                    # loop tests, no API key needed
dayflow brief -v          # morning brief from fixtures (MOCK=1)
dayflow brief --crash-after 2   # lesson 83: die after step 2 is committed
dayflow resume -v         # continue the latest unfinished run
dayflow runs              # list recent runs
dayflow brief --engine sdk -v   # lesson 84: same brief, same tools, Claude Agent SDK drives
dayflow brief --engine orch -v  # lesson 85: three specialists in parallel + a synthesiser; prints a cost table
DAYFLOW_INBOX=large dayflow brief --engine orch   # 120-message inbox: where orchestration earns its keep
dayflow brief                   # lesson 86: on a terminal, each write asks you first (y / n / a)
dayflow brief --approve queue   # nobody there: writes are recorded, not run
dayflow approvals               # what is waiting on you   (--all = the audit trail)
dayflow approve ap_1a2b3c       # run that proposal exactly as recorded
dayflow deny ap_1a2b3c -m "already handled"
dayflow brief --approve auto    # the pre-86 behaviour, said out loud
dayflow auth login              # lesson 87: Google sign-in in your browser (read-only scopes)
dayflow auth login --drafts     # ...and permission to create drafts
dayflow auth status             # what is granted, and whether draft_reply is offered
MOCK=0 dayflow brief            # your real inbox and calendar
DAYFLOW_INBOX=redteam dayflow brief --approve auto   # lesson 88: five planted attacks; nothing is written
dayflow auth logout             # revoke at Google and delete the local token
```

## Quick start (TypeScript)

```
cd ts && npm install --include=dev
cp ../.env.example ../.env   # shared .env at repo root
npm run brief -- -v
npm run brief -- --engine sdk -v   # lesson 84
npm run brief -- --engine orch -v  # lesson 85
npm run brief -- --approve queue   # lesson 86
npm run approvals                  # then: npm run approve -- ap_1a2b3c   /   npm run deny -- ap_1a2b3c -m "why"
```

## Layout

```
dayflow/
  loop.py        the agent loop (81) with per-step checkpoints and resume (83)
  store.py       SQLite run store: runs, steps, tasks (83), approvals (86)
  compact.py     history compaction primitive (83; the SDK does this for you from 84)
  sdk_agent.py   the same tools wrapped as an in-process MCP server, driven by the Agent SDK (84)
  clock.py       what 'today' means: DAYFLOW_TODAY > fixture Monday under MOCK > wall clock (84)
  orchestrate.py fan-out to one-tool specialists on Haiku, fan-in to a tool-less synthesiser (85)
  permissions.py Policy (allow / deny / ask), approvers (terminal, queue) and the Gate both engines call (86)
  security.py    rules on write arguments, taint, and an injection scan that is a signal, not the defence (88)
  tools.py       strict tool registry + result envelope (82)
  prompts.py     system prompts, versioned separately from code
  oauth.py       Google OAuth by hand: PKCE, loopback redirect, refresh, revoke, 0600 token file (87)
  adapters/      fixtures.py (deterministic) and google.py (real Gmail + Calendar, lesson 87); same interface
fixtures/        deterministic inbox, calendar, tasks (also the eval + red-team base)
                 inbox_large.json: 120 messages from gen_inbox_large.py, selected with DAYFLOW_INBOX=large (85)
tests/           fake-client tests (no network) + test_live.py (real API, DAYFLOW_LIVE=1)
ts/              TypeScript mirror of the above
```

## Live tests and CI

`pytest` runs the unit suite with a fake client. `DAYFLOW_LIVE=1 pytest tests/test_live.py`
runs seven small real-API tests with hard token budgets (about $0.15 per run; the SDK and orchestration tests are the expensive ones).
`.github/workflows/ci.yml` runs the unit suite on every PR and the live suite on pushes to
`main` when `ANTHROPIC_API_KEY` is set as a repository secret. (An earlier version of this
README promised lesson 89 would replace that secret; it does not. How the key is held in a
deployment is a capstone topic.)

## Mock mode

`MOCK=1` (still the default) runs everything against `fixtures/`. Deterministic,
free, and the same fixtures become the eval golden set in lesson 89 and the injection
red-team set in lesson 88.

## Two engines, one agent (lesson 84)

`--engine raw` is the loop you wrote in lessons 81-83. `--engine sdk` hands the loop to the
Claude Agent SDK and keeps everything that was actually yours: the registry, the envelope,
the store. Both engines log steps to the same `steps` table; SDK runs also record the
SDK `session_id` on the run, because the SDK session (not the steps table) is what resumes
them. `--crash-after` is a raw-engine flag; the SDK checkpoints on its own.

## Three engines (lesson 85)

| engine | what runs | small inbox (5 msgs) | large inbox (120 msgs) |
|---|---|---|---|
| `raw`  | your loop, Sonnet 5.5 | 3 steps, ~7.4K uncached in, ~$0.03 | not measured; same shape as sdk |
| `sdk`  | one Agent SDK session, Sonnet 5.5 | 5 turns, ~$0.02, ~10 s | 8 turns, 40K cached in, **$0.084**, 19 s, found 7 of 9 replies |
| `orch` | 3 Haiku specialists ‖ + 1 Sonnet synthesiser | 4 legs, ~$0.016, ~20 s | 4 legs, **$0.022**, 42 s, found 9 of 9 |

Orchestration is slower on both and no cheaper on the small inbox. On the large one it is 4x cheaper
(mostly from routing reads to Haiku) and more thorough (one specialist with one job). Those two
columns are the whole decision. `read_inbox` is paged (`limit` ≤ 50, `offset`, `next_offset`) because
the Agent SDK harness persists large tool results to disk and shows the model a 2 KB preview (a
66 KB result tripped it; a 14 KB page does not); with `tools=[]` the model cannot `Read` the rest, so the tool has to stay small.

## Permissions and the human in the loop (lesson 86)

Reads run. Writes do not, until a rule or a person says yes. One `Gate` sits in front of
`dispatch()` in the raw loop and inside a `PreToolUse` hook in the SDK engine, so both engines
make the same decision from the same code.

| `--approve` | who decides a write | when to use it |
|---|---|---|
| `ask` | you, at the terminal: `y`, `n` (with a reason the agent sees), `a` for the rest of the run | you are watching |
| `queue` | nobody yet: the proposal is stored with its exact arguments and the run finishes | scheduled or unattended runs |
| `auto` | the policy says yes | tests, and tools you have decided to trust |
| *(not given)* | `ask` if stdin is a terminal, otherwise `queue` | the default; a cron job never blocks on input |

Rules the code enforces, not the prompt:

- **Deny before allow.** The per-run write limit (2) applies in every mode, including `auto`.
- **What you approve is what runs.** `dayflow approve` replays the stored arguments through
  `dispatch()`. The model is not consulted again.
- **Approvals expire** after `DAYFLOW_APPROVAL_TTL_HOURS` (default 24).
- **Every write decision is a row** in `approvals`: allowed, denied, pending, approved or expired,
  and whether policy or a human decided.
- **The SDK engine has two locks.** `create_task` is not in `allowed_tools`, so the harness refuses
  it unless the hook answers `allow`. When the hook crashed during development, writes failed closed.

A refused or queued write returns `{"ok": false, "error": {"code": ...}}` with one of
`pending_approval`, `denied_by_user`, `read_only`, `write_limit`. None is retryable.

`--read-only` is still the stronger tool: it removes the write tool from the surface, so the model
cannot even propose. Use the gate for actions you want sometimes, and the surface for actions you
never want.

## Real Google (lesson 87)

`MOCK=0` swaps the fixture adapters for Gmail and Calendar. Tools, prompts, the gate and the
store do not change; `tools.use_google()` is the only switch.

One-time setup, about ten minutes, in [Google Cloud Console](https://console.cloud.google.com/):

1. Create a project. Enable the **Gmail API** and the **Google Calendar API**.
2. OAuth consent screen: user type **External**, publishing status **Testing**. Add your own
   Google address under **Test users**.
3. Credentials: **Create credentials > OAuth client ID > Desktop app**.
4. Put the two values in `.env`: `GOOGLE_CLIENT_ID=...` and `GOOGLE_CLIENT_SECRET=...`.
5. `dayflow auth login`, approve in the browser, then `MOCK=0 dayflow brief`.

Things worth knowing before you run it:

- **Scopes.** Login asks for `gmail.readonly` and `calendar.readonly`. `--drafts` adds
  `gmail.compose`, which Google does not split: it can also send. Dayflow has no send tool.
- **The surface follows the token.** Without the compose scope, `draft_reply` is removed from the
  registry, so the model is never offered a tool the token cannot back.
- **Testing-mode tokens expire in 7 days.** Google expires refresh tokens for apps in Testing.
  When that happens Dayflow says so and asks you to log in again.
- **The token file** is `~/.config/dayflow/google_token.json`, mode 600 (`DAYFLOW_TOKEN_PATH` to
  move it). A file readable by other users is refused.
- **Approve where you proposed.** Each proposal records its backend. `dayflow approve` under
  `MOCK=1` refuses a proposal made against Google (and the reverse) and prints the command to run.
- **Drafts are not sent.** `draft_reply` creates a draft in the original thread. You send it from
  Gmail. Gmail has no idempotency key for drafts, so the store keeps its own key-to-draft map.
- **A run reads the newest 30 messages** from the last 3 days, skipping the Promotions and Social
  tabs, with each body cut at 400 characters. When more matched, the result says so. Override with
  `DAYFLOW_INBOX_MAX`, `DAYFLOW_INBOX_QUERY`, `DAYFLOW_BODY_CHARS`. The first defaults (200 messages,
  1,500 characters) were sized against fixtures; on a real inbox one brief passed 400,000 input
  tokens and the token budget stopped it.

The TypeScript mirror runs `draft_reply` against fixtures only. The OAuth flow and the Google
adapters are Python-only for now.

## Security (lesson 88)

Everything Dayflow reads was written by someone else. `fixtures/inbox_redteam.json` adds five
attacks to the normal inbox: a fake system notice ordering a wire-transfer task, a request to
reply with another email's contents, hidden text pushing a password link, an email shaped like
a tool result claiming all writes were approved, and a look-alike sender asking for "Approved, go ahead."

In live runs the model refused all five every time (Sonnet 5.5 and Haiku 5.5, raw and orchestrated).
That is behaviour, not a guarantee, so `tests/test_security.py` scripts a model that **obeys** each
attack and checks what the code allows:

| Control | Where | Trust it? |
|---|---|---|
| No send tool; `draft_reply` can only answer an existing message | the tool surface | Yes. It is absent, not forbidden |
| Content rules: no links in a write, drafts at most 600 chars, no text copied from a different message, no reply to a flagged message | `security.content_violation`, checked in `Policy.decide` before anyone is asked | Yes for what they cover. Paraphrase gets past the copy rule |
| Taint: a run that read flagged mail gets nothing auto-approved | `Policy.decide(tainted=...)` | Yes, as far as the scan fires |
| Approval shows the resolved recipient and a warning | `permissions.render` | Depends on the human reading it |
| Flagged links repeated in the brief are reported from code | `security.flagged_links_in` | Yes for exact links |
| Pattern scan | `security.scan` | No. One reworded sentence evades it; a test proves that |

The refusal code for a content rule is `content_rule`. The orchestrated engine's inbox report gained
a `SUSPICIOUS:` section, because a fixed report format silently dropped the model's own warnings.

## Evals (lesson 89)

Unit tests check the code. Evals check the agent, and the answer is a rate.

```bash
dayflow eval --calibrate                 # is the judge model trustworthy? 8 hand-labelled examples
dayflow eval -n 5 --out baseline.json    # both suites, 5 trials per case, real API
dayflow eval -n 5 --model claude-haiku-5-5 --compare baseline.json
```

- `dayflow/evals.py`: `Case` (which inbox), `Trace` (what a run did), `Check` (one question about a
  trace). Two suites: `clean` (does it do the job) and `redteam` (does it hold under attack, run
  with `--approve auto` so the controls are tested at their most permissive setting).
- **must** checks fail the suite on any failed trial and set the exit code. **track** checks are
  reported and never block: use them for behaviour you want to watch before you gate on it.
- Every rate is printed with its count and a 95% Wilson interval. `3/3` prints as `[0.44, 1.00]`.
- One check is graded by a model (`one_thing_defensible`). `--calibrate` runs that judge over
  `fixtures/judge_calibration.json`; do not read its numbers until it agrees with the labels.
- `--compare` calls a drop a regression only when the intervals do not overlap.
- `tests/test_evals.py` unit-tests every grader against a trace built to pass and one built to fail.

Cost on the fixtures: about 13k input / 2k output tokens per trial. Python only; there is no
TypeScript mirror of the eval harness.

## Observability and cost (lesson 90)

```bash
dayflow brief            # footer now shows cached tokens and dollars
dayflow trace [run_id]   # one run as a waterfall: each model call, its tool calls, ms, tokens, cost
dayflow stats            # recent runs by model: failures, p50/p95, tokens, cached %, $/run
DAYFLOW_CACHE=0 dayflow brief   # turn prompt caching off to see what it saves
```

- `events` table: one row per model call and per tool call (wall time, uncached / cache-read /
  cache-write / output tokens, outcome). Written by the raw loop; SDK and orchestrated runs
  report their own cost and are not in `trace` yet.
- `dayflow/observe.py`: the price table, `cost()`, `trace()`, `stats()`. A model with no price
  prints `n/a`, never `$0.00`.
- Prompt caching is on by default: one breakpoint after the system prompt (covers the tools too)
  and one on the last block of the conversation. Cached tokens still count toward `--token-budget`.
- Measured on the fixtures, Sonnet 5.5: $0.041 per run uncached, $0.032 for a cold cached run,
  about $0.024 warm. Haiku 5.5: about $0.002.

## Reliability (lesson 91)

```bash
DAYFLOW_FAULTS="read_inbox=2" dayflow brief        # inbox fails twice, then works: retried, invisible to the model
DAYFLOW_FAULTS="read_inbox=always" dayflow brief   # an outage: the brief says so and carries on with the rest
DAYFLOW_FAULTS="read_calendar=auth" dayflow brief  # a 401: reported at once, never retried
dayflow eval --suite degraded -n 5                 # does it degrade honestly? (inbox down for the whole run)
```

- `dayflow/retry.py`: exponential backoff with full jitter, capped, with a hard attempt limit.
- `tools.dispatch()` retries a failing READ up to 3 times when the error is retryable. A WRITE is
  never retried automatically. `_classify()` decides what is retryable: 429, 5xx, timeouts and
  connection errors are; 401, 403, 404 and unexpected exceptions (bugs) are not.
- `dayflow/faults.py`: fault injection for the fixture tools, off unless `DAYFLOW_FAULTS` is set.
- The Anthropic client is built with explicit `max_retries` and `timeout` (`DAYFLOW_API_RETRIES`,
  `DAYFLOW_API_TIMEOUT`). When those are spent the run is marked failed with a reason and
  `dayflow resume` continues from the last committed step.
- `dayflow trace` shows `ok (after 3 attempts)` on a retried call.

