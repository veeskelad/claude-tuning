#!/usr/bin/env python3
"""Обёртка dippy-script.py (бонус «code»): скрипты читаются, простые переменные подставляются, опасное спрашивает.

    python3 tests/dippy-script.test.py

Нужен установленный dippy (`brew tap ldayton/dippy && brew install dippy`); без него тест пропускается. Белый список — заготовка
kit/dippy/config.example, положенная во временный HOME: ваш ~/.dippy/config не читается.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "kit", "hooks", "dippy-script.py")
CONFIG = os.path.join(ROOT, "kit", "dippy", "config.example")

if not shutil.which("dippy"):
    print("dippy не установлен (brew tap ldayton/dippy && brew install dippy): тест пропущен")
    sys.exit(0)

HOME = os.path.realpath(tempfile.mkdtemp(prefix="dippy-home-"))
os.makedirs(f"{HOME}/.dippy")
shutil.copy(CONFIG, f"{HOME}/.dippy/config")
ENV = {k: v for k, v in os.environ.items() if k != "DIPPY_CONFIG"}
ENV["HOME"] = HOME


def decide(command: str, cwd: str, mode: str = "default") -> str:
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "permission_mode": mode,
               "cwd": cwd, "tool_input": {"command": command}}
    out = subprocess.run([sys.executable, HOOK], input=json.dumps(payload), capture_output=True, text=True,
                         env=ENV, cwd=cwd).stdout
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip() else "none"


d = os.path.join(HOME, "proj")
os.makedirs(d)
with open(f"{d}/ok.sh", "w") as f:
    f.write("#!/usr/bin/env bash\nset -e\nmkdir -p out\ngit status\nls out\n")
with open(f"{d}/bad.sh", "w") as f:
    f.write("#!/bin/bash\necho hi\nrm -rf ~/x\n")
os.chmod(f"{d}/ok.sh", 0o755)
os.chmod(f"{d}/bad.sh", 0o755)

cases = [
    # заглядывает в скрипт
    (f"bash {d}/ok.sh", "allow"),
    (f"bash -e {d}/ok.sh arg", "allow"),
    (f"{d}/ok.sh", "allow"),
    ("./ok.sh", "allow"),
    (f"bash {d}/bad.sh", "ask"),
    (f"{d}/bad.sh", "ask"),
    ("bash -c 'rm -rf x'", "ask"),
    # подставляет переменные с одним буквальным значением
    (f"S={d}; ls $S > $S/out.log", "allow"),
    ("S=$HOME/.ssh; echo x > $S/authorized_keys", "ask"),
    ("S=/tmp/x; S=~/.bashrc; echo x > $S", "ask"),
    ("S=$(mktemp -d); echo x > $S/a", "ask"),
    ('K="git status"; $K', "allow"),
    ('K="rm -rf x"; $K', "ask"),
    ("for S in a b; do echo x > $S; done", "ask"),
    ("echo x > ~/.zshrc", "ask"),
    # обычные команды остаются на правилах dippy
    ("git status", "allow"),
    ("rm -f a.txt", "ask"),
    ("rm -rf ~", "deny"),
    # функции, объявленные в тексте, разрешены, если их тело безопасно
    ("f() { ls; }; f a", "allow"),
    ("f() { chown x a; }; f", "ask"),
    ("echo 'chown() {'; chown x a", "ask"),
    ("shift; local x=$(rm -f a); return 0", "ask"),
]
mode_cases = [  # в auto и плане «ask» dippy молчит, allow и deny остаются
    ("auto", "rm -f a.txt", "none"),
    ("auto", f"bash {d}/bad.sh", "none"),
    ("auto", f"bash {d}/ok.sh", "allow"),
    ("auto", "rm -rf ~", "deny"),
    ("plan", "rm -f a.txt", "none"),
    ("acceptEdits", "rm -f a.txt", "ask"),
]

fails = 0
for cmd, want in cases:
    got = decide(cmd, d)
    fails += got != want
    print(f"{'ok  ' if got == want else 'FAIL'} {want:5} {got:5} {cmd}")
for mode, cmd, want in mode_cases:
    got = decide(cmd, d, mode)
    fails += got != want
    print(f"{'ok  ' if got == want else 'FAIL'} {want:5} {got:5} [{mode}] {cmd}")
total = len(cases) + len(mode_cases)
shutil.rmtree(HOME, ignore_errors=True)
print(f"\n{total - fails}/{total} ok")
sys.exit(1 if fails else 0)
