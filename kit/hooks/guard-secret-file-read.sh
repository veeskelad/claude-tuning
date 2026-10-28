#!/usr/bin/env bash
# Страж для Bash (вызывает secret-guard-bash.py): не выводить содержимое файлов с ключами через шелл —
# это обход инструмента Read (cat/head/less/xxd/base64/git show и т.п.).
#   deny — приватные ключи любым читающим вербом.
#   ask  — .env и файлы с secret/credential/password в имени через читающий верб; голый env/printenv;
#          git show/diff/log -p по такому пути.
# При сомнении — спрашиваем. secret-guard-bash.py превращает ask в безопасный вид команды.
set -uo pipefail

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // ""' 2>/dev/null)
jq_status=$?
# Не смогли разобрать вход — не молчим (тихий exit 0 означал бы пропуск), а просим подтверждения.
# Пустая команда (jq_status=0) — законный пропуск.
if [ $jq_status -ne 0 ]; then
  jq -n --arg d "ask" --arg r "🔐 Не удалось проверить команду на секреты. Подтверди, если она их не печатает." '{
    hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: $d, permissionDecisionReason: $r }
  }'
  exit 0
fi
[ -z "$cmd" ] && exit 0

# Тело heredoc — данные, которые команда пишет в файл, а не команды. Судить по ним нельзя:
# иначе запись заметки с цитатой опасной команды блокируется собственным правилом.
cmd=$(printf '%s' "$cmd" | awk '
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

norm=" $(printf '%s' "$cmd" | tr '\n\t' '  ' | tr -s ' ') "
low=$(printf '%s' "$norm" | tr 'A-Z' 'a-z')
has() { printf '%s' "$low" | grep -Eq "$1"; }

emit() {
  jq -n --arg d "$1" --arg r "$2" '{
    hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: $d, permissionDecisionReason: $r }
  }'
  exit 0
}

# READ_VERB: команды, печатающие содержимое файла. Кроме классики — интерпретаторы с инлайн-скриптом
# (python -c, node -e) и diff/comm. Граница перед вербом — не только пробел: в `$(cat f)` и `` `cat f` ``
# перед вербом скобка или бэктик. Плюс source/`.`: сами не печатают, но втягивают значения в окружение.
READ_VERB='(^| |\(|`|'\''|")(cat|bat|tac|nl|head|tail|less|more|view|xxd|hexdump|od|strings|base64|openssl|yq|dd|diff|comm|vimdiff|colordiff)( |$)|\bsed\b|\bawk\b|\bjq\b|\bgrep\b|\begrep\b|\bfgrep\b|\brg\b|(^| |\(|`|'\''|")(python3?|node|ruby|perl|php)( +-[a-z]*[cerv][a-z]*|\.js| )|(^| )(source|\.) +[^ ]'
# HARD — приватные ключи и хранилища ключей: любое печатающее чтение запрещено.
HARD='\.(pem|key|p12|pfx|jks|keystore)( |$|\.)|(^| |/)(id_rsa|id_ed25519|id_ecdsa|id_dsa)( |$)|(^| |/)\.secrets/'
# SOFT — по имени. Имя о содержимом говорит не всегда; остальное ловит маска вывода и
# guard-secret-in-output.sh. `env` как расширение, а не имя целиком: `app.env`, `*.env` тоже.
SOFT='(^| |/)[^ /]*\.env($|[^a-z0-9_-])|secret|credential|passwd|password|htpasswd|pgpass|netrc'
SAFE='\.example|\.sample|\.template|\.tpl|\.dist|\.pub( |$)|\.md( |$)'

# SOFT судим только по путеподобным аргументам: слово `secret` чаще стоит в шаблоне поиска,
# чем в имени файла (`grep -rn "secret" src/` вопроса не заслуживает).
paths=$(printf '%s' "$cmd" | tr ' =' '\n\n' | tr -d "\"'" \
        | grep -E '/|^\.|\.[A-Za-z0-9]+$' | tr '\n' ' ' | tr 'A-Z' 'a-z')
has_path() { printf ' %s' "$paths" | grep -Eq "$1"; }

# Единственное исключение: инвентаризация имён ключей. Команда — ровно вызов обработчика по
# абсолютному пути и один аргумент-путь; пайп, подстановка, кавычка, glob, перевод строки → общие правила.
HANDLER="$(cd "$(dirname "$0")" && pwd)/config-key-inventory.py"
case "$cmd" in
  *$'\n'*) : ;;
  *) printf '%s' "$cmd" \
       | grep -Eq "^[[:space:]]*${HANDLER//./\\.}[[:space:]]+[A-Za-z0-9_./+:@-]+[[:space:]]*$" \
       && exit 0 ;;
esac

# ask: полный дамп окружения независимо от пути
if has '(^| )printenv *(\||$)' || has '(^| )env *(\||$)'; then
  emit ask "🔐 Команда выведет все переменные вместе с секретами. Имена — env | cut -d= -f1; одно поле — printenv ИМЯ."
fi

# git, печатающий содержимое файла с ключами (show/diff/log -p)
if has '\bgit\b' && has '(git show|git diff|log -p| -p )' && ! has "$SAFE"; then
  if has_path "$HARD"; then
    emit deny "🔐 git покажет приватный ключ. Историю без содержимого — git log --stat или --name-only."
  fi
  if has_path "$SOFT"; then
    emit ask "🔐 git покажет содержимое файла с секретами. Историю без значений — git log --stat или --name-only."
  fi
fi

# Чтение файла с ключами шелл-вербом — по сегментам и только по путеподобным аргументам того же
# сегмента: `grep -rlE 'id_rsa' docs/` ищет текст, а не читает ключ.
verdict=""
while IFS= read -r seg; do
  [ -z "$seg" ] && continue
  seg_low=" $(printf '%s' "$seg" | tr 'A-Z' 'a-z' | tr -s ' ') "
  printf '%s' "$seg_low" | grep -Eq "$READ_VERB" || continue
  printf '%s' "$seg_low" | grep -Eq "$SAFE" && continue
  seg_paths=" $(printf '%s' "$seg" | tr ' =' '\n\n' | tr -d "\"'" \
                | grep -E '/|^\.|\.[A-Za-z0-9]+$' | tr '\n' ' ' | tr 'A-Z' 'a-z') "
  if printf '%s' "$seg_paths" | grep -Eq "$HARD"; then verdict="deny"; break; fi
  # `set -a && . .env && set +a` — загрузка окружения, а не печать. Пропускаем, если в той же
  # команде нет печати переменных (env/printenv ловит guard-process-env-leak.sh).
  if printf '%s' "$seg_low" | grep -Eq '^ *(source|\.) +[^ ]+ *$' \
     && ! printf '%s' "$low" | grep -Eq '(^| )(echo|printf)[^|;]*\$|(^| )(env|printenv)( |\||$)|(^| )set *(\||;|$)'; then
    continue
  fi
  # Исходники с «secret» в имени (secret-mask.py) — код, а не ключи.
  soft_paths=$(printf '%s' "$seg_paths" | tr ' ' '\n' \
               | grep -Ev '\.(sh|bash|py|ts|tsx|js|mjs|go|rs|rb|swift|kt|java)$' | tr '\n' ' ')
  if printf ' %s ' "$soft_paths" | grep -Eq "$SOFT"; then verdict="ask"; fi
done <<< "$(printf '%s' "$cmd" | tr ';|&\n' '\n\n\n\n')"

case "$verdict" in
  deny) emit deny "🔐 Не показываю приватный ключ. Отпечаток без значения — ssh-keygen -lf <файл>; публичная часть — <файл>.pub." ;;
  ask)  emit ask  "🔐 Команда покажет содержимое файла с секретами. Имена ключей без значений — config-key-inventory.py <файл>." ;;
esac

exit 0
