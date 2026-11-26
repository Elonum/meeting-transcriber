import { useState } from 'react'
import type { FormEvent } from 'react'

interface TokenGateProps {
  onSubmit: (token: string) => Promise<void>
}

/**
 * Shown only when the server was started with `TRANSCRIBE_TOKEN`. The token
 * becomes an HttpOnly cookie, so it never sits in localStorage or the URL.
 */
export function TokenGate({ onSubmit }: TokenGateProps) {
  const [token, setToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!token.trim()) {
      setError('Введите токен доступа')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await onSubmit(token.trim())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось войти')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="gate">
      <form className="card gate__card" onSubmit={submit}>
        <span className="brand__mark">T</span>
        <h1>Нужен токен доступа</h1>
        <p>
          Сервер запущен с <code>TRANSCRIBE_TOKEN</code>. Введите токен — он сохранится в
          защищённой cookie браузера и больше не понадобится.
        </p>
        <label className="field">
          <span>Токен</span>
          <input
            type="password"
            autoFocus
            autoComplete="current-password"
            placeholder="например, 8f3c…"
            value={token}
            onChange={(event) => setToken(event.target.value)}
          />
        </label>
        {error && <p className="form__error">{error}</p>}
        <button type="submit" className="primary" disabled={busy}>
          {busy ? 'Проверяю…' : 'Войти'}
        </button>
      </form>
    </div>
  )
}
