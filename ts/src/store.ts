// Run store on node:sqlite (Node 22.5+). Mirror of ../../dayflow/store.py.
import { DatabaseSync } from 'node:sqlite'
import type Anthropic from '@anthropic-ai/sdk'

export interface StepRow {
  n: number
  stopReason: string
  assistant: Anthropic.ContentBlockParam[]
  toolResults: Anthropic.ToolResultBlockParam[] | null
  inputTokens: number
  outputTokens: number
}
export interface RunRow {
  id: string; kind: string; status: string; system: string; user_msg: string; model: string
  input_tokens: number; output_tokens: number; final_text: string | null; error: string | null
}
export interface TaskRow { id: string; title: string; due: string | null; done: boolean }

const SCHEMA = `
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, system TEXT NOT NULL, user_msg TEXT NOT NULL,
  model TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  final_text TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS steps (
  run_id TEXT NOT NULL REFERENCES runs(id), n INTEGER NOT NULL, stop_reason TEXT NOT NULL, assistant TEXT NOT NULL,
  tool_results TEXT, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, n));
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, due TEXT, done INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL);
`
const now = () => new Date().toISOString().slice(0, 19) + '+00:00'

export class Store {
  private db: DatabaseSync
  constructor(path = process.env.DAYFLOW_DB ?? 'dayflow.db') {
    this.db = new DatabaseSync(path)
    this.db.exec('PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;')
    this.db.exec(SCHEMA)
    this.migrate()
  }
  /** Lesson 84: SDK-driven runs carry the SDK session_id (the SDK owns resume now). */
  private migrate() {
    const cols = new Set((this.db.prepare('PRAGMA table_info(runs)').all() as Array<{ name: string }>).map(c => c.name))
    if (!cols.has('session_id')) this.db.exec('ALTER TABLE runs ADD COLUMN session_id TEXT')
  }
  recordSdkTotals(id: string, sessionId: string, inputTokens: number, outputTokens: number) {
    this.tx(() => this.db.prepare('UPDATE runs SET session_id=?, input_tokens=?, output_tokens=?, updated_at=? WHERE id=?')
      .run(sessionId, inputTokens, outputTokens, now(), id))
  }
  private tx<T>(fn: () => T): T {
    this.db.exec('BEGIN')
    try { const r = fn(); this.db.exec('COMMIT'); return r } catch (e) { this.db.exec('ROLLBACK'); throw e }
  }

  createRun(a: { kind: string; system: string; userMsg: string; model: string }): string {
    const id = crypto.randomUUID().replace(/-/g, '').slice(0, 12)
    const t = now()
    this.tx(() => this.db.prepare('INSERT INTO runs (id,kind,status,system,user_msg,model,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)')
      .run(id, a.kind, 'running', a.system, a.userMsg, a.model, t, t))
    return id
  }
  getRun(id: string): RunRow | undefined { return this.db.prepare('SELECT * FROM runs WHERE id = ?').get(id) as RunRow | undefined }
  latestRun(status?: string): RunRow | undefined {
    return (status
      ? this.db.prepare('SELECT * FROM runs WHERE status = ? ORDER BY created_at DESC LIMIT 1').get(status)
      : this.db.prepare('SELECT * FROM runs ORDER BY created_at DESC LIMIT 1').get()) as RunRow | undefined
  }
  finishRun(id: string, text: string) { this.tx(() => this.db.prepare("UPDATE runs SET status='done', final_text=?, updated_at=? WHERE id=?").run(text, now(), id)) }
  failRun(id: string, error: string) { this.tx(() => this.db.prepare("UPDATE runs SET status='failed', error=?, updated_at=? WHERE id=?").run(error, now(), id)) }
  markRunning(id: string) { this.tx(() => this.db.prepare("UPDATE runs SET status='running', error=NULL WHERE id=?").run(id)) }

  /** Step + token totals in one transaction. */
  appendStep(runId: string, s: StepRow) {
    this.tx(() => {
      this.db.prepare('INSERT INTO steps (run_id,n,stop_reason,assistant,tool_results,input_tokens,output_tokens,created_at) VALUES (?,?,?,?,?,?,?,?)')
        .run(runId, s.n, s.stopReason, JSON.stringify(s.assistant), s.toolResults ? JSON.stringify(s.toolResults) : null, s.inputTokens, s.outputTokens, now())
      this.db.prepare('UPDATE runs SET input_tokens=input_tokens+?, output_tokens=output_tokens+?, updated_at=? WHERE id=?')
        .run(s.inputTokens, s.outputTokens, now(), runId)
    })
  }
  steps(runId: string): StepRow[] {
    return (this.db.prepare('SELECT * FROM steps WHERE run_id = ? ORDER BY n').all(runId) as Array<Record<string, unknown>>).map(r => ({
      n: r.n as number, stopReason: r.stop_reason as string, assistant: JSON.parse(r.assistant as string),
      toolResults: r.tool_results ? JSON.parse(r.tool_results as string) : null,
      inputTokens: r.input_tokens as number, outputTokens: r.output_tokens as number,
    }))
  }
  rebuildMessages(runId: string): Anthropic.MessageParam[] {
    const run = this.getRun(runId); if (!run) throw new Error(`no run ${runId}`)
    const m: Anthropic.MessageParam[] = [{ role: 'user', content: run.user_msg }]
    for (const s of this.steps(runId)) {
      m.push({ role: 'assistant', content: s.assistant })
      if (s.toolResults) m.push({ role: 'user', content: s.toolResults })
    }
    return m
  }

  seedTasks(tasks: Array<{ id: string; title: string; due?: string | null; done?: boolean }>) {
    this.tx(() => { for (const t of tasks) this.db.prepare('INSERT OR IGNORE INTO tasks (id,title,due,done,idempotency_key,created_at) VALUES (?,?,?,?,NULL,?)').run(t.id, t.title, t.due ?? null, t.done ? 1 : 0, now()) })
  }
  listTasks(includeDone = false): TaskRow[] {
    const rows = this.db.prepare(`SELECT id,title,due,done FROM tasks ${includeDone ? '' : 'WHERE done = 0'} ORDER BY due IS NULL, due`).all() as Array<Record<string, unknown>>
    return rows.map(r => ({ id: r.id as string, title: r.title as string, due: r.due as string | null, done: !!r.done }))
  }
  createTask(title: string, due: string | null, idempotencyKey: string) {
    return this.tx(() => {
      const ex = this.db.prepare('SELECT id,title,due,done FROM tasks WHERE idempotency_key = ?').get(idempotencyKey) as Record<string, unknown> | undefined
      if (ex) return { id: ex.id, title: ex.title, due: ex.due, done: !!ex.done, created: false }
      const n = (this.db.prepare('SELECT COUNT(*) AS c FROM tasks').get() as { c: number }).c
      const id = `t${n + 1}`
      this.db.prepare('INSERT INTO tasks (id,title,due,done,idempotency_key,created_at) VALUES (?,?,?,0,?,?)').run(id, title, due, idempotencyKey, now())
      return { id, title, due, done: false, created: true }
    })
  }
  close() { this.db.close() }
}
