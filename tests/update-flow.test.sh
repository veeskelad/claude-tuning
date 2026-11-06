#!/usr/bin/env bash
# Первая установка → обновление: устаревший, но не правленный файл обновляется; правка человека остаётся «своей версией»;
# новое в автообновлении ждёт «да»; старая установка без карты сумм (до 3.8) узнаётся по истории git.
# Всё во временном каталоге: свой «origin» без сети, свой HOME.
set -u
R="$(cd "$(dirname "$0")/.." && pwd)"
T=$(mktemp -d)
ok=0; bad=0
check() { if [ "$2" = "$3" ]; then ok=$((ok+1)); else bad=$((bad+1)); echo "ПРОМАХ $1: ждали [$2], получили [$3]"; fi; }
G="git -c user.name=t -c user.email=t@t"

# «релиз 1.0.0»: копия набора без .git
mkdir "$T/src" && (cd "$R" && tar --exclude=.git -cf - .) | (cd "$T/src" && tar xf -)
echo 1.0.0 > "$T/src/VERSION"
(cd "$T/src" && git init -q -b main && $G add -A && $G commit -q -m v1 && git tag v1.0.0)
git clone -q "$T/src" "$T/home/.claude/skills/claude-tuning"
KIT="$T/home/.claude/skills/claude-tuning"
HOME_="$T/home"
inst() { python3 "$KIT/scripts/install-kit.py" --home "$HOME_" "$@"; }

out=$(inst --apply)
check "первая установка названа" 1 "$(printf '%s' "$out" | grep -c '^Первая установка')"
check "карта сумм записана" true "$(python3 -c 'import json,sys;print(str(len(json.load(open(sys.argv[1]))["files"])>10).lower())' "$HOME_/.claude/claude-tuning.json")"
out=$(inst --apply)
check "повторный запуск без изменений" 0 "$(printf '%s' "$out" | grep -c '^обновлено\|^своя версия')"

# человек правит один файл; набор правит другой и добавляет новый
echo "# моя правка" >> "$HOME_/.claude/hooks/loop-detector.sh"
echo "# релиз 1.1.0" >> "$T/src/kit/hooks/safety-guard.sh"
echo "# релиз 1.1.0" >> "$T/src/kit/hooks/loop-detector.sh"
printf '#!/usr/bin/env bash\nexit 0\n' > "$T/src/kit/hooks/new-hook.sh"
echo 1.1.0 > "$T/src/VERSION"
(cd "$T/src" && $G add -A && $G commit -q -m v1.1 && git tag v1.1.0)

# автообновление через хук
printf '%s\n' '{"auto_update": true}' > /dev/null
python3 - "$HOME_/.claude/claude-tuning.json" <<'PY'
import json,sys
p=sys.argv[1]; d=json.load(open(p)); d["auto_update"]=True; json.dump(d,open(p,"w"))
PY
rm -f "$HOME_/.claude/.claude-tuning-update-check"
msg=$(HOME="$HOME_" bash "$KIT/kit/hooks/tuning-update-check.sh" </dev/null)
check "хук сказал про обновление" 1 "$(printf '%s' "$msg" | grep -c 'обновлён: 1.0.0 → 1.1.0')"
check "хук сказал, что новое ждёт «да»" 1 "$(printf '%s' "$msg" | grep -c 'ждёт твоего «да»')"
check "не правленный файл обновлён" 1 "$(grep -c 'релиз 1.1.0' "$HOME_/.claude/hooks/safety-guard.sh")"
check "правка человека цела" 1 "$(grep -c 'моя правка' "$HOME_/.claude/hooks/loop-detector.sh")"
check "правка человека не затёрта релизом" 0 "$(grep -c 'релиз 1.1.0' "$HOME_/.claude/hooks/loop-detector.sh")"
check "новый файл в авто не ставится" no "$([ -e "$HOME_/.claude/hooks/new-hook.sh" ] && echo yes || echo no)"
check "версия в состоянии" 1.1.0 "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$HOME_/.claude/claude-tuning.json")"

# по «обнови набор»: ставится и новое; правка человека всё ещё своя
out=$(inst --apply)
check "после «да» новое поставлено" yes "$([ -e "$HOME_/.claude/hooks/new-hook.sh" ] && echo yes || echo no)"
check "правка человека всё ещё своя" 1 "$(printf '%s' "$out" | grep -c '^своя версия.*loop-detector')"

# старая установка без карты сумм: файл от прежнего релиза узнаётся по истории git
python3 - "$HOME_/.claude/claude-tuning.json" <<'PY'
import json,sys
p=sys.argv[1]; d=json.load(open(p)); d.pop("files",None); json.dump(d,open(p,"w"))
PY
git -C "$KIT" checkout -q v1.0.0 -- kit/hooks/safety-guard.sh 2>/dev/null   # у человека лежит файл версии 1.0.0
cp "$KIT/kit/hooks/safety-guard.sh" "$HOME_/.claude/hooks/safety-guard.sh"
git -C "$KIT" checkout -q HEAD -- kit/hooks/safety-guard.sh
out=$(inst --apply)
check "старая версия без карты — обновлена" 1 "$(printf '%s' "$out" | grep -c '^обновлено.*safety-guard')"

echo "update-flow: ok=$ok bad=$bad"
[ "$bad" = 0 ]
