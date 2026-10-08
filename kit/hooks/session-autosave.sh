#!/bin/bash
# session-autosave.sh — автосохранение контекста
# Хуки: SessionEnd, PreCompact

mkdir -p "$HOME/.claude"

# Читаем данные хука из stdin
HOOK_INPUT=$(cat - 2>/dev/null || echo "{}")
SESSION_ID=$(echo "$HOOK_INPUT" | jq -r '.session_id // ""' 2>/dev/null || echo "")
TRIGGER=$(echo "$HOOK_INPUT" | jq -r '.trigger // "session_end"' 2>/dev/null || echo "session_end")

HANDOFF="$HOME/.claude/handoff.md"
# Если ~/.claude синхронизируется между машинами, у каждой не-macOS машины свой файл: иначе конфликт синхронизации.
[ "$(uname)" = Darwin ] || HANDOFF="$HOME/.claude/handoff-$(hostname -s).md"
DATE=$(date "+%Y-%m-%d %H:%M")
DEVICE=$(hostname -s 2>/dev/null || echo "unknown")
PROJECT=$(basename "$PWD")
BRANCH=$(git branch --show-current 2>/dev/null || echo "не в git")
STATUS=$(git status --short 2>/dev/null | head -10)
LOG=$(git log --oneline -5 2>/dev/null)

{
  echo ""
  echo "---"
  echo "## Автосохранение [$TRIGGER] ($DATE) | $PROJECT | $DEVICE"
  echo "- Ветка: $BRANCH"
  echo "- Незакоммиченные:"
  echo "$STATUS"
  echo "- Последние коммиты:"
  echo "$LOG"
} >> "$HANDOFF"

# Ротация: без потолка файл дорастает до тысяч записей и сотен КБ.
# Оставляем последние 200 блоков, когда файл переваливает за 300 KB.
if [ -f "$HANDOFF" ] && [ "$(wc -c <"$HANDOFF")" -gt 307200 ]; then
  KEEP=$(grep -n '^## Автосохранение' "$HANDOFF" | tail -200 | head -1 | cut -d: -f1)
  if [ -n "$KEEP" ] && [ "$KEEP" -gt 2 ]; then
    { head -1 "$HANDOFF"; echo; tail -n "+$((KEEP - 1))" "$HANDOFF"; } > "$HANDOFF.tmp" \
      && mv "$HANDOFF.tmp" "$HANDOFF"
  fi
fi

# Хук PreCompact блокирует сжатие при ненулевом коде возврата — здесь этого быть не должно.
exit 0
