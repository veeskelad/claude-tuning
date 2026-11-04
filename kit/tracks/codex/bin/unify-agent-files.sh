#!/usr/bin/env bash
# unify-agent-files.sh — свести пары CLAUDE.md/AGENTS.md в проектах к «канон + симлинк».
#
# Пара обычными файлами всегда расходится: в одном правят факт (платежи включили, репозиторий переехал),
# второй месяцами врёт другому агенту. Симлинк AGENTS.md → CLAUDE.md делает расхождение невозможным.
#
#   unify-agent-files.sh [--apply] [КОРЕНЬ…]
#
# По умолчанию — сухой прогон: только отчёт. Корни — аргументы, иначе CLAUDE_WORK_ROOTS (через двоеточие,
# ~ можно), иначе ~/Work, ~/Projects, ~/code. Глубина поиска — 5 уровней; worktree и node_modules пропускаются.
#
# Классы пары:
#   IDENTICAL — побайтово совпадают → AGENTS.md становится симлинком
#   CLONE     — различаются только имена агентов (Claude/Codex, их папки worktree, имя файла) → CLAUDE.md
#               обезличивается (строки, которые поменяются, показаны в отчёте), AGENTS.md становится симлинком
#   DIVERGED  — различия по существу → не трогать, показать число различающихся строк; сводить вручную
#
# Нужны perl и diff (есть в macOS и в любом Linux): у sed в macOS нет \b, и обезличивание молча не срабатывало.
set -uo pipefail

APPLY=0
ROOTS=()
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) ROOTS+=("$arg") ;;
  esac
done
if [ ${#ROOTS[@]} -eq 0 ]; then
  IFS=: read -r -a ROOTS <<<"${CLAUDE_WORK_ROOTS:-$HOME/Work:$HOME/Projects:$HOME/code}"
fi
for i in "${!ROOTS[@]}"; do ROOTS[$i]="${ROOTS[$i]/#\~/$HOME}"; done

# Имя файла меняется только как отдельное слово: путь ~/.claude/CLAUDE.md остаётся путём.
NEUTRALIZE='s/Claude Code/агент/g; s/\bClaude\b/агент/g; s/\bCodex\b/агент/g;
            s#\.claude/worktrees#.worktrees#g; s#\.codex/worktrees#.worktrees#g;
            s#(?<![/\w.])CLAUDE\.md#AGENTS.md#g'
neutral() { perl -pe "$NEUTRALIZE" "$1"; }

found=0; ident=0; clone=0; diverged=0

while IFS= read -r c; do
  d=$(dirname "$c"); a="$d/AGENTS.md"
  [ -e "$a" ] || continue
  [ -L "$a" ] && continue
  case "$d" in *worktree*|*node_modules*) continue ;; esac
  found=$((found+1))
  rel="${d#"$HOME"/}"

  if cmp -s "$c" "$a"; then
    ident=$((ident+1))
    echo "IDENTICAL  $rel"
    if [ "$APPLY" = 1 ]; then
      rm -f "$a" && ln -s CLAUDE.md "$a" && echo "           → AGENTS.md стал симлинком"
    fi
  elif cmp -s <(neutral "$c") <(neutral "$a"); then
    clone=$((clone+1))
    echo "CLONE      $rel  (различаются только имена агентов)"
    diff <(cat "$c") <(neutral "$c") | grep '^>' | head -5 | sed 's/^>/           станет:/'
    if [ "$APPLY" = 1 ]; then
      perl -pi -e "$NEUTRALIZE" "$c"
      rm -f "$a" && ln -s CLAUDE.md "$a"
      echo "           → CLAUDE.md обезличен, AGENTS.md стал симлинком"
    fi
  else
    diverged=$((diverged+1))
    echo "DIVERGED   $rel  ($(diff "$c" "$a" | grep -c '^[<>]') различающихся строк) — не трогаю"
  fi
done < <(find "${ROOTS[@]}" -maxdepth 5 -name CLAUDE.md -type f 2>/dev/null)

echo
echo "пар: $found | identical: $ident | clone: $clone | diverged: $diverged"
[ "$APPLY" = 0 ] && echo "сухой прогон. применить: $0 --apply${ROOTS[*]:+ ${ROOTS[*]}}"
exit 0
