"""sb_vault: 기록 분석 → 구성안 → 적용·되돌리기. 격리 HOME·SB_HOME 에서만 돈다."""

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import sb_vault  # noqa: E402


def sha(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


class VaultEnv(unittest.TestCase):
    """가짜 홈: 프로젝트 3개(git 1·폴더 2), 홈·임시 세션, 자동 메모리, claude-mem DB."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='sb-vault-test-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.home = self.root / 'home'
        self.sbhome = self.home / '.secondbrain'
        self.claude = self.home / '.claude'
        for d in (self.sbhome / 'config', self.claude / 'projects'):
            d.mkdir(parents=True)
        self.alpha = self.home / 'alpha'          # git 저장소, 하위 폴더 세션 포함
        (self.alpha / 'sub').mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(self.alpha)], check=True)
        self.beta = self.home / 'code' / 'beta'   # git 아님
        self.beta.mkdir(parents=True)
        self.small = self.home / 'small'          # 관측이 적어 scope 로 제안하지 않는다
        self.small.mkdir()
        env = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'USERPROFILE': str(self.home),   # 윈도우 expanduser 는 HOME 이 아니라 USERPROFILE 을 본다
            'SB_HOME': str(self.sbhome), 'CLAUDE_CONFIG_DIR': str(self.claude),
            'SB_CLAUDE_MEM_DIR': str(self.home / '.claude-mem'), 'SB_VAULT_LLM': 'off',
            'SB_PROJECT_ALIASES': str(self.sbhome / 'config' / 'project_aliases.json')})
        env.start()
        self.addCleanup(env.stop)
        # 가짜 홈이 임시 폴더 아래라 그 슬러그가 '임시 폴더 세션'으로 걸러지지 않게 한다(실제 홈은 임시 폴더 밖)
        real_is_temp = sb_vault.sync_automemory.is_temp_slug
        home_slug = str(self.home).replace(os.sep, '-')
        tslug = mock.patch.object(sb_vault.sync_automemory, 'is_temp_slug',
                                  side_effect=lambda s: False if s.startswith(home_slug) else real_is_temp(s))
        tslug.start()
        self.addCleanup(tslug.stop)
        for k in ('SB_STATE_PILOT_SCOPES', 'SB_LOOPS_PATH', 'SB_VAULT_LLM_URL', 'SB_CORRECTION_LLM_URL'):
            os.environ.pop(k, None)
        self.db = self.home / '.claude-mem' / 'claude-mem.db'
        self.db.parent.mkdir()
        now = int(time.time() * 1000)
        old = now - 90 * 86400 * 1000
        con = sqlite3.connect(self.db)
        con.execute('CREATE TABLE sdk_sessions (memory_session_id TEXT, project TEXT, cwd TEXT, started_at_epoch INTEGER)')
        con.execute('CREATE TABLE observations (memory_session_id TEXT, project TEXT, type TEXT, created_at_epoch INTEGER)')
        sessions = [('a1', 'alpha', self.alpha, 60, now), ('a2', 'alpha', self.alpha / 'sub', 30, now),
                    ('b1', 'beta', self.beta, 40, old), ('h1', 'home', self.home, 500, now),
                    ('t1', 'trigger', Path('/tmp/sbvault-test-trigger'), 300, now), ('s1', 'small', self.small, 2, now)]
        for sid, tag, cwd, n, t in sessions:
            con.execute('INSERT INTO sdk_sessions VALUES (?,?,?,?)', (sid, tag, str(cwd), t))
            con.executemany('INSERT INTO observations VALUES (?,?,?,?)',
                            [(sid, tag, 'discovery' if i % 3 else 'decision', t) for i in range(n)])
        con.commit()
        con.close()
        # 자동 메모리: alpha 에 feedback 1개
        slug = self.claude / 'projects' / str(self.alpha).replace(os.sep, '-')
        (slug / 'memory').mkdir(parents=True)
        (slug / 'memory' / 'fb.md').write_text('---\nname: x\nmetadata:\n  type: feedback\n---\nbody\n', encoding='utf-8')
        (slug / 's.jsonl').write_text(json.dumps({'cwd': str(self.alpha)}) + '\n', encoding='utf-8')

    def analysis(self):
        return sb_vault.analyze(str(self.db), str(self.claude / 'projects'))


class AnalyzeTests(VaultEnv):
    def test_candidates_and_exclusions(self):
        a = self.analysis()
        keys = {p['suggested_key']: p for p in a['projects']}
        self.assertIn('alpha', keys)
        self.assertEqual(keys['alpha']['obs_count'], 90)          # 하위 폴더 세션이 git 뿌리로 묶인다
        self.assertIn('git', keys['alpha']['signals'])
        self.assertEqual(keys['alpha']['feedback'], 1)
        self.assertIn('beta', keys)
        self.assertNotIn('home', keys)
        classes = {e['class'] for e in a['excluded']}
        self.assertIn('home', classes)
        self.assertIn('temp', classes)
        self.assertEqual(a['data_volume'], 'thin')                 # 작업 폴더 2개 < 3
        self.assertFalse(a['llm']['used'])
        self.assertEqual(a['kinds']['discovery'] + a['kinds']['decision'], 932)

    def test_empty_volume_without_data(self):
        a = sb_vault.analyze(str(self.root / 'none.db'), str(self.root / 'none'))
        self.assertEqual(a['data_volume'], 'empty')
        self.assertFalse(a['sources'][0]['readable'])

    def test_no_network_without_llm(self):
        # LLM 미설정·off 이면 분석 중 소켓 연결이 한 번도 없어야 한다(개인 데이터 외부 전송 0)
        calls = []
        real = socket.socket.connect

        def guard(sock, addr):
            calls.append(addr)
            return real(sock, addr)
        with mock.patch.object(socket.socket, 'connect', guard):
            self.analysis()
            os.environ['SB_VAULT_LLM'] = 'on'   # 켜도 볼트 전용 경로가 없으면 부르지 않는다
            self.analysis()
            os.environ.pop('SB_VAULT_LLM')     # 다른 기능의 LLM 설정만 있으면 기본(끔)으로 부르지 않는다
            with mock.patch.dict(os.environ, {'SB_CORRECTION_LLM_URL': 'http://127.0.0.1:9/x', 'SB_CORRECTION_MODEL': 'm'}):
                self.analysis()
        self.assertEqual(calls, [])

    def test_llm_receives_folder_parts_only(self):
        sent = []

        class Resp:
            def __init__(self, body): self.body = body
            def read(self): return self.body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, timeout=0):
            sent.append(json.loads(req.data.decode()))
            return Resp(json.dumps({'choices': [{'message': {'content': '[{"i":0,"name":"renamed"}]'}}]}).encode())
        gen = self.home / 'src'
        gen.mkdir()
        con = sqlite3.connect(self.db)
        con.execute('INSERT INTO sdk_sessions VALUES (?,?,?,?)', ('g1', 'src', str(gen), int(time.time() * 1000)))
        con.executemany('INSERT INTO observations VALUES (?,?,?,?)', [('g1', 'src', 'discovery', 1)] * 30)
        con.commit(); con.close()
        with mock.patch.dict(os.environ, {'SB_VAULT_LLM': 'on', 'SB_VAULT_LLM_URL': 'http://127.0.0.1:9/v1/chat/completions',
                                          'SB_VAULT_MODEL': 'm'}), \
                mock.patch.object(sb_vault.urllib.request, 'urlopen', fake):
            a = self.analysis()
        self.assertEqual(len(sent), 1)
        prompt = sent[0]['messages'][0]['content']
        self.assertNotIn(str(self.home), prompt)                   # 전체 경로·대화 내용은 보내지 않는다
        self.assertIn('renamed', {p['suggested_key'] for p in a['projects']})
        self.assertTrue(a['llm']['used'])


class PlanApplyTests(VaultEnv):
    def plan(self, mode='all'):
        return sb_vault.make_plan(self.analysis(), None, mode)

    def test_plan_filters_small_and_marks_activity(self):
        p = self.plan()
        keys = {s['key']: s for s in p['scopes']}
        self.assertNotIn('small', keys)
        self.assertIn('small', p['minor_folders'])
        self.assertTrue(keys['alpha']['state_write'])
        self.assertFalse(keys['beta']['state_write'])               # 90일 전 — 최근 활동 없음
        self.assertTrue(keys['alpha']['rules'])
        self.assertIsNone(p['state_env'])                           # all 모드는 env 를 바꾸지 않는다
        self.assertEqual(p['plan_hash'], sb_vault.plan_hash(p))
        self.assertIn('alpha', sb_vault.render_plan(p))

    def test_listed_mode_generates_env(self):
        p = self.plan('listed')
        self.assertEqual(p['state_env'], 'alpha')

    def test_existing_env_conflict_is_not_overwritten(self):
        settings = self.claude / 'settings.json'
        settings.write_text(json.dumps({'env': {'SB_STATE_PILOT_SCOPES': 'other'}}), encoding='utf-8')
        p = self.plan('listed')
        self.assertIsNone(p['state_env'])
        self.assertTrue(any(c['key'] == 'SB_STATE_PILOT_SCOPES' for c in p['conflicts']))
        before = sha(settings)
        rec = sb_vault.apply_plan(p, p['plan_hash'])
        self.assertTrue(rec['verify']['ok'])
        self.assertEqual(sha(settings), before)

    def test_existing_alias_key_is_not_reused(self):
        # 기존 별칭 'beta' 가 다른 경로를 가리키면 새 후보는 다른 이름으로 — 기존 별칭은 그대로
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text(json.dumps({'beta': [str(self.home / 'elsewhere')]}), encoding='utf-8')
        p = self.plan()
        self.assertNotIn('beta', p['aliases'])
        self.assertIn('code-beta', {sc['key'] for sc in p['scopes']})   # 상위 폴더 이름을 붙여 겹치지 않게
        rec = sb_vault.apply_plan(p, p['plan_hash'])
        self.assertTrue(rec['verify']['ok'])
        self.assertEqual(json.loads(ap.read_text())['beta'], [str(self.home / 'elsewhere')])

    def test_name_alias_is_recognized(self):
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text(json.dumps({'client-work': ['beta']}), encoding='utf-8')   # 경로 없는 이름 별칭
        p = self.plan()
        keys = {sc['key']: sc for sc in p['scopes']}
        self.assertIn('client-work', keys)
        self.assertNotIn('beta', p['aliases'])                     # 새 경로 별칭으로 scope 를 바꾸지 않는다

    def test_broken_settings_aborts_before_any_write(self):
        settings = self.claude / 'settings.json'
        settings.write_text('{"env": ', encoding='utf-8')          # 잘린 JSON
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text('{}\n', encoding='utf-8')
        before = (sha(settings), sha(ap))
        p = self.plan('listed')
        with self.assertRaises(SystemExit):
            sb_vault.apply_plan(p, p['plan_hash'])
        self.assertEqual((sha(settings), sha(ap)), before)
        self.assertFalse(sb_vault.vault_json_path().exists())

    def test_short_hash_rejected(self):
        p = self.plan()
        with self.assertRaises(SystemExit):
            sb_vault.apply_plan(p, p['plan_hash'][:4])

    def test_concurrent_apply_is_locked(self):
        p = self.plan()
        with sb_vault._Lock():
            with self.assertRaises(SystemExit):
                sb_vault.apply_plan(p, p['plan_hash'])

    def test_rollback_rejects_foreign_record(self):
        p = self.plan()
        rec = sb_vault.apply_plan(p, p['plan_hash'])
        data = json.loads(Path(rec['apply_path']).read_text())
        data['backups'].append({'name': 'evil', 'target': str(self.home / 'victim.txt'), 'backup_path': None,
                                'sha256_before': None})
        Path(rec['apply_path']).write_text(json.dumps(data), encoding='utf-8')
        (self.home / 'victim.txt').write_text('keep', encoding='utf-8')
        with self.assertRaises(SystemExit):
            sb_vault.rollback(Path(rec['apply_path']))
        self.assertEqual((self.home / 'victim.txt').read_text(), 'keep')

    def test_record_write_failure_rolls_back(self):
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text('{}\n', encoding='utf-8')
        before = sha(ap)
        p = self.plan()
        real = sb_vault._atomic_write
        calls = {'n': 0}

        def flaky(path, data, private=False):
            if str(path).endswith('.json') and '/vault/apply-' in str(path):
                calls['n'] += 1
                if calls['n'] == 2:      # 첫 기록(write-ahead)은 되고, 결과 기록에서 디스크가 찬다
                    raise OSError(28, 'No space left on device')
            return real(path, data, private)
        with mock.patch.object(sb_vault, '_atomic_write', flaky):
            rec = sb_vault.apply_plan(p, p['plan_hash'])
        self.assertFalse(rec['verify']['ok'])
        self.assertTrue(rec['rolled_back'])
        self.assertEqual(sha(ap), before)

    def test_same_suggested_key_gets_unique_suffix(self):
        con = sqlite3.connect(self.db)
        now = int(time.time() * 1000)
        for i, root in enumerate((self.home / 'one' / 'a' / 'src', self.home / 'two' / 'a' / 'src')):
            root.mkdir(parents=True)
            con.execute('INSERT INTO sdk_sessions VALUES (?,?,?,?)', ('k%d' % i, 'src', str(root), now))
            con.executemany('INSERT INTO observations VALUES (?,?,?,?)', [('k%d' % i, 'src', 'discovery', now)] * 25)
        con.commit(); con.close()
        p = sb_vault.make_plan(self.analysis(), None, 'all')
        keys = [sc['key'] for sc in p['scopes']]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn('a-src', keys)
        self.assertIn('a-src-2', keys)

    def test_rollback_rejects_aliases_entry_pointing_elsewhere(self):
        # M5 리뷰 P0-2: 기록의 'aliases' 대상이 설정된 별칭 파일이 아니면 거부
        p = self.plan()
        rec = sb_vault.apply_plan(p, p['plan_hash'])
        data = json.loads(Path(rec['apply_path']).read_text())
        victim = self.home / 'important.txt'
        victim.write_text('keep', encoding='utf-8')
        for b in data['backups']:
            if b['name'] == 'aliases':
                b['target'] = str(victim)
        Path(rec['apply_path']).write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(SystemExit):
            sb_vault.rollback(Path(rec['apply_path']))
        self.assertEqual(victim.read_text(), 'keep')

    def test_plan_with_other_aliases_path_is_refused(self):
        p = self.plan()
        p['aliases_path'] = str(self.home / 'elsewhere.json')
        p['plan_hash'] = sb_vault.plan_hash(p)
        with self.assertRaises(SystemExit):
            sb_vault.apply_plan(p, p['plan_hash'])
        self.assertFalse((self.home / 'elsewhere.json').exists())

    def test_broken_vault_json_aborts(self):
        sb_vault.vault_json_path().write_text('{"version": ', encoding='utf-8')
        before = sha(sb_vault.vault_json_path())
        p = self.plan()
        with self.assertRaises(SystemExit):
            sb_vault.apply_plan(p, p['plan_hash'])
        self.assertEqual(sha(sb_vault.vault_json_path()), before)

    def test_crash_mid_apply_is_recoverable_from_journal(self):
        # M5 리뷰 P0-3: 쓰는 도중 프로세스가 죽어도 기록의 예정 해시로 되돌린다
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text('{}\n', encoding='utf-8')
        before = sha(ap)
        p = self.plan()
        real = sb_vault._atomic_write
        hits = {'n': 0}

        def die_on_vault_json(path, data, private=False, expect_sha=sb_vault._KEEP):
            if Path(path).name == 'vault.json':
                hits['n'] += 1
                raise SystemExit('simulated crash')   # 예외 처리기도 못 도는 죽음 흉내
            return real(path, data, private, expect_sha)
        with mock.patch.object(sb_vault, '_atomic_write', die_on_vault_json):
            with self.assertRaises(SystemExit):
                sb_vault.apply_plan(p, p['plan_hash'])
        with sb_vault._Lock():   # with 블록이 OS 잠금을 풀었다(kill -9 이면 OS 가 푼다) — 바로 다시 잡힌다
            pass
        self.assertNotEqual(sha(ap), before)            # 별칭은 이미 바뀐 상태
        rec = sb_vault.rollback(sb_vault.latest('apply'))
        self.assertTrue(rec['rollback']['ok'], rec['rollback'])
        self.assertEqual(sha(ap), before)

    def test_change_between_check_and_replace_is_detected(self):
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text('{}\n', encoding='utf-8')
        p = self.plan()
        real_sha = sb_vault._sha
        state = {'armed': False}

        def racing_sha(path):
            if state['armed'] and Path(path) == sb_vault._real(ap):
                ap.write_text('{"other": ["/z"]}\n', encoding='utf-8')   # 다른 프로세스가 끼어든다
                state['armed'] = False
            return real_sha(path)
        real_write = sb_vault._atomic_write

        def arm(path, data, private=False, expect_sha=sb_vault._KEEP):
            if Path(path) == sb_vault._real(ap):
                state['armed'] = True
            return real_write(path, data, private, expect_sha)
        with mock.patch.object(sb_vault, '_sha', racing_sha), mock.patch.object(sb_vault, '_atomic_write', arm):
            rec = sb_vault.apply_plan(p, p['plan_hash'])
        self.assertFalse(rec['verify']['ok'])
        self.assertIn('other', ap.read_text())            # 끼어든 변경을 덮지 않았다

    def test_partial_rollback_can_be_retried(self):
        (self.sbhome / 'config' / 'project_aliases.json').write_text('{}\n', encoding='utf-8')   # 백업이 있어야 복원 쓰기가 일어난다
        p = self.plan()
        rec = sb_vault.apply_plan(p, p['plan_hash'])
        real = sb_vault._atomic_write
        state = {'fail': True}

        def flaky(path, data, private=False, expect_sha=sb_vault._KEEP):
            if state['fail'] and Path(path).name == 'project_aliases.json':
                raise OSError(13, 'Permission denied')
            return real(path, data, private, expect_sha)
        if True:
            with mock.patch.object(sb_vault, '_atomic_write', flaky):
                r1 = sb_vault.rollback(Path(rec['apply_path']))
            self.assertFalse(r1['rollback']['ok'])
            self.assertFalse(r1['rolled_back'])                 # 다시 시도할 수 있다
            r2 = sb_vault.rollback(Path(rec['apply_path']), force=True)
            self.assertTrue(r2['rollback']['ok'])
            self.assertEqual(len(r2['rollback_attempts']), 2)

    def test_home_cache_is_temp(self):
        self.assertEqual(sb_vault.classify_path(str(self.home / '.cache' / 'x')), 'temp')
        self.assertEqual(sb_vault.classify_path(str(self.home / 'tmp' / 'job')), 'temp')

    def test_key_collision_uses_sb_scope_name_rule(self):
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text(json.dumps({'Beta': [str(self.home / 'elsewhere')]}), encoding='utf-8')   # 대소문자만 다름
        p = self.plan()
        keys = {sc['key'] for sc in p['scopes']}
        self.assertNotIn('beta', keys)

    def test_new_files_are_private(self):
        p = self.plan()
        sb_vault.apply_plan(p, p['plan_hash'])
        if os.name != 'nt':   # 윈도우는 chmod 권한 비트가 없다
            self.assertEqual(sb_vault.vault_json_path().stat().st_mode & 0o777, 0o600)

    def test_non_git_nested_folders_share_root(self):
        ws = self.home / 'ws'
        (ws / 'sub').mkdir(parents=True)
        con = sqlite3.connect(self.db)
        now = int(time.time() * 1000)
        for sid, cwd in (('w1', ws), ('w2', ws / 'sub')):
            con.execute('INSERT INTO sdk_sessions VALUES (?,?,?,?)', (sid, 'ws', str(cwd), now))
            con.executemany('INSERT INTO observations VALUES (?,?,?,?)', [(sid, 'ws', 'discovery', now)] * 25)
        con.commit(); con.close()
        keys = {pp['suggested_key']: pp for pp in self.analysis()['projects']}
        self.assertIn('ws', keys)
        self.assertNotIn('sub', keys)
        self.assertEqual(keys['ws']['obs_count'], 50)

    def test_apply_then_rollback_restores_bytes(self):
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text('{"keep": ["/x"]}\n', encoding='utf-8')
        settings = self.claude / 'settings.json'
        settings.write_text('{"env": {"A": "1"}}\n', encoding='utf-8')
        targets = [ap, sb_vault.vault_json_path(), sb_vault.loops_path(), settings]
        before = {str(t): sha(t) for t in targets}
        p = self.plan('listed')
        rec = sb_vault.apply_plan(p, p['plan_hash'][:12])
        self.assertTrue(rec['verify']['ok'], rec['verify'])
        self.assertEqual(json.loads(settings.read_text())['env']['SB_STATE_PILOT_SCOPES'], 'alpha')
        self.assertEqual(json.loads(settings.read_text())['env']['A'], '1')
        self.assertIn('keep', json.loads(ap.read_text()))
        self.assertTrue(sb_vault.vault_json_path().exists())
        rb = sb_vault.rollback(Path(rec['apply_path']))
        self.assertTrue(rb['rollback']['ok'])
        self.assertEqual({str(t): sha(t) for t in targets}, before)

    def test_symlinked_settings_stays_a_link(self):
        real = self.root / 'dotfiles' / 'settings.json'
        real.parent.mkdir()
        real.write_text('{"env": {}}\n', encoding='utf-8')
        link = self.claude / 'settings.json'
        try:
            link.symlink_to(real)
        except OSError:   # 윈도우 일반 사용자는 심볼릭 링크 권한이 없다(WinError 1314)
            self.skipTest('심볼릭 링크를 만들 수 없는 환경')
        p = self.plan('listed')
        rec = sb_vault.apply_plan(p, p['plan_hash'])
        self.assertTrue(rec['verify']['ok'])
        self.assertTrue(link.is_symlink())
        self.assertEqual(json.loads(real.read_text())['env']['SB_STATE_PILOT_SCOPES'], 'alpha')
        sb_vault.rollback(Path(rec['apply_path']))
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), '{"env": {}}\n')

    def test_rollback_refuses_after_user_edit(self):
        p = self.plan()
        rec = sb_vault.apply_plan(p, p['plan_hash'])
        ap = Path(p['aliases_path'])
        ap.write_text('{"mine": ["/y"]}\n', encoding='utf-8')   # 적용 뒤 사용자가 고침
        with self.assertRaises(SystemExit):
            sb_vault.rollback(Path(rec['apply_path']))
        self.assertIn('mine', ap.read_text())
        sb_vault.rollback(Path(rec['apply_path']), force=True)
        with self.assertRaises(SystemExit):                       # 두 번 되돌리지 않는다
            sb_vault.rollback(Path(rec['apply_path']))

    def test_failed_verify_rolls_back_automatically(self):
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text('{}\n', encoding='utf-8')
        before = sha(ap)
        p = self.plan()
        rec = sb_vault.apply_plan(p, p['plan_hash'], _fail_after_write=True)
        self.assertFalse(rec['verify']['ok'])
        self.assertTrue(rec['rolled_back'])
        self.assertEqual(sha(ap), before)
        self.assertFalse(sb_vault.vault_json_path().exists())

    def test_hash_guard(self):
        p = self.plan()
        with self.assertRaises(SystemExit):
            sb_vault.apply_plan(p, 'deadbeef')
        p['scopes'].append({'key': 'sneaky'})
        with self.assertRaises(SystemExit):
            sb_vault.apply_plan(p, p['plan_hash'])

    def test_vault_json_drives_state_env(self):
        p = self.plan('listed')
        sb_vault.apply_plan(p, p['plan_hash'])
        v = json.loads(sb_vault.vault_json_path().read_text())
        listed = sorted(k for k, s in v['scopes'].items() if s['state_write'])
        env = json.loads((self.claude / 'settings.json').read_text())['env']['SB_STATE_PILOT_SCOPES']
        self.assertEqual(env, ','.join(listed))

    def test_cli_roundtrip(self):
        self.assertEqual(sb_vault.main(['status']), 0)
        with mock.patch.object(sb_vault, 'analyze', return_value=self.analysis()):
            self.assertEqual(sb_vault.main(['analyze']), 0)
        self.assertEqual(sb_vault.main(['plan']), 0)
        plan = json.loads(sb_vault.latest('plan').read_text())
        self.assertEqual(sb_vault.main(['apply', '--yes', '--hash', plan['plan_hash'][:12]]), 0)
        self.assertTrue(sb_vault.status()['configured'])
        self.assertEqual(sb_vault.main(['rollback']), 0)
        self.assertFalse(sb_vault.status()['configured'])
        with mock.patch('builtins.input', return_value='no'):
            self.assertEqual(sb_vault.main(['apply']), 1)


class OnboardTests(VaultEnv):
    def test_init_default_is_idempotent(self):
        r = sb_vault.init_default()
        self.assertEqual(len(r['created']), 2)
        v = json.loads(sb_vault.vault_json_path().read_text())
        self.assertTrue(v['default'])
        self.assertEqual(v['kind_policy'], {'default': 'normal'})
        self.assertIn('~', v['scope_excluded'])
        before = sha(sb_vault.vault_json_path())
        self.assertEqual(sb_vault.init_default()['created'], [])
        self.assertEqual(sha(sb_vault.vault_json_path()), before)

    def _add_ids(self, n):
        con = sqlite3.connect(self.db)
        cols = [r[1] for r in con.execute('PRAGMA table_info(observations)')]
        if 'id' not in cols:   # 실제 claude-mem 표처럼 id 열을 둔다
            con.execute('ALTER TABLE observations ADD COLUMN id INTEGER')
            con.execute('UPDATE observations SET id = rowid')
        con.execute('UPDATE observations SET id = rowid + ?', (n,))
        con.commit(); con.close()

    def test_nudge_default_vault_with_enough_records(self):
        sb_vault.init_default()
        self._add_ids(0)          # 관측 932건 ≥ 500
        line = sb_vault.nudge_line('2026-11-15', str(self.db))
        self.assertIn('sb vault analyze', line)
        self.assertEqual(sb_vault.nudge_line('2026-11-15', str(self.db)), '')   # 하루 1회
        self.assertIn('sb vault', sb_vault.nudge_line('2026-11-16', str(self.db)))
        with mock.patch.dict(os.environ, {'SB_VAULT_NUDGE': '0'}):
            self.assertEqual(sb_vault.nudge_line('2026-11-17', str(self.db)), '')

    def test_nudge_quiet_after_configuring_until_growth(self):
        self._add_ids(0)
        p = sb_vault.make_plan(self.analysis(), None, 'all')
        sb_vault.apply_plan(p, p['plan_hash'])
        self.assertEqual(json.loads(sb_vault.vault_json_path().read_text())['obs_seen'], 932)
        self.assertEqual(sb_vault.nudge_line('2026-11-15', str(self.db)), '')   # 막 구성했다
        self._add_ids(5000)       # max id 5932 ≥ 932*1.5 그리고 +2000 이상
        self.assertIn('늘었다', sb_vault.nudge_line('2026-11-16', str(self.db)))

    def test_nudge_silent_without_db(self):
        self.assertEqual(sb_vault.nudge_line('2026-11-15', str(self.root / 'none.db')), '')


class InstallerOnboardTests(VaultEnv):
    def _inst(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('kit_install_vault', BIN.parent / 'installer' / 'install.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _args(self, **kw):
        import argparse
        return argparse.Namespace(**dict({'no_vault_llm': True}, **kw))

    def test_thin_data_makes_default_vault_only(self):
        inst = self._inst()
        calls = []

        def fake_run(cmd, check=True, capture=False, env=None, cwd=None):
            calls.append(cmd[2])
            out = json.dumps({'data_volume': 'thin'}) if cmd[2] == 'analyze' else ''
            return subprocess.CompletedProcess(cmd, 0, out, '')
        with mock.patch.object(inst, 'run', fake_run), mock.patch.object(inst, 'say'):
            inst.vault_onboarding(self._args())
        self.assertEqual(calls, ['init', 'analyze'])          # 기록이 적으면 구성안을 만들지 않는다
        self.assertNotIn('apply', calls)

    def test_enough_data_makes_plan_but_never_applies(self):
        inst = self._inst()
        calls, envs = [], []

        def fake_run(cmd, check=True, capture=False, env=None, cwd=None):
            calls.append(cmd[2]); envs.append(env or {})
            out = json.dumps({'data_volume': 'enough'}) if cmd[2] == 'analyze' else '구성안'
            return subprocess.CompletedProcess(cmd, 0, out, '')
        with mock.patch.object(inst, 'run', fake_run), mock.patch.object(inst, 'say') as say:
            inst.vault_onboarding(self._args())
        self.assertEqual(calls, ['init', 'analyze', 'plan'])
        self.assertEqual(envs[1].get('SB_VAULT_LLM'), 'off')
        self.assertTrue(any('sb vault apply' in str(c) for c in say.call_args_list))

    def test_plan_failure_is_warning_without_apply_hint(self):
        inst = self._inst()

        def fake_run(cmd, check=True, capture=False, env=None, cwd=None):
            if cmd[2] == 'analyze':
                return subprocess.CompletedProcess(cmd, 0, json.dumps({'data_volume': 'enough'}), '')
            if cmd[2] == 'plan':
                return subprocess.CompletedProcess(cmd, 1, '', 'boom')
            return subprocess.CompletedProcess(cmd, 0, '', '')
        with mock.patch.object(inst, 'run', fake_run), mock.patch.object(inst, 'say') as say, \
                mock.patch.object(inst, 'WARNINGS', []) as w:
            inst.vault_onboarding(self._args())
        self.assertEqual(len(w), 1)
        self.assertFalse(any('sb vault apply' in str(c) for c in say.call_args_list))

    def test_analyze_failure_is_warning(self):
        inst = self._inst()
        with mock.patch.object(inst, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'boom')), \
                mock.patch.object(inst, 'say'), mock.patch.object(inst, 'WARNINGS', []) as w:
            inst.vault_onboarding(self._args())
        self.assertEqual(len(w), 1)

    def test_real_end_to_end_on_isolated_home(self):
        # 실제 sb_vault.py 를 자식 프로세스로: 빈 SB_HOME → init·analyze(→ thin) → 기본 볼트
        inst = self._inst()
        inst.SB_HOME = self.sbhome
        inst.VPY = Path(sys.executable)
        with mock.patch.object(inst, 'say'):
            inst.vault_onboarding(self._args())
        v = json.loads(sb_vault.vault_json_path().read_text())
        self.assertTrue(v['default'])
        self.assertTrue(list(sb_vault.vault_dir().glob('analysis-*.json')))


class Round3Tests(VaultEnv):
    """2026-11-15 GPT Pro 3차 리뷰 지적."""

    def plan(self, mode='all'):
        return sb_vault.make_plan(self.analysis(), None, mode)

    def test_duplicate_real_targets_rejected_before_write(self):
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text('{}\n', encoding='utf-8')
        before = ap.read_bytes()
        p = self.plan()
        with mock.patch.dict(os.environ, {'SB_LOOPS_PATH': str(ap)}):
            with self.assertRaises(SystemExit):
                sb_vault.apply_plan(p, p['plan_hash'])
        self.assertEqual(ap.read_bytes(), before)
        self.assertFalse(sb_vault.vault_json_path().exists())

    def test_crlf_backup_restored_byte_for_byte(self):
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_bytes(b'{\r\n "x": ["~/x"]\r\n}\r\n')
        before = ap.read_bytes()
        p = self.plan()
        rec = sb_vault.apply_plan(p, p['plan_hash'])
        self.assertTrue(rec['verify']['ok'], rec['verify'])
        self.assertTrue(any(c['check'].startswith('hash ') for c in rec['verify']['checks']))
        sb_vault.rollback(Path(rec['apply_path']))
        self.assertEqual(ap.read_bytes(), before)

    def _applied(self):
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text('{}\n', encoding='utf-8')
        p = self.plan()
        rec = sb_vault.apply_plan(p, p['plan_hash'])
        return ap, Path(rec['apply_path'])

    def test_record_forged_new_file_entry_keeps_content(self):
        # 기록을 '새로 만든 파일'로 꾸며도 내용은 지워지지 않고 백업 폴더로 옮겨진다
        ap, path = self._applied()
        content = ap.read_bytes()
        data = json.loads(path.read_text())
        for b in data['backups']:
            if b['name'] == 'aliases':
                b['backup_path'] = None
                b['sha256_before'] = None
        path.write_text(json.dumps(data))
        sb_vault.rollback(path)
        kept = list((sb_vault.vault_dir() / 'backup' / data['id']).glob('removed-aliases-*'))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), content)

    def test_record_backup_hash_mismatch_rejected(self):
        ap, path = self._applied()
        data = json.loads(path.read_text())
        bk = next(b for b in data['backups'] if b['name'] == 'aliases')
        Path(bk['backup_path']).write_text('{"tampered": []}\n', encoding='utf-8')
        with self.assertRaises(SystemExit):
            sb_vault.rollback(path)

    def test_record_backup_from_other_apply_rejected(self):
        ap, path = self._applied()
        data = json.loads(path.read_text())
        other = sb_vault.vault_dir() / 'backup' / 'other-id'
        other.mkdir(parents=True)
        bk = next(b for b in data['backups'] if b['name'] == 'aliases')
        dst = other / 'aliases.bak'
        dst.write_bytes(Path(bk['backup_path']).read_bytes())
        bk['backup_path'] = str(dst)
        path.write_text(json.dumps(data))
        with self.assertRaises(SystemExit):
            sb_vault.rollback(path)

    def test_malformed_writes_is_clean_error(self):
        ap, path = self._applied()
        data = json.loads(path.read_text())
        data['writes'] = [{'target': 1}]
        path.write_text(json.dumps(data))
        with self.assertRaises(SystemExit):
            sb_vault.rollback(path)

    def test_lock_survives_long_holder(self):
        # 잠금은 나이가 아니라 OS 잠금으로 판단한다 — 오래된 잠금 파일이 있어도 잡은 쪽이 살아 있으면 못 잡는다
        with sb_vault._Lock() as lk:
            old = time.time() - 3600
            os.utime(str(lk.path), (old, old))
            with self.assertRaises(SystemExit):
                with sb_vault._Lock():
                    pass

    def test_existing_alias_norm_duplicate_blocks_apply(self):
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text(json.dumps({'My_Proj': ['~/a'], 'my-proj': ['~/b']}), encoding='utf-8')
        p = self.plan()
        self.assertTrue(any(c.get('blocking') for c in p['conflicts']))
        with self.assertRaises(SystemExit):
            sb_vault.apply_plan(p, p['plan_hash'])

    def test_init_default_files_are_private(self):
        sb_vault.init_default()
        if os.name != 'nt':
            self.assertEqual(sb_vault.vault_json_path().stat().st_mode & 0o777, 0o600)
            self.assertEqual(sb_vault.loops_path().stat().st_mode & 0o777, 0o600)

    def test_symlinked_automation_root_is_automation(self):
        real = self.root / 'dotfiles-claude'
        (real / 'x').mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(real)], check=True)
        link = self.home / '.codex'
        try:
            link.symlink_to(real)
        except OSError:   # 윈도우 일반 사용자는 심볼릭 링크 권한이 없다(WinError 1314)
            self.skipTest('심볼릭 링크를 만들 수 없는 환경')
        self.assertEqual(sb_vault.classify_path(str(link / 'x')), 'automation')


if __name__ == '__main__':
    unittest.main()
