import contextlib
import io
import json
import os
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
        digest = sb_memory.content_hash('본문 보존\n', '개발: 선호')
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


if __name__ == '__main__':
    unittest.main()
