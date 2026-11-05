// The agent's notion of "today". Mirror of ../../dayflow/clock.py.
// Precedence: DAYFLOW_TODAY > fixture Monday under MOCK=1 > wall clock.
export const FIXTURE_TODAY = '2026-09-07'
export function today(): string {
  if (process.env.DAYFLOW_TODAY) return process.env.DAYFLOW_TODAY
  if ((process.env.MOCK ?? '1') === '1') return FIXTURE_TODAY
  return new Date().toISOString().slice(0, 10)
}
