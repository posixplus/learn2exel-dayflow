import type Anthropic from '@anthropic-ai/sdk'
import { calendar, inbox, tasks } from './fixtures.js'

export const TOOLS: Anthropic.Tool[] = [
  {
    name: 'read_inbox',
    description: 'List recent inbox messages, newest first. Returns id, from, subject, date, body.',
    input_schema: { type: 'object', properties: { limit: { type: 'integer', minimum: 1, maximum: 50, default: 20 } } },
  },
  {
    name: 'read_calendar',
    description: 'List calendar events for the next N days, starting today. Returns id, title, start, end, attendees.',
    input_schema: { type: 'object', properties: { days: { type: 'integer', minimum: 1, maximum: 14, default: 1 } } },
  },
  {
    name: 'read_tasks',
    description: 'List open tasks with due dates.',
    input_schema: { type: 'object', properties: { include_done: { type: 'boolean', default: false } } },
  },
]

type Handler = (args: Record<string, unknown>) => unknown
const HANDLERS: Record<string, Handler> = {
  read_inbox: a => inbox.listMessages(a.limit as number | undefined),
  read_calendar: a => calendar.listEvents(a.days as number | undefined),
  read_tasks: a => tasks.listTasks(a.include_done as boolean | undefined),
}

/** Run a tool and return its result as JSON. Data, not prose. */
export function dispatch(name: string, args: Record<string, unknown>): string {
  const h = HANDLERS[name]
  if (!h) return JSON.stringify({ error: `unknown tool: ${name}` })
  try {
    return JSON.stringify(h(args ?? {}))
  } catch (e) {
    // A failing tool is data, not a crash. The model decides what to do with it.
    return JSON.stringify({ error: `${name} failed: ${(e as Error).message}` })
  }
}
