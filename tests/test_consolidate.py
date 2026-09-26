"""Offline weekly consolidation contracts on Python 3.9 and temporary SQLite."""

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import consolidate as cons
import sb_config
import sb_memory as memory

PYTHON = sys.executable


class ConsolidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / 'observations?#.db')
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute('CREATE TABLE observations (id INTEGER, project TEXT, type TEXT, '
                       'title TEXT, narrative TEXT, facts TEXT, created_at TEXT, '
                       'created_at_epoch INTEGER, metadata TEXT)')
            for ident in range(1, 8):
                metadata = {'kind': 'consolidation'} if ident == 6 else (
                    {'source': 'wrap-backfill'} if ident == 7 else {})
                created = '2026-08-{:02d}T10:00:00Z'.format(23 + ident)
                db.execute('INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           (ident, 'demo', 'discovery', 'Title {}'.format(ident),
                            'n' * 700, json.dumps(list(range(12))), created,
                            int(datetime.fromisoformat(created.replace('Z', '+00:00')).timestamp() * 1000),
                            json.dumps(metadata)))
            db.commit()
        env = patch.dict(os.environ, {'SB_CLAUDE_MEM_DB': self.db,
                                      'SB_DAILY_DIR': self.temp.name,
                                      'SB_HOME': str(Path(self.temp.name) / 'sbhome'),
                                      'SB_CLAUDE_BIN': '/fake/bin/claude',
                                      'SB_LLM_MODEL': 'claude-sonnet-5',
                                      'SB_MEM_JOURNAL': str(Path(self.temp.name) / 'memory.jsonl'),
                                      'SB_MEM_BASE_URL': 'http://offline.invalid',
                                      'PYTHONDONTWRITEBYTECODE': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.output = {'summary': 'First sentence. Second sentence. Third sentence.',
                       'decisions': [{'text': 'Decided', 'source_ids': [1, 2]}],
                       'open_threads': [{'text': 'Pending', 'source_ids': [3]}],
                       'key_facts': [{'text': 'Known', 'source_ids': [4, 5]}]}
        self.llm = patch.object(cons, '_llm', return_value=json.dumps(self.output)).start()
        self.post = patch.object(memory, '_post', return_value=(200, '{"success":true,"id":42}')).start()
        self.saved = patch.object(memory, 'already_saved', return_value=False).start()
        patch.object(memory, 'journal').start()
        self.addCleanup(patch.stopall)

    def test_collect_excludes_derivatives_and_limits_content(self) -> None:
        before = Path(self.db).read_bytes()
        items = cons.collect_inputs('demo', '2026-W35', self.db)
        self.assertEqual([item['id'] for item in items], [1, 2, 3, 4, 5])
        self.assertEqual(set(items[0]), {'id', 'title', 'type', 'narrative', 'facts', 'created_at'})
        self.assertEqual(len(items[0]['narrative']), 300)
        self.assertEqual(items[0]['facts'], list(range(4)))
        self.assertEqual(len(cons.collect_inputs('demo', '2026-W35', self.db, 2)), 2)
        self.assertEqual(cons.collect_inputs('demo', '2026-W35', self.db, 0), [])
        self.assertEqual(cons.collect_inputs('demo', '2026-W35', self.db, -1), [])
        self.assertEqual(cons.collect_inputs('other', '2026-W35', self.db), [])
        self.assertEqual(Path(self.db).read_bytes(), before)

    def test_nested_kind_and_import_source_excluded(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute('UPDATE observations SET metadata=? WHERE id=1',
                       (json.dumps({'prov': {'kind': 'consolidation'}}),))
            db.execute('UPDATE observations SET metadata=? WHERE id=2',
                       (json.dumps({'source': 'legacy-import-tool'}),))
            db.execute('UPDATE observations SET facts=? WHERE id=3', ('broken',))
            db.commit()
        items = cons.collect_inputs('demo', '2026-W35', self.db)
        self.assertEqual([item['id'] for item in items], [3, 4, 5])
        self.assertEqual(items[0]['facts'], [])

    def test_iso_year_boundary_and_hash_order(self) -> None:
        self.assertEqual(cons.iso_week('2021-01-01T00:00:00Z'), '2020-W53')
        self.assertEqual(cons.iso_week('2021-01-04'), '2021-W01')
        with self.assertRaises(ValueError):
            cons.iso_week('invalid')
        items = [{'id': 2, 'title': 'B'}, {'id': 1, 'content_hash': 'abc', 'title': 'A'}]
        digest = cons.input_set_hash(items)
        self.assertRegex(digest, r'^[0-9a-f]{16}$')
        self.assertEqual(digest, cons.input_set_hash(list(reversed(items))))
        self.assertNotEqual(digest, cons.input_set_hash([{'id': 2, 'title': 'C'}, items[1]]))

    def test_prompt_and_json_extraction(self) -> None:
        prompt = cons.build_prompt('demo', '2026-W35', cons.collect_inputs('demo', '2026-W35', self.db))
        for token in ['#1', '#5', 'source_ids', 'summary', 'decisions', 'open_threads', 'key_facts']:
            self.assertIn(token, prompt)
        for wrapper in ['{}', '```json\n{}\n```', 'Result:\n{}\nEnd']:
            self.assertEqual(cons.parse_llm_output(wrapper.format(json.dumps(self.output))), self.output)
        for value in ['not JSON', '[]', '{}', '{"summary": 3}',
                      json.dumps(dict(self.output, decisions='bad')),
                      json.dumps(dict(self.output, decisions=[{'text': 'bad', 'source_ids': '1'}]))]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                cons.parse_llm_output(value)

    def test_save_provenance_and_duplicate(self) -> None:
        digest = cons.input_set_hash(cons.collect_inputs('demo', '2026-W35', self.db))
        before = Path(self.db).read_bytes()
        result = cons.consolidate('demo', '2026-W35', self.db)
        self.assertEqual(result['status'], 'saved')
        self.post.assert_called_once()
        payload = self.post.call_args.args[1]
        prov = payload['metadata']
        self.assertEqual(prov['sid'], 'consolidation:demo:2026-W35:1/1:' + digest)
        for field, value in {'source': 'secondbrain-consolidate', 'kind': 'consolidation',
                             'origin': 'consolidate', 'created_by': 'claude-sonnet-5',
                             'source_ids': [1, 2, 3, 4, 5], 'content_hash': digest,
                             'verification': 'unreviewed'}.items():
            self.assertEqual(prov[field], value)
        self.assertEqual(prov['extra']['week'], '2026-W35')
        self.assertEqual(prov['extra']['input_count'], 5)
        self.assertEqual(prov['extra']['prompt_version'], 'cons-2')
        self.assertTrue(payload['text'].startswith('[주간 통합 · project=demo · week=2026-W35 · inputs=5 · unreviewed]'))
        self.assertIn('(#1, #2)', payload['text'])
        self.assertEqual(payload['title'], '[통합] demo 2026-W35 — First sentence.')
        self.saved.return_value = True
        self.assertEqual(cons.consolidate('demo', '2026-W35', self.db)['status'], 'duplicate')
        self.post.assert_called_once()
        self.assertEqual(Path(self.db).read_bytes(), before)

    def test_invalid_refs_dropped_and_empty_not_saved(self) -> None:
        self.output['decisions'].append({'text': 'Invented', 'source_ids': [1, 999]})
        self.llm.return_value = json.dumps(self.output)
        self.assertEqual(cons.consolidate('demo', '2026-W35', self.db)['status'], 'saved')
        payload = self.post.call_args.args[1]
        self.assertEqual(payload['metadata']['extra']['dropped_refs'], 1)
        self.assertNotIn('Invented', payload['text'])
        self.output['decisions'] = [{'text': 'Invalid', 'source_ids': [999]}]
        self.output['key_facts'] = []
        self.llm.return_value = json.dumps(self.output)
        self.post.reset_mock()
        self.assertEqual(cons.consolidate('demo', '2026-W35', self.db)['status'], 'empty')
        self.post.assert_not_called()

    def test_parsing_retry_failure_and_recovery(self) -> None:
        self.llm.return_value = 'not json'
        self.assertEqual(cons.consolidate('demo', '2026-W35', self.db)['status'], 'failed')
        self.assertEqual(self.llm.call_count, 2)
        self.post.assert_not_called()
        self.llm.reset_mock()
        self.llm.side_effect = ['bad', json.dumps(self.output)]
        self.assertEqual(cons.consolidate('demo', '2026-W35', self.db)['status'], 'saved')
        self.assertEqual(self.llm.call_count, 2)

    def test_skip_and_dry_run_never_call_llm(self) -> None:
        self.assertEqual(cons.consolidate('demo', '2026-W35', self.db, min_items=6),
                         {'status': 'skipped', 'parts': [{'status': 'skipped', 'reason': 'too_few'}]})
        result = cons.consolidate('demo', '2026-W35', self.db, dry_run=True)
        self.assertEqual(result['parts'][0]['input_count'], 5)
        self.assertGreater(result['parts'][0]['prompt_length'], 0)
        self.llm.assert_not_called()
        self.post.assert_not_called()

    def test_targets_since_and_cli_failed_journal(self) -> None:
        now = datetime.now(timezone.utc)
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute('UPDATE observations SET created_at=?, created_at_epoch=?',
                       (now.isoformat(), int(now.timestamp() * 1000)))
            db.commit()
        self.assertEqual(cons.list_targets(self.db, 1), [('demo', cons.iso_week(now.isoformat()))])
        self.llm.return_value = 'bad'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cons.main(['run', '--since-days', '1', '--json']), 1)
        lines = (Path(self.temp.name) / 'consolidation_journal.jsonl').read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])['status'], 'failed')

    def test_save_failure_returns_failed(self) -> None:
        with patch.object(memory, 'save_memory', side_effect=RuntimeError('offline failure')):
            self.assertEqual(cons.consolidate('demo', '2026-W35', self.db)['status'], 'failed')
        self.post.assert_not_called()

    def test_llm_command_and_timeout_contract(self) -> None:
        with patch.object(cons.subprocess, 'run') as run:
            run.return_value.stdout = 'response'
            self.assertEqual(cons._call_llm('prompt'), 'response')
        self.assertEqual(run.call_args.args[0], [
            '/fake/bin/claude', '-p', 'prompt',
            '--model', 'claude-sonnet-5', '--output-format', 'text'])
        self.assertEqual(run.call_args.kwargs['timeout'], 600)
        self.assertTrue(run.call_args.kwargs['check'])
        self.assertIsNone(run.call_args.kwargs['input'])
        child_env = run.call_args.kwargs['env']
        self.assertEqual((child_env['SB_RECALL'], child_env['CLAUDE_MEM_INTERNAL']), ('0', '1'))
        self.assertEqual(child_env['SB_CLAUDE_MEM_DB'], self.db)  # rest of env inherited
        self.llm.side_effect = subprocess.TimeoutExpired('claude', 600)
        with patch.object(cons.time, 'sleep'):
            self.assertEqual(cons.consolidate('demo', '2026-W35', self.db)['status'], 'failed')
        self.post.assert_not_called()

    def test_llm_windows_sends_prompt_via_stdin(self) -> None:
        with patch.object(sb_config, 'IS_WINDOWS', True), \
                patch.object(cons.subprocess, 'run') as run:
            run.return_value.stdout = 'response'
            self.assertEqual(cons._call_llm('x' * 50000), 'response')
        command = run.call_args.args[0]
        self.assertEqual(command, ['/fake/bin/claude', '-p', '--model', 'claude-sonnet-5',
                                   '--output-format', 'text'])
        self.assertEqual(run.call_args.kwargs['input'], 'x' * 50000)
        self.assertEqual(run.call_args.kwargs['env']['SB_RECALL'], '0')

    def test_model_and_claude_bin_come_from_sb_config(self) -> None:
        with patch.dict(os.environ, {'SB_LLM_MODEL': 'opus', 'SB_CLAUDE_BIN': '/other/claude'}), \
                patch.object(cons.subprocess, 'run') as run:
            run.return_value.stdout = 'response'
            cons._call_llm('prompt')
            self.assertEqual(cons.consolidate('demo', '2026-W35', self.db)['status'], 'saved')
        self.assertEqual(run.call_args.args[0][0], '/other/claude')
        self.assertEqual(run.call_args.args[0][3:5], ['--model', 'opus'])
        self.assertEqual(self.post.call_args.args[1]['metadata']['created_by'], 'opus')

    def test_journal_defaults_to_sb_home_logs(self) -> None:
        os.environ.pop('SB_DAILY_DIR')
        with contextlib.redirect_stdout(io.StringIO()):
            code = cons.main(['run', '--project', 'demo', '--week', '2026-W35',
                              '--dry-run', '--json'])
        self.assertEqual(code, 0)
        journal = Path(self.temp.name) / 'sbhome' / 'logs' / 'consolidation_journal.jsonl'
        self.assertEqual(json.loads(journal.read_text(encoding='utf-8'))['status'], 'dry_run')

    def test_quota_failure_retries_then_recovers(self) -> None:
        error = subprocess.CalledProcessError(1, 'claude', output='API Error: Request rejected (429)')
        self.llm.side_effect = [error, error, json.dumps(self.output)]
        with patch.object(cons.time, 'sleep') as sleep:
            self.assertEqual(cons.consolidate('demo', '2026-W35', self.db)['status'], 'saved')
        self.assertEqual(self.llm.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [60.0, 120.0])

    def test_quota_failure_gives_up_after_bounded_attempts(self) -> None:
        self.llm.side_effect = subprocess.CalledProcessError(1, 'claude', output='429')
        with patch.object(cons.time, 'sleep'):
            result = cons.consolidate('demo', '2026-W35', self.db)
        self.assertEqual(result['parts'][0]['reason'], 'llm_error')
        self.assertEqual(self.llm.call_count, 3)
        self.post.assert_not_called()

    def test_optional_fallback_model_is_passed_through(self) -> None:
        with patch.dict(os.environ, {'SB_LLM_FALLBACK_MODEL': 'claude-opus-5'}), \
                patch.object(cons.subprocess, 'run') as run:
            run.return_value.stdout = 'response'
            cons._call_llm('prompt')
        self.assertEqual(run.call_args.args[0][-2:], ['--fallback-model', 'claude-opus-5'])

    def test_targets_exclude_old_and_derivative_only_projects(self) -> None:
        now = datetime.now(timezone.utc)
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute('UPDATE observations SET created_at=?, created_at_epoch=? WHERE id>=6',
                       (now.isoformat(), int(now.timestamp() * 1000)))
            db.execute("UPDATE observations SET project='derived-only' WHERE id>=6")
            db.commit()
        self.assertEqual(cons.list_targets(self.db, 1), [])

    def test_prompt_cap_splits_dates_without_losing_inputs(self) -> None:
        items = [dict(id=i + 1, title='Item', type='discovery', narrative='n' * 300,
                      facts=['fact'] * 4, created_at='2026-08-{:02d}T10:00:00Z'.format(24 + i // 5))
                 for i in range(30)]
        cap = len(cons.build_prompt('demo', '2026-W35', items[:5])) + 20
        with patch.object(cons, 'collect_inputs', return_value=list(reversed(items))):
            result = cons.consolidate('demo', '2026-W35', dry_run=True, max_prompt_chars=cap)
        self.assertEqual(result['status'], 'dry_run')
        parts = result['parts']
        self.assertGreaterEqual(len(parts), 2)
        self.assertEqual(sum(part['input_count'] for part in parts), 30)
        self.assertEqual(len({part['sid'] for part in parts}), len(parts))
        for index, part in enumerate(parts, 1):
            self.assertLessEqual(part['prompt_length'], cap)
            self.assertEqual(part['extra']['part'], {'index': index, 'total': len(parts)})
            self.assertEqual(part['extra']['truncated_count'], 0)
            self.assertIn(':2026-W35:{}/{}:'.format(index, len(parts)), part['sid'])
        self.llm.assert_not_called()
        self.post.assert_not_called()

    def test_single_day_cap_trims_oldest_and_records_provenance(self) -> None:
        items = [dict(id=i + 1, title='Item', type='discovery', narrative='n' * 300,
                      facts=[], created_at='2026-08-24T{:02d}:00:00Z'.format(i))
                 for i in range(10)]
        self.output['decisions'] = [{'text': 'Newest', 'source_ids': [10]}]
        self.output['key_facts'] = []
        self.llm.return_value = json.dumps(self.output)
        cap = len(cons.build_prompt('demo', '2026-W35', items[-3:]))
        with patch.object(cons, 'collect_inputs', return_value=list(reversed(items))):
            result = cons.consolidate('demo', '2026-W35', max_prompt_chars=cap)
        self.assertEqual(result['status'], 'saved')
        self.assertEqual(len(result['parts']), 1)
        part = result['parts'][0]
        self.assertEqual(part['input_count'], 3)
        self.assertLessEqual(part['prompt_length'], cap)
        self.assertEqual(part['extra']['truncated_count'], 7)
        self.assertEqual(part['extra']['date_range'], {'start': '2026-08-24', 'end': '2026-08-24'})
        self.assertEqual(part['extra']['part'], {'index': 1, 'total': 1})
        provenance = self.post.call_args.args[1]['metadata']
        self.assertEqual(provenance['source_ids'], [8, 9, 10])
        self.assertEqual(provenance['extra'], part['extra'])
        self.assertEqual(len(self.llm.call_args.args[0]), cap)

    def test_default_item_cap_preserves_full_week_across_parts(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            for ident in range(8, 133):
                day = '2026-08-24' if ident < 70 else '2026-08-25'
                db.execute('INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           (ident, 'demo', 'discovery', 'Item', 'n', '[]',
                            day + 'T10:00:00Z', 0, '{}'))
            db.commit()
        self.assertEqual(len(cons.collect_inputs('demo', '2026-W35', self.db)), 120)
        result = cons.consolidate('demo', '2026-W35', self.db, dry_run=True)
        self.assertGreater(len(result['parts']), 1)
        self.assertEqual(sum(part['input_count'] for part in result['parts']), 130)
        self.assertTrue(all(part['input_count'] <= 120 for part in result['parts']))
        self.llm.assert_not_called()

    def test_item_cap_on_single_day_keeps_latest_items(self) -> None:
        items = [dict(id=i, title='Item', type='discovery', narrative='', facts=[],
                      created_at='2026-08-24T{:02d}:00:00Z'.format(i)) for i in range(6)]
        with patch.object(cons, 'collect_inputs', return_value=items):
            result = cons.consolidate('demo', '2026-W35', dry_run=True, max_items=2)
        part = result['parts'][0]
        self.assertEqual(part['input_count'], 2)
        self.assertEqual(part['extra']['truncated_count'], 4)
        self.assertTrue(part['sid'].endswith(cons.input_set_hash(items[-2:])))

    def test_prompt_environment_override_and_impossible_limit(self) -> None:
        items = cons.collect_inputs('demo', '2026-W35', self.db)
        cap = len(cons.build_prompt('demo', '2026-W35', items[:1]))
        with patch.dict(os.environ, {'SB_CONS_MAX_PROMPT': str(cap)}):
            result = cons.consolidate('demo', '2026-W35', self.db, dry_run=True)
            self.assertEqual(len(result['parts']), 5)
            explicit = cons.consolidate('demo', '2026-W35', self.db, dry_run=True,
                                        max_prompt_chars=90000)
            self.assertEqual(len(explicit['parts']), 1)
        result = cons.consolidate('demo', '2026-W35', self.db, max_prompt_chars=1)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(sum(part['extra']['truncated_count'] for part in result['parts']), 5)
        for part in result['parts']:
            self.assertEqual(part['reason'], 'prompt_limit')
            self.assertEqual(part['input_count'], 0)
            self.assertLessEqual(part['prompt_length'], 1)
        self.llm.assert_not_called()
        self.post.assert_not_called()

    def test_invalid_limits_and_empty_zero_minimum(self) -> None:
        for limits in [{'max_items': 0}, {'max_items': -1},
                       {'max_prompt_chars': 0}, {'max_prompt_chars': -1}]:
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                cons.consolidate('demo', '2026-W35', self.db, **limits)
        with patch.dict(os.environ, {'SB_CONS_MAX_PROMPT': 'invalid'}), self.assertRaises(ValueError):
            cons.consolidate('demo', '2026-W35', self.db)
        result = cons.consolidate('other', '2026-W35', self.db, min_items=0)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(len(result['parts']), 1)
        self.llm.assert_not_called()

    def test_mixed_part_status_aggregation(self) -> None:
        for statuses, expected in [(['saved', 'duplicate', 'saved'], 'saved'),
                                   (['saved', 'failed', 'saved'], 'failed'),
                                   (['saved', 'empty', 'saved'], 'partial')]:
            with self.subTest(statuses=statuses), patch.object(
                    cons, '_consolidate_part', side_effect=[{'status': status} for status in statuses]):
                result = cons.consolidate('demo', '2026-W35', self.db, max_items=2)
            self.assertEqual(result['status'], expected)
            self.assertEqual([part['status'] for part in result['parts']], statuses)

    def test_real_cli_multipart_journal_matches_output(self) -> None:
        items = cons.collect_inputs('demo', '2026-W35', self.db)
        cap = len(cons.build_prompt('demo', '2026-W35', items[:1]))
        script = str(Path(__file__).resolve().parents[1] / 'bin' / 'consolidate.py')
        with patch.dict(os.environ, {'SB_CONS_MAX_PROMPT': str(cap)}):
            result = subprocess.run([PYTHON, '-B', script, 'run', '--project', 'demo',
                                     '--week', '2026-W35', '--dry-run', '--json'],
                                    capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        journal = Path(self.temp.name) / 'consolidation_journal.jsonl'
        self.assertEqual(events, [json.loads(line) for line in journal.read_text().splitlines()])
        self.assertEqual(len(events), 5)
        self.assertEqual(len({event['sid'] for event in events}), 5)
        self.assertEqual(sum(event['input_count'] for event in events), 5)
        self.assertTrue(all(event['prompt_length'] <= cap for event in events))

    def test_real_cli_dry_run_help_and_bad_argument(self) -> None:
        script = str(Path(__file__).resolve().parents[1] / 'bin' / 'consolidate.py')
        before = Path(self.db).read_bytes()
        run = subprocess.run([PYTHON, '-B', script, 'run', '--project', 'demo',
                              '--week', '2026-W35', '--dry-run', '--json'],
                             capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        event = json.loads(run.stdout)
        self.assertEqual(event['input_count'], 5)
        self.assertEqual(event['status'], 'dry_run')
        journal = Path(self.temp.name) / 'consolidation_journal.jsonl'
        self.assertEqual(json.loads(journal.read_text()), event)
        targets = subprocess.run([PYTHON, '-B', script, 'targets', '--json',
                                  '--since-days', '36500'],
                                 capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(targets.returncode, 0, targets.stderr)
        self.assertEqual(json.loads(targets.stdout), [['demo', '2026-W35']])
        self.assertEqual(Path(self.db).read_bytes(), before)
        help_result = subprocess.run([PYTHON, '-B', script, '--help'],
                                     capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(help_result.returncode, 0)
        bad = subprocess.run([PYTHON, '-B', script, 'run', '--unknown'],
                             capture_output=True, text=True, timeout=10, check=False)
        self.assertNotEqual(bad.returncode, 0)


if __name__ == '__main__':
    unittest.main()
