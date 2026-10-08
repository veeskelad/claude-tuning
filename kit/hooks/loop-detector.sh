#!/bin/bash
# loop-detector.sh — замечает, что агент повторяет одно и то же действие (PostToolUse, matcher: *).
# Пять одинаковых вызовов подряд (тот же инструмент с теми же аргументами) — подсказка агенту
# сменить подход. Ничего не блокирует.
# Вход — JSON хука на stdin (Claude Code не передаёт TOOL_NAME через окружение; прежняя версия
# читала переменные окружения и молча не срабатывала никогда).

INPUT=$(cat)
SID=$(printf '%s' "$INPUT" | jq -r '.session_id // "default"' 2>/dev/null)
KEY=$(printf '%s' "$INPUT" | jq -c '[.tool_name, .tool_input]' 2>/dev/null)
[ -z "$KEY" ] || [ "$KEY" = "null" ] && exit 0

STATE="${TMPDIR:-/tmp}/claude-loop-${SID//[^A-Za-z0-9_-]/}"
HASH=$(printf '%s' "$KEY" | { shasum -a 256 2>/dev/null || sha256sum; } | cut -c1-16)
read -r PREV COUNT < "$STATE" 2>/dev/null || { PREV=""; COUNT=0; }

if [ "$HASH" = "$PREV" ]; then COUNT=$((COUNT + 1)); else COUNT=1; fi
printf '%s %s\n' "$HASH" "$COUNT" > "$STATE"

if [ "$COUNT" -ge 5 ]; then
  printf '%s %s\n' "$HASH" 0 > "$STATE"
  jq -cn --arg c "Одно и то же действие (тот же инструмент, те же аргументы) повторено $COUNT раз подряд. Остановись: назови предположение, которое может быть неверным, и попробуй другой подход." \
    '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$c}}'
fi
exit 0
