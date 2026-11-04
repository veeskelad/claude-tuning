#!/usr/bin/env python3
"""Безопасная работа со значениями секретов: агент их ПЕРЕНОСИТ и СВЕРЯЕТ, но не читает.

Значение никогда не попадает в stdout/stderr, а значит и в транскрипт: транскрипт лежит на диске
и может уйти наружу. Наружу выходят только имена ключей, длина, sha256[:12] и факт записи.

Зачем отдельный инструмент: команда вида `sops -d secrets.yml | grep -A6 some_key` выглядит
точечной, но -A6 захватывает соседние строки и выводит в чат чужие боевые пароли. Любая форма
«расшифровал → отфильтровал в шелле» имеет такой режим отказа: фильтр промахивается, а вывод
уже в контексте. Здесь фильтрация идёт ДО того, как что-то печатается.

    secret-value.py names <file>                              имена ключей + есть/пусто
    secret-value.py fp    <file> <key>                        sha256[:12] — сверить две стороны
    secret-value.py len   <file> <key>                        длина значения
    secret-value.py copy  <file> <key> <dest> [VAR]           значение → строка VAR=... в dest (chmod 600)
    secret-value.py push  <file> <key> <host> <dest> [VAR]    то же, но в файл на сервере по ssh
    secret-value.py exec  <file> <key> [VAR] -- <команда…>    значение только в окружение команды,
                                                              её вывод очищается от значения и secret-mask.py

<file> — .env, *.json, *.yml/*.yaml, *.toml (как «ключ = значение») либо sops-зашифрованный YAML/JSON
(распознаётся по ключу `sops:`, расшифровывается в память, на диск не ложится; нужен установленный sops).
Вложенные ключи адресуются через точку: db.password. <host> — любое имя, которое понимает ssh
(из ~/.ssh/config или user@host): в скрипте нет ни одного зашитого сервера.
"""
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys


def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


def read_plain(path):
    raw = pathlib.Path(path).read_text()
    if re.search(r'^sops:', raw, re.M) or '"sops"' in raw[:4096]:
        env = dict(os.environ)
        env.setdefault("SOPS_AGE_KEY_FILE", os.path.expanduser("~/.config/sops/age/keys.txt"))
        r = subprocess.run(["sops", "-d", path], capture_output=True, text=True, env=env)
        if r.returncode != 0:
            die("sops не смог расшифровать файл (stderr скрыт: там бывают фрагменты значений)")
        return r.stdout
    return raw


def flatten(node, prefix=""):
    out = {}
    if isinstance(node, dict):
        for k, v in node.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.update(flatten(v, f"{prefix}{i}."))
    else:
        out[prefix.rstrip(".")] = node
    return out


# Ключи, которые в .env встретились больше одного раза (файл с блоками под разные окружения):
# парсер берёт последнее значение, поэтому о повторе надо сказать вслух, иначе два окружения
# молча разойдутся одним набором значений.
DUPES = {}


def parse(text):
    if text.lstrip().startswith("{"):
        try:
            return flatten(json.loads(text))
        except Exception:
            pass
    try:
        import yaml
        d = yaml.safe_load(text)
        if isinstance(d, (dict, list)):
            return flatten(d)
    except Exception:
        pass
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        k, _, v = line.partition("=")
        k = k.strip()
        if k in out:
            DUPES[k] = DUPES.get(k, 1) + 1
        out[k] = v.strip().strip('"').strip("'")
    return out


def load(path):
    data = parse(read_plain(path))
    # служебные поля sops — метаданные шифрования, наружу не нужны
    return {k: v for k, v in data.items() if not k.startswith("sops.")}


def pick(data, key):
    if key not in data:
        near = [k for k in sorted(data) if key.lower() in k.lower()][:5]
        die(f"нет ключа {key!r}" + (f"; похожие: {', '.join(near)}" if near else ""))
    v = data[key]
    return "" if v is None else str(v)


def fp(val):
    return hashlib.sha256(val.encode()).hexdigest()[:12]


def warn_whitespace(name, val):
    """Ведущий/хвостовой пробел в значении — почти всегда артефакт записи в хранилище, но заметить
    его, не прочитав значение, нельзя. Поэтому предупреждаем по факту наличия, не показывая значение.
    Без кавычек такой пробел развалил бы строку `VAR=значение` на `VAR=` и команду, а оболочка
    напечатала бы значение в тексте ошибки `command not found`."""
    if val != val.strip():
        print(
            f"ВНИМАНИЕ: значение {name} содержит ведущий/хвостовой пробел — "
            f"вероятно, артефакт записи в хранилище. Записано в кавычках; исправь источник.",
            file=sys.stderr,
        )


def write_kv(dest, name, val):
    p = pathlib.Path(dest)
    lines = p.read_text().splitlines() if p.exists() else []
    lines = [l for l in lines if not re.match(rf"^{re.escape(name)}=", l)]
    # shlex.quote обязателен: без него пробел внутри значения превращает строку
    # в `VAR= команда`, и оболочка выполняет значение, выводя его в stderr.
    lines.append(f"{name}={shlex.quote(val)}")
    p.write_text("\n".join(lines) + "\n")
    os.chmod(p, 0o600)


def var_name(key, override):
    name = override or key.upper().replace(".", "_").replace("-", "_")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        die(f"{name!r} не годится как имя переменной: нужны буквы, цифры и _ (задай своё четвёртым/пятым аргументом)")
    return name


def remote_path(p):
    """Путь для удалённой оболочки: ~/ раскрывается на сервере, остальное — в кавычках."""
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    return shlex.quote(p)


def main(argv):
    if len(argv) < 3:
        die(__doc__)
    mode, path = argv[1], argv[2]
    if not pathlib.Path(path).is_file():
        die(f"нет такого файла: {path}")
    data = load(path)
    for k, n in sorted(DUPES.items()):
        print(f"⚠ {k}: {n} значения в файле, берётся последнее — файл с блоками под разные окружения "
              "стоит разделить", file=sys.stderr)

    if mode == "names":
        for k in sorted(data):
            v = data[k]
            print(f"{k}\t{'пусто' if v in (None, '', []) else 'есть'}{'  ⚠ повтор' if k in DUPES else ''}")
        return

    if len(argv) < 4:
        die(f"usage: secret-value.py {mode} <file> <key> ...")
    key = argv[3]
    val = pick(data, key)

    if mode == "fp":
        print(fp(val))
    elif mode == "len":
        print(len(val))
    elif mode == "copy":
        if len(argv) < 5:
            die("usage: secret-value.py copy <file> <key> <dest> [VAR]")
        dest = argv[4]
        name = var_name(key, argv[5] if len(argv) > 5 else None)
        write_kv(dest, name, val)
        warn_whitespace(name, val)
        print(f"{name} записан в {dest} (длина {len(val)}, sha256:{fp(val)})")
    elif mode == "push":
        if len(argv) < 6:
            die("usage: secret-value.py push <file> <key> <host> <dest> [VAR]")
        host, rdest = argv[4], argv[5]
        if host.startswith("-"):
            die("имя хоста не может начинаться с «-»")
        name = var_name(key, argv[6] if len(argv) > 6 else None)
        d = remote_path(rdest)
        # umask 077 + chmod ПОСЛЕ mv: chmod до записи бесполезен, потому что mv заменяет inode,
        # и файл получает права по umask. Значение идёт через stdin, а не через argv: в argv его видит `ps`.
        remote = (
            f"umask 077; touch {d}; "
            f"line=$(cat); name=${{line%%=*}}; "
            f"grep -v \"^$name=\" {d} > {d}.tmp 2>/dev/null || true; "
            f"printf '%s\\n' \"$line\" >> {d}.tmp; mv {d}.tmp {d}; "
            f"chmod 600 {d}"
        )
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "--", host, remote],
            input=f"{name}={shlex.quote(val)}\n", capture_output=True, text=True,
        )
        if r.returncode != 0:
            die("ssh-перенос не удался (stderr скрыт — там бывает эхо строки со значением)")
        warn_whitespace(name, val)
        print(f"{name} перенесён на {host}:{rdest} (длина {len(val)}, sha256:{fp(val)})")
    elif mode == "exec":
        # Подставить значение в команду, не печатая его: VAR уходит только в окружение дочернего
        # процесса, вывод которого очищается (команда может эхнуть значение: curl -v, set -x).
        rest = argv[4:]
        name = var_name(key, rest[0] if rest and rest[0] != "--" else None)
        if "--" not in rest or rest.index("--") == len(rest) - 1:
            die("usage: secret-value.py exec <file> <key> [VAR] -- <команда…>")
        command = rest[rest.index("--") + 1:]
        env = dict(os.environ, **{name: val})
        r = subprocess.run(command, env=env, capture_output=True, text=True)
        import importlib.util
        mask_path = pathlib.Path(__file__).with_name("secret-mask.py")
        if not mask_path.is_file():
            die("рядом нет secret-mask.py: без маски вывод команды печатать нельзя")
        spec = importlib.util.spec_from_file_location("secret_mask", mask_path)
        masker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(masker)
        # Известное значение скрываем точно, а не по шаблону: маска узнаёт только токены знакомой формы.
        label = f"‹secret {name} sha256:{fp(val)} len:{len(val)}›"
        hide = (lambda t: t.replace(val, label)) if val else (lambda t: t)
        sys.stdout.write(masker.mask(hide(r.stdout)))
        sys.stderr.write(masker.mask(hide(r.stderr)))
        sys.exit(r.returncode)
    else:
        die(f"неизвестный режим {mode!r}")


if __name__ == "__main__":
    main(sys.argv)
