#!/bin/bash
# agents-md-drift.sh — PostToolUse(Edit|Write|MultiEdit)
#
# Ловит момент, когда правят CLAUDE.md, а рядом лежит AGENTS.md обычным файлом.
# С 28.09.2026 пары ведём раздельно: CLAUDE.md — основной и единственный источник
# фактов, AGENTS.md — короткий адаптер под Codex (свои практики, жёсткие правила
# текстом, потому что хуки Claude на Codex не действуют). Копии фактов в двух
# файлах уже расходились до неверных утверждений (в реальных проектах), поэтому
# хук напоминает сверить адаптер. Только подсказка, ничего не блокирует.

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | python3 -c \
  "import sys,json;print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))" 2>/dev/null)

[ -z "$FILE" ] && exit 0
[ "$(basename "$FILE")" = "CLAUDE.md" ] || exit 0

SIBLING="$(dirname "$FILE")/AGENTS.md"
[ -e "$SIBLING" ] || exit 0   # нет пары — нечего сторожить
[ -L "$SIBLING" ] && exit 0   # симлинк — структурно синхронен

MSG="Рядом с $FILE лежит AGENTS.md — адаптер для Codex. Поменял жёсткое правило, команду проверки или факт, который там повторён, — поправь и AGENTS.md. Новые факты писать только в CLAUDE.md."
python3 -c 'import json,sys;print(json.dumps({"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":sys.argv[1]}},ensure_ascii=False))' "$MSG"
exit 0
