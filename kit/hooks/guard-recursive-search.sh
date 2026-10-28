#!/usr/bin/env bash
# PreToolUse-страж: РЕКУРСИВНЫЙ ПОИСК ПО КАТАЛОГУ, в котором лежит секрет-файл.
#
# Дыра, которую он закрывает (инцидент 2026-07-14): `grep -rn "10.0.0.5" .`
# выгреб строку из .env.backup — дампа прод-Secret. Ни один страж сработать не мог:
# guard-secret-{read,file-read,dump} судят по ТЕКСТУ КОМАНДЫ, а имени файла в команде
# не было. Постфактум поймал только PostToolUse guard-secret-in-output — когда значения
# уже были напечатаны.
#
# Идея: не гадать по команде, а ПОСМОТРЕТЬ, что реально лежит в области поиска.
# Нашли там дамп/ключ — требуем исключить его явно (--exclude / --glob '!...').
set -uo pipefail

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // ""' 2>/dev/null) || exit 0
[ -z "$cmd" ] && exit 0

norm=" $(printf '%s' "$cmd" | tr '\n\t' '  ' | tr -s ' ') "

emit() {
  jq -n --arg d "$1" --arg r "$2" '{
    hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: $d, permissionDecisionReason: $r }
  }'
  exit 0
}

# 1. Это вообще рекурсивный поиск по содержимому?
#    grep -r/-R, rg/ag (рекурсивны по умолчанию), find -exec с читающим вербом.
is_recursive=0
printf '%s' "$norm" | grep -Eq '\bgrep\b[^|]*[[:space:]]-[a-zA-Z]*[rR]' && is_recursive=1
printf '%s' "$norm" | grep -Eq '(^| )(rg|ag|ack)( |$)'                   && is_recursive=1
printf '%s' "$norm" | grep -Eq '\bfind\b.*-exec[[:space:]]+(cat|grep|head|tail|less|strings)' && is_recursive=1
[ "$is_recursive" -eq 0 ] && exit 0

# 2. Уже исключил чувствительное сам? Тогда не мешаем.
printf '%s' "$norm" | grep -Eq -- '--exclude(-dir)?=|--glob[[:space:]]*.?!|-g[[:space:]]+.?!' && exit 0

# 2.5 Сужен по типам файлов? rg --type/-t — именованные наборы языков, секрет-файлов
#     в них нет. Позитивные глобы (--include=*.ts, -g '*.py') собираем и ниже
#     проверяем, попадает ли под них хоть один опасный файл.
printf '%s' "$norm" | grep -Eq -- '[[:space:]](-t|--type)[[:space:]=]' && exit 0
includes=$(printf '%s' "$norm" | grep -oE -- "--include=[^ ]+|(--glob|-g)[= ][^ ]+" \
           | sed -E 's/^(--include|--glob|-g)[= ]//' | tr -d "\"'")

# 3. Где ищем: берём аргументы, похожие на пути. Пусто -> текущий каталог.
roots=$(printf '%s' "$cmd" | tr ' ' '\n' \
        | grep -Ev '^-|^$|^"|^\x27' \
        | grep -E '^(/|\./|\.\.|[A-Za-z0-9_.-]+/)' | head -4)
[ -z "$roots" ] && roots="."

# 4. Что опасного реально лежит в этой области. Ограничиваем глубину и время —
#    страж не имеет права подвешивать сессию.
DANGER='\.env($|\.)|\.envrc$|\.backup$|\.dump$|secrets?\.(ya?ml|json|txt)$|\.pem$|\.key$|\.p12$|\.pfx$|id_(rsa|ed25519|ecdsa)$|credentials?$|\.htpasswd$|sa-key.*\.json$'
found=""
for r in $roots; do
  [ -e "$r" ] || continue
  hits=$(timeout 3 find "$r" -maxdepth 4 \
           \( -name node_modules -o -name .git -o -name dist -o -name build \) -prune -o \
           -type f -print 2>/dev/null \
         | grep -Ei "$DANGER" | grep -Eiv '\.(example|sample|template|dist|tpl)$' | head -6)
  [ -n "$hits" ] && found="${found}${hits}"$'\n'
done

found=$(printf '%s' "$found" | grep -v '^$' | head -8)
[ -z "$found" ] && exit 0

# 5. Есть позитивный глоб — оставляем только те опасные файлы, что реально под него
#    попадают. `--include=*.ts` до .env не дотянется, спрашивать не о чем.
if [ -n "$includes" ]; then
  matched=""
  while IFS= read -r f; do
    for pat in $includes; do
      case "${f##*/}" in $pat) matched="${matched}${f}"$'\n'; break ;; esac
    done
  done <<< "$found"
  found=$(printf '%s' "$matched" | grep -v '^$')
  [ -z "$found" ] && exit 0
fi

# В сообщении — имена до трёх файлов в одну строку: длинный список путей человек не читает.
list=$(printf '%s\n' "$found" | head -3 | sed 's#.*/##' | paste -sd, - | sed 's/,/, /g')
total=$(printf '%s\n' "$found" | grep -c .)
[ "$total" -gt 3 ] && list="${list} и ещё $((total - 3))"

emit ask "🔐 Поиск заденет файлы с секретами: ${list}. Исключи их: --exclude='*.env*' (grep) или --glob '!*.env*' (rg)."
