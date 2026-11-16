"""sb_claude_mem: claude-mem 버전·chroma-mcp 워커 인자 기준값과 worker-service.cjs 파서."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / 'bin'
FIX = Path(__file__).resolve().parent / 'fixtures' / 'claude-mem'
sys.path.insert(0, str(BIN))
import sb_claude_mem as cm  # noqa: E402

# 실제 npm 패키지의 worker-service.cjs 에서 잘라 낸 조각(정의 2개 + buildLauncherPrefix)
EXPECTED = {
    '13.24.23': ['onnxruntime>=1.20', 'protobuf<7'],
    '13.34.2': ['onnxruntime>=1.20', 'protobuf<7', 'chromadb==1.5.9'],
    '13.39.0': ['onnxruntime>=1.20', 'protobuf<7', 'chromadb==1.5.9', 'pydantic<2.14'],
    '15.18.3': ['onnxruntime>=1.20', 'protobuf<7', 'chromadb==1.5.9', 'pydantic<2.14'],
}


def fixture(version: str) -> str:
    return (FIX / (version + '.cjs')).read_text(encoding='utf-8')


class ParseTests(unittest.TestCase):
    def test_real_fixtures(self):
        for version, withs in EXPECTED.items():
            got, ver, ok = cm.parse_worker_spec(fixture(version))
            self.assertTrue(ok, version)
            self.assertEqual(got, withs, version)
            self.assertEqual(ver, '0.2.6', version)

    def test_inline_literals(self):
        # 압축기가 상수를 인라인하면 정의 없이 리터럴이 들어온다
        text = ('class A{static buildLauncherPrefix(e){return["--python",e,"--with","onnxruntime>=1.20",'
                '"--with","chromadb==1.6.0","--from","chroma-mcp==0.3.1","chroma-mcp"]}static b(){}}')
        withs, ver, ok = cm.parse_worker_spec(text)
        self.assertTrue(ok)
        self.assertEqual(withs, ['onnxruntime>=1.20', 'chromadb==1.6.0'])
        self.assertEqual(ver, '0.3.1')

    def test_broken_falls_back(self):
        for text in ('', 'class A{static other(e){}}',
                     'static buildLauncherPrefix(e){let r=NOPE.flatMap(n=>["--with",n]);return["--from",`chroma-mcp==${ALSO}`]}'):
            withs, ver, ok = cm.parse_worker_spec(text)
            self.assertFalse(ok)
            self.assertEqual(withs, cm.FALLBACK_WITH)
            self.assertEqual(ver, cm.FALLBACK_CHROMA_MCP)

    def test_member_access_is_not_a_definition(self):
        # obj.BE=[...] 는 정의가 아니다 — 앞에 ., 영숫자, $ 가 붙은 이름은 건너뛴다
        text = ('x.BE=["evil"];var BE=["onnxruntime>=1.20"],F0="0.2.6";'
                'class G{static buildLauncherPrefix(e){let r=BE.flatMap(n=>["--with",n]);'
                'return["--python",e,...r,"--from",`chroma-mcp==${F0}`,"chroma-mcp"]}static z(){}}')
        withs, ver, ok = cm.parse_worker_spec(text)
        self.assertTrue(ok)
        self.assertEqual(withs, ['onnxruntime>=1.20'])


class RequirementTests(unittest.TestCase):
    def test_tool_requirements_adds_pydantic_once(self):
        for version, withs in EXPECTED.items():
            reqs = cm.tool_requirements(withs)
            self.assertTrue(set(withs) <= set(reqs), version)
            self.assertEqual(sum(1 for r in reqs if r.startswith('pydantic')), 1, version)

    def test_worker_args_shape(self):
        self.assertEqual(cm.worker_args(['a', 'b'], '0.2.6'),
                         ['--with', 'a', '--with', 'b', '--from', 'chroma-mcp==0.2.6', 'chroma-mcp'])

    def test_pinned_chromadb(self):
        self.assertEqual(cm.pinned_chromadb(EXPECTED['13.34.2']), '1.5.9')
        self.assertIsNone(cm.pinned_chromadb(EXPECTED['13.24.23']))

    def test_python_version_order(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CLAUDE_MEM_PYTHON_VERSION', None)
            self.assertEqual(cm.python_version({}), '3.13')
            self.assertEqual(cm.python_version({'CLAUDE_MEM_PYTHON_VERSION': '3.12'}), '3.12')
            with mock.patch.dict(os.environ, {'CLAUDE_MEM_PYTHON_VERSION': '3.11'}):
                self.assertEqual(cm.python_version({'CLAUDE_MEM_PYTHON_VERSION': '3.12'}), '3.11')


class VersionTests(unittest.TestCase):
    def test_vkey_and_range(self):
        self.assertEqual(cm.vkey('13.34.2'), (13, 34, 2, 1))
        self.assertEqual(cm.vkey('v13.35.0-beta.1'), (13, 35, 0, 0))
        self.assertGreater(cm.vkey('13.34.2'), cm.vkey('13.34.2-beta'))   # 같은 숫자면 안정판이 높다
        self.assertEqual(cm.vkey('abc'), ())
        self.assertTrue(cm.in_tested(cm.RECOMMENDED))
        self.assertTrue(cm.in_tested(cm.TESTED[0]))
        self.assertFalse(cm.in_tested('13.24.5'))
        self.assertTrue(cm.in_tested('15.18.3'))
        self.assertFalse(cm.in_tested('15.19.0'))
        self.assertFalse(cm.in_tested(None))

    def test_recommended_matches_fallback(self):
        # 기본값은 권장판의 워커 인자여야 한다
        self.assertEqual(cm.FALLBACK_WITH, EXPECTED[cm.RECOMMENDED])

    def test_plugin_root_picks_highest_non_orphan(self):
        tmp = Path(tempfile.mkdtemp())
        base = tmp / 'plugins' / 'cache' / 'thedotmack' / 'claude-mem'
        for v, orphan in (('13.24.23', False), ('13.34.2', False), ('13.40.0', True), ('junk', False)):
            (base / v / 'scripts').mkdir(parents=True)
            (base / v / 'scripts' / 'worker-service.cjs').write_text(fixture('13.34.2'), encoding='utf-8')
            if orphan:
                (base / v / '.orphaned_at').write_text('x', encoding='utf-8')
        (base / '13.99.0').mkdir()   # scripts 없음 — 고아 판정과 같은 취급
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(tmp)}):
            root = cm.plugin_root()
            self.assertEqual(root.name, '13.34.2')
            self.assertEqual(cm.installed_version(), '13.34.2')
            self.assertEqual(cm.worker_chroma_spec()[0], EXPECTED['13.34.2'])
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': tempfile.mkdtemp()}):
            self.assertIsNone(cm.plugin_root())
            self.assertFalse(cm.worker_chroma_spec()[2])

class HardeningTests(unittest.TestCase):
    """2026-11-15 GPT Pro 리뷰 P1-2·3·4·5 / P2-1·2 — 틀린 값을 성공으로 읽지 않는다."""
    BASE = ('var BE=["onnxruntime>=1.20"],F0="0.2.6";class G{static buildLauncherPrefix(e){let r=BE.flatMap(n=>["--with",n]);'
            'return["--python",e,...r,"--from",`chroma-mcp==${F0}`,"chroma-mcp"]}static z(){}}')

    def test_duplicate_definition_fails_closed(self):
        self.assertFalse(cm.parse_worker_spec('var BE=["evil"];' + self.BASE)[2])
        self.assertFalse(cm.parse_worker_spec(self.BASE + 'var F0="9.9.9";')[2])

    def test_array_mutation_fails_closed(self):
        self.assertFalse(cm.parse_worker_spec(self.BASE + 'BE.push("x");')[2])

    def test_comment_or_nonstatic_fake_method_fails_closed(self):
        fake = '/* static buildLauncherPrefix(x){let r=XX.flatMap(n=>["--with",n]);return[`chroma-mcp==${YY}`]} */'
        self.assertFalse(cm.parse_worker_spec(fake + self.BASE)[2])
        self.assertFalse(cm.parse_worker_spec(self.BASE.replace('static buildLauncherPrefix', 'buildLauncherPrefix'))[2])

    def test_branching_literal_returns_fail_closed(self):
        t = ('class A{static buildLauncherPrefix(e){if(x)return["--with","a","--from","chroma-mcp==0.2.6","chroma-mcp"];'
             'return["--with","b","--from","chroma-mcp==0.3.0","chroma-mcp"]}static z(){}}')
        self.assertFalse(cm.parse_worker_spec(t)[2])

    def test_two_methods_fail_closed(self):
        self.assertFalse(cm.parse_worker_spec(self.BASE + self.BASE.replace('class G', 'class H'))[2])

    def test_prerelease_version_kept_whole(self):
        withs, ver, ok = cm.parse_worker_spec(self.BASE.replace('F0="0.2.6"', 'F0="0.2.6-beta.1"'))
        self.assertTrue(ok)
        self.assertEqual(ver, '0.2.6-beta.1')

    def test_pydantic_requirement_must_actually_cap(self):
        for worker in (['pydantic>=2.14'], ['pydantic==2.14'], ['pydantic'], ['pydantic>=2.0'], ['pydantic~=2.13'],
                       ['pydantic<2.14.post1'], ['pydantic<2.14rc1'], ['pydantic<2.15']):
            self.assertIn(cm.PYDANTIC_PIN, cm.tool_requirements(worker), worker)
        for worker in (['pydantic<2.14'], ['pydantic==2.13.5'], ['pydantic<2.13'], ['pydantic>=2,<2.14']):
            self.assertEqual(cm.tool_requirements(worker), worker, worker)   # 이미 보장 — 더하지 않는다

    def test_python_version_is_string(self):
        os.environ.pop('CLAUDE_MEM_PYTHON_VERSION', None)
        self.assertEqual(cm.python_version({'CLAUDE_MEM_PYTHON_VERSION': 3.12}), '3.12')

    def test_chromadb_pin_normalized(self):
        self.assertEqual(cm.pinned_chromadb(['chromadb == 1.5.9 ; python_version >= "3.12"']), '1.5.9')

    def test_active_version_from_registry(self):
        tmp = Path(tempfile.mkdtemp())
        base = tmp / 'plugins' / 'cache' / 'thedotmack' / 'claude-mem'
        for v in ('13.34.2', '13.39.0'):
            (base / v / 'scripts').mkdir(parents=True)
            (base / v / 'scripts' / 'worker-service.cjs').write_text(fixture(v), encoding='utf-8')
        (tmp / 'plugins' / 'installed_plugins.json').write_text(json.dumps({'version': 2, 'plugins': {
            'claude-mem@thedotmack': [{'installPath': str(base / '13.34.2'), 'version': '13.34.2'}]}}), encoding='utf-8')
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(tmp)}):
            self.assertEqual(cm.plugin_root().name, '13.34.2')   # 캐시에 더 높은 판이 있어도 등록된 판
            reg = tmp / 'plugins' / 'installed_plugins.json'
            outside = tmp / 'outside' / '13.99.0' / 'scripts'
            outside.mkdir(parents=True)
            (outside / 'worker-service.cjs').write_text(fixture('13.39.0'), encoding='utf-8')
            reg.write_text(json.dumps({'plugins': {'claude-mem@x': [{'installPath': str(outside.parent)}]}}), encoding='utf-8')
            self.assertEqual(cm.plugin_root().name, '13.39.0')   # 캐시 밖 등록은 무시하고 캐시 최고 판
            reg.write_text(json.dumps({'plugins': {'claude-mem@x': [{'installPath': str(base / '13.34.2')},
                                                                     {'installPath': str(base / '13.39.0')}]}}), encoding='utf-8')
            self.assertEqual(cm.plugin_root().name, '13.39.0')   # 여러 판 등록이면 가장 높은 판
            (tmp / 'plugins' / 'installed_plugins.json').unlink()
            self.assertEqual(cm.plugin_root().name, '13.39.0')


class Round3ParseTests(unittest.TestCase):
    """2026-11-15 GPT Pro 3차 리뷰: 인라인 리터럴은 정확한 꼴만, 환경 표시 붙은 pydantic 고정은 보장으로 치지 않는다."""

    def test_literal_with_extra_tokens_fails_closed(self):
        for body in ('return["--python",e,"--with","x",/*c*/"--with","y","--from","chroma-mcp==0.3.1","chroma-mcp"]',
                     'return["--python",e,"--with",f(),"--from","chroma-mcp==0.3.1","chroma-mcp"]',
                     'return["--python",e,...extra,"--with","x","--from","chroma-mcp==0.3.1","chroma-mcp"]'):
            t = 'class A{static buildLauncherPrefix(e){' + body + '}static b(){}}'
            self.assertFalse(cm.parse_worker_spec(t)[2], body)

    def test_marker_pydantic_not_safe(self):
        self.assertFalse(cm._pydantic_safe('pydantic<2.14; python_version<"3.13"'))
        self.assertIn(cm.PYDANTIC_PIN, cm.tool_requirements(['pydantic<2.14; python_version<"3.13"']))

    def test_malformed_registry_falls_back_to_cache(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / 'plugins' / 'cache' / 'thedotmack' / 'claude-mem' / '13.34.2' / 'scripts'
            base.mkdir(parents=True)
            (base / 'worker-service.cjs').write_text('x', encoding='utf-8')
            reg = Path(d) / 'plugins' / 'installed_plugins.json'
            with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': d}):
                for bad in ('[]', '{"plugins": []}', '{"plugins": {"claude-mem@x": ["s", 1]}}'):
                    reg.write_text(bad, encoding='utf-8')
                    self.assertIsNone(cm.active_root(), bad)
                    self.assertEqual(cm.plugin_root().name, '13.34.2', bad)


if __name__ == '__main__':
    unittest.main()
