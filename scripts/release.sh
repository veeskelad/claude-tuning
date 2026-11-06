#!/usr/bin/env bash
# Выпуск версии набора: scripts/release.sh 3.8.0
# Переносит «Не выпущено» из CHANGELOG.md под номер и дату, пишет VERSION, коммитит и ставит тег vX.Y.Z.
# Тег нужен: по нему у людей tuning-update-check.sh узнаёт, что вышла новая версия. Не пушит — push отдельной командой (подсказка в конце).
set -eu
v="${1:-}"
[[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "нужен номер вида 3.8.0"; exit 1; }
cd "$(dirname "$0")/.."
[ -z "$(git status --porcelain)" ] || { echo "в рабочей копии есть незакоммиченное: сначала коммит"; exit 1; }
git rev-parse "v$v" >/dev/null 2>&1 && { echo "тег v$v уже есть"; exit 1; }
cur=$(cat VERSION)
[ "$(printf '%s\n%s\n' "$cur" "$v" | sort -V | tail -1)" = "$v" ] && [ "$cur" != "$v" ] || { echo "новая версия должна быть больше текущей ($cur)"; exit 1; }
body=$(awk '/^## Не выпущено/{f=1;next} /^## /{f=0} f' CHANGELOG.md | sed '/^[[:space:]]*$/d')
[ -n "$body" ] || { echo "в CHANGELOG.md под «Не выпущено» пусто: опишите, что изменилось, простыми словами"; exit 1; }
python3 - "$v" <<'PY'
import sys, time
v = sys.argv[1]
s = open("CHANGELOG.md").read()
s = s.replace("## Не выпущено\n", f"## Не выпущено\n\n## {v} — {time.strftime('%d.%m.%Y')}\n", 1)
open("CHANGELOG.md", "w").write(s)
PY
echo "$v" > VERSION
bash tests/update-flow.test.sh >/dev/null || { git checkout -- CHANGELOG.md VERSION; echo "тест обновления не прошёл: выпуск отменён"; exit 1; }
git add CHANGELOG.md VERSION
git commit -q -m "release $v"
git tag "v$v"
echo "готово: версия $v, тег v$v. Отправить: git push origin HEAD v$v"
