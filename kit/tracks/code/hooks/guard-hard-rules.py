#!/usr/bin/env python3
"""PreToolUse: жёсткие правила, которые видно по вводу инструмента (Bash, Write/Edit, MCP).

Зачем. CLAUDE.md — подсказка, а не настройка: каждая строка конкурирует с остальными, и «никогда X»
соблюдается «обычно». Жёсткое правило — в хук, он блокирует действие; в CLAUDE.md остаются мягкие
конвенции.

Хук только запрещает и называет замену в причине. Команду не переписывает: два хука с `updatedInput`
на один вызов Bash гоняются друг с другом (какой применится — не определено), переписью владеет
secret-guard-bash.py.

Правила (имя — в тексте отказа):
  ai-signature   подпись ИИ в коммите, MR/PR, комментарии трекера, письме, мессенджере
                 (Co-Authored-By: Claude, «Generated with Claude», Claude-Session:). По умолчанию
                 выключено: подпись честно показывает, что текст написал ИИ. Включить —
                 CLAUDE_GUARD_NO_AI_SIGNATURE=1 (вместе с пустой attribution в settings.json)
  no-verify      git --no-verify, commit -n, -c core.hooksPath=… → исправить причину падения хука
  worktree-base  новая ветка worktree от локального main/HEAD → от origin/<основная ветка> после fetch;
                 от origin/<ветка>, если fetch репо был 30+ минут назад и его нет в той же команде
                 → та же команда с git fetch впереди
  main-copy      Write/Edit файла в основной копии клона на основной ветке (main/master или та, на
                 которую смотрит origin/HEAD) → worktree от origin/<ветка>, commit, push, PR.
                 Игнорируемые git файлы проходят. Правки через Bash (sed -i, >) хук не видит.

Что здесь намеренно нет: запрета новых файлов прямо в $HOME — это делает отдельный хук home-clutter.sh.

Настройка (все выключатели необязательны):
  CLAUDE_GUARD_SKIP=main-copy,no-verify   отключить правила по имени (в env settings.json или шелла)
  CLAUDE_FRESHNESS_OFF=1                  не проверять возраст fetch в worktree-base (клон без сети
                                          или .git приходит синхронизацией); проверка «не от локального
                                          main» остаётся
  git config claude.allowMainEdits true   в этом репо main-copy разрешён (например, личные заметки,
                                          дотфайлы, репо без PR-процесса)
  main-copy действует только в клонах (есть remote origin): свежий `git init` без origin можно править.

Любой сбой хука — пропуск (exit 0): страж не должен ронять инструмент.
Тесты: python3 tests/guard-hard-rules.test.py (из репозитория набора)
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time

HOME = os.path.expanduser("~")

SIGNATURE = re.compile(
    r"Co-Authored-By:[^\n]*(Claude|anthropic)|Generated with \[?Claude|🤖 Generated|"
    r"Claude-Session:|noreply@anthropic\.com", re.I)
MCP_WRITE = re.compile(r"(create|save|update|reply|send|edit|post|comment|note|upload|forward|share)", re.I)

SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "|&", "\n"}
WRAPPERS = {"sudo", "env", "nohup", "time", "exec", "command", "xargs", "then", "do", "else", "!"}
# Базы, от которых новую ветку не начинают: они локальные и молча отстают от origin
LOCAL_BASES = {"main", "master", "HEAD", "@", "develop", "dev", "trunk"}
FETCH_MAX_AGE = 30 * 60


def skipped(rule):
    # подпись ИИ запрещается только по явному выбору человека
    if rule == "ai-signature" and os.environ.get("CLAUDE_GUARD_NO_AI_SIGNATURE") != "1":
        return True
    return rule in {x.strip() for x in os.environ.get("CLAUDE_GUARD_SKIP", "").split(",")}


def deny(rule, why, instead):
    if skipped(rule):
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"⛔ {rule}: {why} Вместо этого: {instead} "
                                    f"(жёсткое правило, хук guard-hard-rules.py; отключить: CLAUDE_GUARD_SKIP={rule})",
    }}, ensure_ascii=False))
    sys.exit(0)


def tokens(cmd):
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|()")
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return cmd.split()


def simple_commands(cmd):
    """argv каждой простой команды: разбито по ; && || | &, без VAR=… и обёрток sudo/env/exec."""
    cur, wrapped = [], False
    for t in tokens(cmd) + [";"]:
        if t in SEPARATORS or set(t) <= set(";&|()"):
            if cur:
                yield cur
            cur, wrapped = [], False
            continue
        if not cur and (t in WRAPPERS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) or (wrapped and t.startswith("-"))):
            wrapped = wrapped or t in WRAPPERS
            continue
        cur.append(t)


def with_cwd(cmd, cwd):
    """(каталог, argv) для каждой простой команды, с учётом cd внутри той же строки."""
    d = cwd
    for argv in simple_commands(cmd):
        if argv[0] == "cd" and len(argv) > 1:
            d = os.path.normpath(os.path.join(d, os.path.expanduser(argv[1])))
            continue
        yield d, argv


def git_out(d, *args):
    try:
        p = subprocess.run(["git", "-C", d, *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def git_sub(argv):
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in ("-C", "-c") else 1
    return argv[i] if i < len(argv) else "", argv[i + 1:]


def git_workdir(d, argv):
    """Каталог, в котором выполнится git: cwd плюс все -C по порядку."""
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] == "-C" and i + 1 < len(argv):
            d = os.path.join(d, os.path.expanduser(argv[i + 1]))
        i += 2 if argv[i] in ("-C", "-c") else 1
    return d


def default_branch(repo):
    """Основная ветка клона: на что смотрит origin/HEAD, иначе main."""
    ref = git_out(repo, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    return ref.split("/", 1)[1] if ref and "/" in ref else "main"


# --- ai-signature ---------------------------------------------------------------------------------

def publishes_text(argv):
    """Команда, которая отправляет текст наружу: коммит, PR/MR, комментарий, запрос с телом."""
    name = os.path.basename(argv[0])
    if name == "git":
        return git_sub(argv)[0] in ("commit", "tag", "notes")
    if name in ("glab", "gh"):
        return bool(set(argv) & {"create", "note", "update", "comment", "edit", "new", "review"}) or (
            argv[1:2] == ["api"] and bool(set(argv) & {"-f", "-F", "--field", "--raw-field", "--input", "-X"}))
    if name == "curl":
        return any(a.startswith(("-d", "--data", "--json", "-F", "--form")) for a in argv[1:])
    return False


def check_signature_bash(cmd, cwd):
    writers = [(d, argv) for d, argv in with_cwd(cmd, cwd) if publishes_text(argv)]
    if not writers:
        return
    hit = SIGNATURE.search(cmd)
    if not hit:
        # git commit -F файл: текст лежит в файле, а не в команде
        for d, argv in writers:
            if argv[0] == "git":
                for i, a in enumerate(argv):
                    path = argv[i + 1] if a in ("-F", "--file") and i + 1 < len(argv) else \
                        a.split("=", 1)[1] if a.startswith("--file=") else None
                    if path and path != "-":
                        p = os.path.join(d, os.path.expanduser(path))
                        if os.path.isfile(p) and os.path.getsize(p) < 1_000_000:
                            hit = hit or SIGNATURE.search(open(p, encoding="utf-8", errors="ignore").read())
    if hit:
        deny("ai-signature", f"в тексте коммита, PR или комментария подпись ИИ («{hit.group(0)[:40]}»).",
             "убрать строку целиком; подписи не ставятся ни в коммитах, ни в PR, ни в трекерах.")


def check_mcp(tool, inp):
    if not MCP_WRITE.search(tool.rsplit("__", 1)[-1]):
        return
    stack = [inp]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
        elif isinstance(v, str):
            hit = SIGNATURE.search(v)
            if hit:
                deny("ai-signature", f"в тексте для {tool} подпись ИИ («{hit.group(0)[:40]}»).",
                     "убрать строку; подписи не ставятся в PR, трекерах и мессенджерах.")


# --- no-verify ------------------------------------------------------------------------------------

GIT_ARG_OPTS = {"-m", "-F", "-c", "-C", "-t", "--message", "--file", "--template", "--author", "--date",
                "--reuse-message", "--reedit-message", "--fixup", "--squash", "--cleanup", "--trailer"}


def check_no_verify(cmd):
    for argv in simple_commands(cmd):
        if os.path.basename(argv[0]) != "git":
            continue
        hooks_off = any(a.startswith("core.hooksPath=") for i, a in enumerate(argv) if i and argv[i - 1] == "-c")
        sub, rest = git_sub(argv)
        short_n, skip = False, False
        for a in rest:
            if skip:
                skip = False
                continue
            if a in GIT_ARG_OPTS:
                skip = True
            elif sub == "commit" and re.match(r"^-[a-zA-Z]+$", a):
                flags = re.split(r"[mFcCt]", a[1:], maxsplit=1)[0]  # после -m/-F… идёт значение
                short_n = short_n or "n" in flags
        if "--no-verify" in rest or short_n or hooks_off:
            deny("no-verify", f"git {sub} в обход git-хуков (pre-commit, commit-msg, pre-push).",
                 "прочитать, что именно упало, исправить причину и повторить команду без обхода. "
                 "Сознательный обход делает человек в терминале.")


# --- worktree-base --------------------------------------------------------------------------------

def remote_base(d, base):
    """(remote, ветка) для базы origin/main, remotes/origin/main, refs/remotes/origin/main; иначе None."""
    ref = re.sub(r"^(refs/)?remotes/", "", base)
    if "/" not in ref:
        return None
    remote, branch = ref.split("/", 1)
    return (remote, branch) if remote in (git_out(d, "remote") or "").split() else None


def last_fetch_age(d):
    """Секунды с последнего fetch по FETCH_HEAD общего git-каталога (у linked worktree он общий с основным)."""
    common = git_out(d, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    path = os.path.join(common, "FETCH_HEAD")
    return time.time() - os.path.getmtime(path) if os.path.exists(path) else float("inf")


def check_worktree_base(cmd, cwd):
    fetched = False
    for d, argv in with_cwd(cmd, cwd):
        if argv[0] != "git":
            continue
        sub, rest = git_sub(argv)
        if sub in ("fetch", "pull") or (sub == "remote" and rest[:1] == ["update"]):
            fetched = True
            continue
        if sub != "worktree" or rest[:1] != ["add"]:
            continue
        git_opts = argv[:len(argv) - len(rest) - 1]
        branch, orphan, pos, j, rest = None, False, [], 0, rest[1:]
        while j < len(rest):
            a = rest[j]
            if a in ("-b", "-B") and j + 1 < len(rest):
                branch = rest[j + 1]
                j += 2
                continue
            if a == "--reason":
                j += 2
                continue
            if a == "--orphan":
                orphan = True
            elif not a.startswith("-"):
                pos.append(a)
            j += 1
        if not branch or orphan:
            continue
        base = pos[1] if len(pos) > 1 else None
        if base is None or base in LOCAL_BASES or base.startswith("HEAD~"):
            main = default_branch(git_workdir(d, argv))
            deny("worktree-base", f"новая ветка {branch} от {base or 'текущего HEAD'}: локальная ветка молча "
                                  "отстаёт от origin, и работа начнётся со старого кода.",
                 f"git fetch origin {main} && git worktree add {pos[0] if pos else '<путь>'} -b {branch} origin/{main}")
        if fetched or os.environ.get("CLAUDE_FRESHNESS_OFF") == "1":
            continue
        repo = git_workdir(d, argv)
        rb = remote_base(repo, base)
        age = last_fetch_age(repo) if rb else None
        if age is None or age <= FETCH_MAX_AGE:
            continue
        remote, ref = rb
        when = "ни разу" if age == float("inf") else \
            f"{int(age // 3600)} ч назад" if age >= 7200 else f"{int(age // 60)} мин назад"
        cd = f"cd {shlex.quote(d)} && " if os.path.normpath(d) != os.path.normpath(cwd) else ""
        fetch = shlex.join(git_opts + ["fetch", remote] + ([] if ref == "HEAD" else [ref]))
        deny("worktree-base", f"новая ветка {branch} от {base}, а {remote} в этом репо обновлялся {when}: "
                              "ветка начнётся со старого кода.",
             f"{cd}{fetch} && {shlex.join(argv)}")


# --- main-copy ------------------------------------------------------------------------------------

def main_copy(path):
    """(репо, ветка), если path с учётом симлинков лежит в основной копии клона на основной ветке и не
    игнорируется git. Linked worktree (.git — файл), репо без origin и репо с claude.allowMainEdits не в счёт."""
    p = os.path.realpath(os.path.expanduser(path))
    if ".tasks" in p.split(os.sep):
        return None
    d = os.path.dirname(p)
    while d and not os.path.isdir(d):  # файл новый, каталога ещё может не быть
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    top = git_out(d, "rev-parse", "--show-toplevel")
    if not top:
        return None
    repo = os.path.realpath(top)
    if repo == HOME or not os.path.isdir(os.path.join(repo, ".git")):
        return None  # worktree или подмодуль: .git — файл
    if p == os.path.join(repo, ".git") or p.startswith(os.path.join(repo, ".git") + os.sep):
        return None
    if "origin" not in (git_out(repo, "remote") or "").split():
        return None
    if (git_out(repo, "config", "--get", "claude.allowMainEdits") or "").lower() == "true":
        return None
    branch = git_out(repo, "symbolic-ref", "-q", "--short", "HEAD")
    if branch not in {"main", "master", default_branch(repo)}:
        return None
    try:
        ignored = subprocess.run(["git", "-C", repo, "check-ignore", "-q", "--no-index", os.path.relpath(p, repo)],
                                 capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None
    return None if ignored else (repo, branch)


def check_file_tools(tool, inp):
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    hit = path and main_copy(path)
    if hit:
        repo, branch = hit
        rel = os.path.relpath(os.path.realpath(os.path.expanduser(path)), repo)
        deny("main-copy", f"{rel} лежит в основной копии {os.path.basename(repo)} на {branch}: правка не уйдёт "
                          "в origin и остановит автообновление основной ветки.",
             f"git -C {repo} fetch origin {branch} && git -C {repo} worktree add -b <тип>/<тема> "
             f"{repo}-worktrees/<тема> origin/{branch}; править там, сразу commit, push и PR "
             "(файл за симлинком ведёт в основную копию, он тоже здесь).")


def main():
    data = json.load(sys.stdin)
    tool = data.get("tool_name", "")
    inp = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()
    if tool == "Bash":
        cmd = inp.get("command") or ""
        check_signature_bash(cmd, cwd)
        check_worktree_base(cmd, cwd)
        check_no_verify(cmd)
    elif tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        check_file_tools(tool, inp)
    elif tool.startswith("mcp__"):
        check_mcp(tool, inp)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        pass
    sys.exit(0)
