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
  tools.py       strict tool registry + result envelope (82)
  prompts.py     system prompts, versioned separately from code
  adapters/      fixture adapters now; Gmail/Calendar adapters in lesson 87
fixtures/        deterministic inbox, calendar, tasks (also the eval + red-team base)
                 inbox_large.json: 120 messages from gen_inbox_large.py, selected with DAYFLOW_INBOX=large (85)
tests/           fake-client tests (no network) + test_live.py (real API, DAYFLOW_LIVE=1)
ts/              TypeScript mirror of the above
```

## Live tests and CI

`pytest` runs the unit suite with a fake client. `DAYFLOW_LIVE=1 pytest tests/test_live.py`
runs seven small real-API tests with hard token budgets (about $0.15 per run; the SDK and orchestration tests are the expensive ones).
`.github/workflows/ci.yml` runs the unit suite on every PR and the live suite on pushes to
`main` when `ANTHROPIC_API_KEY` is set as a repository secret. Lesson 89 replaces that
secret with identity federation.

## Mock mode

`MOCK=1` (the default until lesson 87) runs everything against `fixtures/`. Deterministic,
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

