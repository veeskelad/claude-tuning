#!/usr/bin/env python3
"""Советы подстройки: что пора улучшить под человека и что давно не делалось. Без модели.

Пишет ~/.local/state/claude-tuning/suggestions.json — его показывает раздел «Подстройка» в панели session-hub.
Человек отвечает кнопками: «сделать», «обсудить», «позже», «не предлагать». Ответ — строка в
~/.claude/tuning-feedback.jsonl; по ней этот скрипт решает, что и когда предлагать снова, а self-tune —
что человеку не нужно и что он хочет иначе.

    tuning-suggest.py            # собрать и записать
    tuning-suggest.py --print    # только напечатать, ничего не писать
"""
import argparse, hashlib, importlib.util, json, os, sys, time

H = os.path.expanduser("~")
OUT = f"{H}/.local/state/claude-tuning/suggestions.json"
FEEDBACK = f"{H}/.claude/tuning-feedback.jsonl"
TUNING_LOG = f"{H}/.claude/tuning-log.md"
SCAN = f"{H}/.claude/bin/repeat-scan.py"
DAY = 86400
LATER_DAYS = 7
MAX_ITEMS = 4
MEMORY_LINES = 200


def load_scan():
    if not os.path.exists(SCAN):
        return None
    spec = importlib.util.spec_from_file_location("repeat_scan", SCAN)
    mod = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    spec.loader.exec_module(mod)
    return mod


def feedback():
    """Последний ответ человека по каждому совету: {id: (answer, at)}."""
    last = {}
    try:
        for line in open(FEEDBACK, encoding="utf-8"):
            try:
                r = json.loads(line)
                last[r["id"]] = (r["answer"], float(r["at"]))
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    return last


def hidden(fb, sid, period_days, now):
    """Скрыт ли совет: «не предлагать» — навсегда; «позже» и «обсудить» — на неделю; «сделать» — на свой срок."""
    answer, at = fb.get(sid, (None, 0))
    if answer == "never":
        return True
    if answer in ("later", "discuss"):
        return now - at < LATER_DAYS * DAY
    if answer == "done":
        return now - at < (period_days or 36500) * DAY
    return False


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def days(sec):
    return int(sec // DAY)


def suggest(now):
    fb = feedback()
    rs = load_scan()
    items = []

    # 1. ревизия self-tune — раз в неделю; сигнал — повторы за неделю
    last = max(mtime(TUNING_LOG), fb.get("self-tune", (None, 0))[1] if fb.get("self-tune", ("",))[0] == "done" else 0)
    age = now - last if last else None
    if not hidden(fb, "self-tune", 7, now) and (age is None or age >= 7 * DAY):
        why = []
        if rs:
            files, asks, fixes, _, _, ivs, _ = rs.scan(f"{H}/.claude/projects", 7, True)
            for g in rs.clusters(asks)[:2]:
                its = g["items"]
                why.append(f"просьба ×{len(its)}: «{rs.clean(its[0]['t'], 70)}»")
            rem = [i for i in ivs if i["c"] == "reminder"]
            if len({i["s"] for i in rem}) >= 3:
                why.append(f"напоминаний «продолжай» — {len(rem)}")
        if why or age is None or age >= 14 * DAY:
            when = "не было ни разу" if age is None else f"последняя {days(age)} дн. назад"
            items.append({"id": "self-tune", "title": f"Ревизия под тебя: {when}",
                          "why": "; ".join(why) or "давно не смотрел, что ты повторяешь",
                          "prompt": "Проведи ревизию по скиллу self-tune: что я повторяю и что стоит записать в правила."})

    # 2. уроки, записанные снова: запись не сработала
    if rs:
        _, pairs, _ = rs.lessons(f"{H}/.claude/projects", 30)
        for h, like in pairs[:2]:
            sid = "lesson:" + hashlib.sha1(h["t"].encode()).hexdigest()[:10]
            if hidden(fb, sid, None, now):
                continue
            items.append({"id": sid, "title": "Урок записан второй раз",
                          "why": f"«{rs.clean(h['t'], 80)}», раньше: «{rs.clean(like[0]['t'], 60)}»",
                          "prompt": f"Урок «{h['t']}» ({rs.short_path(h['f'])}:{h['n']}) записан снова, раньше был «{like[0]['t']}». "
                                    "По скиллу self-tune преврати его в правило там, где ошибка случилась, или предложи стража."})

    # 3. память: индекс у потолка или аудита не было месяц
    projects = f"{H}/.claude/projects"
    big, has_memory = [], False
    for d in sorted(os.listdir(projects)) if os.path.isdir(projects) else []:
        m = f"{projects}/{d}/memory/MEMORY.md"
        try:
            n = sum(1 for _ in open(m, encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        has_memory = True
        if n >= MEMORY_LINES * 0.9:
            big.append((n, d))
    # аудит памяти отмечается кнопкой «сделать»: до первого ответа — предложить, если память вообще есть
    never = "memory" not in fb
    if not hidden(fb, "memory", 30, now) and (big or (never and has_memory) or fb.get("memory", ("",))[0] == "done"):
        why = "; ".join(f"MEMORY.md {p}: {n} строк из {MEMORY_LINES}" for n, p in sorted(big, reverse=True)[:2])
        items.append({"id": "memory", "title": "Почистить память", "why": why or "аудита памяти не было месяц",
                      "prompt": "Проведи аудит памяти по скиллу memory-consolidate и покажи, что предлагаешь сжать или убрать."})

    # 4. скиллы, которые месяц не вызывались: лишние загружают описание в каждый разговор
    if rs and not hidden(fb, "skills-idle", 30, now):
        files, _, _, _, used, _, _ = rs.scan(f"{H}/.claude/projects", 30, False)
        idle = [r[0] for r in rs.skill_table(used) if r[2] == 0]
        if len(idle) >= 10:
            items.append({"id": "skills-idle", "title": f"{len(idle)} скиллов не вызывались месяц",
                          "why": ", ".join(idle[:5]) + ("…" if len(idle) > 5 else ""),
                          "prompt": "Посмотри, какие скиллы я не вызывал месяц (repeat-scan.py --days 30, раздел 4), "
                                    "и предложи, какие отключить или объединить. Ничего не удаляй без моего «да»."})
    return items[:MAX_ITEMS]


def answer(sid, ans, now):
    """Ответ человека: строка в журнал и совет убран из файла панели сразу, без нового сбора."""
    try:
        data = json.load(open(OUT, encoding="utf-8"))
    except (OSError, ValueError):
        data = {"at": int(now * 1000), "items": []}
    item = next((i for i in data.get("items", []) if i.get("id") == sid), {})
    os.makedirs(os.path.dirname(FEEDBACK), exist_ok=True)
    with open(FEEDBACK, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": int(now), "date": time.strftime("%Y-%m-%d %H:%M"), "id": sid, "answer": ans,
                            "title": item.get("title", "")}, ensure_ascii=False) + "\n")
    data["items"] = [i for i in data.get("items", []) if i.get("id") != sid]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(OUT + ".tmp", OUT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true")
    ap.add_argument("--answer", nargs=2, metavar=("ID", "ОТВЕТ"), help="ответ человека: done, discuss, later, never")
    a = ap.parse_args()
    now = time.time()
    if a.answer:
        sid, ans = a.answer
        if ans not in ("done", "discuss", "later", "never"):
            sys.exit(f"--answer: {ans} — ждал done, discuss, later или never")
        answer(sid, ans, now)
        return 0
    data = {"at": int(now * 1000), "items": suggest(now)}
    if a.print:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
