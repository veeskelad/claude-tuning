#!/usr/bin/env bash
# Проверка statusline.sh на образцах входного JSON. Запуск: bash tests/statusline.test.sh
set -uo pipefail
S="$(cd "$(dirname "$0")/.." && pwd)/kit/statusline/statusline.sh"
ok=0; bad=0
check() { if [ "$2" = "$3" ]; then ok=$((ok+1)); else bad=$((bad+1)); echo "ПРОМАХ $1: ждали [$2], получили [$3]"; fi; }
strip() { sed $'s/\x1b\\[[0-9;]*m//g'; }
now=$(date +%s)

full=$(printf '{"model":{"display_name":"Opus"},"context_window":{"used_percentage":42.7},"rate_limits":{"five_hour":{"used_percentage":63.2,"resets_at":%d},"seven_day":{"used_percentage":12,"resets_at":%d}}}' $((now+5400)) $((now+500000)) | bash "$S" | strip)
check "модель и контекст" "Opus  context:42%" "$(echo "$full" | cut -c1-17)"
check "5h: процент и время" 1 "$(echo "$full" | grep -c '63% 1h[0-9]*m')"
check "7d: процент и дни" 1 "$(echo "$full" | grep -c '12% 5d')"
check "пустой JSON" "?  context:0%" "$(echo '{}' | bash "$S" | strip)"
check "мусор на входе" "?  context:0%" "$(echo 'not json' | bash "$S" | strip)"
check "процент больше 100 не ломает полосу" 0 "$(echo '{"context_window":{"used_percentage":250},"rate_limits":{"five_hour":{"used_percentage":250,"resets_at":1}}}' | bash "$S" >/dev/null 2>&1; echo $?)"

echo "statusline: ok=$ok bad=$bad"
[ "$bad" = 0 ]
