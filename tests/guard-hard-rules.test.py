#!/usr/bin/env python3
"""Регресс-таблица guard-hard-rules.py (бонус «code»): что запрещается и что проходит (ложняки).

    python3 tests/guard-hard-rules.test.py

Гоняет хук из kit/bonus/code/hooks/ на песочнице во временном каталоге: ни домашний каталог, ни чужие
репозитории не нужны. Свежесть fetch задаётся mtime файла FETCH_HEAD. Сети нет: remote — заглушка.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H = os.path.join(ROOT, "kit", "bonus", "code", "hooks", "guard-hard-rules.py")
T = os.path.realpath(tempfile.mkdtemp(prefix="hard-rules-"))
ENV = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
# ai-signature в наборе выключен по умолчанию: тесты правила — с явным включением
ENV["CLAUDE_GUARD_NO_AI_SIGNATURE"] = "1"


def sh(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True,
                   env={**ENV, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def make_clone(path, branch="main"):
    """Репо в роли клона: ветка branch, remote origin (заглушка), один коммит."""
    sh("git", "init", "-q", "-b", branch, path)
    sh("git", "-C", path, "remote", "add", "origin", "https://example.invalid/x.git")
    sh("git", "-C", path, "commit", "-q", "--allow-empty", "-m", "init")


R = os.path.join(T, "repo")  # основная копия на main
make_clone(R)
open(os.path.join(R, ".gitignore"), "w").write("build/\n")
LINKED = os.path.join(T, "linked")  # linked worktree того же репо
sh("git", "-C", R, "worktree", "add", "-q", "-b", "feature", LINKED)
FEATURE = os.path.join(T, "on-feature")  # основная копия, но на рабочей ветке
make_clone(FEATURE)
sh("git", "-C", FEATURE, "checkout", "-q", "-b", "feature/x")
LOCAL = os.path.join(T, "local-only")  # свежий git init без origin
sh("git", "init", "-q", "-b", "main", LOCAL)
OPTOUT = os.path.join(T, "opt-out")  # клон, где main-copy выключен настройкой репо
make_clone(OPTOUT)
sh("git", "-C", OPTOUT, "config", "claude.allowMainEdits", "true")
TRUNK = os.path.join(T, "trunk-repo")  # основная ветка называется не main
make_clone(TRUNK, "trunk")
sh("git", "-C", TRUNK, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
FH = os.path.join(R, ".git", "FETCH_HEAD")


def run(tool, inp, cwd=R, env=None):
    p = subprocess.run([sys.executable, H], input=json.dumps({"tool_name": tool, "tool_input": inp, "cwd": cwd}),
                       capture_output=True, text=True, env={**ENV, **(env or {})})
    if p.returncode:
        return f"ERR{p.returncode}"
    if not p.stdout.strip():
        return "allow"
    return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]


def reason(tool, inp, cwd=R):
    p = subprocess.run([sys.executable, H], input=json.dumps({"tool_name": tool, "tool_input": inp, "cwd": cwd}),
                       capture_output=True, text=True, env=ENV)
    return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecisionReason"] if p.stdout.strip() else ""


def B(c, cwd=R, env=None):
    return run("Bash", {"command": c}, cwd, env)


def W(path, tool="Write", env=None):
    return run(tool, {"file_path": path, "content": "x", "old_string": "a", "new_string": "b"}, R, env)


def aged(ago, c, cwd=R, env=None):
    """Команда c, когда последний fetch репо был ago секунд назад (None — не было ни разу)."""
    if ago is None:
        if os.path.exists(FH):
            os.remove(FH)
    else:
        open(FH, "w").close()
        os.utime(FH, (time.time() - ago,) * 2)
    return B(c, cwd, env)


def has(text, *parts):
    return "ok" if all(p in text for p in parts) else f"нет {parts} в {text!r}"


CASES = [
    # ai-signature: Bash
    ("allow", lambda: B('git commit -m "fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>"', env={"CLAUDE_GUARD_NO_AI_SIGNATURE": "0"})),
    ("deny", lambda: B('git commit -m "fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>"')),
    ("deny", lambda: B("git commit -m \"$(cat <<'EOF'\nfix: x\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\nEOF\n)\"")),
    ("deny", lambda: B('git commit -m "fix: x" -m "Claude-Session: abc"')),
    ("allow", lambda: B('git commit -m "fix(ci): поправил раннер"')),
    ("allow", lambda: B('git log --grep "Co-Authored-By: Claude" --oneline | head')),
    ("deny", lambda: B('gh pr create --title x --body "text Generated with Claude Code"')),
    ("allow", lambda: B("gh pr view 12")),
    ("deny", lambda: B('curl -X POST -d \'{"body":"ok\\n\\nCo-Authored-By: Claude"}\' https://example.invalid/c')),
    ("allow", lambda: B("grep -rn 'Generated with Claude' docs/")),
    # ai-signature: git commit -F файл
    ("deny", lambda: (open(os.path.join(T, "msg.txt"), "w").write("fix\n\nCo-Authored-By: Claude <x@y>\n"),
                      B(f"git commit -F {T}/msg.txt"))[1]),
    ("allow", lambda: (open(os.path.join(T, "msg2.txt"), "w").write("fix: обычный текст\n"),
                       B(f"git commit -F {T}/msg2.txt"))[1]),
    # ai-signature: MCP
    ("deny", lambda: run("mcp__tracker__save_comment", {"issue": "A-1", "body": "ok\n\nCo-Authored-By: Claude"})),
    ("deny", lambda: run("mcp__chat__send_message", {"text": "x 🤖 Generated with Claude"})),
    ("allow", lambda: run("mcp__tracker__save_comment", {"issue": "A-1", "body": "обычный текст"})),
    ("allow", lambda: run("mcp__tracker__list_issues", {"q": "Co-Authored-By: Claude"})),
    # no-verify
    ("deny", lambda: B('git commit --no-verify -m "fix: x"')),
    ("deny", lambda: B('git commit -nm "fix: x"')),
    ("deny", lambda: B("git commit -a -n -m x")),
    ("deny", lambda: B("git push --no-verify -u origin fix/x")),
    ("deny", lambda: B("git -c core.hooksPath=/dev/null commit -m x")),
    ("allow", lambda: B('git commit -m "docs: drop --no-verify advice"')),
    ("allow", lambda: B('git commit -am "fix: x"')),
    ("allow", lambda: B("git push -n origin fix/x")),
    ("allow", lambda: B("grep -rn -- --no-verify .")),
    # worktree-base: не от локальной ветки
    ("deny", lambda: B("git worktree add ../wt -b fix/x")),
    ("deny", lambda: B("git worktree add ../wt -b fix/x main")),
    ("deny", lambda: B("git -C repo worktree add ../wt -b fix/x master", T)),
    ("deny", lambda: B("git worktree add ../wt -b fix/x HEAD~2")),
    ("allow", lambda: aged(60, "git worktree add ../wt -b fix/x origin/main")),
    ("allow", lambda: aged(60, "git worktree add ../review origin/feature/y")),
    ("allow", lambda: B("git worktree list")),
    ("allow", lambda: B("git worktree add --orphan -b gh-pages ../pages")),
    # worktree-base: свежесть origin (FETCH_HEAD старше 30 минут)
    ("deny", lambda: aged(7200, "git worktree add ../wt -b fix/x origin/main")),
    ("deny", lambda: aged(None, "git worktree add ../wt -b fix/x origin/main")),
    ("deny", lambda: aged(7200, "git -C repo worktree add ../wt -b fix/x origin/main", T)),
    ("deny", lambda: aged(7200, "cd repo && git worktree add ../wt -b fix/x refs/remotes/origin/main", T)),
    ("deny", lambda: aged(7200, "git worktree add ../wt2 -b fix/y origin/main", LINKED)),
    ("allow", lambda: aged(7200, "git fetch origin main && git worktree add ../wt -b fix/x origin/main")),
    ("allow", lambda: aged(7200, "git worktree add ../wt -b fix/x feature/x")),
    ("allow", lambda: aged(7200, "git worktree add ../wt origin/main")),
    ("allow", lambda: aged(7200, "git worktree add ../wt -b fix/x origin/main", tempfile.gettempdir())),
    ("allow", lambda: aged(7200, "git worktree add ../wt -b fix/x origin/main", env={"CLAUDE_FRESHNESS_OFF": "1"})),
    ("ok", lambda: has((aged(7200, "true"), reason("Bash", {"command": "git -C repo worktree add ../wt -b fix/x origin/main"}, T))[1],
                       "git -C repo fetch origin main && git -C repo worktree add")),
    ("ok", lambda: has(reason("Bash", {"command": "git worktree add ../wt -b fix/x main"}),
                       "git fetch origin main && git worktree add ../wt -b fix/x origin/main")),
    ("ok", lambda: has(reason("Bash", {"command": "git worktree add ../wt -b fix/x master"}, TRUNK),
                       "git fetch origin trunk")),
    # main-copy: основная копия клона на основной ветке
    ("deny", lambda: W(os.path.join(R, "src", "a.py"))),
    ("deny", lambda: W(os.path.join(R, "README.md"), "Edit")),
    ("deny", lambda: W(os.path.join(R, "README.md"), "MultiEdit")),
    ("deny", lambda: W(os.path.join(R, "new-dir", "deep", "a.md"))),
    ("deny", lambda: W(os.path.join(TRUNK, "a.md"))),
    ("allow", lambda: W(os.path.join(R, "build", "out.js"))),                 # игнорируется git
    ("allow", lambda: W(os.path.join(R, ".git", "info", "exclude"))),
    ("allow", lambda: W(os.path.join(R, ".tasks", "plan.md"))),
    ("allow", lambda: W(os.path.join(LINKED, "a.md"))),                       # linked worktree
    ("allow", lambda: W(os.path.join(FEATURE, "a.md"))),                      # основная копия, но на рабочей ветке
    ("allow", lambda: W(os.path.join(LOCAL, "a.md"))),                        # нет origin: свежий init
    ("allow", lambda: W(os.path.join(OPTOUT, "a.md"))),                       # claude.allowMainEdits=true
    ("allow", lambda: W(os.path.join(T, "outside", "a.md"))),                 # не репозиторий
    ("allow", lambda: W(os.path.join(R, "a.md"), env={"CLAUDE_GUARD_SKIP": "main-copy"})),
    ("deny", lambda: W(os.path.join(R, "a.md"), env={"CLAUDE_GUARD_SKIP": "no-verify"})),
    ("deny", lambda: B('git commit --no-verify -m x', env={"CLAUDE_GUARD_SKIP": "main-copy"})),
    ("allow", lambda: B('git commit --no-verify -m x', env={"CLAUDE_GUARD_SKIP": "no-verify"})),
    ("ok", lambda: has(reason("Write", {"file_path": os.path.join(R, "a.md"), "content": "x"}),
                       "main-copy", "worktree add", "origin/main")),
    # home-root не входит в этот хук: его ведёт home-clutter.sh
    ("allow", lambda: run("Write", {"file_path": os.path.expanduser("~/hard-rules-test-never-created.md"), "content": "x"})),
    # устойчивость
    ("allow", lambda: B('echo "unbalanced')),
    ("allow", lambda: run("Read", {"file_path": "/etc/hosts"})),
    ("allow", lambda: run("Bash", {})),
]

bad = 0
for i, (exp, case) in enumerate(CASES):
    got = case()
    if got != exp:
        bad += 1
        print(f"FAIL #{i}: ожидали {exp}, получили {got}")
shutil.rmtree(T, ignore_errors=True)
print(f"{len(CASES) - bad}/{len(CASES)} ok")
sys.exit(1 if bad else 0)
