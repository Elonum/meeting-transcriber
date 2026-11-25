import { useCallback, useEffect, useMemo, useState } from 'react'
import { createSession, deleteSession, listSessions, stopSession } from './api'
import type { CreateSessionPayload, SessionInfo } from './types'
import { useSessionStream } from './useSessionStream'
import { formatDuration, useElapsed } from './useElapsed'
import { useTheme } from './theme'
import { roomLabel } from './link'
import { stateLabel } from './format'
import { NewSessionForm } from './components/NewSessionForm'
import { SessionList } from './components/SessionList'
import { ExportMenu } from './components/ExportMenu'
import { ThemeToggle } from './components/ThemeToggle'
import { Scene } from './components/Scene'
import { Transcript } from './components/Transcript'

const SEARCH_DEBOUNCE_MS = 250

function sessionFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get('session')
}

export default function App() {
  const { theme, toggle } = useTheme()

  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [activeId, setActiveId] = useState<string | null>(sessionFromUrl)
  const [busy, setBusy] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [searching, setSearching] = useState(false)

  const stream = useSessionStream(activeId)

  // Typing in the history search should not fire a request per keystroke.
  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query), SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [query])

  const refresh = useCallback(async () => {
    setSearching(true)
    try {
      const { sessions: list } = await listSessions(debouncedQuery)
      setSessions(list)
      setLoadError(null)
    } catch (cause) {
      setLoadError(cause instanceof Error ? cause.message : 'Сервер недоступен')
    } finally {
      setSearching(false)
    }
  }, [debouncedQuery])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const active = useMemo(
    () => stream.session ?? sessions.find((session) => session.id === activeId) ?? null,
    [stream.session, sessions, activeId],
  )

  // Keep the list in sync when a streamed session changes state.
  useEffect(() => {
    if (stream.session) void refresh()
  }, [stream.session?.state, refresh, stream.session])

  // A session link is shareable: keep it in the address bar.
  useEffect(() => {
    const url = new URL(window.location.href)
    if (activeId) url.searchParams.set('session', activeId)
    else url.searchParams.delete('session')
    window.history.replaceState(null, '', url.toString())
  }, [activeId])

  const elapsed = useElapsed(
    active?.started_at_ms,
    active?.state === 'live' || active?.state === 'connecting',
  )

  const onCreate = async (payload: CreateSessionPayload) => {
    setBusy(true)
    try {
      const created = await createSession(payload)
      setActiveId(created.id)
      setQuery('')
      await refresh()
    } finally {
      setBusy(false)
    }
  }

  const onStop = async () => {
    if (!active) return
    await stopSession(active.id)
    await refresh()
  }

  const onDelete = async (id: string) => {
    await deleteSession(id)
    setSessions((current) => current.filter((session) => session.id !== id))
    if (activeId === id) setActiveId(null)
    await refresh()
  }

  const live = active?.state === 'live' || active?.state === 'connecting'
  const waiting = active?.state === 'scheduled'

  return (
    <div className="app">
      <aside className="sidebar">
        <header className="brand">
          <span className="brand__mark">T</span>
          <div>
            <strong>Transcribe</strong>
            <small>протокол встречи SaluteJazz</small>
          </div>
          <ThemeToggle theme={theme} onToggle={toggle} />
        </header>

        <NewSessionForm busy={busy} onCreate={onCreate} />
        <SessionList
          sessions={sessions}
          activeId={activeId}
          query={query}
          searching={searching}
          onSelect={setActiveId}
          onQueryChange={setQuery}
          onDelete={onDelete}
        />

        <footer className="sidebar__foot">
          Речь распознаётся локально · звук не покидает этот компьютер
        </footer>
      </aside>

      <main className="workspace">
        {loadError && <div className="banner banner--error">{loadError}</div>}

        {!active ? (
          <div className="welcome">
            <h1>Встреча превращается в протокол</h1>
            <p>
              Вставьте адрес видеовстречи из SaluteJazz — агент войдёт в комнату как участник,
              распознает речь, определит, кто говорит, и покажет лица.
            </p>
            <ol className="steps">
              <li>
                <strong>Адрес.</strong> Скопируйте «Адрес видеовстречи» из приглашения. Пароль обычно
                уже внутри ссылки.
              </li>
              <li>
                <strong>Подключение.</strong> Агент подключается за 10–20 секунд и включает микрофон,
                иначе платформа не передаёт звук.
              </li>
              <li>
                <strong>Материалы.</strong> Реплики идут в реальном времени, аудио пишется рядом; в
                конце — один архив со всем.
              </li>
            </ol>
          </div>
        ) : (
          <>
            <header className="topbar">
              <div className="topbar__title">
                <h1>{active.title || `Встреча ${roomLabel(active.meeting_url)}`}</h1>
                <span className="topbar__room">
                  комната {roomLabel(active.meeting_url)}
                  {active.pipeline === 'mock' && ' · демо'}
                </span>
              </div>

              <div className="topbar__actions">
                {(live || waiting) && <span className="timer">{formatDuration(elapsed)}</span>}
                <span className={`pill pill--${active.state}`}>
                  <span className={`dot dot--${active.state === 'live' ? 'live' : 'idle'}`} />
                  {stateLabel(active.state)}
                </span>
                <ExportMenu session={active} participants={stream.participants} />
                {(live || waiting) && (
                  <button type="button" className="danger" onClick={onStop}>
                    {waiting ? 'Отменить' : 'Остановить'}
                  </button>
                )}
              </div>
            </header>

            {active.error && <div className="banner banner--error">Ошибка: {active.error}</div>}
            {waiting && active.start_at_ms && (
              <div className="banner banner--info">
                Агент подключится позже — до этого момента ничего не записывается. Сессию можно
                отменить.
              </div>
            )}
            {active.pipeline === 'mock' && (
              <div className="banner banner--info">
                Демо-сессия: речь синтезирована, протокол и экспорт — настоящие.
              </div>
            )}

            <Scene participants={stream.participants} live={live} />
            <Transcript timeline={stream.timeline} participants={stream.participants} live={live} />
          </>
        )}
      </main>
    </div>
  )
}
