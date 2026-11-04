#!/usr/bin/env bash
# PermissionRequest: в режиме плана не спрашивать разрешения на исследование.
# Хук срабатывает, когда Claude Code уже собрался показать окно: команда не попала в белый список,
# слишком сложна для разбора или путь вне рабочих папок. В плане агент только читает и ищет,
# и сотня окон «да/нет» подряд мешает работе, а не защищает.
#
# Стражи к этому моменту уже отработали (PreToolUse): deny остаётся deny, а их «ask» в плане
# bypass-ask-to-deny.sh превращает в отказ с пояснением, как в bypass. Поэтому окно здесь — только
# «нет правила», и его можно одобрить.
#
# Edit/Write/MCP не трогаем: план не правит файлы, а MCP бывают пишущими (отправка сообщений).
# AskUserQuestion и ExitPlanMode — вопросы человеку, а не разрешения: их не трогаем никогда.
set -uo pipefail

input=$(cat)
[ "$(jq -r '.permission_mode // empty' <<<"$input" 2>/dev/null)" = "plan" ] || exit 0

case "$(jq -r '.tool_name // empty' <<<"$input")" in
  Bash|Read|Grep|Glob|LS|WebFetch|WebSearch|Skill|Agent|Task|ToolSearch)
    printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}' ;;
esac
exit 0
