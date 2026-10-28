#!/usr/bin/env python3
"""Кто ещё работает или работал над этим: сессии Claude и Codex за последние часы по ключевым словам.

    related-sessions.py DEV-4968 harbor 'layerinfo|size 0'   ключи — подстроки или regex, без учёта регистра
    related-sessions.py --hours 72 --self <id этой сессии> DEV-4968

Смотрит транскрипты ~/.claude/projects/*/*.jsonl (если ~/.claude синхронизируется с другой машиной, её сессии
тоже здесь) и ~/.codex/sessions. Считает совпадения только в словах человека и в действиях агента: системные вставки
(память, хуки) не в счёт, иначе в каждую сессию попадает всё из индекса памяти. Печатает по сессии: где,
когда последняя активность, заголовок, последнюю просьбу, сколько совпадений в действиях и как вернуть.
"""
import argparse, json, os, re, time

H = os.path.expanduser("~")
HOME_SLUG = H.replace("/", "-")  # каталоги своих сессий начинаются с $HOME через дефисы, другой машины — с её $HOME
p = argparse.ArgumentParser()
p.add_argument("keys", nargs="+")
p.add_argument("--hours", type=float, default=24)
p.add_argument("--self", dest="me", default="")
a = p.parse_args()
rx = re.compile("|".join(f"(?:{k})" for k in a.keys), re.I)
since = time.time() - a.hours * 3600


def files():
    for root in (f"{H}/.claude/projects", f"{H}/.codex/sessions"):
        for d, _, fs in os.walk(root):
            if "/subagents" in d or "/memory" in d:
                continue
            for f in fs:
                path = os.path.join(d, f)
                if f.endswith(".jsonl") and os.path.getmtime(path) >= since:
                    yield path


def texts(e):
    """(вид, текст) из записи: слова человека, текст и действия агента. Системное пропускаем."""
    if e.get("isMeta") or e.get("type") not in ("user", "assistant", "response_item", "event_msg"):
        return
    m = e.get("message") or e.get("payload") or {}
    c = m.get("content")
    role = e.get("type") if e.get("type") in ("user", "assistant") else m.get("role", "")
    items = [{"type": "text", "text": c}] if isinstance(c, str) else c if isinstance(c, list) else []
    if e.get("type") == "event_msg" and m.get("type") == "user_message":
        items, role = [{"type": "text", "text": m.get("message", "")}], "user"
    for it in items:
        if not isinstance(it, dict):
            continue
        t = it.get("type")
        if t in ("text", "input_text", "output_text"):
            s = it.get("text", "")
            if role == "user" and (s.startswith("<") or "system-reminder" in s[:200]):
                continue
            yield ("человек" if role == "user" else "агент"), s
        elif t in ("tool_use", "function_call"):
            yield "действие", json.dumps(it.get("input") or it.get("arguments") or "", ensure_ascii=False)


rows = []
for path in files():
    sid = os.path.basename(path)[:-6]
    if a.me and sid.startswith(a.me):
        continue
    hits = {"человек": 0, "агент": 0, "действие": 0}
    title = last_prompt = first = last = ""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"aiTitle"' in line or '"customTitle"' in line or '"lastPrompt"' in line:
                try:
                    e = json.loads(line)
                    title = e.get("customTitle") or e.get("aiTitle") or title
                    last_prompt = e.get("lastPrompt") or last_prompt
                except ValueError:
                    pass
                continue
            if not rx.search(line):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            for kind, s in texts(e):
                if rx.search(s):
                    hits[kind] += 1
                    ts = (e.get("timestamp") or "")[:16].replace("T", " ")
                    first = first or ts
                    last = ts or last
    if sum(hits.values()):
        where = "Codex" if "/.codex/" in path else "здесь" if f"/{HOME_SLUG}" in path else "другая машина"
        rows.append((os.path.getmtime(path), where, sid, title, last_prompt, hits, first, last, path))

if not rows:
    print(f"за {a.hours:g} ч никто: {' | '.join(a.keys)}")
for mt, where, sid, title, lp, h, first, last, path in sorted(rows, reverse=True):
    ago = int((time.time() - mt) / 60)
    print(f"{where:9} {sid[:8]}  активна {ago} мин назад  «{title or '—'}»")
    print(f"          совпадения: человек {h['человек']}, агент {h['агент']}, действия {h['действие']} (UTC {first} … {last})")
    if lp:
        print(f"          последняя просьба: {lp[:140]}")
    if where != "Codex":
        on = "" if where == "здесь" else "на той машине: "
        print(f"          {on}вернуть: claude --resume {sid}  ·  спросить: claude -p --resume {sid} \"…\"")
