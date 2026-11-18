#!/usr/bin/env python3
"""Редакция, слой без модели: что на сайте и в описании набора разошлось с самим набором.

Каждое число и обещание, которое можно посчитать из репозитория, считается здесь из первоисточника
(тесты, kit/settings.snippet.json, VERSION, INSTALL-VIA-AGENT.txt), а не берётся из прежнего текста.
    python3 scripts/editorial-check.py            # список для человека
    python3 scripts/editorial-check.py --json     # то же для run.py и агента редакции
    python3 scripts/editorial-check.py --root DIR # проверить другую копию (старый коммит, worktree)
Код выхода: 0 — расхождений нет, 1 — есть устаревшее (stale, drift, anchor), 2 — сбой самой проверки.

Виды находок:
    stale         на странице или в описании не то, что в наборе; правка нужна
    drift         набор разошёлся сам с собой (справка, CHANGELOG, VERSION); правка нужна
    anchor        проверка не нашла на странице место, которое должна сверять (фразу переписали); правка в проверке
    info          не ошибка, но редакции стоит знать (невыпущенные правки)
    unverifiable  утверждение из замера автора, из репозитория его не проверить; не трогать
"""
import html
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "--root" in sys.argv:
    ROOT = os.path.abspath(sys.argv[sys.argv.index("--root") + 1])
PAGE = "docs/index.html"

# Числа словами, как они стоят в тексте сайта («вместо четырёх»).
WORDS = {
    "один": 1, "два": 2, "двух": 2, "две": 2, "три": 3, "трёх": 3, "трех": 3, "четыре": 4, "четырёх": 4,
    "четырех": 4, "пять": 5, "пяти": 5, "шесть": 6, "шести": 6, "семь": 7, "семи": 7, "восемь": 8, "восьми": 8,
    "девять": 9, "девяти": 9, "десять": 10, "десяти": 10,
}
NUM = r"(\d+|" + "|".join(sorted(WORDS, key=len, reverse=True)) + r")"

# Обёртки, которые сами ничего не проверяют, а только запускают настоящий хук.
WRAPPERS = {"bypass-ask-to-deny.sh", "unless-bypass.sh"}
# Необязательные хуки: ставятся, только если у человека есть отдельная программа. На сайте их не считаем.
OPTIONAL = ("dippy",)

findings = []


def rd(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def num(tok):
    return int(tok) if tok.isdigit() else WORDS[tok.lower()]


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def add(kind, file, line, claim, actual, source, note=""):
    findings.append({"kind": kind, "file": file, "line": line, "claim": claim, "actual": actual,
                     "source": source, "note": note})


def plain(s):
    """Текст без тегов и с раскрытыми сущностями: подписи на сайте содержат подсказки <span class="tip">."""
    return html.unescape(re.sub(r"<[^>]+>", "", s))


# ── факты из набора ─────────────────────────────────────────────────────────

def guard_cases():
    """Таблица стража: опасные и обычные, а у опасных — сколько остаётся отказом в наборе.

    Набор ставит CLAUDE_GUARD_SKIP_SECRETS=1: команды, которые выводят ключи, страж пропускает, а их вывод закрывает маска
    (guard-secret-in-output). Поэтому «остановлено» считается прогоном стража с этой переменной, «через маску» — те
    опасные, что без неё отказ, а с ней проходят."""
    hook = os.path.join(ROOT, "kit/hooks/safety-guard.sh")
    rows = [l.rstrip("\n").split("\t", 1) for l in open(os.path.join(ROOT, "tests/safety-guard.cases.tsv"), encoding="utf-8") if l.strip()]
    base = {k: v for k, v in os.environ.items() if k != "CLAUDE_GUARD_SKIP_SECRETS"}

    def denied(cmd, skip):
        env = dict(base, CLAUDE_GUARD_SKIP_SECRETS="1") if skip else base
        out = subprocess.run(["bash", hook], input=json.dumps({"tool_input": {"command": cmd}}),
                             capture_output=True, text=True, env=env, timeout=30).stdout
        return bool(out.strip())

    deny = [c for e, c in rows if e == "deny"]
    allow = [c for e, c in rows if e == "allow"]
    stopped = sum(denied(c, True) for c in deny)
    masked = sum(1 for c in deny if not denied(c, True) and denied(c, False))
    return {"dangerous": len(deny), "ordinary": len(allow), "stopped": stopped, "masked": masked}


def hook_handlers():
    """Хуки, которые набор ставит сам: уникальные обработчики в settings.snippet.json.

    Правило подсчёта (оно же на сайте, «N хуков срабатывают сами»): один скрипт = один хук, даже если висит на двух
    событиях (session-autosave на SessionEnd и PreCompact); обёртки не считаются, считается то, что они запускают;
    хук-агент (type=agent) — один на свой matcher; необязательные (dippy) не считаются."""
    snip = json.loads(rd("kit/settings.snippet.json"))
    names = set()
    for event, groups in snip.get("hooks", {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                if h.get("type") == "agent" or "prompt" in h and "command" not in h:
                    names.add(f"agent:{event}:{g.get('matcher', '')}")
                    continue
                cmd = h.get("command", "")
                if any(o in cmd for o in OPTIONAL):
                    continue
                scripts = [os.path.basename(s) for s in re.findall(r"[\w./$-]+\.(?:sh|py)\b", cmd)]
                real = [s for s in scripts if s not in WRAPPERS]
                if real:
                    names.add(real[-1] if len(real) == 1 else real[0])
                elif cmd:
                    names.add(cmd)
    return sorted(names)


def kit_skills():
    d = os.path.join(ROOT, "kit/skills")
    return sorted(n for n in os.listdir(d) if os.path.isfile(os.path.join(d, n, "SKILL.md"))) if os.path.isdir(d) else []


def git(*args):
    r = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


# ── проверки ────────────────────────────────────────────────────────────────

def check_version_and_prompt(page):
    version = rd("VERSION").strip()
    m = re.search(r"<!--version-->(.*?)<!--/version-->", page, re.S)
    if m:
        if m.group(1).strip() != version:
            add("stale", PAGE, line_of(page, m.start()), f"версия {m.group(1).strip()}", version, "VERSION",
                "номер ставит build-page.py; поправить запуском скрипта, не руками")
    else:
        for fm in re.finditer(r"claude-tuning\s+v?(\d+\.\d+\.\d+)", page):
            if fm.group(1) != version:
                add("stale", PAGE, line_of(page, fm.start()), f"claude-tuning {fm.group(1)}", version, "VERSION",
                    "номер версии на странице написан руками; build-page.py с метками <!--version--> ставит его сам")

    prompt = rd("INSTALL-VIA-AGENT.txt")
    m = re.search(r"<!--prompt-->(.*?)<!--/prompt-->", page, re.S)
    block = m.group(1) if m else None
    pos = m.start() if m else None
    if block is None:
        # до build-page.py промпт стоял в первом <div class="cmd"> руками
        cm = re.search(r'<div class="cmd"[^>]*>(.*?)</div>', page, re.S)
        block, pos = (cm.group(1), cm.start()) if cm else (None, None)
    if block is None:
        add("stale", PAGE, 1, "промпта установки нет", "INSTALL-VIA-AGENT.txt", "INSTALL-VIA-AGENT.txt")
        return
    on_page = plain(block).strip()
    if on_page != prompt.strip():
        a, b = on_page.splitlines(), prompt.strip().splitlines()
        i = next((k for k in range(min(len(a), len(b))) if a[k].strip() != b[k].strip()), min(len(a), len(b)))
        add("stale", PAGE, line_of(page, pos), f"промпт: строка {i + 1}: {(a[i] if i < len(a) else '—')[:80]}",
            (b[i] if i < len(b) else "—")[:80], f"INSTALL-VIA-AGENT.txt:{i + 1}",
            "промпт на странице вставляет build-page.py; руками не править")


def anchor_missing(what, where, source):
    """Проверка не нашла на странице места, которое должна сверять. Молчать нельзя: фразу переписали, а число
    на сайте осталось без присмотра. Правка — в регулярном выражении проверки или в тексте сайта."""
    add("anchor", PAGE, where, f"не найдено: {what}", "проверку нужно перенастроить на новый текст", source)


def check_guard_numbers(page, g):
    # «25/25 … опасных команд», «22/22 опасных»
    blocks = 0
    for m in re.finditer(r'data-count="(\d+)"\s+data-suffix="/(\d+)"[^>]*>.*?</div>\s*<p>(.*?)</p>', page, re.S):
        text = plain(m.group(3))
        if "опасн" not in text:
            continue
        blocks += 1
        ln = line_of(page, m.start())
        verified = []
        got, total = int(m.group(1)), int(m.group(2))
        if total != g["dangerous"] or got != g["dangerous"]:
            add("stale", PAGE, ln, f"{got}/{total} опасных", f"{g['stopped'] + g['masked']}/{g['dangerous']}",
                "tests/safety-guard.cases.tsv (deny), прогон kit/hooks/safety-guard.sh")
        checks = [
            (r"(\d+)\s+обычн", g["ordinary"], "обычных", "tests/safety-guard.cases.tsv (allow)"),
            (NUM + r"\s+стирающ", g["stopped"], "остановлено стражем",
             "прогон kit/hooks/safety-guard.sh с CLAUDE_GUARD_SKIP_SECRETS=1, как ставит набор"),
            (r"вместо\s+" + NUM + r",?\s+которые\s+выводят\s+ключи", g["masked"], "через маску",
             "опасные, которые страж пропускает с CLAUDE_GUARD_SKIP_SECRETS=1"),
        ]
        for rx, want, what, src in checks:
            mm = re.search(rx, text, re.I)
            if mm:
                verified.append(mm.span(1))
                if num(mm.group(1)) != want:
                    add("stale", PAGE, ln, f"{mm.group(1)} ({what})", str(want), src)
            elif what == "обычных":
                anchor_missing("число обычных команд в блоке «опасных»", ln, src)
        # любое другое число в этом блоке осталось бы без проверки: фразу переписали, а правило о ней не знает
        for t in re.finditer(r"\b" + NUM + r"\b", text, re.I):
            if not any(a <= t.start(1) < b for a, b in verified):
                anchor_missing(f"проверка для числа «{t.group(1)}» в блоке «опасных»", ln, "scripts/editorial-check.py")
    if not blocks:
        anchor_missing("блок «N/N опасных команд» (data-count, data-suffix)", 1, "tests/safety-guard.cases.tsv")
    # старая форма «N/N опасных» текстом
    for m in re.finditer(r"(\d+)/(\d+)\s+опасн", plain(page)):
        if int(m.group(2)) != g["dangerous"]:
            idx = page.find(m.group(0)) if m.group(0) in page else 0
            add("stale", PAGE, line_of(page, idx), m.group(0), f"{g['dangerous']}/{g['dangerous']}",
                "tests/safety-guard.cases.tsv (deny)")


def check_hook_count(page, handlers):
    found = 0
    for m in re.finditer(r'data-count="(\d+)"[^>]*>[^<]*</div>\s*<p>(.*?)</p>', page, re.S):
        if not re.match(r"\s*хук", plain(m.group(2)), re.I):  # «16 хуков срабатывают…»: слово сразу после числа
            continue
        found += 1
        if int(m.group(1)) != len(handlers):
            add("stale", PAGE, line_of(page, m.start()), f"{m.group(1)} хуков", str(len(handlers)),
                "kit/settings.snippet.json (уникальные обработчики без dippy)", ", ".join(handlers))
    if not found:
        anchor_missing("блок «N хуков» (data-count, текст начинается со слова «хук»)", 1, "kit/settings.snippet.json")


def check_cheatsheet(page):
    lines = [l for l in rd("kit/claude-md.md").splitlines() if l.strip()]
    ranges = list(re.finditer(r"(\d+)[–-](\d+)\s+строк", page))
    if not ranges:
        anchor_missing("длина шпаргалки «N–M строк»", 1, "kit/claude-md.md")
    for m in ranges:
        lo, hi = int(m.group(1)), int(m.group(2))
        if not lo <= len(lines) <= hi:
            add("stale", PAGE, line_of(page, m.start()), m.group(0), f"{len(lines)} непустых строк",
                "kit/claude-md.md")


def check_unverifiable(page):
    for rx in (r"\d+\s+разговор\w*", r'data-count="(\d+)">[^<]*</div>\s*<p>раз\s+меньше'):
        for m in re.finditer(rx, page):
            claim = f"в {m.group(1)} раз меньше читает" if m.groups() else m.group(0)
            add("unverifiable", PAGE, line_of(page, m.start()), claim, "—", "замер автора",
                "из репозитория не проверить; без нового замера не трогать")


def check_hooks_exist_and_documented(handlers):
    readme = rd("kit/hooks/README.md") if os.path.exists(os.path.join(ROOT, "kit/hooks/README.md")) else ""
    snip = rd("kit/settings.snippet.json")
    for rel in re.findall(r"\$HOME/\.claude/hooks/([\w.-]+)", snip):
        if not os.path.exists(os.path.join(ROOT, "kit/hooks", rel)):
            add("drift", "kit/settings.snippet.json", line_of(snip, snip.find(rel)), rel, "файла нет",
                "kit/hooks/")
    if readme:
        # в справке должен быть каждый хук, который ставит набор; вспомогательные скрипты, которые зовёт другой хук
        # (guard-secret-file-read.sh из secret-guard-bash.py), описывать там не обязательно
        for name in handlers:
            if not name.startswith("agent:") and name.endswith((".sh", ".py")) and name not in readme:
                rows = [i + 1 for i, l in enumerate(readme.splitlines()) if l.startswith("| ")]
                add("drift", "kit/hooks/README.md", rows[-1] if rows else 1, f"хук {name} не описан в таблице хуков",
                    "ставится набором", "kit/settings.snippet.json")


def check_skills_listed():
    skill = rd("SKILL.md")
    for s in kit_skills():
        if s not in skill:
            add("drift", "SKILL.md", 1, f"скилл {s} не упомянут", f"kit/skills/{s}/SKILL.md", "kit/skills/")


def check_changelog():
    cl = rd("CHANGELOG.md")
    version = rd("VERSION").strip()
    heads = [(m.group(1), line_of(cl, m.start())) for m in re.finditer(r"^## (\d+\.\d+\.\d+)", cl, re.M)]
    if heads and heads[0][0] != version:
        add("drift", "CHANGELOG.md", heads[0][1], f"последняя версия {heads[0][0]}", version, "VERSION")
    m = re.search(r"^## Не выпущено\n(.*?)(?=^## )", cl, re.S | re.M)
    unreleased = m.group(1).strip() if m else ""
    tag = f"v{version}"
    if git("rev-parse", "-q", "--verify", f"refs/tags/{tag}") is None:
        return  # тегов в копии нет (клон без тегов) — сравнивать не с чем
    changed = git("diff", "--name-only", tag, "HEAD", "--", "kit", "SKILL.md", "scripts/install-kit.py",
                  "INSTALL-VIA-AGENT.txt") or ""
    log_cl = git("diff", "--name-only", tag, "HEAD", "--", "CHANGELOG.md") or ""
    if changed and not log_cl:
        files = changed.splitlines()
        add("drift", "CHANGELOG.md", line_of(cl, cl.find("## Не выпущено")) if "## Не выпущено" in cl else 1,
            "«Не выпущено» пусто", f"после {tag} изменено файлов набора: {len(files)}",
            f"git diff {tag}..HEAD", ", ".join(files[:8]))
    elif unreleased:
        n = git("rev-list", "--count", f"{tag}..HEAD") or "?"
        add("info", "CHANGELOG.md", line_of(cl, m.start()), "есть невыпущенные записи", f"коммитов после {tag}: {n}",
            f"git log {tag}..HEAD", "выпуск — решение владельца (scripts/release.sh)")


def main():
    try:
        page = rd(PAGE)
        g = guard_cases()
        handlers = hook_handlers()
        check_version_and_prompt(page)
        check_guard_numbers(page, g)
        check_hook_count(page, handlers)
        check_cheatsheet(page)
        check_hooks_exist_and_documented(handlers)
        check_skills_listed()
        check_changelog()
        check_unverifiable(page)
    except Exception as e:  # сбой проверки не должен выглядеть как «всё сходится»
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False) if "--json" in sys.argv
              else f"проверка упала: {type(e).__name__}: {e}")
        return 2
    facts = {"version": rd("VERSION").strip(), "guard": g, "hooks": len(handlers), "handlers": handlers,
             "skills": kit_skills(), "head": git("rev-parse", "--short", "HEAD")}
    stale = [f for f in findings if f["kind"] in ("stale", "drift", "anchor")]
    if "--json" in sys.argv:
        print(json.dumps({"facts": facts, "findings": findings}, ensure_ascii=False, indent=1))
    else:
        print(f"набор {facts['version']} ({facts['head']}): опасных {g['dangerous']} (остановлено {g['stopped']}, "
              f"через маску {g['masked']}), обычных {g['ordinary']}, хуков {len(handlers)}")
        if not findings:
            print("расхождений нет")
        for f in findings:
            print(f"[{f['kind']}] {f['file']}:{f['line']}  {f['claim']}  →  {f['actual']}   (источник: {f['source']})"
                  + (f"\n    {f['note']}" if f["note"] else ""))
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
