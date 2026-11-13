import { config } from 'dotenv'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
// .env lives at the repo root, shared with the Python side.
config({ path: join(dirname(fileURLToPath(import.meta.url)), '..', '..', '.env') })
import { LoopError, resume, run } from './loop.js'
import { today } from './clock.js'
import { MORNING_BRIEF, briefRequest } from './prompts.js'
import { Store } from './store.js'
import { useStore } from './tools.js'
import { ApprovalError, approve, deny, gateFor } from './permissions.js'
import { taskSeed } from './fixtures.js'

const [cmd = 'brief', ...rest] = process.argv.slice(2)
const verbose = rest.includes('-v') || rest.includes('--verbose')
const crashIdx = rest.indexOf('--crash-after')
const crashAfterStep = crashIdx >= 0 ? Number(rest[crashIdx + 1]) : undefined

const store = new Store()
useStore(store, taskSeed())

if ((process.env.MOCK ?? '1') !== '1') {
  console.error('Real adapters arrive in lesson 87. Set MOCK=1 for now.')
  process.exit(2)
}

// ── Lesson 86: the approval queue ──
const approveIdx = rest.indexOf('--approve')
const approveMode = approveIdx >= 0 ? rest[approveIdx + 1] : undefined
const pendingNote = (runId?: string | null) => {
  const p = runId ? store.listApprovals('pending', runId) : []
  if (p.length) console.error(`\n${p.length} write(s) waiting for approval: \`npm run approvals\`, then \`npm run approve -- <id>\` or \`npm run deny -- <id>\`  [${p.map(r => r.id).join(' ')}]`)
}

if (cmd === 'approvals') {
  const rows = store.listApprovals(rest.includes('--all') ? null : 'pending')
  if (!rows.length) console.log(rest.includes('--all') ? 'no approvals recorded' : 'no pending approvals')
  for (const r of rows) {
    const stale = r.status === 'pending' && store.isExpired(r) ? '  EXPIRED' : ''
    console.log(`${r.id}  ${r.status}${r.decided_by ? ` by ${r.decided_by}` : ''}${stale}  ${r.tool}  run ${r.run_id}  ${r.created_at}`)
    for (const [k, v] of Object.entries(JSON.parse(r.args))) console.log(`    ${k}: ${JSON.stringify(v)}`)
    if (rest.includes('--all') && r.reason) console.log(`    reason: ${r.reason}`)
  }
  process.exit(0)
}

if (cmd === 'approve' || cmd === 'deny') {
  const id = rest.find(a => a.startsWith('ap_'))
  if (!id) { console.error(`usage: ${cmd} <approval id>`); process.exit(2) }
  try {
    if (cmd === 'approve') console.log(`approved ${id}: ${JSON.stringify(approve(store, id).data)}`)
    else { const m = rest.indexOf('-m'); deny(store, id, m >= 0 ? rest[m + 1] : ''); console.log(`denied ${id}`) }
  } catch (e) {
    if (e instanceof ApprovalError) { console.error(e.message); process.exit(1) }
    throw e
  }
  process.exit(0)
}

if (cmd === 'resume') {
  const id = rest.find(a => !a.startsWith('-')) ?? store.latestRun('running')?.id ?? store.latestRun('failed')?.id
  if (!id) { console.error('nothing to resume'); process.exit(1) }
  try {
    const r = await resume(id, { store, verbose, gate: gateFor(undefined, store, id) })
    console.log(r.text)
    console.error(`\n[resumed ${id} · finished at step ${r.steps} · ${r.inputTokens} in / ${r.outputTokens} out total]`)
  } catch (e) {
    if (e instanceof LoopError) { console.error(`loop stopped: ${e.message}`); process.exit(1) }
    throw e
  }
}

const engineIdx = rest.indexOf('--engine')
const engine = engineIdx >= 0 ? rest[engineIdx + 1] : 'raw'

if (cmd === 'brief' && engine === 'orch') {
  const { runBrief, costTable, totals } = await import('./orchestrate.js')
  const t0 = performance.now()
  const r = await runBrief({ store, verbose })
  console.log(r.text)
  console.error(`\n${costTable(r)}`)
  console.error(`[orch · ${r.legs.length} legs · $${totals(r).costUsd.toFixed(4)} · ${((performance.now() - t0) / 1000).toFixed(1)}s wall · run ${r.runId}]`)
  process.exit(0)
}

if (cmd === 'brief' && engine === 'sdk') {
  const { runBrief } = await import('./sdkAgent.js')
  const r = await runBrief({ store, verbose, kinds: rest.includes('--read-only') ? ['read'] : ['read', 'write'], gate: gateFor(approveMode, store) })
  console.log(r.text)
  pendingNote(r.runId)
  const cost = r.costUsd == null ? '' : ` · $${r.costUsd.toFixed(4)}`
  console.error(`\n[sdk · ${r.numTurns} turns · ${r.toolCalls.length} tool calls · ${r.inputTokens} in (+${r.cacheReadTokens} cached) / ${r.outputTokens} out${cost} · run ${r.runId} · session ${r.sessionId}]`)
  process.exit(0)
}

if (cmd === 'brief') {
  try {
    const r = await run(MORNING_BRIEF, briefRequest(today()), { verbose, store, crashAfterStep, gate: gateFor(approveMode, store) })
    console.log(r.text)
    pendingNote(r.runId)
    console.error(`\n[${r.steps} steps · ${r.toolCalls.length} tool calls · ${r.inputTokens} in / ${r.outputTokens} out · run ${r.runId}]`)
  } catch (e) {
    if (e instanceof LoopError) { console.error(`loop stopped: ${e.message}`); process.exit(1) }
    throw e
  }
}
