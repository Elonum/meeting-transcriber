import type { CreateSessionPayload, SessionInfo } from './types'

async function jsonFetch<T>(input: string, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init)
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const body = await response.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* keep the status text */
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

export interface SessionListResponse {
  sessions: SessionInfo[]
  query: string
  total: number
}

export function listSessions(query?: string): Promise<SessionListResponse> {
  const trimmed = query?.trim()
  const suffix = trimmed ? `?q=${encodeURIComponent(trimmed)}` : ''
  return jsonFetch(`/api/sessions${suffix}`)
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

export interface SessionDetail {
  session: SessionInfo
  participants: unknown[]
  segments: unknown[]
}

export function getSession(id: string): Promise<SessionDetail> {
  return jsonFetch(`/api/sessions/${id}`)
}

export async function deleteSession(id: string): Promise<void> {
  const response = await fetch(`/api/sessions/${id}`, { method: 'DELETE' })
  // 404 means it is already gone, which is the outcome the caller wanted.
  if (!response.ok && response.status !== 404) {
    throw new Error(`не удалось удалить встречу (HTTP ${response.status})`)
  }
}

export function exportUrl(id: string, format: string): string {
  return `/api/sessions/${id}/export/${format}`
}

export function logUrl(id: string): string {
  return `/api/sessions/${id}/log`
}

export function audioUrl(id: string, file: string): string {
  return `/api/sessions/${id}/audio/${encodeURIComponent(file)}`
}
