#!/usr/bin/env python3
"""Codex PreToolUse: те же стражи, что у Claude, через claude_bridge.py.

Вердикт хуков Claude → ответ Codex:
* deny — отказ, и никакое разрешение его не снимает;
* ask — у Codex его нет (хук помечается упавшим, а вызов выполняется), поэтому ask становится отказом
  с готовой командой одноразового разрешения на ровно этот вызов и этот cwd. Агент спрашивает человека
  в чате и только после явного «да» выпускает разрешение через lifecycle_approval.py, затем повторяет вызов;
* ask одного секрет-стража вместе с подстановкой маски — вызов идёт в маске: так Claude ведёт себя в bypass;
* updatedInput (безопасный вид команды) — allow с подменой ввода.

Вход, который не удалось разобрать, — отказ (fail-closed). Нет настроек Claude — молчание (вызов идёт).
Состояние (разрешения, журнал) — в $CODEX_HOME/state/claude-parity/ (по умолчанию ~/.codex).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path


CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
STATE_DIR = CODEX_HOME / "state/claude-parity"
APPROVAL_DIR = STATE_DIR / "approvals"
APPROVAL_AUDIT_LOG = STATE_DIR / "approval-audit.jsonl"
APPROVAL_VERSION = 1
MAX_APPROVAL_BYTES = 16 * 1024
HELPER = Path(__file__).resolve().parent / "lifecycle_approval.py"
SECRET_GUARD = re.compile(r"secret-guard-bash\.py")


def deny_output(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def deny(reason: str) -> None:
    print(json.dumps(deny_output(reason), ensure_ascii=False))
    raise SystemExit(0)


def canonical_cwd(cwd: str) -> str:
    return str(Path(cwd).expanduser().resolve())


def command_fingerprint(command: str, cwd: str) -> str:
    return hashlib.sha256(f"{canonical_cwd(cwd)}\0{command}".encode()).hexdigest()


def audit_approval(event: str, fingerprint: str, cwd: str, **details: object) -> bool:
    """Журнал выпуска и расхода разрешений: отпечаток и cwd, без текста команды."""
    record = {"event": event, "fingerprint": fingerprint, "cwd": canonical_cwd(cwd), "at": int(time.time()), **details}
    try:
        APPROVAL_AUDIT_LOG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with APPROVAL_AUDIT_LOG.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        APPROVAL_AUDIT_LOG.chmod(0o600)
    except OSError:
        return False
    return True


def _secure_token_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_mode & 0o077 == 0 and info.st_size <= MAX_APPROVAL_BYTES)


def consume_lifecycle_approval(command: str, cwd: str) -> tuple[bool, str]:
    """Забрать разрешение атомарно (rename) и удалить: второй запуск той же команды снова спросит."""
    fingerprint = command_fingerprint(command, cwd)
    token = APPROVAL_DIR / f"lifecycle-{fingerprint}.json"
    claimed = APPROVAL_DIR / f".claimed-{fingerprint}-{os.getpid()}"
    try:
        os.replace(token, claimed)
    except FileNotFoundError:
        return False, "no matching one-shot approval"
    except OSError as exc:
        return False, f"approval could not be claimed: {exc.__class__.__name__}"

    outcome, detail = "rejected", "invalid approval"
    try:
        if not _secure_token_file(claimed):
            detail = "approval file ownership, mode, type, or size is unsafe"
            return False, detail
        try:
            payload = json.loads(claimed.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            detail = "approval file is unreadable or invalid JSON"
            return False, detail
        now = int(time.time())
        expected = {
            "version": APPROVAL_VERSION,
            "kind": "lifecycle-command",
            "fingerprint": fingerprint,
            "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            "cwd": canonical_cwd(cwd),
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            detail = "approval does not match the exact command and working directory"
            return False, detail
        if not isinstance(payload.get("issued_at"), int) or payload["issued_at"] > now + 30:
            detail = "approval issue time is invalid"
            return False, detail
        if not isinstance(payload.get("expires_at"), int) or payload["expires_at"] < now:
            detail = "approval has expired"
            return False, detail
        outcome, detail = "consumed", "matching one-shot approval consumed"
        return True, detail
    finally:
        try:
            claimed.unlink(missing_ok=True)
        finally:
            audit_approval(outcome, fingerprint, cwd, detail=detail)


def approval_subject(data: dict) -> str:
    """Точный текст, к которому привязано разрешение: команда оболочки или инструмент + аргументы."""
    ti = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    tool = str(data.get("tool_name") or "")
    if tool in ("Bash", "apply_patch") and isinstance(ti.get("command"), str):
        return ti["command"]
    return tool + " " + json.dumps(ti, ensure_ascii=False, sort_keys=True)


def pending_file(subject: str) -> Path | None:
    """Точная команда — в файл 0600: команде выпуска не нужно её экранировать."""
    try:
        APPROVAL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        for old in APPROVAL_DIR.glob("pending-*.cmd"):
            if time.time() - old.stat().st_mtime > 86400:
                old.unlink(missing_ok=True)
        fd, name = tempfile.mkstemp(prefix="pending-", suffix=".cmd", dir=APPROVAL_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(subject)
        os.chmod(name, 0o600)
        return Path(name)
    except OSError:
        return None


def ask_reason(asks: list[tuple[str, str]], subject: str, cwd: str) -> str:
    reasons = "; ".join(dict.fromkeys(r.strip() for _, r in asks if r.strip())) or "страж требует подтверждения"
    text = (f"⏸ Здесь Claude спросил бы подтверждение: {reasons}\n"
            "Codex не умеет «ask», поэтому вызов остановлен. Спроси пользователя в чате, что именно и где "
            "выполнится, и дождись явного «да». После согласия выпусти одноразовое разрешение на ровно этот "
            "вызов (один запуск, 10 минут) и повтори его без изменений:\n")
    pending = pending_file(subject)
    if pending is None:
        return text + "(не удалось подготовить файл команды — пусть пользователь выполнит вызов сам в терминале)"
    digest = hashlib.sha256(subject.encode()).hexdigest()
    return text + (f"python3 {HELPER} --command-file {pending} --command-sha256 {digest} "
                   f"--cwd {canonical_cwd(cwd)} --reason '<что подтвердил пользователь>'")


def decide(data: dict, v) -> dict | None:
    """Вердикт хуков Claude (claude_bridge.Verdict) → ответ Codex на PreToolUse."""
    cwd = str(data.get("cwd") or os.getcwd())
    context = "\n".join(dict.fromkeys(v.context))
    if v.deny:
        return deny_output("\n".join(dict.fromkeys(v.deny)))
    asks = v.ask
    if asks and v.updated and all(SECRET_GUARD.search(cmd) for cmd, _ in asks):
        asks = []  # маска вместо вопроса, как в bypass у Claude
    if asks:
        subject = approval_subject(data)
        approved, _ = consume_lifecycle_approval(subject, cwd)
        if not approved:
            return deny_output(ask_reason(asks, subject, cwd))
        context = "\n".join(filter(None, [context, "✅ Одноразовое разрешение пользователя использовано."]))
    hso: dict = {}
    if v.updated:
        hso.update(permissionDecision="allow", updatedInput=v.updated)
    if context:
        hso["additionalContext"] = context
    out: dict = {}
    if hso:
        out["hookSpecificOutput"] = dict(hookEventName="PreToolUse", **hso)
    if v.system:
        out["systemMessage"] = "\n".join(v.system)
    return out or None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict):
        deny("🔐 Не удалось разобрать вход хука — вызов остановлен (fail-closed).")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import claude_bridge

    out = decide(data, claude_bridge.pre_tool_use(data))
    if out:
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
