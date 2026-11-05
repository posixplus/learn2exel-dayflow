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

export const inbox = {
  listMessages: (limit = 20): Message[] =>
    load<Message>('inbox').sort((a, b) => b.date.localeCompare(a.date)).slice(0, limit),
}

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
