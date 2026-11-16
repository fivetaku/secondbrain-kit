"""세션 작업 폴더 나누기(원본·평가 복사본·임시)와 MCP 커넥터 이름 감지. 가짜 홈에서만 돈다."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'bin'))
import sb_sources  # noqa: E402

SECRET = 'tok-SECRET-9c1e'                 # 설정 값에만 넣는 표식 — 어떤 결과에도 나오면 안 된다
URL = 'https://mcp.example.invalid/sse?key=' + SECRET


def write(p: Path, text: str = '{}\n') -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')
    return p


class Home(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix='sb-cwdmcp-')
        self.addCleanup(t.cleanup)
        self.root = Path(os.path.realpath(t.name))
        self.h = self.root / 'home'
        self.h.mkdir()
        env = mock.patch.dict(os.environ, {
            'HOME': str(self.h), 'USERPROFILE': str(self.h), 'SB_HOME': str(self.h / '.secondbrain'),
            'CLAUDE_CONFIG_DIR': str(self.h / '.claude'), 'CODEX_HOME': str(self.h / '.codex'),
            'XDG_DATA_HOME': str(self.h / '.local' / 'share')})
        env.start()
        self.addCleanup(env.stop)

    def claude_session(self, cwd: str, n: int = 1, slug: str = None):
        d = self.h / '.claude' / 'projects' / (slug or cwd.replace('/', '-').replace('\\', '-').replace(':', '-'))
        for i in range(n):
            write(d / ('s%d.jsonl' % i), json.dumps({'type': 'user', 'cwd': cwd}) + '\n')


class CwdClassTests(Home):
    def test_original_eval_temp_split(self):
        work = self.h / 'work'
        (work / 'fin-app' / '.git').mkdir(parents=True)            # 원본(git 루트)
        (work / 'notes').mkdir(parents=True)
        self.claude_session(str(work / 'fin-app'), 2)
        self.claude_session(str(work / 'notes'), 1)
        self.claude_session(str(self.h / '_sbeval' / 'fin-app'), 5)  # 표식 + 같은 이름 → 평가 복사본
        self.claude_session(str(self.h / 'kit-smoketest'), 1)        # *-smoketest
        self.claude_session(str(self.h / 'copies' / 'fin-app'), 3)   # 같은 이름, 원본이 git 루트라 복사본
        self.claude_session('/tmp/run-1', 1)
        self.claude_session('/private/tmp/run-2', 1)
        self.claude_session(os.path.join(sb_sources.sync_automemory.tempfile.gettempdir(), 'job'), 1)
        self.claude_session('C:\\Users\\u\\AppData\\Local\\Temp\\x', 1, slug='C--Users-u-AppData-Local-Temp-x')
        s = {x['kind']: x for x in sb_sources.detect_sessions(str(self.h))}['claude_sessions']['summary']
        self.assertEqual((s['cwd_count'], s['git_count'], s['cwd_eval_copy'], s['cwd_temp']), (2, 1, 3, 4))
        self.assertEqual(s['files'], 2 + 1 + 5 + 1 + 3 + 4)          # 파일 수는 그대로(보고만 나눈다)

    def test_same_name_without_git_keeps_busier(self):
        c = sb_sources.classify_cwds({'/a/proj': 3, '/b/proj': 9, '/c/src': 1, '/d/src': 1}, '/nohome')
        self.assertEqual(c, {'/a/proj': 'eval_copy', '/b/proj': 'original', '/c/src': 'original', '/d/src': 'original'})

    def test_symlink_alias_of_original_stays_original(self):
        work = self.h / 'work' / 'proj'
        work.mkdir(parents=True)
        (self.h / 'copy' / 'proj').mkdir(parents=True)
        try:
            os.symlink(self.h / 'work', self.h / 'alias')
        except (OSError, NotImplementedError):
            self.skipTest('심볼릭 링크를 만들 수 없는 환경')
        c = sb_sources.classify_cwds({str(work): 5, str(self.h / 'alias' / 'proj'): 1,
                                      str(self.h / 'copy' / 'proj'): 2}, str(self.h))
        self.assertEqual(c[str(work)], 'original')
        self.assertEqual(c[str(self.h / 'alias' / 'proj')], 'original')   # 같은 폴더의 다른 표기 — 복사본 아님
        self.assertEqual(c[str(self.h / 'copy' / 'proj')], 'eval_copy')

    def test_home_under_temp_is_not_temp(self):
        tmp_home = self.root / 'h2'
        (tmp_home / 'proj').mkdir(parents=True)
        c = sb_sources.classify_cwds({str(tmp_home / 'proj'): 1, '/tmp/x': 1}, str(tmp_home))
        self.assertEqual(c[str(tmp_home / 'proj')], 'original')     # 격리 홈이 임시 폴더 아래여도 그 안은 원본
        self.assertEqual(c['/tmp/x'], 'temp')

    def test_no_extra_keys_when_all_original(self):
        self.claude_session(str(self.h / 'one'), 1)
        s = sb_sources.detect_sessions(str(self.h))[0]['summary']
        self.assertNotIn('cwd_eval_copy', s)
        self.assertNotIn('cwd_temp', s)

    def test_stage2_metrics_report_split(self):
        self.claude_session(str(self.h / 'w' / 'alpha'), 2)
        self.claude_session(str(self.h / 'sbeval-run' / 'alpha'), 1)
        self.claude_session('/tmp/t1', 1)
        res = sb_sources.stage2({})
        m = {x['name']: x for x in res['metrics']}
        self.assertEqual(res['verdict'], 'pass')
        self.assertEqual((m['session_cwds']['value'], m['session_cwds_eval_copy']['value'],
                          m['session_cwds_temp']['value']), (1, 1, 1))
        self.assertTrue(m['session_cwds_eval_copy']['pass'] and m['session_cwds_temp']['pass'])
        text = sb_sources.sources_path().read_text(encoding='utf-8')
        self.assertNotIn('sbeval-run', text)                          # 경로는 개수만 남긴다


class McpTests(Home):
    def make_configs(self):
        srv = {'url': URL, 'headers': {'Authorization': 'Bearer ' + SECRET}, 'env': {'TOKEN': SECRET}}
        write(self.h / '.claude.json', json.dumps({'mcpServers': {'slack': srv},
                                                    'projects': {'/w/p': {'mcpServers': {'atlassian-confluence': srv}}}}))
        claude = self.h / '.claude'
        write(claude / 'settings.json', json.dumps({'mcpServers': {'my-notion': srv},
                                                    'enabledPlugins': {'off@m': False, 'on@m': True}}))
        cache = claude / 'plugins' / 'cache'
        write(cache / 'm' / 'on' / '1.0' / '.mcp.json', json.dumps({'mcpServers': {'github': srv}}))
        write(cache / 'm' / 'flat' / '1.0' / '.mcp.json', json.dumps({'gdrive': srv}))
        write(cache / 'm' / 'off' / '1.0' / '.mcp.json', json.dumps({'mcpServers': {'disabled-one': srv}}))
        write(self.root / 'outside' / '.mcp.json', json.dumps({'mcpServers': {'outside-one': srv}}))
        write(claude / 'plugins' / 'installed_plugins.json', json.dumps({'version': 2, 'plugins': {
            'on@m': [{'installPath': str(cache / 'm' / 'on' / '1.0')}],
            'flat@m': [{'installPath': str(cache / 'm' / 'flat' / '1.0')}],
            'off@m': [{'installPath': str(cache / 'm' / 'off' / '1.0')}],
            'evil@m': [{'installPath': str(self.root / 'outside')}]}}))
        write(self.h / '.codex' / 'config.toml',
              '[mcp_servers.jira]\ncommand = "npx"\nargs = ["%s"]\n[mcp_servers.jira.env]\nTOKEN = "%s"\n'
              '[mcp_servers."team drive"]\nurl = "%s"\n[profiles.x]\nmodel = "m"\n' % (SECRET, SECRET, URL))
        write(self.h / '.gemini' / 'settings.json', json.dumps({'mcpServers': {'figma': srv}}))
        write(self.h / '.gemini' / 'antigravity' / 'mcp_config.json',
              '{\n  // 주석\n  "mcpServers": {"x": {"url": "%s"}}\n}\n' % URL)

    def test_names_only_categories_and_unreadable(self):
        self.make_configs()
        mcp, bad = sb_sources.detect_mcp(str(self.h))
        names = {s['location'][len('mcp:'):]: s for s in mcp}
        self.assertEqual(set(names), {'slack', 'atlassian-confluence', 'my-notion', 'github', 'gdrive', 'jira',
                                      'team drive', 'figma'})
        cats = {k: v['category'] for k, v in names.items()}
        self.assertEqual(cats, {'slack': 'slack', 'atlassian-confluence': 'atlassian', 'my-notion': 'notion',
                                'github': 'github', 'gdrive': 'google_drive', 'jira': 'atlassian',
                                'team drive': 'google_drive', 'figma': 'figma'})
        for s in mcp:
            self.assertEqual((s['kind'], s['mode'], s['discovered_by']), ('mcp', 'live', 'auto'))
        self.assertEqual(names['jira']['summary']['found_in'], ['codex'])
        self.assertEqual(bad, ['antigravity'])                       # 주석 섞인 JSON → 확인 못 함
        dumped = json.dumps(mcp, ensure_ascii=False)
        for v in (SECRET, 'mcp.example.invalid', 'npx', 'Bearer'):
            self.assertNotIn(v, dumped)

    def test_stage2_saves_mcp_live_without_values(self):
        self.make_configs()
        res = sb_sources.stage2({})
        self.assertEqual(res['verdict'], 'pass', res)
        m = {x['name']: x for x in res['metrics']}
        self.assertEqual(m['mcp_connectors']['value'], 8)
        self.assertIn('적재 안 함', m['mcp_connectors']['evidence'])
        self.assertIn('확인 못 함: antigravity', m['mcp_connectors']['evidence'])
        text = sb_sources.sources_path().read_text(encoding='utf-8')
        doc = sb_sources.load()
        mcp = [s for s in doc['sources'] if s['kind'] == 'mcp']
        self.assertEqual(len(mcp), 8)
        for s in mcp:
            self.assertEqual((s['mode'], s['intake']['decision'], s['intake']['index']), ('live', 'live', 0))
        out = text + json.dumps(res, ensure_ascii=False) + sb_sources._render(doc['sources'])
        self.assertEqual(out.count(SECRET), 0)
        self.assertEqual(out.count('mcp.example.invalid'), 0)
        self.assertEqual(sb_sources.body_keys(doc), [])

    def test_cli_detect_output_has_no_values(self):
        import io
        from contextlib import redirect_stdout
        self.make_configs()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(sb_sources.main(['detect', '--json']), 0)
        out = buf.getvalue()
        self.assertIn('"kind": "mcp"', out)
        self.assertEqual(out.count(SECRET), 0)

    def test_codex_toml_with_bom_keeps_first_server(self):
        write(self.h / '.codex' / 'config.toml', '﻿[mcp_servers.jira]\ncommand = "x"\n[mcp_servers.slack]\n')
        mcp, bad = sb_sources.detect_mcp(str(self.h))
        self.assertEqual([s['location'] for s in mcp], ['mcp:jira', 'mcp:slack'])
        self.assertEqual(bad, [])

    def test_no_configs_no_mcp(self):
        self.assertEqual(sb_sources.detect_mcp(str(self.h)), ([], []))
        m = {x['name']: x for x in sb_sources.stage2({})['metrics']}
        self.assertEqual(m['mcp_connectors']['value'], 0)

    def test_category_onedrive_is_microsoft(self):
        self.assertEqual(sb_sources.mcp_category('ms-onedrive'), 'microsoft')   # 'drive' 가 들어 있어도
        self.assertEqual(sb_sources.mcp_category('google-drive'), 'google_drive')
        self.assertEqual(sb_sources.mcp_category('team drive'), 'google_drive')

    def test_link_like_name_redacted(self):
        self.assertNotIn(SECRET, sb_sources._mcp_name(URL))
        self.assertEqual(len(sb_sources._mcp_name('a' * 500)), sb_sources.MCP_NAME_MAX)


if __name__ == '__main__':
    unittest.main()
