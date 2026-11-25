import { useEffect, useRef, useState } from 'react'
import type { AudioTrack, Participant, SessionInfo } from '../types'
import { audioUrl, exportUrl, getSession, logUrl } from '../api'

const FORMATS = [
  { id: 'srt', label: 'Субтитры SRT' },
  { id: 'vtt', label: 'Субтитры VTT' },
  { id: 'md', label: 'Протокол Markdown' },
  { id: 'txt', label: 'Текст TXT' },
  { id: 'json', label: 'Данные JSON' },
]

interface ExportMenuProps {
  session: SessionInfo
  participants: Participant[]
}

/** Everything the meeting produced, in one place. */
export function ExportMenu({ session, participants }: ExportMenuProps) {
  const [open, setOpen] = useState(false)
  const [audio, setAudio] = useState<AudioTrack[]>([])
  const box = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // The list of recorded files is only known on the detail endpoint; one small
  // request when the menu opens is cheaper than streaming it with every event.
  useEffect(() => {
    if (!open) return
    let alive = true
    getSession(session.id)
      .then((detail) => {
        if (alive) setAudio(detail.session.audio ?? [])
      })
      .catch(() => {
        if (alive) setAudio([])
      })
    return () => {
      alive = false
    }
  }, [open, session.id])

  const names = new Map(participants.map((part) => [part.id, part.display_name]))
  const empty = session.segments_count === 0

  return (
    <div className="menu" ref={box}>
      <button
        type="button"
        className="ghost"
        onClick={() => setOpen((value) => !value)}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={empty}
        title={empty ? 'Пока нечего сохранять' : 'Скачать материалы встречи'}
      >
        Скачать
      </button>

      {open && (
        <div className="menu__panel" role="menu">
          <a
            className="menu__item menu__item--primary"
            role="menuitem"
            href={exportUrl(session.id, 'zip')}
            download
            onClick={() => setOpen(false)}
          >
            Всё в архиве ZIP
          </a>

          {FORMATS.map((format) => (
            <a
              key={format.id}
              className="menu__item"
              role="menuitem"
              href={exportUrl(session.id, format.id)}
              download
              onClick={() => setOpen(false)}
            >
              {format.label}
            </a>
          ))}

          {audio.length > 0 && (
            <>
              <span className="menu__label">Записи участников</span>
              {audio.map((track) => (
                <a
                  key={track.file}
                  className="menu__item menu__item--audio"
                  role="menuitem"
                  href={audioUrl(session.id, track.file)}
                  download
                  onClick={() => setOpen(false)}
                >
                  <span>{names.get(track.participant_id) ?? track.participant_id}</span>
                  <span className="menu__hint">{formatClock(track.duration_s)}</span>
                </a>
              ))}
            </>
          )}

          <a
            className="menu__item menu__item--muted"
            role="menuitem"
            href={logUrl(session.id)}
            target="_blank"
            rel="noreferrer"
            onClick={() => setOpen(false)}
          >
            Журнал событий
          </a>
        </div>
      )}
    </div>
  )
}

function formatClock(seconds: number): string {
  const total = Math.round(seconds)
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return `${minutes}:${String(rest).padStart(2, '0')}`
}
