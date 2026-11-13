// Lesson 86: permissions and the human in the loop. Mirror of ../../dayflow/permissions.py,
// which carries the full commentary. Policy decides in code, an Approver answers "ask",
// the Gate ties both to the store and to dispatch(). Both engines use the same Gate.
import { createInterface } from 'node:readline/promises'
import type { Store } from './store.js'
import { REGISTRY, dispatch, type Tool } from './tools.js'
import { RUN, contentViolation, reset } from './security.js'
import { inbox } from './fixtures.js'

export type Behavior = 'allow' | 'deny' | 'ask'
export type WritesMode = 'ask' | 'allow' | 'deny'
export const PENDING = 'pending_approval', DENIED = 'denied_by_user', READ_ONLY = 'read_only', WRITE_LIMIT = 'write_limit', CONTENT_RULE = 'content_rule'
const TTL_HOURS = Number(process.env.DAYFLOW_APPROVAL_TTL_HOURS ?? 24)

export interface Decision { behavior: Behavior; reason: string; code?: string }

export class Policy {
  constructor(readonly writes: WritesMode = 'ask', readonly maxWritesPerRun = 2) {}
  decide(tool: Tool, args: Record<string, unknown>, writesSoFar: number, tainted = false): Decision {
    if (tool.kind === 'read') return { behavior: 'allow', reason: 'read tools do not change anything' }
    // Deny rules first. An allow rule must never be able to skip a limit.
    if (this.writes === 'deny') return { behavior: 'deny', reason: 'this run is read-only', code: READ_ONLY }
    // Lesson 88: rules about the arguments, checked before anyone is asked.
    const violation = contentViolation(tool.name, args)
    if (violation) return { behavior: 'deny', reason: violation, code: CONTENT_RULE }
    if (writesSoFar >= this.maxWritesPerRun) return { behavior: 'deny', reason: `this run already proposed ${writesSoFar} ${tool.name} calls; the limit is ${this.maxWritesPerRun}`, code: WRITE_LIMIT }
    if (this.writes === 'allow' && !tainted) return { behavior: 'allow', reason: 'writes were pre-approved for this run (--approve auto)' }
    // Lesson 88: a run that read flagged mail gets nothing auto-approved.
    if (tainted) return { behavior: 'ask', reason: `${tool.name} is a write tool, and this run read flagged mail` }
    return { behavior: 'ask', reason: `${tool.name} is a write tool` }
  }
}

export interface Request { tool: string; args: Record<string, unknown>; reason: string; runId?: string | null; preview?: Record<string, string>; flagged?: string[] }
/** approved: true, false, or null = nobody here to ask; queue it. */
export interface Verdict { approved: boolean | null; reason: string }
export interface Approver { resolve(req: Request): Promise<Verdict> }

/** The exact arguments, not a summary: a person cannot approve what they were not shown. */
export function render(req: Request): string {
  return [`Dayflow wants to run ${req.tool}  (${req.reason})`,
    ...Object.entries(req.args).map(([k, v]) => `    ${k}: ${JSON.stringify(v)}`),
    // Lesson 88: a message id tells a person nothing. Show where the write would go.
    ...Object.entries(req.preview ?? {}).map(([k, v]) => `    -> ${k}: ${v}`),
    ...(req.flagged?.length ? [`  WARNING: this run read ${req.flagged.length} message(s) that look like injection attempts: ${[...req.flagged].sort().join(', ')}. Check that this action is one YOU want.`] : []),
  ].join('\n')
}

/** Facts about a proposed write that its arguments only point at. */
function preview(name: string, args: Record<string, unknown>): Record<string, string> {
  const to = name === 'draft_reply' ? inbox.senderOf(String(args.message_id ?? '')) : null
  return to ? { to } : {}
}

export class TtyApprover implements Approver {
  private always = new Set<string>()
  constructor(private ask?: (prompt: string) => Promise<string>) {}
  async resolve(req: Request): Promise<Verdict> {
    if (this.always.has(req.tool)) return { approved: true, reason: 'approved for the rest of this run' }
    const rl = this.ask ? null : createInterface({ input: process.stdin, output: process.stderr })
    const ask = this.ask ?? ((p: string) => rl!.question(p))
    try {
      console.error('\n' + render(req))
      for (;;) {
        const a = (await ask('  approve? [y]es / [n]o / [a]lways this run: ')).trim().toLowerCase()
        if (a === 'y' || a === 'yes') return { approved: true, reason: 'approved at the terminal' }
        if (a === 'a' || a === 'always') { this.always.add(req.tool); return { approved: true, reason: 'approved at the terminal for the rest of this run' } }
        if (a === 'n' || a === 'no' || a === '') {   // Enter means no. The safe answer is the default.
          const why = (await ask('  reason (optional, the agent will see it): ')).trim()
          return { approved: false, reason: why || 'declined at the terminal' }
        }
      }
    } catch {
      return { approved: false, reason: 'no answer (input closed)' }   // a closed stdin is not a yes
    } finally { rl?.close() }
  }
}

export class QueueApprover implements Approver {
  async resolve(): Promise<Verdict> { return { approved: null, reason: 'queued; no human was present' } }
}

export interface Outcome { allowed: boolean; envelope?: string; approvalId?: string | null; code?: string }
const refusal = (code: string, message: string) => JSON.stringify({ ok: false, error: { code, message, retryable: false } })

export class Gate {
  /** Writes allowed or queued so far in this run, per tool (lesson 87 added a second write tool). */
  writes: Record<string, number> = {}
  constructor(public policy = new Policy(), public approver: Approver = new QueueApprover(),
              public store?: Store, public runId?: string | null, public ttlHours = TTL_HOURS) {
    reset()   // a new gate is a new run: forget what the last one read
  }

  /** Decide one call. Does not run the tool: the SDK runs it after a PreToolUse 'allow'. */
  async check(name: string, args: Record<string, unknown>): Promise<Outcome> {
    const tool = REGISTRY[name]
    if (!tool || tool.kind === 'read') return { allowed: true }
    const d = this.policy.decide(tool, args, this.writes[name] ?? 0, RUN.tainted)
    const count = () => { this.writes[name] = (this.writes[name] ?? 0) + 1 }
    if (d.behavior === 'deny') {
      this.audit(name, args, 'denied', 'policy', d.reason)
      return { allowed: false, envelope: refusal(d.code!, `Not executed: ${d.reason}. Do not retry.`), code: d.code }
    }
    if (d.behavior === 'allow') { count(); this.audit(name, args, 'allowed', 'policy', d.reason); return { allowed: true } }
    const pv = preview(name, args), flagged = [...RUN.flagged.keys()]
    const v = await this.approver.resolve({ tool: name, args, reason: d.reason, runId: this.runId, preview: pv, flagged })
    if (v.approved === true) { count(); this.audit(name, args, 'allowed', 'human', v.reason); return { allowed: true } }
    if (v.approved === false) {
      this.audit(name, args, 'denied', 'human', v.reason)
      return { allowed: false, code: DENIED, envelope: refusal(DENIED, `Not executed. The user declined: ${v.reason}. Do not retry; say in the brief that it was declined.`) }
    }
    count()   // a queued proposal counts toward the limit too
    let note = v.reason
    if (Object.keys(pv).length) note += '; ' + Object.entries(pv).map(([k, val]) => `${k} ${val}`).join(', ')
    if (flagged.length) note += `; run read flagged mail: ${flagged.sort().join(', ')}`
    const approvalId = this.audit(name, args, 'pending', null, note)
    return { allowed: false, approvalId, code: PENDING,
      envelope: refusal(PENDING, `Not executed. Queued for the user's approval${approvalId ? ` as ${approvalId}` : ''}. Do not retry. List it in the brief under 'Waiting for your approval'.`) }
  }

  /** check() then dispatch(). Always returns an envelope, like dispatch(). */
  async call(name: string, args: Record<string, unknown>): Promise<string> {
    const o = await this.check(name, args)
    return o.allowed ? dispatch(name, args) : (o.envelope ?? refusal('blocked', 'Not executed.'))
  }

  private audit(tool: string, args: Record<string, unknown>, status: string, decidedBy: string | null, reason: string): string | null {
    return this.store ? this.store.recordApproval({ runId: this.runId ?? null, tool, args, status, decidedBy, reason, ttlHours: this.ttlHours }) : null
  }
}

export class ApprovalError extends Error {}

/** Run a queued proposal exactly as recorded. Safe to call twice. */
export function approve(store: Store, id: string): { ok: true; data: unknown } {
  const row = store.getApproval(id)
  if (!row) throw new ApprovalError(`no approval '${id}'`)
  if (row.status === 'approved') return JSON.parse(row.result!)
  if (row.status !== 'pending') throw new ApprovalError(`approval ${id} is ${row.status}, not pending`)
  if (store.isExpired(row)) {
    store.decideApproval(id, { status: 'expired', decidedBy: 'policy', reason: 'older than its TTL' })
    throw new ApprovalError(`approval ${id} expired at ${row.expires_at}; run the brief again to get a fresh proposal`)
  }
  const envelope = dispatch(row.tool, JSON.parse(row.args))
  const result = JSON.parse(envelope)
  // The human said yes and the tool said no: leave it pending so it can be retried.
  if (!result.ok) throw new ApprovalError(`approved, but ${row.tool} failed: ${result.error.code}: ${result.error.message}`)
  store.decideApproval(id, { status: 'approved', decidedBy: 'human', reason: 'approved from the queue', result: envelope })
  return result
}

export function deny(store: Store, id: string, reason = ''): void {
  const row = store.getApproval(id)
  if (!row) throw new ApprovalError(`no approval '${id}'`)
  if (row.status !== 'pending') throw new ApprovalError(`approval ${id} is ${row.status}, not pending`)
  store.decideApproval(id, { status: 'denied', decidedBy: 'human', reason: reason || 'denied from the queue' })
}

/** ask | queue | auto | deny. Default: ask on a terminal, queue otherwise, so a scheduled run never waits on stdin. */
export function gateFor(mode: string | undefined, store?: Store, runId?: string | null): Gate {
  const m = mode ?? (process.stdin.isTTY ? 'ask' : 'queue')
  if (m === 'ask') return new Gate(new Policy('ask'), new TtyApprover(), store, runId)
  if (m === 'queue') return new Gate(new Policy('ask'), new QueueApprover(), store, runId)
  if (m === 'auto') return new Gate(new Policy('allow'), new QueueApprover(), store, runId)
  if (m === 'deny') return new Gate(new Policy('deny'), new QueueApprover(), store, runId)
  throw new Error(`unknown approval mode '${m}'`)
}
