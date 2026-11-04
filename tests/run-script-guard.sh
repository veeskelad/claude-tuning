#!/usr/bin/env bash
# Страж на скриптах удаления: образцы из tests/script-guard/fixtures копируются во временный каталог (там «не тест»
# лежит вне tests/), каждый проверяется стражем как `bash <файл>`, решение сверяется с tests/script-guard/cases.tsv.
# Гоняется /bin/bash (на Маке 3.2, им страж запускается по шебангу) и bash из PATH.
#   tests/run-script-guard.sh [страж]   по умолчанию kit/hooks/safety-guard.sh
set -uo pipefail
K=$(cd "$(dirname "$0")/.." && pwd)
GUARD=${1:-$K/kit/hooks/safety-guard.sh}
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
cp -R "$K/tests/script-guard/fixtures/." "$T/"
fail=0
for sh in /bin/bash $(command -v bash); do
  while IFS="$(printf '\t')" read -r exp name; do
    [ -n "$name" ] || continue
    out=$(jq -n --arg c "bash $T/$name" '{tool_name:"Bash",tool_input:{command:$c}}' | "$sh" "$GUARD")
    got=allow
    [ -n "$out" ] && got=$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision')
    [ "$got" = "$exp" ] || { echo "FAIL [$sh] $name: ждали $exp, получили $got"; fail=$((fail + 1)); }
  done < "$K/tests/script-guard/cases.tsv"
done
[ "$fail" -eq 0 ] && echo "script-guard: все случаи сошлись" || { echo "script-guard: провалов $fail"; exit 1; }
