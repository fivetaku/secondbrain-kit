"""sb_setup: 다섯 단계 엔진·첫 실행 질문·설치기 12단계. 격리 HOME·SB_HOME 에서만 돈다.
1단계(doctor)와 2·3단계(sb_sources, 다른 작업자 몫)는 가짜로 바꿔 끼운다 — 실제 doctor 는 uvx·네트워크를 쓴다."""

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / 'bin'
sys.path.insert(0, str(BIN))
import sb_setup  # noqa: E402
import sb_vault  # noqa: E402

REAL_SOURCES_MOD = sb_setup._sources_mod

OK_ROWS = [('✅', 'venv', 'ok'), ('⚠', 'claude-mem worker', '응답 없음')]
BAD_ROWS = [('✅', 'venv', 'ok'), ('❌', 'claude-mem 설정', '없음')]


class FakeSources:
    """sb_sources 흉내 — 2·3단계 판정과 judge."""

    def __init__(self, v3='pass'):
        self.v3 = v3
        self.judged = None

    def stage2(self, answers):
        return {'stage': 2, 'name': 'register', 'verdict': 'pass', 'hint': '',
                'metrics': [{'name': 'sources', 'value': len(answers.get('sources') or []), 'threshold': '',
                             'pass': True, 'evidence': ''}]}

    def stage3(self, seed=None, n=50, db=None):
        return {'stage': 3, 'name': 'classify', 'verdict': self.v3, 'hint': 'sb setup judge --stage 3' if self.v3 == 'pending' else '',
                'metrics': [{'name': 'seed', 'value': seed, 'threshold': '', 'pass': None, 'evidence': ''}]}

    def judge(self, sample_path, judgments):
        self.judged = (sample_path, dict(judgments))
        return {'stage': 3, 'name': 'classify', 'verdict': 'pass', 'hint': '',
                'metrics': [{'name': 'judged', 'value': len(judgments), 'threshold': '', 'pass': True, 'evidence': ''}]}


class SetupEnv(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='sb-setup-test-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.home = self.root / 'home'
        self.sbhome = self.home / '.secondbrain'
        (self.sbhome / 'config').mkdir(parents=True)
        self.env = {'HOME': str(self.home), 'SB_HOME': str(self.sbhome), 'CLAUDE_CONFIG_DIR': str(self.home / '.claude'),
                    'SB_CLAUDE_MEM_DIR': str(self.home / '.claude-mem'), 'SB_VAULT_LLM': 'off',
                    'SB_PROJECT_ALIASES': str(self.sbhome / 'config' / 'project_aliases.json')}
        p = mock.patch.dict(os.environ, self.env)
        p.start()
        self.addCleanup(p.stop)
        for k in ('CI', 'SB_NO_SETUP', 'SB_STATE_PILOT_SCOPES', 'SB_LOOPS_PATH', 'SB_VAULT_LLM_URL') + sb_setup.AGENT_ENV:
            os.environ.pop(k, None)
        self.rows = list(OK_ROWS)
        self.src = FakeSources()
        for target, kw in (('_doctor_rows', {'side_effect': lambda: list(self.rows)}),
                           ('_sources_mod', {'side_effect': lambda: self.src})):
            m = mock.patch.object(sb_setup, target, **kw)
            m.start()
            self.addCleanup(m.stop)

    def make_db(self):
        """프로젝트 두 개(관측 각 40건)가 쌓인 claude-mem DB — 4단계 구성안이 생긴다."""
        alpha, beta = self.home / 'alpha', self.home / 'code' / 'beta'
        alpha.mkdir(parents=True)
        beta.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(alpha)], check=True)
        db = self.home / '.claude-mem' / 'claude-mem.db'
        db.parent.mkdir(parents=True)
        now = int(time.time() * 1000)
        con = sqlite3.connect(str(db))
        con.execute('CREATE TABLE sdk_sessions (memory_session_id TEXT, project TEXT, cwd TEXT, started_at_epoch INTEGER)')
        con.execute('CREATE TABLE observations (id INTEGER PRIMARY KEY, memory_session_id TEXT, project TEXT, type TEXT, '
                    'created_at_epoch INTEGER)')
        for sid, tag, cwd in (('a1', 'alpha', alpha), ('b1', 'beta', beta)):
            con.execute('INSERT INTO sdk_sessions VALUES (?,?,?,?)', (sid, tag, str(cwd), now))
            con.executemany('INSERT INTO observations (memory_session_id, project, type, created_at_epoch) VALUES (?,?,?,?)',
                            [(sid, tag, 'discovery', now) for _ in range(40)])
        con.commit()
        con.close()

    def doc(self):
        return json.loads(sb_setup.setup_path().read_text(encoding='utf-8'))


class EngineTests(SetupEnv):
    def test_runs_in_order_and_stops_at_plan_approval(self):
        self.make_db()
        s = sb_setup.run(non_interactive=True)
        self.assertEqual([r['stage'] for r in s['results']], [1, 2, 3, 4])
        self.assertEqual((s['stopped_at'], s['verdict']), (4, 'pending'))
        self.assertIn('sb setup approve', s['hint'])
        self.assertEqual(s['current_stage'], 4)
        d = self.doc()
        self.assertEqual([r['verdict'] for r in d['stage_runs']], ['pass', 'pass', 'pass', 'pending'])
        self.assertTrue(d['plan_hash'])
        for r in d['stage_runs']:   # 판정 기록 파일 0600
            p = Path(r['metrics_ref'])
            self.assertTrue(p.name.startswith('setup-%d-' % r['stage']))
            if os.name != 'nt':
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        if os.name != 'nt':
            self.assertEqual(sb_setup.setup_path().stat().st_mode & 0o777, 0o600)

    def test_two_runs_same_fingerprint(self):
        self.make_db()
        a = sb_setup.run(non_interactive=True)
        b = sb_setup.run(non_interactive=True)
        self.assertEqual(a['fingerprint'], b['fingerprint'])
        self.assertEqual(a['plan_hash'], b['plan_hash'])
        # 기록을 치우고 처음부터 다시 해도(같은 입력) 같다
        sb_setup.setup_path().rename(self.root / 'setup.old.json')
        c = sb_setup.run(non_interactive=True)
        self.assertEqual(a['fingerprint'], c['fingerprint'])

    def test_fingerprint_ignores_times_but_not_verdicts(self):
        doc = {'stage_runs': [{'stage': 1, 'verdict': 'pass', 'started_at': 'x', 'metrics': [{'name': 'a', 'value': 1}]}],
               'plan_hash': 'h'}
        f = sb_setup.fingerprint(doc)
        doc['stage_runs'][0]['started_at'] = 'y'
        self.assertEqual(sb_setup.fingerprint(doc), f)
        doc['stage_runs'][0]['verdict'] = 'accepted'
        self.assertNotEqual(sb_setup.fingerprint(doc), f)

    def test_stop_then_accept_with_reason(self):
        self.rows = list(BAD_ROWS)
        s = sb_setup.run(non_interactive=True)
        self.assertEqual((s['stopped_at'], s['verdict']), (1, 'stop'))
        self.assertIn('claude-mem 설정', s['hint'])
        self.assertEqual(s['current_stage'], 1)
        with self.assertRaises(ValueError):
            sb_setup.accept(1, '')
        with self.assertRaises(ValueError):
            sb_setup.accept(1, '   ')
        self.assertEqual(self.doc()['stage_runs'][-1]['verdict'], 'stop')   # 빈 이유는 아무것도 기록하지 않는다
        sb_setup.accept(1, 'claude-mem 은 나중에 설치')
        last = self.doc()['stage_runs'][-1]
        self.assertEqual((last['stage'], last['verdict'], last['reason']), (1, 'accepted', 'claude-mem 은 나중에 설치'))
        self.assertTrue(Path(last['metrics_ref']).is_file())
        self.assertEqual(self.doc()['current_stage'], 2)
        s = sb_setup.run(non_interactive=True)     # 넘긴 단계 다음부터 이어서
        self.assertEqual(s['results'][0]['stage'], 2)

    def test_accept_refuses_passed_or_unrun_stage(self):
        with self.assertRaises(ValueError):
            sb_setup.accept(1, 'x')          # 돌린 적 없음
        sb_setup.run(only=1, non_interactive=True)
        with self.assertRaises(ValueError):
            sb_setup.accept(1, 'x')          # pass 라 넘길 것이 없음

    def test_cannot_skip_ahead(self):
        with self.assertRaises(SystemExit):
            sb_setup.run(from_stage=3, non_interactive=True)

    def test_rerun_earlier_stage_that_stops_rewinds(self):
        self.make_db()
        sb_setup.run(non_interactive=True)
        self.rows = list(BAD_ROWS)
        s = sb_setup.run(from_stage=1, non_interactive=True)
        self.assertEqual((s['stopped_at'], s['current_stage']), (1, 1))

    def test_stage4_pending_then_approve_then_pass(self):
        self.make_db()
        s = sb_setup.run(non_interactive=True)
        h = s['plan_hash']
        with self.assertRaises(ValueError):
            sb_setup.approve(h[:8])            # 12자 미만
        with self.assertRaises(ValueError):
            sb_setup.approve('0' * 12 if not h.startswith('0' * 12) else 'f' * 12)
        sb_setup.approve(h[:12])
        self.assertEqual(self.doc()['answers']['approved_plan_hash'], h[:12])
        s = sb_setup.run(non_interactive=True)
        r4 = s['results'][0]
        self.assertEqual((r4['stage'], r4['verdict']), (4, 'pass'))
        v = json.loads(sb_vault.vault_json_path().read_text(encoding='utf-8'))
        self.assertEqual(v['plan_hash'], h)
        self.assertEqual((s['stopped_at'], s['verdict']), (5, 'pending'))   # 검증 데이터 대기
        self.assertIn('sb setup --stage 5', s['hint'])

    def test_approve_without_plan_is_refused(self):
        with self.assertRaises(ValueError):
            sb_setup.approve('a' * 12)

    def test_stage4_thin_data_passes_with_default_vault(self):
        s = sb_setup.run(non_interactive=True)      # DB 없음 → 구성할 프로젝트 없음
        r4 = [r for r in s['results'] if r['stage'] == 4][0]
        self.assertEqual(r4['verdict'], 'pass')
        self.assertTrue(json.loads(sb_vault.vault_json_path().read_text(encoding='utf-8'))['default'])

    def test_stage5_reads_injection_log(self):
        for st in (1, 2, 3, 4):
            sb_setup._record(sb_setup.load(), sb_setup.result(st, 'pass', []), 'x')
        doc = sb_setup.load()
        doc['current_stage'] = 5
        sb_setup.save(doc)
        log = sb_setup.injections_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('\n'.join(json.dumps({'cross_scope': i < 1, 'automation': False, 'latency_ms': 100 + i})
                                 for i in range(10)) + '\n', encoding='utf-8')
        s = sb_setup.run(non_interactive=True)
        self.assertEqual(s['results'][0]['verdict'], 'pass')
        self.assertTrue(s['complete'])
        log.write_text(json.dumps({'cross_scope': True, 'latency_ms': 9000}) + '\n', encoding='utf-8')
        s = sb_setup.run(from_stage=5, non_interactive=True)
        self.assertEqual(s['results'][0]['verdict'], 'stop')

    def test_stage_error_is_stop_not_crash(self):
        self.src.stage2 = mock.Mock(side_effect=RuntimeError('boom'))
        s = sb_setup.run(non_interactive=True)
        self.assertEqual((s['stopped_at'], s['verdict']), (2, 'stop'))

    def test_broken_setup_json_is_not_overwritten(self):
        sb_setup.setup_path().write_text('{broken', encoding='utf-8')
        with self.assertRaises(SystemExit):
            sb_setup.run(non_interactive=True)
        self.assertEqual(sb_setup.setup_path().read_text(encoding='utf-8'), '{broken')

    def test_judge_merges_and_records_stage3(self):
        self.src.v3 = 'pending'
        s = sb_setup.run(non_interactive=True)
        self.assertEqual((s['stopped_at'], s['verdict']), (3, 'pending'))
        sample = sb_setup.logs_dir() / 'setup-3-sample-1.json'
        sample.write_text('{}', encoding='utf-8')
        s = sb_setup.judge(sb_setup.parse_judgments('11:ok, 12:noise,13:dup'))
        self.assertEqual(self.src.judged, (str(sample), {'11': 'ok', '12': 'noise', '13': 'dup'}))
        self.assertEqual(self.doc()['current_stage'], 4)
        with self.assertRaises(ValueError):
            sb_setup.parse_judgments('11:maybe')


class MissingSourcesTests(SetupEnv):
    def test_import_failure_makes_stage2_pending(self):
        # _sources_mod 가짜 대신 실제 import 경로를 탄다(sb_sources 가 없는 키트 판)
        with mock.patch.object(sb_setup, '_sources_mod', REAL_SOURCES_MOD), \
                mock.patch.dict(sys.modules, {'sb_sources': None}):
            s = sb_setup.run(non_interactive=True)
            self.assertEqual((s['stopped_at'], s['verdict']), (2, 'pending'))
            self.assertIn('미구현', s['hint'])
            sb_setup.accept(2, 'M2 통합 전')
            s = sb_setup.run(non_interactive=True)
            self.assertEqual((s['stopped_at'], s['verdict']), (3, 'pending'))
            with self.assertRaises(ValueError):
                sb_setup.judge({'1': 'ok'})


class FirstRunTests(SetupEnv):
    def test_non_tty_uses_defaults_without_reading(self):
        stdin = mock.Mock()
        stdin.isatty.return_value = False
        out = mock.Mock()
        out.isatty.return_value = False
        a = sb_setup.ask_first_run(timeout=120, stdin=stdin, stdout=out)
        stdin.readline.assert_not_called()
        self.assertEqual(a['sources'], [])
        self.assertEqual(set(self.doc()['answer_sources'].values()), {'default'})

    def test_ci_env_blocks_questions_even_on_tty(self):
        stdin, out = mock.Mock(), mock.Mock()
        stdin.isatty.return_value = out.isatty.return_value = True
        for var in ('CI', 'SB_NO_SETUP'):
            with mock.patch.dict(os.environ, {var: '1'}):
                self.assertFalse(sb_setup.can_ask(stdin, out))
        self.assertEqual(sb_setup.can_ask(stdin, out), os.name != 'nt')

    def test_existing_answers_are_kept(self):
        sb_setup.set_answers({'sources': ['~/meetings']})
        a = sb_setup.ask_first_run()
        self.assertEqual(a['sources'], ['~/meetings'])
        self.assertEqual(self.doc()['answer_sources']['sources'], 'user')

    def test_parse_answer(self):
        self.assertEqual(sb_setup.parse_answer('sources', ' ~/a , ~/b ,', []), ['~/a', '~/b'])
        self.assertTrue(sb_setup.parse_answer('embedding', 'y', False))
        self.assertFalse(sb_setup.parse_answer('embedding', 'n', True))
        self.assertTrue(sb_setup.parse_answer('embedding', '?', True))

    @unittest.skipIf(os.name == 'nt', 'pty 는 POSIX 만')
    def test_tty_answers_enter_input_and_timeout(self):
        import pty
        m, s = pty.openpty()
        self.addCleanup(os.close, m)
        with os.fdopen(s, 'r', encoding='utf-8', closefd=True) as fin, open(os.devnull, 'w') as devnull:
            fake_out = mock.Mock(wraps=devnull)
            fake_out.isatty.return_value = True
            os.write(m, b'\n~/meetings, ~/docs\n')   # 1: Enter(자동값), 2: 입력, 3: 시간 초과 → 나머지 기본값
            t0 = time.time()
            a = sb_setup.ask_first_run(timeout=0.3, stdin=fin, stdout=fake_out)
            self.assertLess(time.time() - t0, 5)
        src = self.doc()['answer_sources']
        self.assertEqual((src['agents'], src['sources'], src['llm_route'], src['embedding']), ('auto', 'user', 'default', 'default'))
        self.assertEqual(a['sources'], ['~/meetings', '~/docs'])


DRIVER = textwrap.dedent('''
    import sys
    sys.path.insert(0, sys.argv[1])
    import sb_setup
    sb_setup._doctor_rows = lambda: [('✅', 'venv', 'ok')]
    sb_setup._sources_mod = lambda: None
    sys.exit(sb_setup.main(['run', '--json']))
''')


class NonInteractiveProcessTests(SetupEnv):
    """CLI 전체 경로(첫 실행 질문 포함)를 자식 프로세스로 — 세 조건 모두 입력을 기다리지 않고 5초 안에 끝난다."""

    def _run(self, extra_env, stdin):
        env = dict(os.environ, **extra_env)
        t0 = time.time()
        r = subprocess.run([sys.executable, '-c', DRIVER, str(BIN)], stdin=stdin, capture_output=True, timeout=5,
                           env=env, cwd=str(self.root))
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(r.returncode, 0, r.stderr.decode('utf-8', 'replace'))
        out = json.loads(r.stdout.decode('utf-8'))
        self.assertEqual((out['stopped_at'], out['verdict']), (2, 'pending'))
        self.assertEqual(set(self.doc()['answer_sources'].values()), {'default'})
        sb_setup.setup_path().unlink()

    def test_stdin_devnull(self):
        with open(os.devnull, 'rb') as f:
            self._run({}, f)

    def test_not_a_tty_pipe(self):
        self._run({}, subprocess.PIPE)

    @unittest.skipIf(os.name == 'nt', 'pty 는 POSIX 만')
    def test_ci_on_a_tty(self):
        import pty
        m, s = pty.openpty()
        try:
            self._run({'CI': '1'}, s)     # stdin 은 tty 지만 CI 라 묻지 않는다
        finally:
            os.close(m)
            os.close(s)


class RouteTests(unittest.TestCase):
    def test_sb_routes(self):
        spec = importlib.util.spec_from_file_location('sb_cli_for_setup', str(BIN / 'sb.py'))
        sb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sb)
        self.assertEqual((sb.ROUTES['setup'], sb.ROUTES['sources'], sb.ROUTES['meeting']),
                         ('sb_setup.py', 'sb_sources.py', 'sb_meeting.py'))
        for script in sb.ROUTES.values():
            self.assertTrue((BIN / script).is_file(), script)

    def test_doctor_exposes_checks(self):
        spec = importlib.util.spec_from_file_location('sbkit_doctor_for_setup', str(ROOT / 'installer' / 'doctor.py'))
        d = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(d)
        self.assertIn(d.check_venv, d.CHECKS)
        self.assertEqual(len(d.CHECKS), len(set(d.CHECKS)))


class InstallerSetupTests(SetupEnv):
    def _inst(self):
        spec = importlib.util.spec_from_file_location('kit_install_setup', str(ROOT / 'installer' / 'install.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.HOME = self.home
        mod.SB_HOME = self.sbhome
        return mod

    def _args(self, **kw):
        return argparse.Namespace(**dict({'no_setup': False, 'no_codex': False}, **kw))

    def test_dry_run_does_nothing(self):
        inst = self._inst()
        inst.DRY = True
        (self.home / '.claude').mkdir(parents=True)
        fake = mock.Mock()
        with mock.patch.dict(sys.modules, {'sb_setup': fake}), mock.patch.object(inst, 'say'):
            inst.setup_stage(self._args())
        fake.run.assert_not_called()
        fake.ask_first_run.assert_not_called()
        self.assertFalse((self.home / '.claude' / 'skills').exists())

    def test_no_setup_links_skill_but_skips_run(self):
        inst = self._inst()
        (self.home / '.claude').mkdir(parents=True)
        fake = mock.Mock()
        with mock.patch.dict(sys.modules, {'sb_setup': fake}), mock.patch.object(inst, 'say'), \
                mock.patch.object(inst, 'which', return_value=None):
            inst.setup_stage(self._args(no_setup=True))
        fake.run.assert_not_called()
        dst = self.home / '.claude' / 'skills' / 'secondbrain-setup'
        self.assertTrue((dst / 'SKILL.md').is_file())
        self.assertFalse((self.home / '.codex' / 'skills').exists())   # Codex 가 없는 홈에는 두지 않는다

    def test_existing_skill_is_left_alone(self):
        inst = self._inst()
        dst = self.home / '.codex' / 'skills' / 'secondbrain-setup'
        dst.mkdir(parents=True)
        (dst / 'SKILL.md').write_text('mine', encoding='utf-8')
        with mock.patch.object(inst, 'say'), mock.patch.object(inst, 'which', return_value=None):
            inst.link_skills(self._args())
            inst.link_skills(self._args())
        self.assertEqual((dst / 'SKILL.md').read_text(encoding='utf-8'), 'mine')
        self.assertFalse(dst.is_symlink())

    def test_stopped_setup_is_guidance_not_failure(self):
        inst = self._inst()
        fake = mock.Mock()
        fake.run.return_value = {'complete': False, 'stopped_at': 4, 'verdict': 'pending', 'current_stage': 4}
        fake.render.return_value = '셋업 요약'
        with mock.patch.dict(sys.modules, {'sb_setup': fake}), mock.patch.object(inst, 'say') as say, \
                mock.patch.object(inst, 'WARNINGS', []) as w, mock.patch.object(inst, '_is_tty', return_value=False):
            inst.setup_stage(self._args())
        fake.ask_first_run.assert_called_once()
        fake.run.assert_called_once_with(non_interactive=True)
        self.assertEqual(w, [])
        self.assertTrue(any('4단계에서 멈췄습니다' in str(c) for c in say.call_args_list))

    def test_setup_crash_is_warning(self):
        inst = self._inst()
        fake = mock.Mock()
        fake.run.side_effect = SystemExit('잠금')
        with mock.patch.dict(sys.modules, {'sb_setup': fake}), mock.patch.object(inst, 'say'), \
                mock.patch.object(inst, 'WARNINGS', []) as w:
            inst.setup_stage(self._args())
        self.assertEqual(len(w), 1)

    def test_real_engine_on_isolated_home(self):
        inst = self._inst()
        with mock.patch.object(inst, 'say'), mock.patch.object(inst, '_is_tty', return_value=False):
            inst.setup_stage(self._args())
        d = self.doc()
        self.assertEqual([r['stage'] for r in d['stage_runs']][:2], [1, 2])
        self.assertEqual(d['answer_sources']['agents'], 'default')



class RealSourcesTests(SetupEnv):
    """mock 아닌 실제 sb_sources 로 1→2→3→4 를 돈다(1단계 doctor 만 가짜 — uvx·네트워크)."""

    def setUp(self):
        super().setUp()
        for k in ('CODEX_HOME', 'XDG_DATA_HOME'):   # 가짜 홈 밖 세션 위치를 보지 않게
            os.environ.pop(k, None)
        m = mock.patch.object(sb_setup, '_sources_mod', REAL_SOURCES_MOD)
        m.start()
        self.addCleanup(m.stop)

    @staticmethod
    def _title(i):          # 숫자는 정규화에서 지워지므로 글자로 구분(같은 세션 안 중복 판정 피함)
        return 'item %s%s' % (chr(97 + i // 26), chr(97 + i % 26))

    def make_real_db(self, otype='decision'):
        """title 이 있는 claude-mem DB(프로젝트 둘, 관측 각 40건) + Claude 세션 파일 하나."""
        alpha, beta = self.home / 'alpha', self.home / 'code' / 'beta'
        alpha.mkdir(parents=True)
        beta.mkdir(parents=True)
        proj = self.home / '.claude' / 'projects' / '-alpha'
        proj.mkdir(parents=True)
        (proj / 's1.jsonl').write_text(json.dumps({'cwd': str(alpha), 'type': 'user', 'message': {'content': 'BODY'}})
                                       + '\n', encoding='utf-8')
        db = self.home / '.claude-mem' / 'claude-mem.db'
        db.parent.mkdir(parents=True)
        now = int(time.time() * 1000)
        con = sqlite3.connect(str(db))
        con.execute('CREATE TABLE sdk_sessions (memory_session_id TEXT, project TEXT, cwd TEXT, started_at_epoch INTEGER)')
        con.execute('CREATE TABLE observations (id INTEGER PRIMARY KEY, memory_session_id TEXT, project TEXT, type TEXT, '
                    'title TEXT, created_at_epoch INTEGER)')
        for sid, tag, cwd in (('a1', 'alpha', alpha), ('b1', 'beta', beta)):
            con.execute('INSERT INTO sdk_sessions VALUES (?,?,?,?)', (sid, tag, str(cwd), now))
            con.executemany('INSERT INTO observations (memory_session_id, project, type, title, created_at_epoch) '
                            'VALUES (?,?,?,?,?)', [(sid, tag, otype, self._title(i), now) for i in range(40)])
        con.commit()
        con.close()

    def test_real_sources_run_stops_at_stage4_and_is_repeatable(self):
        self.make_real_db()
        a = sb_setup.run(non_interactive=True)
        self.assertEqual([r['stage'] for r in a['results']], [1, 2, 3, 4])
        self.assertEqual([r['verdict'] for r in a['results']], ['pass', 'pass', 'pass', 'pending'])
        self.assertEqual((a['stopped_at'], a['current_stage']), (4, 4))
        m2 = {m['name']: m['value'] for m in a['results'][1]['metrics']}
        self.assertEqual((m2['session_files'], m2['session_cwds'], m2['body_fields_saved']), (1, 1, 0))
        m3 = {m['name']: m['value'] for m in a['results'][2]['metrics']}
        self.assertEqual((m3['sample_n'], m3['noise_dup_ratio'], m3['scope_mismatch_ratio'], m3['unjudged_ratio']),
                         (50, 0.0, 0.0, 0.0))
        raw = (self.sbhome / 'sources.json').read_text(encoding='utf-8')
        self.assertNotIn('BODY', raw)
        sample = sb_setup.latest_sample()
        self.assertEqual(sample.name, 'setup-3-sample-%d.json' % sb_setup.SAMPLE_SEED)
        self.assertNotIn('item', sample.read_text(encoding='utf-8'))      # 제목은 표본 파일에 남기지 않는다
        b = sb_setup.run(non_interactive=True)
        self.assertEqual(b['results'][-1]['verdict'], 'pending')
        self.assertEqual(a['fingerprint'], b['fingerprint'])
        # 기록을 치우고 1단계부터 다시 돌려도 같은 홈이면 같다
        sb_setup.setup_path().rename(self.root / 'setup.old.json')
        c = sb_setup.run(non_interactive=True)
        self.assertEqual([r['stage'] for r in c['results']], [1, 2, 3, 4])
        self.assertEqual(a['fingerprint'], c['fingerprint'])

    def test_real_unjudged_sample_pending_then_judge_moves_on(self):
        self.make_real_db(otype='discovery')          # 휴리스틱이 판정 못 하는 제목·type → unjudged 100%
        s = sb_setup.run(non_interactive=True)
        self.assertEqual((s['stopped_at'], s['verdict']), (3, 'pending'))
        self.assertIn('sb setup judge', s['hint'])
        ids = json.loads(sb_setup.latest_sample().read_text(encoding='utf-8'))['ids']
        s = sb_setup.judge({i: 'ok' for i in ids})
        self.assertEqual(s['results'][0]['verdict'], 'pass')
        self.assertEqual(self.doc()['current_stage'], 4)
        s = sb_setup.run(non_interactive=True)
        self.assertEqual([(r['stage'], r['verdict']) for r in s['results']], [(4, 'pending')])



class ReviewRegressionTests(SetupEnv):
    """0182f9e 검토 반영 — 차례 검사·승인 1회·답 바뀜 반영·기록 상한·지문·묻기 조건·권한."""

    def test_judge_refuses_when_earlier_stage_is_stopped(self):
        self.src.v3 = 'pending'
        sb_setup.run(non_interactive=True)
        (sb_setup.logs_dir() / 'setup-3-sample-1.json').write_text('{}', encoding='utf-8')
        self.rows = list(BAD_ROWS)
        s = sb_setup.run(from_stage=1, non_interactive=True)
        self.assertEqual(s['current_stage'], 1)
        with self.assertRaises(ValueError):
            sb_setup.judge({'11': 'ok'})
        self.assertEqual(self.doc()['current_stage'], 1)
        self.assertIsNone(self.src.judged)          # 표본 파일도 건드리지 않는다

    def test_approval_is_used_once_rollback_is_respected(self):
        self.make_db()
        s = sb_setup.run(non_interactive=True)
        sb_setup.approve(s['plan_hash'][:12])
        s = sb_setup.run(non_interactive=True)
        self.assertEqual(s['stages'][4], 'pass')
        self.assertNotIn('approved_plan_hash', self.doc()['answers'])
        applies = lambda: len(list(sb_vault.vault_dir().glob('apply-*.json')))
        n = applies()
        sb_vault.rollback()
        self.assertFalse(sb_vault.status()['configured'])
        s = sb_setup.run(from_stage=4, non_interactive=True)
        self.assertEqual(s['stages'][4], 'pending')            # 다시 승인을 받는다
        self.assertEqual(applies(), n)
        self.assertFalse(sb_vault.status()['configured'])

    def test_changed_sources_answer_reruns_stage2(self):
        sb_setup.run(non_interactive=True)
        self.assertEqual(self.doc()['current_stage'], 5)
        sb_setup.set_answers({'sources': ['~/docs']})
        self.assertEqual(self.doc()['current_stage'], 2)
        s = sb_setup.run(non_interactive=True)
        r2 = s['results'][0]
        self.assertEqual(r2['stage'], 2)
        self.assertEqual({m['name']: m['value'] for m in r2['metrics']}['sources'], 1)
        sb_setup.set_answers({'sources': ['~/docs'], 'llm_route': 'x'})    # 같은 답이면 되감지 않는다
        self.assertEqual(self.doc()['current_stage'], 5)

    def test_trim_keeps_last_verdict_of_every_stage(self):
        sb_setup.run(non_interactive=True)
        before = sb_setup.status()
        for _ in range(sb_setup.MAX_RUNS + 5):
            sb_setup.run(non_interactive=True)
        after = sb_setup.status()
        self.assertLessEqual(len(self.doc()['stage_runs']), sb_setup.MAX_RUNS)
        self.assertEqual(after['stages'], before['stages'])
        self.assertEqual(after['fingerprint'], before['fingerprint'])

    def test_fingerprint_ignores_hook_latency_row(self):
        a = sb_setup.run(only=1, non_interactive=True)['fingerprint']
        self.rows = list(OK_ROWS) + [('⚠', '회수 훅 소요 시간', '11.0초')]
        b = sb_setup.run(only=1, non_interactive=True)['fingerprint']
        self.assertEqual(a, b)

    def test_agent_shell_does_not_ask_even_on_tty(self):
        stdin, out = mock.Mock(), mock.Mock()
        stdin.isatty.return_value = out.isatty.return_value = True
        stdin.fileno.side_effect = OSError
        for var in ('CLAUDECODE', 'CODEX_SANDBOX'):
            with mock.patch.dict(os.environ, {var: '1'}):
                self.assertFalse(sb_setup.can_ask(stdin, out))

    @unittest.skipIf(os.name == 'nt', 'POSIX 만')
    def test_background_process_group_does_not_ask(self):
        stdin, out = mock.Mock(), mock.Mock()
        stdin.isatty.return_value = out.isatty.return_value = True
        stdin.fileno.return_value = 0
        with mock.patch.object(os, 'tcgetpgrp', return_value=os.getpgrp() + 1):
            self.assertFalse(sb_setup.can_ask(stdin, out))
        with mock.patch.object(os, 'tcgetpgrp', return_value=os.getpgrp()):
            self.assertEqual(sb_setup.can_ask(stdin, out), os.name != 'nt')

    @unittest.skipIf(os.name == 'nt', 'POSIX 권한')
    def test_new_sb_home_and_logs_are_0700_and_setup_json_back_to_0600(self):
        fresh = self.root / 'fresh-home'
        with mock.patch.dict(os.environ, {'SB_HOME': str(fresh)}):
            sb_setup.run(only=1, non_interactive=True)
            self.assertEqual(fresh.stat().st_mode & 0o777, 0o700)
            self.assertEqual(sb_setup.logs_dir().stat().st_mode & 0o777, 0o700)
            sb_setup.setup_path().chmod(0o644)
            sb_setup.run(only=1, non_interactive=True)
            self.assertEqual(sb_setup.setup_path().stat().st_mode & 0o777, 0o600)

    def test_doctor_children_get_no_stdin_and_timeouts(self):
        spec = importlib.util.spec_from_file_location('sbkit_doctor_rt', str(ROOT / 'installer' / 'doctor.py'))
        d = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(d)
        self.assertIs(d.UTF8['stdin'], subprocess.DEVNULL)
        calls = []

        def fake_run(cmd, **kw):
            calls.append(kw)
            return subprocess.CompletedProcess(cmd, 0, 'x', '')
        with mock.patch.object(d.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(d, 'VPY', Path(sys.executable)):
            d.check_venv()
            d.check_state_and_capture()
        self.assertTrue(calls)
        for kw in calls:
            self.assertIn('timeout', kw)
            self.assertIs(kw.get('stdin'), subprocess.DEVNULL)


class InstallerReviewTests(InstallerSetupTests):
    def test_no_vault_stops_before_stage4(self):
        inst = self._inst()
        with mock.patch.object(inst, 'say'), mock.patch.object(inst, '_is_tty', return_value=False), \
                mock.patch.object(inst, 'link_skills'):
            inst.setup_stage(self._args(no_vault=True))
        self.assertEqual(sorted({r['stage'] for r in self.doc()['stage_runs']}), [1, 2, 3])
        self.assertFalse(sb_vault.vault_json_path().exists())
        self.assertFalse((self.sbhome / 'vault').exists())

    def test_say_survives_cp949_stdout(self):
        import io
        inst = self._inst()
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding='cp949', errors='strict')
        with mock.patch.object(sys, 'stdout', out):
            inst.say('완료 — ✅ ⏸ ↷')
        out.flush()
        self.assertIn('완료'.encode('cp949'), raw.getvalue())

    # 부모 클래스 시험을 두 번 돌리지 않는다
    test_dry_run_does_nothing = test_no_setup_links_skill_but_skips_run = None
    test_existing_skill_is_left_alone = test_stopped_setup_is_guidance_not_failure = None
    test_setup_crash_is_warning = test_real_engine_on_isolated_home = None


if __name__ == '__main__':
    unittest.main()
