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
```

## Quick start (TypeScript)

```
cd ts && npm install --include=dev
cp ../.env.example ../.env   # shared .env at repo root
npm run brief -- -v
```

## Layout

```
dayflow/
  loop.py        the agent loop (lesson 81)
  tools.py       tool schemas + dispatch (lesson 82 refactors this)
  prompts.py     system prompts, versioned separately from code
  adapters/      fixture adapters now; Gmail/Calendar adapters in lesson 87
fixtures/        deterministic inbox, calendar, tasks (also the eval + red-team base)
tests/           fake-client tests; no network
ts/              TypeScript mirror of the above
```

## Mock mode

`MOCK=1` (the default until lesson 87) runs everything against `fixtures/`. Deterministic,
free, and the same fixtures become the eval golden set in lesson 89 and the injection
red-team set in lesson 88.
