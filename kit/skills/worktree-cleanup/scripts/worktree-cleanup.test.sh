#!/usr/bin/env bash
# Самопроверка worktree-cleanup на одноразовом репо: bash worktree-cleanup.test.sh
set -euo pipefail

bin="$(cd "$(dirname "$0")" && pwd)/worktree-cleanup"
tmp=$(cd "$(mktemp -d)" && pwd -P)
trap 'kill $sleeper 2>/dev/null; rm -rf "$tmp"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

g() { git -C "$tmp/repo" "$@" >/dev/null 2>&1; }
wt() { git -C "$tmp/repo" worktree add -q -b "$1" "$tmp/${2:-repo-worktrees}/$1" origin/main; }
commit() { echo "$2" > "$tmp/repo-worktrees/$1/$1.txt"; git -C "$tmp/repo-worktrees/$1" add -A; git -C "$tmp/repo-worktrees/$1" commit -qm "$1"; }

git init -q --bare -b main "$tmp/origin.git"
git clone -q "$tmp/origin.git" "$tmp/repo" 2>/dev/null
echo base > "$tmp/repo/base.txt"; g add -A; g commit -m base; g push origin main

wt merged;   commit merged x;   g push origin merged; g merge --ff-only merged; g push origin main
wt rebased;  commit rebased y;  g push origin rebased
echo z > "$tmp/repo/z.txt"; g add -A; g commit -m z; g cherry-pick rebased; g push origin main
g fetch origin
wt pushed;   commit pushed p;   g push origin pushed
wt unpushed; commit unpushed u
wt dirty;    touch "$tmp/repo-worktrees/dirty/untracked"
wt busy;     commit busy b; g merge --ff-only busy; g push origin main; g fetch origin
(cd "$tmp/repo-worktrees/busy" && exec sleep 60) & sleeper=$!
echo '.tasks/' >> "$tmp/repo/.git/info/exclude"
wt tasks;    commit tasks t; g merge --ff-only tasks; g push origin main; g fetch origin
mkdir "$tmp/repo-worktrees/tasks/.tasks"; printf -- '- [x] done\n- [ ] open\n' > "$tmp/repo-worktrees/tasks/.tasks/t.md"
wt outside elsewhere
wt fresh

sleep 1
out=$("$bin" scan "$tmp/repo")
echo "$out"
expect() { grep -qE "$1" <<<"$out" || { echo "FAIL: ждали /$1/"; exit 1; }; }
expect "^READY +$tmp/repo-worktrees/merged "
expect "^READY +$tmp/repo-worktrees/rebased .*patch"
expect "^PUSHED +$tmp/repo-worktrees/pushed "
expect "^REFUSE +$tmp/repo-worktrees/unpushed "
expect "^REFUSE +$tmp/repo-worktrees/dirty "
expect "^REFUSE +$tmp/repo-worktrees/busy .*процесс"
expect "^REFUSE +$tmp/elsewhere/outside "
expect "^REFUSE +$tmp/repo-worktrees/fresh .*начатую"
expect "^REFUSE +$tmp/repo-worktrees/tasks .*чек-лист"

out=$("$bin" clean "$tmp/repo"); echo "$out"
[ ! -d "$tmp/repo-worktrees/merged" ] && [ ! -d "$tmp/repo-worktrees/rebased" ] && [ -d "$tmp/repo-worktrees/pushed" ]
! git -C "$tmp/repo" show-ref -q refs/heads/merged
out=$("$bin" clean --pushed "$tmp/repo"); echo "$out"
[ ! -d "$tmp/repo-worktrees/pushed" ] && git -C "$tmp/repo" show-ref -q refs/heads/pushed
[ -d "$tmp/repo-worktrees/unpushed" ] && [ -d "$tmp/repo-worktrees/dirty" ] && [ -d "$tmp/repo-worktrees/busy" ] && [ -d "$tmp/repo-worktrees/fresh" ] && [ -d "$tmp/repo-worktrees/tasks" ]
echo OK
