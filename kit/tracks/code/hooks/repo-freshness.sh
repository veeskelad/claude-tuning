#!/usr/bin/env bash
# Держит origin/* свежим и делает отставание клона ЗАМЕТНЫМ.
#
# Зачем: в отставшем клоне `git status` врёт по смыслу — он сравнивает с локальным main,
# поэтому уже смерженная чужая правка выглядит как незакоммиченная работа: агент предлагает
# «закоммитить» то, что неделю лежит в origin/main.
#
# fetch всех клонов, а основную копию на main ещё и fast-forward до origin/main: эталонный клон
# всегда на свежем main, а работа идёт в worktree. Только --ff-only: копию на другой ветке, main
# с локальными правками в тех же файлах и разошедшуюся историю git не трогает, такие клоны уходят
# в отчёт, чтобы разобрать руками (правку в ветку, копию на main). Ветки и worktree не трогаются,
# правки не сбрасываются и никуда не переносятся.
#
# Использование:
#   repo-freshness.sh                  — корни по умолчанию, отчёт только про отставшие
#   repo-freshness.sh --quiet          — молча (для таймера): одна строка в лог, если что-то менялось
#   repo-freshness.sh --work-hours     — вне 10:00-18:59 не ходить в сеть (для таймера)
#   repo-freshness.sh <путь> [<путь>…] — свои корни
#
# Корни по умолчанию — CLAUDE_WORK_ROOTS (через двоеточие), иначе ~/Work:~/Projects:~/code.
set -uo pipefail

# Выключатель: CLAUDE_FRESHNESS_OFF=1 (например, на машине, куда .git приходит синхронизацией:
# fetch с двух концов даёт конфликты).
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

with_timeout() {
  # в базовой macOS нет timeout, perl есть везде
  local t=$1
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$t" "$@"
  else
    perl -e 'alarm shift; exec @ARGV or exit 127' "$t" "$@"
  fi
}

mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"; }  # GNU первым: на Linux `stat -f` — статус ФС

# Fetch всех репозиториев параллельно: последовательно это десятки секунд, и хук ощутимо тормозит
# старт сессии. Считать отставание после — операция локальная и мгновенная.
# На старте сессии клон, который fetch'или меньше 15 минут назад (таймер, соседняя сессия), не трогаем:
# иначе старт ждёт самый медленный fetch. Таймер (--quiet) fetch'ит всё.
FRESH_SEC=900
FETCH_TIMEOUT=20; [ "$QUIET" = 1 ] || FETCH_TIMEOUT=10
now=$(date +%s)
repos=()
for root in "${ROOTS[@]}"; do
  root=${root/#\~/$HOME}
  [ -d "$root" ] || continue
  for repo in "$root" "$root"/*/; do
    [ -e "$repo/.git" ] || continue
    repos+=("${repo%/}")
    fh="$repo/.git/FETCH_HEAD"
    if [ "$QUIET" = 0 ] && [ -f "$fh" ] && [ $(( now - $(mtime "$fh") )) -lt "$FRESH_SEC" ]; then
      continue
    fi
    # Таймаут: сеть может лежать или VPN быть выключен — хук не имеет права блокировать старт.
    with_timeout "$FETCH_TIMEOUT" git -C "$repo" fetch --quiet --prune origin 2>/dev/null &
  done
done
wait

stale=()
updated=()
blocked=()
for repo in ${repos[@]+"${repos[@]}"}; do  # пустой массив + set -u: на bash 3.2 это ошибка
  head_ref=$(git -C "$repo" symbolic-ref -q --short HEAD 2>/dev/null || echo '-')
  base_br=$(git -C "$repo" rev-parse --verify -q origin/main >/dev/null 2>&1 && echo main || echo master)
  base="origin/$base_br"
  behind=$(git -C "$repo" rev-list --count "HEAD..$base" 2>/dev/null || echo 0)
  [ "${behind:-0}" -eq 0 ] && continue
  if [ "$head_ref" = "$base_br" ]; then
    # Только fast-forward: если не выходит, git ничего не меняет, а клон попадает в отчёт.
    if err=$(with_timeout 60 git -C "$repo" merge --ff-only -q "$base" 2>&1); then
      updated+=("$(basename "$repo") +$behind")
      continue
    fi
    files=$(git -C "$repo" diff --name-only HEAD | head -3 | tr '\n' ' ')
    blocked+=("$(basename "$repo"): $(printf '%s' "$err" | grep -m1 -v '^[[:space:]]*$' | cut -c1-100)${files:+ — $files}")
  fi
  [ "${behind:-0}" -ge 20 ] && stale+=("$(basename "$repo") [$head_ref] отстаёт от $base на $behind")
done

if [ "$QUIET" = 1 ]; then
  # Таймер: одна строка в лог, в сессии ничего не выводим.
  if [ ${#updated[@]} -gt 0 ] || [ ${#blocked[@]} -gt 0 ]; then
    echo "$(date '+%d.%m %H:%M') подтянуты: ${updated[*]:-нет}; не обновлены: ${blocked[*]:-нет}"
  fi
  exit 0
fi
if [ ${#updated[@]} -gt 0 ]; then
  echo "🔄 Основные копии подтянуты до origin/main: ${updated[*]}"
fi
if [ ${#blocked[@]} -gt 0 ]; then
  echo "⚠️  main не обновился (разобрать: правку в ветку, копию на main):"
  printf '   %s\n' "${blocked[@]}"
fi
if [ ${#stale[@]} -gt 0 ]; then
  echo "⚠️  Отставшие клоны — читать эталон через 'git show origin/main:<путь>', не из рабочей копии:"
  printf '   %s\n' "${stale[@]}"
  echo "   В таком клоне 'git status' показывает уже смерженное чужое как незакоммиченное."
fi
exit 0
