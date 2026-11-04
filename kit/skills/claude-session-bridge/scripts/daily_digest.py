#!/usr/bin/env python3
"""Вечерняя и утренняя сводка из стора сводок (~/.agents/handoffs) — без ручных /endday и /morning.

Сводки сессий Claude и Codex уже лежат в сторе (их пишет auto_summary.py и сами агенты через
handoff.py). Модель здесь не нужна: скрипт только раскладывает записи.

  evening  записи за сутки → ~/.claude/global-handoff.md: «### проект» → «- **Сессия ДАТА — …**»,
           одна запись на задачу (обновляется на месте), затем handoff-tier.py --apply.
           Расписание — launchd 23:30 (kit/launchd/handoff-evening.plist.template).
  morning  ~/.claude/morning-digest.md: блокеры, задачи по проектам со следующим шагом,
           открытые чек-листы .tasks/. После выходных — с пятницы. launchd 07:00.
  show     печатает утреннюю сводку один раз в день — для хука SessionStart; в claude -p молчит.
           Сводки нет или она старше 20 часов (компьютер спал, нет launchd) — собирает её сам.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import handoff  # noqa: E402

HOME = Path.home()
STORE = handoff.STORE
GLOBAL = HOME / '.claude/global-handoff.md'
DIGEST = HOME / '.claude/morning-digest.md'
TIER = HOME / '.claude/bin/handoff-tier.py'
OPEN_TASKS = HOME / '.claude/hooks/open-tasks.sh'
SHOWN = HOME / '.local/state/claude-tuning/digest-shown'
SKELETON = '# Handoff\n\n## По проектам\n\n## Человеческий контекст\n'
STATUS = {'blocked': '⛔', 'in_progress': '▶', 'complete': '✓'}
SHOW_LINES = 40
EMPTY = re.compile(r'^## За \d+ ч: 0 задач$', re.M)


def records(since):
    """Последняя версия каждой задачи, изменённой после since: все проекты и агенты.

    Каждая запись проходит ту же проверку, что и при чтении через handoff.py: чужой формат,
    ссылка вместо файла или текст, похожий на ключ, в сводку не попадают."""
    items = []
    for path in STORE.glob('*/*.json'):
        try:
            raw = json.loads(path.read_text()) if not path.is_symlink() else None
            if not isinstance(raw, dict) or not isinstance(raw.get('project'), dict) \
                    or raw['project'].get('key') != path.parent.name:
                continue
            r = handoff.load_record(path, raw['project'])
            r['_at'] = datetime.fromisoformat(r['created_at'])
        except (OSError, ValueError, TypeError, KeyError):
            continue
        items.append(r)
    superseded = {r['supersedes'] for r in items if r['supersedes']}
    return sorted((r for r in items if r['id'] not in superseded and r['_at'] >= since),
                  key=lambda r: r['_at'], reverse=True)


def project_name(r):
    """Имя репозитория: общий .git всех его worktree → папка репозитория."""
    ident = str((r.get('project') or {}).get('identity') or '').rstrip('/')
    if ident.endswith('/.git'):
        ident = ident[:-5]
    if not ident:
        return 'без проекта'
    if ident == str(HOME):
        return '~'
    return Path(ident).name or ident


def one(text, n=220):
    text = ' '.join(str(text).split())
    return text if len(text) <= n else text[:n] + '…'


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as fh:
        fh.write(text)
    os.replace(fh.name, path)


# --- вечер: global-handoff.md -----------------------------------------------------------

def entry(r):
    c = r['content']
    day = r['_at'].astimezone().strftime('%Y-%m-%d')
    # Маркер «- **Сессия ГГГГ-ММ-ДД» — по нему handoff-tier.py сворачивает старое.
    lines = [f"- **Сессия {day} — {one(c['title'], 150)}** ({r['source_agent']}, {STATUS.get(c['status'], '')}) <!-- task:{r['task_id']} -->",
             f"  {one(c['summary'], 600)}"]
    if c.get('blockers'):
        lines.append(f"  Блокер: {one(c['blockers'][0])}")
    if c.get('next_steps') and c['status'] != 'complete':
        lines.append(f"  Дальше: {one(c['next_steps'][0])}")
    resume = {'claude': 'claude --resume', 'codex': 'codex resume'}.get(r['source_agent'])
    if resume:
        lines.append(f"  Вернуть: `{resume} {r['source_session']}`")
    return lines


def evening(apply_tier=True):
    recs = records(datetime.now(timezone.utc) - timedelta(hours=26))
    old = GLOBAL.read_text() if GLOBAL.exists() else SKELETON
    lines = old.split('\n')
    if not any(l.startswith('## По проектам') for l in lines):
        lines[1:1] = ['', '## По проектам']
    added = updated = 0
    for r in reversed(recs):  # старые первыми: новые встанут выше
        marker = f"<!-- task:{r['task_id']} -->"
        new = entry(r)
        # та же задача уже есть — заменить запись (заголовок и строки с отступом под ним)
        idx = next((i for i, l in enumerate(lines) if marker in l), None)
        if idx is not None:
            end = idx + 1
            while end < len(lines) and lines[end].startswith('  '):
                end += 1
            if lines[idx:end] != new:
                lines[idx:end] = new
                updated += 1
            continue
        head = f"### {project_name(r)}"
        start = next(i for i, l in enumerate(lines) if l.startswith('## По проектам'))
        stop = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('## ')), len(lines))
        sec = next((i for i in range(start, stop) if lines[i].strip().lower() == head.lower()), None)
        if sec is None:
            lines[start + 1:start + 1] = ['', head]
            sec = start + 2
        lines[sec + 1:sec + 1] = new
        added += 1
    if lines and lines[0].startswith('# Handoff'):
        lines[0] = f"# Handoff — {datetime.now().strftime('%Y-%m-%d')}"
    text = '\n'.join(lines)
    if text != old or not GLOBAL.exists():
        write_atomic(GLOBAL, text)
    print(f'global-handoff: добавлено {added}, обновлено {updated} из {len(recs)} задач за сутки')
    if apply_tier and TIER.is_file():
        subprocess.run([sys.executable, str(TIER), '--apply'], check=False, timeout=120)


# --- утро: morning-digest.md --------------------------------------------------------------

def morning(quiet=False, tasks_timeout=60):
    now = datetime.now(timezone.utc)
    hours = 72 if datetime.now().weekday() == 0 else 26  # в понедельник — с пятницы
    recs = records(now - timedelta(hours=hours))
    out = [f"# Утро {datetime.now().strftime('%Y-%m-%d %H:%M')}", '']
    blocked = [r for r in recs if r['content']['status'] == 'blocked']
    if blocked:
        out.append('## Блокеры')
        out += [f"- {project_name(r)}: {one(r['content']['title'], 100)} — {one(r['content']['blockers'][0], 160)}"
                for r in blocked]
        out.append('')
    out.append(f'## За {hours} ч: {len(recs)} задач')
    by_project = {}
    for r in recs:
        by_project.setdefault(project_name(r), []).append(r)
    for name, items in sorted(by_project.items(), key=lambda kv: -len(kv[1])):
        out.append(f'### {name}')
        for r in items:
            c = r['content']
            tail = f" → {one(c['next_steps'][0], 140)}" if c['status'] != 'complete' and c['next_steps'] else ''
            out.append(f"- {STATUS.get(c['status'], '')} {one(c['title'], 110)} ({r['source_agent']}){tail}")
    if OPEN_TASKS.is_file():
        try:
            tasks = subprocess.run(['bash', str(OPEN_TASKS), '--all'], cwd=str(HOME), capture_output=True,
                                   text=True, timeout=tasks_timeout,
                                   env=dict(os.environ, OPEN_TASKS_MAX='5')).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            tasks = ''
        if tasks:
            out += ['', '## Чек-листы', tasks]
    write_atomic(DIGEST, '\n'.join(out) + '\n')
    if not quiet:
        print(f'morning-digest: {len(recs)} задач, {len(blocked)} блокеров → {DIGEST}')


def show():
    if os.environ.get('CLAUDE_CODE_ENTRYPOINT', '').startswith('sdk'):
        return  # claude -p: сводка в автоматическом прогоне не нужна
    today = datetime.now().strftime('%Y-%m-%d')
    if SHOWN.exists() and SHOWN.read_text().strip() == today:
        return
    # сводки нет или она старше 20 часов — launchd не отработал (компьютер спал, Linux без cron)
    if not DIGEST.exists() or datetime.now().timestamp() - DIGEST.stat().st_mtime > 20 * 3600:
        morning(quiet=True, tasks_timeout=15)
    text = DIGEST.read_text()
    SHOWN.parent.mkdir(parents=True, exist_ok=True)
    SHOWN.write_text(today)
    if EMPTY.search(text) and '## Чек-листы' not in text:
        return  # пустая сводка — не шуметь
    lines = text.rstrip('\n').split('\n')
    tail = f'\n… ещё {len(lines) - SHOW_LINES} строк: {DIGEST}' if len(lines) > SHOW_LINES else ''
    print('\n'.join(lines[:SHOW_LINES]) + tail)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', choices=['evening', 'morning', 'show'])
    ap.add_argument('--no-tier', action='store_true', help='evening без handoff-tier.py')
    a = ap.parse_args()
    if a.mode == 'show':
        try:  # хук SessionStart: сбой сводки не мешает старту сессии
            show()
        except Exception:
            pass
        return 0
    evening(not a.no_tier) if a.mode == 'evening' else morning()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
