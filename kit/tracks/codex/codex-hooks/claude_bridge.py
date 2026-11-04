#!/usr/bin/env python3
"""Мост Codex → хуки Claude Code: Codex исполняет те же стражи, что и Claude.

Codex читает хуки из ~/.codex/hooks.json, Claude — из settings.json. Своей копии стражей у Codex нет:
отдельная копия расходится (регулярка в ней начинает блокировать то, что Claude давно пропускает).
Мост запускает те самые команды хуков, что записаны для Claude:
  ~/.claude/settings.json, ~/.claude/settings.local.json и <проект>/.claude/settings*.json —
  проектные только если проект доверен в Claude (hasTrustDialogAccepted в ~/.claude.json).
Правка стража в Claude сразу действует и в Codex.

    claude_bridge.py PostToolUse | SessionStart | UserPromptSubmit | Stop | PreCompact   (JSON хука на stdin)

PreToolUse идёт через pre_tool_guard.py: там вердикт превращается в ответ Codex.

Чем Codex отличается от Claude и что с этим делает мост:
* apply_patch — один вызов на много файлов: он раскладывается на Write (новый файл) и Edit (правка) по каждому;
* у Codex нет «ask» (хук помечается упавшим, а вызов ВЫПОЛНЯЕТСЯ), поэтому «ask» отдаётся отдельным
  вердиктом — pre_tool_guard.py превращает его в отказ с одноразовым разрешением;
* хуки, которые имеют смысл только внутри Claude (читают его транскрипт, одобряют его окна), пропускаются — SKIP;
* нет настроек Claude — нет хуков: мост молчит и ничего не блокирует.

Окружение: CLAUDE_CONFIG_DIR — другая папка настроек Claude (как у самого Claude Code);
CLAUDE_PARITY_SKIP — своя регулярка пропуска поверх SKIP; CLAUDE_PARITY_MCP_SYNC=0 — не синхронизировать MCP.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
_CFG = os.environ.get("CLAUDE_CONFIG_DIR")
CLAUDE_DIR = Path(_CFG).expanduser() if _CFG else HOME / ".claude"
CLAUDE_JSON = CLAUDE_DIR / ".claude.json" if _CFG else HOME / ".claude.json"
USER_SETTINGS = [CLAUDE_DIR / "settings.json", CLAUDE_DIR / "settings.local.json"]

# Codex сам убивает хук по своему таймауту (hooks.json.template: SessionStart 30 с, остальные 10 с),
# поэтому каждый хук Claude получает чуть меньше — мост успевает ответить.
DEADLINE = {"SessionStart": 25.0}
DEFAULT_DEADLINE = 8.5

# Только для Claude: dippy одобряет окна Claude (у Codex свои одобрения песочницы), compact-context и
# session-autosave читают транскрипт Claude, plan-auto-allow отвечает на PermissionRequest Claude.
SKIP = re.compile(r"\bdippy\b|compact-context\.py|session-autosave|plan-auto-allow")
_EXTRA_SKIP = os.environ.get("CLAUDE_PARITY_SKIP")
# Подстановка безопасного вида секрет-стража важнее любой другой переписанной команды.
SECRET_GUARD = re.compile(r"secret-guard-bash\.py")
# Общие скрипты, которые различают агента по флагу.
SUBSTITUTE = [(re.compile(r"--agent claude\b"), "--agent codex")]
EVENTS = ("PostToolUse", "SessionStart", "UserPromptSubmit", "Stop", "PreCompact")


@dataclass
class Verdict:
    deny: list[str] = field(default_factory=list)
    ask: list[tuple[str, str]] = field(default_factory=list)  # (команда хука, причина)
    updated: dict | None = None
    updated_by: str = ""
    context: list[str] = field(default_factory=list)
    block: list[str] = field(default_factory=list)  # decision: block вне PreToolUse
    system: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def skipped(cmd: str) -> bool:
    if SKIP.search(cmd):
        return True
    if _EXTRA_SKIP:
        try:
            return re.search(_EXTRA_SKIP, cmd) is not None
        except re.error:
            return False
    return False


def claude_trusted(project: Path) -> bool:
    projects = _load(CLAUDE_JSON).get("projects") or {}
    return bool((projects.get(str(project)) or {}).get("hasTrustDialogAccepted"))


def project_dir(cwd: Path) -> Path | None:
    """Ближайший предок ниже домашней папки с .claude/settings*.json — проектные настройки Claude."""
    for d in (cwd, *cwd.parents):
        if d == HOME or HOME not in d.parents:
            break
        if (d / ".claude/settings.json").is_file() or (d / ".claude/settings.local.json").is_file():
            return d
    return None


def settings_files(cwd: Path) -> tuple[list[Path], Path | None]:
    files = list(USER_SETTINGS)
    proj = project_dir(cwd)
    if proj and claude_trusted(proj):
        files += [proj / ".claude/settings.json", proj / ".claude/settings.local.json"]
    return files, proj


def matches(matcher: str | None, value: str) -> bool:
    """Как в Claude: пусто или * — всё; слова через | — точное совпадение; иначе — регулярка целиком."""
    if matcher in (None, "", "*"):
        return True
    if re.fullmatch(r"[\w|-]+", matcher):
        return value in matcher.split("|")
    try:
        return re.fullmatch(matcher, value) is not None
    except re.error:
        return False


def hooks_for(event: str, value: str | None, cwd: Path) -> tuple[list[tuple[str, float]], Path | None]:
    """Команды хуков Claude для события; value=None — событие без matcher (UserPromptSubmit, Stop)."""
    files, proj = settings_files(cwd)
    found: list[tuple[str, float]] = []
    for f in files:
        for group in (_load(f).get("hooks") or {}).get(event) or []:
            if not isinstance(group, dict):
                continue
            if value is not None and not matches(group.get("matcher"), value):
                continue
            for h in group.get("hooks") or []:
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command")
                if h.get("type", "command") != "command" or not cmd or skipped(cmd):
                    continue  # prompt/agent-хуки исполняет только сам Claude
                for rx, repl in SUBSTITUTE:
                    cmd = rx.sub(repl, cmd)
                found.append((cmd, float(h.get("timeout") or 60)))
    return found, proj


# ---------------------------------------------------------------- apply_patch → Write/Edit
PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$")


def patch_calls(patch: str, cwd: Path) -> list[tuple[str, dict]]:
    calls: list[dict] = []
    cur: dict | None = None
    for line in patch.splitlines():
        m = PATCH_FILE.match(line)
        if m:
            cur = {"op": m.group(1), "path": os.path.normpath(cwd / m.group(2).strip()), "add": [], "del": []}
            calls.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("*** Move to: "):
            cur["path"] = os.path.normpath(cwd / line[13:].strip())
        elif line.startswith("+"):
            cur["add"].append(line[1:])
        elif line.startswith("-"):
            cur["del"].append(line[1:])
    out: list[tuple[str, dict]] = []
    for c in calls:
        if c["op"] == "Delete":
            continue  # удаление файла стражи Write/Edit не судят; rm в Bash судит safety-guard
        if c["op"] == "Add":
            out.append(("Write", {"file_path": c["path"], "content": "\n".join(c["add"])}))
        else:
            out.append(("Edit", {"file_path": c["path"], "old_string": "\n".join(c["del"]),
                                 "new_string": "\n".join(c["add"])}))
    return out


def claude_calls(data: dict) -> list[tuple[str, dict]]:
    """Вызов Codex → один или несколько вызовов в виде Claude (tool_name, tool_input)."""
    tool = str(data.get("tool_name") or "")
    ti = data.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    if tool == "apply_patch":
        return patch_calls(str(ti.get("command") or ""), Path(str(data.get("cwd") or os.getcwd())))
    return [(tool, ti)]


# ---------------------------------------------------------------- запуск
def run_one(cmd: str, timeout: float, payload: dict, cwd: Path, proj: Path | None) -> tuple[str, int, str, str]:
    event = str(payload.get("hook_event_name"))
    timeout = min(timeout, DEADLINE.get(event, DEFAULT_DEADLINE))
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(proj or cwd), CODEX_CLAUDE_BRIDGE="1")
    try:
        p = subprocess.run(["bash", "-c", cmd], input=json.dumps(payload), text=True, capture_output=True,
                           cwd=str(cwd) if cwd.is_dir() else None, env=env, timeout=timeout)
        return cmd, p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return cmd, -1, "", f"timeout after {timeout:.0f}s"
    except OSError as e:
        return cmd, -1, "", f"{type(e).__name__}: {e}"


def _parse(text: str) -> dict | None:
    for chunk in (text, text.splitlines()[-1] if text else ""):
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        return obj if isinstance(obj, dict) else None
    return None


def collect(event: str, data: dict, value: str | None, tool_input: dict | None = None) -> Verdict:
    cwd = Path(str(data.get("cwd") or os.getcwd()))
    hooks, proj = hooks_for(event, value, cwd)
    payload = dict(data, hook_event_name=event)
    if tool_input is not None:
        payload.update(tool_name=value, tool_input=tool_input)
    v = Verdict()
    if not hooks:
        return v
    with ThreadPoolExecutor(max_workers=len(hooks)) as pool:
        results = list(pool.map(lambda h: run_one(h[0], h[1], payload, cwd, proj), hooks))
    rewrites: list[tuple[str, dict]] = []
    for cmd, code, out, err in results:
        if code == 2:  # exit 2 у Claude — отказ с причиной в stderr
            (v.deny if event == "PreToolUse" else v.block).append(err.strip() or "blocked by Claude hook")
            continue
        if code == -1:
            v.errors.append(f"{cmd[:80]}: {err}")
            continue
        text = out.strip()
        if not text.startswith("{"):
            # обычный текст Claude добавляет в контекст только на SessionStart и UserPromptSubmit
            if event in ("SessionStart", "UserPromptSubmit") and text:
                v.context.append(text)
            continue
        obj = _parse(text)
        if obj is None:
            continue
        hso = obj.get("hookSpecificOutput") or {}
        hso = hso if isinstance(hso, dict) else {}
        dec, reason = hso.get("permissionDecision"), hso.get("permissionDecisionReason") or ""
        if obj.get("decision") == "block":
            (v.deny if event == "PreToolUse" else v.block).append(obj.get("reason") or reason or "blocked")
        if dec == "deny":
            v.deny.append(reason or "denied by Claude hook")
        elif dec == "ask":
            v.ask.append((cmd, reason))
        if isinstance(hso.get("updatedInput"), dict):
            rewrites.append((cmd, hso["updatedInput"]))
        if hso.get("additionalContext"):
            v.context.append(str(hso["additionalContext"]))
        if obj.get("systemMessage"):
            v.system.append(str(obj["systemMessage"]))
    if rewrites:
        rewrites.sort(key=lambda r: not SECRET_GUARD.search(r[0]))
        v.updated_by, v.updated = rewrites[0]
    return v


def pre_tool_use(data: dict) -> Verdict:
    calls = claude_calls(data)
    total = Verdict()
    for tool, ti in calls:
        v = collect("PreToolUse", data, tool, ti)
        total.deny += v.deny
        total.ask += v.ask
        total.context += v.context
        total.system += v.system
        total.errors += v.errors
        # переписать обратно можно только вызов один к одному (Bash, MCP), не разложенный apply_patch
        if len(calls) == 1 and str(data.get("tool_name")) != "apply_patch" and v.updated:
            total.updated, total.updated_by = v.updated, v.updated_by
    return total


def post_tool_use(data: dict) -> dict | None:
    out: dict = {}
    ctx: list[str] = []
    for name, ti in claude_calls(data):
        v = collect("PostToolUse", data, name, ti)
        if v.block or v.deny:
            out.update(decision="block", reason="\n".join(v.block + v.deny))
        ctx += v.context
        if v.system:
            out["systemMessage"] = "\n".join(v.system)
    if ctx:
        out["hookSpecificOutput"] = {"hookEventName": "PostToolUse", "additionalContext": "\n".join(ctx)}
    return out or None


def sync_settings() -> str:
    """MCP-серверы и права к ним Claude → Codex; действуют со следующей сессии Codex."""
    if os.environ.get("CLAUDE_PARITY_MCP_SYNC") == "0":
        return ""
    script = Path(__file__).resolve().parent / "sync_from_claude.py"
    if not script.is_file():
        return ""
    try:
        p = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = p.stdout.strip() if p.returncode == 0 else ""
    return f"Настройки MCP подтянуты из Claude (действуют со следующей сессии Codex):\n{text}" if text else ""


def session_start(data: dict) -> dict | None:
    v = collect("SessionStart", data, str(data.get("source") or "startup"))
    synced = sync_settings()
    if synced:
        v.context.append(synced)
    if not v.context:
        return None
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n\n".join(v.context)}}


def user_prompt_submit(data: dict) -> dict | None:
    v = collect("UserPromptSubmit", data, None)
    out: dict = {}
    if v.block or v.deny:
        out.update(decision="block", reason="\n".join(v.block + v.deny))
    if v.context:
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(v.context)}
    if v.system:
        out["systemMessage"] = "\n".join(v.system)
    return out or None


def stop(data: dict) -> dict | None:
    v = collect("Stop", data, None)
    out: dict = {}
    # stop_hook_active: ход уже продолжен по Stop-хуку — второй «block» зациклил бы агента
    if (v.block or v.deny) and not data.get("stop_hook_active"):
        out.update(decision="block", reason="\n".join(v.block + v.deny))
    if v.system:
        out["systemMessage"] = "\n".join(v.system)
    return out or None


def pre_compact(data: dict) -> dict | None:
    # хуки PreCompact в Claude ничего не возвращают модели — только побочный эффект (запись, снимок)
    v = collect("PreCompact", data, str(data.get("trigger") or "auto"))
    return {"systemMessage": "\n".join(v.system)} if v.system else None


HANDLERS = {
    "PostToolUse": post_tool_use,
    "SessionStart": session_start,
    "UserPromptSubmit": user_prompt_submit,
    "Stop": stop,
    "PreCompact": pre_compact,
}


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    handler = HANDLERS.get(event)
    if handler is None:
        print(f"claude_bridge: событие {event!r} не поддержано (есть: {', '.join(EVENTS)}; PreToolUse — pre_tool_guard.py)",
              file=sys.stderr)
        return 0
    res = handler(data)
    if res:
        print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
