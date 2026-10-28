#!/usr/bin/env python3
"""Страж ключей для Bash (PreToolUse): одно решение и одна безопасная команда.

Вместо отказа там, где можно, — безопасный вид той же команды:
  * `cat .env` одним вызовом → только имена ключей (config-key-inventory.py);
  * `env` / `printenv` без аргументов → только имена переменных;
  * команда печатает одно значение (`printenv TOKEN`, `echo "$PASSWORD"`, `gh auth token`) →
    вывод идёт через secret-mask.py --whole: вместо значения метка ‹secret ИМЯ sha256:… len:…›;
  * дамп окружения, процессов, конфига (`env | grep`, `ps aux`, `docker inspect`, `source .env`) →
    secret-mask.py --strict: скрыто всё, что похоже на секрет, даже у «невинного» имени;
  * ssh, чтение файлов, логи, история шелла, `curl -v` → обычная маска.
Отказ остаётся, где безопасного вида нет: приватный ключ, запись значения в документ, историю
или файл под git.

Зачем один хук, а не несколько. Если две проверки одного события вернут updatedInput, Claude Code
применит ту, что закончилась последней, — порядок не определён (документация хуков, раздел
про PreToolUse). Поэтому стражи вызываются отсюда, а итоговую команду выбирает одна функция route().

    secret-guard-bash.py                 PreToolUse(Bash)
    secret-guard-bash.py --write-tool    PreToolUse(Write|Edit|MultiEdit|NotebookEdit): ключ в текст файла
    secret-guard-bash.py --plan          отладка: план для команды из JSON на stdin

В режиме bypassPermissions вопрос стража не задаётся: команда сразу идёт в безопасном виде.
"""
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HOOKS = os.path.dirname(os.path.abspath(__file__))
MASK = os.path.join(HOOKS, "secret-mask.py")
INVENTORY = os.path.join(HOOKS, "config-key-inventory.py")
GUARDS = [  # (имя, резать ли heredoc-данные перед вызовом)
    ("guard-secret-file-read.sh", False),   # режет heredoc сам
    ("guard-recursive-search.sh", False),
    ("guard-process-env-leak.sh", True),
]

SECRET_VAR = r"[A-Za-z_]*(?:TOKEN|SECRET|PASS(?:WORD|WD)?|PWD|API_?KEY|ACCESS_KEY|PRIVATE_KEY|_KEY|DSN|API_HASH|COOKIE|CREDENTIALS?|AUTH|JWT|BEARER)[A-Za-z0-9_]*"

# ---------------------------------------------------------------- классы команд
# Порядок важен: первое совпадение определяет режим маски.
WHOLE = [  # команда печатает одно значение
    r"(^|[;&|(`]\s*|\s)printenv\s+" + SECRET_VAR + r"\b",
    r"\b(echo|printf)\b[^|;&]*\$\{?(?!#)" + SECRET_VAR,
    r"\bsecurity\s+find-(generic|internet)-password\b[^|;&]*\s-w\b",
    r"\bgh\s+auth\s+token\b|\bgh\s+auth\s+status\b[^|;&]*(-t\b|--show-token)",
    r"\bglab\s+auth\s+status\b[^|;&]*(-t\b|--show-token)",
    r"\bgcloud\s+auth\s+(application-default\s+)?print-(access|identity)-token\b",
    r"\baws\s+configure\s+get\s+\S*(secret|key|token)",
    r"\b(op\s+read|pass\s+show|bw\s+get\s+password|doctl\s+auth\s+token|heroku\s+auth:token|npm\s+token\s+create)\b",
    r"\baz\s+account\s+get-access-token\b",
    # файл-значение: ~/.config/app/token, api_key.txt (не .md и не код — у них расширение)
    r"(^|[;&|(]\s*)(cat|head|tail|less|more|bat)\s+(-\S+\s+)*[^\s|;&]*(token|apikey|api[_-]key|secret|passw|credential)[^/\s|;&.]*(\.(txt|token|key|pass|secret|cred|b64))?(\s|$|[|;&)])",
]
STRICT = [  # дамп, где имя ключа ничего не говорит: окружение, процессы, конфиг целиком
    r"(^|[;&|(`\s])(env|printenv)(\s*$|\s*[|;&)])",
    r"\bdocker\s+(exec\s+\S+\s+)?(env|printenv)\b",
    r"\bps\b[^|;&]*(aux|axu|-ef|-ax\b|\bef\b|args|command|cmd)|\bpgrep\s+(-[a-z]*a|--list-full)",
    r"/proc/[0-9*$][^/\s]*/(environ|cmdline)",
    r"\bdocker(-compose|\s+compose)?\s+[^|;&]*\b(inspect|config)\b",
    r"\bsystemctl\b[^|;&]*\b(show|cat)\b",
    r"\b(aws\s+configure\s+(export-credentials|list)|security\s+find-(generic|internet)-password)\b",
    r"(^|[;&|(]\s*)(source|\.)\s+[^\s;&|]*(\.env\b|env\b|\.envrc)",
    r"\bset\s+-a\b|\benv\s+-0\b",
    r"\bbase64\s+(-d|--decode|-D)\b",
    r"\.env([^A-Za-z0-9]|$)|\.envrc|\.pgpass|\.netrc|credentials|secrets?\.(ya?ml|json|txt)",
]
NORMAL = [  # может напечатать секрет среди прочего — обычная маска
    r"config\.(ya?ml|json|toml|ini)|settings(\.local)?\.json|auth\.json|secrets?([^A-Za-z]|$)|\.npmrc|\.pypirc|mcp\.json",
    r"\.claude\.json|claude\s+mcp\s+(list|get)|codex\s+mcp\s+(list|get)",
    r"\.service([^A-Za-z]|$)|/etc/systemd|Environment=",
    r"[a-z]+://[^ ]*:[^ ]*@",
    r"(^|[;&|(\s])ssh\s",
    r"\b(curl|wget|http|xh)\b[^|;&]*(\s-v\b|--verbose|--trace|\s-i\b|--include|-H\s*[\"']?(authorization|x-api-key|api-key|private-token|cookie|x-auth)|--header[= ][\"']?(authorization|x-api-key|cookie)|\s-u\s|--user\s|\$\{?" + SECRET_VAR + r")",
    r"\bdocker(\s+compose)?\s+logs\b|\bjournalctl\b|\.log\b",
    r"_history\b|(^|[;&|(]\s*)history\b|\bfc\s+-l",
    r"\bset\s+-x\b|\b(bash|sh|zsh)\s+-x\b",
    r"\bgit\s+(remote\s+-v|config\s+(-l|--list|--get))|\.git-credentials|\.docker/config",
    r"\bjwt\b|\bopenssl\s+(enc|base64)",
    # Чтение файла целиком или куском: имя файла о содержимом не говорит (пароль в XML-конфиге,
    # секрет, захардкоженный в исходнике). Запись `cat > f <<EOF` сюда не попадает: после верба > или <<.
    r"(^|[;&|(`]\s*|\s)(cat|head|tail|less|more|bat|nl)(\s+-\S+)*\s+[^\s<>|;&-]",
    r"\bsed\s+(-\S+\s+)*-n\b|\bgit\s+(-C\s+\S+\s+)?show\s+\S*:\S+",
]

WRITE_OK_TARGET = re.compile(
    r"^/dev/(null|stdout|stderr|fd/\d)$|(^|/)\.env([.\-][^/]*)?$|\.env$|\.(key|pem|token|secret|pass|cred|age)$"
    r"|(secret|credential|token|passw|auth)[^/]*$", re.I)
WRITE_BAD_TARGET = re.compile(r"(_history|\.history|/history)$|\.(md|txt|log|jsonl|csv|html?|rst|org)$", re.I)


_RAW = ""


def load_input():
    global _RAW
    raw = _RAW = sys.stdin.read()
    try:
        return raw, json.loads(raw)
    except Exception:
        return raw, None


def heredoc_lines(cmd):
    """(строка, это_данные) — тело heredoc-данных помечено True (см. strip_data_heredocs)."""
    skip, delim = False, None
    for line in cmd.split("\n"):
        if skip:
            if re.match(r"^\s*" + re.escape(delim) + r"\s*$", line):
                skip = False
                yield line, False
            else:
                yield line, True
            continue
        yield line, False
        m = re.search(r"<<-?\s*(\"[^\"]+\"|'[^']+'|[A-Za-z_][A-Za-z0-9_]*)", line)
        if not m:
            continue
        head = line[:m.start()]
        executes = re.search(r"\b(bash|sh|zsh)(\s+-s)?\s*['\"]?\s*$|\b(bash|sh|zsh)\s+-s\b|\bssh\b[^'\"]*$", head)
        data = re.search(r"\b(cat|tee|python3?|node|ruby|perl|jq)\b", head)
        if data and not executes:
            delim = m.group(1).strip("'\"")
            skip = True


def strip_data_heredocs(cmd):
    return "\n".join(l for l, is_data in heredoc_lines(cmd) if not is_data)


def run_guard(name, inp):
    try:
        p = subprocess.run(["bash", os.path.join(HOOKS, name)], input=json.dumps(inp),
                           capture_output=True, text=True, timeout=8)
        out = p.stdout.strip()
        if not out:
            return name, None, ""
        o = json.loads(out)
        hso = o.get("hookSpecificOutput") or {}
        return name, hso.get("permissionDecision"), hso.get("permissionDecisionReason", "")
    except Exception as e:  # страж упал — не молчим, но и работу не роняем
        return name, None, f"(страж {name} не отработал: {type(e).__name__})"


def run_guards(data):
    cmd = data.get("tool_input", {}).get("command", "")
    stripped = strip_data_heredocs(cmd)
    jobs = []
    for name, strip in GUARDS:
        inp = json.loads(json.dumps(data))
        if strip:
            inp["tool_input"]["command"] = stripped
        jobs.append((name, inp))
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        return list(ex.map(lambda j: run_guard(*j), jobs))


def matches(patterns, text):
    return any(re.search(p, text, re.I | re.M) for p in patterns)


def mask_mode(cmd):
    """Какой режим маски нужен команде (None — не нужен)."""
    body = strip_data_heredocs(cmd)
    # хеш/длина значения — не значение: `printf %s "$TOKEN" | sha256sum` маскировать нечего
    digest = re.search(r"\|\s*(sha256sum|shasum|md5sum|wc\b|cut\s+-c\s*1-\d\b|head\s+-c\s*\d\b)", body)
    if matches(WHOLE, body) and not digest:
        return "whole"
    # base64 -d в «секретном» контексте — одно значение; иначе strict
    if not digest and re.search(r"\bbase64\s+(-d|--decode|-D)\b", body) and re.search(
            r"secret|token|passw|\$\{?" + SECRET_VAR, body, re.I):
        return "whole"
    if matches(STRICT, body):
        return "strict"
    if matches(NORMAL, body):
        return "normal"
    return None


def wrap(cmd, mode):
    flag = {"strict": " --strict", "whole": " --whole"}.get(mode, "")
    # Подоболочка: exit не трогает оболочку Bash-инструмента; перевод строки перед } —
    # чтобы heredoc внутри исходной команды закрылся корректно. Код выхода — PIPESTATUS[0].
    return f'( {{ {cmd}\n}} 2>&1 | python3 "{MASK}"{flag}; exit ${{PIPESTATUS[0]}} )'


def safe_single_read(cmd):
    """`cat .env` одним вызовом → список имён ключей (значения не читаются)."""
    if "\n" in cmd:
        return None
    m = re.match(r"^\s*(cat|head|tail|less|more|bat)(\s+-[A-Za-z0-9]+)*\s+([A-Za-z0-9_./~+@-]*\.env[A-Za-z0-9._-]*)\s*$", cmd)
    if m:
        return f"{INVENTORY} {m.group(3)}"
    if re.match(r"^\s*(env|printenv)\s*$", cmd):
        return "env | cut -d= -f1 | sort"
    return None


# ---------------------------------------------------------------- запись секрета в файл
def _resolve(path, cwd):
    path = os.path.expanduser(path.strip("'\""))
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(cwd or os.getcwd(), path))


def tracked_unignored(path):
    """Файл внутри git-дерева и не в .gitignore → значение уедет в коммит."""
    d = os.path.dirname(path) or "."
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    try:
        top = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=3)
        if top.returncode != 0:
            return False
        ign = subprocess.run(["git", "-C", d, "check-ignore", "-q", path], capture_output=True, timeout=3)
        return ign.returncode != 0
    except Exception:
        return False


def bad_write_target(target, cwd):
    if not target or target.startswith("&") or target.startswith("$"):
        return None
    p = _resolve(target, cwd)
    if WRITE_OK_TARGET.search(p):
        return None
    if WRITE_BAD_TARGET.search(p):
        return "документ/лог/история"
    if tracked_unignored(p):
        return "файл под git, не в .gitignore"
    return None


def load_mask():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sm", MASK)
    sm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sm)
    return sm


def secret_write(cmd, cwd):
    """Значение секрета пишется в документ, историю или файл под git."""
    lines = cmd.split("\n")
    # heredoc без кавычек у разделителя раскрывает $VAR → значение уходит в файл
    for i, line in enumerate(lines):
        m = re.search(r"<<-?\s*([A-Za-z_][A-Za-z0-9_]*)\b(?!['\"])", line)
        t = re.search(r"(?:>>?|\btee\s+(?:-a\s+)?)\s*([^\s;&|<>]+)", line)
        if m and t:
            body = []
            for l2 in lines[i + 1:]:
                if l2.strip() == m.group(1):
                    break
                body.append(l2)
            if re.search(r"\$\{?" + SECRET_VAR, "\n".join(body)):
                why = bad_write_target(t.group(1), cwd)
                if why:
                    return t.group(1), why
    body = strip_data_heredocs(cmd)
    for seg in re.split(r"(?:&&|\|\||;|\n)", body):
        if not re.search(r">|\btee\b", seg) or not re.match(r"\s*(echo|printf)\b", seg):
            continue  # значение пишет только производитель-печать; вывод curl/сборки — не значение
        src_secret = re.search(r"\$\{?(?!#)" + SECRET_VAR + r"\b", seg.split(">")[0].split("tee")[0]) \
            or re.search(r"\$\((cat|<)\s*[^)]*\.env", seg)
        literal = False
        try:
            literal = bool(load_mask().scan(seg))
        except Exception:
            pass
        if not (src_secret or literal):
            continue
        for t in re.findall(r"(?:\d?>>?|\btee\s+(?:-a\s+)?)\s*([^\s;&|<>]+)", seg):
            why = bad_write_target(t, cwd)
            if why:
                return t, why
    return None


def already_safe(cmd):
    """Уже безопасный путь — не оборачиваем повторно (идемпотентность). Только настоящий пайп
    в маску или вызов инвентаризации первым словом: упоминание имени в тексте команды не в счёт."""
    if re.search(r"\|\s*(python3\s+)?[\"']?[^\s|;&]*secret-mask\.py[\"']?(\s+--(strict|whole))?\s*(;|\)|$)", cmd, re.M):
        return True
    return "\n" not in cmd and bool(re.match(
        r"^\s*(python3\s+)?[^\s|;&]*config-key-inventory\.py\s+[^|;&`$<>]*$", cmd))


# ---------------------------------------------------------------- маршрутизация
def route(data, guards=None):
    """→ dict(decision=None|'deny'|'ask', command=<новая|None>, reason, context)."""
    ti = data.get("tool_input") or {}
    cmd = ti.get("command") or ""
    bypass = data.get("permission_mode") == "bypassPermissions"
    if not cmd.strip():
        return {}
    if guards is None:
        guards = run_guards(data)
    dec = {n: (d, r) for n, d, r in guards}

    def deny(reason):
        return {"decision": "deny", "reason": reason}

    # 1. Жёсткие отказы без безопасного вида.
    fr_d, fr_r = dec.get("guard-secret-file-read.sh", (None, ""))
    if fr_d == "deny":
        return deny(fr_r)
    w = secret_write(cmd, data.get("cwd"))
    if w:
        return deny(f"🔐 Не записываю секрет в {w[1]}: оттуда он попадёт в git или логи. "
                    "В текст — только имя ключа; значение — в .env, который в .gitignore.")

    # 2. Безопасный вид.
    new = cmd
    if already_safe(new):
        return {}
    single = safe_single_read(new)
    fired = [n for n, d, _ in guards if d in ("ask", "deny")]
    mode = mask_mode(new)
    if single:
        new, how = single, "показываю только имена ключей"
    elif mode or fired:
        mode = mode or "strict"
        new, how = wrap(new, mode), {"whole": "значение скрыто", "strict": "секреты в выводе скрыты",
                                     "normal": "секреты в выводе скрыты"}[mode]
    else:
        how = None
    if new == cmd:
        return {}
    res = {"command": new, "context": "🔐 " + how.capitalize() + "."}
    if fired and not bypass:
        # вне bypass вопрос стража остаётся вопросом, но человек видит уже безопасную команду
        asks = [dec[n][1] for n in fired if dec[n][0] == "ask"]
        if asks:
            res.update(decision="ask", reason=asks[0])
    return res


def emit(data, res):
    if not res:
        return
    hso = {"hookEventName": "PreToolUse"}
    if res.get("command"):
        hso["updatedInput"] = dict(data.get("tool_input") or {}, command=res["command"])
    if res.get("decision"):
        hso["permissionDecision"] = res["decision"]
        hso["permissionDecisionReason"] = res.get("reason", "")
    if res.get("context"):
        hso["additionalContext"] = res["context"]
    print(json.dumps({"hookSpecificOutput": hso}, ensure_ascii=False))


# ---------------------------------------------------------------- Write/Edit
def write_tool(data):
    ti = data.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ""
    parts = [ti.get("content"), ti.get("new_string"), ti.get("new_source")]
    parts += [e.get("new_string") for e in ti.get("edits") or [] if isinstance(e, dict)]
    text = "\n".join(p for p in parts if isinstance(p, str))
    if not text or not path:
        return
    low = path.lower()
    if WRITE_OK_TARGET.search(low) or re.search(r"(^|/)(tests?|__tests__|fixtures?|spec|testdata)/|\.test\.|_test\.|\.spec\.", low):
        return
    sm = load_mask()
    real = []
    for name, val in sm.scan(text):
        core = val.split("-----")[2] if name == "PRIVATE_KEY" and val.count("-----") >= 4 else val
        # плейсхолдер (`sk-xxxxxxxx…`, `<token>`) — не значение: мало разных символов
        if name == "PRIVATE_KEY" or len(set(core)) >= 10 and sm.looks_secret(core, 12):
            real.append(name)
    if real:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"🔐 Не записываю: в тексте для {os.path.basename(path)} похоже на настоящий ключ "
                f"({', '.join(sorted(set(real)))}). Вместо значения — имя ключа или ${{KEY}}; фейк для теста — в tests/.")}}, ensure_ascii=False))


def main():
    args = sys.argv[1:]
    raw, data = load_input()
    if data is None:
        # fail-closed: не смогли разобрать вход — не молчим
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": "🔐 secret-guard-bash: вход не JSON"}}))
        return
    if args[:1] == ["--write-tool"]:
        write_tool(data)
        return
    if data.get("tool_name") not in (None, "Bash"):
        return
    res = route(data)
    if args[:1] == ["--plan"]:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return
    emit(data, res)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # Сбой самого хука — не тихий пропуск: команда выполняется, но вывод идёт через строгую маску.
        try:
            d = json.loads(_RAW) if _RAW else {}
            c = (d.get("tool_input") or {}).get("command")
            if c and d.get("tool_name", "Bash") == "Bash" and sys.argv[1:2] != ["--write-tool"]:
                emit(d, {"command": wrap(c, "strict"),
                         "context": f"🔐 Проверка ключей дала сбой ({type(e).__name__}) — вывод скрыт на всякий случай"})
        except Exception:
            pass
        sys.stderr.write(f"secret-guard-bash: {type(e).__name__}: {e}\n")
