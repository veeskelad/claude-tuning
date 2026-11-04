#!/usr/bin/env bash
# Проверка tuning-update-check.sh: новая версия — одна строка, та же или старее — тишина, второй старт за сутки — тишина.
# Всё во временном каталоге: свой «origin» без сети, свой HOME.
set -u
R="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$R/kit/hooks/tuning-update-check.sh"
T=$(mktemp -d)
ok=0; bad=0
check() { if [ "$2" = "$3" ]; then ok=$((ok+1)); else bad=$((bad+1)); echo "ПРОМАХ $1: ждали [$2], получили [$3]"; fi; }

git init -q --bare "$T/origin.git"
git clone -q "$T/origin.git" "$T/work" 2>/dev/null
git -C "$T/work" -c user.name=t -c user.email=t@t commit -q --allow-empty -m one
git -C "$T/work" tag v3.1.0
git -C "$T/work" push -q origin HEAD --tags 2>/dev/null
git clone -q "$T/origin.git" "$T/home/.claude/skills/claude-tuning" 2>/dev/null
KIT="$T/home/.claude/skills/claude-tuning"
state() { printf '{"version": "%s", "tracks": [], "kit": "%s"}\n' "$1" "$KIT" > "$T/home/.claude/claude-tuning.json"; }
run() { rm -f "$T/home/.claude/.claude-tuning-update-check"; HOME="$T/home" bash "$HOOK" </dev/null; }

state 3.1.0
check "та же версия — тишина" "" "$(run)"

git -C "$T/work" -c user.name=t -c user.email=t@t commit -q --allow-empty -m two
git -C "$T/work" tag v3.2.0
git -C "$T/work" push -q origin HEAD --tags 2>/dev/null
out=$(run)
check "новая версия — строка с номером" 1 "$(printf '%s' "$out" | grep -c 'вышла версия 3.2.0 (у тебя 3.1.0)')"
check "второй старт за сутки — тишина" "" "$(HOME="$T/home" bash "$HOOK" </dev/null)"

state 3.10.0
check "у тебя новее (3.10 > 3.2) — тишина" "" "$(run)"

check "выключено переменной — тишина" "" "$(rm -f "$T/home/.claude/.claude-tuning-update-check"; state 3.1.0; CLAUDE_TUNING_UPDATE_CHECK=0 HOME="$T/home" bash "$HOOK")"

printf '{"version": "3.1.0", "tracks": [], "kit": "%s"}\n' "$T/nowhere" > "$T/home/.claude/claude-tuning.json"
check "набор не клон git — тишина" "" "$(run)"

check "без файла состояния — тишина" "" "$(rm -f "$T/home/.claude/claude-tuning.json"; run)"

echo "update-check: ok=$ok bad=$bad"
[ "$bad" = 0 ]
