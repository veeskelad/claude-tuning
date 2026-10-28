#!/usr/bin/env python3
"""Классы утечек ключей через цепочку PreToolUse-хуков, поставленную установщиком.

    python3 tests/leak-replay.py --home DIR [-v] [фильтр-по-id]

DIR — домашний каталог, куда поставлен набор (`scripts/install-kit.py --home DIR --apply`).
Для каждого кейса:
  1. строим вход хука (режим bypassPermissions — самый жёсткий: вопросов нет) и прогоняем все
     хуки из DIR/.claude/settings.json, чей matcher подходит к инструменту;
  2. сводим решения по правилу Claude Code (deny > ask > allow) и считаем, сколько хуков вернули
     updatedInput (больше одного — гонка: какой применится, не определено);
  3. если команда не запрещена — выполняем итоговую команду в песочнице: фейковый HOME, заглушки
     внешних утилит, фейковые значения той же формы, что у настоящих;
  4. кейс зелёный, если ни одно фейковое значение (ни как есть, ни в base64) не попало в вывод,
     либо если ожидался и получен deny.

Настоящих значений тут нет: фейки генерируются заново при каждом запуске. Песочница остаётся во
временном каталоге системы (путь печатается в конце): тест ничего не удаляет.
"""
import base64
import concurrent.futures as cf
import json
import os
import re
import secrets
import string
import subprocess
import sys
import tempfile
import uuid

ARGS = sys.argv[1:]
if "--home" not in ARGS or ARGS.index("--home") + 1 >= len(ARGS):
    sys.exit("нужен --home DIR: каталог, куда поставлен набор (install-kit.py --home DIR --apply)")
KIT_HOME = os.path.abspath(ARGS[ARGS.index("--home") + 1])
SETTINGS = os.path.join(KIT_HOME, ".claude/settings.json")
MASK = os.path.join(KIT_HOME, ".claude/hooks/secret-mask.py")
VERBOSE = "-v" in ARGS
FILTER = [a for i, a in enumerate(ARGS) if not a.startswith("-") and (i == 0 or ARGS[i - 1] != "--home")]

A = string.ascii_letters + string.digits


def rnd(n, alpha=A):
    while True:
        s = "".join(secrets.choice(alpha) for _ in range(n))
        if alpha != A or (re.search(r"[a-z]", s) and re.search(r"[A-Z]", s) and re.search(r"\d", s)):
            return s


FAKE = {
    "PROVIDER_API_KEY": "sk-or-v1-" + secrets.token_hex(32),
    "BOT_TOKEN": str(secrets.randbelow(9 * 10**9) + 10**9) + ":AA" + rnd(33, A + "_-"),
    "NOTION_TOKEN": "ntn_" + rnd(45),
    "DB_PASSWORD": rnd(16),
    "CACHE_CH": rnd(22),                  # имя без «секретного» слова
    "REDIS_PW": secrets.token_hex(12),
    "MCP_KEY": str(uuid.uuid4()),
    "GH_TOK": "ghp_" + rnd(36),
    "JWT": "eyJ" + rnd(20) + ".eyJ" + rnd(30) + "." + rnd(40),
    "SHORT_PW": rnd(12),
    "TG_HASH": secrets.token_hex(16),
    "AWS_SECRET": rnd(40),
    "PAY_KEY": rnd(40),
    "PANEL_PRIV": rnd(43),
    "PEM_LINE": rnd(64),
}
PEM = ("-----BEGIN OPENSSH PRIVATE" + " KEY-----\n" + FAKE["PEM_LINE"] + "\n" + rnd(64) +
       "\n-----END OPENSSH PRIVATE" + " KEY-----\n")


def b64(s):
    return base64.b64encode(s.encode()).decode()


# ---------------------------------------------------------------- песочница
STUB = r'''#!/usr/bin/env python3
import os, sys, json, base64, subprocess, re
F = {k[5:]: v for k, v in os.environ.items() if k.startswith("FAKE_")}
T = os.environ["SANDBOX"]
name = os.path.basename(sys.argv[0]); a = sys.argv[1:]; s = " ".join(a)
def remote(cmd):
    cmd = re.sub(r"(?<![\w.])/(opt|root|etc|srv|var)/", T + "/remote/\\1/", cmd)
    cmd = cmd.replace("~/", T + "/remote/home/")
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    sys.stdout.write(r.stdout); sys.stderr.write(r.stderr); sys.exit(r.returncode)
if name == "ssh":
    i = 0
    while i < len(a) and a[i].startswith("-"):
        i += 2 if a[i] in ("-o", "-i", "-p", "-l", "-J", "-F") else 1
    remote(" ".join(a[i + 1:]) if len(a) > i + 1 else sys.stdin.read())
elif name == "docker":
    if "inspect" in a:
        print(f"DB_PASSWORD={F['DB_PASSWORD']}\nCACHE_CH={F['CACHE_CH']}\nPATH=/usr/bin")
    elif "config" in a and "--services" not in a:
        print(f"services:\n  bot:\n    environment:\n      BOT_TOKEN: {F['BOT_TOKEN']}\n      CACHE_CH: {F['CACHE_CH']}\n      REDIS_URL: redis://:{F['REDIS_PW']}@redis:6379/0")
    elif "config" in a:
        print("bot\nredis")
    elif "logs" in a:
        print(f"INFO start\nDEBUG POST /v1 headers={{'Authorization': 'Bearer {F['PROVIDER_API_KEY']}'}}\nWARN retry password={F['DB_PASSWORD']}")
    elif "exec" in a and ("env" in a or "printenv" in a):
        print(f"HOSTNAME=abc\nBOT_TOKEN={F['BOT_TOKEN']}\nCACHE_CH={F['CACHE_CH']}")
    elif "ps" in a:
        print("CONTAINER ID   IMAGE   NAMES\n1a2b3c4d5e6f   app  bot")
elif name == "systemctl":
    print(f"ActiveState=active\nEnvironment=REDIS_URL=redis://:{F['REDIS_PW']}@127.0.0.1:6379 CACHE_CH={F['CACHE_CH']}")
elif name == "journalctl":
    print(f"sep 28 app[1]: connect redis://:{F['REDIS_PW']}@127.0.0.1\nsep 28 app[1]: auth X-Api-Key: {F['PAY_KEY']}")
elif name == "claude":
    print(f"search: https://mcp.example.ai/mcp?apiKey={F['MCP_KEY']} (HTTP) - ✓ Connected\nscraper: https://mcp.example.com/?token={F['GH_TOK']}")
elif name == "gh":
    print(F["GH_TOK"])
elif name == "security":
    print(F["SHORT_PW"])
elif name == "curl":
    hdr = [a[i + 1] for i, x in enumerate(a) if x in ("-H", "--header") and i + 1 < len(a)]
    if "-v" in a or "--verbose" in a:
        for h in hdr:
            sys.stderr.write("> " + h + "\n")
        sys.stderr.write("< HTTP/2 200\n")
    print(json.dumps({"node": "ams", "rawInbound": {"privateKey": F["PANEL_PRIV"], "shortId": "ab12"}}))
elif name in ("ps", "pgrep"):
    print(f"root 101 python3 warmup.py --api-key {F['PAY_KEY']}\nroot 102 docker exec -e ADMIN_PASSWORD={F['SHORT_PW']} -e CACHE_STATUS={F['CACHE_CH']} app warmup")
'''
STUB_NAMES = ["ssh", "docker", "systemctl", "journalctl", "claude", "gh", "security", "curl", "ps", "pgrep"]


def build_sandbox():
    T = tempfile.mkdtemp(prefix="leak-replay-")
    j = lambda *p: os.path.join(T, *p)
    for d in ("bin", "home/.ssh", "home/.config/app", "proj/src", "proj/docs", "proj/conf",
              "remote/opt/app", "remote/home/.app", "proc/1234", "git"):
        os.makedirs(j(*d.split("/")), exist_ok=True)
    stub = j("bin", "fakecli.py")
    open(stub, "w").write(STUB)
    os.chmod(stub, 0o755)
    for n in STUB_NAMES:
        os.symlink(stub, j("bin", n))
    F = FAKE
    env_body = (f"PROVIDER_API_KEY={F['PROVIDER_API_KEY']}\nBOT_TOKEN={F['BOT_TOKEN']}\n"
                f"CACHE_CH={F['CACHE_CH']}\nDATABASE_URL=postgres://app:{F['DB_PASSWORD']}@db:5432/app\n"
                f"LOG_LEVEL=info\n")
    W = lambda p, s, m=0o644: (open(j(*p.split("/")), "w").write(s), os.chmod(j(*p.split("/")), m))
    W("proj/.env", env_body)
    W("proj/.env.production", env_body)
    W("proj/.env.backup", env_body + "BACKEND_HOST=10.9.8.7\n")
    W("proj/broken.env", f"LOG_LEVEL=info\nPANEL_JWT=  {F['JWT']}\n")
    W("proj/config.yaml", f"model: x\nmcp_servers:\n  notion:\n    env:\n      NOTION_TOKEN: {F['NOTION_TOKEN']}\n"
                          f"      TELEGRAM_API_HASH: {F['TG_HASH']}\n")
    W("proj/conf/db.xml", f"<db><users><default><password><![CDATA[{F['SHORT_PW']}]]></password></default></users></db>\n")
    W("proj/conf/server.xml", f"<server><db><password>{F['SHORT_PW']}</password></db></server>\n")
    W("proj/src/app.ts", "export const token = process.env.API_TOKEN;\nconst x = 1;\n")
    W("proj/src/oauth.ts", f"export const clientSecret = '{F['AWS_SECRET']}';\n")
    W("proj/app.log", f"2026-09-28 INFO start\n2026-09-28 DEBUG Authorization: Bearer {F['PROVIDER_API_KEY']}\n"
                      f"2026-09-28 DEBUG redis://:{F['REDIS_PW']}@127.0.0.1:6379\n")
    W("home/.bash_history", f"export PROVIDER_API_KEY={F['PROVIDER_API_KEY']}\nmysql -uroot -p{F['SHORT_PW']} -e 'select 1'\n"
                            f"redis-cli -a {F['REDIS_PW']} ping\nls -la\n")
    W("home/.claude.json", json.dumps({"mcpServers": {"search": {"type": "http", "url": f"https://mcp.example.ai/mcp?apiKey={F['MCP_KEY']}"}}}, indent=1))
    W("home/.netrc", f"machine api.example.test login bot password {F['SHORT_PW']}\n", 0o600)
    W("home/.pgpass", f"db.example.test:5432:app:app:{F['DB_PASSWORD']}\n", 0o600)
    W("home/.config/app/token", F["PAY_KEY"] + "\n", 0o600)
    W("home/.ssh/id_ed25519", PEM, 0o600)
    W("remote/opt/app/.env", env_body)
    W("remote/home/.app/config.yaml", f"mcp_servers:\n  notion:\n    env:\n      NOTION_TOKEN: {F['NOTION_TOKEN']}\n")
    W("proc/1234/environ", f"PATH=/usr/bin\0CACHE_CH={F['CACHE_CH']}\0BOT_TOKEN={F['BOT_TOKEN']}\0")
    # git-репозиторий с закоммиченным .env (git show файла с ключами) и неигнорируемым конфигом
    g = j("git")
    subprocess.run(["git", "init", "-q", g])
    open(os.path.join(g, ".env"), "w").write(env_body)
    open(os.path.join(g, ".gitignore"), "w").write("*.token\n")
    subprocess.run(["git", "-C", g, "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", g, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"], capture_output=True)
    return T


# ---------------------------------------------------------------- цепочка хуков
def hooks_for(tool):
    d = json.load(open(SETTINGS))
    out = []
    for m in d.get("hooks", {}).get("PreToolUse", []):
        mt = m.get("matcher") or "*"
        if mt == "*" or re.fullmatch(mt, tool):
            out += [h["command"] for h in m.get("hooks", []) if h.get("type") == "command" and h.get("command")]
    return out


def hook_env():
    env = dict(os.environ, HOME=KIT_HOME)
    env.update({k: v for k, v in json.load(open(SETTINGS)).get("env", {}).items()})
    return env


def run_hook(cmd, payload):
    try:
        p = subprocess.run(["bash", "-c", cmd], input=json.dumps(payload), capture_output=True, text=True,
                           timeout=30, env=hook_env())
        o = p.stdout.strip()
        if p.returncode == 2:
            return {"permissionDecision": "deny", "permissionDecisionReason": p.stderr[:200]}
        if not o.startswith("{"):
            return {}
        return json.loads(o).get("hookSpecificOutput") or {}
    except Exception as e:
        return {"error": str(e)}


def chain(tool, tool_input, cwd):
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
               "permission_mode": "bypassPermissions", "cwd": cwd, "session_id": "leak-replay"}
    cmds = hooks_for(tool)
    with cf.ThreadPoolExecutor(max_workers=max(1, len(cmds))) as ex:
        res = list(ex.map(lambda c: run_hook(c, payload), cmds))
    rank = {"deny": 4, "defer": 3, "ask": 2, "allow": 1}
    dec, reason = None, ""
    for r in res:
        d = r.get("permissionDecision")
        if d and rank.get(d, 0) > rank.get(dec, 0):
            dec, reason = d, r.get("permissionDecisionReason", "")
    ups = [r["updatedInput"] for r in res if r.get("updatedInput")]
    distinct = {json.dumps(u, sort_keys=True) for u in ups}
    final = ups[-1] if ups else tool_input
    return dec or "allow", reason, final, len(distinct)


# ---------------------------------------------------------------- кейсы
# (id, класс, команда, ожидание) ожидание: leak0 — выполнить, значений в выводе быть не должно;
# deny — должна быть запрещена; same — обычная команда: вывод не должен измениться маской.
CASES = [
    ("env-cat", "дамп env", "cat .env", "leak0"),
    ("env-head", "дамп env", "head -5 .env.production", "leak0"),
    ("env-grep", "дамп env", "grep -E 'API|TOKEN|CACHE' .env", "leak0"),
    ("env-bare", "дамп env", "env", "leak0"),
    ("env-grep-pipe", "дамп env", "env | grep -iE 'token|cache|key'", "leak0"),
    ("env-source-broken", "source env", "set -a; . ./broken.env; set +a; echo loaded", "leak0"),
    ("cfg-yaml", "дамп конфига", "cat config.yaml", "leak0"),
    ("cfg-python", "дамп конфига", "python3 -c \"import yaml; print(yaml.safe_load(open('config.yaml')))\"", "leak0"),
    ("cfg-xml-users", "XML-пароль", "cat conf/db.xml", "leak0"),
    ("cfg-xml-clean-name", "XML-пароль", "cat conf/server.xml", "leak0"),
    ("ssh-env", "ssh-вывод", "ssh app1 'cat /opt/app/.env'", "leak0"),
    ("ssh-grep", "ssh-вывод", "ssh -o BatchMode=yes app1 'grep -h DATABASE_URL /opt/app/.env'", "leak0"),
    ("ssh-config", "ssh-вывод", "ssh app1 'cat ~/.app/config.yaml'", "leak0"),
    ("ssh-pgrep", "аргументы процессов", "ssh app1 'pgrep -af warmup | head -3'", "leak0"),
    ("docker-inspect-env", "docker inspect", "docker inspect db --format '{{range .Config.Env}}{{println .}}{{end}}'", "leak0"),
    ("docker-compose-config", "compose config", "docker compose config", "leak0"),
    ("docker-exec-env", "дамп env", "docker exec bot env", "leak0"),
    ("docker-logs", "логи", "docker logs bot --tail 20", "leak0"),
    ("ps-aux", "аргументы процессов", "ps aux | grep warmup", "leak0"),
    ("pgrep-a", "аргументы процессов", "pgrep -af warmup", "leak0"),
    ("proc-environ", "аргументы процессов", "cat /proc/1234/environ", "leak0"),
    ("systemctl-show", "systemd Environment=", "systemctl show app-bot", "leak0"),
    ("journalctl", "логи", "journalctl -u app-bot -n 20", "leak0"),
    ("app-log", "логи", "tail -n 20 app.log", "leak0"),
    ("git-show-env", "git show файла с ключами", "git -C ../git show HEAD:.env", "leak0"),
    ("git-log-p-env", "git show файла с ключами", "git -C ../git log -p -- .env", "leak0"),
    ("grep-recursive", "рекурсивный grep", "grep -rn 10.9.8.7 .", "leak0"),
    ("mcp-list", "MCP-URL с ключом", "claude mcp list", "leak0"),
    ("mcp-json", "MCP-URL с ключом", "jq . ~/.claude.json", "leak0"),
    ("curl-v", "curl -v с Authorization", "curl -v -H \"Authorization: Bearer $PROVIDER_API_KEY\" https://api.example.test/v1/models", "leak0"),
    ("curl-body", "ответ API", "curl -s -H \"X-Api-Key: $PAY_API_KEY\" https://panel.example.test/api/nodes/1 | head -5", "leak0"),
    ("set-x", "трассировка шелла", "set -x; curl -s -H \"Authorization: Bearer $PROVIDER_API_KEY\" https://api.example.test >/dev/null", "leak0"),
    ("history", "история шелла", "tail -n 20 ~/.bash_history", "leak0"),
    ("netrc", "файл с паролем", "cat ~/.netrc", "leak0"),
    ("pgpass", "файл с паролем", "cat ~/.pgpass", "leak0"),
    ("token-file", "одно значение", "cat ~/.config/app/token", "leak0"),
    ("printenv-one", "одно значение", "printenv PROVIDER_API_KEY", "leak0"),
    ("echo-var", "одно значение", "echo \"$DB_PASSWORD\"", "leak0"),
    ("echo-b64", "base64/JWT", "printenv BOT_TOKEN | base64", "leak0"),
    ("b64-decode", "base64/JWT", "echo " + b64(FAKE["CACHE_CH"]) + " | base64 -d", "leak0"),
    ("jwt-echo", "base64/JWT", "echo $PANEL_JWT", "leak0"),
    ("gh-token", "одно значение", "gh auth token", "leak0"),
    ("keychain", "одно значение", "security find-generic-password -s app -w", "leak0"),
    ("ssh-key", "приватный ключ", "cat ~/.ssh/id_ed25519", "deny"),
    ("write-md", "запись в файл", "echo \"$BOT_TOKEN\" >> docs/notes.md", "deny"),
    ("write-history", "запись в файл", "printf '%s\\n' \"$BOT_TOKEN\" | tee -a ~/.bash_history", "deny"),
    ("write-tracked", "запись в файл", "echo \"TOKEN=$BOT_TOKEN\" > ../git/app.conf", "deny"),
    ("write-heredoc-md", "запись в файл", "cat > docs/setup.md <<EOF\nключ: $PROVIDER_API_KEY\nEOF", "deny"),
    ("write-token-file", "запись в файл", "umask 077; echo \"$BOT_TOKEN\" > ../git/bot.token; wc -c < ../git/bot.token", "leak0"),
    ("hardcoded-src", "ключ в исходнике", "sed -n 1,5p src/oauth.ts", "leak0"),
    # обычные команды: маска не должна менять вывод
    ("ok-ls", "обычная", "ls", "same"),
    ("ok-grep-code", "обычная", "grep -n token src/app.ts", "same"),
    ("ok-git-log", "обычная", "git -C ../git log --oneline", "same"),
    ("ok-printf-sha", "обычная", "printf %s \"$BOT_TOKEN\" | shasum -a 256 | cut -c1-12", "same"),
    ("ok-len", "обычная", "echo ${#BOT_TOKEN}", "same"),
    ("ok-compose-services", "обычная", "docker compose config --services", "same"),
    ("ok-docker-ps", "обычная", "docker ps", "same"),
    ("ok-heredoc-data", "обычная", "cat > docs/n.txt <<'EOF'\nпотом env | grep X и ps aux\nEOF\nwc -c < docs/n.txt", "same"),
]
WRITE_CASES = [  # (id, путь, содержимое, ожидание)
    ("wt-md", "docs/notes.md", "ключ провайдера: " + FAKE["PROVIDER_API_KEY"] + "\n", "deny"),
    ("wt-memory", "notes/memory/note.md", "NOTION_TOKEN=" + FAKE["NOTION_TOKEN"], "deny"),
    ("wt-pem", "infra/deploy_key", PEM, "deny"),
    ("wt-env", ".env", "BOT_TOKEN=" + FAKE["BOT_TOKEN"] + "\n", "allow"),
    ("wt-test", "tests/test_mask.py", "FAKE = '" + FAKE["GH_TOK"] + "'\n", "allow"),
    ("wt-placeholder", "docs/setup.md", "PROVIDER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n", "allow"),
    ("wt-code", "src/config.py", "api_key = settings.provider.api_key\n", "allow"),
]


def leaked(text):
    hits = []
    for k, v in FAKE.items():
        for form in (v, b64(v)):
            if len(form) >= 8 and form in text:
                hits.append(k)
    if FAKE["PEM_LINE"] in text:
        hits.append("PEM")
    return sorted(set(hits))


def execute(T, command):
    env = {"HOME": os.path.join(T, "home"), "PATH": f"{T}/bin:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
           "SANDBOX": T, "LANG": "en_US.UTF-8", "TERM": "dumb"}
    env.update({"FAKE_" + k: v for k, v in FAKE.items()})
    env.update({"PROVIDER_API_KEY": FAKE["PROVIDER_API_KEY"], "BOT_TOKEN": FAKE["BOT_TOKEN"],
                "DB_PASSWORD": FAKE["DB_PASSWORD"], "CACHE_CH": FAKE["CACHE_CH"], "PAY_API_KEY": FAKE["PAY_KEY"],
                "PANEL_JWT": FAKE["JWT"]})
    command = command.replace("/proc/1234/", f"{T}/proc/1234/")
    try:
        p = subprocess.run(["bash", "-c", command], cwd=os.path.join(T, "proj"), env=env,
                           capture_output=True, text=True, timeout=20)
        return p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        return "(timeout)", 124


def masked_view(s, n=160):
    p = subprocess.run(["python3", MASK, "--strict"], input=s, capture_output=True, text=True)
    return p.stdout.replace("\n", " ⏎ ")[:n]


def main():
    if not os.path.exists(SETTINGS):
        sys.exit(f"нет {SETTINGS}: сначала install-kit.py --home {KIT_HOME} --apply")
    T = build_sandbox()
    rows, red = [], []
    for cid, cls, cmd, expect in CASES:
        if FILTER and not any(f in cid for f in FILTER):
            continue
        dec, reason, final, n_up = chain("Bash", {"command": cmd}, os.path.join(T, "proj"))
        fcmd = final.get("command", cmd)
        rewritten = fcmd != cmd
        status, note = "GREEN", ""
        if n_up > 1:
            status, note = "RED", f"гонка: {n_up} разных updatedInput"
        elif dec == "deny":
            if expect == "same":
                status, note = "RED", "обычная команда запрещена"
            else:
                note = "deny" + ("" if expect == "deny" else " (ожидали безопасный вид)")
                if expect != "deny":
                    status = "YELLOW"
        elif expect == "deny":
            out, _ = execute(T, fcmd)
            lk = leaked(out)
            status, note = ("RED", "ждали deny; утекло: " + ",".join(lk)) if lk else ("YELLOW", "ждали deny, но значение не утекло")
        else:
            out, rc = execute(T, fcmd)
            lk = leaked(out)
            if lk:
                status, note = "RED", "в выводе: " + ",".join(lk)
            elif expect == "same":
                base, _ = execute(T, cmd)
                if base != out:
                    status, note = "RED", "маска изменила вывод обычной команды"
                else:
                    note = "вывод не изменён" + (" (обёрнута)" if rewritten else "")
            else:
                note = ("переписана" if rewritten else "как есть") + f", rc={rc}"
            if VERBOSE:
                note += "\n        cmd: " + masked_view(fcmd, 140) + "\n        out: " + masked_view(out)
        rows.append((status, cid, cls, dec, note))
        if status == "RED":
            red.append(cid)
    for cid, path, content, expect in WRITE_CASES:
        if FILTER and not any(f in cid for f in FILTER):
            continue
        full = os.path.join(T, "proj", path)
        dec, reason, _, _ = chain("Write", {"file_path": full, "content": content}, os.path.join(T, "proj"))
        ok = (dec == "deny") == (expect == "deny")
        rows.append(("GREEN" if ok else "RED", cid, "Write-инструмент", dec, f"ждали {expect}"))
        if not ok:
            red.append(cid)
    w = max(len(r[1]) for r in rows)
    for st, cid, cls, dec, note in rows:
        print(f"{st:6} {cid:{w}}  {cls:26} {dec:6} {note}")
    cnt = {s: sum(1 for r in rows if r[0] == s) for s in ("GREEN", "YELLOW", "RED")}
    print(f"\nИтого: {len(rows)} кейсов — GREEN {cnt['GREEN']}, YELLOW {cnt['YELLOW']}, RED {cnt['RED']}")
    print(f"Песочница: {T}")
    if red:
        print("RED:", ", ".join(red))
    sys.exit(1 if red else 0)


if __name__ == "__main__":
    main()
