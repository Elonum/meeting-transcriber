export type SessionState =
  | 'scheduled'
  | 'connecting'
  | 'live'
  | 'stopping'
  | 'finished'
  | 'failed'

export interface AudioTrack {
  participant_id: string
  file: string
  duration_s: number
}

export interface SessionInfo {
  id: string
  title: string
  meeting_url: string
  pipeline: string
  state: SessionState
  error: string | null
  started_at_ms: number
  created_at_ms: number
  start_at_ms: number | null
  duration_min: number | null
  participants_count: number
  segments_count: number
  /** Present on the detail endpoint: recorded WAV per participant. */
  audio?: AudioTrack[]
  /** Present in search results: an excerpt around the match. */
  snippet?: string | null
}

export type FaceStatus = 'unknown' | 'detected' | 'none' | 'frame'

export interface Participant {
  id: string
  display_name: string
  role: 'human' | 'bot'
  has_video: boolean
  color: string
  speaking: boolean
  /** False once the person leaves; the tile disappears, the transcript keeps them. */
  present?: boolean
  face_status: FaceStatus
  face_confidence: number | null
  thumbnail: string | null
  joined_at_ms: number
}

export interface Segment {
  id: string
  participant_id: string
  start_ms: number
  end_ms: number
  text: string
  /** Language Whisper detected for this phrase; shown when it is not Russian. */
  language?: string | null
  final: boolean
  confidence: number | null
  revision?: number
}

export interface Notice {
  id: string
  at_ms: number
  level: string
  message: string
}

export type ServerEvent =
  | { type: 'hello'; session: SessionInfo; participants: Participant[]; segments: Segment[] }
  | { type: 'session'; at_ms: number; state: SessionState; error?: string }
  | { type: 'participant'; at_ms: number; participant: Participant }
  | { type: 'speech'; at_ms: number; participant_id: string; speaking: boolean }
  | { type: 'segment'; at_ms: number; segment: Segment }
  | { type: 'face'; at_ms: number; participant_id: string; status: FaceStatus; confidence?: number; thumbnail?: string }
  | { type: 'notice'; at_ms: number; level: string; message: string }
  | { type: 'done'; at_ms: number; reason: string }
  | { type: 'ping'; at_ms: number }

export interface CreateSessionPayload {
  meeting_url: string
  title?: string | null
  duration_min?: number | null
  start_at?: string | null
  pipeline?: 'mock' | 'jazz'
  language?: 'auto' | 'ru' | 'en'
  password?: string | null
  speed?: number
  headless?: boolean
  model?: string
}
