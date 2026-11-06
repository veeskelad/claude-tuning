#!/usr/bin/env bash
# SessionStart: раз в сутки проверить, не вышла ли новая версия claude-tuning, и сказать одной строкой.
# По умолчанию сам ничего не обновляет: обновление меняет настройки, его запускает человек фразой «обнови набор»
# (SKILL.md, раздел «Обновление набора»). Сеть недоступна или набор поставлен не клоном git — молчит.
# Автообновление (по желанию): CLAUDE_TUNING_AUTO_UPDATE=1 или `install-kit.py --auto-update on`. Тогда хук сам подтянет
# новую версию и обновит только то, что уже стоит и не правилось человеком (`install-kit.py --auto`); новые хуки, задачи
# по расписанию и настройки ждут «обнови набор».
# Выключить проверку: CLAUDE_TUNING_UPDATE_CHECK=0.
set -u
[ "${CLAUDE_TUNING_UPDATE_CHECK:-1}" = 0 ] && exit 0

state="$HOME/.claude/claude-tuning.json"
stamp="$HOME/.claude/.claude-tuning-update-check"
[ -r "$state" ] || exit 0

# не чаще раза в сутки: fetch на каждом старте сессии — лишняя задержка
if [ -f "$stamp" ] && [ -n "$(find "$stamp" -mmin -1440 2>/dev/null)" ]; then
  exit 0
fi
touch "$stamp" 2>/dev/null

kit=$(sed -n 's/.*"kit": *"\([^"]*\)".*/\1/p' "$state" | head -1)
have=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$state" | head -1)
[ -n "$kit" ] || exit 0
{ [ -d "$kit/.git" ] || [ -f "$kit/.git" ]; } || exit 0

# короткий таймаут без утилиты timeout (в macOS её нет): git сам рвёт медленное соединение
git -C "$kit" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=10 fetch -q --tags origin 2>/dev/null || exit 0
latest=$(git -C "$kit" tag -l 'v[0-9]*' --sort=-v:refname 2>/dev/null | head -1)
latest=${latest#v}
[ -n "$latest" ] && [ -n "$have" ] || exit 0
[ "$latest" = "$have" ] && exit 0
# новее ли: сравнение версий сортировкой (sort -V есть и в macOS, и в Linux)
newest=$(printf '%s\n%s\n' "$have" "$latest" | sort -V | tail -1)
[ "$newest" = "$latest" ] || exit 0

auto=${CLAUDE_TUNING_AUTO_UPDATE:-}
if [ -z "$auto" ] && grep -q '"auto_update": *true' "$state" 2>/dev/null; then auto=1; fi
if [ "$auto" = 1 ]; then
  tracks=$(python3 -c 'import json,sys; print(",".join(json.load(open(sys.argv[1])).get("tracks", [])))' "$state" 2>/dev/null)
  # только перемотка вперёд: свои правки в клоне набора не трогаем
  if git -C "$kit" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=10 pull -q --ff-only origin 2>/dev/null \
     && out=$(python3 "$kit/scripts/install-kit.py" ${tracks:+--with "$tracks"} --auto 2>&1); then
    waits=$(printf '%s\n' "$out" | grep -c '^ждёт «да»')
    msg="🔄 claude-tuning обновлён: $have → $latest. Что нового — в CHANGELOG.md набора."
    [ "${waits:-0}" -gt 0 ] && msg="$msg Новое в наборе ($waits) ждёт твоего «да»: скажи «обнови набор»."
    echo "$msg"
    exit 0
  fi
  echo "⚠️ claude-tuning: автообновление до $latest не прошло (в клоне набора свои правки или нет сети). Скажи «обнови набор» — разберёмся."
  exit 0
fi
echo "🆕 claude-tuning: вышла версия $latest (у тебя $have). Что нового — в CHANGELOG.md набора. Обновить: скажи «обнови набор» — покажу, что изменится, и ничего не поменяю без твоего «да»."
