import { useCallback, useEffect, useMemo, useState } from 'react'
import { createSession, listSessions, stopSession } from './api'
import type { CreateSessionPayload, SessionInfo } from './types'
import { useSessionStream } from './useSessionStream'
import { formatClock, formatWhen, stateLabel } from './format'
import { NewSessionForm } from './components/NewSessionForm'
import { SessionList } from './components/SessionList'
import { ExportPanel } from './components/ExportPanel'
import { Scene } from './components/Scene'
import { Transcript } from './components/Transcript'

export default function App() {
  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  const stream = useSessionStream(activeId)

  const refresh = useCallback(async () => {
    try {
      const { sessions: list } = await listSessions()
      setSessions(list)
    } catch (cause) {
      setLoadError(cause instanceof Error ? cause.message : 'Сервер недоступен')
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const active = useMemo(
    () => stream.session ?? sessions.find((session) => session.id === activeId) ?? null,
    [stream.session, sessions, activeId],
  )

  // Keep the sidebar in sync when a streamed session changes state.
  useEffect(() => {
    if (stream.session) void refresh()
  }, [stream.session?.state, refresh, stream.session])

  const onCreate = async (payload: CreateSessionPayload) => {
    setBusy(true)
    try {
      const created = await createSession(payload)
      setActiveId(created.id)
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

  const live = active?.state === 'live' || active?.state === 'connecting'
  const waiting = active?.state === 'scheduled'

  return (
    <div className="app">
      <aside className="sidebar">
        <header className="brand">
          <span className="brand__mark">T</span>
          <div>
            <strong>Transcribe</strong>
            <small>онлайн-транскрибация встреч</small>
          </div>
        </header>

        <NewSessionForm busy={busy} onCreate={onCreate} />
        <SessionList sessions={sessions} activeId={activeId} onSelect={setActiveId} onRefresh={refresh} />
        <ExportPanel session={active} />
      </aside>

      <main className="workspace">
        {loadError && <div className="banner banner--error">{loadError}</div>}

        {!active ? (
          <div className="welcome">
            <h1>Слушайте встречу, получайте протокол</h1>
            <p>
              Укажите ссылку на встречу Jazz — агент подключится как участник, распознает речь, определит
              говорящих и покажет их лица. Всё, что он услышит, попадёт в файл.
            </p>
            <ul>
              <li>Текст появляется в реальном времени, промежуточные гипотезы — серым.</li>
              <li>Спикер определяется по отдельной аудиодорожке участника, а не догадками.</li>
              <li>Экспорт: SRT, VTT, Markdown, TXT, JSON плюс журнал событий.</li>
            </ul>
          </div>
        ) : (
          <>
            <header className="topbar">
              <div className="topbar__title">
                <h1>{active.title || 'Встреча'}</h1>
                <a className="topbar__url" href={active.meeting_url} target="_blank" rel="noreferrer">
                  {active.meeting_url}
                </a>
              </div>

              <div className="topbar__actions">
                <span className={`pill pill--${active.state}`}>
                  <span className={`dot dot--${active.state === 'live' ? 'live' : 'idle'}`} />
                  {stateLabel(active.state)}
                </span>
                <span className="topbar__meta">
                  {waiting && active.start_at_ms
                    ? `агент подключится ${formatWhen(active.start_at_ms)}`
                    : `начало ${formatClock(active.started_at_ms)} · ${stream.finals} реплик`}
                  {!stream.connected && active.state === 'live' && ' · переподключение…'}
                </span>
                {(live || waiting) && (
                  <button type="button" className="danger" onClick={onStop}>
                    {waiting ? 'Отменить' : 'Остановить'}
                  </button>
                )}
              </div>
            </header>

            {active.error && <div className="banner banner--error">Ошибка сессии: {active.error}</div>}
            {waiting && active.start_at_ms && (
              <div className="banner banner--info">
                Агент подключится {formatWhen(active.start_at_ms)}. До этого момента ничего не записывается —
                сессию можно отменить.
              </div>
            )}
            {active.pipeline === 'mock' && (
              <div className="banner banner--info">
                Демо-пайплайн: речь синтезирована, но протокол, спикеры, лица и экспорт — настоящие.
              </div>
            )}

            <Scene participants={stream.participants} />
            <Transcript timeline={stream.timeline} participants={stream.participants} live={live} />
          </>
        )}
      </main>
    </div>
  )
}
