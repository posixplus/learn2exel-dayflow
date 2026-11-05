// The agent loop. Lessons 81-83. See ../../dayflow/loop.py for the annotated version.
import Anthropic from '@anthropic-ai/sdk'
import { definitions, dispatch } from './tools.js'
import type { Store } from './store.js'

export class LoopError extends Error {}

export interface RunResult {
  text: string
  steps: number
  inputTokens: number
  outputTokens: number
  stopReason: string
  toolCalls: { step: number; tool: string; args: unknown }[]
  runId?: string
}

export interface RunOptions {
  model?: string
  maxSteps?: number
  tokenBudget?: number
  maxTokens?: number
  client?: Anthropic
  verbose?: boolean
  tools?: Anthropic.Tool[]
  store?: Store
  kind?: string
  crashAfterStep?: number
}

/** SDK blocks -> plain JSON the API accepts back verbatim. */
function plain(blocks: Anthropic.ContentBlock[]): Anthropic.ContentBlockParam[] {
  return blocks.map(b => {
    if (b.type === 'text') return { type: 'text', text: b.text }
    if (b.type === 'tool_use') return { type: 'tool_use', id: b.id, name: b.name, input: b.input }
    return b as unknown as Anthropic.ContentBlockParam
  })
}

export async function run(system: string, user: string, opts: RunOptions = {}): Promise<RunResult> {
  const model = opts.model ?? process.env.DAYFLOW_MODEL ?? 'claude-sonnet-5-5'
  const runId = opts.store?.createRun({ kind: opts.kind ?? 'brief', system, userMsg: user, model })
  const messages: Anthropic.MessageParam[] = [{ role: 'user', content: user }]
  return drive(messages, { ...opts, model, system, startStep: 1, usedIn: 0, usedOut: 0, runId })
}

export async function resume(runId: string, opts: RunOptions & { store: Store }): Promise<RunResult> {
  const run = opts.store.getRun(runId)
  if (!run) throw new LoopError(`no run ${runId}`)
  // Lesson 84: SDK-driven runs are resumed through the SDK session, not our audit rows.
  if (run.kind.endsWith('-sdk')) throw new LoopError(`run ${runId} was driven by the Agent SDK; resume it with --engine sdk (lesson 91), not the raw loop`)
  if (run.status === 'done') {
    return { text: run.final_text ?? '', steps: opts.store.steps(runId).length, inputTokens: run.input_tokens, outputTokens: run.output_tokens, stopReason: 'end_turn', toolCalls: [], runId }
  }
  const done = opts.store.steps(runId)
  const messages = opts.store.rebuildMessages(runId)
  if (opts.verbose) console.error(`[resume ${runId}] replaying ${done.length} committed step(s), ${run.input_tokens} in / ${run.output_tokens} out already spent`)
  opts.store.markRunning(runId)
  return drive(messages, { ...opts, crashAfterStep: undefined, model: run.model, system: run.system, startStep: done.length + 1, usedIn: run.input_tokens, usedOut: run.output_tokens, runId })
}

async function drive(
  messages: Anthropic.MessageParam[],
  o: RunOptions & { model: string; system: string; startStep: number; usedIn: number; usedOut: number; runId?: string },
): Promise<RunResult> {
  const { maxSteps = 10, tokenBudget = 60_000, maxTokens = 2_048, client = new Anthropic(), verbose = false, tools = definitions(), store, runId } = o
  let usedIn = o.usedIn, usedOut = o.usedOut
  const toolCalls: RunResult['toolCalls'] = []
  const fail = (msg: string) => { if (store && runId) store.failRun(runId, msg); return new LoopError(msg) }

  for (let step = o.startStep; step <= maxSteps; step++) {
    const resp = await client.messages.create({ model: o.model, max_tokens: maxTokens, system: o.system, tools, messages })
    usedIn += resp.usage.input_tokens
    usedOut += resp.usage.output_tokens
    if (verbose) console.error(`[step ${step}] stop=${resp.stop_reason} in=${resp.usage.input_tokens} out=${resp.usage.output_tokens}`)
    if (usedIn + usedOut > tokenBudget) throw fail(`token budget exceeded at step ${step}: ${usedIn + usedOut} > ${tokenBudget}`)

    const content = plain(resp.content)

    if (resp.stop_reason === 'end_turn') {
      const text = content.filter(b => b.type === 'text').map(b => (b as Anthropic.TextBlockParam).text).join('')
      if (store && runId) {
        store.appendStep(runId, { n: step, stopReason: 'end_turn', assistant: content, toolResults: null, inputTokens: resp.usage.input_tokens, outputTokens: resp.usage.output_tokens })
        store.finishRun(runId, text)
      }
      return { text, steps: step, inputTokens: usedIn, outputTokens: usedOut, stopReason: resp.stop_reason, toolCalls, runId }
    }
    if (resp.stop_reason === 'max_tokens') throw fail(`model hit max_tokens=${maxTokens} at step ${step}`)
    if (resp.stop_reason !== 'tool_use') throw fail(`unexpected stop_reason ${resp.stop_reason} at step ${step}`)

    messages.push({ role: 'assistant', content })
    const results: Anthropic.ToolResultBlockParam[] = []
    for (const block of content) {
      if (block.type !== 'tool_use') continue
      const output = dispatch(block.name, block.input as Record<string, unknown>)
      toolCalls.push({ step, tool: block.name, args: block.input })
      if (verbose) console.error(`    -> ${block.name}(${JSON.stringify(block.input)}) ${output.length} chars`)
      results.push({ type: 'tool_result', tool_use_id: block.id, content: output })
    }
    messages.push({ role: 'user', content: results })

    // Checkpoint before the next call can spend anything.
    if (store && runId) store.appendStep(runId, { n: step, stopReason: 'tool_use', assistant: content, toolResults: results, inputTokens: resp.usage.input_tokens, outputTokens: resp.usage.output_tokens })
    if (o.crashAfterStep !== undefined && step >= o.crashAfterStep) { console.error(`simulated crash after step ${step}`); process.exit(0) }
  }
  throw fail(`no end_turn after ${maxSteps} steps; the model is looping`)
}
