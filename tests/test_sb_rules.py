"""sb_rules.py 계약 — feedback 자동 메모리 → rules/<scope>.md (생성 파일만 갱신, 손으로 쓴 파일 보존)."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import sb_rules  # noqa: E402
import sb_search  # noqa: E402
import sync_automemory as automemory  # noqa: E402

FEEDBACK = '---\nname: {name}\ndescription: {title}\nmetadata:\n  type: feedback\n---\n{body}\n'
LEGACY = '---\nname: {name}\ndescription: {title}\ntype: feedback\n---\n{body}\n'
USER = '---\nname: {name}\ndescription: {title}\nmetadata:\n  type: user\n---\n{body}\n'


class RulesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'projects'
        self.root.mkdir()
        self.rules = self.base / 'rules'
        self.sbhome = self.base / 'sbhome'
        aliases = self.base / 'aliases.json'
        # 세션 cwd 는 임시 폴더 밖의 가짜 경로로 둔다(임시 폴더 세션은 규칙층에서 빠진다). 존재하지 않아도 별칭이 맞는다.
        aliases.write_text(json.dumps({'alpha': ['/work/alpha'], 'beta': ['/work/beta']}), encoding='utf-8')
        env = patch.dict(os.environ, {'SB_RULES_DIR': str(self.rules), 'SB_PROJECT_ALIASES': str(aliases),
                                      'SB_HOME': str(self.sbhome)})
        env.start()
        self.addCleanup(env.stop)
        home = patch.object(automemory, '_home_slug', return_value='-Users-demo')
        home.start()
        self.addCleanup(home.stop)

    def project(self, slug: str, cwd, memories: dict) -> Path:
        project = self.root / slug
        memory = project / 'memory'
        memory.mkdir(parents=True)
        for name, text in memories.items():
            (memory / name).write_text(text, encoding='utf-8')
        if cwd is not None:
            lines = [json.dumps({'type': 'summary'}), 'not json', json.dumps({'cwd': cwd, 'type': 'user'})]
            (project / 'session.jsonl').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return memory

    def run_main(self, *args: str) -> dict:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sb_rules.main(['--root', str(self.root), '--json'] + list(args)), 0)
        return json.loads(out.getvalue())

    def standard(self) -> None:
        self.project('-Users-demo-alpha', '/work/alpha', {
            'MEMORY.md': '- index\n',
            'new.md': FEEDBACK.format(name='new', title='알파 새 규칙', body='스모크 테스트를 먼저 돌린다'),
            'legacy.md': LEGACY.format(name='legacy', title='알파 예전 형식 규칙', body='mock 금지'),
            'profile.md': USER.format(name='profile', title='사용자 정보', body='feedback 아님'),
        })
        self.project('-Users-demo-beta', '/work/beta', {
            'rule.md': FEEDBACK.format(name='rule', title='베타 규칙', body='배포 전 확인'),
        })
        self.project('-Users-demo-gamma', '/work/gamma', {
            'note.md': USER.format(name='note', title='감마 메모', body='규칙 없음'),
        })

    def test_scope_files_and_legacy_type(self) -> None:
        self.standard()
        report = self.run_main()
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['rules'], 3)
        self.assertEqual(sorted(report['written']), ['alpha.md', 'beta.md'])
        self.assertEqual(sorted(p.name for p in self.rules.iterdir()), ['alpha.md', 'beta.md'])
        alpha = (self.rules / 'alpha.md').read_text(encoding='utf-8')
        self.assertEqual(alpha.splitlines()[0], sb_rules.MARKER)
        self.assertIn('# 규칙 — alpha', alpha)
        self.assertIn('## 알파 새 규칙\n스모크 테스트를 먼저 돌린다', alpha)
        self.assertIn('## 알파 예전 형식 규칙\nmock 금지', alpha)   # 최상위 type: 형식도 feedback 으로 읽는다
        self.assertNotIn('사용자 정보', alpha)
        self.assertIn('출처: ' + str(self.root / '-Users-demo-alpha' / 'memory' / 'legacy.md'), alpha)
        self.assertNotIn('알파', (self.rules / 'beta.md').read_text(encoding='utf-8'))
        # sb search --mode rules 가 같은 파일을 읽는다(이름 규칙 일치)
        envelope = sb_search._rules_items('alpha')
        self.assertEqual(envelope['status'], 'ok')
        self.assertIn('알파 새 규칙', envelope['items'][0]['body'])

    def test_hand_written_kept_and_stale_generated_removed(self) -> None:
        self.standard()
        self.rules.mkdir()
        (self.rules / 'alpha.md').write_text('# 손으로 쓴 알파 규칙\n', encoding='utf-8')
        (self.rules / 'notes.md').write_text('# 손으로 쓴 메모\n', encoding='utf-8')
        (self.rules / 'gone.md').write_text(sb_rules.MARKER + '\n# 규칙 — gone\n', encoding='utf-8')
        report = self.run_main()
        self.assertEqual(report['kept'], ['alpha.md'])
        self.assertEqual(report['removed'], ['gone.md'])
        self.assertEqual(report['written'], ['beta.md'])
        self.assertEqual((self.rules / 'alpha.md').read_text(encoding='utf-8'), '# 손으로 쓴 알파 규칙\n')
        self.assertEqual((self.rules / 'notes.md').read_text(encoding='utf-8'), '# 손으로 쓴 메모\n')
        self.assertFalse((self.rules / 'gone.md').exists())

    def test_rerun_is_unchanged_and_dry_run_writes_nothing(self) -> None:
        self.standard()
        self.run_main()
        before = (self.rules / 'beta.md').read_bytes()
        self.assertEqual(sorted(self.run_main()['unchanged']), ['alpha.md', 'beta.md'])
        (self.root / '-Users-demo-beta' / 'memory' / 'rule.md').write_text(
            FEEDBACK.format(name='rule', title='베타 규칙', body='바뀐 본문'), encoding='utf-8')
        (self.rules / 'old.md').write_text(sb_rules.MARKER + '\n', encoding='utf-8')
        report = self.run_main('--dry-run')
        self.assertEqual((report['written'], report['removed']), (['beta.md'], ['old.md']))
        self.assertEqual((self.rules / 'beta.md').read_bytes(), before)
        self.assertTrue((self.rules / 'old.md').exists())
        self.assertEqual(self.run_main()['written'], ['beta.md'])
        self.assertIn('바뀐 본문', (self.rules / 'beta.md').read_text(encoding='utf-8'))
        self.assertEqual([p.name for p in self.rules.iterdir() if p.suffix == '.tmp'], [])

    def test_empty_input_and_missing_root(self) -> None:
        report = self.run_main()
        self.assertEqual((report['status'], report['rules'], report['written']), ('ok', 0, []))
        self.assertFalse(self.rules.exists())
        # 원천 폴더가 없으면 기존 생성물을 지우지 않는다(설치 직후·경로 오류).
        self.rules.mkdir()
        (self.rules / 'alpha.md').write_text(sb_rules.MARKER + '\n', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            sb_rules.main(['--root', str(self.base / 'absent'), '--json'])
        self.assertEqual(json.loads(out.getvalue())['status'], 'no_source')
        self.assertTrue((self.rules / 'alpha.md').exists())
        # 원천은 있는데 feedback 이 하나도 없으면 생성물은 지운다.
        self.assertEqual(self.run_main()['removed'], ['alpha.md'])

    def test_size_limit_truncates_and_omits(self) -> None:
        memories = {'r{:02d}.md'.format(i): FEEDBACK.format(name='r{}'.format(i), title='규칙 {}'.format(i),
                                                             body='가' * 1500) for i in range(30)}
        self.project('-Users-demo-alpha', '/work/alpha', memories)
        self.run_main()
        data = (self.rules / 'alpha.md').read_bytes()
        self.assertLessEqual(len(data), 20000)   # sb search 가 읽는 앞 20KB 안에 들어간다
        text = data.decode('utf-8')
        self.assertIn('가' * sb_rules.MAX_BODY + ' …(이하 출처 참조)', text)
        self.assertNotIn('가' * (sb_rules.MAX_BODY + 1), text)
        self.assertRegex(text, r'용량 한도로 \d+건 생략')

    def test_missing_or_temp_cwd_skipped_and_excluded_project(self) -> None:
        self.project('-Users-demo-nocwd', None, {'a.md': FEEDBACK.format(name='a', title='t', body='b')})
        self.project('-Users-demo-scratch', os.path.join(tempfile.gettempdir(), 'scratch'),
                     {'a.md': FEEDBACK.format(name='a', title='t', body='b')})
        self.project('-Users-demo-private', '/work/beta', {'a.md': FEEDBACK.format(name='a', title='비공개', body='b')})
        (self.sbhome / 'config').mkdir(parents=True)
        (self.sbhome / 'config' / 'automemory_exclude.txt').write_text('private\n', encoding='utf-8')
        report = self.run_main()
        self.assertEqual(sorted((s['dir'], s['reason']) for s in report['skipped']),
                         [('-Users-demo-nocwd', 'no_cwd'), ('-Users-demo-scratch', 'temp_cwd')])
        self.assertEqual((report['rules'], report['written']), (0, []))

    def test_lost_session_record_keeps_previous_scope(self) -> None:
        # Claude Code 가 오래된 대화 기록(jsonl)을 정리해도 전에 만든 규칙층은 남고, 메모리 변경은 계속 반영된다.
        self.standard()
        self.run_main()
        (self.root / '-Users-demo-alpha' / 'session.jsonl').unlink()
        (self.root / '-Users-demo-alpha' / 'memory' / 'added.md').write_text(
            FEEDBACK.format(name='added', title='알파 추가 규칙', body='나중에 생김'), encoding='utf-8')
        report = self.run_main()
        self.assertEqual((report['removed'], report['written'], report['skipped']), ([], ['alpha.md'], []))
        alpha = (self.rules / 'alpha.md').read_text(encoding='utf-8')
        for title in ('알파 새 규칙', '알파 예전 형식 규칙', '알파 추가 규칙'):
            self.assertIn('## ' + title, alpha)
        # 그 폴더의 feedback 이 모두 사라지면 그때는 지운다
        for name in ('new.md', 'legacy.md', 'added.md'):
            (self.root / '-Users-demo-alpha' / 'memory' / name).unlink()
        self.assertEqual(self.run_main()['removed'], ['alpha.md'])

    def test_lost_session_record_in_shared_scope_keeps_its_rules(self) -> None:
        self.project('-Users-demo-alpha', '/work/alpha', {'a.md': FEEDBACK.format(name='a', title='첫째', body='1')})
        self.project('-Users-demo-alpha-sub', '/work/alpha/sub', {'b.md': FEEDBACK.format(name='b', title='둘째', body='2')})
        self.run_main()
        (self.root / '-Users-demo-alpha-sub' / 'session.jsonl').unlink()
        self.assertEqual(self.run_main()['unchanged'], ['alpha.md'])
        self.assertIn('## 둘째', (self.rules / 'alpha.md').read_text(encoding='utf-8'))
        # 손으로 쓴 파일의 '출처:' 줄은 scope 근거로 쓰지 않는다
        (self.rules / 'alpha.md').write_text('# 규칙 — alpha\n\n출처: {}\n'.format(
            self.root / '-Users-demo-alpha-sub' / 'memory' / 'b.md'), encoding='utf-8')
        self.assertEqual(sb_rules.previous_scopes(), {})

    def test_temp_roots_shared_with_automemory(self) -> None:
        self.assertIs(sb_rules.temp_roots, automemory.temp_roots)
        self.assertTrue(sb_rules._is_temp(os.path.join(tempfile.gettempdir(), 'scratch')))
        self.assertFalse(sb_rules._is_temp('/work/alpha'))

    def test_same_scope_from_two_folders_merges(self) -> None:
        self.project('-Users-demo-alpha', '/work/alpha', {'a.md': FEEDBACK.format(name='a', title='첫째', body='1')})
        self.project('-Users-demo-alpha-sub', '/work/alpha/sub', {'b.md': FEEDBACK.format(name='b', title='둘째', body='2')})
        self.assertEqual(self.run_main()['written'], ['alpha.md'])
        text = (self.rules / 'alpha.md').read_text(encoding='utf-8')
        self.assertIn('## 첫째', text)
        self.assertIn('## 둘째', text)
        self.assertIn('메모리 2건', text)

    def test_sb_cli_routes(self) -> None:
        self.standard()
        result = subprocess.run([sys.executable, str(BIN / 'sb.py'), 'rules', '--root', str(self.root),
                                 '--dry-run', '--json'], capture_output=True, text=True, encoding='utf-8',
                                timeout=60, env={**os.environ, 'PYTHONUTF8': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(json.loads(result.stdout)['written']), ['alpha.md', 'beta.md'])
        self.assertFalse(self.rules.exists())
        import sb
        self.assertEqual((sb.ROUTES['rules'], sb.ROUTES['context']), ('sb_rules.py', 'sb_context.py'))


if __name__ == '__main__':
    unittest.main()
