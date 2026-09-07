// The agent loop, from scratch. Lesson 81. See ../../dayflow/loop.py for the annotated version.
import Anthropic from '@anthropic-ai/sdk'
import { TOOLS, dispatch } from './tools.js'

export class LoopError extends Error {}

export interface RunResult {
  text: string
  steps: number
  inputTokens: number
  outputTokens: number
  stopReason: string
  toolCalls: { step: number; tool: string; args: unknown }[]
}

export interface RunOptions {
  model?: string
  maxSteps?: number
  tokenBudget?: number
  maxTokens?: number
  client?: Anthropic
  verbose?: boolean
}

export async function run(system: string, user: string, opts: RunOptions = {}): Promise<RunResult> {
  const {
    model = process.env.DAYFLOW_MODEL ?? 'claude-sonnet-5',
    maxSteps = 10,
    tokenBudget = 60_000,
    maxTokens = 2_048,
    client = new Anthropic(),
    verbose = false,
  } = opts

  const messages: Anthropic.MessageParam[] = [{ role: 'user', content: user }]
  let usedIn = 0, usedOut = 0
  const toolCalls: RunResult['toolCalls'] = []

  for (let step = 1; step <= maxSteps; step++) {
    const resp = await client.messages.create({ model, max_tokens: maxTokens, system, tools: TOOLS, messages })
    usedIn += resp.usage.input_tokens
    usedOut += resp.usage.output_tokens
    if (verbose) console.error(`[step ${step}] stop=${resp.stop_reason} in=${resp.usage.input_tokens} out=${resp.usage.output_tokens}`)

    if (usedIn + usedOut > tokenBudget) throw new LoopError(`token budget exceeded at step ${step}: ${usedIn + usedOut} > ${tokenBudget}`)

    if (resp.stop_reason === 'end_turn') {
      const text = resp.content.filter(b => b.type === 'text').map(b => (b as Anthropic.TextBlock).text).join('')
      return { text, steps: step, inputTokens: usedIn, outputTokens: usedOut, stopReason: resp.stop_reason, toolCalls }
    }
    if (resp.stop_reason === 'max_tokens') throw new LoopError(`model hit max_tokens=${maxTokens} at step ${step}`)
    if (resp.stop_reason !== 'tool_use') throw new LoopError(`unexpected stop_reason ${resp.stop_reason} at step ${step}`)

    messages.push({ role: 'assistant', content: resp.content })

    const results: Anthropic.ToolResultBlockParam[] = []
    for (const block of resp.content) {
      if (block.type !== 'tool_use') continue
      const output = dispatch(block.name, block.input as Record<string, unknown>)
      toolCalls.push({ step, tool: block.name, args: block.input })
      if (verbose) console.error(`    -> ${block.name}(${JSON.stringify(block.input)}) ${output.length} chars`)
      results.push({ type: 'tool_result', tool_use_id: block.id, content: output })
    }
    messages.push({ role: 'user', content: results })
  }
  throw new LoopError(`no end_turn after ${maxSteps} steps; the model is looping`)
}
