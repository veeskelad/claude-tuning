#!/bin/bash
# home-clutter.sh — не даёт агенту создавать новые файлы прямо в домашней папке (PreToolUse, matcher: Write).
# Зачем: за пару недель агент обрастает мусором — дампы, черновики, скрипты на раз лежат в ~ вперемешку,
# и уже непонятно, что актуально. Временное — в scratchpad сессии, результат — в папку темы или проекта.
# Существующие файлы в ~ (.zshrc, .gitconfig…) править можно: блок только на создание нового.

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
[ -z "$FILE" ] && exit 0
case "$FILE" in "~/"*) FILE="$HOME/${FILE#\~/}" ;; esac
DIR=$(dirname "$FILE")
[ "$DIR" = "$HOME" ] || exit 0
[ -e "$FILE" ] && exit 0

jq -cn --arg r "Новый файл прямо в домашней папке не создаётся: здесь он станет мусором. Временное — в scratchpad сессии, результат — в ~/projects/<тема>/ или в папку проекта." \
  '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
exit 0
