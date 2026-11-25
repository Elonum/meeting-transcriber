import type { CSSProperties } from 'react'
import type { Participant } from '../types'
import { initials } from '../format'

interface SceneProps {
  participants: Participant[]
}

/** The meeting "stage": one tile per participant, the speaker is highlighted. */
export function Scene({ participants }: SceneProps) {
  const humans = participants.filter((participant) => participant.role !== 'bot')
  const speakers = participants.filter((participant) => participant.speaking).length

  return (
    <section className="scene" aria-label="Участники встречи">
      <div className="scene__head">
        <h2>Сцена</h2>
        <span className="scene__hint">
          {humans.length === 0
            ? 'агент подключается к встрече'
            : `${humans.length} ${plural(humans.length)}${speakers ? ` · говорит ${speakers}` : ''}`}
        </span>
      </div>

      {participants.length === 0 ? (
        <div className="scene__empty">
          <span className="dot dot--live" /> Ждём, когда агент войдёт в комнату…
        </div>
      ) : (
        <div className="tiles">
          {participants.map((participant) => (
            <ParticipantTile key={participant.id} participant={participant} />
          ))}
        </div>
      )}
    </section>
  )
}

function plural(count: number): string {
  const mod10 = count % 10
  const mod100 = count % 100
  if (mod10 === 1 && mod100 !== 11) return 'участник'
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'участника'
  return 'участников'
}

function ParticipantTile({ participant }: { participant: Participant }) {
  const confidence = participant.face_confidence
  const accent = { '--accent': participant.color } as CSSProperties
  const isBot = participant.role === 'bot'

  return (
    <article
      className={`tile${participant.speaking ? ' tile--speaking' : ''}${isBot ? ' tile--bot' : ''}`}
      style={accent}
      title={participant.display_name}
    >
      <div className="tile__avatar">
        {participant.thumbnail ? (
          <img src={participant.thumbnail} alt={participant.display_name} />
        ) : (
          <span className="tile__initials">{initials(participant.display_name)}</span>
        )}

        {participant.face_status === 'detected' && (
          <span className="tile__badge" title="Лицо найдено в видеопотоке">
            лицо{confidence ? ` ${Math.round(confidence * 100)}%` : ''}
          </span>
        )}
        {participant.face_status === 'frame' && (
          <span className="tile__badge tile__badge--muted" title="Кадр получен, лицо не распознано">
            кадр
          </span>
        )}
        {participant.face_status === 'none' && (
          <span className="tile__badge tile__badge--muted" title="Камера выключена">
            без камеры
          </span>
        )}

        {participant.speaking && (
          <span className="tile__bars" aria-label="говорит">
            <i />
            <i />
            <i />
            <i />
          </span>
        )}
      </div>

      <div className="tile__meta">
        <strong className="tile__name">{participant.display_name}</strong>
        <span className="tile__sub">{isBot ? 'ассистент' : participant.speaking ? 'говорит' : 'слушает'}</span>
      </div>
    </article>
  )
}
