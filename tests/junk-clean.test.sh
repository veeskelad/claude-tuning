#!/usr/bin/env bash
# Проверка claude-junk-clean.sh в песочнице ($CLAUDE_HOME во временной папке) и шаблонов launchd.
# Запуск: bash tests/junk-clean.test.sh. Настоящий ~/.claude не затрагивается.
set -uo pipefail
R="$(cd "$(dirname "$0")/.." && pwd)/kit"
T=$(mktemp -d)
C="$T/claude"; export CLAUDE_HOME="$C" CLAUDE_JUNK_CLEAN_LOG_DIR="$T/log"
ok=0; bad=0
check() { if [ "$2" = "$3" ]; then ok=$((ok+1)); else bad=$((bad+1)); echo "ПРОМАХ $1: ждали [$2], получили [$3]"; fi; }
ex() { [ -e "$1" ] && echo есть || echo нет; }
tree() { (cd "$C" && find . | sort | shasum); }

mkdir -p "$C"/{plans/archive,rules,hooks,bin,session-env/empty-old,projects/-private-tmp-x,file-history/a,plugins/cache/temp_git_1,plugins/cache/real-plugin}
touch "$C/plans/new.md" "$C/settings.json" "$C/hooks/keep.sh" "$C/hooks/keep.sh.bak-new" "$C/plans/INDEX.md" \
      "$C/plans/old.md" "$C/plans/archive/ancient.md" "$C/file-history/a/f1"
# «давно»: mtime 2026-01-01 (старше 90 дней); план — 2026-10-01 (старше 14 дней, но моложе 90)
touch -t 202610010000 "$C/plans/old.md" "$C/plans/INDEX.md"
touch -t 202601010000 "$C/plans/archive/ancient.md" "$C/file-history/a/f1" \
      "$C/session-env/empty-old" "$C/projects/-private-tmp-x" "$C/plugins/cache/temp_git_1" "$C/plugins/cache/real-plugin"
# Бэкапы старше 7 дней определяются по ctime, который задать нельзя, поэтому здесь проверяется только,
# что свежий бэкап остаётся.

before=$(tree)
bash "$R/bin/claude-junk-clean.sh" --dry-run >/dev/null
check "dry-run: дерево не изменилось" "$before" "$(tree)"
check "dry-run: старый план на месте" есть "$(ex "$C/plans/old.md")"

bash "$R/bin/claude-junk-clean.sh" >/dev/null
check "старый план ушёл в archive" "нет есть" "$(ex "$C/plans/old.md") $(ex "$C/plans/archive/old.md")"
check "свежий план остался" есть "$(ex "$C/plans/new.md")"
check "INDEX.md остался на месте" есть "$(ex "$C/plans/INDEX.md")"
check "архив старше 90 дней удалён" нет "$(ex "$C/plans/archive/ancient.md")"
check "file-history старше 30 дней удалён" нет "$(ex "$C/file-history/a/f1")"
check "пустой session-env убран" нет "$(ex "$C/session-env/empty-old")"
check "временный projects/-private-* убран" нет "$(ex "$C/projects/-private-tmp-x")"
check "обломок плагина убран" нет "$(ex "$C/plugins/cache/temp_git_1")"
check "настоящий плагин цел" есть "$(ex "$C/plugins/cache/real-plugin")"
check "settings.json цел" есть "$(ex "$C/settings.json")"
check "свежий бэкап остался" есть "$(ex "$C/hooks/keep.sh.bak-new")"
check "журнал пишется" есть "$(ex "$T/log/clean.log")"
check "нет папки — не ошибка" 0 "$(CLAUDE_HOME="$T/none" bash "$R/bin/claude-junk-clean.sh" >/dev/null 2>&1; echo $?)"

# шаблоны launchd: подстановка и корректность plist
if command -v plutil >/dev/null 2>&1; then
  # repo-freshness — направление «code»: шаблон лежит рядом с его хуком
  for t in launchd/claude-junk-clean tracks/code/launchd/repo-freshness; do
    sed -e "s#__HOME__#/home/example#g" -e "s#__LABEL_PREFIX__#com.example#g" "$R/$t.plist.template" > "$T/${t##*/}.plist"
    t=${t##*/}
    check "$t.plist: plutil" OK "$(plutil -lint "$T/$t.plist" | sed 's/.*: //')"
    check "$t.plist: плейсхолдеры подставлены" 0 "$(grep -c '__HOME__\|__LABEL_PREFIX__' "$T/$t.plist")"
  done
  check "repo-freshness: аргументы" 1 "$(grep -c 'repo-freshness.sh --quiet --work-hours' "$T/repo-freshness.plist")"
fi

echo "junk-clean: ok=$ok bad=$bad"
rm -r "$T"
[ "$bad" = 0 ]
