#!/usr/bin/env python3
"""Проверка secret-value.py на выдуманном .env во временной папке: значения не должны попадать в вывод.
Запуск: python3 tests/secret-value.test.py. Настоящих секретов не читает; ssh подменён заглушкой."""
import hashlib, os, pathlib, secrets, shutil, string, subprocess, sys, tempfile

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "kit/hooks/secret-value.py"
ok = bad = 0


def check(name, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1
    else:
        bad += 1
        print("ПРОМАХ", name, extra)


def run(*args, env=None):
    e = dict(env or os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=e)


tmp = pathlib.Path(tempfile.mkdtemp())
alnum = string.ascii_letters + string.digits
v_token = "".join(secrets.choice(alnum) for _ in range(32))
v_space = "".join(secrets.choice(alnum) for _ in range(12)) + " " + "".join(secrets.choice(alnum) for _ in range(12))
env_file = tmp / "app.env"
env_file.write_text(f"# комментарий\nAPI_TOKEN={v_token}\nexport SPACED=\"{v_space}\"\nEMPTY=\nAPI_TOKEN={v_token}\n")


def no_leak(name, r):
    out = r.stdout + r.stderr
    check(name + ": значение не в выводе", v_token not in out and v_space not in out)


# names
r = run("names", str(env_file))
check("names: есть/пусто", r.returncode == 0 and "API_TOKEN\tесть" in r.stdout and "EMPTY\tпусто" in r.stdout, r.stdout)
check("names: повтор отмечен", "повтор" in r.stdout)
no_leak("names", r)

# fp / len
r = run("fp", str(env_file), "API_TOKEN")
check("fp: sha256[:12]", r.stdout.strip() == hashlib.sha256(v_token.encode()).hexdigest()[:12], r.stdout)
no_leak("fp", r)
r = run("len", str(env_file), "API_TOKEN")
check("len", r.stdout.strip() == "32", r.stdout)
r = run("fp", str(env_file), "API_TOKE")
check("нет ключа: ошибка и похожие имена", r.returncode != 0 and "API_TOKEN" in r.stderr)

# copy: значение в файле, права 600, в выводе нет; пробел в значении не разваливает строку
dest = tmp / "out.env"
r = run("copy", str(env_file), "SPACED", str(dest), "MY_VAR")
no_leak("copy", r)
check("copy: сообщение", "MY_VAR записан" in r.stdout, r.stdout)
check("copy: права 600", oct(dest.stat().st_mode & 0o777) == "0o600")
sh = subprocess.run(["bash", "-c", f'. "{dest}" && printf %s "$MY_VAR"'], capture_output=True, text=True)
check("copy: оболочка читает значение целиком", sh.stdout == v_space, sh.stderr)
run("copy", str(env_file), "API_TOKEN", str(dest))
check("copy: дописывает, не затирая", "MY_VAR=" in dest.read_text() and "API_TOKEN=" in dest.read_text())
check("copy: плохое имя переменной — отказ", run("copy", str(env_file), "API_TOKEN", str(dest), "bad name").returncode != 0)

# push: ssh подменён — исполняет удалённую команду локально в песочнице
bin_dir = tmp / "bin"
bin_dir.mkdir()
fake = bin_dir / "ssh"
fake.write_text('#!/usr/bin/env bash\nwhile [ "$1" != "--" ]; do shift; done; shift; host=$1; shift\n'
                f'echo "$host" > "{tmp}/ssh-host"\nHOME="{tmp}/remote-home" exec bash -c "$1"\n')
fake.chmod(0o755)
(tmp / "remote-home").mkdir()
e = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
r = run("push", str(env_file), "API_TOKEN", "any-host", "~/sub dir/.env", "REMOTE_TOKEN", env=e)
check("push: нет каталога на сервере — ошибка без значения", r.returncode != 0 and v_token not in r.stdout + r.stderr)
(tmp / "remote-home" / "sub dir").mkdir()
r = run("push", str(env_file), "API_TOKEN", "any-host", "~/sub dir/.env", "REMOTE_TOKEN", env=e)
no_leak("push", r)
remote = tmp / "remote-home" / "sub dir" / ".env"
check("push: хост взят из аргумента", (tmp / "ssh-host").read_text().strip() == "any-host")
check("push: файл записан, права 600", remote.exists() and oct(remote.stat().st_mode & 0o777) == "0o600")
check("push: значение на месте", f"REMOTE_TOKEN={v_token}" in remote.read_text())
check("push: хост с «-» отклонён", run("push", str(env_file), "API_TOKEN", "-oProxyCommand=x", "~/.env", env=e).returncode != 0)

# exec: значение в окружении команды, вывод проходит через маску
r = run("exec", str(env_file), "API_TOKEN", "TOK", "--", "bash", "-c", 'test -n "$TOK" && echo ok-env')
check("exec: команда видит переменную", r.stdout.strip() == "ok-env", r.stderr)
r = run("exec", str(env_file), "API_TOKEN", "TOK", "--", "bash", "-c", 'echo "$TOK"')
check("exec: эхо значения замаскировано", v_token not in r.stdout + r.stderr, r.stdout)

# JSON, вложенный ключ
jf = tmp / "c.json"
jf.write_text('{"db": {"password": "%s"}}' % v_token)
r = run("fp", str(jf), "db.password")
check("json: вложенный ключ", r.stdout.strip() == hashlib.sha256(v_token.encode()).hexdigest()[:12], r.stderr)

# В исходнике нет зашитых серверов
src = SCRIPT.read_text()
check("в коде нет зашитых хостов", "ssh-host-alias" not in src and "@" not in src.replace("user@host", ""))

shutil.rmtree(tmp)
print(f"secret-value: ok={ok} bad={bad}")
sys.exit(1 if bad else 0)
