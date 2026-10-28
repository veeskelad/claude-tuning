#!/usr/bin/env bash
# PreToolUse-страж: не печатать окружение процессов и контейнеров в транскрипт.
#
# Повод (19.08.2026): `pgrep -af warmup` вывел командную строку docker exec целиком,
# вместе с `-e ADMIN_PASSWORD=...` и `-e CACHE_PASSWORD=...`.
# Оба пароля утекли в транскрипт, который синкается наружу. PostToolUse-детектор
# поймал их уже после факта — отменить нельзя, только ротировать.
#
# Ловит команды, которые печатают аргументы процессов или переменные окружения целиком.
# Пропускает то же самое, если вывод заведомо обезврежен: счётчик, только имена ключей,
# только pid, только конкретное поле через --format.
set -uo pipefail

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // ""')
norm=" $(printf '%s' "$cmd" | tr '\n\t' '  ' | tr -s ' ') "
has() { printf '%s' "$norm" | grep -Eiq -- "$1"; }

deny() {
  jq -n --arg r "$1" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: ("🔐 " + $r)
    }
  }'
  exit 0
}

# Вывод уже обезврежен: считаем, берём только имена ключей, только pid, одно поле формата.
sanitized() {
  # A safe-looking fragment must not waive checks for another shell command.
  if [[ "$cmd" == *';'* || "$cmd" == *'&&'* || "$cmd" == *'||'* || "$cmd" == *$'\n'* || "$cmd" == *'`'* || "$cmd" == *'$('* || "$cmd" == *'<('* || "$cmd" == *'>('* ]]; then
    return 1
  fi
  # Only one producer and one sanitizer may form a safe pipeline.
  [[ "$cmd" == *'|'* && "${cmd#*|}" != *'|'* ]] || return 1
  has '\|[[:space:]]*tee([[:space:]]|$)' && return 1
  has '\|[[:space:]]*cut[[:space:]]+-d=?[[:space:]]+-f ?1' && return 0
  has '\| ?wc -l' && return 0
  has '\|[[:space:]]*grep -[a-z]*c[a-z]* ' && return 0
  has '\|[[:space:]]*(sha256sum|shasum|md5sum)([[:space:]]|$)' && return 0
  return 1
}

sanitized && exit 0

# 1. ps с выводом аргументов команды
if has ' ps ' || has '^ ?ps '; then
  if has ' ps [a-z-]*(aux|axu|-ef|-ax|ef)' || has ' ps [^|]*(args|command|cmd)'; then
    deny "Эта команда выведет пароли из аргументов процессов. Имена — ps -o pid,comm; pid — pgrep -f '<шаблон>'."
  fi
fi

# 2. pgrep/pidof с печатью полной командной строки
if has 'pgrep (-[a-z]*a[a-z]*|--list-full)( |$)'; then
  deny "pgrep -a выведет пароли из командной строки. Только pid — pgrep -f '<шаблон>'; счёт — pgrep -c -f."
fi

# 2б. На macOS pgrep -l вместе с -f печатает полную командную строку, как -a. так утёк токен:
# командная строка Bash-обёртки Claude Code содержит export токена.
if has 'pgrep( [^|;&]*)? -[a-z]*l' && has 'pgrep( [^|;&]*)? -[a-z]*f'; then
  deny "pgrep -l вместе с -f печатает полную командную строку, как -a. Только pid: pgrep -f '<шаблон>'; живость своего фона: файл вывода задачи или ps -o pid=,etime= -p <pid>."
fi

# 3. docker inspect с окружением
if has 'docker [a-z ]*inspect' && has '\.Config\.Env|\.Env(}|\b)'; then
  deny "docker inspect выведет переменные контейнера с паролями. Имена — --format '{{range .Config.Env}}{{println .}}{{end}}' | cut -d= -f1."
fi

# 4. env / printenv без сужения
if has ' (env|printenv)([[:space:]]*([|;&]|$))' || has 'docker exec [^|]* (env|printenv)'; then
  deny "Эта команда выведет все переменные вместе с паролями. Имена — env | cut -d= -f1; одна несекретная — printenv ИМЯ."
fi

# 5. /proc/<pid>/environ — окружение чужого процесса
if has '/proc/[0-9*]+/environ'; then
  deny "environ выведет пароли чужого процесса. Только имена — tr '\\0' '\\n' | cut -d= -f1."
fi

# 6. systemctl show и docker compose config раскрывают Environment= целиком
if has 'systemctl [a-z-]*show' && ! has ' --property=(ActiveState|SubState|LoadState|UnitFileState|Description)'; then
  deny "systemctl show выведет Environment= с паролями. Одно поле — systemctl show <unit> --property=ActiveState."
fi
if has 'docker[- ]compose [^|]*config' && ! has '--services|--volumes|--profiles|--images'; then
  deny "docker compose config подставит пароли из .env. Список сервисов — docker compose config --services."
fi

exit 0
