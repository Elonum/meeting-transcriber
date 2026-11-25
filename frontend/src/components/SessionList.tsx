import type { SessionInfo } from '../types'
import { formatWhen, stateLabel } from '../format'
import { roomLabel } from '../link'

interface SessionListProps {
  sessions: SessionInfo[]
  activeId: string | null
  onSelect: (id: string) => void
}

/** Compact history: only what helps pick a previous meeting. */
export function SessionList({ sessions, activeId, onSelect }: SessionListProps) {
  if (sessions.length === 0) return null

  return (
    <section className="history">
      <h2 className="card__title">История</h2>
      <ul className="history__list">
        {sessions.slice(0, 8).map((session) => (
          <li key={session.id}>
            <button
              type="button"
              className={`history__item${session.id === activeId ? ' history__item--active' : ''}`}
              onClick={() => onSelect(session.id)}
              title={session.meeting_url}
            >
              <span className="history__room">{roomLabel(session.meeting_url)}</span>
              <span className="history__title">{session.title || 'Без названия'}</span>
              <span className="history__meta">
                {session.state === 'scheduled' && session.start_at_ms
                  ? formatWhen(session.start_at_ms)
                  : formatWhen(session.started_at_ms)}
                {' · '}
                {stateLabel(session.state).toLowerCase()}
                {session.segments_count > 0 && ` · ${session.segments_count} репл.`}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}
