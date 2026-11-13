// Lesson 88: injection, exfiltration and least privilege. Mirror of ../../dayflow/security.py,
// which carries the full commentary. Structural rules on write arguments, taint, and a
// pattern scan that is a signal, never the defence.
export const MAX_DRAFT_CHARS = 600
export const MIN_COPIED_CHARS = 40
const URL_RE = /(?:https?:\/\/|www\.)[^\s"'<>)]+/gi

export const SIGNS: Record<string, RegExp> = {
  overrides_instructions: /ignore\s+(?:all\s+|your\s+)?(?:previous|prior|above)\s+instructions/i,
  addresses_assistant: /\b(?:ai\s+)?assistants?\b|\bto\s+ai\b|\bllm\b/i,
  claims_authority: /\bsystem\s+(?:notice|prompt|message)\b|pre-?approved/i,
  asks_for_secrecy: /do\s+not\s+(?:mention|tell|reveal|include)\b/i,
  hidden_text: /<!--[\s\S]*?-->/,
  imitates_tool_result: /"ok"\s*:\s*(?:true|false)/i,
}

/** Names of the signs found. An empty list means 'nothing matched', not 'safe'. */
export const scan = (text: string): string[] => Object.entries(SIGNS).filter(([, re]) => re.test(text ?? '')).map(([name]) => name)
export const urls = (text: string): string[] => [...(text ?? '').matchAll(URL_RE)].map(m => m[0].replace(/[.,;]+$/, ''))

type Msg = { id: string; subject?: string; body?: string; [k: string]: unknown }

/** What this run has read. Reset at the start of every run. */
export class RunState {
  bodies = new Map<string, string>()
  flagged = new Map<string, string[]>()
  flaggedUrls = new Set<string>()
  get tainted() { return this.flagged.size > 0 }
  observe<T extends Msg>(messages: T[]): T[] {
    return messages.map(m => {
      const body = m.body ?? ''
      this.bodies.set(m.id, body)
      const signs = scan(`${m.subject ?? ''}\n${body}`)
      if (!signs.length) return m
      this.flagged.set(m.id, signs)
      urls(body).forEach(u => this.flaggedUrls.add(u))
      return { ...m, flags: signs }
    })
  }
}
export let RUN = new RunState()
export const reset = () => (RUN = new RunState())

const squash = (s: string) => s.split(/\s+/).filter(Boolean).join(' ')

/** Id of a message, other than the one being answered, that `body` quotes at length. */
export function copiedFromOtherMessage(body: string, answering?: string, state = RUN): string | null {
  const text = squash(body)
  for (const [id, raw] of state.bodies) {
    if (id === answering) continue
    const other = squash(raw)
    for (let i = 0; i + MIN_COPIED_CHARS <= other.length; i += 8) if (text.includes(other.slice(i, i + MIN_COPIED_CHARS))) return id
  }
  return null
}

/** Why this write must not run, or null. Deterministic. */
export function contentViolation(tool: string, args: Record<string, unknown>, state = RUN): string | null {
  const text = Object.values(args).filter((v): v is string => typeof v === 'string').join(' ')
  if (urls(text).length) return 'it contains a link'
  if (tool === 'draft_reply') {
    const body = String(args.body ?? ''), id = args.message_id as string | undefined
    if (body.length > MAX_DRAFT_CHARS) return `the draft is ${body.length} characters; the limit is ${MAX_DRAFT_CHARS}`
    const source = copiedFromOtherMessage(body, id, state)
    if (source) return `the draft quotes message ${source}, which is not the message it answers`
    if (id && state.flagged.has(id)) return `message ${id} was flagged (${state.flagged.get(id)!.join(', ')})`
  }
  return null
}

/** Links in the final brief that came from a flagged message. */
export const flaggedLinksIn = (text: string, state = RUN): string[] => urls(text).filter(u => state.flaggedUrls.has(u))
