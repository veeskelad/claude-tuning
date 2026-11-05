#!/usr/bin/env python3
"""tuning-suggest.py во временном HOME: какие советы предлагаются, как ответ человека их прячет и пишется в журнал.
Запуск: python3 tests/tuning-suggest.test.py"""
import json, os, pathlib, shutil, subprocess, sys, tempfile, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "kit/bin/tuning-suggest.py"
ok = bad = 0


def check(name, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1
    else:
        bad += 1
        print("ПРОМАХ", name, extra)


tmp = pathlib.Path(tempfile.mkdtemp())
home = tmp / "home"
(home / ".claude/bin").mkdir(parents=True)
shutil.copy(ROOT / "kit/bin/repeat-scan.py", home / ".claude/bin/repeat-scan.py")
mem = home / ".claude/projects/-demo/memory"
mem.mkdir(parents=True)
(mem / "MEMORY.md").write_text("# Память\n" + "- строка\n" * 195, encoding="utf-8")
today = time.strftime("%Y-%m-%d")
(mem / "lessons-learned.md").write_text(
    "# Уроки\n\n## Миграция базы без бэкапа положила стенд оплаты (2025-03-01)\nтекст\n\n"
    f"## Миграция базы стенда оплаты снова без бэкапа ({today})\nопять\n", encoding="utf-8")
OUT = home / ".local/state/claude-tuning/suggestions.json"
FB = home / ".claude/tuning-feedback.jsonl"
env = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1")


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)


def ids():
    return [i["id"] for i in json.loads(OUT.read_text())["items"]]


r = run()
check("сбор: код 0", r.returncode == 0, r.stderr)
got = ids()
check("ревизии не было — совет self-tune", "self-tune" in got, got)
check("MEMORY.md у потолка — совет memory", "memory" in got, got)
check("урок записан снова — совет lesson:*", any(i.startswith("lesson:") for i in got), got)
items = json.loads(OUT.read_text())["items"]
check("у каждого совета заголовок, что сделает Claude и что отправить", all(i["title"] and i["what"] and i["prompt"] for i in items), items)
check("в текстах нет обрывков ссылок и ‹скрыто›", not any("‹скрыто›" in i["why"] or "http" in i["why"] for i in items), items)
check("self-tune отправляется фразой, не командой", not next(i for i in items if i["id"] == "self-tune")["prompt"].startswith("/"))
check("не больше четырёх", len(items) <= 4, items)

r = run("--answer", "memory", "never")
check("ответ: код 0", r.returncode == 0, r.stderr)
line = json.loads(FB.read_text().splitlines()[-1])
check("ответ в журнале", line["id"] == "memory" and line["answer"] == "never" and line["title"] == "Навести порядок в памяти", line)
check("ответ убрал совет из панели сразу", "memory" not in ids(), ids())
run()
check("«не предлагать» — не возвращается", "memory" not in ids(), ids())

lesson = next(i for i in ids() if i.startswith("lesson:"))
run("--answer", lesson, "later")
run()
check("«позже» — прячет на неделю", lesson not in ids(), ids())
with open(FB, "a") as f:  # «позже» восемь дней назад
    f.write(json.dumps({"at": int(time.time()) - 8 * 86400, "id": lesson, "answer": "later"}) + "\n")
run()
check("«позже» старше недели — снова виден", lesson in ids(), ids())

run("--answer", "self-tune", "done")
run()
check("«сделать» — self-tune спрятан на неделю", "self-tune" not in ids(), ids())
(home / ".claude/tuning-log.md").write_text("# Журнал\n")
run()
check("свежий журнал ревизии — совета нет", "self-tune" not in ids(), ids())

r = run("--answer", "x", "maybe")
check("неизвестный ответ — отказ", r.returncode != 0)
r = run("--print")
check("--print не пишет файл и печатает JSON", r.returncode == 0 and json.loads(r.stdout)["items"] is not None)

shutil.rmtree(tmp, ignore_errors=True)
print(f"tuning-suggest: {ok} проверок прошло, {bad} провалилось")
sys.exit(1 if bad else 0)
