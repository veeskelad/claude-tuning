#!/usr/bin/env bash
# Страж: тело heredoc у команды, которой это текст (сообщение коммита, описание PR, файл), не проверяется
# как команда; у шелла, в конвейере и без кавычек у разделителя — проверяется (23.11.2026).
# Запуск: bash tests/heredoc-guard.test.sh [страж], по умолчанию kit/hooks/safety-guard.sh
set -uo pipefail
K=$(cd "$(dirname "$0")/.." && pwd)
GUARD=${1:-$K/kit/hooks/safety-guard.sh}
fail=0

decision() {
  local out
  out=$(jq -n --arg command "$1" '{tool_name:"Bash",tool_input:{command:$command}}' | bash "$GUARD")
  if [ -z "$out" ]; then echo allow; else printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision'; fi
}
check() {
  local got
  got=$(decision "$2")
  [ "$got" = "$1" ] || { printf 'FAIL: %s: expected %s, got %s\n' "$3" "$1" "$got"; fail=$((fail + 1)); }
}

BODY='Страж спрашивал на git reset --hard и git branch -D feature'
check allow $'git commit -q -F - <<\'EOF\'\n'"$BODY"$'\nEOF' 'commit message from heredoc'
check allow $'cd /tmp && git -C /tmp commit -F - <<\'EOF\'\n'"$BODY"$'\nEOF' 'commit message after cd'
check allow $'gh pr create --title t --body-file - <<\'EOF\'\n'"$BODY"$'\nEOF' 'PR body from heredoc'
check allow $'tee /tmp/x.md <<\'EOF\'\n'"$BODY"$'\nEOF' 'tee into a file'
check allow $'mkdir -p /tmp/d && cat > /tmp/d/x.md <<\'EOF\'\n'"$BODY"$'\nEOF' 'cat into a file after mkdir'
check ask $'bash <<\'EOF\'\n'"$BODY"$'\nEOF' 'heredoc into a shell'
check ask $'cat <<\'EOF\' | bash\n'"$BODY"$'\nEOF' 'heredoc piped into a shell'
check ask $'git commit -F - <<EOF\n'"$BODY"$'\nEOF' 'unquoted delimiter'
check ask $'git commit -F - <<\'EOF\'\nmsg\nEOF\ngit reset --hard' 'command after the heredoc'
check ask $'git commit -F msg.txt <<\'EOF\'\n'"$BODY"$'\nEOF' 'commit reads a file, not stdin'

if [ "$fail" -ne 0 ]; then
  printf '%d heredoc-guard test(s) failed\n' "$fail"
  exit 1
fi
echo 'All heredoc-guard tests passed'
