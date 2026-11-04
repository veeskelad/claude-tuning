#!/usr/bin/env python3
"""Автосводка сессии в общий стор (~/.agents/handoffs) — без ручного /session-handoff.

Кто зовёт: хуки Claude Code SessionEnd и PreCompact (у Codex — Stop и PreCompact).
Хук передаёт JSON в stdin (session_id, transcript_path, cwd). Режим `hook` отвечает сразу
и запускает `run` отдельным процессом в своей сессии (start_new_session): выход из сессии и
сжатие контекста не ждут модель, а код возврата хука всегда 0 — PreCompact с ненулевым кодом
блокирует сжатие.

`run`: выжимка транскрипта (просьбы, итоги ходов, правленые файлы, описания команд — без сырого
вывода инструментов) → маска ключей (secret-mask.py набора) → `claude -p` с потолком бюджета по
схеме references/content.json → handoff.save. Новая сводка той же сессии заменяет прошлую
(`supersedes`). Свежая ручная сводка сессии не перетирается.

Пропуск без модели: сессии `claude -p` (точка входа sdk-*), сессии без правок и меньше
чем с тремя просьбами, прирост транскрипта меньше 20 КБ с прошлой сводки.

  auto_summary.py hook --agent claude                      (из хука; JSON в stdin)
  auto_summary.py run --agent claude --session ID --transcript PATH --cwd DIR [--force] [--dry-run]
  auto_summary.py save-json --agent claude --session ID --cwd DIR   (готовая сводка JSON в stdin)

Журнал: ~/.local/state/claude-tuning/auto-summary.log
Настройка: CLAUDE_TUNING_SUMMARY_MODEL (по умолчанию sonnet), CLAUDE_TUNING_SUMMARY_BUDGET
(потолок на одну сводку в долларах, по умолчанию 0.25).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import handoff  # noqa: E402

STATE = Path.home() / '.local/state/claude-tuning'
LOG = STATE / 'auto-summary.log'
MASK_SCRIPT = Path.home() / '.claude/hooks/secret-mask.py'
GUARD = 'CLAUDE_TUNING_AUTO_SUMMARY'  # дочерний claude -p не должен сам запускать сводку
MIN_GROWTH = 20_000                   # байт транскрипта с прошлой сводки
CODEX_DEBOUNCE = 20 * 60              # Stop у Codex приходит каждый ход
DIGEST_LIMIT = 45_000
STATE_TTL = 30 * 86400                # служебные файлы сессий старше — убрать
NOISE = ('<', 'Base directory for this skill', 'Caveat:', '# AGENTS.md instructions')
MIDTURN = re.compile(r'The user sent a new message while you were working:\s*|\s*This is how Claude Code surfaces messages.*$', re.S)
MODEL = os.environ.get('CLAUDE_TUNING_SUMMARY_MODEL', 'sonnet')
BUDGET = os.environ.get('CLAUDE_TUNING_SUMMARY_BUDGET', '0.25')
HIDDEN = '[скрыто: похоже на ключ]'
LABEL = re.compile(r'‹secret[^›]*›')
KEY_BEFORE_LABEL = re.compile(r'["\']?[A-Za-z0-9_.\-]+["\']?\s*[:=]\s*(?=‹secret)')


def log(msg):
    STATE.mkdir(parents=True, exist_ok=True)
    with LOG.open('a') as fh:
        fh.write(f'{datetime.now().isoformat(timespec="seconds")} {msg}\n')


def clip(text, n):
    text = ' '.join(str(text).split())
    return text if len(text) <= n else text[:n] + '…'


# --- маска ключей ------------------------------------------------------------

_mask = None


def masker():
    """mask() из ~/.claude/hooks/secret-mask.py набора; нет файла — только построчный фильтр ниже."""
    global _mask
    if _mask is None:
        _mask = False
        if MASK_SCRIPT.is_file():
            try:
                spec = importlib.util.spec_from_file_location('kit_secret_mask', MASK_SCRIPT)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _mask = mod.mask
            except Exception:  # чужая или сломанная версия — работаем без неё
                _mask = False
    return _mask


def scrub(text):
    """Значения ключей → метки; строка, которая и после маски похожа на ключ, скрывается целиком."""
    text = str(text)
    fn = masker()
    if fn:
        try:
            text = fn(text)
        except Exception:
            pass
    text = KEY_BEFORE_LABEL.sub('', text)  # «TOKEN=‹secret…›» → «‹secret…›»: имя ключа есть в метке
    return '\n'.join(HIDDEN if handoff.SENSITIVE.search(LABEL.sub(' ', line)) else line
                     for line in text.split('\n'))


# --- выжимка транскрипта -----------------------------------------------------

def claude_digest(path):
    d = dict(prompts=[], replies=[], recaps=[], plans=[], compacts=[], files={}, bash=[], entry=None)
    with open(path, errors='replace') as fh:
        lines = fh.readlines()
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        d['entry'] = d['entry'] or r.get('entrypoint')
        m = r.get('message')
        if not isinstance(m, dict):
            continue
        # результат инструмента у Claude тоже приходит как role=user — его не берём
        if r.get('toolUseResult') is not None:
            continue
        content = m.get('content')
        blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) else (content or [])
        for c in blocks:
            if not isinstance(c, dict):
                continue
            if c.get('type') == 'text' and c.get('text'):
                t = c['text'].strip()
                if m.get('role') == 'user':
                    if t.startswith('This session is being continued'):
                        d['compacts'].append(clip(t, 6000))
                    elif t and not t.startswith(NOISE) and len(t) > 3:
                        t = clip(MIDTURN.sub('', t), 600)
                        if not d['prompts'] or d['prompts'][-1] != t:
                            d['prompts'].append(t)
                elif m.get('role') == 'assistant':
                    d['replies'].append(clip(t, 1500))
            elif c.get('type') == 'tool_use':
                name, inp = c.get('name') or '', c.get('input') or {}
                if name in ('Write', 'Edit', 'MultiEdit', 'NotebookEdit'):
                    fp = inp.get('file_path') or inp.get('notebook_path')
                    if fp:
                        d['files'][fp] = d['files'].get(fp, 0) + 1
                elif name == 'Bash' and inp.get('description'):
                    d['bash'].append(clip(inp['description'], 120))
                elif name.endswith('wrap_up') and inp.get('recap'):
                    d['recaps'].append(clip(inp['recap'], 600))
                elif name.endswith(('update_plan', 'TodoWrite')) and (inp.get('items') or inp.get('todos')):
                    items = inp.get('items') or inp.get('todos') or []
                    d['plans'].append('; '.join(f"[{str(i.get('status', ''))[:1]}] {i.get('content', '')}"
                                                for i in items if isinstance(i, dict)))
    return d


def codex_digest(path):
    d = dict(prompts=[], replies=[], recaps=[], plans=[], compacts=[], files={}, bash=[], entry=None)
    with open(path, errors='replace') as fh:
        lines = fh.readlines()
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        p = r.get('payload')
        if r.get('type') != 'response_item' or not isinstance(p, dict):
            continue
        kind = p.get('type')
        if kind == 'message':
            text = ' '.join(c.get('text', '') for c in p.get('content') or [] if isinstance(c, dict)).strip()
            if not text or text.startswith(NOISE):
                continue
            if p.get('role') == 'user':
                d['prompts'].append(clip(text, 600))
            elif p.get('role') == 'assistant':
                d['replies'].append(clip(text, 1500))
        elif kind == 'custom_tool_call':
            # патчи и команды внутри строки, переводы строк экранированы
            src = str(p.get('input', ''))
            for fp in re.findall(r'\*\*\* (?:Update|Add|Delete) File: ([^\\\n"]+)', src):
                d['files'][fp.strip()] = d['files'].get(fp.strip(), 0) + 1
            for cmd in re.findall(r'cmd:\s*"((?:[^"\\]|\\.)*)"', src):
                d['bash'].append(clip(cmd.replace('\\"', '"'), 120))
        elif kind == 'function_call':
            try:
                args = json.loads(p.get('arguments') or '{}')
            except ValueError:
                args = {}
            cmd = args.get('command') or args.get('cmd')
            if cmd:
                d['bash'].append(clip(' '.join(cmd) if isinstance(cmd, list) else cmd, 120))
    return d


def render(d, agent, cwd):
    parts = [f'Агент: {agent}. Каталог: {cwd}.']
    if d['files']:
        parts.append('Изменённые файлы:\n' + '\n'.join(f'- {f} ({n})' for f, n in
                                                     sorted(d['files'].items(), key=lambda x: -x[1])[:40]))
    if d['compacts']:
        parts.append('Сводка после сжатия контекста (последняя):\n' + d['compacts'][-1])
    parts.append('Просьбы пользователя по порядку:\n' + '\n'.join(f'{i}. {p}' for i, p in enumerate(d['prompts'][-40:], 1)))
    if d['plans']:
        parts.append('Последний план:\n' + d['plans'][-1])
    if d['recaps']:
        parts.append('Итоги ходов:\n' + '\n'.join(f'- {r}' for r in d['recaps'][-6:]))
    if d['bash']:
        parts.append('Команды (описания, последние):\n' + '\n'.join(f'- {b}' for b in d['bash'][-30:]))
    parts.append('Последние ответы агента:\n' + '\n---\n'.join(d['replies'][-8:]))
    text = scrub('\n\n'.join(parts))
    return text if len(text) <= DIGEST_LIMIT else text[-DIGEST_LIMIT:]


# --- модель ------------------------------------------------------------------

SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['skip', 'title', 'goal', 'status', 'summary', 'decisions', 'changes',
                 'verification', 'blockers', 'next_steps', 'constraints'],
    'properties': {
        'skip': {'type': 'boolean'},
        'title': {'type': 'string', 'maxLength': 150},
        'goal': {'type': 'string'}, 'summary': {'type': 'string'},
        'status': {'enum': ['in_progress', 'blocked', 'complete']},
        **{k: {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 12}
           for k in ('decisions', 'changes', 'verification', 'blockers', 'next_steps', 'constraints')},
    },
}

SYSTEM = """Ты пишешь сводку рабочей сессии ИИ-агента для общего журнала Claude и Codex.
По ней следующая сессия продолжит работу, а утренняя сводка покажет, что сделано. Пиши по-русски, кратко и по фактам
из выжимки, ничего не додумывай. Никаких ключей, токенов, паролей и сырого вывода команд.
- skip=true, если сессия — короткий вопрос-ответ без результата; остальные поля тогда можно оставить пустыми строками и списками.
- title: тема задачи (и номер задачи из трекера, если он есть); goal: что просил пользователь и его последние поправки.
- summary: что сделано и где остановились. changes: «абсолютный путь — что и зачем».
- verification: что проверено и с каким итогом; непроверенное — так и написать.
- status: complete только если цель достигнута и следующего шага нет; blocked — назвать блокер.
- next_steps: первым — конкретное действие; если есть чек-лист .tasks/*.md — его абсолютный путь."""


def claude_bin():
    exe = shutil.which('claude')
    if exe:
        return exe
    fallback = Path.home() / '.local/bin/claude'
    return str(fallback) if fallback.is_file() else None


def summarize(digest):
    exe = claude_bin()
    if not exe:
        raise RuntimeError('claude не найден в PATH')
    env = dict(os.environ, **{GUARD: '1'})
    # Без --bare: он выключил бы и вход по подписке. Хуки дочернего прогона выключены настройкой,
    # инструментов нет, MCP не грузится, сессия не сохраняется.
    cmd = [exe, '-p', '--model', MODEL, '--max-budget-usd', BUDGET,
           '--no-session-persistence', '--output-format', 'json',
           '--tools', '', '--strict-mcp-config', '--disable-slash-commands',
           '--settings', '{"disableAllHooks":true}', '--system-prompt', SYSTEM,
           '--json-schema', json.dumps(SCHEMA)]
    STATE.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(cmd, input=digest, capture_output=True, text=True, timeout=300,
                         env=env, cwd=str(STATE))
    try:
        data = json.loads(out.stdout or '{}')
    except ValueError:
        data = {}
    body = data.get('structured_output') if isinstance(data, dict) else None
    if out.returncode or data.get('is_error') or not isinstance(body, dict):
        raise RuntimeError(f'claude -p: rc={out.returncode} {clip(scrub(out.stderr or data.get("result", "")), 300)}')
    return body, data.get('total_cost_usd')


def clean_body(body):
    body = {k: v for k, v in body.items() if k != 'skip'}
    for k in ('decisions', 'changes', 'verification', 'blockers', 'next_steps', 'constraints'):
        body[k] = [clip(scrub(v), 1500) for v in body.get(k) or [] if str(v).strip()][:12]
    body['title'] = clip(scrub(body.get('title') or '') or 'Сессия без темы', 150)
    body['summary'] = clip(scrub(body.get('summary') or ''), 5800) + ' (автосводка)'
    body['goal'] = clip(scrub(body.get('goal') or ''), 5800) or body['title']
    if body.get('status') not in ('in_progress', 'blocked', 'complete'):
        body['status'] = 'in_progress'
    if body['status'] != 'complete' and not body['next_steps']:
        body['next_steps'] = ['Уточнить у пользователя следующий шаг: в сессии он не назван']
    if body['status'] == 'blocked' and not body['blockers']:
        body['status'] = 'in_progress'
    return body


# --- запуск ------------------------------------------------------------------

def last_own(project, agent, session):
    records = handoff.all_records(project)
    succ = {r['supersedes'] for r in records if r['supersedes']}
    own = [r for r in records if r['source_agent'] == agent and r['source_session'] == session and r['id'] not in succ]
    return max(own, key=lambda r: r['created_at']) if own else None


def prune_state():
    now = time.time()
    for p in STATE.glob('*-*.*'):
        if p.suffix in ('.json', '.lock') and now - p.stat().st_mtime > STATE_TTL:
            p.unlink(missing_ok=True)


def run(agent, session, transcript, cwd, event='', force=False, dry=False):
    STATE.mkdir(parents=True, exist_ok=True)
    tpath = Path(transcript)
    if not tpath.is_file():
        return log(f'{agent} {session}: нет транскрипта {transcript}')
    state_file = STATE / f'{agent}-{re.sub(r"[^A-Za-z0-9_.-]", "_", session)}.json'
    with open(state_file.with_suffix('.lock'), 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return log(f'{agent} {session}: уже идёт')
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        size = tpath.stat().st_size

        def remember(**extra):
            state.update(size=size, at=time.time(), **extra)
            state_file.write_text(json.dumps(state))

        if not force:
            if size - state.get('size', 0) < MIN_GROWTH:
                return log(f'{agent} {session}: прирост {size - state.get("size", 0)} Б — пропуск')
            if agent == 'codex' and event == 'Stop' and time.time() - state.get('at', 0) < CODEX_DEBOUNCE:
                return log(f'{agent} {session}: Stop чаще {CODEX_DEBOUNCE} с — пропуск')
        d = claude_digest(tpath) if agent == 'claude' else codex_digest(tpath)
        # sdk-* — автоматические прогоны claude -p (в том числе сама автосводка): итог у них свой
        if not force and str(d.get('entry') or '').startswith('sdk'):
            remember()
            return log(f'{agent} {session}: {d["entry"]} (claude -p) — пропуск')
        if not force and not d['files'] and len(d['prompts']) < 3:
            remember()
            return log(f'{agent} {session}: без правок и меньше 3 просьб — пропуск')
        project = handoff.project_context(cwd)
        prev = last_own(project, agent, session)
        mtime = datetime.fromtimestamp(tpath.stat().st_mtime, timezone.utc)
        if prev and not force and prev['id'] != state.get('id') and \
                datetime.fromisoformat(prev['created_at']) >= mtime.replace(microsecond=0) - timedelta(minutes=2):
            return log(f'{agent} {session}: свежая ручная сводка {prev["id"]} — пропуск')
        digest = render(d, agent, cwd)
        if dry:
            print(digest)
            return
        body, cost = summarize(digest)
        if body.get('skip'):
            remember()
            return log(f'{agent} {session}: модель — без результата, пропуск (${cost})')
        body = clean_body(body)
        target = handoff.DEFAULT_TARGET[agent]
        try:
            rec = handoff.save(project, agent, target, session, body, prev['id'] if prev else None)
        except ValueError as e:
            if not prev:
                raise
            log(f'{agent} {session}: цепочка {prev["id"]} закрыта ({e}), новая запись')
            rec = handoff.save(project, agent, target, session, body)
        remember(id=rec['id'])
        log(f'{agent} {session}: сохранено {rec["id"]} «{body["title"]}» {event} ${cost}')


def save_json(agent, session, cwd, text):
    """Сводку написала сама сессия (например, перед переносом в новый чат): сохранить в цепочку сессии."""
    if not session:
        raise ValueError('нужен --session')
    m = re.search(r'\{.*\}', text, re.S)  # модель могла обернуть JSON в ``` или пояснение
    body = json.loads(m.group(0) if m else text)
    body.setdefault('skip', False)
    body = clean_body(body)
    body['summary'] = body['summary'].replace(' (автосводка)', ' (сводка сессии при переносе)')
    project = handoff.project_context(cwd)
    prev = last_own(project, agent, session)
    target = handoff.DEFAULT_TARGET[agent]
    try:
        rec = handoff.save(project, agent, target, session, body, prev['id'] if prev else None)
    except ValueError:
        if not prev:
            raise
        rec = handoff.save(project, agent, target, session, body)
    path = handoff.directory_for(project) / f'{rec["id"]}.json'
    log(f'{agent} {session}: сохранено из сессии {rec["id"]} «{body["title"]}»')
    print(path)


def hook(agent):
    # дочерний прогон самой автосводки и любые claude -p — без сводки
    if os.environ.get(GUARD) or os.environ.get('CLAUDE_CODE_ENTRYPOINT', '').startswith('sdk'):
        return 0
    # выключатель человека: сводка тратит лимит подписки
    if os.environ.get('CLAUDE_TUNING_AUTO_SUMMARY') == '0':
        return 0
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    session, transcript = str(data.get('session_id') or ''), str(data.get('transcript_path') or '')
    if session and not transcript and agent == 'codex':
        # Codex может не передать путь: rollout-<время>-<session_id>.jsonl
        found = sorted((Path.home() / '.codex/sessions').glob(f'*/*/*/rollout-*{session}.jsonl'))
        transcript = str(found[-1]) if found else ''
    if not session or not transcript:
        return 0
    STATE.mkdir(parents=True, exist_ok=True)
    try:
        prune_state()
    except OSError:
        pass
    args = [sys.executable, str(Path(__file__).resolve()), 'run', '--agent', agent, '--session', session,
            '--transcript', transcript, '--cwd', str(data.get('cwd') or os.getcwd()),
            '--event', str(data.get('hook_event_name') or '')]
    # Своя сессия процесса: выход Claude Code не убивает сводку, хук не ждёт модель.
    with LOG.open('a') as fh:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=fh, stderr=fh, start_new_session=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', choices=['hook', 'run', 'save-json'])
    ap.add_argument('--agent', default='claude', choices=['claude', 'codex'])
    ap.add_argument('--session')
    ap.add_argument('--transcript')
    ap.add_argument('--cwd', default=os.getcwd())
    ap.add_argument('--event', default='')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    if sys.argv[1:2] == ['hook']:
        # Хук не падает и не держит сессию ни при каких входных данных.
        try:
            return hook(ap.parse_args().agent)
        except BaseException:
            return 0
    a = ap.parse_args()
    if a.mode == 'save-json':
        try:
            save_json(a.agent, a.session, a.cwd, sys.stdin.read())
        except Exception as e:
            print(f'{type(e).__name__}: {clip(scrub(e), 300)}', file=sys.stderr)
            return 1
        return 0
    if not a.session or not a.transcript:
        ap.error('run: нужны --session и --transcript')
    try:
        run(a.agent, a.session, a.transcript, a.cwd, a.event, a.force, a.dry_run)
    except Exception as e:  # фон: ошибка только в журнал
        log(f'{a.agent} {a.session}: ошибка {type(e).__name__}: {clip(scrub(e), 400)}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
