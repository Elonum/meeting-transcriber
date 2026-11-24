import type { CSSProperties } from 'react'
import type { Participant } from '../types'
import { initials } from '../format'

interface SceneProps {
  participants: Participant[]
}

export function Scene({ participants }: SceneProps) {
  const speakers = participants.filter((p) => p.speaking).length

  return (
    <section className="scene" aria-label="Участники встречи">
      <div className="scene__head">
        <h2>Сцена</h2>
        <span className="scene__hint">
          {participants.length === 0
            ? 'ждём подключения агента'
            : `${participants.length} участников · говорят сейчас: ${speakers}`}
        </span>
      </div>

      {participants.length === 0 ? (
        <div className="scene__empty">
          <span className="dot dot--live" /> Агент подключается к встрече…
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

function ParticipantTile({ participant }: { participant: Participant }) {
  const confidence = participant.face_confidence
  const accent = { '--accent': participant.color } as CSSProperties

  return (
    <article
      className={`tile${participant.speaking ? ' tile--speaking' : ''}`}
      style={accent}
      title={`${participant.display_name} · ${participant.role === 'bot' ? 'бот' : 'участник'}`}
    >
      <div className="tile__avatar">
        {participant.thumbnail ? (
          <img src={participant.thumbnail} alt={`Лицо: ${participant.display_name}`} />
        ) : (
          <span className="tile__initials">{initials(participant.display_name)}</span>
        )}

        {participant.face_status === 'detected' && (
          <span className="tile__face" title="Лицо найдено в видеопотоке">
            ◉ {confidence ? `${Math.round(confidence * 100)}%` : 'лицо'}
          </span>
        )}
        {participant.face_status === 'frame' && (
          <span className="tile__face tile__face--none" title="Кадр получен, детектор лиц не подключён">
            кадр
          </span>
        )}
        {participant.face_status === 'none' && (
          <span className="tile__face tile__face--none" title="Камера выключена или лица нет в кадре">
            камера off
          </span>
        )}
        {!participant.has_video && participant.role !== 'bot' && (
          <span className="tile__face tile__face--none" title="Участник передаёт только звук">
            без видео
          </span>
        )}
        {participant.role === 'bot' && <span className="tile__bot">бот</span>}
      </div>

      <div className="tile__meta">
        <strong className="tile__name">{participant.display_name}</strong>
        <span className="tile__sub">
          {participant.role === 'bot' ? 'слушает встречу' : 'участник'}
        </span>
      </div>

      {participant.speaking && (
        <div className="tile__bars" aria-label="говорит">
          <i />
          <i />
          <i />
          <i />
        </div>
      )}
    </article>
  )
}
