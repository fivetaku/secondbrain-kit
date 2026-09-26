"""V4 — sb_search --mode current/history/next/rules 계약: scope 선필터, legacy 호환, empty/degraded/unavailable/error 구분."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / 'bin'
sys.path.insert(0, str(BIN))
import sb_state  # noqa: E402

PY = sys.executable


class SearchModeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='sb-search-mode-'))
        self.home = self.tmp / 'home'; (self.home / 'work' / 'alpha').mkdir(parents=True)
        (self.home / 'work' / 'beta').mkdir(); (self.home / 'a' / 'shared').mkdir(parents=True); (self.home / 'b' / 'shared').mkdir(parents=True)
        self.aliases = self.tmp / 'aliases.json'
        self.aliases.write_text(json.dumps({'alpha': [str(self.home / 'work' / 'alpha')], 'beta': [str(self.home / 'work' / 'beta')],
                                            'shared-a': [str(self.home / 'a' / 'shared')], 'shared-b': [str(self.home / 'b' / 'shared')]}))
        self.state = self.tmp / 'state.db'; self.cm = self.tmp / 'claude-mem.db'
        self.loops = self.tmp / 'loops.jsonl'; self.rules = self.tmp / 'rules'; self.rules.mkdir()
        self.env = {**os.environ, 'HOME': str(self.home), 'USERPROFILE': str(self.home),
                    'SB_HOME': str(self.tmp / 'sb-home'), 'SB_CLAUDE_MEM_DIR': str(self.tmp / 'claude-mem-dir'),
                    'SB_PROJECT_ALIASES': str(self.aliases), 'SB_STATE_DB': str(self.state),
                    'SB_CLAUDE_MEM_DB': str(self.cm), 'SB_LOOPS_PATH': str(self.loops), 'SB_RULES_DIR': str(self.rules),
                    'SB_STATE_PILOT_SCOPES': 'alpha,beta,global', 'SB_VERIFY_ALLOWED_ROOTS': str(self.tmp), 'PYTHONDONTWRITEBYTECODE': '1'}
        env = patch.dict(os.environ, {k: self.env[k] for k in ('SB_HOME', 'SB_CLAUDE_MEM_DIR', 'SB_PROJECT_ALIASES', 'SB_STATE_DB',
                                                               'SB_CLAUDE_MEM_DB', 'SB_STATE_PILOT_SCOPES', 'SB_VERIFY_ALLOWED_ROOTS')})
        env.start()
        self.addCleanup(env.stop)
        self.loops.write_text('', encoding='utf-8')

    def build_claude_mem(self, observations):
        with sqlite3.connect(self.cm) as db:
            db.executescript('CREATE TABLE observations(id INTEGER PRIMARY KEY, memory_session_id TEXT, project TEXT NOT NULL, text TEXT, '
                             'type TEXT NOT NULL, title TEXT, subtitle TEXT, facts TEXT, narrative TEXT, created_at TEXT NOT NULL, '
                             'created_at_epoch INTEGER NOT NULL, metadata TEXT);'
                             'CREATE VIRTUAL TABLE observations_fts USING fts5(title, narrative, content=observations, content_rowid=id);')
            for i, (project, title, narrative) in enumerate(observations, 1):
                db.execute('INSERT INTO observations(id, memory_session_id, project, text, type, title, narrative, created_at, created_at_epoch, metadata) '
                           'VALUES (?,?,?,?,?,?,?,?,?,?)', (i, 's', project, narrative, 'discovery', title, narrative, '2026-09-10T00:00:00Z',
                                                          int(datetime(2026, 9, 10).timestamp() * 1000), '{}'))
            db.execute("INSERT INTO observations_fts(observations_fts) VALUES ('rebuild')")

    def put_head(self, scope, key, body):
        if not self.state.exists():
            sb_state.migrate(str(self.state))
        ev = self.tmp / ('ev-%s-%s.txt' % (scope, key)); ev.write_text('measured: %s\n' % body, encoding='utf-8')
        r = sb_state.propose({'scope_id': scope, 'fact_key': key, 'body': body, 'kind': 'measured_fact', 'source': 't', 'write_id': scope + key + body},
                             str(self.state))
        sb_state.verify(r['candidate_id'], 'file_contains', str(ev), db_path=str(self.state))
        with sqlite3.connect(self.state) as db:
            row = db.execute('SELECT version FROM state WHERE scope_id=? AND fact_key=? AND is_head=1', (scope, key)).fetchone()
        sb_state.accept(r['candidate_id'], row[0] if row else 0, str(self.state))

    def cli(self, *args, cwd=None):
        p = subprocess.run([PY, str(BIN / 'sb_search.py'), *args], capture_output=True, text=True, env=self.env,
                           cwd=str(cwd or self.home / 'work' / 'alpha'))
        try:
            j = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else None
        except ValueError:
            j = None
        return p.returncode, j, p


class CurrentModeTests(SearchModeBase):
    def test_current_returns_scope_head_only_with_verification_label(self):
        self.put_head('alpha', 'deploy.target', 'staging-2'); self.put_head('beta', 'deploy.target', 'prod-9')
        code, j, _ = self.cli('--mode', 'current', '--scope', 'alpha', '--fact-key', 'deploy.target', '--json')
        self.assertEqual((code, j['status'], j['query_mode']), (0, 'ok', 'current'))
        self.assertEqual([(i['scope_id'], i['body'], i['verification_status'], i['label']) for i in j['items']],
                         [('alpha', 'staging-2', 'verified', 'operationally measured')])

    def test_current_without_head_is_empty_and_never_promotes_history(self):
        sb_state.migrate(str(self.state))
        self.build_claude_mem([('alpha', '배포 대상', '예전 배포 대상은 legacy-1')])
        code, j, p = self.cli('--mode', 'current', '--scope', 'alpha', '--fact-key', 'deploy.target', '--json')
        self.assertEqual((code, j['status'], j['items']), (0, 'empty', []))
        self.assertNotIn('legacy-1', p.stdout)

    def test_scope_resolution_uses_alias_path_not_basename(self):
        self.put_head('alpha', 'k', 'alpha-value')
        code, j, _ = self.cli('--mode', 'current', '--json', cwd=self.home / 'work' / 'alpha')
        self.assertEqual(j['scope'], 'alpha'); self.assertEqual(j['items'][0]['body'], 'alpha-value')
        code, j, _ = self.cli('--mode', 'current', '--json', cwd=self.home / 'a' / 'shared')
        self.assertEqual((j['scope'], j['status']), ('shared-a', 'empty'))

    def test_global_is_labelled_and_separate(self):
        self.put_head('alpha', 'deploy.target', 'staging-2'); self.put_head('global', 'editor', 'vim')
        code, j, p = self.cli('--mode', 'current', '--scope', 'global', '--json')
        self.assertEqual((j['scope'], [i['scope_id'] for i in j['items']]), ('global', ['global']))
        self.assertNotIn('staging-2', p.stdout)

    def test_scopes_all_view_labels_each_item(self):
        self.put_head('alpha', 'k', 'a'); self.put_head('beta', 'k', 'b')
        code, j, _ = self.cli('--mode', 'current', '--scopes', 'alpha,beta', '--json')
        self.assertEqual(j['scope'], {'all': True, 'scopes': ['alpha', 'beta']})
        self.assertEqual(sorted(i['scope_id'] for i in j['items']), ['alpha', 'beta'])

    def test_empty_scope_is_error_exit_2_and_missing_db_is_unavailable_exit_8(self):
        code, j, _ = self.cli('--mode', 'current', '--scope', '', '--json')
        self.assertEqual((code, j['status'], j['warnings']), (2, 'error', ['scope_required']))
        code, j, _ = self.cli('--mode', 'current', '--scope', 'alpha', '--json')
        self.assertEqual((code, j['status']), (8, 'unavailable'))
        self.assertFalse(self.state.exists())

    def test_locked_db_is_degraded_not_empty(self):
        self.put_head('alpha', 'k', 'v')
        holder = sqlite3.connect(self.state, isolation_level=None); holder.execute('BEGIN EXCLUSIVE')
        try:
            code, j, _ = self.cli('--mode', 'current', '--scope', 'alpha', '--json')
        finally:
            holder.execute('ROLLBACK'); holder.close()
        self.assertIn(j['status'], ('degraded', 'ok')); self.assertNotEqual(j['status'], 'empty')


class HistoryModeTests(SearchModeBase):
    def test_l0_history_prefilters_scope_before_topk(self):
        # beta 에 같은 문구의 관측이 더 많고 점수가 높아도 alpha 결과에 섞이지 않는다
        obs = [('beta', '배포 대상 변경', '배포 대상 변경 배포 대상 변경 prod-9')] * 8 + [('alpha', '배포 대상 변경', '알파 배포 대상 staging-2')]
        self.build_claude_mem(obs)
        code, j, p = self.cli('--mode', 'history', '--scope', 'alpha', '--json', '--limit', '3', '배포 대상 변경')
        self.assertIsNotNone(j, 'no JSON; rc=%s stdout=%r stderr=%r' % (code, p.stdout[-500:], p.stderr[-1500:]))
        self.assertEqual((code, j['status']), (0, 'ok'))
        self.assertEqual({i['scope_id'] for i in j['items']}, {'alpha'})
        self.assertEqual(j['items'][0]['evidence_ref'], 'observation:9')
        self.assertNotIn('prod-9', p.stdout)

    def test_l2_history_with_fact_key_lists_versions(self):
        self.put_head('alpha', 'deploy.target', 'staging-1'); self.put_head('alpha', 'deploy.target', 'staging-2')
        code, j, _ = self.cli('--mode', 'history', '--scope', 'alpha', '--fact-key', 'deploy.target', '--json')
        self.assertEqual([(i['version'], i['is_head'], i['body']) for i in j['items']], [(1, False, 'staging-1'), (2, True, 'staging-2')])

    def test_missing_claude_mem_db_is_unavailable_not_empty(self):
        code, j, p = self.cli('--mode', 'history', '--scope', 'alpha', '--json', '아무거나')
        self.assertEqual((code, j['status']), (8, 'unavailable')); self.assertNotIn('Traceback', p.stderr)

    def test_history_without_query_is_error(self):
        self.build_claude_mem([('alpha', 't', 'n')])
        code, j, _ = self.cli('--mode', 'history', '--scope', 'alpha', '--json')
        self.assertEqual((code, j['status']), (2, 'error'))


class NextAndRulesTests(SearchModeBase):
    def write_loops(self, loops):
        self.loops.write_text(''.join(json.dumps(l, ensure_ascii=False) + '\n' for l in loops), encoding='utf-8')

    def test_next_filters_scope_and_flags_missing_action(self):
        self.write_loops([{'id': 'L1', 'title': 'a', 'project': 'alpha', 'status': 'open', 'value': 'high', 'next_action': 'do'},
                          {'id': 'L2', 'title': 'b', 'project': 'beta', 'status': 'open', 'next_action': 'x'},
                          {'id': 'L3', 'title': 'c', 'project': 'alpha', 'status': 'open'},
                          {'id': 'L4', 'title': 'd', 'project': 'alpha', 'status': 'done'},
                          {'id': 'L5', 'title': 'e', 'project': 'Alpha_', 'status': 'open'}])
        code, j, _ = self.cli('--mode', 'next', '--scope', 'alpha', '--json')
        self.assertEqual([i['loop_id'] for i in j['items']], ['L1', 'L3'])
        self.assertIn('missing_next_action', j['items'][1]['reason_codes'])

    def test_malformed_loops_line_is_degraded_not_crash(self):
        self.loops.write_text('{"id": "L1", "title": "a", "project": "alpha", "status": "open"}\n{broken\n', encoding='utf-8')
        code, j, p = self.cli('--mode', 'next', '--scope', 'alpha', '--json')
        self.assertEqual((code, j['status'], j['warnings']), (0, 'degraded', ['malformed_line:2']))
        self.assertEqual(len(j['items']), 1); self.assertNotIn('Traceback', p.stderr)

    def test_rules_are_scoped_and_paths_stay_inside_dir(self):
        (self.rules / 'alpha.md').write_text('- 알파 규칙: smoke 테스트\n', encoding='utf-8'); (self.rules / 'beta.md').write_text('- 베타 규칙: mock 금지\n', encoding='utf-8')
        code, j, p = self.cli('--mode', 'rules', '--scope', 'alpha', '--json')
        self.assertIn('smoke 테스트', j['items'][0]['body']); self.assertNotIn('mock 금지', p.stdout)
        code, j, _ = self.cli('--mode', 'rules', '--scope', '../alpha', '--json')
        self.assertIn(j['status'], ('empty', 'error')); self.assertEqual(j['items'], [])
        code, j, _ = self.cli('--mode', 'rules', '--scope', 'gamma', '--json')
        self.assertEqual(j['status'], 'empty')


class LegacyCompatibilityTests(SearchModeBase):
    def test_legacy_positional_query_output_unchanged(self):
        self.build_claude_mem([('alpha', '포트 변경', '8443 으로 변경')])
        code, j, p = self.cli('포트 변경', '--project', 'alpha', '--json', '--no-vector')
        self.assertEqual(code, 0)
        self.assertEqual(set(j), {'query', 'scope', 'results', 'meta'})
        self.assertEqual(j['results'][0]['obs_id'], 1)
        code, j, p = self.cli('포트 변경', '--project', 'alpha', '--no-vector')
        self.assertTrue(p.stdout.startswith('순위  점수'))

    def test_legacy_requires_query_and_unknown_mode_exits_2_without_traceback(self):
        code, j, p = self.cli('--project', 'alpha', '--json')
        self.assertEqual(code, 2); self.assertNotIn('Traceback', p.stderr)
        code, j, p = self.cli('--mode', 'bogus', '--scope', 'alpha', '--json')
        self.assertEqual(code, 2); self.assertNotIn('Traceback', p.stderr)


if __name__ == '__main__':
    unittest.main()
