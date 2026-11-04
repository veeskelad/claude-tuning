#!/usr/bin/env bash
# correction-nudge.py: подсказка модели на «опять», «я же просил», «запомни»…; тишина на обычном, в claude -p и на служебном.
set -u
H="$(cd "$(dirname "$0")/.." && pwd)/kit/hooks/correction-nudge.py"
ok=0; bad=0
run() { jq -cn --arg p "$1" '{prompt:$p}' | env -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_TUNING_NUDGE ${2:-} python3 "$H"; }
yes() { if run "$1" | grep -q '"additionalContext"'; then ok=$((ok+1)); else bad=$((bad+1)); echo "ПРОМАХ ждали подсказку: $1"; fi; }
no()  { if [ -z "$(run "$1" "${2:-}")" ]; then ok=$((ok+1)); else bad=$((bad+1)); echo "ПРОМАХ ждали тишину: $1 ${2:-}"; fi; }
yes "ты опять поставил тире"
yes "я же просил без эмодзи"
yes "сколько можно спрашивать"
yes "запомни: отчёты в ~/reports"
yes "в следующий раз сначала тесты"
yes "всегда проверяй CI перед мержем"
yes "никогда не пушь в main"
yes "I told you to use pnpm"
no  "сделай экспорт отчёта"
no  "запусти тесты снова"
no  "почему всегда так медленно грузится страница"
no  "<task-notification>опять</task-notification>"
no  "[alert-triage] опять упал сервис"
no  "ты опять поставил тире" "CLAUDE_CODE_ENTRYPOINT=sdk-cli"
no  "ты опять поставил тире" "CLAUDE_TUNING_NUDGE=0"
out=$(printf 'не json' | python3 "$H"; echo "rc=$?")
if [ "$out" = "rc=0" ]; then ok=$((ok+1)); else bad=$((bad+1)); echo "ПРОМАХ мусор на входе: $out"; fi
echo "correction-nudge: ok=$ok bad=$bad"
[ "$bad" = 0 ]
