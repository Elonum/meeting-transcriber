import type { SessionInfo } from '../types'
import { formatClock, stateLabel } from '../format'

interface SessionListProps {
  sessions: SessionInfo[]
  activeId: string | null
  onSelect: (id: string) => void
  onRefresh: () => void
}

export function SessionList({ sessions, activeId, onSelect, onRefresh }: SessionListProps) {
  return (
    <section className="card">
      <div className="card__head">
        <h2 className="card__title">Сессии</h2>
        <button type="button" className="ghost" onClick={onRefresh} title="Обновить список">
          ⟳
        </button>
      </div>

      {sessions.length === 0 ? (
        <p className="card__hint">Пока пусто — запустите первую встречу.</p>
      ) : (
        <ul className="sessions">
          {sessions.map((session) => (
            <li key={session.id}>
              <button
                type="button"
                className={`session${session.id === activeId ? ' session--active' : ''}`}
                onClick={() => onSelect(session.id)}
              >
                <span className={`session__state session__state--${session.state}`}>
                  {stateLabel(session.state)}
                </span>
                <span className="session__title">{session.title || 'Без названия'}</span>
                <span className="session__meta">
                  {formatClock(session.started_at_ms)} · {session.segments_count} реплик
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
