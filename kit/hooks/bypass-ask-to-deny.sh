#!/usr/bin/env bash
# Обёртка для стражей, которые должны работать в любом режиме, включая bypassPermissions и план.
#
#   bypass-ask-to-deny.sh '<команда стража>'
#
# Проблема: страж отвечает «ask» (спросить человека), а в bypass и в плане спросить нельзя — окно
# не появится, и ответ либо пропадёт, либо вызов пройдёт без вопроса. Обёртка запускает стража как
# есть и, если он ответил «ask», а режим bypass или план, превращает «ask» в «deny» с пояснением:
# агент видит отказ и ищет другой способ (например, просит человека выполнить команду самому).
# «deny» и молчание («allow») стража не трогаются никогда. В остальных режимах страж работает как раньше.
#
# Подключение (пример для safety-guard):
#   bash "$HOME/.claude/hooks/bypass-ask-to-deny.sh" 'bash "$HOME/.claude/hooks/safety-guard.sh"'
set -uo pipefail

[ $# -ge 1 ] || { echo "usage: bypass-ask-to-deny.sh '<команда стража>'" >&2; exit 0; }

input=$(cat)
out=$(bash -c "$1" <<<"$input")
code=$?

pm=$(jq -r '.permission_mode // empty' <<<"$input" 2>/dev/null)
ask=$(jq -r '.hookSpecificOutput.permissionDecision // empty' <<<"$out" 2>/dev/null)

if { [ "$pm" != "bypassPermissions" ] && [ "$pm" != "plan" ]; } || [ "$ask" != "ask" ]; then
  [ -n "$out" ] && printf '%s\n' "$out"
  exit "$code"
fi

jq -c '.hookSpecificOutput.permissionDecision = "deny"
  | .hookSpecificOutput.permissionDecisionReason = ((.hookSpecificOutput.permissionDecisionReason // "") + " Спросить нельзя (bypass или план) — нужен другой способ.")' <<<"$out"
exit 0
