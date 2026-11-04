#!/usr/bin/env bash
# Проверка обёрток режимов: unless-bypass.sh, bypass-ask-to-deny.sh, plan-auto-allow.sh.
# Запуск: bash tests/adapters.test.sh. Ничего не пишет вне временной папки.
set -uo pipefail
H="$(cd "$(dirname "$0")/.." && pwd)/kit/hooks"
T=$(mktemp -d)
ok=0; bad=0
check() { # имя ожидаемое полученное
  if [ "$2" = "$3" ]; then ok=$((ok+1)); else bad=$((bad+1)); echo "ПРОМАХ $1: ждали [$2], получили [$3]"; fi
}

# Фиктивные стражи: один всегда спрашивает, другой отказывает, третий молчит
cat > "$T/ask.sh" <<'G'
#!/usr/bin/env bash
cat >/dev/null
echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"проверь"}}'
G
cat > "$T/deny.sh" <<'G'
#!/usr/bin/env bash
cat >/dev/null
echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"нельзя"}}'
G
cat > "$T/quiet.sh" <<'G'
#!/usr/bin/env bash
cat >/dev/null
G
cat > "$T/marker.sh" <<G
#!/usr/bin/env bash
cat >/dev/null
echo ran >> "$T/marker"
G

dec() { jq -r '.hookSpecificOutput.permissionDecision // "—"' 2>/dev/null; }
mk() { printf '{"permission_mode":"%s","tool_name":"Bash","tool_input":{"command":"ls"}}' "$1"; }

# bypass-ask-to-deny: ask → deny в bypass и плане, ask в остальных режимах, deny и тишина не меняются
for m in bypassPermissions plan; do
  check "ask→deny ($m)" deny "$(mk $m | bash "$H/bypass-ask-to-deny.sh" "bash $T/ask.sh" | dec)"
done
for m in default acceptEdits auto; do
  check "ask остаётся ($m)" ask "$(mk $m | bash "$H/bypass-ask-to-deny.sh" "bash $T/ask.sh" | dec)"
done
check "deny остаётся (bypass)" deny "$(mk bypassPermissions | bash "$H/bypass-ask-to-deny.sh" "bash $T/deny.sh" | dec)"
check "тишина (bypass)" "" "$(mk bypassPermissions | bash "$H/bypass-ask-to-deny.sh" "bash $T/quiet.sh")"
check "пояснение добавлено" 1 "$(mk plan | bash "$H/bypass-ask-to-deny.sh" "bash $T/ask.sh" | jq -r '.hookSpecificOutput.permissionDecisionReason' | grep -c 'Спросить нельзя')"
check "без аргумента не падает" 0 "$(mk plan | bash "$H/bypass-ask-to-deny.sh" >/dev/null 2>&1; echo $?)"

# unless-bypass: в bypass хук не запускается, в остальных — запускается и получает stdin
mk bypassPermissions | bash "$H/unless-bypass.sh" "bash $T/marker.sh"
check "bypass: не запущен" no "$([ -e "$T/marker" ] && echo yes || echo no)"
for m in default plan acceptEdits; do
  rm -f "$T/marker"
  mk $m | bash "$H/unless-bypass.sh" "bash $T/marker.sh"
  check "$m: запущен" yes "$([ -e "$T/marker" ] && echo yes || echo no)"
done
check "stdin передан дальше" ls "$(mk default | bash "$H/unless-bypass.sh" "jq -r .tool_input.command")"

# plan-auto-allow: только план и только безопасные инструменты
pa() { printf '{"permission_mode":"%s","tool_name":"%s"}' "$1" "$2" | bash "$H/plan-auto-allow.sh" | jq -r '.hookSpecificOutput.decision.behavior // "—"'; }
for t in Bash Read Grep Glob WebFetch Skill Agent; do check "plan/$t" allow "$(pa plan $t)"; done
for t in Edit Write mcp__srv__send AskUserQuestion ExitPlanMode; do check "plan/$t не трогаем" "" "$(pa plan $t)"; done
for m in default bypassPermissions acceptEdits; do check "$m/Bash не трогаем" "" "$(pa $m Bash)"; done

echo "adapters: ok=$ok bad=$bad"
rm -r "$T"
[ "$bad" = 0 ]
