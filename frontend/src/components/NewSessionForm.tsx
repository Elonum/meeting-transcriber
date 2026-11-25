import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import type { CreateSessionPayload } from '../types'
import { parseMeetingLink } from '../link'
import { toLocalInputValue } from '../format'

interface NewSessionFormProps {
  busy: boolean
  onCreate: (payload: CreateSessionPayload) => Promise<void>
}

const PLACEHOLDER = 'https://salutejazz.ru/calls/xxxxxx?psw=…'

export function NewSessionForm({ busy, onCreate }: NewSessionFormProps) {
  const [url, setUrl] = useState('')
  const [password, setPassword] = useState('')
  const [title, setTitle] = useState('')
  const [startAt, setStartAt] = useState('')
  const [error, setError] = useState<string | null>(null)

  const link = useMemo(() => parseMeetingLink(url), [url])
  const trimmedUrl = url.trim()

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)

    if (!/^https?:\/\/\S+$/i.test(trimmedUrl)) {
      setError('Вставьте адрес видеовстречи — он начинается с https://')
      return
    }
    if (!link.recognized) {
      setError('Это не похоже на ссылку SaluteJazz: ожидается …/calls/xxxxxx')
      return
    }
    if (!link.hasPassword && !password.trim()) {
      setError('В ссылке нет пароля — введите пароль видеовстречи')
      return
    }

    try {
      await onCreate({
        meeting_url: trimmedUrl,
        title: title.trim() || null,
        password: link.hasPassword ? null : password.trim(),
        start_at: startAt ? new Date(startAt).toISOString() : null,
        pipeline: 'jazz',
      })
      setUrl('')
      setPassword('')
      setTitle('')
      setStartAt('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось подключиться')
    }
  }

  return (
    <form className="card form" onSubmit={submit}>
      <h2 className="card__title">Новая встреча</h2>

      <label className="field">
        <span>Адрес видеовстречи</span>
        <input
          type="url"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          placeholder={PLACEHOLDER}
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          required
        />
      </label>

      {trimmedUrl && (
        <p className="hint">
          {link.recognized ? (
            <>
              <span className="chip">Комната {link.code}</span>
              {link.hasPassword ? (
                <span className="hint__text">пароль уже в ссылке</span>
              ) : (
                <span className="hint__text hint__text--warn">нужен пароль</span>
              )}
            </>
          ) : (
            <span className="hint__text hint__text--warn">
              не нашли код комнаты — проверьте адрес
            </span>
          )}
        </p>
      )}

      {(!link.hasPassword || !trimmedUrl) && (
        <label className="field">
          <span>
            Пароль видеовстречи <em>если его нет в ссылке</em>
          </span>
          <input
            type="text"
            autoComplete="off"
            placeholder="например, a1b2c3d4"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
      )}

      <details className="advanced">
        <summary>Запланировать</summary>
        <div className="advanced__body">
          <label className="field">
            <span>Начать в <em>пусто — подключиться сразу</em></span>
            <input
              type="datetime-local"
              value={startAt}
              min={toLocalInputValue(Date.now())}
              onChange={(event) => setStartAt(event.target.value)}
            />
          </label>
          <label className="field">
            <span>Название <em>для истории</em></span>
            <input
              type="text"
              placeholder="Планёрка по транскрибации"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
            />
          </label>
        </div>
      </details>

      {error && <p className="form__error">{error}</p>}

      <button type="submit" className="primary" disabled={busy}>
        {busy ? 'Подключаюсь…' : 'Подключить агента'}
      </button>
    </form>
  )
}
