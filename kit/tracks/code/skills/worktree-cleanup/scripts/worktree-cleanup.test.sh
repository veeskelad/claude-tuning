#!/usr/bin/env bash
# Самопроверка worktree-cleanup на одноразовом репо: bash worktree-cleanup.test.sh
set -euo pipefail

bin="$(cd "$(dirname "$0")" && pwd)/worktree-cleanup"
base=$(mktemp -d)
trap 'kill $sleeper 2>/dev/null; rm -rf "$base"' EXIT
# git пишет пути без симлинков (/private/var на macOS)
tmp=$(cd "$base" && pwd -P)
export GIT_CONFIG_GLOBAL=/dev/null GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export WORKTREE_CLEANUP_TRASH="$tmp/trash"

g() { git -C "$tmp/repo" "$@" >/dev/null 2>&1; }
wt() { git -C "$tmp/repo" worktree add -q -b "$1" "$tmp/${2:-repo-worktrees}/$1" origin/main; }
commit() { echo "$2" > "$tmp/repo-worktrees/$1/$1.txt"; git -C "$tmp/repo-worktrees/$1" add -A; git -C "$tmp/repo-worktrees/$1" commit -qm "$1"; }

git init -q --bare -b main "$tmp/origin.git"
git clone -q "$tmp/origin.git" "$tmp/repo" 2>/dev/null
echo base > "$tmp/repo/base.txt"; printf '*.tfvars\nnode_modules/\n.next/\n*.tsbuildinfo\n.claude/settings.local.json\n' > "$tmp/repo/.gitignore"
mkdir -p "$tmp/repo/.claude"; echo '{"permissions":{}}' > "$tmp/repo/.claude/settings.local.json"
g add -A; g commit -m base; g push origin main

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
wt ignored; commit ignored i; g merge --ff-only ignored; g push origin main; g fetch origin
echo secret > "$tmp/repo-worktrees/ignored/terraform.tfvars"
wt cached;  commit cached c;  g merge --ff-only cached;  g push origin main; g fetch origin
mkdir -p "$tmp/repo-worktrees/cached/node_modules/x"; echo m > "$tmp/repo-worktrees/cached/node_modules/x/i.js"
wt nextcache; commit nextcache n; g merge --ff-only nextcache; g push origin main; g fetch origin
mkdir -p "$tmp/repo-worktrees/nextcache/.next/cache"; echo m > "$tmp/repo-worktrees/nextcache/.next/cache/a"
echo '{}' > "$tmp/repo-worktrees/nextcache/tsconfig.tsbuildinfo"
wt pushign; commit pushign q; g push origin pushign
wt localcopy; commit localcopy l; g merge --ff-only localcopy; g push origin main; g fetch origin
mkdir -p "$tmp/repo-worktrees/localcopy/.claude"; cp "$tmp/repo/.claude/settings.local.json" "$tmp/repo-worktrees/localcopy/.claude/"
wt localdiff; commit localdiff m; g merge --ff-only localdiff; g push origin main; g fetch origin
mkdir -p "$tmp/repo-worktrees/localdiff/.claude"; echo '{"mine":1}' > "$tmp/repo-worktrees/localdiff/.claude/settings.local.json"
echo secret > "$tmp/repo-worktrees/pushign/local.tfvars"
# squash-мерж: в main один новый коммит, ветки на origin нет (GitLab удалил после мержа)
wt squashed; commit squashed s1; echo s2 > "$tmp/repo-worktrees/squashed/second.txt"
git -C "$tmp/repo-worktrees/squashed" add -A; git -C "$tmp/repo-worktrees/squashed" commit -qm s2
g merge --squash squashed; g commit -m "squash"; g push origin main; g fetch origin
# сироты, которые привёз mutagen: без .git с одним кэшем, с битым .git, и пустой контейнер
mkdir -p "$tmp/repo-worktrees/leftover/node_modules/x"; echo m > "$tmp/repo-worktrees/leftover/node_modules/x/i.js"
mkdir -p "$tmp/repo-worktrees/ghost"; echo 'gitdir: /nonexistent/.git/worktrees/ghost' > "$tmp/repo-worktrees/ghost/.git"
mkdir -p "$tmp/repo-worktrees/strayfile"; echo note > "$tmp/repo-worktrees/strayfile/notes.md"
mkdir -p "$tmp/repo/.worktrees"
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
expect "^REFUSE +$tmp/repo-worktrees/ignored .*terraform.tfvars"
expect "^READY +$tmp/repo-worktrees/cached "
expect "^READY +$tmp/repo-worktrees/nextcache "
expect "^REFUSE +$tmp/repo-worktrees/pushign .*local.tfvars"
expect "^READY +$tmp/repo-worktrees/localcopy "
expect "^REFUSE +$tmp/repo-worktrees/localdiff .*settings.local.json"
expect "^READY +$tmp/repo-worktrees/squashed .*squash"
expect "^LEFTOVER +$tmp/repo-worktrees/leftover "
expect "^ORPHAN +$tmp/repo-worktrees/ghost .*nonexistent"
expect "^ORPHAN +$tmp/repo-worktrees/strayfile .*не из кэша"

out=$("$bin" scan --no-fetch --ready-only "$tmp/repo")
grep -q "^READY " <<<"$out" && grep -q "^LEFTOVER " <<<"$out" && ! grep -qE "^(REFUSE|ORPHAN|PUSHED) " <<<"$out"

out=$("$bin" clean "$tmp/repo"); echo "$out"
[ ! -d "$tmp/repo-worktrees/merged" ] && [ ! -d "$tmp/repo-worktrees/rebased" ] && [ -d "$tmp/repo-worktrees/pushed" ]
[ ! -d "$tmp/repo-worktrees/cached" ] && [ -f "$tmp/repo-worktrees/ignored/terraform.tfvars" ]
[ ! -d "$tmp/repo-worktrees/nextcache" ] && [ ! -d "$tmp/repo-worktrees/squashed" ]
[ ! -d "$tmp/repo-worktrees/localcopy" ] && [ -f "$tmp/repo-worktrees/localdiff/.claude/settings.local.json" ]
! git -C "$tmp/repo" show-ref -q refs/heads/merged
[ ! -d "$tmp/repo-worktrees/leftover" ] && ls "$tmp/trash" | grep -q '^leftover-wt-'
[ -f "$tmp/repo-worktrees/ghost/.git" ] && [ -f "$tmp/repo-worktrees/strayfile/notes.md" ]
[ ! -d "$tmp/repo/.worktrees" ]
out=$("$bin" clean --pushed "$tmp/repo"); echo "$out"
[ ! -d "$tmp/repo-worktrees/pushed" ] && git -C "$tmp/repo" show-ref -q refs/heads/pushed
[ -d "$tmp/repo-worktrees/unpushed" ] && [ -d "$tmp/repo-worktrees/dirty" ] && [ -d "$tmp/repo-worktrees/busy" ] && [ -d "$tmp/repo-worktrees/fresh" ] && [ -d "$tmp/repo-worktrees/tasks" ]
[ -f "$tmp/repo-worktrees/ignored/terraform.tfvars" ] && [ -f "$tmp/repo-worktrees/pushign/local.tfvars" ]
echo OK
