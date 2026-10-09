#!/usr/bin/env python3
"""Что человек просит снова и снова: сырьё для ревизии инструкций (скилл self-tune).

Читает транскрипты Claude Code за последние дни (~/.claude/projects/*/*.jsonl) и печатает отчёт:
  1. Повторённые просьбы — похожие сообщения человека из разных разговоров.
  2. Поправки — сообщения со словами «опять», «я же просил», «запомни», «всегда», «никогда»…
  3. Повторённые действия — одна и та же команда в разных разговорах (кандидат в скилл).
  4. Скиллы — сколько строк и сколько раз вызывались (кому пора делиться, кто лишний).
  5. Размер ~/.claude/CLAUDE.md.

Только чтение. Похожее на ключи и токены в цитатах заменяется на ‹скрыто›.

    repeat-scan.py [--days 14] [--projects ~/.claude/projects]
"""
import argparse, glob, json, os, re, sys, time
from collections import Counter, defaultdict

H = os.path.expanduser("~")
SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|\d{6,}:[A-Za-z0-9_-]{30,}|[A-Za-z0-9+/_-]{32,}={0,2})")
CORRECTION = re.compile(r"\b(опять|снова|я же (просил|говорил|писал)|сколько (раз|можно)|запомни|в следующий раз|всегда|никогда|перестань|не надо|хватит|again|i told you|stop doing|always|never|remember)\b", re.I)
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


def scan(root, days):
    since = time.time() - days * 86400
    files = [f for f in glob.glob(f"{root}/*/*.jsonl") if os.path.getmtime(f) >= since]
    asks, fixes = [], []
    shapes = defaultdict(lambda: [0, set()])
    skills = Counter()
    seen = set()
    for f in files:
        proj = os.path.basename(os.path.dirname(f)).replace("-Users-", "~/").replace("-", "/")[-40:]
        sid = os.path.basename(f)[:8]
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"type":"user"' not in line and '"tool_use"' not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                ts = r.get("timestamp", "")[:10]
                if r.get("type") == "user" and not r.get("isSidechain") and not r.get("isMeta"):
                    for t in user_texts(r.get("message", {})):
                        t = t.strip()
                        if not t or t.startswith(SKIP_PREFIX) or len(t) > 1500:
                            continue
                        key = r.get("uuid") or (t, r.get("timestamp"))
                        if key in seen or (t, ts) in seen:
                            continue
                        seen.add(key)
                        seen.add((t, ts))
                        item = {"t": t, "s": sid, "p": proj, "d": ts, "k": stems(t)}
                        asks.append(item)
                        if CORRECTION.search(t):
                            fixes.append(item)
                elif r.get("type") == "assistant":
                    for x in r.get("message", {}).get("content", []) or []:
                        if not isinstance(x, dict) or x.get("type") != "tool_use":
                            continue
                        if x.get("name") == "Bash":
                            sh = shape(x.get("input", {}).get("command", ""))
                            if sh:
                                shapes[sh][0] += 1
                                shapes[sh][1].add(sid)
                        elif x.get("name") == "Skill":
                            skills[x.get("input", {}).get("skill", "?")] += 1
    return files, asks, fixes, shapes, skills


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--projects", default=f"{H}/.claude/projects")
    a = ap.parse_args()
    files, asks, fixes, shapes, used = scan(a.projects, a.days)
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


if __name__ == "__main__":
    sys.exit(main())
