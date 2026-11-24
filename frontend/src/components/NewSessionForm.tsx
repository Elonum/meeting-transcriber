import { useState } from 'react'
import type { FormEvent } from 'react'
import type { CreateSessionPayload } from '../types'
import { toLocalInputValue } from '../format'

type Mode = 'mock' | 'jazz'

interface NewSessionFormProps {
  busy: boolean
  onCreate: (payload: CreateSessionPayload) => Promise<void>
}

export function NewSessionForm({ busy, onCreate }: NewSessionFormProps) {
  const [url, setUrl] = useState('')
  const [title, setTitle] = useState('')
  const [duration, setDuration] = useState('')
  const [startAt, setStartAt] = useState('')
  const [speed, setSpeed] = useState('1')
  const [mode, setMode] = useState<Mode>('mock')
  const [showBrowser, setShowBrowser] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const live = mode === 'jazz'

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    if (!/^https?:\/\/\S+$/i.test(url.trim())) {
      setError('Нужна полная ссылка на встречу, например https://jazz.sber.ru/…')
      return
    }
    try {
      await onCreate({
        meeting_url: url.trim(),
        title: title.trim() || null,
        duration_min: duration ? Number(duration) : null,
        start_at: startAt ? new Date(startAt).toISOString() : null,
        pipeline: mode,
        speed: live ? 1 : Number(speed),
        headless: !showBrowser,
      })
      setUrl('')
      setTitle('')
      setDuration('')
      setStartAt('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось запустить сессию')
    }
  }

  return (
    <form className="card form" onSubmit={submit}>
      <h2 className="card__title">Новая встреча</h2>
      <p className="card__hint">
        {live
          ? 'Агент откроет встречу в браузере, будет слушать каждого участника отдельно и распознавать речь локально.'
          : 'Агент войдёт по ссылке и начнёт транскрибацию.'}
      </p>

      <label className="field">
        <span>Ссылка на встречу</span>
        <input
          type="url"
          placeholder="https://jazz.sber.ru/…"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          required
        />
      </label>

      <label className="field">
        <span>Режим</span>
        <select value={mode} onChange={(event) => setMode(event.target.value as Mode)}>
          <option value="mock">Демо-сценарий (без подключения)</option>
          <option value="jazz">Живая встреча (браузерный агент)</option>
        </select>
      </label>

      <label className="field">
        <span>Название <em>необязательно</em></span>
        <input
          type="text"
          placeholder="Планёрка по транскрибации"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
        />
      </label>

      <label className="field">
        <span>Начать в <em>пусто — подключиться сразу</em></span>
        <input
          type="datetime-local"
          value={startAt}
          min={toLocalInputValue(Date.now())}
          onChange={(event) => setStartAt(event.target.value)}
        />
      </label>

      <div className="field-row">
        <label className="field">
          <span>Лимит, мин <em>необязательно</em></span>
          <input
            type="number"
            min={1}
            max={600}
            placeholder="—"
            value={duration}
            onChange={(event) => setDuration(event.target.value)}
          />
        </label>

        {live ? (
          <label className="field field--check">
            <span>Браузер</span>
            <label className="check">
              <input
                type="checkbox"
                checked={showBrowser}
                onChange={(event) => setShowBrowser(event.target.checked)}
              />
              <span>показывать окно</span>
            </label>
          </label>
        ) : (
          <label className="field">
            <span>Темп демо</span>
            <select value={speed} onChange={(event) => setSpeed(event.target.value)}>
              <option value="1">1× реальное время</option>
              <option value="4">4× быстрее</option>
              <option value="10">10× быстрее</option>
            </select>
          </label>
        )}
      </div>

      {live && (
        <p className="form__note">
          Речь распознаётся локально (faster-whisper), звук никуда не уходит. Первый запуск
          подгружает модель и занимает до минуты.
        </p>
      )}

      {error && <p className="form__error">{error}</p>}

      <button type="submit" className="primary" disabled={busy}>
        {busy ? 'Запускаю агента…' : 'Подключить агента'}
      </button>
    </form>
  )
}
