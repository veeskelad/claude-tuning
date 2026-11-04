#!/usr/bin/env bash
# Проверка скилла claude-session-bridge: автосводка из хука, пропуски, маска ключей, вечер, утро, показ
# раз в день, шаблоны launchd. Запуск: bash tests/bridge.test.sh.
# HOME — временная папка, вместо claude — заглушка на PATH: настоящая модель не зовётся,
# ~/.claude, ~/.agents и ~/Library не затрагиваются, расписание не включается.
set -uo pipefail
R="$(cd "$(dirname "$0")/.." && pwd)/kit"
B="$R/skills/claude-session-bridge/scripts"
T=$(mktemp -d)
T=$(cd "$T" && pwd -P)
trap 'rm -rf "$T"' EXIT
PY=$(command -v python3)
export HOME="$T/home" PYTHONDONTWRITEBYTECODE=1
unset CLAUDE_CODE_ENTRYPOINT CLAUDE_TUNING_AUTO_SUMMARY CLAUDE_TUNING_SUMMARY_MODEL CLAUDE_TUNING_SUMMARY_BUDGET CLAUDE_WORK_ROOTS
mkdir -p "$HOME/.claude/hooks" "$HOME/.claude/bin"
cp "$R/hooks/secret-mask.py" "$R/hooks/open-tasks.sh" "$HOME/.claude/hooks/"
cp "$R/bin/handoff-tier.py" "$HOME/.claude/bin/"
printf '# Handoff\n\n## По проектам\n\n## Человеческий контекст\n' > "$HOME/.claude/global-handoff.md"  # каркас install-kit.py
FB="$T/fakebin"; mkdir -p "$FB"
export PATH="$FB:$(dirname "$PY"):/usr/bin:/bin:/usr/sbin:/sbin"
export FAKE_DIR="$T/fake"; mkdir -p "$FAKE_DIR"
STORE="$HOME/.agents/handoffs"
LOG="$HOME/.local/state/claude-tuning/auto-summary.log"
ok=0; bad=0
check() { if [ "$2" = "$3" ]; then ok=$((ok+1)); else bad=$((bad+1)); echo "ПРОМАХ $1: ждали [$2], получили [$3]"; fi; }
has() { grep -qF -- "$2" "$1" 2>/dev/null && echo да || echo нет; }
calls() { grep -c '^=== call' "$FAKE_DIR/calls" 2>/dev/null || echo 0; }
nrec() { find "$STORE" -name '*.json' -not -path '*/receipts/*' 2>/dev/null | wc -l | tr -d ' '; }
now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }

# Синтетические значения собираются по частям: в файле теста их нет целиком.
TOK="gh""p_$(printf 'A1b2C3d4%.0s' 1 2 3 4 5)"
PW="hunter""2-very-secret-pw"
MODEL_KEY="s""k-$(printf 'Zx9Qw%.0s' 1 2 3 4 5)"
export TOK PW MODEL_KEY

# Заглушка claude: argv — в журнал вызовов, stdin — в файл, ответ — как у claude -p --output-format json.
cat > "$FB/claude" <<'EOF'
#!/usr/bin/env bash
{ echo "=== call"; for a in "$@"; do echo "ARG $a"; done; echo "ENV_GUARD ${CLAUDE_TUNING_AUTO_SUMMARY:-}"; } >> "$FAKE_DIR/calls"
cat > "$FAKE_DIR/stdin"
sleep "${FAKE_SLEEP:-0}"
python3 - <<'PY'
import json, os
body = {'skip': False, 'title': 'Правка конфига сервиса', 'goal': 'Поправить конфиг',
        'status': 'in_progress', 'summary': 'Конфиг поправлен, ключ ' + os.environ['MODEL_KEY'] + ' подставлен.',
        'decisions': ['Значения ключей — через переменные окружения'], 'changes': ['/work/app/config.py — правка'],
        'verification': ['тесты не запускались'], 'blockers': [], 'next_steps': ['Запустить тесты'], 'constraints': []}
print(json.dumps({'type': 'result', 'is_error': False, 'structured_output': body, 'total_cost_usd': 0.01}))
PY
EOF
chmod +x "$FB/claude"
check "заглушка claude первой на PATH" "$FB/claude" "$(command -v claude)"

# Транскрипт: full — три просьбы, правка файла, ключ и пароль в тексте; short — две просьбы без правок;
# sdk — как full, но точка входа claude -p. Хвост больше 20 КБ: меньший прирост автосводка пропускает.
mk() {
  python3 - "$@" <<'PY'
import json, os, sys
path, kind = sys.argv[1], sys.argv[2]
entry = 'sdk-cli' if kind == 'sdk' else 'cli'
def line(role, content):
    return json.dumps({'type': role, 'entrypoint': entry, 'message': {'role': role, 'content': content}}, ensure_ascii=False)
L = [line('user', 'поправь конфиг сервиса'), line('user', 'вот токен ' + os.environ['TOK'] + ', подставь')]
if kind != 'short':
    L.append(line('user', 'DB_PASSWORD=' + os.environ['PW'] + ' положи в .env'))
    L.append(line('assistant', [{'type': 'tool_use', 'name': 'Edit', 'input': {'file_path': '/work/app/config.py'}},
                                {'type': 'text', 'text': 'Готово, конфиг поправлен.'}]))
L.append(json.dumps({'pad': 'x' * 25000}))
open(path, 'w').write('\n'.join(L) + '\n')
PY
}
hook_json() { printf '{"session_id":"%s","transcript_path":"%s","cwd":"%s","hook_event_name":"%s"}' "$1" "$2" "$3" "${4:-SessionEnd}"; }
wait_rec() { for _ in $(seq 1 80); do [ "$(nrec)" -ge "$1" ] && return 0; sleep 0.25; done; return 1; }

P="$T/work/app"; mkdir -p "$P"; git -C "$P" init -q 2>/dev/null

# --- 1. хук: отвечает сразу, код 0, сводка пишется в фоне --------------------------------
mk "$T/full.jsonl" full
t0=$(now_ms)
hook_json sess-full "$T/full.jsonl" "$P" | FAKE_SLEEP=2 python3 "$B/auto_summary.py" hook --agent claude
rc=$?; el=$(( $(now_ms) - t0 ))
check "hook: код 0" 0 "$rc"
check "hook: не ждёт модель (меньше 1500 мс, модель спит 2 с)" да "$([ "$el" -lt 1500 ] && echo да || echo "нет, $el мс")"
wait_rec 1
REC=$(find "$STORE" -name '*.json' -not -path '*/receipts/*' | head -1)
check "фоновая сводка записана" 1 "$(nrec)"
check "сводка помечена (автосводка)" да "$(has "$REC" '(автосводка)')"
check "журнал: сохранено" да "$(has "$LOG" 'sess-full: сохранено')"
check "claude -p: потолок бюджета" 1 "$(grep -c '^ARG --max-budget-usd$' "$FAKE_DIR/calls")"
check "claude -p: модель задана" 1 "$(grep -c '^ARG --model$' "$FAKE_DIR/calls")"
check "claude -p: без --bare" 0 "$(grep -c '^ARG --bare$' "$FAKE_DIR/calls")"
check "claude -p: хуки дочернего прогона выключены" 1 "$(grep -c 'disableAllHooks' "$FAKE_DIR/calls")"
check "claude -p: метка дочернего прогона" 1 "$(grep -c '^ENV_GUARD 1$' "$FAKE_DIR/calls")"

# --- 2. ключи не уходят ни в модель, ни в стор -------------------------------------------
check "в модель: правленый файл есть" да "$(has "$FAKE_DIR/stdin" '/work/app/config.py')"
check "в модель: токена нет" нет "$(has "$FAKE_DIR/stdin" "$TOK")"
check "в модель: пароля нет" нет "$(has "$FAKE_DIR/stdin" "$PW")"
check "в модель: вместо значения метка" да "$(has "$FAKE_DIR/stdin" '‹secret')"
check "в сторе: ключа из ответа модели нет" 0 "$(grep -rcF -- "$MODEL_KEY" "$STORE" | awk -F: '{s+=$2} END {print s+0}')"
check "в сторе: токена нет" 0 "$(grep -rcF -- "$TOK" "$STORE" | awk -F: '{s+=$2} END {print s+0}')"
check "в журнале: ключей нет" 0 "$(grep -cF -e "$TOK" -e "$PW" -e "$MODEL_KEY" "$LOG")"

# --- 3. пропуски: без модели и без записи -------------------------------------------------
c0=$(calls)
python3 "$B/auto_summary.py" run --agent claude --session sess-full --transcript "$T/full.jsonl" --cwd "$P"
check "прирост меньше 20 КБ — пропуск" да "$(has "$LOG" 'sess-full: прирост')"
mk "$T/sdk.jsonl" sdk
python3 "$B/auto_summary.py" run --agent claude --session sess-sdk --transcript "$T/sdk.jsonl" --cwd "$P"
check "сессия claude -p (sdk-cli) — пропуск" да "$(has "$LOG" 'sess-sdk: sdk-cli (claude -p) — пропуск')"
mk "$T/short.jsonl" short
python3 "$B/auto_summary.py" run --agent claude --session sess-short --transcript "$T/short.jsonl" --cwd "$P"
check "короткая сессия без правок — пропуск" да "$(has "$LOG" 'sess-short: без правок и меньше 3 просьб')"
hook_json sess-p "$T/full.jsonl" "$P" | CLAUDE_CODE_ENTRYPOINT=sdk-cli python3 "$B/auto_summary.py" hook --agent claude
check "хук внутри claude -p: код 0" 0 "$?"
hook_json sess-child "$T/full.jsonl" "$P" | CLAUDE_TUNING_AUTO_SUMMARY=1 python3 "$B/auto_summary.py" hook --agent claude
sleep 1
check "хук внутри claude -p и дочернего прогона: в фоне ничего" нет "$(grep -qE 'sess-(p|child):' "$LOG" && echo да || echo нет)"
check "пропуски не звали модель" "$c0" "$(calls)"
check "пропуски не писали в стор" 1 "$(nrec)"
check "хук: мусор в stdin — код 0" 0 "$(echo 'не json' | python3 "$B/auto_summary.py" hook --agent claude >/dev/null 2>&1; echo $?)"
check "хук: пустой stdin — код 0" 0 "$(python3 "$B/auto_summary.py" hook </dev/null >/dev/null 2>&1; echo $?)"
check "хук: неизвестный агент — код 0" 0 "$(echo '{}' | python3 "$B/auto_summary.py" hook --agent other >/dev/null 2>&1; echo $?)"
check "хук: нет транскрипта — код 0" 0 "$(hook_json s-x "$T/none.jsonl" "$P" | python3 "$B/auto_summary.py" hook >/dev/null 2>&1; echo $?)"

# --- 4. save-json: сводка, которую написала сама сессия ------------------------------------
J=$(printf '```json\n{"title":"Перенос в новый чат","goal":"Продолжить","status":"blocked","summary":"Ждём доступ.","decisions":[],"changes":[],"verification":["не запускалось"],"blockers":["Нет доступа к стенду"],"next_steps":["Получить доступ"],"constraints":[]}\n```')
out=$(printf '%s' "$J" | python3 "$B/auto_summary.py" save-json --agent claude --session sess-json --cwd "$P")
check "save-json: код 0 и путь записи" да "$([ -f "$out" ] && echo да || echo нет)"
check "save-json: пометка переноса" да "$(has "$out" '(сводка сессии при переносе)')"
check "save-json: статус blocked сохранён" да "$(has "$out" '"status": "blocked"')"

# второй репозиторий и сводка Codex — через handoff.py
P2="$T/work/other-repo"; mkdir -p "$P2"; git -C "$P2" init -q 2>/dev/null
printf '%s' '{"title":"Миграция схемы","goal":"Новая колонка","status":"complete","summary":"Миграция готова.","decisions":[],"changes":[],"verification":["тесты прошли"],"blockers":[],"next_steps":[],"constraints":[]}' > "$T/c.json"
python3 "$B/handoff.py" save --project "$P2" --agent codex --session codex-1 --content-file "$T/c.json" >/dev/null
check "handoff.py save от codex" 3 "$(nrec)"
check "handoff.py: похожее на ключ не сохраняется" 1 "$(printf '{"title":"x","goal":"x","status":"complete","summary":"Bear''er abc","decisions":[],"changes":[],"verification":[],"blockers":[],"next_steps":[],"constraints":[]}' > "$T/bad.json"; python3 "$B/handoff.py" save --project "$P2" --agent codex --session codex-1 --content-file "$T/bad.json" >/dev/null 2>&1; echo $?)"

# --- 5. вечер: global-handoff.md, маркер тиринга, повтор без изменений ----------------------
G="$HOME/.claude/global-handoff.md"
DAY=$(date +%Y-%m-%d)
python3 "$B/daily_digest.py" evening > "$T/ev1.out"
check "evening: добавлено 3" да "$(has "$T/ev1.out" 'добавлено 3, обновлено 0 из 3')"
check "evening: маркер «- **Сессия ДАТА —»" 3 "$(grep -c "^- \*\*Сессия $DAY — " "$G")"
check "evening: секция репозитория app" 1 "$(grep -c '^### app$' "$G")"
check "evening: секция репозитория other-repo" 1 "$(grep -c '^### other-repo$' "$G")"
check "evening: как вернуть сессию claude" да "$(has "$G" 'Вернуть: `claude --resume sess-full`')"
check "evening: как вернуть сессию codex" да "$(has "$G" 'Вернуть: `codex resume codex-1`')"
check "evening: блокер в записи" да "$(has "$G" 'Блокер: Нет доступа к стенду')"
check "evening: хвостовой раздел на месте и последний" '## Человеческий контекст' "$(grep '^## ' "$G" | tail -1)"
check "evening: ключей нет" 0 "$(grep -cF -e "$TOK" -e "$PW" -e "$MODEL_KEY" "$G")"
cp "$G" "$T/g1"
python3 "$B/daily_digest.py" evening > "$T/ev2.out"
check "evening повторно: ничего нового" да "$(has "$T/ev2.out" 'добавлено 0, обновлено 0 из 3')"
check "evening повторно: файл тот же" "$(shasum < "$T/g1")" "$(shasum < "$G")"
check "handoff-tier видит записи свежими" да "$(python3 "$HOME/.claude/bin/handoff-tier.py" | grep -q 'hot=3 ' && echo да || echo нет)"
# новая версия задачи заменяет свою запись, а не добавляет вторую
REC_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$REC")
python3 -c 'import json,sys; b=json.load(open(sys.argv[1]))["content"]; b["summary"]="Конфиг поправлен и проверен."; json.dump(b, open(sys.argv[2],"w"), ensure_ascii=False)' "$REC" "$T/upd.json"
python3 "$B/handoff.py" save --project "$P" --agent claude --session sess-full --supersedes "$REC_ID" --content-file "$T/upd.json" >/dev/null
python3 "$B/daily_digest.py" evening > "$T/ev3.out"
check "evening: новая версия обновляет запись" да "$(has "$T/ev3.out" 'добавлено 0, обновлено 1 из 3')"
check "evening: записей по-прежнему 3" 3 "$(grep -c "^- \*\*Сессия $DAY — " "$G")"
check "evening: текст новой версии" да "$(has "$G" 'Конфиг поправлен и проверен.')"

# --- 6. утро: morning-digest.md ---------------------------------------------------------------
mkdir -p "$HOME/Work/app/.tasks"
printf '# Демо-задача\n\n- [x] сделано\n- [ ] следующий пункт\n' > "$HOME/Work/app/.tasks/demo.md"
D="$HOME/.claude/morning-digest.md"
python3 "$B/daily_digest.py" morning >/dev/null
check "morning: файл собран" да "$([ -s "$D" ] && echo да || echo нет)"
check "morning: блокеры" да "$(has "$D" '## Блокеры')"
check "morning: блокер с проектом" да "$(has "$D" 'app: Перенос в новый чат — Нет доступа к стенду')"
check "morning: задачи по проектам" 2 "$(grep -cE '^### (app|other-repo)$' "$D")"
check "morning: следующий шаг" да "$(has "$D" '→ Запустить тесты')"
check "morning: чек-листы" да "$(has "$D" 'следующий пункт')"

# --- 7. show: раз в день, молчит в claude -p ------------------------------------------------
SHOWN="$HOME/.local/state/claude-tuning/digest-shown"
o1=$(CLAUDE_CODE_ENTRYPOINT=sdk-cli python3 "$B/daily_digest.py" show; echo "rc=$?")
check "show в claude -p: молчит, код 0" rc=0 "$o1"
check "show в claude -p: показ не засчитан" нет "$([ -e "$SHOWN" ] && echo да || echo нет)"
o2=$(python3 "$B/daily_digest.py" show)
check "show: первая сессия дня видит сводку" да "$(printf '%s' "$o2" | grep -q '^# Утро' && echo да || echo нет)"
o3=$(python3 "$B/daily_digest.py" show; echo "rc=$?")
check "show: второй раз за день молчит" rc=0 "$o3"
rm -f "$SHOWN"; touch -t 202601010000 "$D"
python3 "$B/daily_digest.py" show >/dev/null
check "show: старая сводка пересобрана" да "$([ "$(find "$D" -mmin -5 | wc -l | tr -d ' ')" = 1 ] && echo да || echo нет)"
o4=$(HOME="$T/empty" python3 "$B/daily_digest.py" show; echo "rc=$?")
check "show: пустой стор — молчит, код 0" rc=0 "$o4"

# --- 8. шаблоны launchd -------------------------------------------------------------------
if command -v plutil >/dev/null 2>&1; then
  for spec in handoff-evening:23:30:evening morning-digest:7:0:morning; do
    IFS=: read -r t h m mode <<< "$spec"
    sed -e "s#__HOME__#/home/example#g" -e "s#__LABEL_PREFIX__#com.example#g" "$R/launchd/$t.plist.template" > "$T/$t.plist"
    check "$t.plist: plutil" OK "$(plutil -lint "$T/$t.plist" | sed 's/.*: //')"
    check "$t.plist: плейсхолдеры подставлены" 0 "$(grep -c '__HOME__\|__LABEL_PREFIX__' "$T/$t.plist")"
    check "$t.plist: метка" "com.example.$t" "$(plutil -extract Label raw "$T/$t.plist")"
    check "$t.plist: время" "$h:$m" "$(plutil -extract StartCalendarInterval.Hour raw "$T/$t.plist"):$(plutil -extract StartCalendarInterval.Minute raw "$T/$t.plist")"
    check "$t.plist: режим $mode" 1 "$(plutil -extract ProgramArguments.2 raw "$T/$t.plist" | grep 'claude-session-bridge/scripts/daily_digest.py' | grep -c "\"\$f\" $mode\$")"
    check "$t.plist: журнал в ~/Library/Logs" "/home/example/Library/Logs/com.example.$t.log" "$(plutil -extract StandardOutPath raw "$T/$t.plist")"
  done
else
  echo "plutil нет (не macOS) — шаблоны launchd не проверены"
fi

# --- 9. модульные тесты скилла -------------------------------------------------------------
check "unittest test_handoff test_auto_summary" 0 "$(cd "$B" && python3 -m unittest -q test_handoff test_auto_summary >/dev/null 2>&1; echo $?)"
check "pycache в kit не появился" 0 "$(find "$R/skills/claude-session-bridge" -name __pycache__ | wc -l | tr -d ' ')"

echo "bridge: $ok верно, $bad промахов"
[ "$bad" -eq 0 ]
