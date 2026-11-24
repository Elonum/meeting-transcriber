"""Mock pipeline: a scripted meeting used to develop and demo the whole stack.

It produces exactly the same protocol events as the real pipeline will, so the
UI, the WebSocket fan-out, the persistence layer and the exporters can be built
and tested before Jazz access is sorted out. ``speed`` scales every delay, which
keeps the end-to-end test under a couple of seconds.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from . import protocol as p
from .session import Emitter

PARTICIPANTS = [
    ("p-anna", "Анна Соколова", "Руководитель проекта"),
    ("p-dmitry", "Дмитрий Лебедев", "Backend"),
    ("p-irina", "Ирина Ким", "Продукт"),
]

AGENT_ID = "p-agent"

# (speaker_id, text, pause_after_ms)
SCRIPT: list[tuple[str, str, int]] = [
    ("p-anna", "Коллеги, начинаем. Задача — подключить транскрибацию к встречам в Jazz.", 350),
    ("p-dmitry", "Агент заходит по ссылке как обычный участник, поэтому доступ к API не обязателен.", 300),
    ("p-anna", "Хорошо. Что с задержкой распознавания?", 280),
    ("p-dmitry", "Сейчас около полутора секунд от конца фразы до финального текста.", 320),
    ("p-irina", "Для демонстрации этого достаточно, но в интерфейсе стоит показать промежуточный текст серым.", 300),
    ("p-anna", "Согласна. Дмитрий, а как определяется, кто говорит?", 280),
    ("p-dmitry", "Мы не угадываем спикера, а берём его из отдельной аудиодорожки участника.", 350),
    ("p-irina", "Тогда и лица можно привязать к тем же дорожкам.", 260),
    ("p-dmitry", "Именно. Кадр из видеопотока участника даёт лицо без отдельной базы.", 330),
    ("p-anna", "Отлично. Что осталось до конца недели?", 280),
    ("p-dmitry", "Экспорт в субтитры, журнал событий и подключение настоящего распознавания.", 300),
    ("p-irina", "И проверка на реальной встрече в Jazz.", 260),
    ("p-anna", "Тогда фиксируем: демо в пятницу, я приглашаю заказчика.", 300),
    ("p-dmitry", "Принято, готовлю стенд.", 0),
]

NOTICE_START = (
    "Демо-режим: транскрипт сформирован mock-пайплайном. "
    "События, протокол и экспорт — настоящие."
)


class MockPipeline:
    name = "mock"

    def __init__(self, meeting_url: str, *, speed: float = 1.0) -> None:
        self.meeting_url = meeting_url
        self.speed = max(0.05, speed)

    def _ms(self, value: float) -> int:
        return int(value / self.speed)

    async def _sleep(self, ms: float) -> None:
        if ms > 0:
            await asyncio.sleep(ms / 1000 / self.speed)

    async def run(self, emit: Emitter, deadline_ms: int | None) -> None:
        started = p.now_ms()

        def elapsed_ms() -> int:
            return p.now_ms() - started

        def expired() -> bool:
            return deadline_ms is not None and elapsed_ms() >= deadline_ms

        await emit(p.ev_notice(elapsed_ms(), NOTICE_START, "info"))

        # The agent itself, then people joining one by one.
        await emit(
            p.ev_participant(
                elapsed_ms(),
                p.new_participant(AGENT_ID, "Ассистент (бот)", role="bot", has_video=False),
            )
        )
        for index, (pid, name, _role) in enumerate(PARTICIPANTS):
            await self._sleep(400)
            if expired():
                return await emit(p.ev_done(elapsed_ms(), "deadline"))
            participant = p.new_participant(pid, name, color_index=index + 1)
            participant["joined_at_ms"] = elapsed_ms()
            await emit(p.ev_participant(elapsed_ms(), participant))
            # Face recognition is reported as a separate event, exactly like the
            # real pipeline will report it after the first video frame.
            await self._sleep(250)
            await emit(
                p.ev_face(
                    elapsed_ms(),
                    pid,
                    "detected",
                    confidence=round(random.uniform(0.88, 0.99), 3),
                )
            )

        await self._sleep(600)

        for line_index, (speaker, text, pause_ms) in enumerate(SCRIPT):
            if expired():
                await emit(p.ev_done(elapsed_ms(), "deadline"))
                return

            segment_id = f"{speaker}-{line_index}"
            line_start = elapsed_ms()
            await emit(p.ev_speech(elapsed_ms(), speaker, True))

            words = text.split(" ")
            shown: list[str] = []
            for word_index, word in enumerate(words):
                shown.append(word)
                await emit(
                    p.ev_segment(
                        elapsed_ms(),
                        {
                            "id": segment_id,
                            "participant_id": speaker,
                            "start_ms": line_start,
                            "end_ms": elapsed_ms(),
                            "text": " ".join(shown),
                            "final": False,
                            "confidence": None,
                            "revision": word_index + 1,
                        },
                    )
                )
                await self._sleep(230 + random.uniform(-40, 60))

            await emit(
                p.ev_segment(
                    elapsed_ms(),
                    {
                        "id": segment_id,
                        "participant_id": speaker,
                        "start_ms": line_start,
                        "end_ms": elapsed_ms(),
                        "text": text,
                        "final": True,
                        "confidence": round(random.uniform(0.93, 0.99), 3),
                        "revision": len(words),
                    },
                )
            )
            await emit(p.ev_speech(elapsed_ms(), speaker, False))
            await self._sleep(pause_ms)

        await emit(
            p.ev_notice(elapsed_ms(), "Встреча завершена. Транскрипт доступен для экспорта.", "info")
        )


def build_mock(session_id: str, meeting_url: str, *, speed: float = 1.0) -> Any:
    del session_id
    return MockPipeline(meeting_url, speed=speed)
