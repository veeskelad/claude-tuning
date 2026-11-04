#!/usr/bin/env bash
# Направление codex: unify-agent-files.sh сводит пары CLAUDE.md/AGENTS.md к «канон + симлинк».
# Запуск: bash tests/codex-unify.test.sh. Пары — во временной папке, HOME подменён: ~/Work и ~/Projects не трогаются.
set -uo pipefail
S="$(cd "$(dirname "$0")/.." && pwd)/kit/tracks/codex/bin/unify-agent-files.sh"
T=$(mktemp -d); T=$(cd "$T" && pwd -P)
trap 'rm -rf "$T"' EXIT
export HOME="$T/home"; mkdir -p "$HOME"
unset CLAUDE_WORK_ROOTS
ok=0; bad=0
check() { if [ "$2" = "$3" ]; then ok=$((ok+1)); else bad=$((bad+1)); echo "ПРОМАХ $1: ждали [$2], получили [$3]"; fi; }
kind() { if [ -L "$1" ]; then echo "ссылка→$(readlink "$1")"; elif [ -f "$1" ]; then echo файл; else echo нет; fi; }

R="$T/roots"
mkdir -p "$R/same" "$R/clone" "$R/diverged" "$R/x-worktrees/wt" "$R/only"
printf '# Проект\nТесты: make test\n' > "$R/same/CLAUDE.md"; cp "$R/same/CLAUDE.md" "$R/same/AGENTS.md"
printf '# Проект\nClaude читает ~/.claude/CLAUDE.md. Правила — в CLAUDE.md.\nworktree: .claude/worktrees\n' > "$R/clone/CLAUDE.md"
printf '# Проект\nCodex читает ~/.claude/CLAUDE.md. Правила — в AGENTS.md.\nworktree: .codex/worktrees\n' > "$R/clone/AGENTS.md"
printf '# Проект\nПлатежи выключены.\n' > "$R/diverged/CLAUDE.md"
printf '# Проект\nПлатежи включены.\n' > "$R/diverged/AGENTS.md"
cp "$R/same/CLAUDE.md" "$R/x-worktrees/wt/CLAUDE.md"; cp "$R/same/CLAUDE.md" "$R/x-worktrees/wt/AGENTS.md"
printf 'один\n' > "$R/only/CLAUDE.md"

# сухой прогон: отчёт, ничего не меняется
out=$(bash "$S" "$R")
check "код сухого прогона" 0 $?
check "identical в отчёте" 1 "$(grep -c '^IDENTICAL .*same' <<<"$out")"
check "clone в отчёте" 1 "$(grep -c '^CLONE .*clone' <<<"$out")"
check "diverged в отчёте" 1 "$(grep -c '^DIVERGED .*diverged' <<<"$out")"
check "worktree пропущен" 0 "$(grep -c 'x-worktrees' <<<"$out")"
check "итог" "пар: 3 | identical: 1 | clone: 1 | diverged: 1" "$(grep '^пар:' <<<"$out")"
check "сухой прогон не трогает same" файл "$(kind "$R/same/AGENTS.md")"
check "сухой прогон не трогает clone" файл "$(kind "$R/clone/AGENTS.md")"
check "показано, что станет" 1 "$(grep -c 'станет:.*агент читает' <<<"$out")"

# применить
bash "$S" --apply "$R" >/dev/null
check "same → ссылка" "ссылка→CLAUDE.md" "$(kind "$R/same/AGENTS.md")"
check "clone → ссылка" "ссылка→CLAUDE.md" "$(kind "$R/clone/AGENTS.md")"
check "clone обезличен" "агент читает ~/.claude/CLAUDE.md. Правила — в AGENTS.md." "$(sed -n 2p "$R/clone/CLAUDE.md")"
check "clone: путь worktree" "worktree: .worktrees" "$(sed -n 3p "$R/clone/CLAUDE.md")"
check "diverged не тронут" файл "$(kind "$R/diverged/AGENTS.md")"
check "diverged текст цел" "Платежи включены." "$(sed -n 2p "$R/diverged/AGENTS.md")"
check "worktree не тронут" файл "$(kind "$R/x-worktrees/wt/AGENTS.md")"
check "одиночный CLAUDE.md без пары" нет "$(kind "$R/only/AGENTS.md")"

# повторный прогон: ссылки уже стоят, пар для работы нет, кроме разошедшейся
check "повтор" "пар: 1 | identical: 0 | clone: 0 | diverged: 1" "$(bash "$S" "$R" | grep '^пар:')"

# корни из CLAUDE_WORK_ROOTS, ~ раскрывается
mkdir -p "$HOME/w/p"; printf 'x\n' > "$HOME/w/p/CLAUDE.md"; cp "$HOME/w/p/CLAUDE.md" "$HOME/w/p/AGENTS.md"
check "CLAUDE_WORK_ROOTS" 1 "$(CLAUDE_WORK_ROOTS='~/w' bash "$S" | grep -c '^IDENTICAL  w/p')"

echo "unify-agent-files: $ok ок, $bad промахов"
[ "$bad" -eq 0 ]
