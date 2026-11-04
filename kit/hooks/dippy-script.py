#!/usr/bin/env python3
"""dippy, который заглядывает в запускаемый скрипт и подставляет простые переменные.

1. `bash file.sh` dippy видит как «bash interactive» и всегда спрашивает: внутрь файла он не смотрит.
   Для `bash|sh|zsh [-eux…] <файл> [аргументы]` и для прямого запуска `./x.sh` / `/путь/x.sh` с шебангом
   оболочки команда подменяется текстом файла, и dippy судит каждую строку скрипта по своим правилам.
2. `S=/путь; … > $S/out` и `K="kubectl --context x"; $K get pods` dippy не разбирает и спрашивает.
   Переменную, которой в команде один раз присвоено буквальное значение, подставляем в текст, и dippy
   видит настоящий путь или команду. Переменные с `$(…)`, повторным присваиванием, `read` или `for`
   не трогаем — они остаются вопросом.
Остальные команды уходят в dippy как есть.

Нужен dippy (`pipx install dippy`); без него хук ничего не делает. Белый список команд — свой файл
`~/.dippy/config`, заготовка — `kit/dippy/config.example` (установщик кладёт её в `~/.dippy/config`, если своего файла нет).
Тест: `python3 tests/dippy-script.test.py` из репозитория набора.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

SHELLS = {"bash", "sh", "zsh"}
MAX_SIZE = 256 * 1024
NAME = r"[A-Za-z_][A-Za-z0-9_]*"
# Присваивание в начале простой команды (или после export/local/declare/readonly)
ASSIGN_RE = re.compile(
    rf"(?:^|(?<=[\s;&|(){{]))(?:(?:export|local|declare|readonly)\s+(?:-\w+\s+)*)?({NAME})=")
LITERAL_RE = re.compile(r"""("[^"$`\\]*"|'[^']*'|[^\s;&|$`()<>'"\\]*)(?=$|[\s;&|)])""")
OTHER_BIND_RE = re.compile(rf"\b(?:for|read(?:\s+-\w+)*|select|getopts\s+\S+)\s+((?:{NAME}\s*)+)")
FUNC_RE = re.compile(rf"(?:^|(?<=[\s;&|{{]))(?:function\s+({NAME})|({NAME})\s*\(\)\s*\{{)", re.M)
KUBECONFIG_RE = re.compile(r"(\b(?:kubectl|helm)\s+(?:-n\s+\S+\s+)?)--kubeconfig(?:=|\s+)\S+\s+")


def script_path(command: str, cwd: str) -> str | None:
    if "\n" in command.strip():
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not words:
        return None
    first = os.path.basename(words[0])
    if first in SHELLS:
        i = 1
        while i < len(words) and words[i].startswith("-"):
            # -c "код" и -s (stdin) — не файл; -o pipefail — флаг со значением
            if "c" in words[i] or "s" in words[i]:
                return None
            if words[i] == "-o":
                i += 1
            i += 1
        if i >= len(words):
            return None
        target, need_shebang = words[i], False
    elif "/" in words[0]:
        target, need_shebang = words[0], True
    else:
        return None
    path = os.path.expanduser(target)
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    if not os.path.isfile(path) or os.path.getsize(path) > MAX_SIZE:
        return None
    if need_shebang:
        try:
            with open(path, encoding="utf-8") as f:
                line = f.readline()
        except (OSError, UnicodeDecodeError):
            return None
        if not re.match(r"#!\s*\S*(?:/|env\s+)(?:ba|z)?sh\b", line):
            return None
    return path


def literal_vars(command: str) -> dict[str, str]:
    """Переменные, которым в тексте присвоено ровно одно буквальное значение."""
    values: dict[str, set] = {}
    bad: set[str] = set()
    for m in ASSIGN_RE.finditer(command):
        name = m.group(1)
        lit = LITERAL_RE.match(command, m.end())
        if not lit:
            bad.add(name)
            continue
        v = lit.group(1)
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        values.setdefault(name, set()).add(v)
    for m in OTHER_BIND_RE.finditer(command):
        bad.update(m.group(1).split())
    home = os.path.expanduser("~")
    out = {}
    for name, vs in values.items():
        if name in bad or len(vs) != 1:
            continue
        v = next(iter(vs))
        if v.startswith("~/") or v == "~":
            v = home + v[1:]
        out[name] = v
    return out


def substitute(command: str, env: dict[str, str]) -> str:
    """Подставить $X и ${X} вне одинарных кавычек."""
    if not env:
        return command
    ref = re.compile(r"\$(?:\{(" + NAME + r")\}|(" + NAME + r"))")
    out, i, n = [], 0, len(command)
    in_single = in_double = False
    while i < n:
        c = command[i]
        if c == "\\" and not in_single and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == "$" and not in_single:
            m = ref.match(command, i)
            if m:
                name = m.group(1) or m.group(2)
                if name in env:
                    out.append(env[name])
                    i = m.end()
                    continue
        out.append(c)
        i += 1
    return "".join(out)


def expand(command: str, cwd: str) -> tuple[str, str | None]:
    """Текст для второго прохода: скрипт вместо его запуска, подставленные переменные, kubectl без --kubeconfig."""
    path = script_path(command, cwd)
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                command = f.read()
        except (OSError, UnicodeDecodeError):
            path = None
    if "HOME" not in literal_vars(command):
        command = substitute(command, {"HOME": os.path.expanduser("~")})
    command = substitute(command, literal_vars(command))
    # dippy не узнаёт подкоманду, если перед ней стоит --kubeconfig; на действие флаг не влияет
    command = KUBECONFIG_RE.sub(r"\1", command)
    return command, path


def functions(command: str) -> list[str]:
    """Имена функций, объявленных в тексте; имя настоящей команды не берём, чтобы им не разрешить её саму."""
    names = {a or b for a, b in FUNC_RE.findall(command)}
    return sorted(n for n in names if not shutil.which(n))


def run_dippy(raw: str, functions: list[str]) -> tuple[dict | None, str, str, int]:
    """Один прогон dippy. Функции, объявленные в тексте, разрешаем: их тело dippy уже проверил при объявлении."""
    env = dict(os.environ)
    with tempfile.NamedTemporaryFile("w", prefix="dippy-fn-", suffix=".cfg") as extra:
        if functions and "DIPPY_CONFIG" not in env:
            extra.write("".join(f"allow {name}\n" for name in functions))
            extra.flush()
            env["DIPPY_CONFIG"] = extra.name
        res = subprocess.run(["dippy"], input=raw, capture_output=True, text=True, env=env)
    try:
        hook = json.loads(res.stdout).get("hookSpecificOutput") or {}
    except ValueError:
        hook = None
    return hook, res.stdout, res.stderr, res.returncode


def main() -> int:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not (isinstance(data, dict) and data.get("tool_name") == "Bash"):
        res = subprocess.run(["dippy"], input=raw, capture_output=True, text=True)
        sys.stdout.write(res.stdout)
        sys.stderr.write(res.stderr)
        return res.returncode

    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command") or ""
    hook, out, err, code = run_dippy(raw, functions(command))
    # Свои правила dippy (allow/deny по исходной команде) главнее: второй проход только для «ask»
    if hook is not None and hook.get("permissionDecision") == "ask":
        new, path = expand(command, data.get("cwd") or os.getcwd())
        if new != command:
            tool_input["command"] = new
            hook2, out2, err2, code2 = run_dippy(json.dumps(data), functions(new))
            if hook2 is not None:
                if path and hook2.get("permissionDecisionReason"):
                    hook2["permissionDecisionReason"] = f"{os.path.basename(path)}: {hook2['permissionDecisionReason']}"
                out, err, code = json.dumps({"hookSpecificOutput": hook2}, ensure_ascii=False), err2, code2
    # В auto и плане «ask» dippy значит «нет в белом списке», а не «опасно», но перебивает классификатор auto
    # и плодит лишние окна. Молчим — решает сам режим. Опасное ловят стражи (safety-guard, secret-guard-bash);
    # allow и deny dippy остаются.
    if data.get("permission_mode") in ("auto", "plan"):
        try:
            final = json.loads(out).get("hookSpecificOutput") or {}
        except ValueError:
            final = {}
        if final.get("permissionDecision") == "ask":
            return 0
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


if __name__ == "__main__":
    sys.exit(main())
