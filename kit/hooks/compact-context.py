#!/usr/bin/env python3
"""SessionStart(compact): вернуть агенту после сжатия контекста его задачу и файлы, которые он правил.

Зачем. После сжатия open-tasks.sh показывает все открытые чек-листы проекта (их бывает 15), но не
какой из них ведёт эта сессия. Урок Claude Code in Action «Hooks» (трек Academy, 28.09.2026): вернуть
контекст после сжатия может только SessionStart с matcher `compact` — его вывод попадает в разговор,
вывод PostCompact — нет.

Что делает: читает транскрипт сессии (transcript_path из входа хука), собирает файлы, которые сессия
меняла инструментами Edit/Write/MultiEdit/NotebookEdit, и чек-листы `.tasks/*.md` / `TASKS*.md` среди
них. Печатает additionalContext: цель, строку «Готово = …» и открытые пункты каждого чек-листа плюс
последние правленые файлы. Только чтение. Любой сбой — тихий выход 0.

Проверка: python3 ~/.claude/hooks/compact-context.py --selftest <transcript.jsonl>
"""
import json
import os
import re
import sys

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
TASKS_RE = re.compile(r"(/\.tasks/[^/]+\.md|/TASKS[^/]*\.md)$")
MAX_FILES, MAX_TASKS, MAX_ITEMS = 12, 2, 10


def edited_files(transcript):
    """Файлы в порядке последней правки (последний — самый свежий)."""
    order = {}
    n = 0
    with open(transcript, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"tool_use"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            content = (entry.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in EDIT_TOOLS:
                    inp = block.get("input") or {}
                    path = inp.get("file_path") or inp.get("notebook_path")
                    if path:
                        n += 1
                        order[path] = n
    return [p for p, _ in sorted(order.items(), key=lambda kv: kv[1])]


def checklist(path):
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return None
    goal = next((l for l in lines if l.startswith("# ")), "")
    done = next((l for l in lines if l.startswith("Готово")), "")
    items = [l.strip() for l in lines if l.lstrip().startswith("- [ ]")]
    if not items:
        return None
    out = [f"Чек-лист {path}", goal, done] + items[:MAX_ITEMS]
    if len(items) > MAX_ITEMS:
        out.append(f"… ещё {len(items) - MAX_ITEMS}")
    return "\n".join(l for l in out if l)


def build(transcript):
    files = edited_files(transcript)
    if not files:
        return ""
    tasks = [p for p in reversed(files) if TASKS_RE.search(p)][:MAX_TASKS]
    parts = ["Контекст сжат. Эта сессия вела:"]
    for p in tasks:
        block = checklist(p)
        if block:
            parts.append(block)
    if len(parts) == 1:
        parts.append("чек-листа среди правленых файлов нет.")
    recent = [p for p in reversed(files) if p not in tasks][:MAX_FILES]
    if recent:
        parts.append("Последние правленые файлы (свежие сверху):\n" + "\n".join(f"- {p}" for p in recent))
    parts.append("Продолжай с первого открытого пункта своего чек-листа; перед правкой файл перечитай.")
    return "\n\n".join(parts)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--selftest":
        print(build(sys.argv[2]))
        return
    data = json.load(sys.stdin)
    transcript = data.get("transcript_path") or ""
    if not os.path.isfile(transcript):
        return
    text = build(transcript)
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}},
                         ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
