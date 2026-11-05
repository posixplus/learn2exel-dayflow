// Lesson 85: fan out to three one-tool specialists, fan in to a tool-less synthesiser.
// Mirror of ../../dayflow/orchestrate.py; the reasoning lives in that docstring.
import { query, type HookCallback, type PostToolUseHookInput, type SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { today as clockToday } from './clock.js'
import { CALENDAR_READER, INBOX_TRIAGE, SYNTHESISER, TASKS_READER, synthRequest } from './prompts.js'
import { SERVER, buildServer, collect, short, toolNames } from './sdkAgent.js'
import type { Store } from './store.js'

export const SPECIALIST_MODEL = process.env.DAYFLOW_SPECIALIST_MODEL ?? 'claude-haiku-5-5'

export interface Specialist { name: string; system: string; tool: string; request: string; maxTurns: number }
export const SPECIALISTS: Specialist[] = [
  { name: 'inbox', system: INBOX_TRIAGE, tool: 'read_inbox', request: 'Today is {today}. Triage the inbox.', maxTurns: 10 },
  { name: 'calendar', system: CALENDAR_READER, tool: 'read_calendar', request: 'Today is {today}. Read the calendar.', maxTurns: 3 },
  { name: 'tasks', system: TASKS_READER, tool: 'read_tasks', request: 'Today is {today}. Read the tasks.', maxTurns: 3 },
]

export interface Leg { name: string; model: string; turns: number; inputTokens: number; cacheReadTokens: number; outputTokens: number; costUsd: number; seconds: number; text: string }
export interface OrchResult { text: string; legs: Leg[]; runId: string | null; sessionId: string }

const sum = (legs: Leg[], k: keyof Leg) => legs.reduce((a, l) => a + (l[k] as number), 0)
export const totals = (r: OrchResult) => ({
  costUsd: sum(r.legs, 'costUsd'), inputTokens: sum(r.legs, 'inputTokens'), cacheReadTokens: sum(r.legs, 'cacheReadTokens'), outputTokens: sum(r.legs, 'outputTokens'),
  // Specialists overlap: wall time is max(specialists) + synthesiser.
  seconds: Math.max(0, ...r.legs.filter(l => l.name !== 'synthesiser').map(l => l.seconds)) + sum(r.legs.filter(l => l.name === 'synthesiser'), 'seconds'),
})

const baseOptions = (system: string, model: string, maxTurns: number) => ({
  systemPrompt: system, strictMcpConfig: true, settingSources: [] as never[], tools: [] as string[],
  permissionMode: 'default' as const, maxTurns, maxBudgetUsd: 0.10, model,
})

async function runSpecialist(spec: Specialist, ctx: { today: string; store?: Store; runId: string | null; step: { n: number }; verbose?: boolean; model: string }): Promise<Leg> {
  const recordStep: HookCallback = async input => {
    const h = input as PostToolUseHookInput
    ctx.step.n += 1
    if (ctx.store && ctx.runId) ctx.store.appendStep(ctx.runId, {
      n: ctx.step.n, stopReason: 'tool_use',
      assistant: [{ type: 'tool_use', id: h.tool_use_id, name: short(h.tool_name), input: h.tool_input as Record<string, unknown> }],
      toolResults: [{ type: 'tool_result', tool_use_id: h.tool_use_id, content: JSON.stringify(h.tool_response) }],
      inputTokens: 0, outputTokens: 0,
    })
    return {}
  }
  const t0 = performance.now()
  try {
    const q = query({
      prompt: spec.request.replace('{today}', ctx.today),
      options: {
        ...baseOptions(spec.system, ctx.model, spec.maxTurns),
        mcpServers: { [SERVER]: buildServer(['read'], [spec.tool]) },          // exactly one tool
        allowedTools: toolNames(['read'], [spec.tool]),
        hooks: { PostToolUse: [{ matcher: `mcp__${SERVER}__.*`, hooks: [recordStep] }] },
      },
    })
    const { parts, result } = await collect(q as AsyncIterable<SDKMessage>, { verbose: ctx.verbose, label: spec.name })
    const u = result.usage
    return { name: spec.name, model: ctx.model, turns: result.num_turns, inputTokens: u.input_tokens ?? 0, cacheReadTokens: u.cache_read_input_tokens ?? 0,
      outputTokens: u.output_tokens ?? 0, costUsd: result.total_cost_usd ?? 0, seconds: (performance.now() - t0) / 1000,
      text: parts.filter(p => p.trim()).join('\n') || ('result' in result ? result.result : '') }
  } catch (e) {
    // Out of turns or budget: a missing source, in the shape the synthesiser already understands.
    return { name: spec.name, model: ctx.model, turns: 0, inputTokens: 0, cacheReadTokens: 0, outputTokens: 0, costUsd: 0,
      seconds: (performance.now() - t0) / 1000, text: `${spec.name.toUpperCase()}_UNAVAILABLE: ${String(e)}` }
  }
}

export async function runBrief(opts: { store?: Store; verbose?: boolean; model?: string; specialistModel?: string } = {}): Promise<OrchResult> {
  const model = opts.model ?? process.env.DAYFLOW_MODEL ?? 'claude-sonnet-5-5'
  const today = clockToday()
  const store = opts.store
  const runId = store ? store.createRun({ kind: 'brief-orch', system: SYNTHESISER, userMsg: `orchestrated brief for ${today}`, model }) : null
  const step = { n: 0 }
  try {
    const legs = await Promise.all(SPECIALISTS.map(s => runSpecialist(s, { today, store, runId, step, verbose: opts.verbose, model: opts.specialistModel ?? SPECIALIST_MODEL })))
    const reports = Object.fromEntries(legs.map(l => [l.name, l.text]))
    const t0 = performance.now()
    const q = query({ prompt: synthRequest(today, reports), options: baseOptions(SYNTHESISER, model, 1) })
    const { parts, result } = await collect(q as AsyncIterable<SDKMessage>, { verbose: opts.verbose, label: 'synth' })
    const text = parts.filter(p => p.trim()).join('\n') || ('result' in result ? result.result : '')
    const u = result.usage
    legs.push({ name: 'synthesiser', model, turns: result.num_turns, inputTokens: u.input_tokens ?? 0, cacheReadTokens: u.cache_read_input_tokens ?? 0,
      outputTokens: u.output_tokens ?? 0, costUsd: result.total_cost_usd ?? 0, seconds: (performance.now() - t0) / 1000, text })
    const out: OrchResult = { text, legs, runId, sessionId: result.session_id }
    if (store && runId) {
      const t = totals(out)
      store.finishRun(runId, text)
      store.recordSdkTotals(runId, out.sessionId, t.inputTokens + t.cacheReadTokens, t.outputTokens)
    }
    return out
  } catch (e) {
    if (store && runId) store.failRun(runId, String(e))
    throw e
  }
}

export function costTable(r: OrchResult): string {
  const pad = (s: string | number, n: number, right = false) => right ? String(s).padStart(n) : String(s).padEnd(n)
  const rows = [pad('leg', 12) + pad('model', 20) + pad('turns', 5, true) + pad('in', 7, true) + pad('cached', 7, true) + pad('out', 6, true) + pad('$', 9, true) + pad('s', 6, true)]
  for (const l of r.legs) rows.push(pad(l.name, 12) + pad(l.model, 20) + pad(l.turns, 5, true) + pad(l.inputTokens, 7, true) + pad(l.cacheReadTokens, 7, true) + pad(l.outputTokens, 6, true) + pad(l.costUsd.toFixed(4), 9, true) + pad(l.seconds.toFixed(1), 6, true))
  const t = totals(r)
  rows.push(pad('total', 12) + pad('', 20) + pad('', 5) + pad(t.inputTokens, 7, true) + pad(t.cacheReadTokens, 7, true) + pad(t.outputTokens, 6, true) + pad(t.costUsd.toFixed(4), 9, true) + pad(t.seconds.toFixed(1), 6, true))
  return rows.join('\n')
}
