// Fixture adapters. Same interface the real Google adapters will implement in lesson 87.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { ToolError } from './tools.js'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'fixtures')
const load = <T>(name: string): T[] => JSON.parse(readFileSync(join(FIXTURES, `${name}.json`), 'utf8'))

export type Message = { id: string; from: string; subject: string; date: string; body: string }
export type Event = { id: string; title: string; start: string; end: string; attendees: string[] }
export type Task = { id: string; title: string; due: string; done: boolean }

/** DAYFLOW_INBOX=large (lesson 85) or redteam (lesson 88) swaps the inbox fixture. */
const inboxName = () => ({ large: 'inbox_large', redteam: 'inbox_redteam' } as Record<string, string>)[process.env.DAYFLOW_INBOX ?? ''] ?? 'inbox'

export const inbox = {
  /** Lesson 88: who a reply to this message would go to. Shown at approval time. */
  senderOf: (messageId: string): string | null => load<Message>(inboxName()).find(m => m.id === messageId)?.from ?? null,
  listMessages: (limit = 20, offset = 0) => {
    const all = load<Message>(inboxName()).sort((a, b) => b.date.localeCompare(a.date))
    const next = offset + limit
    return { messages: all.slice(offset, next), total: all.length, next_offset: next < all.length ? next : null }
  },
  /** Lesson 87: fixture twin of the Gmail draft. Returns null for an unknown message id. */
  createDraft: (messageId: string, idempotencyKey: string) => {
    const known = drafts.get(idempotencyKey)
    if (known) return { ...known, created: false }
    const original = load<Message>(inboxName()).find(m => m.id === messageId)
    if (!original) return null
    const draft = { draft_id: `d${drafts.size + 1}`, to: original.from, subject: /^re:/i.test(original.subject) ? original.subject : `Re: ${original.subject}` }
    drafts.set(idempotencyKey, draft)
    return { ...draft, created: true }
  },
}
const drafts = new Map<string, { draft_id: string; to: string; subject: string }>()

export const calendar = {
  listEvents: (days = 1): Event[] => {
    const events = load<Event>('calendar').sort((a, b) => a.start.localeCompare(b.start))
    if (!events.length) return []
    const day0 = Number(events[0].start.slice(8, 10))
    return events.filter(e => Number(e.start.slice(8, 10)) - day0 < days)
  },
}

export const taskSeed = (): Task[] => load<Task>('tasks')
const taskState: Task[] = load<Task>('tasks')
const byKey = new Map<string, Task>()

export const tasks = {
  listTasks: (includeDone = false): Task[] => taskState.filter(t => includeDone || !t.done),
  createTask: (title: string, due: string | null, idempotencyKey: string) => {
    if (!title.trim()) throw new ToolError('invalid_title', 'title must not be empty')
    if (due !== null && !/^\d{4}-\d{2}-\d{2}$/.test(due)) throw new ToolError('invalid_due', `due must be YYYY-MM-DD, got ${due}`)
    const existing = byKey.get(idempotencyKey)
    if (existing) return { ...existing, created: false }
    const task: Task = { id: `t${taskState.length + 1}`, title: title.trim(), due: due ?? '', done: false }
    taskState.push(task); byKey.set(idempotencyKey, task)
    return { ...task, created: true }
  },
}
