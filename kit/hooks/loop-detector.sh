#!/bin/bash
# loop-detector.sh — замечает, что агент ходит по кругу (PostToolUse, matcher: *).
# Ловит не только пять одинаковых вызовов подряд, но и цикл: последние 2–4 шага повторились
# три раза подряд (правка → проверка → откат → проверка → та же правка…). Агенты редко
# повторяют ровно одно действие, зато часто крутятся между двумя-тремя попытками.
# Ничего не блокирует: подсказка уходит агенту в контекст.
# Вход — JSON хука на stdin (Claude Code не передаёт TOOL_NAME через окружение).

INPUT=$(cat)
SID=$(printf '%s' "$INPUT" | jq -r '.session_id // "default"' 2>/dev/null)
KEY=$(printf '%s' "$INPUT" | jq -c '[.tool_name, .tool_input]' 2>/dev/null)
[ -z "$KEY" ] || [ "$KEY" = "null" ] && exit 0

STATE="${TMPDIR:-/tmp}/claude-loop-${SID//[^A-Za-z0-9_-]/}"
HASH=$(printf '%s' "$KEY" | { shasum -a 256 2>/dev/null || sha256sum; } | cut -c1-16)
# Старый формат («хеш счётчик» в одной строке) превращается в окно из одного шага.
{ cat "$STATE" 2>/dev/null | awk '{print $1}'; echo "$HASH"; } | tail -n 12 > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"

# Самый короткий цикл в хвосте окна: длина k, повторов r.
FOUND=$(awk '
  { h[NR] = $1 }
  END {
    n = NR
    for (k = 1; k <= 4; k++) {
      need = (k == 1) ? 5 : 3
      if (n < k * need) continue
      ok = 1
      for (i = n - k * need + 1; i <= n - k; i++) if (h[i] != h[i + k]) { ok = 0; break }
      if (ok) { print k, need; exit }
    }
  }' "$STATE")

if [ -n "$FOUND" ]; then
  read -r K R <<< "$FOUND"
  : > "$STATE"
  if [ "$K" = 1 ]; then
    MSG="Одно и то же действие (тот же инструмент, те же аргументы) повторено $R раз подряд."
  else
    MSG="Последние шаги идут по кругу: один и тот же цикл из $K действий повторился $R раза."
  fi
  jq -cn --arg c "$MSG Остановись: назови предположение, которое может быть неверным, и попробуй другой подход или спроси человека." \
    '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$c}}'
fi
exit 0
