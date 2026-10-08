#!/usr/bin/env bash
# Показывает чек-листы TASKS.md с незакрытыми пунктами: цель и первые открытые пункты.
#
# Зачем: любая рабочая задача ведёт чек-лист (.tasks/<slug>.md), но между
# сессиями и после сжатия контекста про открытый файл никто не помнит — задача теряется молча.
# Правило чек-листа — kit/tasks-policy.md скилла claude-tuning.
#
# Кто зовёт: SessionStart (корень текущего проекта), /morning и /endday (все рабочие корни).
# Где ищет под корнем: файлы в .tasks/ (с 25.09.2026 — основное место), а также старые TASKS.md и
# TASKS-<slug>.md в корне, в worktree (*-worktrees/*/), в devops-plan/<тема>/.
# Только чтение, без сети.
#
# Использование:
#   open-tasks.sh                 — корень $CLAUDE_PROJECT_DIR (иначе текущий каталог)
#   open-tasks.sh --all           — все рабочие корни из CLAUDE_WORK_ROOTS (через двоеточие; по умолчанию ~/Work, ~/Projects, ~/code)
#   open-tasks.sh <путь> [<путь>…] — свои корни
set -uo pipefail

SHOW_ITEMS=5
STALE_DAYS=7

ROOTS=()
for a in "$@"; do
  case "$a" in
    --all) IFS=: read -r -a _r <<< "${CLAUDE_WORK_ROOTS:-$HOME/Work:$HOME/Projects:$HOME/code}"; ROOTS+=("${_r[@]}") ;;
    *) ROOTS+=("$a") ;;
  esac
done
[ ${#ROOTS[@]} -eq 0 ] && ROOTS=("${CLAUDE_PROJECT_DIR:-$PWD}")

files=""
for root in "${ROOTS[@]}"; do
  [ -d "$root" ] || continue
  files+=$(find "$root" -maxdepth 4 \
    \( -name node_modules -o -name .git -o -name dist -o -name build -o -name done \) -prune -o \
    -type f \( -name 'TASKS*.md' -o -path '*/.tasks/*.md' \) -print 2>/dev/null)
  files+=$'\n'
done
files=$(printf '%s' "$files" | grep -v '^$' | sort -u)
[ -z "$files" ] && exit 0

OPEN_RE='^[[:space:]]*[-*] \[ \]'
now=$(date +%s)
out=()
while IFS= read -r f; do
  open=$(grep -cE "$OPEN_RE" "$f" 2>/dev/null) || open=0
  [ "$open" -eq 0 ] && continue
  done_n=$(grep -cE '^[[:space:]]*[-*] \[[xX]\]' "$f" 2>/dev/null) || done_n=0
  # Сначала GNU (-c): на Linux `stat -f` — это статус ФС, он печатает текст и ломает арифметику.
  age=$(( ( now - $(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f") ) / 86400 ))
  stale=""; [ "$age" -ge "$STALE_DAYS" ] && stale=" ⚠️ залежался: доделать или закрыть с причиной"
  goal=$(grep -m1 -E '^# ' "$f" | sed -E 's/^# +//' | cut -c1-120)
  out+=("${f/#"$HOME"/"~"} — открыто $open из $((open + done_n)), правка $age дн. назад$stale")
  [ -n "$goal" ] && out+=("   цель: $goal")
  while IFS= read -r item; do
    out+=("   ${item:0:120}")
  done < <(grep -m"$SHOW_ITEMS" -E "$OPEN_RE" "$f" | sed -E 's/^[[:space:]]*//')
  [ "$open" -gt "$SHOW_ITEMS" ] && out+=("   … ещё $((open - SHOW_ITEMS))")
done <<< "$files"

[ ${#out[@]} -eq 0 ] && exit 0
echo "📋 Незакрытые задачи (.tasks/ и TASKS.md). Задача по теме из списка начинается с чтения её файла:"
printf '%s\n' "${out[@]}"
