---
name: memory-consolidate
description: "Аудит и консолидация файловой памяти Claude Code проекта (~/.claude/projects/<slug>/memory): потолок MEMORY.md, битые ссылки индекса, файлы-сироты, записи без frontmatter, раздутые записи, размер handoff. Вызывай когда пользователь пишет /memory-consolidate, 'почисти память', 'аудит памяти'."
---

# Memory Consolidate

Память проекта — `~/.claude/projects/<slug>/memory/`: один файл на факт, индекс `MEMORY.md`
грузится в каждую сессию: **первые 200 строк или 25 КБ, что наступит раньше** (code.claude.com/docs/en/memory),
остальное отбрасывается. Упирается обычно в размер, а не в строки: кириллица и ссылки `[текст](файл.md)` тяжёлые.
Тематические файлы читаются только по запросу. Закрытое и узкое — в `ARCHIVE.md`.

## Фаза 1: Диагностика (только чтение)

Запускать из корня проекта, память которого чистим.

```bash
M=~/.claude/projects/$(pwd | sed 's/[^A-Za-z0-9]/-/g')/memory
cd "$M" || exit 1
echo "MEMORY.md: $(wc -l < MEMORY.md) строк (потолок 200), $(python3 -I -c 'print(len(open("MEMORY.md",encoding="utf-8").read()))') символов (ориентир < 17 тыс.: 25 КБ кириллицы)"
skip='MEMORY.md|ARCHIVE.md|handoff.md|lessons-learned.md'   # служебные, проверки к ним не относятся
refs=$(grep -ohE '\]\([^)]+\.md\)' MEMORY.md ARCHIVE.md 2>/dev/null | sed -E 's/.*\(([^)]+)\)/\1/' | grep -vxE "$skip" | sort -u)
files=$(for f in *.md; do echo "$f"; done | grep -vxE "$skip" | sort)
echo "файлов: $(echo "$files" | wc -l)"
echo "--- битые ссылки индекса";  comm -13 <(echo "$files") <(echo "$refs")
echo "--- сироты (нет в индексе)"; comm -23 <(echo "$files") <(echo "$refs")
echo "--- без frontmatter/description"
for f in $files; do head -1 "$f" | grep -qx -- '---' && grep -q '^description:' "$f" || echo "$f"; done
echo "--- крупнее 15 КБ"
for f in $files; do s=$(wc -c < "$f"); [ "$s" -gt 15360 ] && echo "$((s/1024)) КБ $f"; done
~/.claude/bin/handoff-tier.py   # dry-run: что свернётся в handoff
```

`handoff.md` — не сирота: им управляет `handoff-tier.py` (свой тиринг, HOT ≤3 дней
остаётся целиком). `lessons-learned.md` большой намеренно, читается только грепом.

Транскрипты сессий (`~/.claude/projects/*/*.jsonl`) руками не чистить: Claude Code сам
удаляет старше `cleanupPeriodDays` (по умолчанию 30 дней), а `session-search` на них живёт.

## Фаза 2: Консолидация (только с подтверждением)

Показать список находок, править после «да».

1. **Битая ссылка** — найти, куда уехал файл (`grep -l '^name: <slug>'`), поправить строку;
   файла нет — убрать строку.
2. **Сирота** — вписать хук в подходящий раздел `MEMORY.md` или в `ARCHIVE.md`.
   Дубль существующей записи — слить в неё, сироту удалить.
3. **Без frontmatter** — дописать `name`, `description`, `metadata.type`.
4. **`MEMORY.md` ближе 180 строк или больше 17 тыс. символов** — закрытое и узкое перенести в `ARCHIVE.md`
   (история, разовые инциденты, дубли CLAUDE.md, то, что покрыто скиллом),
   соседние хуки одной темы собрать в одну строку, как в существующих разделах.
5. **Файл крупнее 15 КБ** — сжать до факта плюс **Why** / **How to apply**, хронологию разбора убрать.
6. **Handoff** — если dry-run показал выигрыш, `~/.claude/bin/handoff-tier.py --apply`.
   Файл целиком не перемещать и пустым не создавать: `/morning` читает именно его,
   а ссылки `[детали]` из свёрнутых записей ведут в `handoff-workspace/ГГГГ-ММ/ГГГГ-ММ-ДД.md`.

## Фаза 3: Отчёт

```
## До
- MEMORY.md: X строк, файлов: N
- Битых ссылок: A, сирот: B, без frontmatter: C, крупных: D
- Handoff: X → Y байт (dry-run)

## После
- Что исправлено, слито, перенесено в ARCHIVE.md, удалено
```
