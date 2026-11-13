// Lesson 84: Dayflow on the Claude Agent SDK. Mirror of ../../dayflow/sdk_agent.py.
//
// The registry (tools.ts), the envelope and the store are untouched. The SDK
// takes over the loop, permissions, session persistence and compaction. One
// TypeScript-specific wrinkle: the SDK's tool() wants a Zod shape, not JSON
// Schema, so we translate the four property shapes the registry uses.
import {
  createSdkMcpServer,
  query,
  tool,
  type HookCallback,
  type PostToolUseHookInput,
  type PreToolUseHookInput,
  type SDKMessage,
  type SDKResultMessage,
} from '@anthropic-ai/claude-agent-sdk'
import { z, type ZodTypeAny } from 'zod'
import { today } from './clock.js'
import { MORNING_BRIEF, briefRequest } from './prompts.js'
import { Gate, Policy } from './permissions.js'
import type { Store } from './store.js'
import { REGISTRY, dispatch, type Kind, type Tool } from './tools.js'

export const SERVER = 'dayflow'

// ── JSON Schema property → Zod. Covers exactly what the registry declares. ──
type Prop = { type: string | string[]; enum?: string[]; description?: string }
function zodFor(p: Prop): ZodTypeAny {
  const types = Array.isArray(p.type) ? p.type : [p.type]
  const nullable = types.includes('null')
  const base = types.find(t => t !== 'null') ?? 'string'
  let s: ZodTypeAny =
    p.enum ? z.enum(p.enum as [string, ...string[]])
    : base === 'integer' ? z.number().int()
    : base === 'number' ? z.number()
    : base === 'boolean' ? z.boolean()
    : z.string()
  if (p.description) s = s.describe(p.description)
  return nullable ? s.nullable() : s
}

function asMcpTool(t: Tool) {
  const shape = Object.fromEntries(Object.entries(t.properties).map(([k, v]) => [k, zodFor(v as Prop)]))
  return tool(t.name, t.description, shape, async args => {
    // Same dispatch, same envelope. No isError: the model branches on `ok`.
    const envelope = dispatch(t.name, args as Record<string, unknown>)
    return { content: [{ type: 'text' as const, text: envelope }] }
  })
}

/** Registry subset: by kind (82) and, since 85, by explicit name so a specialist gets exactly one tool. */
function select(kinds: Kind[], names?: string[]): Tool[] {
  return Object.values(REGISTRY).filter(t => kinds.includes(t.kind) && (!names || names.includes(t.name)))
}

export function buildServer(kinds: Kind[] = ['read', 'write'], names?: string[]) {
  return createSdkMcpServer({ name: SERVER, version: '0.88.0', tools: select(kinds, names).map(asMcpTool) })
}

export function toolNames(kinds: Kind[] = ['read', 'write'], names?: string[]): string[] {
  return select(kinds, names).map(t => `mcp__${SERVER}__${t.name}`)
}

/** Lesson 86: what the harness may run without asking. Reads only, whatever `kinds` says.
 *  A write needs the PreToolUse hook to say allow; if the hook is missing or throws, it fails closed. */
export function preapprovedTools(kinds: Kind[] = ['read', 'write']): string[] {
  return toolNames(kinds.filter(k => k === 'read'))
}

/** Drain one query() stream: assistant text parts + the final result message. Shared by 84 and 85. */
export async function collect(q: AsyncIterable<SDKMessage>, opts: { verbose?: boolean; label?: string } = {}) {
  const parts: string[] = []
  let result: SDKResultMessage | undefined
  const tag = opts.label ? `[${opts.label}] ` : ''
  for await (const msg of q) {
    if (msg.type === 'assistant') {
      for (const b of msg.message.content) {
        if (b.type === 'text') parts.push(b.text)
        else if (b.type === 'tool_use' && opts.verbose) console.error(`  ${tag}→ ${short(b.name)}`)
      }
    } else if (msg.type === 'result') result = msg
  }
  if (!result) throw new Error('SDK stream ended without a result message')
  return { parts, result }
}
export const short = (n: string) => n.replace(`mcp__${SERVER}__`, '')

export interface SdkResult {
  text: string; sessionId: string; numTurns: number
  toolCalls: Array<{ name: string; input: unknown }>
  inputTokens: number; outputTokens: number; cacheReadTokens: number
  costUsd: number | null; runId: string | null
}

export async function runBrief(opts: {
  store?: Store; kinds?: Kind[]; verbose?: boolean; maxTurns?: number; maxBudgetUsd?: number; model?: string; resume?: string
  gate?: Gate
} = {}): Promise<SdkResult> {
  const kinds = opts.kinds ?? ['read', 'write']
  const model = opts.model ?? process.env.DAYFLOW_MODEL ?? 'claude-sonnet-5-5'
  const store = opts.store
  const userMsg = briefRequest(today())
  const runId = store ? store.createRun({ kind: 'brief-sdk', system: MORNING_BRIEF, userMsg, model }) : null
  const toolCalls: SdkResult['toolCalls'] = []
  let n = 0

  // Lesson 86: the same Gate the raw loop uses. Default is the safe one (queue).
  const gate = opts.gate ?? new Gate(new Policy(), undefined, store)
  gate.runId ??= runId; gate.store ??= store

  // PreToolUse: decide before the call. On deny, the reason is what the model
  // receives in place of a result, so the reason is our envelope.
  const askGate: HookCallback = async input => {
    const h = input as PreToolUseHookInput
    const name = short(h.tool_name)
    const o = await gate.check(name, h.tool_input as Record<string, unknown>)
    if (opts.verbose && !o.allowed) console.error(`  ✋ ${name}: ${o.code}${o.approvalId ? ` (${o.approvalId})` : ''}`)
    return { hookSpecificOutput: {
      hookEventName: 'PreToolUse' as const,
      permissionDecision: o.allowed ? 'allow' as const : 'deny' as const,
      permissionDecisionReason: o.allowed ? 'allowed by the Dayflow gate' : o.envelope,
    } }
  }

  // PostToolUse: one audit row per tool call, in lesson 83's steps table.
  const recordStep: HookCallback = async input => {
    const h = input as PostToolUseHookInput
    const name = short(h.tool_name)
    n += 1
    toolCalls.push({ name, input: h.tool_input })
    if (opts.verbose) console.error(`  ↳ ${name}(${JSON.stringify(h.tool_input)})`)
    if (store && runId) store.appendStep(runId, {
      n, stopReason: 'tool_use',
      assistant: [{ type: 'tool_use', id: h.tool_use_id, name, input: h.tool_input as Record<string, unknown> }],
      toolResults: [{ type: 'tool_result', tool_use_id: h.tool_use_id, content: JSON.stringify(h.tool_response) }],
      inputTokens: 0, outputTokens: 0,
    })
    return {}
  }

  const q = query({
    prompt: userMsg,
    options: {
      systemPrompt: MORNING_BRIEF,
      mcpServers: { [SERVER]: buildServer(kinds) },
      strictMcpConfig: true,
      settingSources: [],
      tools: [],                       // the registry is the whole surface; no Read/Bash/Edit
      allowedTools: preapprovedTools(kinds),   // reads only; a write needs the hook's allow (two locks)
      permissionMode: 'default',
      maxTurns: opts.maxTurns ?? 8,
      maxBudgetUsd: opts.maxBudgetUsd ?? 0.25,
      model,
      resume: opts.resume,
      hooks: {
        PreToolUse: [{ matcher: `mcp__${SERVER}__.*`, hooks: [askGate] }],
        PostToolUse: [{ matcher: `mcp__${SERVER}__.*`, hooks: [recordStep] }],
      },
    },
  })

  let parts: string[], result: SDKResultMessage
  try {
    ({ parts, result } = await collect(q as AsyncIterable<SDKMessage>, { verbose: opts.verbose }))
  } catch (e) {
    if (store && runId) store.failRun(runId, String(e))
    throw e
  }
  if (result.is_error || result.subtype !== 'success') {
    const msg = `${result.subtype}: ${('errors' in result ? result.errors : []).join('; ')}`
    if (store && runId) store.failRun(runId, msg)
    throw new Error(msg)
  }

  const text = parts.filter(p => p.trim()).join('\n') || result.result
  const u = result.usage
  const out: SdkResult = {
    text, sessionId: result.session_id, numTurns: result.num_turns, toolCalls,
    inputTokens: u.input_tokens ?? 0, outputTokens: u.output_tokens ?? 0,
    cacheReadTokens: u.cache_read_input_tokens ?? 0,
    costUsd: result.total_cost_usd ?? null, runId,
  }
  if (store && runId) {
    store.finishRun(runId, text)
    store.recordSdkTotals(runId, out.sessionId, out.inputTokens + out.cacheReadTokens, out.outputTokens)
  }
  return out
}
