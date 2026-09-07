import 'dotenv/config'
import { LoopError, run } from './loop.js'
import { MORNING_BRIEF } from './prompts.js'

const [cmd = 'brief', ...rest] = process.argv.slice(2)
const verbose = rest.includes('-v') || rest.includes('--verbose')

if ((process.env.MOCK ?? '1') !== '1') {
  console.error('Real adapters arrive in lesson 87. Set MOCK=1 for now.')
  process.exit(2)
}

if (cmd === 'brief') {
  try {
    const r = await run(MORNING_BRIEF, 'Give me my morning brief for today.', { verbose })
    console.log(r.text)
    console.error(`\n[${r.steps} steps · ${r.toolCalls.length} tool calls · ${r.inputTokens} in / ${r.outputTokens} out]`)
  } catch (e) {
    if (e instanceof LoopError) { console.error(`loop stopped: ${e.message}`); process.exit(1) }
    throw e
  }
}
