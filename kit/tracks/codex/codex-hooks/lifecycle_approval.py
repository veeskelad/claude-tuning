#!/usr/bin/env python3
"""Одноразовое разрешение на один точный вызов, о котором страж Claude спросил бы («ask»).

Выпускает агент только после явного «да» человека в чате; pre_tool_guard.py печатает готовую команду.

Разрешение:
* привязано к точной команде и каноническому cwd (sha256 обоих), другая команда или папка его не берёт;
* живёт не дольше 15 минут (по умолчанию 10), файл 0600 в папке 0700 своего пользователя;
* забирается атомарно и удаляется при первой попытке — второй запуск снова спросит;
* выпуск и расход пишутся в approval-audit.jsonl без текста команды.

Команду лучше передавать файлом (--command-file + --command-sha256): так вызов выпуска не содержит
защищённую команду и страж не судит её второй раз. Файл pending-*.cmd готовит сам pre_tool_guard.py.
Для ssh + systemctl есть разбор по частям (--ssh-systemd-host, --systemd-action, --unit): имя хоста
и юнитов проверяются по узкой грамматике, команда собирается здесь, вставить в неё лишнее нельзя.

Выпуск пишет вне рабочей папки проекта: в песочнице Codex понадобится обычное повышение прав.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import tempfile
import time
import uuid
from pathlib import Path

import pre_tool_guard as guard


MAX_TTL_SECONDS = 15 * 60
MAX_COMMAND_BYTES = 16 * 1024
SSH_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,252}$")
SYSTEMD_UNIT = re.compile(r"^[A-Za-z0-9@_.:-]+\.(?:service|timer|path|socket)$")


def secure_approval_dir() -> Path:
    path = guard.APPROVAL_DIR
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError("approval directory must be a real directory owned by the current user")
    path.chmod(0o700)
    return path


def read_command_file(path: Path, expected_sha256: str) -> str:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_size > MAX_COMMAND_BYTES):
        raise PermissionError("command file ownership, mode, type, or size is unsafe")
    raw = path.read_text(encoding="utf-8")
    # редактор мог дописать перевод строки в конце
    for command in (raw, raw[:-1] if raw.endswith("\n") else None):
        if command and hashlib.sha256(command.encode()).hexdigest() == expected_sha256.lower():
            return command
    if not raw.strip():
        raise ValueError("command file is empty")
    raise ValueError("command file SHA-256 does not match the approved digest")


def build_ssh_systemd_command(host: str, action: str, units: list[str], connect_timeout: int) -> str:
    if not SSH_HOST.fullmatch(host):
        raise ValueError("SSH host must be a simple hostname or configured alias")
    if action not in {"stop", "restart", "disable"}:
        raise ValueError("unsupported systemd lifecycle action")
    if connect_timeout < 1 or connect_timeout > 30:
        raise ValueError("SSH connect timeout must be between 1 and 30 seconds")
    if not units or len(units) > 32 or len(set(units)) != len(units):
        raise ValueError("provide between 1 and 32 unique systemd units")
    if any(not SYSTEMD_UNIT.fullmatch(unit) for unit in units):
        raise ValueError("invalid systemd unit name")
    remote = "sudo -n systemctl " + action + " " + " ".join(units)
    return f"ssh -o BatchMode=yes -o ConnectTimeout={connect_timeout} {shlex.quote(host)} {shlex.quote(remote)}"


def issue_approval(command: str, cwd: str, ttl: int, reason: str, *, now: int | None = None) -> dict[str, object]:
    if ttl < 1 or ttl > MAX_TTL_SECONDS:
        raise ValueError(f"ttl must be between 1 and {MAX_TTL_SECONDS} seconds")
    if not reason.strip():
        raise ValueError("a concise approval reason is required")

    issued_at = int(time.time()) if now is None else int(now)
    normalized_cwd = guard.canonical_cwd(cwd)
    fingerprint = guard.command_fingerprint(command, normalized_cwd)
    payload: dict[str, object] = {
        "version": guard.APPROVAL_VERSION,
        "kind": "lifecycle-command",
        "token_id": str(uuid.uuid4()),
        "fingerprint": fingerprint,
        "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
        "cwd": normalized_cwd,
        "issued_at": issued_at,
        "expires_at": issued_at + ttl,
        "reason": reason.strip()[:240],
    }

    approval_dir = secure_approval_dir()
    destination = approval_dir / f"lifecycle-{fingerprint}.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".issuing-", dir=approval_dir)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        destination.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)

    token_id = payload["token_id"]
    if not guard.audit_approval("issued", fingerprint, normalized_cwd, expires_at=payload["expires_at"],
                                reason=payload["reason"], token_id=token_id):
        destination.unlink(missing_ok=True)
        raise OSError("approval audit could not be written; token was removed fail-closed")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--command", help="точная команда, как её видит страж")
    source.add_argument("--command-file", type=Path, help="файл 0600 с точной командой")
    source.add_argument("--ssh-systemd-host", help="собрать каноническую команду ssh + systemctl")
    parser.add_argument("--command-sha256", help="обязателен с --command-file")
    parser.add_argument("--systemd-action", choices=("stop", "restart", "disable"))
    parser.add_argument("--unit", action="append", default=[])
    parser.add_argument("--connect-timeout", type=int, default=8)
    parser.add_argument("--cwd", required=True, help="точная рабочая папка вызова")
    parser.add_argument("--ttl", type=int, default=600, help="срок в секундах (не больше 900)")
    parser.add_argument("--reason", required=True, help="коротко: что подтвердил пользователь")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command_file is not None:
        if not args.command_sha256:
            raise SystemExit("--command-sha256 is required with --command-file")
        command = read_command_file(args.command_file, args.command_sha256)
        if (args.command_file.parent.resolve() == guard.APPROVAL_DIR.resolve()
                and args.command_file.name.startswith("pending-")):
            args.command_file.unlink(missing_ok=True)
    elif args.ssh_systemd_host is not None:
        if args.command_sha256:
            raise SystemExit("--command-sha256 is only valid with --command-file")
        if not args.systemd_action:
            raise SystemExit("--systemd-action is required with --ssh-systemd-host")
        command = build_ssh_systemd_command(args.ssh_systemd_host, args.systemd_action, args.unit,
                                            args.connect_timeout)
    else:
        if args.command_sha256 or args.systemd_action or args.unit:
            raise SystemExit("structured options require their matching command source")
        command = args.command
    payload = issue_approval(command, args.cwd, args.ttl, args.reason)
    print(json.dumps({"token_id": payload["token_id"], "fingerprint": payload["fingerprint"],
                      "expires_at": payload["expires_at"]}, sort_keys=True))


if __name__ == "__main__":
    main()
