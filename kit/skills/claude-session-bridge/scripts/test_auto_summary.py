"""Проверка auto_summary.py без модели: запуск — python3 -m unittest test_auto_summary (из папки scripts)."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import auto_summary
import handoff


def claude_line(role, content, **extra):
    return json.dumps({'type': role, 'entrypoint': extra.pop('entry', 'cli'),
                       'message': {'role': role, 'content': content}, **extra})


class AutoSummaryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.project = self.base / 'project'
        self.project.mkdir()
        for target, name, value in ((handoff, 'STORE', self.base / 'store'),
                                    (auto_summary, 'STATE', self.base / 'state'),
                                    (auto_summary, 'LOG', self.base / 'state' / 'log'),
                                    (auto_summary, 'MASK_SCRIPT', self.base / 'no-mask.py'),
                                    (auto_summary, '_mask', None)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.body = {'skip': False, 'title': 'Правка скрипта', 'goal': 'Починить скрипт',
                     'status': 'in_progress', 'summary': 'Скрипт поправлен.', 'decisions': [],
                     'changes': ['/tmp/x.py — правка'], 'verification': ['тест не запускался'],
                     'blockers': [], 'next_steps': [], 'constraints': []}

    def transcript(self, name, lines):
        path = self.base / name
        # прирост больше MIN_GROWTH, иначе run пропустит
        path.write_text('\n'.join(lines + [json.dumps({'pad': 'x' * auto_summary.MIN_GROWTH})]) + '\n')
        return path

    def claude_transcript(self, entry='cli', edits=True):
        lines = [claude_line('user', 'почини скрипт', entry=entry),
                 claude_line('user', 'The user sent a new message while you were working:\nи тест'
                             '\n\nThis is how Claude Code surfaces messages the user sends mid-turn'),
                 claude_line('user', 'почини скрипт'),
                 claude_line('user', [{'type': 'tool_result', 'content': 'СЫРОЙ_ВЫВОД'}], toolUseResult={'x': 1})]
        if edits:
            lines.append(claude_line('assistant', [
                {'type': 'tool_use', 'name': 'Edit', 'input': {'file_path': '/tmp/x.py'}},
                {'type': 'tool_use', 'name': 'mcp__ccd_turn__wrap_up', 'input': {'recap': 'Скрипт поправлен.'}},
                {'type': 'text', 'text': 'Готово.'}]))
        return self.transcript('c.jsonl', lines)

    def run_summary(self, path, agent='claude', **kw):
        with patch.object(auto_summary, 'summarize', return_value=(dict(self.body), 0.01)) as model:
            auto_summary.run(agent, 'sess-1', str(path), str(self.project), **kw)
        return model

    def records(self):
        return handoff.all_records(handoff.project_context(self.project))

    def test_claude_digest(self):
        d = auto_summary.claude_digest(self.claude_transcript())
        self.assertEqual(d['files'], {'/tmp/x.py': 1})
        self.assertEqual(d['recaps'], ['Скрипт поправлен.'])
        self.assertIn('и тест', d['prompts'])
        self.assertFalse(any('This is how' in p for p in d['prompts']))
        text = auto_summary.render(d, 'claude', '/p')
        self.assertIn('/tmp/x.py', text)
        self.assertNotIn('СЫРОЙ_ВЫВОД', text)

    def test_codex_digest(self):
        lines = [json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                             'content': [{'type': 'input_text', 'text': 'сделай'}]}}),
                 json.dumps({'type': 'response_item', 'payload': {'type': 'custom_tool_call',
                             'input': '*** Begin Patch\n*** Update File: /tmp/y.py\n@@\n'}}),
                 json.dumps({'type': 'response_item', 'payload': {'type': 'function_call',
                             'arguments': json.dumps({'command': ['bash', '-lc', 'ls']})}}),
                 json.dumps(['не словарь'])]
        d = auto_summary.codex_digest(self.transcript('x.jsonl', lines))
        self.assertEqual(d['files'], {'/tmp/y.py': 1})
        self.assertEqual(d['prompts'], ['сделай'])
        self.assertEqual(d['bash'], ['bash -lc ls'])

    def test_saves_then_supersedes(self):
        path = self.claude_transcript()
        self.run_summary(path)
        first = self.records()
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0]['content']['summary'].endswith('(автосводка)'))
        self.assertTrue(first[0]['content']['next_steps'])  # дописан, раз модель не назвала
        with path.open('a') as fh:
            fh.write(json.dumps({'pad': 'y' * auto_summary.MIN_GROWTH}) + '\n')
        self.body['summary'] = 'Скрипт поправлен и проверен.'
        self.run_summary(path)
        second = [r for r in self.records() if r['supersedes']]
        self.assertEqual(second[0]['supersedes'], first[0]['id'])

    def test_small_growth_is_skipped(self):
        path = self.claude_transcript()
        self.run_summary(path)
        model = self.run_summary(path)  # транскрипт не вырос
        model.assert_not_called()

    def test_skips(self):
        model = self.run_summary(self.claude_transcript(entry='sdk-cli'))
        model.assert_not_called()
        model = self.run_summary(self.claude_transcript(edits=False))
        model.assert_not_called()  # без правок и меньше 3 разных просьб
        self.assertEqual(self.records(), [])

    def test_save_json_from_session(self):
        body = {k: v for k, v in self.body.items() if k != 'skip'}
        body['next_steps'] = ['Запустить тест']
        with patch('sys.stdout'):
            auto_summary.save_json('claude', 'sess-2', str(self.project), '```json\n' + json.dumps(body) + '\n```')
        rec = self.records()[0]
        self.assertEqual(rec['source_session'], 'sess-2')
        self.assertTrue(rec['content']['summary'].endswith('(сводка сессии при переносе)'))

    def test_scrub_without_mask_hides_line(self):
        fake = 'gh' + 'p_' + 'A1b2C3d4' * 5
        text = auto_summary.scrub(f'обычная строка\nтокен {fake} в строке')
        self.assertNotIn(fake, text)
        self.assertIn('обычная строка', text)
        self.assertIn(auto_summary.HIDDEN, text)

    def test_model_output_with_secret_is_saved_masked(self):
        fake = 's' + 'k-' + 'Zx9' * 8
        self.body['summary'] = f'Поставил ключ {fake}'
        self.body['next_steps'] = ['Проверить']
        self.run_summary(self.claude_transcript())
        rec = self.records()[0]
        self.assertNotIn(fake, json.dumps(rec, ensure_ascii=False))

    def test_hook_ignores_child_and_print_mode(self):
        for env in ({auto_summary.GUARD: '1'}, {'CLAUDE_CODE_ENTRYPOINT': 'sdk-cli'}):
            with patch.dict('os.environ', env), patch('subprocess.Popen') as popen, \
                    patch('sys.stdin', io.StringIO(json.dumps({'session_id': 's', 'transcript_path': '/x'}))):
                self.assertEqual(auto_summary.hook('claude'), 0)
                popen.assert_not_called()

    def test_hook_mode_never_fails(self):
        for argv, data in ((['auto_summary.py', 'hook'], 'не json'),
                           (['auto_summary.py', 'hook', '--agent', 'нет-такого'], '{}'),
                           (['auto_summary.py', 'hook'], '[]')):
            with patch('sys.argv', argv), patch('sys.stdin', io.StringIO(data)), \
                    patch('sys.stderr', io.StringIO()), patch('subprocess.Popen') as popen:
                self.assertEqual(auto_summary.main(), 0)
                popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
