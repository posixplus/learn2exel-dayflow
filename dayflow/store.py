"""Run store. Lesson 83: every run is a row, every step is a row.

Two tables:
  runs   one per run: status, token totals, the final text
  steps  one per loop iteration: the assistant turn (as returned, tool_use
         blocks included) and the tool_results we sent back

The message history of a run is REBUILT from the steps table. That is the
whole trick behind resumability: if the process dies after step 4, the rows
for steps 1 to 4 are already committed, and a resume replays them into
`messages` and calls the API for step 5.

Tasks (lesson 82's write tool) move here too, so a created task outlives the
process. SQLite is the right database for a single-user agent on one machine;
lesson 93 swaps it for Postgres when Dayflow runs on a server.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_PATH = Path(os.environ.get("DAYFLOW_DB", "dayflow.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,            -- 'brief' for now
  status        TEXT NOT NULL,            -- running | done | failed
  system        TEXT NOT NULL,
  user_msg      TEXT NOT NULL,
  model         TEXT NOT NULL,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  final_text    TEXT,
  error         TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
  run_id        TEXT NOT NULL REFERENCES runs(id),
  n             INTEGER NOT NULL,         -- 1-based step index
  stop_reason   TEXT NOT NULL,
  assistant     TEXT NOT NULL,            -- JSON: the assistant content blocks
  tool_results  TEXT,                     -- JSON: the tool_result blocks we sent back (null on end_turn)
  input_tokens  INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (run_id, n)
);
CREATE TABLE IF NOT EXISTS tasks (
  id              TEXT PRIMARY KEY,
  title           TEXT NOT NULL,
  due             TEXT,
  done            INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT UNIQUE,            -- NULL for seeded fixtures
  created_at      TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class StepRow:
    n: int
    stop_reason: str
    assistant: list[dict[str, Any]]
    tool_results: list[dict[str, Any]] | None
    input_tokens: int
    output_tokens: int


class Store:
    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path, isolation_level=None)  # autocommit; we manage txns
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")   # readers never block the writer
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One transaction. A step is written atomically or not at all."""
        self._conn.execute("BEGIN")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ── runs ──
    def create_run(self, *, kind: str, system: str, user_msg: str, model: str) -> str:
        run_id = uuid.uuid4().hex[:12]
        now = _now()
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs (id, kind, status, system, user_msg, model, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, kind, "running", system, user_msg, model, now, now),
            )
        return run_id

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    def latest_run(self, *, status: str | None = None) -> sqlite3.Row | None:
        q = "SELECT * FROM runs" + (" WHERE status = ?" if status else "") + " ORDER BY created_at DESC LIMIT 1"
        return self._conn.execute(q, (status,) if status else ()).fetchone()

    def finish_run(self, run_id: str, *, text: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE runs SET status='done', final_text=?, updated_at=? WHERE id=?", (text, _now(), run_id))

    def fail_run(self, run_id: str, *, error: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE runs SET status='failed', error=?, updated_at=? WHERE id=?", (error, _now(), run_id))

    # ── steps ──
    def append_step(self, run_id: str, step: StepRow) -> None:
        """Write the step AND bump the run's token totals in one transaction, so a
        crash between the two cannot leave the totals out of sync with the steps."""
        with self.tx() as c:
            c.execute(
                "INSERT INTO steps (run_id, n, stop_reason, assistant, tool_results, input_tokens, output_tokens, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, step.n, step.stop_reason, json.dumps(step.assistant),
                 json.dumps(step.tool_results) if step.tool_results is not None else None,
                 step.input_tokens, step.output_tokens, _now()),
            )
            c.execute(
                "UPDATE runs SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?, updated_at=? WHERE id=?",
                (step.input_tokens, step.output_tokens, _now(), run_id),
            )

    def steps(self, run_id: str) -> list[StepRow]:
        rows = self._conn.execute("SELECT * FROM steps WHERE run_id = ? ORDER BY n", (run_id,)).fetchall()
        return [StepRow(r["n"], r["stop_reason"], json.loads(r["assistant"]),
                        json.loads(r["tool_results"]) if r["tool_results"] else None,
                        r["input_tokens"], r["output_tokens"]) for r in rows]

    def rebuild_messages(self, run_id: str) -> list[dict[str, Any]]:
        """The exact `messages` list the loop would have at the end of the last
        committed step. This is what resume feeds back to the API."""
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        messages: list[dict[str, Any]] = [{"role": "user", "content": run["user_msg"]}]
        for s in self.steps(run_id):
            messages.append({"role": "assistant", "content": s.assistant})
            if s.tool_results is not None:
                messages.append({"role": "user", "content": s.tool_results})
        return messages

    # ── tasks (persisted version of lesson 82's write tool) ──
    def seed_tasks(self, tasks: list[dict[str, Any]]) -> None:
        """Load fixture tasks once. Idempotent: existing ids are left alone."""
        with self.tx() as c:
            for t in tasks:
                c.execute(
                    "INSERT OR IGNORE INTO tasks (id, title, due, done, idempotency_key, created_at) VALUES (?,?,?,?,NULL,?)",
                    (t["id"], t["title"], t.get("due"), 1 if t.get("done") else 0, _now()),
                )

    def list_tasks(self, include_done: bool = False) -> list[dict[str, Any]]:
        q = "SELECT id, title, due, done FROM tasks" + ("" if include_done else " WHERE done = 0") + " ORDER BY due IS NULL, due"
        return [{"id": r["id"], "title": r["title"], "due": r["due"], "done": bool(r["done"])} for r in self._conn.execute(q)]

    def create_task(self, *, title: str, due: str | None, idempotency_key: str) -> dict[str, Any]:
        with self.tx() as c:
            existing = c.execute("SELECT id, title, due, done FROM tasks WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if existing:
                return {"id": existing["id"], "title": existing["title"], "due": existing["due"], "done": bool(existing["done"]), "created": False}
            n = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            task_id = f"t{n + 1}"
            c.execute(
                "INSERT INTO tasks (id, title, due, done, idempotency_key, created_at) VALUES (?,?,?,0,?,?)",
                (task_id, title, due, idempotency_key, _now()),
            )
        return {"id": task_id, "title": title, "due": due, "done": False, "created": True}

    def close(self) -> None:
        self._conn.close()
