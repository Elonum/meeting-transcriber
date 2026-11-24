export function formatMs(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  const pad = (value: number) => String(value).padStart(2, '0')
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${pad(minutes)}:${pad(seconds)}`
}

export function formatClock(ms: number): string {
  return new Date(ms).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/** "14:30 сегодня" / "12.03 09:00" — short enough for a list row. */
export function formatWhen(ms: number): string {
  const moment = new Date(ms)
  const today = new Date()
  const sameDay = moment.toDateString() === today.toDateString()
  const time = moment.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
  if (sameDay) return `${time} сегодня`
  return `${moment.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' })} ${time}`
}

/** Value for <input type="datetime-local"> in local time. */
export function toLocalInputValue(ms: number): string {
  const moment = new Date(ms - new Date(ms).getTimezoneOffset() * 60_000)
  return moment.toISOString().slice(0, 16)
}

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).slice(0, 2)
  return parts.map((part) => part[0]?.toUpperCase() ?? '').join('') || '?'
}

const STATE_LABELS: Record<string, string> = {
  scheduled: 'Запланировано',
  connecting: 'Подключение',
  live: 'В эфире',
  stopping: 'Остановка',
  finished: 'Завершено',
  failed: 'Ошибка',
}

export function stateLabel(state: string): string {
  return STATE_LABELS[state] ?? state
}
