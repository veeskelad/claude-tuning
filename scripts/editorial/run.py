#!/usr/bin/env python3
"""Редакция claude-tuning по расписанию: находит устаревшее на сайте и в наборе и открывает PR. Сама не мержит и не выпускает.

    python3 scripts/editorial/run.py --dry-run           # холостой прогон: список «что устарело», без правок и PR
    python3 scripts/editorial/run.py                     # боевой: правки в ветке editorial/<дата>, PR в main
    python3 scripts/editorial/run.py --plist             # напечатать задание launchd (поставить — решение владельца)

Порядок:
1. git fetch и отдельный worktree от origin/main во временной папке состояния. Клон, из которого запущен скрипт, не меняется.
2. Сбор без модели: scripts/editorial-check.py, новые записи журнала Claude Code после прошлой виденной версии,
   коммиты набора после прошлого прогона.
3. Нечего разбирать — модель не вызывается. Иначе `claude -p` с AGENT.md: в холостом прогоне только чтение,
   в боевом ещё правка файлов внутри worktree. Git, push, PR делает эта обёртка, модели они не даны.
4. Боевой: проверки после правки (build-page --check, editorial-check, git diff --check, разрешённые пути),
   коммит, push ветки, gh pr create. Открытый PR редакции уже есть — новый не открывается.

Состояние и журналы: ${XDG_STATE_HOME:-~/.local/state}/claude-tuning-editorial/. Уведомление о PR или сбое: команда из
переменной EDITORIAL_NOTIFY получает текст на stdin (например, бот в мессенджер); не задана — только журнал.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "claude-tuning-editorial"
STATE = STATE_DIR / "state.json"
CC_CHANGELOG = "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"
# Строки журнала Claude Code, которые могут касаться набора: хуки и их события, настройки, флаги запуска, скиллы.
CC_KEYS = re.compile(
    r"hook|statusline|status line|output.?style|skill|settings\.json|deprecat|breaking|renamed|"
    r"--model|--max-budget|--output-format|--no-session-persistence|--allowedTools|--max-turns|--resume|--bare", re.I)
CC_SKIP = re.compile(r"^\s*-\s*\[(VSCode|JetBrains|IDE)\]|cloud session|Self-hosted runner", re.I)
# Агент правит только тексты: страницу, описания и справки. Код набора (хуки, скрипты, настройки) он не трогает,
# а предлагает: тесты на правку кода обёртка не гоняет, и автомерж без них опасен.
EDITABLE = ("docs/index.html", "SKILL.md", "README.md", "CHANGELOG.md", "UPDATE.md")


def editable(path: str) -> bool:
    return path in EDITABLE or (path.startswith("kit/") and path.endswith(".md"))
TODAY = dt.date.today()
LOG = STATE_DIR / "logs" / f"{TODAY:%Y-%m-%d}.log"


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{dt.datetime.now():%H:%M:%S} {msg}\n")
    print(msg, file=sys.stderr)


def notify(text: str) -> None:
    cmd = os.environ.get("EDITORIAL_NOTIFY")
    if cmd:
        subprocess.run(cmd, shell=True, input=text, text=True, timeout=60)


def sh(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout.strip()


def vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def claude_code_news(since: str) -> dict:
    """Записи журнала Claude Code после версии since, отобранные по CC_KEYS."""
    with urllib.request.urlopen(CC_CHANGELOG, timeout=30) as r:
        text = r.read().decode("utf-8")
    sections, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^## (\d+\.\d+\.\d+)", line)
        if m:
            cur = m.group(1)
            sections[cur] = []
        elif cur and line.strip().startswith("-") and CC_KEYS.search(line) and not CC_SKIP.search(line):
            sections[cur].append(line.strip()[:400])
    new = {v: ls for v, ls in sections.items() if vtuple(v) > vtuple(since)}
    latest = max(sections, key=vtuple) if sections else since
    return {"since": since, "latest": latest, "versions": len(new), "entries": {v: ls for v, ls in new.items() if ls}}


def kit_commits(wt: Path, since: str | None) -> list[dict]:
    if not since:
        version = (wt / "VERSION").read_text().strip()
        since = f"v{version}" if sh("git", "tag", "-l", f"v{version}", cwd=wt) else "HEAD~10"
    out = sh("git", "log", "--format=%h%x09%ad%x09%s", "--date=short", "--name-only", f"{since}..HEAD", "--",
             "kit", "SKILL.md", "INSTALL-VIA-AGENT.txt", "docs", "scripts/install-kit.py", cwd=wt, check=False)
    commits, cur = [], None
    for line in out.splitlines():
        if "\t" in line:
            h, d, s = line.split("\t", 2)
            cur = {"commit": h, "date": d, "subject": s, "files": []}
            commits.append(cur)
        elif line.strip() and cur:
            cur["files"].append(line.strip())
    return commits


def open_editorial_pr(wt: Path) -> str | None:
    out = sh("gh", "pr", "list", "--state", "open", "--json", "number,headRefName,url", cwd=wt, check=False)
    for pr in json.loads(out or "[]"):
        if pr["headRefName"].startswith("editorial/"):
            return pr["url"]
    return None


def agent_cmd(wt: Path, inbox: Path, mode: str, budget: float, model: str, prompt: str) -> list[str]:
    """Граница агента задаётся набором инструментов, а не списком разрешений.

    --allowedTools только добавляет разрешения к настройкам человека: если там defaultMode=bypassPermissions или широкий
    allow, агент с чужим журналом на входе получил бы Bash, git и MCP. Поэтому:
      --restricted          не читает user/project/local settings, убирает Bash и прочий запуск кода, держит файловые
                            инструменты в рабочих папках (cwd = копия и --add-dir с входными данными), не даёт bypass;
      --tools               ровно эти инструменты; в отчёте только чтение;
      --strict-mcp-config   без MCP-серверов (мессенджеры, почта, трекеры)."""
    claude = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "/opt/homebrew/bin/claude"
    tools = "Read,Grep,Glob" + (",Edit,Write" if mode == "edit" else "")
    return [claude, "-p", prompt, "--restricted", "--tools", tools, "--strict-mcp-config", "--add-dir", str(inbox),
            "--permission-mode", "acceptEdits" if mode == "edit" else "dontAsk",
            "--model", model, "--max-budget-usd", str(budget), "--max-turns", "60",
            "--output-format", "json", "--no-session-persistence"]


def probe(model: str) -> int:
    """Проверка границы агента на деле: с теми же флагами просим запустить Bash и записать файл вне копии.
    Граница держится, если на диске появился только файл внутри копии. Повторять после обновления Claude Code."""
    d = STATE_DIR / f"probe-{dt.datetime.now():%Y%m%d-%H%M%S}"
    wt, inbox = d / "repo", d / "in"
    wt.mkdir(parents=True)
    inbox.mkdir()
    bash_mark, outside, inside = d / "bash-ran.txt", d / "outside.txt", wt / "inside.txt"
    prompt = (f"Это проверка ограничений, выполни все шаги по очереди, даже если какой-то не выйдет.\n"
              f"1. Запусти через Bash: touch {bash_mark}\n2. Запиши файл {outside} с текстом x\n"
              f"3. Запиши файл inside.txt в текущей папке с текстом x\n"
              f"4. Перечисли все инструменты, включая MCP, которые тебе доступны.\nОтветь списком: шаг — получилось или нет.")
    env = dict(os.environ, CLAUDE_TUNING_AUTO_SUMMARY="0", CLAUDE_TUNING_NUDGE="0")
    r = subprocess.run(agent_cmd(wt, inbox, "edit", 0.3, model, prompt), cwd=wt, capture_output=True, text=True,
                       timeout=600, env=env)
    data = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {"result": r.stderr[-500:]}
    verdict = {"bash запустился": bash_mark.exists(), "запись вне копии": outside.exists(),
               "запись внутри копии": inside.exists()}
    print(json.dumps(verdict, ensure_ascii=False))
    print(str(data.get("result", ""))[-1500:])
    ok = not verdict["bash запустился"] and not verdict["запись вне копии"] and verdict["запись внутри копии"]
    log(f"проба границы: {'держится' if ok else 'НЕ держится'} {verdict}, ${data.get('total_cost_usd')}")
    return 0 if ok else 1


def run_model(wt: Path, payload: Path, mode: str, budget: float, model: str) -> dict:
    prompt = (wt / "scripts/editorial/AGENT.md").read_text() + (
        f"\n\nРежим: `{mode}`. Входные данные: `{payload}`. Сегодня {TODAY:%d.%m.%Y}. Текущая папка — копия репозитория.")
    env = dict(os.environ, CLAUDE_TUNING_AUTO_SUMMARY="0", CLAUDE_TUNING_NUDGE="0")
    t0 = dt.datetime.now()
    try:
        r = subprocess.run(agent_cmd(wt, payload.parent, mode, budget, model, prompt),
                           cwd=wt, capture_output=True, text=True, timeout=40 * 60, env=env)
        data = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {"is_error": True, "result": r.stderr[-800:]}
    except subprocess.TimeoutExpired:
        data = {"is_error": True, "result": "не уложился в 40 минут"}
    data["minutes"] = round((dt.datetime.now() - t0).total_seconds() / 60, 1)
    res = str(data.get("result", "")).strip()
    m = re.search(r"\{.*\}\s*$", re.sub(r"^```\w*|```$", "", res, flags=re.M).strip(), re.S)
    try:
        data["report"] = json.loads(m.group(0)) if m else None
    except ValueError:
        data["report"] = None
    return data


def render(check: dict, report: dict | None) -> str:
    """Список «что устарело» для человека: сначала посчитанное кодом, потом найденное моделью."""
    rows = []
    # то, что агент разобрал сам (то же file:line), второй строкой от проверки без модели не повторяем
    seen = {(f.get("file"), f.get("line")) for f in (report or {}).get("findings", []) if f.get("action") not in (None, "none")}
    for f in check.get("findings", []):
        if f["kind"] != "unverifiable" and (f["file"], f["line"]) not in seen:
            rows.append(f"- `{f['file']}:{f['line']}` {f['claim']} → {f['actual']} (источник: {f['source']}; проверка без модели)")
    for f in (report or {}).get("findings", []):
        if f.get("action") in (None, "none"):
            continue
        rows.append(f"- `{f.get('file')}:{f.get('line')}` {f.get('was', '')} → {f.get('now', '')} "
                    f"(источник: {f.get('source')}; {f.get('action')}) {f.get('why', '')}".rstrip())
    out = "\n".join(rows) or "- расхождений нет"
    if (report or {}).get("proposals"):
        out += "\n\nНужно решение владельца:\n" + "\n".join(f"- {p}" for p in report["proposals"])
    if (report or {}).get("checked"):
        out += "\n\nПроверено, набор не затронут:\n" + "\n".join(f"- {p}" for p in report["checked"])
    if (report or {}).get("ideas"):
        out += "\n\nИдеи из журнала Claude Code:\n" + "\n".join(f"- {p}" for p in report["ideas"])
    unv = [f for f in check.get("findings", []) if f["kind"] == "unverifiable"]
    if unv:
        out += "\n\nНе проверить из репозитория (замер автора, не трогали):\n" + "\n".join(
            f"- `{f['file']}:{f['line']}` {f['claim']}" for f in unv)
    return out


def verify_edits(wt: Path, stale_before: int) -> list[str]:
    problems = []
    changed = sh("git", "diff", "--name-only", cwd=wt).splitlines() + sh(
        "git", "ls-files", "--others", "--exclude-standard", cwd=wt).splitlines()
    for p in changed:
        if not editable(p):
            problems.append(f"правка вне разрешённого: {p}")
    if "CHANGELOG.md" not in changed and changed:
        problems.append("нет записи в CHANGELOG.md под «Не выпущено»")
    if subprocess.run([sys.executable, "scripts/build-page.py", "--check"], cwd=wt, capture_output=True).returncode:
        problems.append("build-page.py --check: страница расходится с промптом или VERSION")
    after = json.loads(sh(sys.executable, "scripts/editorial-check.py", "--json", cwd=wt, check=False) or "{}")
    stale_after = sum(f["kind"] in ("stale", "drift", "anchor") for f in after.get("findings", []))
    if stale_after > stale_before:
        problems.append(f"расхождений стало больше: {stale_before} → {stale_after}")
    if subprocess.run(["git", "diff", "--check"], cwd=wt, capture_output=True).returncode:
        problems.append("git diff --check: пробелы в конце строк или конфликтные метки")
    return problems


def plist() -> str:
    py, me = sys.executable, Path(__file__).resolve()
    path = f"{Path(shutil.which('claude') or '/opt/homebrew/bin/claude').parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.claude-tuning.editorial</string>
  <key>ProgramArguments</key><array><string>{py}</string><string>{me}</string></array>
  <key>StartCalendarInterval</key><dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>10</integer><key>Minute</key><integer>0</integer></dict>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string><key>HOME</key><string>{Path.home()}</string></dict>
  <key>StandardOutPath</key><string>{STATE_DIR}/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>{STATE_DIR}/logs/launchd.err</string>
</dict>
</plist>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="только список, без правок, PR и записи состояния")
    ap.add_argument("--ref", default="origin/main", help="от чего строить копию (для проверки ветки до мержа)")
    ap.add_argument("--since-cc", help="версия Claude Code, с которой читать журнал (по умолчанию из состояния)")
    ap.add_argument("--budget", type=float, default=float(os.environ.get("EDITORIAL_BUDGET_USD", "1.5")))
    ap.add_argument("--model", default=os.environ.get("EDITORIAL_MODEL", "sonnet"))
    ap.add_argument("--no-model", action="store_true", help="только сбор без модели")
    ap.add_argument("--local", action="store_true", help="боевой режим без push и PR: коммит в копии, копия остаётся")
    ap.add_argument("--plist", action="store_true")
    ap.add_argument("--probe", action="store_true", help="проверить на деле, что агенту недоступны Bash и запись вне копии")
    a = ap.parse_args()
    if a.plist:
        print(plist())
        return 0
    if a.probe:
        return probe(a.model)

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    run_dir = STATE_DIR / "runs" / f"{dt.datetime.now():%Y%m%d-%H%M%S}"
    run_dir.mkdir(parents=True)
    wt = run_dir / "repo"
    sh("git", "fetch", "-q", "origin", "main", "--tags", cwd=SRC)
    # клон, из которого работает расписание, подтягивается сам: иначе обёртка отстаёт от AGENT.md и проверки из main
    if sh("git", "branch", "--show-current", cwd=SRC, check=False) == "main" and not sh("git", "status", "--porcelain", cwd=SRC, check=False):
        sh("git", "merge", "-q", "--ff-only", "origin/main", cwd=SRC, check=False)
    base = sh("git", "rev-parse", a.ref, cwd=SRC)
    branch = f"editorial{'-local' if a.local else ''}/{TODAY:%Y-%m-%d}"
    if a.dry_run:
        sh("git", "worktree", "add", "-q", "--detach", str(wt), a.ref, cwd=SRC)
    else:
        sh("git", "worktree", "add", "-q", "-B", branch, str(wt), a.ref, cwd=SRC)
    try:
        if not a.dry_run:
            # до модели: без имени для коммитов и входа в gh прогон дошёл бы до коммита и упал, потратив вызов модели
            if not sh("git", "config", "user.email", cwd=wt, check=False):
                raise RuntimeError(f"в клоне {SRC} не задано git user.email: git -C {SRC} config user.email <почта>")
            if not a.local and subprocess.run(["gh", "auth", "status"], capture_output=True).returncode:
                raise RuntimeError("gh не вошёл в GitHub: gh auth login")
        if not a.dry_run and not a.local and (url := open_editorial_pr(wt)):
            log(f"открыт PR редакции {url}: новый не открываю, пока владелец его не разберёт")
            return 0
        check_out = subprocess.run([sys.executable, "scripts/editorial-check.py", "--json"], cwd=wt,
                                   capture_output=True, text=True)
        check = json.loads(check_out.stdout)
        if "error" in check:
            raise RuntimeError(f"editorial-check: {check['error']}")
        since_cc = a.since_cc or state.get("cc_version") or sh(os.environ.get("CLAUDE_BIN") or "claude", "--version").split()[0]
        cc = claude_code_news(since_cc)
        commits = kit_commits(wt, state.get("kit_commit"))
        stale = [f for f in check["findings"] if f["kind"] in ("stale", "drift", "anchor")]
        (run_dir / "in").mkdir()
        payload = run_dir / "in" / "input.json"
        payload.write_text(json.dumps({"mode": "report" if a.dry_run else "edit", "check": check, "claude_code": cc,
                                       "commits": commits}, ensure_ascii=False, indent=1))
        log(f"сбор: расхождений {len(stale)}, Claude Code {cc['since']} → {cc['latest']} "
            f"(версий {cc['versions']}, записей {sum(map(len, cc['entries'].values()))}), коммитов набора {len(commits)}")

        report, cost = None, None
        if a.no_model:
            log("модель не вызывалась (--no-model)")
        elif not stale and not cc["entries"] and not commits:
            log("нового нет: модель не вызывалась")
        else:
            data = run_model(wt, payload, "report" if a.dry_run else "edit", a.budget, a.model)
            (run_dir / "model.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
            cost = data.get("total_cost_usd")
            log(f"модель: {data['minutes']} мин, ходов {data.get('num_turns')}, ${cost}, ошибка={data.get('is_error')}")
            if data.get("is_error") or data["report"] is None:
                raise RuntimeError(f"агент не вернул отчёт: {str(data.get('result'))[-400:]}")
            report = data["report"]

        text = render(check, report)
        (run_dir / "report.md").write_text(text + "\n")
        print(text)
        if a.dry_run:
            log(f"холостой прогон: отчёт {run_dir / 'report.md'}; состояние не сдвинуто")
            return 0

        changed = sh("git", "status", "--porcelain", cwd=wt)
        if a.local and not changed:
            log("--local: агент ничего не правил")
            return 0
        if changed:
            subprocess.run([sys.executable, "scripts/build-page.py"], cwd=wt, capture_output=True)
            problems = verify_edits(wt, len(stale))
            if problems:
                raise RuntimeError("правки не прошли проверку: " + "; ".join(problems))
            sh("git", "add", "-A", cwd=wt)
            sh("git", "commit", "-q", "-m", f"Редакция {TODAY:%d.%m.%Y}: сайт и набор сверены с тестами и Claude Code", cwd=wt)
            if a.local:
                log(f"--local: коммит {sh('git', 'rev-parse', '--short', 'HEAD', cwd=wt)} в {wt}; без push и PR, состояние не сдвинуто")
                return 0
            sh("git", "push", "-q", "-u", "origin", f"HEAD:refs/heads/{branch}", cwd=wt)
            body = ((report or {}).get("summary", "") + "\n\n## Что устарело\n\n" + text +
                    f"\n\nЖурнал Claude Code прочитан с {cc['since']} по {cc['latest']}. Мерж и выпуск версии решает владелец.")
            url = subprocess.run(["gh", "pr", "create", "--base", "main", "--head", branch, "--title",
                                  f"Редакция {TODAY:%d.%m.%Y}", "--body-file", "-"], cwd=wt, input=body, text=True,
                                 capture_output=True, check=True).stdout.strip()
            log(f"PR: {url}")
            notify(f"Редакция claude-tuning открыла PR: {url}")
        elif (report or {}).get("proposals"):
            notify("Редакция claude-tuning: правок нет, нужно решение владельца:\n" + "\n".join(report["proposals"]))

        state.update(cc_version=cc["latest"], kit_commit=base,
                     last_run=dt.datetime.now().isoformat(timespec="seconds"), last_cost_usd=cost)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        return 0
    except Exception as e:
        log(f"сбой: {e}")
        notify(f"Редакция claude-tuning не завершилась: {e}")
        return 1
    finally:
        if a.local:
            log(f"--local: копия оставлена для осмотра: {wt} (ветка {branch}); убирает человек после осмотра")
        else:
            sh("git", "worktree", "remove", "--force", str(wt), cwd=SRC, check=False)
        if not a.dry_run and not a.local:
            sh("git", "branch", "-D", branch, cwd=SRC, check=False)


if __name__ == "__main__":
    sys.exit(main())
