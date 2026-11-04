"""Проверка handoff.py: запуск — python3 -m unittest test_handoff (из папки scripts)."""
from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import handoff


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.project = self.base / 'project'
        self.project.mkdir()
        self.store = self.base / 'handoffs'
        self.addCleanup(self.temporary.cleanup)
        storage = patch.object(handoff, 'STORE', self.store)
        storage.start()
        self.addCleanup(storage.stop)
        self.context = handoff.project_context(self.project)
        self.body = {'title': 'Fix settings', 'goal': 'Preserve the selected option',
                     'status': 'in_progress', 'summary': 'The update is implemented.',
                     'decisions': ['Keep the existing data format'],
                     'changes': ['settings.py — preserve the option'],
                     'verification': ['Unit checks passed; UI check not run'],
                     'blockers': [], 'next_steps': ['Verify the UI'],
                     'constraints': ['Do not deploy without a request']}

    def save(self, body=None, **kwargs):
        return handoff.save(self.context, 'codex', 'claude', 'codex-session',
                            body or self.body, **kwargs)

    def test_round_trip_preserves_task_and_history(self):
        first = self.save()
        read = handoff.read(self.context, first['id'], 'claude')
        self.assertEqual(read['handoff']['content'], self.body)
        self.assertTrue(all(read['checkout_check'].values()))
        handoff.receive(self.context, first['id'], 'claude', 'claude-session')
        updated = copy.deepcopy(self.body)
        updated['verification'].append('UI check passed')
        updated['status'] = 'complete'
        updated['next_steps'] = []
        second = handoff.save(self.context, 'claude', 'codex', 'claude-session', updated, first['id'])
        self.assertEqual(second['task_id'], first['task_id'])
        self.assertEqual(second['supersedes'], first['id'])
        self.assertEqual(handoff.read(self.context, second['id'], 'codex')['handoff']['content'], updated)
        self.assertEqual(len(handoff.all_records(self.context)), 2)
        self.assertEqual(handoff.active_records(self.context, 'claude'), [])
        self.assertEqual(handoff.active_records(self.context, 'codex'), [])
        self.assertEqual(len(handoff.active_records(self.context, 'codex', True)), 1)

    def test_unchanged_update_is_idempotent(self):
        record = self.save()
        self.assertEqual(self.save(supersedes=record['id'])['id'], record['id'])
        self.assertEqual(len(handoff.all_records(self.context)), 1)

    def test_parallel_tasks_have_separate_records(self):
        first = self.save()
        other = copy.deepcopy(self.body)
        other['title'] = 'A different task'
        second = self.save(other)
        self.assertNotEqual(first['task_id'], second['task_id'])
        self.assertEqual(len(handoff.active_records(self.context, 'claude')), 2)

    def test_unreceived_handoff_cannot_be_superseded_by_other_session(self):
        first = self.save()
        with self.assertRaises(ValueError):
            handoff.save(self.context, 'claude', 'codex', 'claude-session', self.body, first['id'])

    def test_received_record_cannot_be_overwritten_by_sender(self):
        first = self.save()
        handoff.receive(self.context, first['id'], 'claude', 'claude-session')
        with self.assertRaises(ValueError):
            self.save(supersedes=first['id'])

    def test_receiving_is_idempotent_but_exclusive(self):
        first = self.save()
        handoff.receive(self.context, first['id'], 'claude', 'claude-session')
        handoff.receive(self.context, first['id'], 'claude', 'claude-session')
        with self.assertRaises(ValueError):
            handoff.receive(self.context, first['id'], 'claude', 'other-session')
        with self.assertRaises(ValueError):
            handoff.receive(self.context, first['id'], 'codex', 'codex-session')

    def test_stale_revision_cannot_be_received_or_updated(self):
        first = self.save()
        updated = copy.deepcopy(self.body)
        updated['summary'] = 'Another check completed'
        self.save(updated, supersedes=first['id'])
        with self.assertRaises(ValueError):
            handoff.receive(self.context, first['id'], 'claude', 'claude-session')
        with self.assertRaises(ValueError):
            self.save(updated, supersedes=first['id'])

    def test_unrelated_repository_cannot_read_record(self):
        first = self.save()
        other = self.base / 'other'
        other.mkdir()
        project = handoff.project_context(other)
        self.assertEqual(handoff.active_records(project, 'claude'), [])
        with self.assertRaises(OSError):
            handoff.read(project, first['id'], 'claude')

    def test_checkout_changes_are_reported(self):
        first = self.save()
        changed = {**self.context, 'checkout': str(self.base / 'linked'), 'branch': 'other', 'head': 'new'}
        result = handoff.read(changed, first['id'], 'claude')
        self.assertFalse(any(result['checkout_check'].values()))

    def test_discovery_only_returns_metadata_and_does_not_write(self):
        body = copy.deepcopy(self.body)
        body['summary'] = 'PRIVATE_TASK_BODY_NOT_FOR_STARTUP'
        self.save(body)
        before = {p: p.read_bytes() for p in self.store.rglob('*') if p.is_file()}
        text = json.dumps(handoff.discover({'cwd': str(self.project), 'session_id': 'receiver'}, 'claude'))
        after = {p: p.read_bytes() for p in self.store.rglob('*') if p.is_file()}
        self.assertNotIn(body['summary'], text)
        self.assertIn(body['title'], text)
        self.assertEqual(before, after)

    def test_malformed_record_does_not_leak_unknown_fields(self):
        first = self.save()
        path = handoff.directory_for(self.context) / f'{first["id"]}.json'
        first['unexpected'] = 'unvalidated data'
        path.write_text(json.dumps(first))
        self.assertEqual(handoff.active_records(self.context, 'claude'), [])
        with self.assertRaises(ValueError):
            handoff.read(self.context, first['id'], 'claude')

    def test_sensitive_content_is_rejected_by_existing_filter(self):
        body = copy.deepcopy(self.body)
        body['summary'] = 'Bear' + 'er ' + 'synthetic-example'
        with self.assertRaises(ValueError):
            self.save(body)
        self.assertFalse(self.store.exists())

    def test_unknown_agent_is_rejected(self):
        with self.assertRaises(ValueError):
            handoff.save(self.context, 'other', 'claude', 'x-session', self.body)

    def test_unfinished_task_requires_next_action(self):
        body = copy.deepcopy(self.body)
        body['next_steps'] = []
        with self.assertRaises(ValueError):
            self.save(body)

    def test_real_git_subdirectory_and_linked_worktree_identity(self):
        # Only temporary repositories are created; project hooks are never executed.
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, capture_output=True)
        nested = self.project / 'nested'
        nested.mkdir()
        root_context = handoff.project_context(self.project)
        self.assertEqual(root_context['key'], handoff.project_context(nested)['key'])
        linked = self.base / 'linked'
        linked.mkdir()
        common = self.project / '.git'
        def git_result(directory, *args):
            outputs = {('rev-parse', '--show-toplevel'): str(linked),
                       ('rev-parse', '--git-common-dir'): str(common),
                       ('branch', '--show-current'): 'feature', ('rev-parse', 'HEAD'): ''}
            return outputs[args]
        with patch.object(handoff, 'git', side_effect=git_result):
            linked_context = handoff.project_context(linked)
        self.assertEqual(root_context['key'], linked_context['key'])
        self.assertNotEqual(root_context['checkout'], linked_context['checkout'])

    def test_cli_save_discover_read_receive(self):
        content = self.base / 'summary.json'
        content.write_text(json.dumps(self.body))
        def run(*args, data=''):
            output = io.StringIO()
            with patch('sys.argv', ['handoff.py', *args]), patch('sys.stdin', io.StringIO(data)), redirect_stdout(output):
                self.assertEqual(handoff.main(), 0)
            return json.loads(output.getvalue())
        saved = run('save', '--project', str(self.project), '--agent', 'codex', '--session', 'codex-session', '--content-file', str(content))
        discovered = run('discover', '--agent', 'claude', data=json.dumps({'cwd':str(self.project), 'session_id':'claude-session'}))
        self.assertIn(saved['id'], discovered['hookSpecificOutput']['additionalContext'])
        result = run('read', '--project', str(self.project), '--agent', 'claude', '--id', saved['id'])
        self.assertEqual(result['handoff']['content'], self.body)
        receipt = run('receive', '--project', str(self.project), '--agent', 'claude', '--session', 'claude-session', '--id', saved['id'])
        self.assertEqual(receipt['session'], 'claude-session')


if __name__ == '__main__':
    unittest.main()
