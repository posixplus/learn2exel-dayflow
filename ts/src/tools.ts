// Lesson 82: tools are an API surface. See ../../dayflow/tools.py for the annotated version.
import type Anthropic from '@anthropic-ai/sdk'
import { calendar, inbox, tasks as fixtureTasks } from './fixtures.js'

// Tasks: in-memory fixtures by default; useStore() points them at SQLite (lesson 83).
let tasks: { listTasks: (d?: boolean) => unknown; createTask: (t: string, d: string | null, k: string) => unknown } = fixtureTasks
export function useStore(store: { seedTasks: (t: never[]) => void; listTasks: (d?: boolean) => unknown; createTask: (t: string, d: string | null, k: string) => unknown }, seed: unknown[]) {
  store.seedTasks(seed as never[]); tasks = store
}

export type Kind = 'read' | 'write'

export class ToolError extends Error {
  constructor(public code: string, message: string, public retryable = false) { super(message) }
}

export interface Tool {
  name: string
  description: string
  kind: Kind
  properties: Record<string, unknown>
  handler: (args: Record<string, unknown>) => unknown
}

/** Strict mode: every property required, no extras, no numeric constraints. */
function schema(t: Tool): Anthropic.Tool {
  return {
    name: t.name,
    description: t.description,
    strict: true,
    input_schema: {
      type: 'object',
      properties: t.properties,
      required: Object.keys(t.properties),
      additionalProperties: false,
    },
  } as Anthropic.Tool
}

export const REGISTRY: Record<string, Tool> = {
  read_inbox: {
    name: 'read_inbox', kind: 'read',
    description: 'List inbox messages, newest first, one page at a time. Returns {messages, total, next_offset}; next_offset is null on the last page. Page through a backlog with limit 25 and offset 0, 25, 50...',
    properties: {
      limit: { type: 'integer', description: 'Messages per page, 1 to 50. 20 is enough for a daily brief.' },
      // Lesson 85: big results get persisted-with-preview by the SDK harness; pages keep every result small.
      offset: { type: 'integer', description: '0-based index of the first message to return.' },
    },
    handler: a => inbox.listMessages(Math.max(1, Math.min(50, a.limit as number)), Math.max(0, a.offset as number)),
  },
  read_calendar: {
    name: 'read_calendar', kind: 'read',
    description: "List calendar events. Use 'today' for the day's schedule, 'week' for deadlines and planning.",
    properties: { range: { type: 'string', enum: ['today', 'week'] } },
    handler: a => calendar.listEvents(a.range === 'today' ? 1 : 7),
  },
  read_tasks: {
    name: 'read_tasks', kind: 'read',
    description: 'List tasks with due dates. Completed tasks are excluded unless include_done is true.',
    properties: { include_done: { type: 'boolean' } },
    handler: a => tasks.listTasks(a.include_done as boolean),
  },
  create_task: {
    name: 'create_task', kind: 'write',
    description: 'Create a task for a concrete action the user has not tracked. Reuse idempotency_key on retry.',
    properties: {
      title: { type: 'string', description: 'Imperative, under 80 characters' },
      due: { type: ['string', 'null'], description: 'YYYY-MM-DD, or null' },
      idempotency_key: { type: 'string', description: 'Stable key for this intent' },
    },
    handler: a => tasks.createTask(a.title as string, a.due as string | null, a.idempotency_key as string),
  },
}

export function definitions(kinds: Kind[] = ['read', 'write']): Anthropic.Tool[] {
  return Object.values(REGISTRY).filter(t => kinds.includes(t.kind)).map(schema)
}

const ok = (data: unknown) => JSON.stringify({ ok: true, data })
const err = (code: string, message: string, retryable: boolean) =>
  JSON.stringify({ ok: false, error: { code, message, retryable } })

/** Run a tool and return a JSON envelope. Never throws. */
export function dispatch(name: string, args: Record<string, unknown>): string {
  const t = REGISTRY[name]
  if (!t) return err('unknown_tool', `no tool named ${name}`, false)
  try {
    return ok(t.handler(args ?? {}))
  } catch (e) {
    if (e instanceof ToolError) return err(e.code, e.message, e.retryable)
    return err('tool_failed', (e as Error).message, true)
  }
}
