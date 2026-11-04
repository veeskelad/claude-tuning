# Хуки claude-tuning: как устроены стражи и как их проверять

Справочник для того, кто правит хуки. В сессию он не грузится: правила, которые нужны модели каждый раз, лежат в
`~/.claude/rules/`. Простыми словами для человека — README набора, раздел «Что и чем сделано».

## Раскладка

| Инструмент | Хук | Что делает |
|---|---|---|
| Bash | `bypass-ask-to-deny.sh 'safety-guard.sh'` | необратимое (`rm -rf` по корню и домашней папке, в том числе не первым аргументом, `drop database`, `terraform destroy`, `git reset --hard origin/…`, `npm publish`…) — отказ; рискованное (`git reset --hard`, `git push --force`, запуск скрипта, который удаляет файлы) — вопрос; остальное — пропуск. Свой тестовый контейнер (метка `claude.agent.session=<id>`) агент убирает сам, чужой — нет |
| Bash | `secret-guard-bash.py` | не запрещает, а подставляет безопасный вид команды: `cat .env` и `env` — только имена, одно значение — метка `‹secret ИМЯ sha256:… len:…›`, дампы процессов и конфигов — строгая маска. Отказ — где безопасного вида нет: приватный ключ, запись значения в документ или файл под git |
| Write, Edit | `secret-guard-bash.py --write-tool` | настоящий ключ или PEM в документ, память, код — отказ (кроме `.env*`, `tests/`, `fixtures/`) |
| Read | `bypass-ask-to-deny.sh 'guard-secret-read.sh'` | Read маскировать нельзя, поэтому `.env`, ключи, `credentials*` — отказ; точечно разрешить — `CLAUDE_SECRET_READ_ALLOW` |
| Write | `home-clutter.sh` | новый файл прямо в домашней папке — отказ с подсказкой, куда положить |
| Bash | `unless-bypass.sh 'dippy-script.py'` | dippy одобряет то, что только читает; обёртка показывает ему текст запускаемого скрипта и подставляет простые переменные |
| вывод Bash, Read | `guard-secret-in-output.sh` | тревога, если значение всё же прошло: агент обязан сказать, что и где утекло |
| ExitPlanMode | агент-проверяющий | до показа сверяет план с кодом; блокирует только дефект с конкретной причиной |
| PermissionRequest | `plan-auto-allow.sh` | в режиме плана окно разрешения на чтение и поиск одобряется само |
| PostToolUse `*` | `loop-detector.sh` | пять одинаковых шагов подряд или круг из 2–4 шагов три раза — подсказка сменить подход |
| SessionStart | `open-tasks.sh`, `compact-context.py`, `tuning-update-check.sh` | открытые чек-листы; после сжатия — задача этой сессии и правленые файлы; раз в сутки — нет ли новой версии набора |
| SessionEnd, PreCompact | `session-autosave.sh` | машинная запись о сессии в `~/.claude/handoff.md` |
| направление «программирование» | `guard-hard-rules.py`, `repo-freshness.sh`, `agents-md-drift.sh` | без `--no-verify`, ветки и worktree от свежего `origin`, без правок основной копии на main; предупреждение об отставших клонах; напоминание, что CLAUDE.md и AGENTS.md разошлись |
| направление «задачи по расписанию» | `autonomy-pause-guard.sh` | в автономном запуске: пауза останавливает всё, push, мерж, отправка и удаление вне папки задания — отказ |

## Режимы bypass и плана

Хуки PreToolUse работают и в этих режимах, но спросить человека там нельзя: вопрос стража превращается в отказ без
объяснения. Поэтому стражи с вопросом обёрнуты `bypass-ask-to-deny.sh`: в bypass и плане «ask» становится отказом с
причиной, и агент ищет безопасный путь. `unless-bypass.sh` выключает в bypass то, что там только шумит (dippy,
напоминания). На SessionStart, PreCompact и SessionEnd обёртка хук не пропускает — он работает всегда.

Новый страж, который должен работать в любом режиме, подключать без `unless-bypass.sh`.

## Один переписывающий хук на Bash

Хуки одного события идут параллельно, и при нескольких `updatedInput` побеждает тот, кто закончит последним
(документация Claude Code, hooks-guide: «non-deterministic»). Поэтому команду Bash переписывает только
`secret-guard-bash.py`; новую логику подстановки — в его `route()`. Хуки, которые только отказывают
(`guard-hard-rules.py`, `safety-guard.sh`), этому не мешают.

## Скрипты, которые удаляют файлы

Запуск скрипта, который удаляет файлы, — вопрос (в bypass — отказ). Без вопроса проходит:

- тест на своём временном каталоге: bash-скрипт в `tests/` или с `test` в имени сам заводит `mktemp -d`, каждый `rm -r`
  удаляет путь от своей переменной в кавычках без `..` и масок; образцы — `tests/run-script-guard.sh`;
- просмотренный человеком скрипт: его хеш в `~/.claude/hooks/reviewed-scripts.sha256` (строки как у `shasum -a 256`).
  Правка файла меняет хеш, и вопрос возвращается. Список пополняет человек после просмотра, агент сам туда не пишет.

## Ключи: перенести и сверить, не читая

```
secret-value.py names <file>                      имена ключей + есть/пусто
secret-value.py fp    <file> <key>                sha256[:12] — сверить две стороны
secret-value.py len   <file> <key>                длина значения
secret-value.py copy  <file> <key> <dest> [VAR]   значение → VAR=… в файл (chmod 600)
secret-value.py push  <file> <key> <host> <dest> [VAR]   то же, но на сервер по ssh
secret-value.py exec  <file> <key> [VAR] -- <команда…>   значение только в окружение команды, вывод — через маску
```

Подтверждение «да» от утечки не спасает: подтверждают намерение, а утекает результат промаха фильтра (например,
`grep -A6` захватил соседние строки). Поэтому значения не выводятся вовсе, а сверяются по отпечатку.

Раз в неделю–месяц: `scan-transcripts-for-secrets.sh -d 30` печатает только метки значений, осевших в транскриптах.

## Проверка после любой правки стражей

Из папки набора:

- `env -u CLAUDE_GUARD_SKIP_SECRETS python3 tests/run-guard.py kit/hooks/safety-guard.sh tests/safety-guard.cases.tsv`
- `python3 tests/leak-replay.py --home <каталог установки>` — классы утечек на фейковых значениях
- `python3 kit/hooks/secret-mask.py --selftest`
- `bash tests/adapters.test.sh`, `bash tests/run-script-guard.sh`
- с направлениями: `python3 tests/guard-hard-rules.test.py`, `python3 tests/dippy-script.test.py`, `bash tests/autonomy.test.sh`

Новый паттерн стража — сначала строка в таблице `tests/safety-guard.cases.tsv`, потом правка.
