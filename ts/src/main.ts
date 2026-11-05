import { config } from 'dotenv'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
// .env lives at the repo root, shared with the Python side.
config({ path: join(dirname(fileURLToPath(import.meta.url)), '..', '..', '.env') })
import { LoopError, resume, run } from './loop.js'
import { MORNING_BRIEF } from './prompts.js'
import { Store } from './store.js'
import { useStore } from './tools.js'
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

if (cmd === 'resume') {
  const id = rest.find(a => !a.startsWith('-')) ?? store.latestRun('running')?.id ?? store.latestRun('failed')?.id
  if (!id) { console.error('nothing to resume'); process.exit(1) }
  try {
    const r = await resume(id, { store, verbose })
    console.log(r.text)
    console.error(`\n[resumed ${id} · finished at step ${r.steps} · ${r.inputTokens} in / ${r.outputTokens} out total]`)
  } catch (e) {
    if (e instanceof LoopError) { console.error(`loop stopped: ${e.message}`); process.exit(1) }
    throw e
  }
}

if (cmd === 'brief') {
  try {
    const r = await run(MORNING_BRIEF, 'Give me my morning brief for today.', { verbose, store, crashAfterStep })
    console.log(r.text)
    console.error(`\n[${r.steps} steps · ${r.toolCalls.length} tool calls · ${r.inputTokens} in / ${r.outputTokens} out · run ${r.runId}]`)
  } catch (e) {
    if (e instanceof LoopError) { console.error(`loop stopped: ${e.message}`); process.exit(1) }
    throw e
  }
}
