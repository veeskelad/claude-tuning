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

emit() {
  # $1 = decision (deny|ask), $2 = reason
  jq -cn --arg d "$1" --arg r "$2" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:$d,permissionDecisionReason:$r}}'
  exit 0
}

# === Ключи: значение, выведенное в терминал, попадает в чат и транскрипт ===
if printf '%s' "$CMD" | grep -qE '(^|[[:space:];|&])(cat|less|more|head|tail|bat|strings|xxd|base64) +[^|;&]*(\.env([[:space:]]|$|\.[a-z]+)|id_rsa|id_ed25519|\.pem([[:space:]]|$)|\.netrc)' \
   && ! printf '%s' "$CMD" | grep -qE '\.env\.(example|sample|template)'; then
  emit deny "⛔ Вывод файла с ключами запрещён: значение попадёт в чат. Имена ключей: grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' <файл>."
fi
if printf '%s' "$CMD" | grep -qE '(^|[[:space:];|&])(printenv|env)([[:space:]]*$|[[:space:]]*[|;&])'; then
  emit deny "⛔ Полный дамп переменных окружения выводит ключи в чат. Нужна одна переменная — проверь, что она задана: [ -n \"$ИМЯ\" ] && echo задана."
fi


# Опасное ищем только в сегментах, которые что-то ЗАПУСКАЮТ. `grep -rn "kubectl delete"
# docs/` и `echo "rm -rf /"` — чтение и печать текста, а не выполнение; на них страж
# выдавал deny и блокировал обычную работу.
# Сегмент режем по | ; && || и смотрим на первое слово. `bash -c "rm -rf /"` начинается
# не с читающего верба, поэтому проверяется целиком вместе с кавычками.
READ_ONLY='^[[:space:]]*(sudo +)?(grep|egrep|fgrep|rg|ag|ack|echo|printf|cat|bat|head|tail|less|more|jq|yq|wc|diff|comm|sort|uniq|column)\b'
SEGMENTS=$(printf '%s' "$CMD" | sed -E 's/\|\||&&|[|;]/\n/g')
if printf '%s' "$CMD" | grep -Eq '\$\(|<\(|>\(|`'; then
  SCAN=$(printf '%s' "$SEGMENTS" | tr '\n' ' ')
else
  SCAN=$(printf '%s' "$SEGMENTS" | grep -Ev "$READ_ONLY" | tr '\n' ' ')
fi
[ -z "${SCAN//[[:space:]]/}" ] && exit 0

# rm с рекурсией по КОРНЮ: / , /* , ~ , $HOME , /etc , /Users/* — путь не глубже одного
# сегмента. Прежний паттерн `rm -rf /[^t]` требовал символ ПОСЛЕ слэша, поэтому `rm -rf /`
# и `rm -fr /` проходили молча, а исключение «/t» пропускало `rm -rf /tmp/../Users`.
# Корень может стоять не первым аргументом: `rm -rf tests/plan/ ~/` снёс домашнюю папку (Gigazine, 16.12.2025).
RM_ROOT='rm +(-[a-zA-Z-]+ +)+([^;&|]* +)?["'"'"']?(/|~|\$\{?HOME\}?)[A-Za-z0-9._-]*/?\*?["'"'"']?([[:space:]]|;|&|\||$)'

# === Высокий риск — DENY (жёсткий блок) ===
if printf '%s' "$SCAN" | grep -qiE "($RM_ROOT|drop database|drop table|drop schema|truncate table|terraform destroy|drizzle-kit push.*--force|prisma migrate reset|prisma db push.*--accept-data-loss|git reset --hard.*origin|kubectl delete|kubectl drain|docker rm -f|docker system prune|aws s3 rb|aws s3 rm.*--recursive|gcloud.*delete|heroku destroy|npm publish|pip install.*--break-system-packages|chmod 777)"; then
  emit deny "⛔ Необратимая команда заблокирована. Если она нужна — запусти вручную в терминале."
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
if printf '%s' "$SCAN_RM" | grep -qE 'rm +(-[a-zA-Z-]+ +)+([^;&|]* +)?["'"'"']?(/|~/|\$\{?HOME\}?/)'; then
  emit ask "⚠️ rm -rf за пределами рабочей папки. Проверь путь перед подтверждением."
fi

if printf '%s' "$SCAN" | grep -qiE '(docker stop|systemctl restart|systemctl stop|systemctl disable|reboot|shutdown|git push +([^|;]* )?(--force|-f)( |$)|git reset +--hard|git clean +-[a-z]*f|git checkout +-- +\.|git branch +-D)'; then
  emit ask "⚠️ Команда останавливает сервис, переписывает историю или стирает незакоммиченное. Проверь цель перед подтверждением."
fi

exit 0
