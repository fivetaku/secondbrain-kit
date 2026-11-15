"""M3 검토(11-16, 커밋 1859eaf) 반영 회귀 시험 — 가짜 홈·임시 DB만 쓴다.

P1: 같은 프로젝트 몫 하한·주입 구간, 자동화 문구 오탐, project 키 합침, 별칭 키, set 순서 의존,
    세션 시작 노출(자동화·아침 문자 대상), L2 절차 오류 종결.
P2: 하위 폴더 건너뛰기, 윈도우형 홈 아래 임시 폴더, recalld 정규화 비교, nan/inf, 자동화 로그 통계, 감사 출력.
"""
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / 'bin'))
sys.path.insert(0, str(KIT / 'hooks'))
sys.path.insert(0, str(KIT / 'tests'))
import sb_recalld  # noqa: E402
import sb_recall  # noqa: E402
import sb_scope  # noqa: E402
from test_recalld_hybrid import engine  # noqa: E402


def _clean_env(**extra):
    keep = {k: v for k, v in os.environ.items()
            if k not in ('CLAUDE_MEM_INTERNAL', 'SB_RECALL', 'SB_RECALL_AUTOMATION', 'SB_RECALL_SKIP_PROJECTS',
                         'SB_SCOPE_NORM', 'SB_HOME_ALIASES', 'SB_RECALLD_SAME_MIN', 'SB_RECALLD_SHARE_WINDOW',
                         'CLAUDE_CODE_ENTRYPOINT', 'SB_LOOPS_EXPOSURE')}
    keep.update(extra)
    return mock.patch.dict(os.environ, keep, clear=True)


# ── P1-1 같은 프로젝트 몫: 하한·주입 구간 ─────────────────────────────────────────────
class ShareFloorTests(unittest.TestCase):
    def setUp(self):
        p = _clean_env()
        p.start()
        self.addCleanup(p.stop)

    def test_limit10_share_counts_in_injected_window(self):
        # 다른 프로젝트 30건 뒤에 같은 프로젝트 10건 — 몫은 앞 3건 기준 2건, 끌어올린 기록이 앞 3건 안에 온다
        rows = [('o%d' % i, 0, 'other', '') for i in range(30)] + [('s%d' % i, 0, 'me', '') for i in range(10)]
        out = sb_recalld.same_project_share(rows, 'me', 10, lambda r: r[2], window=sb_recalld.share_window(10))
        ids = [r[0] for r in out]
        self.assertEqual(len(ids), 10)
        self.assertEqual(ids[:3], ['o0', 's0', 's1'])        # 주입되는 앞 3건에 같은 프로젝트 2건
        self.assertEqual(ids[3:5], ['o1', 'o2'])              # 밀려난 다른 프로젝트는 바로 뒤로(버리지 않음)
        self.assertEqual(sum(1 for i in ids if i.startswith('s')), 2)   # 7건을 끌어오지 않는다

    def test_ineligible_same_project_is_not_promoted(self):
        rows = [('o%d' % i, 0, 'other', '') for i in range(30)] + [('s%d' % i, 0, 'me', '') for i in range(10)]
        out = sb_recalld.same_project_share(rows, 'me', 10, lambda r: r[2], window=3,
                                            eligible=lambda r: r[0] == 's4')
        self.assertEqual([r[0] for r in out][:3], ['o0', 'o1', 's4'])
        none = sb_recalld.same_project_share(rows, 'me', 10, lambda r: r[2], window=3, eligible=lambda r: False)
        self.assertEqual([r[0] for r in none], ['o%d' % i for i in range(10)])

    def test_share_window_env(self):
        self.assertEqual(sb_recalld.share_window(10), 3)
        self.assertEqual(sb_recalld.share_window(2), 2)
        with mock.patch.dict(os.environ, {'SB_RECALLD_SHARE_WINDOW': '5'}):
            self.assertEqual(sb_recalld.share_window(10), 5)
        for bad in ('x', '0', '-2'):
            with mock.patch.dict(os.environ, {'SB_RECALLD_SHARE_WINDOW': bad}):
                self.assertEqual(sb_recalld.share_window(10), 3, bad)

    def test_nan_inf_fall_back_to_default(self):
        for raw in ('nan', 'inf', '-inf', 'NaN'):
            with mock.patch.dict(os.environ, {'SB_RECALLD_SAME_MIN': raw}):
                self.assertEqual(sb_recalld.same_min(3), 2, raw)


class EngineFloorTests(unittest.TestCase):
    """recalld.recall: 끌어올리는 같은 프로젝트 기록도 그 자체로 주입 문턱을 넘어야 한다."""
    # 다른 프로젝트 3건은 질의 명사 3개가 모두 들었고, 같은 프로젝트 2건 중 하나는 흔한 단어 1개만 겹친다
    LEX = [(20, -9.0, 'other', '거래처 단가 계약'), (21, -8.0, 'other', '거래처 단가 계약'),
           (22, -7.0, 'other', '거래처 단가 계약'), (30, -1.0, 'work', '거래처'),
           (31, -0.5, 'work', '거래처 단가 계약 갱신')]

    def recall(self, project='work', limit=10, lex=None):
        e, patches = engine(lex or self.LEX, [(20, 0.9)], order=[20, 21, 22, 30, 31],
                            texts={i: 't' for i in (20, 21, 22, 30, 31)})
        with _clean_env():
            for p in patches:
                p.start()
            try:
                return e.recall('거래처 단가 계약 어떻게 됐어', project, limit)
            finally:
                for p in reversed(patches):
                    p.stop()

    def test_only_relevant_same_project_promoted_and_flagged(self):
        d = self.recall()
        ids = [it['id'] for it in d['items']]
        self.assertEqual(ids[:3], [20, 21, 31])      # 31(문턱 통과)만 올라오고, 30(단어 1개)은 올라오지 않는다
        flags = {it['id']: it['pass'] for it in d['items']}
        self.assertEqual((flags[31], flags[30]), (True, False))
        self.assertTrue(d['gate']['pass'])

    def test_normalized_project_variants_count_as_same(self):
        lex = [(20, -9.0, 'other', '거래처 단가 계약'), (21, -8.0, 'other', '거래처 단가 계약'),
               (22, -7.0, 'other', '거래처 단가 계약'), (31, -0.5, 'Work_Proj', '거래처 단가 계약 갱신'),
               (32, -0.4, 'work-proj', '거래처 단가 계약 메모')]
        d = self.recall(project='work-proj', lex=lex)
        top = [it['id'] for it in d['items']][:3]
        self.assertIn(31, top)   # 대소문자·'_' 만 다른 꼬리표도 같은 프로젝트로 센다(P2-3)
        self.assertIn(32, top)

    def test_project_tags_lookup(self):
        e = sb_recalld.Engine.__new__(sb_recalld.Engine)
        e.projects = frozenset({'Work_Proj', 'work-proj', 'other', ''})
        with _clean_env():
            self.assertEqual(e.project_tags('work-proj'), ['Work_Proj', 'work-proj'])
            self.assertEqual(e.project_tags('nothing'), ['nothing'])


class RefinePassTests(unittest.TestCase):
    def test_same_project_without_pass_is_not_boosted(self):
        items = [{'id': 1, 'project': 'other', 'title': '거래처 단가 계약', '_d': '2026-11-01', 'pass': True},
                 {'id': 2, 'project': 'other', 'title': '거래처 단가 정리', '_d': '2026-11-02', 'pass': True},
                 {'id': 3, 'project': 'proj', 'title': '무관', '_d': '2026-11-10', 'pass': False},
                 {'id': 4, 'project': 'proj', 'title': '거래처 단가 갱신', '_d': '2026-11-09', 'pass': True}]
        with _clean_env(), mock.patch.object(sb_recall, 'superseded_ids', return_value=set()):
            out = sb_recall.refine(items, '거래처 단가 계약 정리', 'proj')
        ids = [it['id'] for it in out]
        self.assertEqual(ids[0], 4)                 # 문턱을 넘은 같은 프로젝트만 우대
        # 문턱 밖 같은 프로젝트 기록(3)은 우대받지 않는다 — 가장 최신이어도 문턱을 넘은 4 뒤(나머지는 예전처럼 최신순)
        self.assertGreater(ids.index(3), ids.index(4))
        items[2]['pass'] = True
        with _clean_env(), mock.patch.object(sb_recall, 'superseded_ids', return_value=set()):
            self.assertEqual([it['id'] for it in sb_recall.refine(items, '거래처 단가 계약 정리', 'proj')][0], 3)

    def test_worker_items_without_pass_keep_old_boost(self):
        items = [{'id': 1, 'project': 'other', 'title': '거래처 단가 계약', '_d': '2026-11-10'},
                 {'id': 2, 'project': 'proj', 'title': '아무', '_d': '2026-11-01'}]
        with _clean_env(), mock.patch.object(sb_recall, 'superseded_ids', return_value=set()):
            out = sb_recall.refine(items, '거래처 단가 계약', 'proj')
        self.assertEqual([it['id'] for it in out], [2, 1])


# ── P1-2 자동화 문구 오탐 ─────────────────────────────────────────────────────────
class ProbePhraseTests(unittest.TestCase):
    def setUp(self):
        p = _clean_env()
        p.start()
        self.addCleanup(p.stop)

    def why(self, prompt, cwd='/Users/x/work/alpha'):
        return sb_recall.automation_reason({'cwd': cwd}, prompt)

    def test_normal_prompts_are_not_automation(self):
        for p in ('Look only at the diff and tell me', 'Please look only at the failing tests',
                  'book only 옵션 추가해줘', '이 book only 정리해', 'hook only 바꿔줘',
                  '이 함수가 respond with only JSON 하도록 바꿔줘', '봇이 reply exactly 사용자 입력을 하게',
                  'please reply exactly as the customer wrote',
                  '이 로그 뭐야 [SYSTEM NOTIFICATION] 작업 끝남', '로그에 [SYSTEM NOTIFICATION 문자열이 왜 찍혀?',
                  '이 프롬프트에 <<autonomous-loop 표지가 왜 있어'):
            self.assertEqual(self.why(p), '', p)

    def test_probe_prompts_still_detected(self):
        for p in ('Reply exactly: PONG', 'reply exactly OK', 'Respond with only the number',
                  'say hi. OK only', 'Answer yes or no. OK only.', '<<autonomous-loop>> 다음 단계',
                  '[SYSTEM NOTIFICATION] 작업 끝남'):
            self.assertTrue(self.why(p), p)

    def test_long_prompt_starting_with_phrase_is_not_probe(self):
        self.assertEqual(self.why('Reply exactly: ' + '설명 ' * 120), '')


# ── P1-3 project 키 합침 ────────────────────────────────────────────────────────
class ProjectKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(os.path.realpath(self.tmp.name))
        self.home = root / 'Users' / 'dev'
        self.home.mkdir(parents=True)
        p = _clean_env(HOME=str(self.home), USERPROFILE=str(self.home), USER='dev', USERNAME='dev', LOGNAME='dev')
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        sb_scope._HOME_CACHE.clear()

    def test_other_homes_and_shared_folders_stay_distinct(self):
        k = sb_scope.project_key
        self.assertNotEqual(k('/Users/alice'), k('/home/bob'))
        self.assertNotEqual(k('/Users/alice'), '~')
        self.assertNotEqual(k('/home/projects'), '~')
        self.assertNotEqual(k('/Users/Shared'), '~')

    def test_same_user_home_on_other_pc_is_home(self):
        k = sb_scope.project_key
        for v in ('dev', 'Dev', '/Users/dev', '/home/dev', 'C:\\Users\\dev', str(self.home), '~'):
            self.assertEqual(k(v), '~', v)

    def test_project_folder_named_like_user_is_not_home_when_path(self):
        self.assertEqual(sb_scope.project_key('/work/x/dev'), 'dev')

    def test_space_is_not_dropped(self):
        self.assertNotEqual(sb_scope.project_key('my app'), sb_scope.project_key('myapp'))
        self.assertEqual(sb_scope.project_key('My_App'), sb_scope.project_key('my-app'))

    def test_home_aliases_env(self):
        self.assertNotEqual(sb_scope.project_key('laptopuser'), '~')
        with mock.patch.dict(os.environ, {'SB_HOME_ALIASES': 'laptopuser'}):
            self.assertEqual(sb_scope.project_key('laptopuser'), '~')
            self.assertEqual(sb_scope.project_key('/Users/laptopuser'), '~')

    def test_hook_and_recalld_use_same_key(self):
        for v in ('Second_Brain', '/Users/dev', 'dev', '/x/y/Proj_A', 'my app'):
            self.assertEqual(sb_recall.norm_project(v), sb_recalld._project_key(v), v)


# ── P1 자동화 판정이 set 순서에 따라 갈림 / P2 윈도우 홈 아래 임시 폴더 ────────────────────
class AutomationDirTests(unittest.TestCase):
    CODE = ('import os, sys; sys.path.insert(0, %r); sys.path.insert(0, %r); import sb_recall; '
            'print(sb_recall.automation_reason({"cwd": sys.argv[1]}, "배포 스크립트 고쳐줘"))')

    def test_home_under_tmp_is_stable_across_hash_seeds(self):
        with tempfile.TemporaryDirectory() as t:
            root = os.path.realpath(t)
            home = os.path.join(root, 'hh')
            os.makedirs(os.path.join(home, 'proj'))
            code = self.CODE % (str(KIT / 'bin'), str(KIT / 'hooks'))
            seen = set()
            for seed in range(8):
                env = {k: v for k, v in os.environ.items() if k not in ('SB_RECALL', 'CLAUDE_MEM_INTERNAL')}
                env.update(HOME=home, USERPROFILE=home, PYTHONHASHSEED=str(seed), TMPDIR=root)
                p = subprocess.run([sys.executable, '-B', '-c', code, os.path.join(home, 'proj')], env=env,
                                   capture_output=True, text=True, encoding='utf-8', timeout=30)
                self.assertEqual(p.returncode, 0, p.stderr)
                seen.add(p.stdout.strip())
            self.assertEqual(seen, {''})

    def test_temp_under_home_is_automation(self):
        # 윈도우 %TEMP%(C:\Users\<u>\AppData\Local\Temp)처럼 홈 아래 임시 폴더는 더 구체적인 임시 폴더가 이긴다
        with tempfile.TemporaryDirectory() as t:
            root = os.path.realpath(t)
            home = os.path.join(root, 'Users', 'u')
            temp = os.path.join(home, 'AppData', 'Local', 'Temp')
            os.makedirs(os.path.join(temp, 'run1'))
            os.makedirs(os.path.join(home, 'proj'))
            with _clean_env(HOME=home, USERPROFILE=home), \
                    mock.patch.object(sb_recall.tempfile, 'gettempdir', return_value=temp):
                self.assertTrue(sb_recall._under_automation_dir(os.path.join(temp, 'run1')))
                self.assertFalse(sb_recall._under_automation_dir(os.path.join(home, 'proj')))


class SkipProjectSubdirTests(unittest.TestCase):
    def test_subfolder_of_skip_project(self):
        with tempfile.TemporaryDirectory() as t:
            home = os.path.join(os.path.realpath(t), 'Users', 'u')
            os.makedirs(home)
            with _clean_env(HOME=home, USERPROFILE=home):
                why = lambda c: sb_recall.automation_reason({'cwd': c}, '배포 스크립트 고쳐줘')  # noqa: E731
                self.assertEqual(why(os.path.join(home, 'insane-writer')), 'skip_project')
                self.assertEqual(why(os.path.join(home, 'insane-writer', 'books', 'b1')), 'skip_project')
                self.assertEqual(why(os.path.join(home, 'insane-writer-notes')), '')
                self.assertEqual(why(os.path.join(home, 'work', 'alpha')), '')


# ── 자동화 기록·통계 ─────────────────────────────────────────────────────────────
class AutomationLogTests(unittest.TestCase):
    def run_main(self, env, prompt='Reply exactly: 거래처 단가 계약 정리'):
        logs, out = [], io.StringIO()
        payload = {'prompt': prompt, 'session_id': 's', 'cwd': '/Users/x/alpha'}
        with tempfile.TemporaryDirectory() as state, _clean_env(**env), \
                mock.patch.object(sb_recall, 'STATE_DIR', state), \
                mock.patch.object(sb_recall, 'recall_log', side_effect=logs.append), \
                mock.patch.object(sb_recall, 'correction_note', return_value=''), \
                mock.patch.object(sb_recall, 'recalld_search') as rs, \
                mock.patch('sys.stdin', io.StringIO(json.dumps(payload, ensure_ascii=False))), \
                mock.patch('sys.stdout', out):
            sb_recall.main()
        rs.assert_not_called()
        return logs

    def test_internal_and_off_write_nothing(self):
        self.assertEqual(self.run_main({'CLAUDE_MEM_INTERNAL': '1'}), [])
        self.assertEqual(self.run_main({'SB_RECALL': '0'}), [])
        self.assertEqual(len(self.run_main({})), 1)

    def test_health_usage_skips_automation_lines(self):
        import time as _t
        import sb_health
        with tempfile.TemporaryDirectory() as home:
            logs = Path(home) / 'logs'
            logs.mkdir()
            now = _t.strftime('%Y-%m-%dT%H:%M:%S')
            (logs / 'recall.jsonl').write_text('\n'.join([
                json.dumps({'ts': now, 'ids': [1]}),
                json.dumps({'ts': now, 'automation': True, 'why': 'skip_project'}),
                json.dumps({'ts': now, 'automation': True, 'why': 'tmp_cwd'})]), encoding='utf-8')
            with mock.patch.dict(os.environ, {'SB_HOME': home}):
                u = sb_health.recall_usage(7)
        self.assertEqual((u['injections'], u['empty'], u['automation']), (1, 0, 2))


# ── 세션 시작 노출 기록 ─────────────────────────────────────────────────────────
class ExposureSkipTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('sc_review', str(KIT / 'hooks' / 'session_context.py'))
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(os.path.realpath(self.tmp.name))
        self.home = root / 'Users' / 'u'
        (self.home / 'work' / 'alpha').mkdir(parents=True)
        self.ledger = root / 'loops.jsonl'
        p = _clean_env(HOME=str(self.home), USERPROFILE=str(self.home), SB_HOME=str(root / 'sb'),
                       SB_LOOPS_PATH=str(self.ledger))
        p.start()
        self.addCleanup(p.stop)
        import sb_store
        self.sb_store = sb_store
        recs = [{'id': 'L0000aaa1', 'title': '보통', 'status': 'open', 'date_opened': '2026-11-01',
                 'project': 'alpha', 'value': 'medium', 'expose_count': 0},
                {'id': 'L0000aaa2', 'title': '중요', 'status': 'open', 'date_opened': '2026-11-01',
                 'project': 'alpha', 'value': 'high', 'expose_count': 0},
                {'id': 'L0000aaa3', 'title': '마감', 'status': 'open', 'date_opened': '2026-11-01',
                 'project': 'alpha', 'value': 'low', 'expose_count': 0, 'due': '2000-01-01'}]
        sb_store.save_loops(recs, str(self.ledger))
        self.text = '트랙 3건:\n  · [L0000aaa1] 보통\n  · [L0000aaa2] 중요\n  · [L0000aaa3] 마감\n주의: [L0000ffff] 다른 scope'
        self.cwd = {'cwd': str(self.home / 'work' / 'alpha')}

    def counts(self):
        return {r['id']: r.get('expose_count', 0) for r in self.sb_store.load_loops(str(self.ledger))}

    def test_brief_targets_are_not_counted_at_session_start(self):
        self.assertEqual(self.mod.record_exposure(self.text, self.cwd), ['L0000aaa1'])
        self.assertEqual(self.counts(), {'L0000aaa1': 1, 'L0000aaa2': 0, 'L0000aaa3': 0})

    def test_automation_sessions_do_not_count(self):
        before = self.ledger.read_bytes()
        for env in ({'CLAUDE_MEM_INTERNAL': '1'}, {'SB_RECALL': '0'}, {'CLAUDE_CODE_ENTRYPOINT': 'sdk-cli'}):
            with mock.patch.dict(os.environ, env):
                self.assertEqual(self.mod.record_exposure(self.text, self.cwd), [], env)
        self.assertEqual(self.mod.record_exposure(self.text, {'cwd': '/tmp/run1'}), [])
        self.assertEqual(self.mod.record_exposure(self.text, {'cwd': str(self.home / 'insane-writer' / 'b')}), [])
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_only_track_lines_count(self):
        self.assertEqual(self.mod.record_exposure('주의: [L0000aaa1] 닫힘 · 상태 = L0000aaa1', self.cwd), [])


# ── L2 절차 오류 종결 ────────────────────────────────────────────────────────────
class L2ProceduralRejectTests(unittest.TestCase):
    def setUp(self):
        import sb_state
        import sb_audit
        self.sb_state, self.sb_audit = sb_state, sb_audit
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(os.path.realpath(self.tmp.name))
        self.t = t
        (t / 'aliases.json').write_text('{}', encoding='utf-8')
        (t / 'loops.jsonl').write_text('', encoding='utf-8')
        p = mock.patch.dict(os.environ, {'SB_HOME': str(t / 'sb'), 'SB_STATE_DB': str(t / 'state.db'),
                                         'SB_STATE_PILOT_SCOPES': 'alpha', 'SB_VERIFY_ALLOWED_ROOTS': str(t),
                                         'SB_PROJECT_ALIASES': str(t / 'aliases.json'),
                                         'SB_CLAUDE_MEM_DB': str(t / 'none.db'), 'SB_LOOPS_PATH': str(t / 'loops.jsonl'),
                                         'SB_EVAL_NOW': '2026-11-16T00:00:00+00:00', 'SB_L2_EXPIRE': '1'})
        p.start()
        self.addCleanup(p.stop)
        sb_state.migrate(str(t / 'state.db'))

    def status(self, cid):
        with contextlib.closing(sqlite3.connect(str(self.t / 'state.db'))) as db:
            return db.execute('SELECT status FROM state_candidate WHERE id=?', (cid,)).fetchone()[0]

    def measured(self, body, write_id):
        return self.sb_state.propose({'scope_id': 'alpha', 'fact_key': 'deploy.target', 'body': body, 'value_json': body,
                                      'kind': 'measured_fact', 'source': 't', 'write_id': write_id})['candidate_id']

    def test_procedural_rejected_stays_retryable(self):
        st = self.sb_state
        r = st.propose({'scope_id': 'alpha', 'fact_key': 'd', 'body': 'x', 'kind': 'user_decision',
                        'source': 't', 'write_id': 'ud'})
        self.assertEqual(st.verify(r['candidate_id'], 'file_contains', str(self.t / 'loops.jsonl'))['result'], 'rejected')
        self.assertEqual(st.expire()['changed'], 0)
        self.assertEqual(self.status(r['candidate_id']), 'proposed')

    def test_reopen_after_expire_then_accept(self):
        st = self.sb_state
        cid = self.measured('val-a', 'w-a')
        ev = self.t / 'ev.txt'
        ev.write_text('measured: zzz\n', encoding='utf-8')
        self.assertEqual(st.verify(cid, 'file_contains', str(ev))['result'], 'contradicted')
        self.assertEqual(st.expire()['changed'], 1)
        with self.assertRaises(st.StateError):
            st.accept(cid, 0)
        self.assertEqual(st.reopen(cid)['to'], 'proposed')
        ev.write_text('measured: val-a\n', encoding='utf-8')
        self.assertEqual(st.verify(cid, 'file_contains', str(ev))['result'], 'confirmed')
        self.assertEqual(st.accept(cid, 0)['status'], 'ok')
        with self.assertRaises(st.StateError):   # expire 가 닫지 않은 후보는 되살리지 않는다
            st.reopen(cid)

    def test_audit_keeps_non_expired_rejected_candidates(self):
        st = self.sb_state
        ok = self.measured('val-good', 'w-good')
        ev = self.t / 'ok.txt'
        ev.write_text('measured: val-good\n', encoding='utf-8')
        st.verify(ok, 'file_contains', str(ev))
        st.accept(ok, 0)
        bad = self.measured('val-bad', 'w-bad')
        with contextlib.closing(sqlite3.connect(str(self.t / 'state.db'))) as db:   # expire 밖에서 rejected 가 된 옛 후보
            db.execute("UPDATE state_candidate SET status='rejected' WHERE id=?", (bad,))
            db.commit()
        item = [i for i in self.sb_audit.audit_scope('alpha')['items'] if i['layer'] == 'L2'][0]
        self.assertIn('value_conflict', item['reason_codes'])


if __name__ == '__main__':
    unittest.main()
