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
```

## Quick start (TypeScript)

```
cd ts && npm install --include=dev
cp ../.env.example ../.env   # shared .env at repo root
npm run brief -- -v
npm run brief -- --engine sdk -v   # lesson 84
```

## Layout

```
dayflow/
  loop.py        the agent loop (81) with per-step checkpoints and resume (83)
  store.py       SQLite run store: runs, steps, tasks (83)
  compact.py     history compaction primitive (83; the SDK does this for you from 84)
  sdk_agent.py   the same tools wrapped as an in-process MCP server, driven by the Agent SDK (84)
  clock.py       what 'today' means: DAYFLOW_TODAY > fixture Monday under MOCK > wall clock (84)
  tools.py       strict tool registry + result envelope (82)
  prompts.py     system prompts, versioned separately from code
  adapters/      fixture adapters now; Gmail/Calendar adapters in lesson 87
fixtures/        deterministic inbox, calendar, tasks (also the eval + red-team base)
tests/           fake-client tests (no network) + test_live.py (real API, DAYFLOW_LIVE=1)
ts/              TypeScript mirror of the above
```

## Live tests and CI

`pytest` runs the unit suite with a fake client. `DAYFLOW_LIVE=1 pytest tests/test_live.py`
runs five small real-API tests with hard token budgets (about $0.10 per run on Sonnet 5.5; the SDK test is the expensive one).
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
