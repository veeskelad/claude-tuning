#!/bin/bash
# Safety guard для Bash-команд (PreToolUse).
# Читает hook-JSON со stdin (Claude Code НЕ передаёт команду через env).
#
# Контракт вывода (PreToolUse advanced JSON):
#   permissionDecision "deny" -> блок, причина уходит в Claude
#   permissionDecision "ask"  -> принудительный запрос подтверждения у пользователя
#                                (перебивает defaultMode: auto)
#   нет вывода / exit 0        -> разрешить, продолжить цепочку

INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)

# Нечего проверять -> пропуск.
[ -z "$CMD" ] && exit 0

# Тело heredoc — ДАННЫЕ для записи в файл, а не команды шелла. Судить по ним нельзя:
# иначе запись обычного скрипта блокируется из-за фразы в комментарии.
ORIGINAL_CMD=$CMD
CMD=$(printf '%s' "$CMD" | awk '
  BEGIN { skip = 0 }
  skip { if ($0 ~ ("^[[:space:]]*" delim "[[:space:]]*$")) skip = 0; next }
  {
    line = $0
    if (match(line, /<<-?[[:space:]]*("[^"]+"|'"'"'[^'"'"']+'"'"'|[A-Za-z_][A-Za-z0-9_]*)/)) {
      d = substr(line, RSTART, RLENGTH)
      sub(/^<<-?[[:space:]]*/, "", d)
      gsub(/["'"'"']/, "", d)
      delim = d
      skip = 1
    }
    print line
  }')
# Strip a heredoc only for a plain file-writing cat command. Other bodies may be executable.
FIRST_LINE=${ORIGINAL_CMD%%$'\n'*}
inert_heredoc() {
  [[ "$FIRST_LINE" == *'<<'* && "$FIRST_LINE" == *'>'* ]] || return 1
  [[ "$FIRST_LINE" != *'|'* && "$FIRST_LINE" != *';'* && "$FIRST_LINE" != *'&&'* && "$FIRST_LINE" != *'||'* && "$FIRST_LINE" != *'`'* && "$FIRST_LINE" != *'$('* && "$FIRST_LINE" != *'<('* && "$FIRST_LINE" != *'>('* ]] || return 1
  [[ "${FIRST_LINE#*<<}" != *'<<'* ]] || return 1
  [[ "$FIRST_LINE" == *"<<'"* || "$FIRST_LINE" == *'<<"'* || "$FIRST_LINE" == *"<<-'"* || "$FIRST_LINE" == *'<<-"'* ]] || return 1
  printf '%s' "$FIRST_LINE" | grep -Eq '^[[:space:]]*cat[[:space:]]'
}
inert_heredoc || CMD=$ORIGINAL_CMD
# Перенос строки через `\` шелл склеивает: `docker \<перевод строки> rm -f x` — одна команда (27.10.2026).
CMD=${CMD//\\$'\n'/ }

emit() {
  # $1 = decision (deny|ask), $2 = reason
  jq -cn --arg d "$1" --arg r "$2" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:$d,permissionDecisionReason:$r}}'
  exit 0
}

# === Ключи: значение, выведенное в терминал, попадает в чат и транскрипт ===
# CLAUDE_GUARD_SKIP_SECRETS=1 — у тебя свой страж ключей (маска вместо отказа), этот раздел ему мешал бы.
if [ "${CLAUDE_GUARD_SKIP_SECRETS:-}" != 1 ]; then
  if printf '%s' "$CMD" | grep -qE '(^|[[:space:];|&])(cat|less|more|head|tail|bat|strings|xxd|base64) +[^|;&]*(\.env([[:space:]]|$|\.[a-z]+)|id_rsa|id_ed25519|\.pem([[:space:]]|$)|\.netrc)' \
     && ! printf '%s' "$CMD" | grep -qE '\.env\.(example|sample|template)'; then
    emit deny "⛔ Вывод файла с ключами запрещён: значение попадёт в чат. Имена ключей: grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' <файл>."
  fi
  if printf '%s' "$CMD" | grep -qE '(^|[[:space:];|&])(printenv|env)([[:space:]]*$|[[:space:]]*[|;&])'; then
    emit deny "⛔ Полный дамп переменных окружения выводит ключи в чат. Нужна одна переменная — проверь, что она задана: [ -n \"$ИМЯ\" ] && echo задана."
  fi
fi


# Опасное ищем только в сегментах, которые что-то ЗАПУСКАЮТ. `grep -rn "kubectl delete"
# docs/` и `echo "rm -rf /"` — чтение и печать текста, а не выполнение; на них страж
# выдавал deny и блокировал обычную работу.
# Сегмент режем по | ; && || и смотрим на первое слово. `bash -c "rm -rf /"` начинается
# не с читающего верба, поэтому проверяется целиком вместе с кавычками.
READ_ONLY='^[[:space:]]*(sudo +)?(grep|egrep|fgrep|rg|ag|ack|echo|printf|cat|bat|head|tail|less|more|jq|yq|wc|diff|comm|sort|uniq|column)\b'
SEGMENTS=$(printf '%s' "$CMD" | sed -E 's/\|\||&&|[|;]/\n/g')
if printf '%s' "$CMD" | grep -Eq '\$\(|<\(|>\(|`'; then
  SCAN_LINES=$SEGMENTS
else
  SCAN_LINES=$(printf '%s' "$SEGMENTS" | grep -Ev "$READ_ONLY")
fi
SCAN=$(printf '%s' "$SCAN_LINES" | tr '\n' ' ')
[ -z "${SCAN//[[:space:]]/}" ] && exit 0

# rm с рекурсией по КОРНЮ: / , /* , ~ , $HOME , /etc , /Users/* — путь не глубже одного
# сегмента. Прежний паттерн `rm -rf /[^t]` требовал символ ПОСЛЕ слэша, поэтому `rm -rf /`
# и `rm -fr /` проходили молча, а исключение «/t» пропускало `rm -rf /tmp/../Users`.
# Корень может стоять не первым аргументом: `rm -rf tests/ patches/ plan/ ~/` снёс домашнюю папку (Gigazine, 16.12.2025).
# Требуем границу команды перед rm: флаг Docker --rm не запускает rm.
RM_VERB='(^|[^[:alnum:]_-])rm'
RM_ROOT="$RM_VERB"' +(-[a-zA-Z-]+ +)+([^;&|]* +)?["'"'"']?(/|~|\$\{?HOME\}?)[A-Za-z0-9._-]*/?\*?["'"'"']?([[:space:]]|;|&|\||$)'

# === Высокий риск — DENY (жёсткий блок) ===
if printf '%s' "$SCAN" | grep -qiE "($RM_ROOT|drop database|drop table|drop schema|truncate table|terraform destroy|drizzle-kit push.*--force|prisma migrate reset|prisma db push.*--accept-data-loss|git reset --hard.*origin|kubectl delete|kubectl drain|docker system prune|docker (container|volume) prune|aws s3 rb|aws s3 rm.*--recursive|gcloud.*delete|heroku destroy|npm publish|pip install.*--break-system-packages|chmod( +-[A-Za-z]+)* +777)"; then
  emit deny "⛔ Необратимая команда заблокирована. Если она нужна, запусти вручную в терминале."
fi

# === Контейнеры Docker (27.10.2026): свои тестовые агент убирает сам ===
# Удалить с -f, остановить или убить можно контейнер с меткой claude.agent.session=<session_id этой
# сессии>: на готовый контейнер метку не повесить, значит, её поставил агент при создании. Остальные —
# как раньше: удаление deny, остановка ask. Исключение — только для одной простой команды без кавычек,
# подстановок, цепочек, sudo и глобальных флагов docker: иначе неясно, какие цели и на каком демоне.
# Кавычки и `\` для поиска вырезаем: `docker rm "-f" x` и `"docker" rm -f x` шелл выполнит так же.
AGENT_LABEL=claude.agent.session
SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
[[ "$SID" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$ ]] || SID=""
DOCKER_SCAN=$(printf '%s' "$SCAN_LINES" | tr -d "\"'\\\\")
DOCKER_AT='(^|[^[:alnum:]_.-])docker([[:space:]]+-[^[:space:]]+([[:space:]]+[^-[:space:]][^[:space:]]*)?)*[[:space:]]+(container[[:space:]]+)?'

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

OWN_WHY=""
own_containers_only() {
  local LC_ALL=C re sub tok i n out
  local -a toks targets
  [ -n "$SID" ] || { OWN_WHY="в данных хука нет session_id"; return 1; }
  re='^ *docker +(container +)?(rm|remove|stop|kill)( +[A-Za-z0-9_.=-]+)+ *$'
  [[ "$ORIGINAL_CMD" =~ $re ]] || { OWN_WHY="команда не одна простая docker rm/stop/kill с именами без кавычек"; return 1; }
  sub=${BASH_REMATCH[2]}
  [ "$sub" = remove ] && sub=rm
  read -r -a toks <<<"$ORIGINAL_CMD"
  i=2
  [ "${toks[1]}" = container ] && i=3
  n=${#toks[@]}
  targets=()
  while [ "$i" -lt "$n" ]; do
    tok=${toks[$i]}
    case "$sub $tok" in
      "rm -f" | "rm --force" | "rm -v" | "rm --volumes" | "rm -fv" | "rm -vf") ;;
      "stop --time="[0-9]* | "stop --timeout="[0-9]* | "stop --signal="[A-Z0-9]* | "kill --signal="[A-Z0-9]*) ;;
      *" -"*) OWN_WHY="флаг $tok не из списка разрешённых"; return 1 ;;
      *)
        [[ "$tok" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]+$ ]] || { OWN_WHY="имя $tok не похоже на контейнер"; return 1; }
        targets+=("$tok")
        ;;
    esac
    i=$((i + 1))
  done
  [ "${#targets[@]}" -gt 0 ] || { OWN_WHY="не названо ни одного контейнера"; return 1; }
  out=$(with_timeout 5 docker container inspect --format "{{json (index .Config.Labels \"$AGENT_LABEL\")}}" "${targets[@]}" 2>/dev/null) \
    || { OWN_WHY="docker inspect не ответил или такого контейнера нет"; return 1; }
  if [ "$(printf '%s\n' "$out" | grep -c .)" -ne "${#targets[@]}" ] \
    || [ -n "$(printf '%s\n' "$out" | grep -vxF "\"$SID\"")" ]; then
    OWN_WHY="у контейнера нет метки этой сессии"
    return 1
  fi
  return 0
}

DOCKER_FORCE=0
DOCKER_HALT=0
printf '%s\n' "$DOCKER_SCAN" | grep -iE "$DOCKER_AT"'(rm|remove)([[:space:]]|$)' \
  | grep -qE '[[:space:]](-[a-zA-Z]*f[a-zA-Z]*(=[^[:space:]]*)?|--force(=[^[:space:]]*)?)([[:space:]]|$)' && DOCKER_FORCE=1
printf '%s\n' "$DOCKER_SCAN" | grep -qiE "$DOCKER_AT"'(stop|kill)([[:space:]]|$)' && DOCKER_HALT=1
if [ $((DOCKER_FORCE + DOCKER_HALT)) -gt 0 ] && ! own_containers_only; then
  [ "$DOCKER_FORCE" = 1 ] && emit deny "⛔ Удалить можно только свой контейнер: с меткой $AGENT_LABEL=${SID:-<id сессии>}, одной командой docker rm -f <имя> без кавычек, цепочек и \$(…). Причина отказа: $OWN_WHY. Чужой или без метки пусть удалит владелец в терминале."
  emit ask "⚠️ Остановка чужого или непомеченного контейнера ($OWN_WHY). Свой, с меткой $AGENT_LABEL=${SID:-<id сессии>}, останавливай одной командой docker stop <имя> без кавычек и цепочек."
fi

# Контейнер в фоне без метки этой сессии потом не убрать без владельца — метку просим сразу.
# Только локальный docker в начале сегмента: через ssh и sudo метка не поможет.
if [ -n "$SID" ] \
  && printf '%s\n' "$DOCKER_SCAN" | grep -qE '^[[:space:]]*docker[[:space:]]+(container[[:space:]]+)?(create([[:space:]]|$)|run[[:space:]](.*[[:space:]])?(-[a-zA-Z]*d[a-zA-Z]*|--detach(=true)?)([[:space:]]|$))' \
  && ! printf '%s' "$CMD" | grep -qE "(--label(=|[[:space:]]+)|-l[[:space:]]*)[\"']?claude\\.agent\\.session=$SID([\"'[:space:]]|$)"; then
  emit deny "⛔ Контейнер в фоне без метки сессии потом не убрать без владельца. Добавь --label $AGENT_LABEL=$SID и повтори."
fi

# === Средний риск — ASK (запрос подтверждения) ===
# rm -rf по абсолютному/домашнему пути: не корень, но и не рабочий каталог. Временные
# каталоги (в т.ч. scratchpad сессии в /private/tmp) не трогаем — их уборка штатна.
# `..` в пути снимает исключение: из /tmp им уходят куда угодно (`rm -rf /tmp/../Users`).
# Временные пути вырезаются из строки по одному, остальные абсолютные пути проверяются как обычно:
# `rm -rf ./build /tmp/x` проходит, `rm -rf /tmp/x /etc/y` спрашивает.
SCAN_RM=$SCAN
printf '%s' "$SCAN" | grep -q '\.\.' \
  || SCAN_RM=$(printf '%s' "$SCAN" | sed -E 's#(^|[[:space:]])["'"'"']?(/tmp/|/private/tmp/|/var/folders/)[^[:space:];&|]*#\1#g')
if printf '%s' "$SCAN_RM" | grep -qE "$RM_VERB"' +(-[a-zA-Z-]+ +)+([^;&|]* +)?["'"'"']?(/|~/|\$\{?HOME\}?/)'; then
  emit ask "⚠️ rm -rf за пределами рабочей папки. Проверь путь перед подтверждением."
fi

# Скрипт, который удаляет файлы. 48 218 файлов за 103 с (сентябрь 2026) стёр не rm, а скрипт
# очистки, который агент написал сам: в команде запуска удаления не видно. Поэтому при запуске
# интерпретатора смотрим в сам файл (и во встроенный код) и показываем человеку до запуска.
DEL_RE='shutil\.rmtree|os\.(remove|unlink|rmdir|removedirs)\(|\.unlink\(|\.rmtree\(|send2trash|fs\.(rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync)\(|fs\.promises\.(rm|unlink)|Remove-Item|(^|[[:space:];&|])rm +-[a-zA-Z]*[rR]'
INTERP='(^|[[:space:]])(python[0-9.]*|node|deno|bun|tsx|ts-node|bash|sh|zsh|ruby|perl|pwsh|powershell)([[:space:]]|$)'
if printf '%s' "$SCAN" | grep -qE "$INTERP"; then
  HCWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)
  for f in $(printf '%s' "$SCAN" | grep -oE '[^[:space:]"'"'"']+\.(py|js|mjs|cjs|ts|sh|bash|zsh|rb|pl|ps1)([[:space:]]|$)'); do
    p=$f
    case "$p" in
      /*) ;;
      '~/'*) p="$HOME/${p#'~/'}" ;;
      *) [ -n "$HCWD" ] && p="$HCWD/$p" ;;
    esac
    if [ -f "$p" ] && grep -qE "$DEL_RE" "$p" 2>/dev/null; then
      emit ask "⚠️ Скрипт $f удаляет файлы. Посмотри, какие папки он трогает, прежде чем подтвердить."
    fi
  done
  if printf '%s' "$ORIGINAL_CMD" | grep -qE 'shutil\.rmtree|os\.(remove|unlink|rmdir|removedirs)\(|\.unlink\(|\.(rm|rmSync|unlinkSync|rmdirSync)\(|Remove-Item'; then
    emit ask "⚠️ Встроенный скрипт удаляет файлы. Посмотри, какие папки он трогает, прежде чем подтвердить."
  fi
fi

if printf '%s' "$SCAN" | grep -qiE '(systemctl restart|systemctl stop|systemctl disable|reboot|shutdown|git push +([^|;]* )?(--force|-f)( |$)|git reset +--hard|git clean +-[a-z]*f|git checkout +-- +\.|git branch +-D)'; then
  emit ask "⚠️ Команда останавливает сервис, переписывает историю или стирает незакоммиченное. Проверь цель перед подтверждением."
fi

exit 0
