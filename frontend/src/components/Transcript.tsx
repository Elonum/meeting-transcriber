import { useEffect, useMemo, useRef, useState } from 'react'
import type { Participant } from '../types'
import type { TimelineItem } from '../useSessionStream'
import { formatMs } from '../format'

interface TranscriptProps {
  timeline: TimelineItem[]
  participants: Participant[]
  live: boolean
}

/** A two-hour meeting is thousands of lines; the DOM only needs the last ones. */
const MAX_RENDERED = 400

export function Transcript({ timeline, participants, live }: TranscriptProps) {
  const [query, setQuery] = useState('')
  const [follow, setFollow] = useState(true)
  const scroller = useRef<HTMLDivElement | null>(null)

  const byId = useMemo(
    () => Object.fromEntries(participants.map((participant) => [participant.id, participant])),
    [participants],
  )

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return timeline
    return timeline.filter((item) =>
      item.kind === 'notice'
        ? item.notice.message.toLowerCase().includes(needle)
        : item.segment.text.toLowerCase().includes(needle),
    )
  }, [timeline, query])

  const visible = useMemo(
    () => (filtered.length > MAX_RENDERED ? filtered.slice(-MAX_RENDERED) : filtered),
    [filtered],
  )
  const hidden = filtered.length - visible.length

  useEffect(() => {
    const node = scroller.current
    if (!node || !follow) return
    node.scrollTop = node.scrollHeight
  }, [visible.length, follow, timeline])

  const onScroll = () => {
    const node = scroller.current
    if (!node) return
    const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 48
    if (atBottom !== follow) setFollow(atBottom)
  }

  return (
    <section className="transcript" aria-label="Транскрипт">
      <div className="transcript__head">
        <h2>
          Транскрипт
          {live && (
            <span className="live-badge">
              <span className="dot dot--live" />
              запись
            </span>
          )}
        </h2>

        <div className="transcript__tools">
          <input
            className="search"
            type="search"
            placeholder="Поиск по тексту"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          {query && <span className="search__count">{filtered.length}</span>}
          <button
            type="button"
            className={`ghost ghost--icon${follow ? ' ghost--on' : ''}`}
            onClick={() => {
              setFollow(true)
              const node = scroller.current
              if (node) node.scrollTop = node.scrollHeight
            }}
            title="Следовать за новыми репликами"
            aria-label="Автоскролл"
          >
            ↓
          </button>
        </div>
      </div>

      <div className="transcript__feed" ref={scroller} onScroll={onScroll}>
        {filtered.length === 0 && (
          <p className="feed__empty">
            {query
              ? 'Ничего не найдено.'
              : 'Тишина. Реплики появятся, как только участники начнут говорить.'}
          </p>
        )}

        {hidden > 0 && (
          <p className="feed__truncated">
            Показаны последние {visible.length} из {filtered.length}. Полный протокол — в экспорте.
          </p>
        )}

        {visible.map((item) => {
          if (item.kind === 'notice') {
            return (
              <p key={item.id} className="feed__notice">
                <span className="feed__notice-icon">{item.notice.level === 'warn' ? '!' : 'i'}</span>
                {item.notice.message}
              </p>
            )
          }

          const speaker = byId[item.segment.participant_id]
          const partial = item.kind === 'partial'
          const language = item.segment.language
          return (
            <article
              key={item.id}
              className={`line${partial ? ' line--partial' : ''}`}
              style={{ borderLeftColor: speaker?.color ?? 'var(--line)' }}
            >
              <div className="line__meta">
                <span className="line__speaker" style={{ color: speaker?.color }}>
                  {speaker?.display_name ?? 'Спикер'}
                </span>
                <time className="line__time">{formatMs(item.segment.start_ms)}</time>
                {language && language !== 'ru' && (
                  <span className="line__lang" title={`Язык фразы: ${language}`}>
                    {language.toUpperCase()}
                  </span>
                )}
              </div>
              <p className="line__text">
                {item.segment.text}
                {partial && <span className="caret" />}
              </p>
            </article>
          )
        })}
      </div>
    </section>
  )
}
