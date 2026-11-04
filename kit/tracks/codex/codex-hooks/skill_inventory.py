#!/usr/bin/env python3
"""Codex SessionStart: каталог скиллов Claude, которых Codex сам не видит.

Скиллы набора установщик связывает в ~/.codex/skills — их Codex находит сам. Не видит он проектные
<проект>/.claude/skills/ и личные ~/.claude/skills/ без ссылки в ~/.codex/skills. Хук печатает их
список: имя, путь и начало описания из frontmatter. Тела скиллов не грузятся — агент читает SKILL.md
выбранного скилла сам, когда он нужен задаче. Только чтение, без сети.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOME = Path.home()
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or HOME / ".codex").expanduser()
MAX_ENTRIES = 80


def payload() -> dict:
    try:
        value = json.load(sys.stdin)
    except (ValueError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def cwd_of(data: dict) -> Path:
    try:
        return Path(str(data.get("cwd") or ".")).resolve()
    except OSError:
        return Path.cwd()


def description(path: Path) -> str:
    """Описание из frontmatter: читается только шапка, многострочные формы YAML тоже."""
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            if stream.readline(4096).strip() != "---":
                return ""
            parts: list[str] = []
            collecting = False
            for _ in range(64):
                line = stream.readline(4096)
                if not line or line.strip() == "---":
                    break
                if line.startswith("description:"):
                    value = line.partition(":")[2].strip()
                    collecting = True
                    if value not in {"|", ">", "|-", ">-", "|+", ">+"}:
                        parts.append(value)
                elif collecting and line[:1].isspace():
                    parts.append(line.strip())
                elif collecting:
                    break
            text = " ".join(parts).strip().strip("\"'")
            return " ".join(text.split())[:140]
    except OSError:
        return ""


def roots(project: Path) -> list[Path]:
    """Папки .claude/skills от рабочей папки вверх до домашней включительно."""
    out: list[Path] = []
    for d in (project, *project.parents):
        if d != HOME and HOME not in d.parents:
            continue  # вне домашней папки проектных скиллов Claude не бывает
        out.append(d / ".claude" / "skills")
        if d == HOME:
            break
    if HOME / ".claude" / "skills" not in out:
        out.append(HOME / ".claude" / "skills")
    return out


def inventory(project: Path) -> list[str]:
    known = {p.name for p in (CODEX_HOME / "skills").glob("*") if (p / "SKILL.md").exists()}
    seen: set[Path] = set()
    entries: list[str] = []
    for skills in roots(project):
        if not skills.is_dir():
            continue
        for path in sorted(skills.glob("*/SKILL.md")):
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen or path.parent.name in known:
                continue
            seen.add(resolved)
            desc = description(path)
            entries.append(f"- `{path.parent.name}`: `{path}`" + (f" — {desc}" if desc else ""))
    return entries[:MAX_ENTRIES]


def main() -> int:
    entries = inventory(cwd_of(payload()))
    if not entries:
        return 0
    text = ("## Скиллы Claude, доступные и здесь\n\n" + "\n".join(entries)
            + "\n\nВыбирать по задаче, а не по совпадению слов. Выбранный SKILL.md прочитать целиком до применения; "
            "посторонние не грузить. Скилл не расширяет полномочий: границы задаёт пользователь и AGENTS.md.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
