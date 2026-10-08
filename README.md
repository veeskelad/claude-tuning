# claude-tuning

Скилл для Claude Code, который за один проход ставит рабочую систему вокруг модели: память между сессиями, экономию токенов, защиту от удаления и утечки ключей, уборку веток и базовый набор скиллов.

*English: a Claude Code skill that installs a working setup around the model in one pass — cross-session handoff and task checklists, a lean CLAUDE.md, safety hooks against destructive commands and secret leaks, worktree hygiene and a core skill set. Docs are in Russian.*

## Зачем

Claude Code из коробки и после настройки — одна и та же модель. Без системы вокруг неё у всех одни и те же боли:

- каждое утро объяснять проект заново, после сжатия контекста он забывает задачу;
- лимиты кончаются к обеду: длинный CLAUDE.md и растущие заметки читаются в каждой сессии;
- правило в CLAUDE.md он прочитает, согласится и всё равно нарушит — по документации Claude Code это контекст, а не запрет;
- одна команда — и нет домашней папки, базы или незакоммиченной работы;
- ключи из `.env` оказываются прямо в чате.

Запреты держат хуки, память — файлы и команды передачи дел. Это и ставит скилл.

## Что внутри

| Боль | Что ставится |
|---|---|
| Забывает между сессиями | чек-лист задачи `.tasks/<slug>.md` и хук, показывающий открытые задачи на старте; возврат задачи после сжатия контекста; скиллы `session-handoff`, `endday`, `morning` с общим `~/.claude/global-handoff.md` |
| Токены | шаблон CLAUDE.md-карты на 40–60 строк; сжатие handoff (целиком читаются только последние 3 дня); автокомпакт на 60 % |
| Удаление | `safety-guard.sh`: блок необратимого (`rm -rf` по корню и домашней папке, `drop database`, `terraform destroy`, `git reset --hard origin/…`, `npm publish`…), вопрос на рискованное (`git reset --hard`, `git push --force`, `git clean -f`), пропуск остального |
| Ключи | `guard-secret-read.sh` и `safety-guard.sh`: агент не читает `.env`, ssh-ключи, `*.pem` и не печатает окружение целиком; шаблон `~/.claude/.env` |
| Ходит по кругу | правило остановок в CLAUDE.md, `loop-detector.sh` |
| Ветки и копии репо | `worktree-cleanup`, `repo-freshness.sh`, worktree от свежего `origin/main` |
| Качество | `systematic-debugging` (причина до фикса), `review-triage` (замечания ревью классами), `memory-consolidate`, `session-search` |

## Установка

Нужны: Claude Code, `bash`, `python3`, `jq`, `git`. Проверено на macOS; Linux должен работать, Windows без WSL — нет.

1. Положить папку скилла в `~/.claude/skills/claude-tuning/`.
2. В Claude Code написать «тюнинг» или «настрой claude code». Скилл проведёт аудит, покажет сухой прогон установщика и спросит разрешение.

Или вручную:

```bash
python3 ~/.claude/skills/claude-tuning/scripts/install-kit.py            # что будет сделано, ничего не меняет
python3 ~/.claude/skills/claude-tuning/scripts/install-kit.py --apply    # поставить
python3 ~/.claude/skills/claude-tuning/scripts/install-kit.py --check    # проверить
```

Отдельно ставятся `find-skills` и `skill-creator` из репозиториев их авторов — команды печатает установщик.

## Что установщик меняет и чего не трогает

- Копирует скиллы в `~/.agents/skills/` и делает на них ссылки из `~/.claude/skills/` (и `~/.codex/skills/`, если есть Codex); хуки — в `~/.claude/hooks/`.
- В `~/.claude/settings.json` добавляет только недостающее: хуки, `env`, `worktree.baseRef`, пустую `attribution`. Перед правкой — копия `settings.json.bak-ГГГГММДД`.
- **Ничего не перезаписывает.** Файл с тем же именем и другим содержимым остаётся как есть и попадает в отчёт «своя версия».
- `CLAUDE.md` создаёт только если его нет. Существующий CLAUDE.md скилл переписывает вместе с вами по шаблону, а не копией поверх.
- Не читает и не выводит значения ключей.

## Удаление

```bash
python3 ~/.claude/skills/claude-tuning/scripts/install-kit.py --uninstall           # что будет снято
python3 ~/.claude/skills/claude-tuning/scripts/install-kit.py --uninstall --apply
```

Снимаются только файлы, совпадающие с набором, и только хуки набора. Ваши правки, чужие хуки, `env`, `CLAUDE.md`, handoff и чек-листы остаются. Полный откат настроек — копия `~/.claude/settings.json.bak-ГГГГММДД`.

## Своё поверх набора

Не правьте набор под себя: обновления будут конфликтовать с вашими правками. Личное (свои серверы, пути, хуки, скиллы) держите отдельной папкой той же формы и подключайте поверх:

```text
my-claude/
  skills/<имя>/SKILL.md
  hooks/<скрипт>
  bin/<скрипт>
  settings.snippet.json     # {"hooks": {...}, "env": {...}} — как в kit/settings.snippet.json
```

```bash
python3 ~/.claude/skills/claude-tuning/scripts/install-kit.py --extra ~/my-claude            # что будет сделано
python3 ~/.claude/skills/claude-tuning/scripts/install-kit.py --extra ~/my-claude --apply
```

`--extra` можно повторять. `--check` и `--uninstall` с тем же `--extra` проверяют и снимают и вашу папку.

## Настройка

| Переменная | Что делает | По умолчанию |
|---|---|---|
| `CLAUDE_WORK_ROOTS` | папки с проектами для `open-tasks.sh --all` и `repo-freshness.sh`, через двоеточие | `~/Work:~/Projects:~/code` |
| `CLAUDE_SECRET_READ_ALLOW` | файлы с ключами, которые агенту можно читать, через двоеточие | пусто |
| `CLAUDE_FRESHNESS_OFF` | `1` — не делать `git fetch` на старте сессии | выключено |

## Лицензия

MIT, см. `LICENSE`. Скилл `systematic-debugging` — форк [obra/superpowers](https://github.com/obra/superpowers) под MIT, см. `THIRD_PARTY_NOTICES.md`.
