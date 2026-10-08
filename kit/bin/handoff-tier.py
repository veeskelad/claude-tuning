#!/usr/bin/env python3
"""Тиринг handoff: свежее — в global-handoff.md, остывшее — в workspace по дням.

`/morning` читает `global-handoff.md` целиком, поэтому в нём остаётся только свежее.
Всё остальное уезжает в `~/.claude/handoff-workspace/ГГГГ-ММ/ГГГГ-ММ-ДД.md` — один файл
на день. `handoff-workspace/INDEX.md` перестраивается каждый прогон: дата → проекты →
что сделано (заголовки записей того дня), со ссылками на дневные файлы.

Тиры (дата берётся из маркера «Сессия ГГГГ-ММ-ДД», записи идут новыми сверху):
  HOT   <= --hot дней   полный текст в global-handoff.md
  WARM  <= --warm дней  заголовок со ссылкой [детали], тело — в дневной файл
  COLD  старше          целиком в дневной файл

Хвостовые разделы (Открытые TASKS.md, Pipeline state, Человеческий контекст, Метрики дня) не трогаются.
handoff.md (машинный автосейв): остаётся последняя запись за календарный день.

Идемпотентен: свёрнутую запись повторно не сворачивает и в архив не дублирует.
По умолчанию dry-run. Применить: --apply
"""
import argparse, datetime, os, re, sys

HOME = os.path.expanduser("~")
GLOBAL = os.path.join(HOME, ".claude/global-handoff.md")
AUTOSAVE = os.path.join(HOME, ".claude/handoff.md")
WORKSPACE = os.path.join(HOME, ".claude/handoff-workspace")
LEGACY_ARCHIVE = os.path.join(HOME, ".claude/handoff-archive")

DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
FOLDED_RE = re.compile(r"\(\[детали\]\((?:handoff-workspace|handoff-archive)/")
ENTRY_RE = re.compile(r"^- \*\*")
SECTION_RE = re.compile(r"^### ")
TAIL_RE = re.compile(r"^## (?!По проектам)")
SESSION_HEAD_RE = re.compile(r"^- \*\*Сессия [^—]*—\s*(.+)$")   # «Сессия ДАТА — что сделано»
PLAIN_HEAD_RE = re.compile(r"^- \*\*(.+)$")                     # запись без маркера даты


def day_path(date_str):
    """Путь дневного файла относительно ~/.claude — он же ссылка из global-handoff.md."""
    return f"handoff-workspace/{date_str[:7]}/{date_str}.md"


def clean_head(text):
    """Строка для индекса: без разметки, ссылки [детали] и хвостового двоеточия."""
    text = text.split("([детали]")[0]
    text = text.replace("**", "").strip().rstrip(":").strip()
    return text[:160] + ("…" if len(text) > 160 else "")


def write_day(date_str, lines, project, apply_):
    """Дописать блок в дневной файл под заголовок проекта."""
    if not apply_:
        return
    p = os.path.join(HOME, ".claude", day_path(date_str))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    new = not os.path.exists(p)
    with open(p, "a") as f:
        if new:
            f.write(f"# Handoff {date_str}\n")
        if project:
            f.write(f"\n## {project}\n")
        f.write("\n".join(lines).rstrip() + "\n")


def tier_global(today, hot_days, warm_days, apply_):
    if not os.path.exists(GLOBAL):
        print("нет", GLOBAL); return
    lines = open(GLOBAL).read().split("\n")

    tail_at = len(lines)
    for i, l in enumerate(lines):
        if i > 0 and TAIL_RE.match(l):
            tail_at = i
            break
    body, tail = lines[:tail_at], lines[tail_at:]

    # нарезать на блоки: запись = от "- **" до следующего "- **" / "### " / конца
    blocks, cur = [], []
    for l in body:
        if ENTRY_RE.match(l) or SECTION_RE.match(l):
            if cur:
                blocks.append(cur)
            cur = [l]
        else:
            cur.append(l)
    if cur:
        blocks.append(cur)

    out = []
    cur_date, project = None, None
    n_hot = n_warm = n_cold = n_kept = 0
    moved = {}

    for b in blocks:
        head = b[0]
        if SECTION_RE.match(head):
            project = head[4:].strip()
            out.extend(b)
            continue
        if not ENTRY_RE.match(head):
            out.extend(b)
            continue
        m = DATE_RE.search(head)
        if m:
            cur_date = m.group(1)
        if not cur_date:
            out.extend(b)          # дату определить нечем — не трогаем
            continue
        try:
            age = (today - datetime.date.fromisoformat(cur_date)).days
        except ValueError:
            out.extend(b); continue

        folded = bool(FOLDED_RE.search(head))   # тело уже в дневном файле

        if age <= hot_days:
            out.extend(b); n_hot += 1
        elif age <= warm_days:
            if folded:
                out.extend(b); n_kept += 1
            else:
                out.append(head.rstrip() + f"  ([детали]({day_path(cur_date)}))")
                out.append("")
                write_day(cur_date, b, project, apply_)
                moved[cur_date] = moved.get(cur_date, 0) + 1
                n_warm += 1
        else:
            if not folded:                      # иначе тело уже в дневном файле
                write_day(cur_date, b, project, apply_)
                moved[cur_date] = moved.get(cur_date, 0) + 1
            n_cold += 1

    res = []
    for l in out:
        if l == "" and res[-2:] == ["", ""]:
            continue
        res.append(l)
    new = "\n".join(res + tail)

    old_b, new_b = os.path.getsize(GLOBAL), len(new.encode())
    print(f"global-handoff.md: hot={n_hot} warm={n_warm} cold={n_cold} "
          f"уже-свёрнуто={n_kept}  {old_b} → {new_b} B "
          f"({100 * (old_b - new_b) // max(old_b, 1)}% меньше)")
    for d in sorted(moved):
        print(f"  → {day_path(d)}: {moved[d]} записей")

    if apply_:
        open(GLOBAL, "w").write(new)
        print("  применено")


def rebuild_index(apply_):
    """INDEX.md: дата → проекты → что сделано. Собирается из дневных файлов."""
    if not os.path.isdir(WORKSPACE):
        return
    days = {}
    for mon in sorted(os.listdir(WORKSPACE)):
        d = os.path.join(WORKSPACE, mon)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not DATE_RE.match(fn):
                continue
            date = fn[:-3]
            project, items = None, []
            for l in open(os.path.join(d, fn)):
                if l.startswith("## "):
                    project = l[3:].strip()
                elif ENTRY_RE.match(l):
                    m = SESSION_HEAD_RE.match(l.rstrip()) or PLAIN_HEAD_RE.match(l.rstrip())
                    if m:
                        items.append((project or "—", clean_head(m.group(1))))
            days[date] = items

    rows = ["# Индекс handoff", "",
            "Что было сделано по дням. Тело записи — в дневном файле по ссылке.", ""]
    for date in sorted(days, reverse=True):
        items = days[date]
        projects = sorted({p for p, _ in items})
        # ссылка относительно самого INDEX.md, а он лежит внутри workspace
        rows.append(f"## [{date}]({date[:7]}/{date}.md) — {', '.join(projects) or '—'}")
        for p, text in items:
            rows.append(f"- **{p}** — {text}")
        rows.append("")

    body = "\n".join(rows)
    p = os.path.join(WORKSPACE, "INDEX.md")
    print(f"INDEX.md: {len(days)} дней, {sum(len(v) for v in days.values())} записей")
    if apply_:
        open(p, "w").write(body)
        print("  перестроен")


def dedupe_autosave(apply_):
    if not os.path.exists(AUTOSAVE):
        print("нет", AUTOSAVE); return
    lines = open(AUTOSAVE).read().split("\n")
    hdr = re.compile(r"^## Автосохранение .*?\((20\d{2}-\d{2}-\d{2})")

    blocks, cur, head_i, preamble = [], [], None, []
    for l in lines:
        if hdr.match(l):
            if cur:
                blocks.append((head_i, cur))
            cur, head_i = [l], hdr.match(l).group(1)
        elif cur:
            cur.append(l)
        else:
            preamble.append(l)
    if cur:
        blocks.append((head_i, cur))

    last = {}
    for d, b in blocks:
        last[d] = b                      # порядок в файле — хронологический
    kept = [b for d, b in sorted(last.items())]

    new = "\n".join(preamble + [l for b in kept for l in b]).rstrip() + "\n"
    old_b, new_b = os.path.getsize(AUTOSAVE), len(new.encode())
    print(f"handoff.md: {len(blocks)} записей → {len(kept)} (по одной на день), "
          f"{old_b} → {new_b} B")
    if apply_:
        open(AUTOSAVE, "w").write(new)
        print("  применено")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--hot", type=int, default=3, help="дней в HOT (полный текст)")
    ap.add_argument("--warm", type=int, default=14, help="дней в WARM (только заголовок)")
    ap.add_argument("--today", default=None, help="ГГГГ-ММ-ДД, для тестов")
    a = ap.parse_args()
    today = datetime.date.fromisoformat(a.today) if a.today else datetime.date.today()

    print(f"today={today} hot<={a.hot}д warm<={a.warm}д"
          f"{'' if a.apply else '   [DRY-RUN]'}\n")
    tier_global(today, a.hot, a.warm, a.apply)
    print()
    rebuild_index(a.apply)
    print()
    dedupe_autosave(a.apply)
    if not a.apply:
        print("\ndry-run. применить: %s --apply" % sys.argv[0])
