import type { SessionInfo } from '../types'
import { exportUrl, logUrl } from '../api'

const FORMATS = [
  { id: 'srt', label: 'SRT', hint: 'субтитры для плеера' },
  { id: 'vtt', label: 'VTT', hint: 'веб-субтитры' },
  { id: 'md', label: 'Markdown', hint: 'протокол встречи' },
  { id: 'txt', label: 'TXT', hint: 'плоский текст' },
  { id: 'json', label: 'JSON', hint: 'полные данные' },
]

interface ExportPanelProps {
  session: SessionInfo | null
}

export function ExportPanel({ session }: ExportPanelProps) {
  return (
    <section className="card">
      <h2 className="card__title">Файлы</h2>
      <p className="card__hint">
        {session ? 'Транскрипт собирается на сервере из журнала событий.' : 'Появится после старта сессии.'}
      </p>

      <div className="exports">
        {FORMATS.map((format) => (
          <a
            key={format.id}
            className={`export${session ? '' : ' export--disabled'}`}
            href={session ? exportUrl(session.id, format.id) : undefined}
            download
            aria-disabled={!session}
            onClick={(event) => {
              if (!session) event.preventDefault()
            }}
          >
            <strong>{format.label}</strong>
            <span>{format.hint}</span>
          </a>
        ))}
      </div>

      {session && (
        <a className="log-link" href={logUrl(session.id)} target="_blank" rel="noreferrer">
          Журнал событий (JSONL)
        </a>
      )}
    </section>
  )
}
