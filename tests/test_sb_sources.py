"""sb_sources: 세션 감지·자료 등록·분류(2단계)와 표본 판정(3단계). 격리 HOME·SB_HOME 에서만 돈다."""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import sb_sources  # noqa: E402

MARK = 'BODY-MARKER-7f3a'   # 대화 본문에만 넣는 표식 — 어떤 결과 파일에도 나오면 안 된다


def word(i: int) -> str:
    """숫자 없는 서로 다른 단어(제목 정규화가 숫자를 지우므로)."""
    s = ''
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(97 + r) + s
    return s


class Env(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='sb-sources-test-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.home = self.root / 'home'
        self.sbhome = self.home / '.secondbrain'
        self.claude = self.home / '.claude'
        self.sbhome.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'USERPROFILE': str(self.home), 'SB_HOME': str(self.sbhome),
            'CLAUDE_CONFIG_DIR': str(self.claude), 'CODEX_HOME': str(self.home / '.codex'),
            'XDG_DATA_HOME': str(self.home / '.local' / 'share'),
            'SB_CLAUDE_MEM_DIR': str(self.home / '.claude-mem'),
            'SB_PROJECT_ALIASES': str(self.sbhome / 'config' / 'project_aliases.json')})
        env.start()
        self.addCleanup(env.stop)

    def jsonl(self, p: Path, rows):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')

    def make_sessions(self):
        self.alpha = self.home / 'alpha'
        (self.alpha / '.git').mkdir(parents=True)
        self.beta = self.home / 'work' / 'beta'
        self.beta.mkdir(parents=True)
        for cwd in (self.alpha, self.beta):
            slug = self.claude / 'projects' / str(cwd).replace(os.sep, '-')
            for k in range(2):
                self.jsonl(slug / ('s%d.jsonl' % k), [
                    {'type': 'summary', 'summary': MARK},
                    {'type': 'user', 'cwd': str(cwd), 'message': {'content': MARK}, 'text': MARK}])
        codex = self.home / '.codex'
        self.jsonl(codex / 'sessions' / '2026' / '11' / '15' / 'rollout-a.jsonl', [
            {'type': 'session_meta', 'payload': {'cwd': str(self.alpha), 'base_instructions': MARK}},
            {'type': 'response_item', 'payload': {'content': MARK}}])
        (codex / 'sessions' / '2026' / '11' / '14').mkdir(parents=True)
        (codex / 'sessions' / '2026' / '11' / '14' / 'rollout-b.jsonl.zst').write_bytes(b'\x28\xb5\x2f\xfd' + MARK.encode())
        self.jsonl(codex / 'archived_sessions' / 'rollout-c.jsonl', [{'payload': {'cwd': str(self.beta), 'prompt': MARK}}])
        gem = self.home / '.gemini'
        (gem / 'tmp' / 'beta' / 'chats').mkdir(parents=True)
        (gem / 'tmp' / 'beta' / 'chats' / 'session-1.json').write_text(
            json.dumps({'messages': [{'content': MARK}]}), encoding='utf-8')
        (gem / 'projects.json').write_text(json.dumps({'projects': {str(self.beta): 'beta'}}), encoding='utf-8')
        self.jsonl(self.home / '.omo' / 'agent' / 'sessions' / '--alpha--' / '2026-11-01_x.jsonl', [
            {'type': 'session', 'cwd': str(self.alpha)}, {'type': 'message', 'content': MARK}])

    def make_folder(self) -> Path:
        d = self.home / 'docs'
        files = {
            'notes/강의안.md': 2048, 'notes/report.pdf': 4096, 'syntax.md': 1500,     # own_output (syntax ≠ tax)
            '2025 세금계산서.pdf': 2048, 'contract_final.docx': 3000, 'keys/.env': 50,  # sensitive
            'node_modules/pkg/index.md': 2048, 'setup.dmg': 10, '.DS_Store': 10,       # junk
            'photo.png': 5000, 'tiny.md': 100,                                          # unknown
        }
        for rel, size in files.items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'x' * size)
        return d


class DetectTest(Env):
    def test_sessions_counts_cwds_without_body(self):
        self.make_sessions()
        got = {s['kind']: s for s in sb_sources.detect_sessions()}
        self.assertEqual(set(got), set(sb_sources.SESSION_KINDS))
        c = got['claude_sessions']['summary']
        self.assertEqual((c['files'], c['cwd_count'], c['git_count']), (4, 2, 1))
        x = got['codex_sessions']['summary']
        self.assertEqual((x['files'], x['compressed'], x['cwd_count']), (3, 1, 2))
        g = got['gemini_sessions']['summary']
        self.assertEqual((g['files'], g['cwd_count']), (1, 1))
        o = got['omo_sessions']['summary']
        self.assertEqual((o['files'], o['cwd_count'], o['git_count']), (1, 1, 1))
        for s in got.values():
            self.assertTrue(s['location'].startswith('~'), s['location'])
            self.assertEqual((s['mode'], s['discovered_by']), ('index', 'auto'))
            self.assertEqual(len(s['summary']['period']), 2)
        dumped = json.dumps(list(got.values()), ensure_ascii=False)
        self.assertNotIn(MARK, dumped)
        self.assertNotIn(str(self.alpha), dumped)       # 작업 폴더 경로는 개수만 남긴다
        self.assertEqual(sb_sources.body_keys(list(got.values())), [])

    def test_explicit_home_and_empty_home(self):
        self.assertEqual(sb_sources.detect_sessions(), [])
        self.make_sessions()
        self.assertEqual(len(sb_sources.detect_sessions(home=str(self.home))), 4)
        other = self.root / 'other'
        other.mkdir()
        self.assertEqual(sb_sources.detect_sessions(home=str(other)), [])

    def test_head_cwd_ignores_body(self):
        p = self.root / 'h.jsonl'
        self.jsonl(p, [{'content': 'x "cwd": "/fake"'}, {'cwd': '/real/path'}])
        # 본문 문자열 안의 따옴표는 이스케이프돼 있어 진짜 cwd 키만 잡힌다
        self.assertEqual(sb_sources._head_cwd(p), '/real/path')


class RegisterTest(Env):
    def test_folder_classification(self):
        d = self.make_folder()
        src = sb_sources.register(str(d))
        self.assertEqual((src['kind'], src['mode'], src['discovered_by']), ('folder', 'index', 'user'))
        self.assertEqual(src['location'], '~' + os.sep + 'docs')
        sm = src['summary']
        self.assertEqual(sm['files'], 11)
        self.assertEqual(sm['sensitive_hint'], 3)
        self.assertEqual(sm['by_ext']['.md'], 4)
        c = sb_sources.classify(src)
        self.assertEqual({k: c[k] for k in ('own_output', 'sensitive', 'junk', 'unknown')},
                         {'own_output': 3, 'sensitive': 3, 'junk': 3, 'unknown': 2})
        self.assertEqual(c['intake']['decision'], 'index')
        self.assertEqual((c['intake']['index'], c['intake']['skip']), (3, 8))
        self.assertEqual(c['intake']['reasons'],
                         {'sensitive_name': 3, 'junk': 3, 'unknown_ext': 1, 'size_out_of_range': 1})
        self.assertNotIn('강의안', json.dumps(src, ensure_ascii=False))   # 파일 이름은 저장하지 않는다

    def test_meeting_link_missing(self):
        m = self.home / '회의록'
        m.mkdir()
        (m / 'a.txt').write_bytes(b'x' * 2000)
        self.assertEqual(sb_sources.register(str(m))['kind'], 'meeting')
        link = sb_sources.register('https://example.com/doc')
        self.assertEqual((link['kind'], link['mode']), ('link', 'live'))
        self.assertNotIn('summary', link)
        self.assertEqual(sb_sources.classify(link)['intake']['decision'], 'live')
        miss = sb_sources.register(str(self.home / 'nope'))
        self.assertTrue(miss['missing'])
        self.assertEqual(miss['mode'], 'skip')
        with self.assertRaises(ValueError):
            sb_sources.register('  ')

    def test_classify_file_rules(self):
        cf = sb_sources.classify_file
        self.assertEqual(cf('급여명세.md', 2000)[0], 'sensitive')
        self.assertEqual(cf('Invoice-2026.pdf', 2000)[0], 'sensitive')
        self.assertEqual(cf('syntax.md', 2000)[0], 'own_output')
        self.assertEqual(cf('big.pdf', 30 * 1024 * 1024), ('unknown', 'size_out_of_range'))
        self.assertEqual(cf('a.zip', 2000)[0], 'junk')
        self.assertEqual(cf('a.md', 2000, in_junk=True)[0], 'junk')
        self.assertEqual(cf('a.xyz', 2000), ('unknown', 'unknown_ext'))


class Stage2Test(Env):
    def test_stage2_saves_without_body(self):
        self.make_sessions()
        d = self.make_folder()
        res = sb_sources.stage2({'sources': [str(d), 'https://example.com/x']})
        self.assertEqual(res['verdict'], 'pass', res)
        self.assertEqual((res['stage'], res['name']), (2, 'register'))
        m = {x['name']: x for x in res['metrics']}
        self.assertEqual(m['sources']['value'], 6)
        self.assertEqual(m['session_files']['value'], 4 + 3 + 1 + 1)
        self.assertEqual(m['sensitive_hints']['value'], 3)
        self.assertEqual(m['body_fields_saved']['value'], 0)
        for x in res['metrics']:
            self.assertEqual(set(x), {'name', 'value', 'threshold', 'pass', 'evidence'})
        p = sb_sources.sources_path()
        text = p.read_text(encoding='utf-8')
        self.assertNotIn(MARK, text)
        for k in ('"text"', '"content"', '"prompt"'):
            self.assertNotIn(k, text)
        if os.name != 'nt':
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        doc = sb_sources.load()
        self.assertEqual(doc['version'], 1)
        self.assertEqual(len(doc['sources']), 6)
        # 다시 돌려도 같은 목록(같은 id, 중복 없음)
        sb_sources.stage2({'sources': [str(d)]})
        again = sb_sources.load()['sources']
        self.assertEqual(sorted(s['id'] for s in again), sorted(s['id'] for s in doc['sources']))

    def test_stage2_empty_and_missing(self):
        res = sb_sources.stage2({})
        self.assertEqual(res['verdict'], 'pass')
        self.assertEqual({x['name']: x['value'] for x in res['metrics']}['sources'], 0)
        res = sb_sources.stage2({'sources': [str(self.home / 'gone')]})
        self.assertEqual(res['verdict'], 'stop')
        self.assertTrue(res['hint'])

    def test_save_rejects_body_and_broken_file(self):
        with self.assertRaises(ValueError):
            sb_sources.save({'sources': [{'id': 'x', 'summary': {'content': 'hi'}}]})
        sb_sources.sources_path().write_text('{broken', encoding='utf-8')
        with self.assertRaises(SystemExit):
            sb_sources.load()

    def test_cli_add_list(self):
        d = self.make_folder()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(sb_sources.main(['add', str(d), '--json']), 0)
        self.assertEqual(json.loads(buf.getvalue())[0]['kind'], 'folder')
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(sb_sources.main(['list']), 0)
        self.assertIn('folder', buf.getvalue())
        # 사용자가 add 로 넣은 자료는 stage2 가 다시 잴 때도 남는다
        sb_sources.stage2({})
        self.assertEqual([s['kind'] for s in sb_sources.load()['sources']], ['folder'])


class Stage3Test(Env):
    def make_db(self, n=200, mismatch=False, noise_every=40, dup_every=50, plain=True):
        """관측 n건, 세션 10개(cwd=home/projK). decision 은 ok, '상태 확인' 제목은 소음, 같은 제목 짝은 중복."""
        db = self.home / '.claude-mem' / 'claude-mem.db'
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db))
        con.execute('CREATE TABLE sdk_sessions (memory_session_id TEXT, project TEXT, cwd TEXT)')
        con.execute('CREATE TABLE observations (id INTEGER PRIMARY KEY, memory_session_id TEXT, project TEXT, '
                    'type TEXT, title TEXT, text TEXT, narrative TEXT)')
        for k in range(10):
            cwd = self.home / ('proj%s' % word(k))
            cwd.mkdir(parents=True, exist_ok=True)
            con.execute('INSERT INTO sdk_sessions VALUES (?,?,?)',
                        ('s%d' % k, 'other' if mismatch else cwd.name, str(cwd)))
        for i in range(1, n + 1):
            k = i % 10
            project = 'other' if mismatch else 'proj%s' % word(k)
            if i % noise_every == 0:
                title, typ = '상태 확인 %s 대기 중' % word(i), 'discovery'
            elif i % dup_every == 0:
                title, typ = '공통 제목 반복', 'discovery'
            elif i % 3 == 0 or not plain:
                title, typ = '구조 결정 %s' % word(i), 'decision'
            else:
                title, typ = '모듈 %s 살펴봄' % word(i), 'discovery'
            con.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?)',
                        (i, 's%d' % k, project, typ, title, MARK, MARK))
        con.commit()
        con.close()
        return db

    def test_seed_reproducible_and_no_body(self):
        self.make_db()
        a = sb_sources.stage3(seed=7)
        pa = sb_sources.sample_path(7)
        sample_a = json.loads(pa.read_text(encoding='utf-8'))
        b = sb_sources.stage3(seed=7)
        sample_b = json.loads(pa.read_text(encoding='utf-8'))
        self.assertEqual(sample_a['ids'], sample_b['ids'])
        self.assertEqual(a['metrics'], b['metrics'])
        self.assertEqual(a['verdict'], b['verdict'])
        self.assertEqual(len(sample_a['ids']), 50)
        sb_sources.stage3(seed=8)
        self.assertNotEqual(json.loads(sb_sources.sample_path(8).read_text(encoding='utf-8'))['ids'], sample_a['ids'])
        text = pa.read_text(encoding='utf-8')
        self.assertNotIn(MARK, text)
        self.assertNotIn('살펴봄', text)            # 제목도 남기지 않는다
        self.assertEqual(set(sample_a), {'seed', 'n', 'ids', 'auto', 'judged', 'scope', 'db'})
        if os.name != 'nt':
            self.assertEqual(pa.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(sample_a['scope'].values()), {'match'})

    def test_pending_then_judge_passes(self):
        self.make_db()
        res = sb_sources.stage3(seed=3)
        self.assertEqual(res['verdict'], 'pending', res)
        self.assertIn('sb setup judge --stage 3', res['hint'])
        p = sb_sources.sample_path(3)
        sample = json.loads(p.read_text(encoding='utf-8'))
        todo = {i: 'ok' for i in sample['ids'] if sample['auto'][i] == 'unjudged'}
        self.assertTrue(todo)
        out = sb_sources.judge(str(p), todo)
        self.assertEqual(out['verdict'], 'pass', out)
        merged = json.loads(p.read_text(encoding='utf-8'))
        self.assertEqual(merged['judged'], todo)
        # 같은 seed 로 다시 돌려도 사람 판정은 남는다
        self.assertEqual(sb_sources.stage3(seed=3)['verdict'], 'pass')
        # 판정을 noise 로 바꾸면 문턱을 넘어 멈춘다
        out = sb_sources.judge(str(p), {i: 'noise' for i in list(todo)[:15]})
        self.assertEqual(out['verdict'], 'stop')

    def test_judge_rejects_bad_input(self):
        self.make_db()
        sb_sources.stage3(seed=1)
        p = str(sb_sources.sample_path(1))
        with self.assertRaises(ValueError):
            sb_sources.judge(p, {'999999': 'ok'})
        sid = json.loads(Path(p).read_text(encoding='utf-8'))['ids'][0]
        with self.assertRaises(ValueError):
            sb_sources.judge(p, {sid: 'maybe'})
        with self.assertRaises(ValueError):
            sb_sources.judge(str(self.root / 'none.json'), {})

    def test_dup_and_noise_detected(self):
        self.make_db(n=60, noise_every=2, dup_every=3, plain=False)
        res = sb_sources.stage3(seed=5)
        sample = json.loads(sb_sources.sample_path(5).read_text(encoding='utf-8'))
        vals = set(sample['auto'].values())
        self.assertIn('noise', vals)
        self.assertIn('dup', vals)
        self.assertEqual(res['verdict'], 'stop')

    def test_scope_mismatch_stops(self):
        self.make_db(mismatch=True, plain=False)
        res = sb_sources.stage3(seed=2)
        m = {x['name']: x for x in res['metrics']}
        self.assertEqual(m['scope_mismatch_ratio']['value'], 1.0)
        self.assertEqual(res['verdict'], 'stop')

    def test_no_db_or_small_db_passes(self):
        res = sb_sources.stage3()
        self.assertEqual(res['verdict'], 'pass')
        self.assertIn('데이터 부족', res['metrics'][0]['evidence'])
        self.make_db(n=30)
        res = sb_sources.stage3()
        self.assertEqual(res['verdict'], 'pass')
        self.assertIn('30', res['metrics'][0]['evidence'])

    def test_db_is_opened_read_only(self):
        db = self.make_db()
        before = db.read_bytes()
        sb_sources.stage3(seed=4)
        self.assertEqual(db.read_bytes(), before)


class HelperTest(unittest.TestCase):
    def test_heuristic(self):
        h = sb_sources.heuristic
        self.assertEqual(h('decision', '아무 제목'), 'ok')
        self.assertEqual(h('discovery', '워커 상태 확인'), 'noise')
        self.assertEqual(h('discovery', 'Waiting for build'), 'noise')
        self.assertEqual(h('discovery', '타임아웃 원인 해결'), 'ok')
        self.assertEqual(h('discovery', '모듈 구조'), 'unjudged')
        self.assertEqual(h(None, None), 'unjudged')

    def test_norm_title(self):
        self.assertEqual(sb_sources._norm_title(' Fix: 3 Bugs! '), sb_sources._norm_title('fix bugs'))

    def test_scope_match(self):
        home = os.path.expanduser('~')
        self.assertTrue(sb_sources.scope_match(os.path.join(home, 'alpha'), 'alpha'))
        self.assertTrue(sb_sources.scope_match(os.path.join(home, 'alpha'), '~' + os.sep + 'alpha'))
        self.assertTrue(sb_sources.scope_match('My Proj', 'my proj'))
        self.assertFalse(sb_sources.scope_match(os.path.join(home, 'alpha'), 'beta'))
        self.assertFalse(sb_sources.scope_match('', 'beta'))


if __name__ == '__main__':
    unittest.main()
