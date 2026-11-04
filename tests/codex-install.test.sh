#!/usr/bin/env bash
# Направление codex: установка дописывает хуки моста в конец hooks.json, не трогая чужие, повтор ничего не меняет,
# проверка видит стража, удаление возвращает hooks.json к исходному.
set -u
cd "$(dirname "$0")/.."
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
export CLAUDE_GUARD_NO_AI_SIGNATURE=1
fail=0
ok() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi; }

mkdir -p "$T/.codex"
mine='{"hooks":{"PreToolUse":[{"matcher":".*","hooks":[{"type":"command","command":"echo mine"}]}]}}'
echo "$mine" > "$T/.codex/hooks.json"

python3 scripts/install-kit.py --home "$T" --with codex --apply > "$T/out1" 2>&1
ok "скрипты моста на месте" '[ -x "$T/.codex/hooks/claude-parity/pre_tool_guard.py" ]'
ok "чужой хук остался первым" '[ "$(jq -r ".hooks.PreToolUse[0].hooks[0].command" "$T/.codex/hooks.json")" = "echo mine" ]'
ok "хук стража дописан вторым" 'jq -r ".hooks.PreToolUse[1].hooks[0].command" "$T/.codex/hooks.json" | grep -q pre_tool_guard.py'
ok "бэкап hooks.json" 'ls "$T/.codex/" | grep -q "hooks.json.bak-"'
ok "AGENTS.md поставлен" '[ -f "$T/.codex/AGENTS.md" ]'
ok "подсказка про /hooks" 'grep -q "/hooks" "$T/out1"'

before=$(jq -S . "$T/.codex/hooks.json")
python3 scripts/install-kit.py --home "$T" --with codex --apply > /dev/null 2>&1
ok "повтор ничего не дописал" '[ "$(jq -S . "$T/.codex/hooks.json")" = "$before" ]'

python3 scripts/install-kit.py --home "$T" --with codex --check > "$T/out2" 2>&1
ok "проверка зелёная" 'grep -q "всё на месте" "$T/out2"'
ok "страж codex отказал" 'grep -q "страж codex" "$T/out2"'

python3 scripts/install-kit.py --home "$T" --with codex --uninstall --apply > /dev/null 2>&1
ok "hooks.json вернулся к исходному" '[ "$(jq -cS . "$T/.codex/hooks.json")" = "$(echo "$mine" | jq -cS .)" ]'
ok "скрипты моста убраны" '[ ! -e "$T/.codex/hooks/claude-parity/pre_tool_guard.py" ]'

U=$(mktemp -d)
python3 scripts/install-kit.py --home "$U" --with codex --apply > "$U/out" 2>&1
ok "без ~/.codex — подсказка, файлов нет" 'grep -q "Codex не найден" "$U/out" && [ ! -e "$U/.codex/hooks.json" ]'
rm -rf "$U"

exit $fail
