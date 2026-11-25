import { useEffect, useMemo, useReducer, useRef } from 'react'
import type { Notice, Participant, Segment, ServerEvent, SessionInfo } from './types'

interface StreamState {
  session: SessionInfo | null
  participants: Record<string, Participant>
  finals: Record<string, Segment>
  partials: Record<string, Segment>
  notices: Notice[]
  connected: boolean
  doneReason: string | null
}

const initialState: StreamState = {
  session: null,
  participants: {},
  finals: {},
  partials: {},
  notices: [],
  connected: false,
  doneReason: null,
}

type Action =
  | { type: 'reset' }
  | { type: 'connected'; value: boolean }
  | { type: 'event'; event: ServerEvent }

function reducer(state: StreamState, action: Action): StreamState {
  if (action.type === 'reset') return { ...initialState }
  if (action.type === 'connected') return { ...state, connected: action.value }

  const event = action.event
  switch (event.type) {
    case 'hello':
      return {
        session: event.session,
        participants: Object.fromEntries(event.participants.map((p) => [p.id, p])),
        finals: Object.fromEntries(event.segments.map((s) => [s.id, s])),
        partials: {},
        notices: state.notices,
        connected: true,
        doneReason: null,
      }
    case 'session':
      if (!state.session) return state
      return {
        ...state,
        session: { ...state.session, state: event.state, error: event.error ?? state.session.error },
      }
    case 'participant':
      return {
        ...state,
        participants: {
          ...state.participants,
          [event.participant.id]: {
            ...state.participants[event.participant.id],
            ...event.participant,
          },
        },
      }
    case 'speech': {
      const participant = state.participants[event.participant_id]
      if (!participant) return state
      return {
        ...state,
        participants: {
          ...state.participants,
          [participant.id]: { ...participant, speaking: event.speaking },
        },
      }
    }
    case 'face': {
      const participant = state.participants[event.participant_id]
      if (!participant) return state
      return {
        ...state,
        participants: {
          ...state.participants,
          [participant.id]: {
            ...participant,
            face_status: event.status,
            face_confidence: event.confidence ?? participant.face_confidence,
            thumbnail: event.thumbnail ?? participant.thumbnail,
          },
        },
      }
    }
    case 'segment': {
      const segment = event.segment
      if (segment.final) {
        // A final replaces the live line of the same speaker. The ids differ
        // (`p-1-live` vs `p-1-52000`), so matching by id alone left stale
        // italics behind for the rest of the meeting.
        const partials = { ...state.partials }
        for (const [id, item] of Object.entries(partials)) {
          if (id === segment.id || item.participant_id === segment.participant_id) delete partials[id]
        }
        return { ...state, finals: { ...state.finals, [segment.id]: segment }, partials }
      }
      return { ...state, partials: { ...state.partials, [segment.id]: segment } }
    }
    case 'notice':
      return {
        ...state,
        notices: [
          ...state.notices,
          {
            id: `notice-${state.notices.length}-${event.at_ms}`,
            at_ms: event.at_ms,
            level: event.level,
            message: event.message,
          },
        ],
      }
    case 'done':
      return { ...state, doneReason: event.reason }
    default:
      return state
  }
}

export type TimelineItem =
  | { kind: 'final'; id: string; at_ms: number; segment: Segment }
  | { kind: 'partial'; id: string; at_ms: number; segment: Segment }
  | { kind: 'notice'; id: string; at_ms: number; notice: Notice }

export interface SessionStream {
  session: SessionInfo | null
  participants: Participant[]
  timeline: TimelineItem[]
  connected: boolean
  doneReason: string | null
  finals: number
}

/** Subscribes to one session's event stream and folds it into view state. */
export function useSessionStream(sessionId: string | null): SessionStream {
  const [state, dispatch] = useReducer(reducer, initialState)
  const latest = useRef(state)
  latest.current = state

  useEffect(() => {
    dispatch({ type: 'reset' })
    if (!sessionId) return

    let socket: WebSocket | null = null
    let retryTimer: number | undefined
    let disposed = false

    const connect = () => {
      const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(`${scheme}://${window.location.host}/events/${sessionId}`)

      socket.onopen = () => dispatch({ type: 'connected', value: true })

      socket.onmessage = (message) => {
        let event: ServerEvent
        try {
          event = JSON.parse(message.data as string) as ServerEvent
        } catch {
          return
        }
        if (event.type === 'ping') return
        dispatch({ type: 'event', event })
      }

      socket.onclose = () => {
        dispatch({ type: 'connected', value: false })
        const finished = latest.current.session?.state === 'finished' || latest.current.session?.state === 'failed'
        if (!disposed && !finished) retryTimer = window.setTimeout(connect, 1500)
      }
    }

    connect()
    return () => {
      disposed = true
      window.clearTimeout(retryTimer)
      socket?.close()
    }
  }, [sessionId])

  const timeline = useMemo<TimelineItem[]>(() => {
    const items: TimelineItem[] = []
    for (const segment of Object.values(state.finals)) {
      items.push({ kind: 'final', id: segment.id, at_ms: segment.start_ms, segment })
    }
    for (const segment of Object.values(state.partials)) {
      if (!state.finals[segment.id]) {
        items.push({ kind: 'partial', id: `partial-${segment.id}`, at_ms: segment.start_ms, segment })
      }
    }
    for (const notice of state.notices) {
      items.push({ kind: 'notice', id: notice.id, at_ms: notice.at_ms, notice })
    }
    return items.sort((a, b) => a.at_ms - b.at_ms)
  }, [state.finals, state.partials, state.notices])

  const participants = useMemo(
    () => Object.values(state.participants).sort((a, b) => a.joined_at_ms - b.joined_at_ms),
    [state.participants],
  )

  return {
    session: state.session,
    participants,
    timeline,
    connected: state.connected,
    doneReason: state.doneReason,
    finals: Object.keys(state.finals).length,
  }
}
