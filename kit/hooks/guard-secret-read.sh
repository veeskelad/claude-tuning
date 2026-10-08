#!/bin/bash
# guard-secret-read.sh — не даёт агенту прочитать файлы с ключами (PreToolUse, matcher: Read).
# Зачем: Claude Code из коробки читает .env и вставляет токены прямо в чат и в скрипты
# (issue anthropics/claude-code#24185, 08.02.2026). Значение, попавшее в чат, уже утекло:
# оно в транскрипте, в логах и в контексте модели.
# Имена ключей можно смотреть так: grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' .env
# Разрешить файл точечно: CLAUDE_SECRET_READ_ALLOW="путь1:путь2".

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
[ -z "$FILE" ] && exit 0

IFS=: read -r -a ALLOW <<< "${CLAUDE_SECRET_READ_ALLOW:-}"
for a in "${ALLOW[@]}"; do [ -n "$a" ] && [ "$FILE" = "$a" ] && exit 0; done

base=$(basename "$FILE")
case "$base" in
  .env.example|.env.sample|.env.template|*.example) exit 0 ;;
  .env|.env.*|*.pem|*.key|*.p12|*.pfx|id_rsa*|id_ed25519*|id_ecdsa*|.netrc|.pgpass|credentials|credentials.json|*service-account*.json|*.keystore|*.session)
    jq -cn --arg r "Файл с ключами ($base) агенту не читается: значение попадёт в чат и транскрипт. Имена ключей: grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' $base. Нужен доступ — человек вписывает путь в CLAUDE_SECRET_READ_ALLOW." \
      '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
    exit 0 ;;
esac
exit 0
