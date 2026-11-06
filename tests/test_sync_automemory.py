import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Tuple
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import sb_memory
import sync_automemory as automemory

REAL_HOME_SLUG = automemory._home_slug  # setUp pins the module attribute


class AutomemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'projects'
        self.state_path = self.base / 'state.json'
        self.alpha = self.root / '-Users-demo-sample-app' / 'memory'
        self.beta = self.root / '-Users-demo-secondbrain' / 'memory'
        self.alpha.mkdir(parents=True)
        self.beta.mkdir(parents=True)
        self.topic = self.alpha / 'preferences.md'
        self.topic.write_text(
            '---\nname: coding\ndescription: "개발: 선호"\nmetadata:\n'
            '  type: feedback\n---\n본문 보존\n', encoding='utf-8')
        self.plain = self.beta / 'plain.md'
        self.plain.write_text('No frontmatter.\n', encoding='utf-8')
        (self.alpha / 'MEMORY.md').write_text('- preferences.md\n', encoding='utf-8')
        (self.beta / 'MEMORY.md').write_text('- plain.md\n', encoding='utf-8')
        (self.beta / 'ignore.txt').write_text('ignore', encoding='utf-8')
        env = patch.dict(os.environ, {
            'SB_AUTOMEM_STATE': str(self.state_path),
            'SB_CLAUDE_MEM_DB': str(self.base / 'absent.db'),
            'SB_MEM_JOURNAL': str(self.base / 'journal.jsonl'),
            'SB_MEM_BASE_URL': 'http://fake.invalid',
        })
        env.start()
        self.addCleanup(env.stop)
        # Fixtures use macOS-style slugs; pin the home slug so tests pass on any machine/OS.
        home = patch.object(automemory, '_home_slug', return_value='-Users-demo')
        home.start()
        self.addCleanup(home.stop)
        post = patch.object(sb_memory, '_post', side_effect=self.fake_post)
        self.post = post.start()
        self.addCleanup(post.stop)
        self.next_id = 100

    def fake_post(self, url: str, payload: Dict[str, Any], timeout: int) -> Tuple[int, str]:
        self.assertEqual(url, 'http://fake.invalid/api/memory/save')
        self.assertEqual(timeout, 10)
        self.next_id += 1
        return 200, json.dumps({'success': True, 'id': self.next_id})

    def run_sync(self, **kwargs: Any) -> Dict[str, Any]:
        return automemory.sync(root=str(self.root), state_path=str(self.state_path), **kwargs)

    def test_slug_conversion(self) -> None:
        self.assertEqual(automemory.project_from_slug('-Users-demo-sample-app'),
                         'sample-app')
        self.assertEqual(automemory.project_from_slug('old-Users-demo-a-Users-demo-b'),
                         'old-Users-demo-a-Users-demo-b')
        self.assertEqual(automemory.project_from_slug('-old-Users-demo-a-Users-demo-b'), 'b')
        self.assertEqual(automemory.project_from_slug('other-slug'), 'other-slug')

    def test_home_slug_conversion(self) -> None:
        for slug in ('-Users-demo', '-Users-demo-'):
            with self.subTest(slug=slug):
                self.assertEqual(automemory.project_from_slug(slug), 'home')
        self.assertEqual(automemory.project_from_slug(''), '')

    def test_windows_like_home_slug(self) -> None:
        home = 'C--Users-kim'
        for flag in (False, True):
            with self.subTest(windows=flag), patch.object(automemory, 'IS_WINDOWS', flag):
                self.assertEqual(automemory.project_from_slug('C--Users-kim-proj', home), 'proj')
                self.assertEqual(automemory.project_from_slug('C--Users-kim-my-app', home), 'my-app')
                self.assertEqual(automemory.project_from_slug('C--Users-kim', home), 'home')
                self.assertEqual(automemory.project_from_slug('C--Users-kim-', home), 'home')
                # Outside home (other drive) or a sibling user whose name shares the prefix.
                self.assertEqual(automemory.project_from_slug('D--work-proj', home), 'D--work-proj')
                self.assertEqual(automemory.project_from_slug('C--Users-kimberly-x', home),
                                 'C--Users-kimberly-x')
        with patch.object(automemory, 'IS_WINDOWS', True):
            self.assertEqual(automemory.project_from_slug('c--Users-kim-proj', home), 'proj')
        with patch.object(automemory, 'IS_WINDOWS', False):
            self.assertEqual(automemory.project_from_slug('c--Users-kim-proj', home),
                             'c--Users-kim-proj')

    def test_home_slug_matches_claude_code_rule(self) -> None:
        with patch.object(automemory.Path, 'home', return_value=Path('/Users/kim.lee')):
            self.assertEqual(REAL_HOME_SLUG(), '-Users-kim-lee')

    def test_discover_windows_like_root(self) -> None:
        win = self.base / 'win-projects' / 'C--Users-kim-proj' / 'memory'
        win.mkdir(parents=True)
        (win / 'note.md').write_text('body\n', encoding='utf-8')
        with patch.object(automemory, '_home_slug', return_value='C--Users-kim'):
            self.assertEqual(automemory.discover(str(self.base / 'win-projects')),
                             [('proj', str(win / 'note.md'))])

    def test_default_state_path_under_sb_home(self) -> None:
        with patch.dict(os.environ, {'SB_HOME': str(self.base / 'sbhome')}):
            os.environ.pop('SB_AUTOMEM_STATE', None)
            self.assertEqual(automemory._state_path(),
                             self.base / 'sbhome' / 'index' / 'automemory_state.json')

    def test_parser_frontmatter_and_plain_file(self) -> None:
        self.assertEqual(automemory.parse_memory_file(str(self.topic)), {
            'name': 'coding', 'description': '개발: 선호', 'type': 'feedback',
            'body': '본문 보존\n', 'title': '개발: 선호'})
        self.assertEqual(automemory.parse_memory_file(str(self.plain)), {
            'name': 'plain', 'description': '', 'type': 'unknown',
            'body': 'No frontmatter.\n', 'title': 'plain'})

    def test_parser_legacy_top_level_type(self) -> None:
        # 예전 형식: metadata 블록 없이 최상위 type: — unknown 이 아니라 그 값을 읽는다.
        self.plain.write_text('---\nname: old\ndescription: 예전 규칙\ntype: "feedback"\n---\n본문\n',
                              encoding='utf-8')
        self.assertEqual(automemory.parse_memory_file(str(self.plain))['type'], 'feedback')
        # 둘 다 있으면 순서와 상관없이 metadata.type 이 우선한다.
        for text in ('---\ntype: user\nmetadata:\n  type: feedback\n---\nb\n',
                     '---\nmetadata:\n  type: feedback\ntype: user\n---\nb\n'):
            self.plain.write_text(text, encoding='utf-8')
            self.assertEqual(automemory.parse_memory_file(str(self.plain))['type'], 'feedback', text)
        # 본문 안의 type: 줄은 프론트매터가 아니다.
        self.plain.write_text('---\nname: x\n---\ntype: feedback\n', encoding='utf-8')
        self.assertEqual(automemory.parse_memory_file(str(self.plain))['type'], 'unknown')

    def test_legacy_type_reaches_saved_text(self) -> None:
        self.plain.write_text('---\nname: old\ndescription: 예전 규칙\ntype: feedback\n---\n본문\n',
                              encoding='utf-8')
        automemory.sync(root=str(self.root), state_path=str(self.state_path), only_project='secondbrain')
        payload = self.post.call_args.args[1]
        self.assertTrue(payload['text'].startswith('[자동 메모리 · type=feedback · file=plain.md]'))
        self.assertEqual(payload['metadata']['extra']['type'], 'feedback')

    def test_parser_unterminated_frontmatter_preserves_body(self) -> None:
        text = '---\nname: incomplete\n본문\n'
        self.plain.write_text(text, encoding='utf-8')
        parsed = automemory.parse_memory_file(str(self.plain))
        self.assertEqual(parsed['body'], text)
        self.assertEqual(parsed['title'], 'plain')

    def test_first_save_then_unchanged_and_source_read_only(self) -> None:
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(automemory.discover(str(self.root)), [
            ('sample-app', str(self.topic)), ('secondbrain', str(self.plain))])
        first = self.run_sync()
        self.assertEqual((first['scanned'], first['saved'], first['failed']), (2, 2, 0))
        self.assertEqual(self.post.call_count, 2)
        payload = self.post.call_args_list[0].args[1]
        digest = sb_memory.content_hash('본문 보존\n', '개발: 선호\nfeedback')   # type 도 해시에 든다
        self.assertEqual(payload['text'],
                         '[자동 메모리 · type=feedback · file=preferences.md]\n본문 보존\n')
        self.assertEqual(payload['title'], '[automemory] sample-app · 개발: 선호')
        self.assertEqual(payload['project'], 'sample-app')
        metadata = payload['metadata']
        for key, value in {'source': 'claude-code-automemory', 'kind': 'automemory-sync',
                           'origin': 'sync_automemory', 'created_by': 'secondbrain-batch',
                           'sid': str(self.topic) + '#' + digest, 'content_hash': digest,
                           'verification': 'user-authored'}.items():
            self.assertEqual(metadata[key], value)
        self.assertEqual(metadata['extra'], {'type': 'feedback', 'name': 'coding',
                                             'path': str(self.topic), 'supersedes': None})
        second = self.run_sync()
        self.assertEqual((second['unchanged'], second['saved']), (2, 0))
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_changed_file_accumulates_supersedes(self) -> None:
        self.run_sync()
        old_id = automemory.load_state()[str(self.topic)]['obs_id']
        with self.topic.open('a', encoding='utf-8') as stream:
            stream.write('second version\n')
        result = self.run_sync()
        self.assertEqual((result['saved'], result['unchanged']), (1, 1))
        self.assertEqual(self.post.call_args.args[1]['metadata']['extra']['supersedes'], old_id)
        state = automemory.load_state()[str(self.topic)]
        self.assertEqual(state['supersedes'], [old_id])
        second_id = state['obs_id']
        with self.topic.open('a', encoding='utf-8') as stream:
            stream.write('third version\n')
        self.run_sync()
        self.assertEqual(automemory.load_state()[str(self.topic)]['supersedes'], [old_id, second_id])

    def old_state(self) -> Dict[str, Any]:
        """type 칸이 없던 예전 상태(해시는 본문·제목만)로 되돌린다."""
        state = automemory.load_state()
        for path, entry in state.items():
            memory = automemory.parse_memory_file(path)
            entry['hash'] = sb_memory.content_hash(memory['body'], memory['title'])
            del entry['type']
        automemory.save_state(state, str(self.state_path))
        return state

    def test_old_state_migrates_without_resaving(self) -> None:
        # 해시 형식이 바뀌어도 기존 사용자가 첫 동기화에 전 파일을 다시 저장하지 않는다
        self.run_sync()
        before = self.old_state()
        self.assertEqual(self.run_sync(dry_run=True)['unchanged'], 2)
        self.assertEqual(automemory.load_state(), before)   # dry-run 은 상태도 안 바꾼다
        result = self.run_sync()
        self.assertEqual((result['unchanged'], result['saved'], self.post.call_count), (2, 0, 2))
        state = automemory.load_state()
        self.assertEqual(state[str(self.topic)]['type'], 'feedback')
        self.assertEqual(state[str(self.topic)]['obs_id'], before[str(self.topic)]['obs_id'])
        self.assertEqual(state[str(self.plain)]['hash'],
                         automemory.memory_hash(automemory.parse_memory_file(str(self.plain))))
        self.assertEqual(self.run_sync()['unchanged'], 2)

    def test_old_state_legacy_type_file_resynced_once(self) -> None:
        # 예전 파서가 type=unknown 으로 올린 최상위 type: 파일만 교정된 type 으로 다시 올린다(나머지는 그대로)
        self.plain.write_text('---\nname: old\ndescription: 예전 규칙\ntype: feedback\n---\n본문\n', encoding='utf-8')
        self.run_sync()
        old_id = automemory.load_state()[str(self.plain)]['obs_id']
        self.old_state()
        result = self.run_sync()
        self.assertEqual((result['saved'], result['unchanged']), (1, 1))
        payload = self.post.call_args.args[1]
        self.assertTrue(payload['text'].startswith('[자동 메모리 · type=feedback · file=plain.md]'))
        self.assertEqual(payload['metadata']['extra']['supersedes'], old_id)
        self.assertEqual(automemory.load_state()[str(self.plain)]['supersedes'], [old_id])
        self.assertEqual(self.run_sync()['saved'], 0)

    def test_type_only_change_resyncs(self) -> None:
        self.run_sync()
        self.topic.write_text(self.topic.read_text(encoding='utf-8').replace('type: feedback', 'type: user'),
                              encoding='utf-8')
        result = self.run_sync()
        self.assertEqual((result['saved'], result['unchanged']), (1, 1))
        self.assertTrue(self.post.call_args.args[1]['text'].startswith('[자동 메모리 · type=user'))

    def test_discover_skips_temp_folder_projects(self) -> None:
        slug = lambda path: re.sub(r'[^A-Za-z0-9]', '-', path)  # noqa: E731
        temp_slugs = ['-tmp-scratch', '-private-tmp-run1', '-private-var-folders-ab-cd-T-tmpxyz',
                      slug(os.path.join(tempfile.gettempdir(), 'tmpproj'))]   # 윈도우에서는 %TEMP% 아래
        for name in temp_slugs + ['-Users-demo-tmp-notes', '-tmpwork']:
            folder = self.root / name / 'memory'
            folder.mkdir(parents=True)
            (folder / 'note.md').write_text('body\n', encoding='utf-8')
        found = {Path(path).parent.parent.name for _, path in automemory.discover(str(self.root))}
        self.assertFalse(found & set(temp_slugs))
        self.assertTrue({'-Users-demo-tmp-notes', '-tmpwork', '-Users-demo-sample-app'} <= found)
        with patch.object(automemory, 'IS_WINDOWS', True), \
                patch.object(automemory, 'temp_roots', return_value=['C:\\Users\\kim\\AppData\\Local\\Temp']):
            self.assertTrue(automemory.is_temp_slug('c--users-kim-AppData-Local-Temp-proj'))
            self.assertFalse(automemory.is_temp_slug('C--Users-kim-proj'))

    def test_dry_run_has_no_post_or_state_write(self) -> None:
        result = self.run_sync(dry_run=True)
        self.assertEqual(result['scanned'], 2)
        self.assertEqual([d['action'] for d in result['details']], ['would_save', 'would_save'])
        self.post.assert_not_called()
        self.assertFalse(self.state_path.exists())
        self.assertFalse((self.base / 'journal.jsonl').exists())

    def test_duplicate_does_not_record_negative_observation_id(self) -> None:
        with patch.object(sb_memory, 'already_saved', return_value=True):
            result = self.run_sync()
        self.assertEqual((result['skipped_dup'], result['saved'], result['failed']), (2, 0, 0))
        self.post.assert_not_called()
        self.assertEqual(automemory.load_state(), {})

    def test_runtime_failure_continues_and_main_returns_one(self) -> None:
        self.post.side_effect = [RuntimeError('fake failure'), (200, '{"success":true,"id":500}')]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = automemory.main(['--root', str(self.root), '--json'])
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual((result['failed'], result['saved']), (1, 1))
        self.assertNotIn(str(self.topic), automemory.load_state())
        self.assertEqual(automemory.load_state()[str(self.plain)]['obs_id'], 500)

    def test_deletion_is_scoped_and_reappearance_clears_marker(self) -> None:
        self.run_sync()
        original = self.topic.read_text(encoding='utf-8')
        self.topic.unlink()
        self.plain.unlink()
        before = self.state_path.read_bytes()
        self.run_sync(dry_run=True)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.run_sync(only_project='sample-app')
        state = automemory.load_state()
        self.assertIn('deleted_at', state[str(self.topic)])
        self.assertNotIn('deleted_at', state[str(self.plain)])
        automemory.sync(root=str(self.base / 'other-root'), state_path=str(self.state_path))
        self.assertEqual(automemory.load_state(), state)
        self.topic.write_text(original, encoding='utf-8')
        self.run_sync(only_project='sample-app')
        self.assertNotIn('deleted_at', automemory.load_state()[str(self.topic)])
        self.assertEqual(self.post.call_count, 2)

    def test_cli_project_dry_run_json(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = automemory.main(['--root', str(self.root), '--project', 'secondbrain',
                                   '--dry-run', '--json'])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['scanned'], 1)
        self.assertEqual(result['details'][0]['project'], 'secondbrain')
        self.post.assert_not_called()

    def test_state_atomic_replace_preserves_previous_file_on_failure(self) -> None:
        automemory.save_state({'before': {}}, str(self.state_path))
        with patch.object(automemory.os, 'replace', side_effect=OSError('fake replace failure')):
            with self.assertRaises(OSError):
                automemory.save_state({'after': {}}, str(self.state_path))
        self.assertEqual(automemory.load_state(), {'before': {}})
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), ['projects', 'state.json'])


LONG_BODY = '\n\n'.join('## 구간 {}\n'.format(i) + ('내용 문장 {} 입니다. '.format(i) * 18) for i in range(1, 7))


class SplitBodyTests(unittest.TestCase):
    def test_short_body_stays_whole(self) -> None:
        self.assertEqual(automemory.split_body('짧은 본문', 800), ['짧은 본문'])

    def test_zero_cap_disables_split(self) -> None:
        self.assertEqual(automemory.split_body(LONG_BODY, 0), [LONG_BODY])

    def test_every_part_fits_cap_and_keeps_content(self) -> None:
        parts = automemory.split_body(LONG_BODY, 300)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 300 for part in parts))
        squeeze = lambda text: re.sub(r'\s', '', text)  # noqa: E731
        self.assertEqual(squeeze(''.join(parts)), squeeze(LONG_BODY))

    def test_parts_start_at_headings_when_possible(self) -> None:
        parts = automemory.split_body(LONG_BODY, 300)
        self.assertTrue(parts[0].startswith('## 구간 1'))
        self.assertTrue(any(part.startswith('## 구간 2') for part in parts[1:]))

    def test_single_long_line_is_sliced(self) -> None:
        parts = automemory.split_body('가' * 2000, 800)
        self.assertEqual([len(part) for part in parts], [800, 800, 400])


class AutomemorySplitTests(unittest.TestCase):
    fake_post = AutomemoryTests.fake_post
    run_sync = AutomemoryTests.run_sync

    def setUp(self) -> None:
        AutomemoryTests.setUp(self)
        self.long = self.alpha / 'handoff.md'
        self.long.write_text('---\nname: handoff\ndescription: 인계 문서\nmetadata:\n  type: project\n---\n'
                             + LONG_BODY + '\n', encoding='utf-8')
        env = patch.dict(os.environ, {'SB_AUTOMEM_CHUNK_CHARS': '300'})
        env.start()
        self.addCleanup(env.stop)

    def saved_titles(self) -> list:
        return [call.args[1]['title'] for call in self.post.call_args_list]

    def long_entry(self) -> Dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding='utf-8'))[str(self.long)]

    def test_long_file_becomes_numbered_parts(self) -> None:
        self.run_sync(only_project='sample-app')
        entry = self.long_entry()
        parts = len(entry['obs_ids'])
        self.assertGreater(parts, 1)
        self.assertEqual(entry['obs_id'], entry['obs_ids'][0])
        self.assertEqual(entry['split'], 300)
        titles = [t for t in self.saved_titles() if '인계 문서' in t]
        self.assertEqual(len(titles), parts)
        self.assertEqual(len(set(titles)), parts)   # 근사 중복 접기에 묶이지 않게 제목이 달라야 한다
        self.assertTrue(titles[1].endswith('(2/{})'.format(parts)))

    def test_part_text_repeats_memory_title(self) -> None:
        self.run_sync(only_project='sample-app')
        texts = [c.args[1]['text'] for c in self.post.call_args_list if '인계 문서' in c.args[1]['title']]
        self.assertIn('조각 2/', texts[1])
        self.assertTrue(all('인계 문서' in text for text in texts))   # 벡터엔 본문만 들어가 제목을 본문에도 넣는다

    def test_short_file_keeps_old_shape(self) -> None:
        self.run_sync(only_project='sample-app')
        state = json.loads(self.state_path.read_text(encoding='utf-8'))
        entry = state[str(self.topic)]
        self.assertEqual(entry['split'], 0)
        self.assertEqual(entry['obs_ids'], [entry['obs_id']])
        titles = [t for t in self.saved_titles() if '선호' in t]
        self.assertEqual(titles, ['[automemory] sample-app · 개발: 선호'])

    def test_second_run_saves_nothing(self) -> None:
        self.run_sync()
        calls = self.post.call_count
        result = self.run_sync()
        self.assertEqual(self.post.call_count, calls)
        self.assertEqual(result['saved'], 0)

    def test_existing_whole_record_is_migrated_once(self) -> None:
        with patch.dict(os.environ, {'SB_AUTOMEM_CHUNK_CHARS': '0'}):
            self.run_sync(only_project='sample-app')
        old = self.long_entry()['obs_id']
        result = self.run_sync(dry_run=True, only_project='sample-app')
        pending = [d for d in result['details'] if d.get('action') == 'would_save']
        self.assertEqual([d['path'] for d in pending], [str(self.long)])
        self.run_sync(only_project='sample-app')
        entry = self.long_entry()
        self.assertIn(old, entry['supersedes'])
        self.assertGreater(len(entry['obs_ids']), 1)
        calls = self.post.call_count
        self.run_sync(only_project='sample-app')
        self.assertEqual(self.post.call_count, calls)

    def test_turning_split_off_restores_whole_record(self) -> None:
        saved: Dict[str, int] = {}
        real = self.fake_post

        def tracking(url: str, payload: Dict[str, Any], timeout: int) -> Tuple[int, str]:
            status, body = real(url, payload, timeout)
            saved[payload['metadata']['dedup_key']] = json.loads(body)['id']
            return status, body

        self.post.side_effect = tracking
        with patch.object(sb_memory, 'already_saved', side_effect=lambda key, db=None: key in saved), \
                patch.object(automemory, 'find_saved_id', side_effect=saved.get):
            with patch.dict(os.environ, {'SB_AUTOMEM_CHUNK_CHARS': '0'}):
                self.run_sync(only_project='sample-app')
            whole = self.long_entry()['obs_id']
            self.run_sync(only_project='sample-app')
            parts = self.long_entry()['obs_ids']
            with patch.dict(os.environ, {'SB_AUTOMEM_CHUNK_CHARS': '0'}):
                self.run_sync(only_project='sample-app')
        entry = self.long_entry()
        self.assertEqual(entry['obs_ids'], [whole])
        self.assertNotIn(whole, entry['supersedes'])   # 다시 쓰는 기록이 회수에서 숨겨지면 안 된다
        self.assertTrue(set(parts) <= set(entry['supersedes']))
        self.assertEqual(entry['split'], 0)

    def test_changed_file_supersedes_every_old_part(self) -> None:
        self.run_sync(only_project='sample-app')
        old = self.long_entry()['obs_ids']
        self.long.write_text(self.long.read_text(encoding='utf-8') + '\n추가 문단\n', encoding='utf-8')
        self.run_sync(only_project='sample-app')
        entry = self.long_entry()
        self.assertTrue(set(old) <= set(entry['supersedes']))
        self.assertFalse(set(old) & set(entry['obs_ids']))

    def test_failed_part_keeps_state_and_resumes_without_duplicates(self) -> None:
        saved: Dict[str, int] = {}
        real = self.fake_post

        def flaky(url: str, payload: Dict[str, Any], timeout: int) -> Tuple[int, str]:
            if '(3/' in payload['title'] and not hasattr(self, 'failed_once'):
                raise OSError('worker down')
            status, body = real(url, payload, timeout)
            saved[payload['metadata']['dedup_key']] = json.loads(body)['id']
            return status, body

        self.post.side_effect = flaky
        with patch.object(sb_memory, 'already_saved', side_effect=lambda key, db=None: key in saved), \
                patch.object(automemory, 'find_saved_id', side_effect=saved.get), \
                patch.object(sb_memory.time, 'sleep'):
            first = self.run_sync(only_project='sample-app')
            self.assertEqual(first['failed'], 1)
            self.assertNotIn('hash', self.long_entry())
            self.assertEqual(len(self.long_entry()['pending']), 2)
            self.failed_once = True
            before = len(saved)
            self.run_sync(only_project='sample-app')
        entry = self.long_entry()
        self.assertEqual(len(entry['obs_ids']), len(set(entry['obs_ids'])))
        self.assertTrue(set(list(saved.values())[:before]) <= set(entry['obs_ids']) | {self.state_topic_id()})

    def fail_third_part_once(self) -> None:
        real = self.fake_post
        self.failed_once = False

        def flaky(url: str, payload: Dict[str, Any], timeout: int) -> Tuple[int, str]:
            if '(3/' in payload['title'] and not self.failed_once:
                raise OSError('worker down')
            return real(url, payload, timeout)

        self.post.side_effect = flaky

    def test_parts_of_abandoned_version_are_hidden_after_edit(self) -> None:
        self.fail_third_part_once()
        with patch.object(sb_memory.time, 'sleep'):
            self.run_sync(only_project='sample-app')
        orphans = self.long_entry()['pending']
        self.failed_once = True
        self.long.write_text(self.long.read_text(encoding='utf-8') + '\n수정\n', encoding='utf-8')
        self.run_sync(only_project='sample-app')
        entry = self.long_entry()
        self.assertTrue(set(orphans) <= set(entry['supersedes']))
        self.assertNotIn('pending', entry)

    def test_parts_of_failed_save_are_hidden_when_file_is_deleted(self) -> None:
        self.fail_third_part_once()
        with patch.object(sb_memory.time, 'sleep'):
            self.run_sync(only_project='sample-app')
        orphans = self.long_entry()['pending']
        self.long.unlink()
        self.run_sync(only_project='sample-app')
        self.assertTrue(set(orphans) <= set(self.long_entry()['supersedes']))

    def test_chunk_size_change_never_reuses_old_parts(self) -> None:
        self.run_sync(only_project='sample-app')
        keys = {c.args[1]['metadata']['dedup_key'] for c in self.post.call_args_list}
        with patch.dict(os.environ, {'SB_AUTOMEM_CHUNK_CHARS': '290'}):
            self.run_sync(only_project='sample-app')
        new = {c.args[1]['metadata']['dedup_key'] for c in self.post.call_args_list} - keys
        self.assertEqual(len(new), len(self.long_entry()['obs_ids']))

    def test_part_keeps_leading_indent(self) -> None:
        parts = automemory.split_body('a' * 10 + '\n\n    들여쓴 코드\n', 12)
        self.assertEqual(parts[1], '    들여쓴 코드')

    def state_topic_id(self) -> int:
        return json.loads(self.state_path.read_text(encoding='utf-8'))[str(self.topic)]['obs_id']

    def test_deleted_file_hides_every_part(self) -> None:
        self.run_sync(only_project='sample-app')
        ids = set(self.long_entry()['obs_ids'])
        self.long.unlink()
        self.run_sync(only_project='sample-app')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hooks'))
        import sb_recall
        import sb_recalld
        with patch.object(sb_recalld.sb_config, 'sb_path', return_value=str(self.state_path)):
            self.assertTrue(ids <= sb_recalld.superseded_ids())
        with patch.object(sb_recall.sb_config, 'sb_path', return_value=str(self.state_path)):
            self.assertTrue(ids <= sb_recall.superseded_ids())


if __name__ == '__main__':
    unittest.main()
