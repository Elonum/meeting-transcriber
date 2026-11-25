import { useEffect, useState } from 'react'
import type { SessionInfo } from '../types'
import { formatWhen, stateLabel } from '../format'
import { roomLabel } from '../link'

interface SessionListProps {
  sessions: SessionInfo[]
  /** Matches on the server; can be larger than what the page shows. */
  total: number
  activeId: string | null
  query: string
  searching: boolean
  onSelect: (id: string) => void
  onQueryChange: (query: string) => void
  onDelete: (id: string) => Promise<void>
}

/** History with search over transcripts and a two-step delete. */
export function SessionList({
  sessions,
  total,
  activeId,
  query,
  searching,
  onSelect,
  onQueryChange,
  onDelete,
}: SessionListProps) {
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  // A pending confirmation should not stay armed while the user does something else.
  useEffect(() => {
    if (!confirmId) return
    const timer = window.setTimeout(() => setConfirmId(null), 4000)
    return () => window.clearTimeout(timer)
  }, [confirmId])

  const remove = async (id: string) => {
    setBusyId(id)
    try {
      await onDelete(id)
      setConfirmId(null)
    } finally {
      setBusyId(null)
    }
  }

  return (
    <section className="history">
      <div className="history__head">
        <h2 className="card__title">История</h2>
        {searching && <span className="history__counting">ищем…</span>}
      </div>

      <input
        className="history__search"
        type="search"
        placeholder="Поиск по встречам и репликам"
        value={query}
        onChange={(event) => onQueryChange(event.target.value)}
        aria-label="Поиск по истории"
      />

      {total > sessions.length && (
        <p className="history__more">
          Показаны последние {sessions.length} из {total}
        </p>
      )}

      {sessions.length === 0 ? (
        <p className="history__empty">
          {query.trim() ? 'Ничего не нашлось — попробуйте другое слово.' : 'Здесь появятся прошедшие встречи.'}
        </p>
      ) : (
        <ul className="history__list">
          {sessions.map((session) => (
            <li key={session.id} className="history__row">
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
                {session.snippet && <span className="history__snippet">«{session.snippet}»</span>}
              </button>

              {confirmId === session.id ? (
                <span className="history__confirm">
                  <button
                    type="button"
                    className="history__danger"
                    onClick={() => void remove(session.id)}
                    disabled={busyId === session.id}
                  >
                    {busyId === session.id ? '…' : 'удалить'}
                  </button>
                  <button type="button" className="history__cancel" onClick={() => setConfirmId(null)}>
                    нет
                  </button>
                </span>
              ) : (
                <button
                  type="button"
                  className="history__delete"
                  onClick={() => setConfirmId(session.id)}
                  title="Удалить встречу и записи"
                  aria-label={`Удалить ${session.title || 'встречу'}`}
                >
                  ✕
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
