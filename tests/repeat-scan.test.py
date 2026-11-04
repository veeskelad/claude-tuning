#!/usr/bin/env python3
"""Проверка repeat-scan.py на выдуманных транскриптах во временной папке: повторы просьб, классы вмешательств,
секреты, побочные записи. Настоящие транскрипты не читаются (HOME подменён).
Запуск: python3 tests/repeat-scan.test.py"""
import importlib.util, json, os, pathlib, shutil, subprocess, sys, tempfile

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "kit/bin/repeat-scan.py"
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
proj = home / ".claude/projects/-demo-proj"
proj.mkdir(parents=True)
n_uuid = [0]


def rec(kind, content, day="01", **extra):
    n_uuid[0] += 1
    r = {"type": kind, "uuid": f"u{n_uuid[0]}", "timestamp": f"2026-01-{day}T10:00:00Z", "message": {"content": content}}
    r.update(extra)
    return json.dumps(r, ensure_ascii=False, separators=(",", ":"))


def human(t, **extra):
    return rec("user", t, **extra)  # day="NN" — другой день: одинаковый текст в один день считается копией


def agent(t):
    return rec("assistant", [{"type": "text", "text": t}])


def write(name, lines):
    (proj / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(*args):
    env = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, str(SCRIPT), "--projects", str(home / ".claude/projects"), *args],
                          capture_output=True, text=True, env=env)


# --- Разговор A: по одной реплике каждого класса, строки считаются с 1 ---
longtail = "и ещё много текста про внутренние дела " * 6
write("aaaaaaaa-1.jsonl", [
    human("Сделай экспорт отчёта в файл"),                                            # 1: прочее
    agent("Готово. Выкатить на прод?"),                                               # 2: вопрос
    human("да"),                                                                      # 3: решение (после вопроса)
    agent("Выкатил."),                                                                # 4: не вопрос
    human("продолжай"),                                                               # 5: напоминание
    human("а где результат проверки?"),                                               # 6: напоминание
    human("вот результат, теперь допиши отчёт\n" + "\n".join(f"строка вывода {i}" for i in range(5))),  # 7: перенос
    human("Ты забыл обновить тесты, опять"),                                          # 8: поправка
    human("кстати, у нас теперь новый домен https://example.org/docs"),               # 9: новые сведения
    human("не мержи, пока я сам не посмотрю"),                                        # 10: решение (придержал)
    human("да, конечно"),                                                             # 11: без вопроса, прочее
    human("вот лог: " + longtail),                                                    # 12: перенос, длинная реплика
])

# --- Разговор B: то, что читать нельзя ---
secret1 = "sk-" + "Zx9Qw3Er5Ty7Ui1Op2As4Df"
secret2 = "hunter2correcthorse"
write("bbbbbbbb-1.jsonl", [
    human(f"держи ключ, продолжай: export OPENAI_KEY={secret1}", day="02"),                    # 1: секрет-токен
    human(f"пароль: {secret2}, дальше", day="02"),                                              # 2: секрет-пароль
    human("опять не так, продолжай", day="02", isSidechain=True),                               # 3: побочная запись
    rec("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "вот результат: ты забыл"}], day="02", toolUseResult={"x": 1}),  # 4
    human("<system-reminder>Ты забыл про продолжай, дальше</system-reminder>", day="02"),       # 5: служебный текст
    human("продолжай <system-reminder>hook: опять не так</system-reminder>", day="02"),         # 6: человек + вставка хука
    human("Caveat: ты забыл всё", day="02"),                                                    # 7: служебный префикс
    human("<command-name>/clear</command-name>", day="02"),                                     # 8
])

# --- Старое: три похожие просьбы в трёх разговорах ---
for k in "123":
    write(f"cccc000{k}-1.jsonl", [human("после мержа проверь прод и напиши результат проверки в чат", day=f"1{k}")])

# --- 1. Без флага: старый отчёт на месте, раздела 6 нет ---
r = run()
check("старый режим: выход 0", r.returncode == 0, r.stderr)
check("старый режим: повторённая просьба найдена", "## 1. Повторённые просьбы" in r.stdout and "**3 раз, разговоров 3**" in r.stdout, r.stdout)
check("старый режим: поправки считаются", "## 2. Поправки" in r.stdout and "Ты забыл обновить тесты, опять" in r.stdout)
check("старый режим: разделы 3-5 на месте", "## 3. " in r.stdout and "## 4. " in r.stdout)
check("старый режим: раздела 6 нет", "## 6." not in r.stdout)

# --- 2. С флагом ---
r = run("--interventions", "--examples", "5")
out = r.stdout
check("флаг: выход 0", r.returncode == 0, r.stderr)
check("флаг: старые разделы не пропали", "**3 раз, разговоров 3**" in out and "## 2. Поправки" in out)
check("флаг: раздел 6 есть", "## 6. Вмешательства человека" in out)


def section(title):
    i = out.index("### " + title)
    j = out.find("\n### ", i + 1)
    return out[i:j if j > 0 else len(out)]


A = "aaaaaaaa-1.jsonl"
dec = section("Решение, оставленное намеренно")
check("решение: «да» после вопроса, строка 3", f"{A}:3 " in dec and "«да»" in dec, dec)
check("решение: придержанный шаг, строка 10", f"{A}:10 " in dec, dec)
check("решение: «да» без вопроса не решение", f"{A}:11 " not in dec, dec)
tr = section("Ручной перенос данных")
check("перенос: «вот результат, теперь», строка 7", f"{A}:7 " in tr, tr)
check("перенос: длинный «вот лог», строка 12", f"{A}:12 " in tr, tr)
rem = section("Напоминание о следующем шаге")
check("напоминание: «продолжай», строка 5", f"{A}:5 " in rem and "«продолжай»" in rem, rem)
check("напоминание: «а где …», строка 6", f"{A}:6 " in rem, rem)
cor = section("Исправление ошибки агента")
check("поправка: «ты забыл», строка 8", f"{A}:8 " in cor, cor)
inf = section("Новые сведения")
check("сведения: домен, строка 9", f"{A}:9 " in inf, inf)

# --- 3. Приватность и фильтры ---
B = "bbbbbbbb-1.jsonl"
check("секрет-токен не в выводе", secret1 not in out and "OPENAI_KEY" not in out)
check("секрет-пароль не в выводе", secret2 not in out)
check("секретных пропущено 2", "пропущено как похожие на секреты: 2" in out, out[:600])
check("побочная запись не прочитана", f"{B}:3 " not in out)
check("результат инструмента не прочитан", f"{B}:4 " not in out)
check("служебный текст хука не прочитан", f"{B}:5 " not in out and "hook: опять" not in out)
check("человек + вставка хука читается без вставки", f"{B}:6 " in section("Напоминание о следующем шаге"))
check("служебные префиксы пропущены", f"{B}:7 " not in out and f"{B}:8 " not in out)
check("в B ровно одна реплика размечена", out.count(f"{B}:") == 1, out)
check("длинная реплика не печатается целиком", longtail not in out and "и ещё много текста" in out)
check("переносы строк не печатаются", "строка вывода 3" not in out)
ex = [l for l in out.splitlines() if l.startswith("- ") and "«" in l and ".jsonl:" in l]
check("примеры найдены", len(ex) >= 8, len(ex))
check("примеры: до 80 знаков цитаты", all(len(l.split("«", 1)[1].rstrip("»")) <= 80 for l in ex), ex)
check("путь начинается с ~, не с абсолютного дома", all(l.startswith("- ~/") for l in ex), ex[:2])

# --- 4. --examples ограничивает ---
r = run("--interventions", "--examples", "1")
sec = r.stdout.split("## 6.", 1)[1]
rem1 = sec[sec.index("### Напоминание"):sec.index("### Исправление")]
check("--examples 1: один пример в классе", rem1.count(".jsonl:") == 1, rem1)
check("--examples 1: счёт полный", "Напоминание о следующем шаге: 3 раз" in sec, sec)
r = run("--interventions", "--examples", "0")
check("--examples 0: примеров нет", ".jsonl:" not in r.stdout.split("## 6.", 1)[1])

# --- 4б. Уроки, которые повторяются (раздел 7, --brief) ---
import time as _t
today = _t.strftime("%Y-%m-%d")
lessons_md = (
    "# Уроки\n\n"
    "## Миграция базы без бэкапа положила стенд оплаты (2025-03-01)\nтекст\n\n"
    "## Миграция базы стенда оплаты снова без бэкапа (" + today + ")\nопять то же\n\n"
    "## Новый урок про кэш раннера (" + today + ")\nничего общего\n")
for slug in ("-demo-proj", "-demo-copy"):  # та же память под двумя slug — урок считается один раз
    (home / f".claude/projects/{slug}/memory").mkdir(parents=True, exist_ok=True)
    (home / f".claude/projects/{slug}/memory/lessons-learned.md").write_text(lessons_md, encoding="utf-8")
r = run("--days", "365", "--lessons")
sec7 = r.stdout.split("## 7.", 1)[-1] if "## 7." in r.stdout else ""
check("раздел 7 печатается с --lessons", bool(sec7), r.stdout[-300:])
check("раздел 7: свежий урок того же класса", "Миграция базы стенда оплаты снова" in sec7, sec7)
check("раздел 7: прежний урок рядом", "раньше: «Миграция базы без бэкапа" in sec7, sec7)
check("раздел 7: копия памяти не удвоила урок", sec7.count("Миграция базы стенда оплаты снова") == 1, sec7)
check("раздел 7: несвязанный урок не в парах", "кэш раннера" not in sec7.split("в тексте")[0], sec7)
r = run("--days", "3650")
check("без --lessons раздела 7 нет", "## 7." not in r.stdout)
r = run("--days", "365", "--brief")
check("--brief: короткий, без заголовков", bool(r.stdout.strip()) and "#" not in r.stdout and len(r.stdout.strip().splitlines()) <= 6, r.stdout)
check("--brief: урок того же класса", "урок снова того же класса" in r.stdout, r.stdout)
empty = tmp / "empty"
(empty / "p").mkdir(parents=True)
r = subprocess.run([sys.executable, str(SCRIPT), "--projects", str(empty), "--brief"], capture_output=True, text=True,
                   env=dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1"))
check("--brief без сигнала — пусто, код 0", r.stdout == "" and r.returncode == 0, repr(r.stdout))

# --- 5. Классификатор напрямую ---
spec = importlib.util.spec_from_file_location("repeat_scan", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True
spec.loader.exec_module(mod)
cases = [
    ("continue", False, "reminder"), ("дальше", False, "reminder"), ("что дальше?", False, "reminder"),
    ("don't forget the tests", False, "reminder"), ("не забудь про тесты", False, "reminder"),
    ("that's not what I asked", False, "correction"), ("you forgot the migration", False, "correction"),
    ("это неправильно", False, "correction"), ("нет, не так", True, "correction"),
    ("yes", True, "decision"), ("мержи", True, "decision"), ("вариант 2", True, "decision"),
    ("hold off on the deploy", False, "decision"), ("yes", False, "other"),
    ("here is the output: ok", False, "transfer"),
    ("```\nlog\n```\nline\nline", False, "transfer"),
    ("by the way, the staging host moved", False, "info"),
    ("сделай экспорт отчёта", False, "other"),
    ("продолжай без вопросов и не останавливайся, пока всё не заработает на проде везде и всюду", False, "other"),
]
for text, asked, want in cases:
    got = mod.classify(text, asked)
    check(f"classify({text[:30]!r}, asked={asked}) = {want}", got == want, got)

shutil.rmtree(tmp, ignore_errors=True)
print(f"repeat-scan: {ok} проверок прошло, {bad} провалилось")
sys.exit(1 if bad else 0)
