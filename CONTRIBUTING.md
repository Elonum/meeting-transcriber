# Как писать коммиты

Один формат на весь репозиторий — [Conventional Commits](https://www.conventionalcommits.org/)
с небольшими уточнениями. Сообщение читают люди и инструменты: по типу и scope видно, что затронуто,
по телу — почему, а не только что.

## Формат

```
<тип>(<область>): <краткое описание>

<тело: что и зачем, абзацами по 72 символа>

<футер: BREAKING CHANGE или ссылка на задачу, если нужно>
```

**Первая строка**

- английский, повелительное наклонение: `add`, `fix`, `remove`, а не `added` и не `adds`;
- строчная буква после двоеточия, без точки в конце;
- до 72 символов, по возможности до 50;
- без « - » внутри: описание продолжается словами, а не дефисом.

**Тело** (необязательно, но почти всегда нужно)

- отвечает на «зачем»: какую проблему решает и почему именно так;
- абзацы по 72 символа, между абзацами пустая строка;
- списки — только через `-`, без вложенности;
- если в коммите несколько несвязанных изменений, это повод разбить его.

**Футер** — только `BREAKING CHANGE:`, `Refs:` или номер задачи. Трейлеров
`Co-Authored-By` и подписей генератора в этом репозитории нет: автор коммита — владелец проекта.

## Типы

| Тип | Когда |
|---|---|
| `feat` | новая функциональность для пользователя или API |
| `fix` | исправление ошибки |
| `docs` | только документация и комментарии |
| `test` | только тесты |
| `refactor` | изменение кода без изменения поведения |
| `perf` | производительность |
| `build` | зависимости, Dockerfile, сборка |
| `ci` | workflows |
| `chore` | рутина: лицензия, скриншоты, конфиги |
| `style` | форматирование, пробелы, опечатки |

## Области

`agent` (браузерный агент), `asr`, `faces`, `api`, `ui`, `history`, `recording`, `repo`, `ci`.
Scope необязателен, но если коммит целиком в одной подсистеме — он помогает.

## Примеры

```
feat(agent): join a live SaluteJazz meeting end to end

Verified against a real room: the agent joins as a guest, unmutes, receives one
audio track per participant and produces an attributed transcript.

Prefer Chrome over Edge, which is the difference between the lobby and an
unsupported-browser screen.
```

```
fix(agent): make the guest join work for a real meeting link

Four obstacles, each with its own cause:

- the client treats an invite link with a password parameter as a deep link for
  its desktop app;
- it reports an unsupported browser unless the automation switch is dropped.
```

Плохо:

```
Update README.md
fix
feat: add themes, responsive layout, recording and history tools and also fix bug with docker
```

## Проверка

- Хук `.githooks/commit-msg` не даёт сделать коммит с неправильной первой строкой. Включить один раз:

  ```powershell
  git config core.hooksPath .githooks
  ```

- CI (`.github/workflows/ci.yml`) проверяет первые строки всех коммитов тем же регулярным выражением,
  так что формат не разъедется даже при коммите из другой среды.
