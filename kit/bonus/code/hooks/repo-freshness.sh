#!/usr/bin/env bash
# Держит origin/* свежим и делает отставание клона ЗАМЕТНЫМ.
#
# Зачем: в отставшем клоне `git status` врёт по смыслу — он сравнивает с локальным main,
# поэтому уже смерженная чужая правка выглядит как незакоммиченная работа: агент предлагает
# «закоммитить» то, что неделю лежит в origin/main.
#
# ТОЛЬКО fetch, никогда pull. Рабочие деревья бывают грязными, на фиче или с worktree —
# автоматический pull там либо упадёт, либо утащит чужую работу. fetch не трогает ничего.
#
# Использование:
#   repo-freshness.sh                  — корни по умолчанию, отчёт только про отставшие
#   repo-freshness.sh --quiet          — молча (для таймера)
#   repo-freshness.sh <путь> [<путь>…] — свои корни
set -uo pipefail

# Выключатель: CLAUDE_FRESHNESS_OFF=1 (например, на машине, куда .git приходит синхронизацией).
[ "${CLAUDE_FRESHNESS_OFF:-0}" = 1 ] && exit 0

QUIET=0
WORK_HOURS=0
ROOTS=()
for a in "$@"; do
  case "$a" in
    --quiet) QUIET=1 ;;
    --work-hours) WORK_HOURS=1 ;;
    *) ROOTS+=("$a") ;;
  esac
done
[ ${#ROOTS[@]} -eq 0 ] && IFS=: read -r -a ROOTS <<< "${CLAUDE_WORK_ROOTS:-$HOME/Work:$HOME/Projects:$HOME/code}"

# Для таймера: вне 10:00-18:59 не ходим в сеть вовсе. Окно проверяется здесь, а не в plist,
# чтобы не плодить два десятка StartCalendarInterval и держать расписание в одном месте.
if [ "$WORK_HOURS" = 1 ]; then
  h=$(date +%-H)
  { [ "$h" -lt 10 ] || [ "$h" -ge 19 ]; } && exit 0
fi

# Fetch всех репозиториев параллельно: последовательно это ~14 с и хук ощутимо тормозит старт
# сессии. Считать отставание после — операция локальная и мгновенная.
repos=()
for root in "${ROOTS[@]}"; do
  [ -d "$root" ] || continue
  for repo in "$root" "$root"/*/; do
    [ -e "$repo/.git" ] || continue
    repos+=("${repo%/}")
    # Таймаут: сеть может лежать или VPN быть выключен — хук не имеет права блокировать старт.
    timeout 20 git -C "$repo" fetch --quiet --prune origin 2>/dev/null &
  done
done
wait

stale=()
for repo in "${repos[@]}"; do
  head_ref=$(git -C "$repo" symbolic-ref -q --short HEAD 2>/dev/null || echo '-')
  base=$(git -C "$repo" rev-parse --verify -q origin/main >/dev/null 2>&1 && echo origin/main || echo origin/master)
  behind=$(git -C "$repo" rev-list --count "HEAD..$base" 2>/dev/null || echo 0)
  [ "${behind:-0}" -ge 20 ] && stale+=("$(basename "$repo") [$head_ref] отстаёт от $base на $behind")
done

if [ "$QUIET" = 0 ] && [ ${#stale[@]} -gt 0 ]; then
  echo "⚠️  Отставшие клоны — читать эталон через 'git show origin/main:<путь>', не из рабочей копии:"
  printf '   %s\n' "${stale[@]}"
  echo "   В таком клоне 'git status' показывает уже смерженное чужое как незакоммиченное."
fi
exit 0
