#!/usr/bin/env python3
"""Что человек просит снова и снова: сырьё для ревизии инструкций (скилл self-tune).

Читает транскрипты Claude Code за последние дни (~/.claude/projects/*/*.jsonl) и печатает отчёт:
  1. Повторённые просьбы — похожие сообщения человека из разных разговоров.
  2. Поправки — сообщения со словами «опять», «я же просил», «запомни», «всегда», «никогда»…
  3. Повторённые действия — одна и та же команда в разных разговорах (кандидат в скилл).
  4. Скиллы — сколько строк и сколько раз вызывались (кому пора делиться, кто лишний).
  5. Размер ~/.claude/CLAUDE.md.
  6. С флагом --interventions: вмешательства человека по классам (решение, оставленное намеренно /
     ручной перенос данных / напоминание о шаге / исправление ошибки агента / новые сведения):
     счёт и до N примеров на класс в виде «путь:строка» и первых ~80 знаков одной строки.

Только чтение. Похожее на ключи и токены в цитатах заменяется на ‹скрыто›. В разделе 6 сообщения,
похожие на секреты, пропускаются целиком, побочные (sidechain) записи, результаты инструментов и
служебный текст хуков не читаются, а сообщение целиком не печатается никогда. Разметка — по словам и
форме текста, без вызова модели: это подсказка для ревизии, а не вердикт.

    repeat-scan.py [--days 14] [--projects ~/.claude/projects] [--interventions] [--examples 3]
"""
import argparse, glob, json, os, re, sys, time
from collections import Counter, defaultdict

H = os.path.expanduser("~")
SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|\d{6,}:[A-Za-z0-9_-]{30,}|[A-Za-z0-9+/_-]{32,}={0,2})")
CORRECTION = re.compile(r"\b(опять|снова|я же (просил|говорил|писал)|сколько (раз|можно)|запомни|в следующий раз|всегда|никогда|перестань|не надо|хватит|again|i told you|stop doing|always|never|remember)\b", re.I)
# Секреты для раздела 6: кроме длинных токенов — «пароль: …», ключи PEM, JWT, учётка в URL, Bearer.
SECRET_MSG = re.compile(
    r"(?i)((пароль|парол[ья]|password|passwd|token|токен|secret|секрет|api[_ -]?key|ключ)\s*[:=]\s*\S{4,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY|eyJ[A-Za-z0-9_-]{10,}\.|AKIA[0-9A-Z]{16}|bearer\s+\S{16,}|://[^/\s:@]+:[^@\s]+@)")
# Служебный текст, который harness подмешивает в сообщение человека: вырезается до разметки.
INJECTED = re.compile(r"<(system-reminder|command-[a-z-]+|local-command-[a-z-]+|task-notification|ide_[a-z_]+|user-prompt-submit-hook)>.*?</\1>", re.S)
SKIP_PREFIX = ("<", "This session is being continued", "Base directory for this skill", "Caveat:", "[Request interrupted")
SHELL_WORDS = {"for", "while", "until", "if", "do", "then", "set", "export", "source", "time", "timeout", "sudo"}
TRIVIAL = {"ls", "cat", "grep", "rg", "cd", "echo", "sed", "head", "tail", "find", "wc", "pwd", "which", "true", "test", "sleep", "mkdir", "cp", "mv", "printf", "jq", "awk", "sort", "stat", "du", "file"}
TRIVIAL_PAIRS = {"git status", "git diff", "git log", "git show", "git add", "git branch", "git fetch", "python3 -c", "python3 -", "node -e", "bash -c"}


def mask(t):
    return SECRET.sub("‹скрыто›", t)


def clean(t, n=170):
    t = mask(re.sub(r"\s+", " ", t)).strip()
    return t if len(t) <= n else t[:n - 1] + "…"


def stems(t):
    words = re.findall(r"[a-zA-Zа-яА-ЯёЁ]{4,}", t.lower())
    return {w[:5] for w in words}


def user_texts(msg):
    c = msg.get("content")
    if isinstance(c, str):
        yield c
    elif isinstance(c, list):
        for x in c:
            if isinstance(x, dict) and x.get("type") == "text":
                yield x.get("text", "")


def shape(cmd):
    first = cmd.strip().split("\n", 1)[0]
    first = re.sub(r"^(cd [^&;]+(&&|;)\s*)+", "", first)
    first = re.sub(r"^(\w+=\S+\s+)+", "", first)
    parts = first.split()
    while parts and parts[0] in ("time", "sudo", "timeout") or (parts and re.fullmatch(r"\d+[smh]?", parts[0])):
        parts = parts[1:]
    if not parts or parts[0] in SHELL_WORDS or parts[0].startswith("$"):
        return None
    toks = []
    for tok in parts:
        if tok in ("|", "&&", ";", "||", ">", ">>", "2>&1"):
            break
        if re.search(r"\.(py|sh|js|mjs|ts|rb)$", tok):
            toks.append(os.path.basename(tok))
            if len(toks) == 3:
                break
            continue
        if "/" in tok or "=" in tok or tok[:1] in "\"'$<(" or re.fullmatch(r"[-\d.]+", tok):
            continue
        toks.append(tok)
        if len(toks) == 3:
            break
    if toks and toks[0] in ("python3", "python", "node", "bash", "sh") and (len(toks) == 1 or not re.search(r"\.(py|sh|js|mjs|ts)$", toks[1])):
        return None
    if not toks or toks[0] in TRIVIAL or " ".join(toks[:2]) in TRIVIAL_PAIRS:
        return None
    return " ".join(toks)


# --- Вмешательства человека: классы и признаки (раздел 6) ---
# Порядок проверки в classify() важен: поправка → решение → перенос → напоминание → новые сведения.
CLASSES = [
    ("decision", "Решение, оставленное намеренно", "агент спросил или человек придержал шаг; автоматизировать не надо"),
    ("transfer", "Ручной перенос данных между шагами", "кандидат: шаг, который агент может сделать сам (скрипт, хук, пункт чек-листа)"),
    ("reminder", "Напоминание о следующем шаге", "кандидат: правило «довести до конца» или пункт чек-листа задачи"),
    ("correction", "Исправление ошибки агента", "кандидат: правило там, где ошибка случилась"),
    ("info", "Новые сведения", "факт о человеке или проекте: место ему в памяти, а не в правилах"),
]
IV_CORRECTION = re.compile(
    r"(не так\b|не то\b|неправильн|неверн|ошибся|ошибочн|сломал|не работает|не сработал|не заработал"
    r"|ты (забыл|пропустил|не (сделал|проверил|учёл|учел|понял|туда)|опять|снова|зря)|я же (просил|говорил|писал|сказал)"
    r"|не это я|не этого я|верни как было|откати\b|почему ты|зачем ты|должно быть не|а должно быть"
    r"|\bwrong\b|that'?s not|not what i (asked|wanted|meant)|you (forgot|missed|broke|didn'?t|should have|shouldn'?t)"
    r"|doesn'?t work|isn'?t working|still (broken|fails|failing)|\bbroke\b|undo that|revert that)", re.I)
IV_ANSWER = re.compile(
    r"^\s*(да|нет|ок|окей|ok|okay|yes|yep|no|nope|давай|делай|делаем|согласен|согласна|одобряю|разрешаю|можно|"
    r"не надо|не нужно|go ahead|go|approved?|merge|мержи|выкатывай|катай|ship it|вариант|первый|второй|третий|[1-9]|[abабв])\b", re.I)
IV_HOLD = re.compile(
    r"(я (решил|выбираю|сам)|выбираю\b|оставь (как есть|так)|не трогай|пока не надо|не мержи|не выкатывай|подожди меня|"
    r"только после моего|это моё решение|i decided|let'?s go with|leave it( as is)?|hold off|don'?t (merge|deploy|push|ship))", re.I)
IV_TRANSFER_LEAD = re.compile(
    r"^\s*(вот|держи|смотри|лови|here(?:'s| is| are)?|output|result|log|вывод|результат|лог)\b.{0,60}?"
    r"(результат|вывод|лог|ответ|ошибк|скрин|список|данные|json|таблиц|diff|output|log|result|response|error|trace)", re.I)
IV_LOGLIKE = re.compile(
    r"^(\$ |>>> |Traceback|\s+at \S+\(|\s+File \"|\d{4}-\d\d-\d\d[ T]\d\d:\d\d|\[?(ERROR|WARN|INFO|DEBUG)\]?\b|\S+@\S+:.*[$#] |[a-z0-9_.-]+\s*[:|]\s*\S.*\|)", re.M)
IV_REMIND_SHORT = re.compile(
    r"^\s*(и\s+|ну\s+)?(продолжай|продолжи|продолжаем|продолжайте|дальше|давай дальше|что дальше|и что дальше|ну что|ну и как|"
    r"как (успехи|дела|там|прогресс)|что там|ещё|еще|далее|готово|ты (тут|закончил|всё)|статус|"
    r"next|continue|go on|keep going|proceed|carry on|and then|what'?s next|status|done|any (news|updates?))[\s.,!?…]*$", re.I)
IV_REMIND_LONG = re.compile(
    r"^\s*(а|и|ну)?\s*(где|как насчёт|как насчет|что с|а что с|не забудь|ты не забыл|ты (уже )?(сделал|проверил|запустил|закоммитил|выкатил|смержил)|"
    r"where(?:'s| is)|what about|did you|don'?t forget)\b", re.I)
IV_INFO = re.compile(
    r"(https?://|\bучти\b|\bкстати\b|\bимей в виду\b|\bоказывается\b|\bу нас (теперь|есть|уже|нет)|\bпоменялось\b|\bизменилось\b|"
    r"\bпотому что\b|\bfyi\b|\bnote that\b|\bfor context\b|\bheads up\b|\bby the way\b)", re.I)


def strip_injected(t):
    return INJECTED.sub("", t).strip()


def looks_secret(t):
    return bool(SECRET.search(t) or SECRET_MSG.search(t))


def classify(t, asked=False):
    """Класс вмешательства по тексту реплики человека; asked — агент перед ней задал вопрос. Прочее — 'other'."""
    lines = [l for l in t.splitlines() if l.strip()]
    head = t[:300]
    if len(lines) <= 8 and IV_CORRECTION.search(head):
        return "correction"
    if IV_HOLD.search(head) or (asked and len(t) <= 60 and IV_ANSWER.match(t)):
        return "decision"
    loglike = len(IV_LOGLIKE.findall(t))
    if (IV_TRANSFER_LEAD.match(t[:120]) or (len(lines) >= 4 and ("```" in t or loglike >= 2))
            or (len(lines) >= 3 and loglike >= 1 and re.search(r"\b(теперь|дальше|now|next)\b", t, re.I))):
        return "transfer"
    if (len(lines) <= 2 and len(t) <= 60 and IV_REMIND_SHORT.match(t)) or (len(lines) <= 2 and len(t) <= 140 and IV_REMIND_LONG.match(t)):
        return "reminder"
    if IV_INFO.search(head):
        return "info"
    return "other"


def first_line(t):
    return next((l.strip() for l in t.splitlines() if l.strip()), "")


def scan(root, days, want_iv=False):
    since = time.time() - days * 86400
    files = [f for f in glob.glob(f"{root}/*/*.jsonl") if os.path.getmtime(f) >= since]
    asks, fixes = [], []
    shapes = defaultdict(lambda: [0, set()])
    skills = Counter()
    seen = set()
    ivs, secret_skipped = [], 0
    for f in files:
        proj = os.path.basename(os.path.dirname(f)).replace("-Users-", "~/").replace("-", "/")[-40:]
        sid = os.path.basename(f)[:8]
        asked = False  # последняя реплика агента в этом разговоре — вопрос человеку
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for ln, line in enumerate(fh, 1):
                if '"type":"user"' not in line and '"tool_use"' not in line and not (want_iv and '"type":"assistant"' in line):
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                ts = r.get("timestamp", "")[:10]
                if (r.get("type") == "user" and not r.get("isSidechain") and not r.get("isMeta")
                        and not r.get("toolUseResult") and not r.get("isCompactSummary")):
                    for t in user_texts(r.get("message", {})):
                        t = strip_injected(t.strip())  # вставки harness и хуков — не слова человека
                        if not t or t.startswith(SKIP_PREFIX):
                            continue
                        key = r.get("uuid") or (t, r.get("timestamp"))
                        if key in seen or (t, ts) in seen:
                            continue
                        seen.add(key)
                        seen.add((t, ts))
                        if want_iv:
                            if looks_secret(t):
                                secret_skipped += 1
                            else:
                                ivs.append({"c": classify(t, asked), "f": f, "n": ln, "s": sid, "d": ts, "t": clean(first_line(t), 80)})
                        if len(t) > 1500:
                            continue
                        item = {"t": t, "s": sid, "p": proj, "d": ts, "k": stems(t)}
                        asks.append(item)
                        if CORRECTION.search(t):
                            fixes.append(item)
                elif r.get("type") == "assistant":
                    blocks = r.get("message", {}).get("content", []) or []
                    if want_iv and not r.get("isSidechain") and isinstance(blocks, list):
                        if any(isinstance(x, dict) and x.get("type") == "tool_use" for x in blocks):
                            asked = False
                        else:
                            txt = " ".join(x.get("text", "") for x in blocks if isinstance(x, dict) and x.get("type") == "text").strip()
                            if txt:
                                asked = "?" in txt[-300:]
                    for x in blocks if isinstance(blocks, list) else []:
                        if not isinstance(x, dict) or x.get("type") != "tool_use":
                            continue
                        if x.get("name") == "Bash":
                            sh = shape(x.get("input", {}).get("command", ""))
                            if sh:
                                shapes[sh][0] += 1
                                shapes[sh][1].add(sid)
                        elif x.get("name") == "Skill":
                            skills[x.get("input", {}).get("skill", "?")] += 1
    return files, asks, fixes, shapes, skills, ivs, secret_skipped


def clusters(items, min_size=3, min_sessions=2, jac=.5):
    groups = []
    for it in items:
        if len(it["k"]) < 3 or len(it["t"]) > 500:
            continue
        for g in groups:
            k = g["k"]
            if len(k & it["k"]) / len(k | it["k"]) >= jac:
                g["items"].append(it)
                break
        else:
            groups.append({"k": it["k"], "items": [it]})
    out = [g for g in groups if len(g["items"]) >= min_size and len({i["s"] for i in g["items"]}) >= min_sessions]
    return sorted(out, key=lambda g: -len(g["items"]))


def skill_table(used):
    rows = []
    for d in sorted(set(glob.glob(f"{H}/.claude/skills/*/SKILL.md") + glob.glob(f"{H}/.agents/skills/*/SKILL.md"))):
        name = os.path.basename(os.path.dirname(d))
        try:
            n = sum(1 for _ in open(os.path.realpath(d), encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        rows.append((name, n, used.get(name, 0)))
    seen, uniq = set(), []
    for r in rows:
        if r[0] not in seen:
            seen.add(r[0])
            uniq.append(r)
    return uniq


def short_path(f):
    return f.replace(H, "~", 1) if f.startswith(H) else f


def pick_examples(items, n):
    """До n примеров: сначала по одному из разных разговоров, потом остальные; свежие вперёд."""
    out, seen_s = [], set()
    ordered = sorted(items, key=lambda i: (i["d"], i["f"], i["n"]), reverse=True)
    for i in ordered:
        if len(out) < n and i["s"] not in seen_s:
            out.append(i)
            seen_s.add(i["s"])
    for i in ordered:
        if len(out) < n and i not in out:
            out.append(i)
    return out


def print_interventions(ivs, secret_skipped, n):
    print("\n## 6. Вмешательства человека (разметка по словам, без модели — проверять по цитате)\n")
    by = defaultdict(list)
    for i in ivs:
        by[i["c"]].append(i)
    print(f"Реплик размечено: {len(ivs)}; пропущено как похожие на секреты: {secret_skipped}; без класса: {len(by['other'])}.\n")
    for key, title, hint in CLASSES:
        items = by.get(key, [])
        print(f"### {title}: {len(items)} раз, разговоров {len({i['s'] for i in items})}")
        if not items:
            print("Нет.\n")
            continue
        print(f"{hint}.")
        for i in pick_examples(items, n):
            print(f"- {short_path(i['f'])}:{i['n']} ({i['d']}) «{i['t']}»")
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--projects", default=f"{H}/.claude/projects")
    ap.add_argument("--interventions", action="store_true", help="добавить раздел 6: вмешательства человека по классам")
    ap.add_argument("--examples", type=int, default=3, help="примеров на класс в разделе 6 (по умолчанию 3)")
    a = ap.parse_args()
    files, asks, fixes, shapes, used, ivs, secret_skipped = scan(a.projects, a.days, a.interventions)
    print(f"# Ревизия: что повторяется за {a.days} дн.\n\nРазговоров: {len(files)}, сообщений человека: {len(asks)}.\n")

    print("## 1. Повторённые просьбы (кандидаты в правила)\n")
    cs = clusters(asks)[:12]
    if not cs:
        print("Нет: похожих просьб из разных разговоров меньше трёх.\n")
    for g in cs:
        its = g["items"]
        print(f"- **{len(its)} раз, разговоров {len({i['s'] for i in its})}**, проекты: {', '.join(sorted({i['p'] for i in its}))[:120]}")
        for i in its[:3]:
            print(f"  - {i['d']}: «{clean(i['t'])}»")
    print("\n## 2. Поправки («опять», «я же просил», «запомни»…)\n")
    fc = clusters(fixes, min_size=2, min_sessions=1, jac=.35)[:10]
    shown = 0
    for g in fc:
        its = g["items"]
        print(f"- **{len(its)} раз**: " + " / ".join(f"{i['d']} «{clean(i['t'], 120)}»" for i in its[:3]))
        shown += 1
    for i in [x for x in fixes if not any(x in g["items"] for g in fc)][-6:]:
        print(f"- 1 раз: {i['d']} «{clean(i['t'], 140)}»")
        shown += 1
    if not shown:
        print("Нет.")
    print("\n## 3. Повторённые действия (кандидаты в скилл)\n")
    rep = sorted(((k, v[0], len(v[1])) for k, v in shapes.items() if len(v[1]) >= 3 and v[0] >= 5), key=lambda x: (-x[2], -x[1]))[:15]
    if not rep:
        print("Нет: ни одна команда не встречалась в трёх разговорах и больше.")
    for k, n, s in rep:
        print(f"- `{k}` — {n} раз в {s} разговорах")
    print("\n## 4. Скиллы: размер и вызовы\n")
    rows = skill_table(used)
    big = [r for r in rows if r[1] > 300]
    idle = [r for r in rows if r[2] == 0]
    for name, n, u in sorted(rows, key=lambda r: -r[2])[:15]:
        print(f"- {name}: {n} строк, вызван {u} раз")
    if big:
        print("\nДлиннее 300 строк (кандидаты на разделение, детали — в references/): " + ", ".join(f"{r[0]} ({r[1]})" for r in big))
    if idle:
        print(f"\nНе вызывались за {a.days} дн.: " + ", ".join(r[0] for r in idle[:30]))
    cm = f"{H}/.claude/CLAUDE.md"
    if os.path.exists(cm):
        n = sum(1 for _ in open(cm, encoding="utf-8", errors="ignore"))
        note = " — длиннее 80 строк: правила одной задачи переносить в её скилл" if n > 80 else ""
        print(f"\n## 5. ~/.claude/CLAUDE.md: {n} строк{note}")
    if a.interventions:
        print_interventions(ivs, secret_skipped, max(a.examples, 0))


if __name__ == "__main__":
    sys.exit(main())
