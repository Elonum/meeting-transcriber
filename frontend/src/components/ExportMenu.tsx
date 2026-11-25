import { useEffect, useRef, useState } from 'react'
import type { SessionInfo } from '../types'
import { exportUrl, logUrl } from '../api'

const FORMATS = [
  { id: 'srt', label: 'Субтитры SRT' },
  { id: 'vtt', label: 'Субтитры VTT' },
  { id: 'md', label: 'Протокол Markdown' },
  { id: 'txt', label: 'Текст TXT' },
  { id: 'json', label: 'Данные JSON' },
]

/** Download menu for the finished or running transcript. */
export function ExportMenu({ session }: { session: SessionInfo }) {
  const [open, setOpen] = useState(false)
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
        title={empty ? 'Пока нечего сохранять' : 'Скачать транскрипт'}
      >
        Скачать
      </button>

      {open && (
        <div className="menu__panel" role="menu">
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
