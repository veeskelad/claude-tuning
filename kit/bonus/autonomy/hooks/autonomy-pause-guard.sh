#!/usr/bin/env bash
# PreToolUse-страж безголовых запусков (бонус autonomy). В обычной сессии молчит: работает, только когда
# claude-run.sh выставил CLAUDE_AUTONOMY_JOB.
#
# 1. Пауза: есть ~/.claude/autonomy/PAUSE или PAUSE-<задание> — отклоняется ЛЮБОЙ вызов инструмента. Так пауза
#    останавливает и уже идущий запуск: модель получает отказ на каждом шаге и заканчивает.
# 2. L3 по rules/autonomy.md в минимальном, нарочно осторожном виде (лучше лишний отказ, чем лишнее действие):
#    - git push в любом виде (в том числе внутри bash -c "...");
#    - мерж: gh pr merge, glab mr merge; gh api с методом POST/PUT/PATCH/DELETE или полями -f/-F/--input;
#    - curl с -X/--request POST|PUT|PATCH|DELETE, -d/--data*, -F/--form, --json, -T/--upload-file;
#      wget с --post-data/--post-file/--method/--body-*;
#    - отправка почты и сообщений: mail, mailx, sendmail, msmtp, mutt, swaks, osascript ... send;
#      MCP-инструменты с send/reply/forward/post/publish/merge/invite/share в имени;
#    - управление самой автономией изнутри запуска: claude-autonomy, launchctl, crontab, вложенный claude;
#    - rm -r вне рабочей папки задания (и самой папки целиком); Write/Edit вне рабочей папки.
#    Не ловит: find -delete, xargs rm, скрипты, которые делают то же внутри себя, чтение секретов (это — стражи
#    базы). Границу держит узкий --allowedTools задания, этот страж — второй рубеж.
# 3. След: если задан CLAUDE_AUTONOMY_TRACE, на каждый вызов дописывается строка «время инструмент решение»
#    (без команд и путей). По ней claude-run.sh видит, что хуки вообще срабатывают внутри claude -p.
set -uo pipefail

job="${CLAUDE_AUTONOMY_JOB:-}"
[ -n "$job" ] || exit 0
A="${CLAUDE_AUTONOMY_HOME:-$HOME/.claude/autonomy}"
TRACE="${CLAUDE_AUTONOMY_TRACE:-}"
input=$(cat)

tool=""
trace() { [ -n "$TRACE" ] && echo "$(date -u +%FT%TZ) ${tool:-?} $1" >> "$TRACE" 2>/dev/null; return 0; }
deny() {
  trace deny
  if command -v jq >/dev/null 2>&1; then
    jq -cn --arg r "$1" '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny",
      permissionDecisionReason: ("autonomy: " + $r)}}'
  else
    echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"autonomy: отказ стража безголового запуска"}}'
  fi
  exit 0
}

[[ "$job" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || deny "странное имя задания в CLAUDE_AUTONOMY_JOB — отказ на всякий случай."
if [ -e "$A/PAUSE" ] || [ -e "$A/PAUSE-$job" ]; then
  deny "пауза ($( [ -e "$A/PAUSE" ] && echo PAUSE || echo "PAUSE-$job" )). Человек остановил автономные запуски: инструменты не вызывать, закончить ответом «остановлено паузой». Снимает паузу только человек: claude-autonomy resume."
fi
command -v jq >/dev/null 2>&1 || deny "нет jq — страж не может разобрать вызов, поэтому отклоняет все."

tool=$(jq -r '.tool_name // ""' <<<"$input" 2>/dev/null) || deny "не разобрать вызов инструмента — отказ на всякий случай."
cwd=$(jq -r '.cwd // ""' <<<"$input")
WD="${CLAUDE_AUTONOMY_WORKDIR:-$cwd}"
[ -n "$WD" ] && WD=$(cd "$WD" 2>/dev/null && pwd -P)
[ -n "$cwd" ] || cwd="$WD"

# Путь строго внутри рабочей папки (не сама папка). Не разобрать ($, `, ..) — значит снаружи.
inside_wd() {
  local p="$1" d b r
  [ -n "$WD" ] || return 1
  case "$p" in *'$'*|*'`'*|*..*|"") return 1 ;; esac
  # shellcheck disable=SC2088  # тильда — буквальный текст из команды
  case "$p" in "~") p="$HOME" ;; "~/"*) p="$HOME/${p#\~/}" ;; /*) ;; *) p="$cwd/$p" ;; esac
  p="${p%/}"; d=$(dirname "$p"); b=$(basename "$p")
  if r=$(cd "$d" 2>/dev/null && pwd -P); then
    case "$b" in .) p="$r" ;; *) p="$r/$b" ;; esac
  fi
  case "$p" in "$WD"/*) return 0 ;; esac
  return 1
}

case "$tool" in
  Write|Edit|MultiEdit|NotebookEdit)
    f=$(jq -r '.tool_input.file_path // .tool_input.notebook_path // ""' <<<"$input")
    inside_wd "$f" || deny "запись вне рабочей папки задания ($WD) — L3 для безголового запуска. Записать в итог, что нужно сделать."
    ;;
  mcp__*)
    name="${tool#mcp__}"; name="${name#*__}"
    name=$(printf '%s' "$name" | tr '[:upper:]-' '[:lower:]_')
    if [[ "_${name}_" =~ _(send|reply|forward|post|publish|merge|invite|share)_ ]]; then
      deny "$tool — исходящее действие (L3). В безголовом запуске не отправлять: записать черновик в рабочую папку и упомянуть в итоге."
    fi
    ;;
  Bash)
    cmd=$(jq -r '.tool_input.command // ""' <<<"$input")
    n=" $(printf '%s' "$cmd" | tr '\n\t' ';;' | tr -s ' ') "
    has() { printf '%s' "$n" | grep -Eq -- "$1"; }
    # команда в позиции команды: после начала, ; & | ( или $(, с необязательными sudo/env/command/nohup/exec
    at='(^|[;&|(]|\$\()[[:space:]]*((sudo|env|command|nohup|exec|time)[[:space:]]+([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*)*([^[:space:];&|()]*/)?'
    end='([^A-Za-z0-9_.-]|$)'
    has "(^|[^A-Za-z0-9_-])git[[:space:]]([^;&|]*[[:space:]])?push$end" \
      && deny "git push — L3 (публикация). Оставить коммит локально и написать в итоге «Нужно от тебя: push»."
    has "(gh[[:space:]]+pr|glab[[:space:]]+mr)[[:space:]]+merge$end" \
      && deny "мерж — L3. Написать в итоге, что готово к мержу."
    has "gh[[:space:]]+api[[:space:]](.*[[:space:]])?((-X|--method)[[:space:]=]*['\"]?(POST|PUT|PATCH|DELETE)|(-f|-F|--field|--raw-field|--input)([[:space:]=]|$))" \
      && deny "gh api с записью — L3. Только чтение (GET)."
    has "curl[[:space:]](.*[[:space:]])?((-X|--request)[[:space:]=]*['\"]?(POST|PUT|PATCH|DELETE)|(-d|--data[a-z-]*|-F|--form[a-z-]*|--json|-T|--upload-file)([[:space:]=]|$))" \
      && deny "curl с отправкой данных — L3 (исходящее действие). Только чтение."
    has "wget[[:space:]].*--(post-data|post-file|method|body-data|body-file)" \
      && deny "wget с отправкой данных — L3. Только чтение."
    has "${at}(mail|mailx|sendmail|msmtp|mutt|swaks)$end" \
      && deny "отправка почты — L3. Черновик в рабочую папку, отправит человек."
    has "${at}osascript$end.*[Ss]end" \
      && deny "osascript с отправкой — L3."
    has "${at}(claude-autonomy|launchctl|crontab|claude)$end" \
      && deny "управление расписанием, паузой или вложенный claude изнутри запуска запрещены: это решает человек."
    # rm -r: каждая цель внутри рабочей папки
    segs=$(printf '%s' "$cmd" | tr '\n' ';' | sed -e 's/&&/;/g' -e 's/||/;/g' | tr '|&' ';;')
    IFS=';' read -r -a parts <<<"$segs"
    for seg in "${parts[@]}"; do
      read -r -a w <<<"$seg"
      i=0
      while [ "$i" -lt "${#w[@]}" ]; do
        case "${w[$i]}" in sudo|env|command|nohup|exec|time|*=*) i=$((i + 1)) ;; *) break ;; esac
      done
      [ "$i" -lt "${#w[@]}" ] || continue
      case "${w[$i]}" in rm|*/rm) ;; *) continue ;; esac
      rec=false; targets=(); opts=true
      for t in "${w[@]:$((i + 1))}"; do
        t="${t#[\'\"]}"; t="${t%[\'\"]}"
        if $opts && [ "$t" = "--" ]; then opts=false; continue; fi
        if $opts && [[ "$t" == --* ]]; then [ "$t" = "--recursive" ] && rec=true; continue; fi
        if $opts && [[ "$t" == -* ]]; then [[ "$t" == *[rR]* ]] && rec=true; continue; fi
        targets+=("$t")
      done
      $rec || continue
      for t in "${targets[@]+"${targets[@]}"}"; do
        inside_wd "$t" || deny "rm -r вне рабочей папки задания ($WD): $t — L3 (удаление данных)."
      done
    done
    ;;
esac
trace allow
exit 0
