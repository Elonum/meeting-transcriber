import type { CreateSessionPayload, SessionInfo } from './types'

async function jsonFetch<T>(input: string, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init)
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const body = await response.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* keep status text */
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

export function listSessions(): Promise<{ sessions: SessionInfo[] }> {
  return jsonFetch('/api/sessions')
}

export function createSession(payload: CreateSessionPayload): Promise<SessionInfo> {
  return jsonFetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export function stopSession(id: string): Promise<SessionInfo> {
  return jsonFetch(`/api/sessions/${id}/stop`, { method: 'POST' })
}

export function exportUrl(id: string, format: string): string {
  return `/api/sessions/${id}/export/${format}`
}

export function logUrl(id: string): string {
  return `/api/sessions/${id}/log`
}
