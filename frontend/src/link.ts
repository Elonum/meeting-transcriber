/**
 * SaluteJazz gives the organiser three things:
 *
 *   Адрес видеовстречи: https://salutejazz.ru/calls/xxxxxx?psw=<зашифрованный пароль>
 *   Код видеовстречи:   xxxxxx@salutejazz.ru
 *   Пароль:             a1b2c3d4
 *
 * The password usually travels inside the link (encrypted as `psw`), so the form
 * only needs the address. The code is derived, not typed.
 */

export interface MeetingLink {
  /** Six-character room code, e.g. "abc123". */
  code: string | null
  /** The link already carries ?psw=, so no password has to be entered. */
  hasPassword: boolean
  /** Looks like a SaluteJazz/Jazz meeting link at all. */
  recognized: boolean
}

const ROOM = /\/calls\/([a-z0-9]{6})(?:$|[/?#])/i

export function parseMeetingLink(raw: string): MeetingLink {
  const value = raw.trim()
  if (!value) return { code: null, hasPassword: false, recognized: false }
  try {
    const url = new URL(value)
    const match = url.pathname.match(ROOM)
    return {
      code: match ? match[1].toLowerCase() : null,
      hasPassword: url.searchParams.has('psw'),
      recognized: Boolean(match),
    }
  } catch {
    return { code: null, hasPassword: false, recognized: false }
  }
}

/** Short, human label for a room: code when known, host otherwise. */
export function roomLabel(meetingUrl: string): string {
  const parsed = parseMeetingLink(meetingUrl)
  if (parsed.code) return parsed.code
  try {
    return new URL(meetingUrl).hostname.replace(/^www\./, '')
  } catch {
    return 'встреча'
  }
}
