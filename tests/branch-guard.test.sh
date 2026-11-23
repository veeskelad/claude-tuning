#!/usr/bin/env bash
# Страж: удаление влитой в origin/main ветки проходит без вопроса, невлитой — вопрос (23.11.2026).
# Запуск: bash tests/branch-guard.test.sh [страж], по умолчанию kit/hooks/safety-guard.sh
set -uo pipefail
K=$(cd "$(dirname "$0")/.." && pwd)
GUARD=${1:-$K/kit/hooks/safety-guard.sh}
fail=0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

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

R="$tmp/repo"
g() { git -C "$R" -c user.name=t -c user.email=t@t -c commit.gpgsign=false "$@" >/dev/null 2>&1; }
git init -q --bare "$tmp/origin.git"
git init -q -b main "$R"
g remote add origin "$tmp/origin.git"
echo a >"$R/a.txt"; g add -A; g commit -m init; g push origin main
g switch -c squashed; echo s1 >"$R/s.txt"; g add -A; g commit -m s1; echo s2 >>"$R/s.txt"; g commit -am s2
g switch main; g merge --squash squashed; g commit -m squash
g switch -c ancestor; echo n >"$R/n.txt"; g add -A; g commit -m n; g switch main; g merge --ff-only ancestor
g switch -c unmerged; echo u >"$R/u.txt"; g add -A; g commit -m u
g switch main; g switch -c changed; echo c >"$R/a.txt"; g commit -am c; echo c1 >>"$R/a.txt"; g commit -am c1
g switch main; g merge --squash changed; g commit -m squash2; echo c2 >"$R/a.txt"; g commit -am later
g push origin main; g fetch origin; g switch main

check allow "git -C $R branch -D squashed" 'branch merged by squash'
check allow "git -C $R branch -D ancestor" 'branch merged as ancestor'
check allow "git -C $R branch --delete --force squashed ancestor" 'long flags, two merged branches'
check allow "git -C $R branch -d unmerged" 'plain -d is left to git'
check allow "git -C $R branch --list" 'listing branches'
check ask "git -C $R branch -D unmerged" 'unmerged branch'
check ask "git -C $R branch -D changed" 'main changed the same file later'
check ask "git -C $R branch -D squashed unmerged" 'merged and unmerged together'
check ask "git -C $R branch -df unmerged" 'combined -df on unmerged'
check ask "git -C $R branch -D missing" 'no such local branch'
check ask "git -C $R branch -D 'squashed'" 'quoted name'
check ask "git -C $R branch -D squashed && git -C $R branch -D unmerged" 'chain of commands'
check ask "git -C $tmp branch -D squashed" 'not a git repository'
check ask "git -C $R branch -D -r origin/main" 'remote-tracking flag'
check ask "bash -c 'git -C $R branch -D unmerged'" 'inside a quoted shell argument'
check ask "(git -C $R branch -D unmerged)" 'subshell'
check ask "git -c a=b -C $R branch -D squashed" 'git option before branch'
check ask "git -C $R branch \"-D\" unmerged" 'quoted flag'

if [ "$fail" -ne 0 ]; then
  printf '%d branch-guard test(s) failed\n' "$fail"
  exit 1
fi
echo 'All branch-guard tests passed'
