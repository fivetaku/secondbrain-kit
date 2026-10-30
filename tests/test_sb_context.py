"""sb_context — 외부로 나가는 맥락 블록의 마스킹·층 조립·회수 서버 불통 시 동작."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import sb_context  # noqa: E402

DEAD_RECALLD = 'http://127.0.0.1:9'   # 아무도 듣지 않는 포트 — 연결 거부


class MaskTests(unittest.TestCase):
    def test_masks_money_contacts_and_accounts(self) -> None:
        text = ('견적 1,200,000원 · 계약금 500만원 · 문의 kim.lee+biz@example.co.kr · 연락 010-1234-5678 · '
                '01098765432님 · 계좌 110-123-456789 · 송장 12345678')
        out = sb_context.mask(text)
        for secret in ('1,200,000', '500만', 'example.co.kr', '1234-5678', '01098765432', '110-123-456789', '12345678'):
            self.assertNotIn(secret, out)
        for tag in ('<amount>', '<email>', '<phone>', '<account>', '<num>'):
            self.assertIn(tag, out)
        self.assertIn('01098765432님'.replace('01098765432', '<phone>'), out)   # 뒤에 한글이 붙어도 가린다

    def test_keeps_dates_versions_and_short_ids(self) -> None:
        text = '2026-10-30 배포 v1.24.3 · 관측 #104840 · 3건'
        self.assertEqual(sb_context.mask(text), text)

    def test_keeps_source_ids_compact_dates_and_won_words(self) -> None:
        # 이 블록이 내는 출처 ID(관련 기록 줄 머리 '- #N', 확정 사실 출처 observation:N·prompt:N)·8자리 날짜·
        # '숫자+원+한글' 낱말은 금액·계좌가 아니다
        for text in ('- #1234567 2026-10-01 work · 로컬 기록',
                     '- [k] 본문 (출처 observation:12345678, 확인 2026-10-28) · prompt:1048400 · 20261030 배포 · '
                     '답변 3원칙 · 2원화 · 30 원격'):
            with self.subTest(text=text):
                self.assertEqual(sb_context.mask(text), text)

    def test_other_labels_and_mid_text_hash_still_masked(self) -> None:
        # 출처 ID 예외는 실제로 내는 형식만 — 영문·한글 라벨 뒤 계좌류 숫자, 본문 중간 '#N' 은 가린다
        out = sb_context.mask('acct:12345678 · 계좌:12345678 · ID:9876543 · 주문 #12345678 · observation:12345678')
        self.assertEqual(out, 'acct:<num> · 계좌:<num> · ID:<num> · 주문 #<num> · observation:12345678')

    def test_masks_korean_id_card_and_phone_variants(self) -> None:
        # 4묶음 계좌·카드, 주민·법인번호, 국가번호·점 구분 전화는 3묶음 계좌·<num> 패턴을 빠져나갔다
        for secret in ('농협 351-1234-5678-13', '기업 123-123456-01-012', '주민 900101-1234567', '법인 110111-1234567',
                       '카드 4111-1111-1111-1111', '카드 4111 1111 1111 1111', '+82 10-1234-5678', '010.1234.5678'):
            with self.subTest(secret=secret):
                out = sb_context.mask(secret)
                self.assertNotRegex(out, r'\d{4}')
                self.assertRegex(out, r'<(account|id|phone)>')
        self.assertEqual(sb_context.mask('2026-10-30 · v1.2.3.4'), '2026-10-30 · v1.2.3.4')

    def test_amount_followed_by_particle_still_masked(self) -> None:
        out = sb_context.mask('견적 1,200,000원에 계약금 500만원을 · 잔액:12345678 · 송장 20261399')
        self.assertEqual(out, '견적 <amount>에 계약금 <amount>을 · 잔액:<num> · 송장 <num>')


class ModeTests(unittest.TestCase):
    def run_mode(self, returncode: int, stdout: str):
        done = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr='')
        with patch.object(sb_context.subprocess, 'run', return_value=done):
            return sb_context.mode('current', 'work')

    def test_ok_and_empty_are_results(self) -> None:
        items = [{'fact_key': 'k'}]
        self.assertEqual(self.run_mode(0, json.dumps({'status': 'ok', 'items': items})), items)
        self.assertEqual(self.run_mode(0, json.dumps({'status': 'empty', 'items': []})), [])

    def test_error_unavailable_or_failed_exit_is_none(self) -> None:
        # '없음'([])과 '조회 못 함'(None)을 구분해야 블록에 실패 표시가 붙는다
        for code, status in ((8, 'unavailable'), (2, 'error'), (0, 'unavailable'), (0, 'error'), (1, 'ok')):
            with self.subTest(code=code, status=status):
                self.assertIsNone(self.run_mode(code, json.dumps({'status': status, 'items': []})))
        self.assertIsNone(self.run_mode(0, 'not json'))
        self.assertIsNone(self.run_mode(0, '[]'))

    def test_not_yet_created_layer_is_empty(self) -> None:
        # 새 사용자: 상태층 DB·미결 원장·규칙 폴더를 아직 안 만든 것은 '없음'이다 — 실패 표시를 붙이지 않는다
        for code in ('state_db_missing', 'loops_file_missing', 'rules_dir_missing'):
            with self.subTest(code=code):
                self.assertEqual(self.run_mode(8, json.dumps({'status': 'unavailable', 'items': [], 'warnings': [code]})), [])

    def test_other_unavailable_is_still_failure(self) -> None:
        # 테이블 없음·잠금 같은 다른 사유가 섞이거나, 종료코드·상태가 어긋나면 여전히 조회 실패다
        for code, status, warnings in ((8, 'unavailable', ['state_table_missing']),
                                       (8, 'unavailable', ['rules_dir_missing', 'sqlite:database is locked']),
                                       (8, 'unavailable', [{'code': 'rules_dir_missing'}]),
                                       (0, 'unavailable', ['loops_file_missing']),
                                       (2, 'error', ['rules_dir_missing'])):
            with self.subTest(code=code, status=status, warnings=warnings):
                self.assertIsNone(self.run_mode(code, json.dumps({'status': status, 'items': [], 'warnings': warnings})))

    def test_unavailable_layer_marks_block(self) -> None:
        with patch.object(sb_context, 'mode', side_effect=lambda name, scope: None if name == 'next' else []):
            block = sb_context.build('', 'work', 6000, '')
        self.assertIn('(일부 층 조회 실패 — 없는 것과 구분할 것)', block)


class BuildTests(unittest.TestCase):
    FACTS = [{'fact_key': 'billing.vendor', 'body': '거래처 단가 1,500,000원 확정', 'evidence_ref': 'prompt:42',
              'checked_at': '2026-10-28T09:00:00Z'}]
    NEXTS = [{'item_id': 'L001', 'body': '견적서 회신 확인'}]
    RULES = [{'body': '# 규칙\n## 배포 전 테스트 전체 실행\n본문\n## 키는 볼트로만\n'}]

    def layers(self, name, scope):
        return {'current': self.FACTS, 'next': self.NEXTS, 'rules': self.RULES}[name]

    def test_layers_in_order_with_masking(self) -> None:
        def recall(query, project=''):
            if project:
                return [{'id': 7, 'date': '2026-10-01', 'project': 'work', 'title': '로컬 기록'}]
            return [{'id': 7, 'date': '2026-10-01', 'project': 'work', 'title': '로컬 기록'},
                    {'id': 9, 'date': '2026-09-01', 'project': 'other', 'title': '담당 a@b.com 연락'}]

        with patch.object(sb_context, 'mode', side_effect=self.layers), patch.object(sb_context, 'recall', side_effect=recall):
            block = sb_context.build('견적 회신', 'work', 6000, 'work')
        heads = [h for h in ('## 확정 사실(L2)', '## 다음 행동(L1)', '## 규칙(L3, 제목만)', '## 관련 기록(제목, 관측 ID)')]
        self.assertEqual([block.index(h) for h in heads], sorted(block.index(h) for h in heads))
        self.assertIn('- [billing.vendor] 거래처 단가 <amount> 확정 (출처 prompt:42, 확인 2026-10-28)', block)
        self.assertIn('- L001 견적서 회신 확인', block)
        self.assertIn('- 배포 전 테스트 전체 실행', block)
        self.assertEqual(block.count('#7 '), 1)   # 프로젝트·전역 결과 중복은 한 번만
        self.assertIn('#9 2026-09-01 other · 담당 <email> 연락', block)
        self.assertNotIn('1,500,000', block)
        self.assertNotIn('조회 실패', block)

    def test_block_keeps_emitted_source_ids_but_masks_labelled_numbers(self) -> None:
        facts = [{'fact_key': 'k', 'body': '정산 acct:12345678 · 계좌:87654321', 'evidence_ref': 'observation:12345678',
                  'checked_at': '2026-10-28'}]
        hits = [{'id': 1234567, 'date': '2026-10-01', 'project': 'work', 'title': '주문 #7654321 acct:12345678'}]
        with patch.object(sb_context, 'mode', side_effect=lambda name, scope: facts if name == 'current' else []), \
                patch.object(sb_context, 'recall', return_value=hits):
            block = sb_context.build('정산', 'work', 6000, '')
        self.assertIn('- [k] 정산 acct:<num> · 계좌:<num> (출처 observation:12345678, 확인 2026-10-28)', block)
        self.assertIn('- #1234567 2026-10-01 work · 주문 #<num> acct:<num>', block)
        self.assertNotIn('조회 실패', block)

    def test_max_bytes_cuts_with_marker(self) -> None:
        with patch.object(sb_context, 'mode', side_effect=self.layers), patch.object(sb_context, 'recall', return_value=[]):
            block = sb_context.build('', 'work', 300, '')
        self.assertTrue(block.endswith('(용량 한도로 이하 생략)'))
        self.assertLessEqual(len(block.rsplit('\n', 1)[0].encode('utf-8')), 300)

    def test_recall_server_down_still_builds_block(self) -> None:
        refused = urllib.error.URLError(ConnectionRefusedError(61, 'refused'))
        with patch.object(sb_context, 'mode', side_effect=self.layers), \
                patch.object(sb_context.urllib.request, 'urlopen', side_effect=refused):
            self.assertIsNone(sb_context.recall('견적'))
            block = sb_context.build('견적 회신', 'work', 6000, 'work')
        self.assertIn('## 확정 사실(L2)', block)
        self.assertNotIn('## 관련 기록', block)
        self.assertIn('(일부 층 조회 실패 — 없는 것과 구분할 것)', block)

    def test_gate_block_is_empty_not_failure(self) -> None:
        body = io.BytesIO(json.dumps({'items': [{'id': 1}], 'gate': {'pass': False}}).encode('utf-8'))
        with patch.object(sb_context.urllib.request, 'urlopen', return_value=body):
            self.assertEqual(sb_context.recall('무관한 질문'), [])

    def test_recalld_url_override(self) -> None:
        with patch.dict(os.environ, {'SB_RECALLD_URL': 'http://127.0.0.1:5000/'}):
            self.assertEqual(sb_context.recalld_url(), 'http://127.0.0.1:5000')
        env = {k: v for k, v in os.environ.items() if k != 'SB_RECALLD_URL'}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(sb_context.recalld_url(), sb_context.sb_recalld.base_url())


class EndToEndTests(unittest.TestCase):
    def test_main_runs_with_empty_home_and_dead_recalld(self) -> None:
        """설치 직후처럼 상태층·루프·규칙이 비어 있고 회수 서버도 꺼진 상태 — 실제 sb_search 를 띄워도 블록은 나온다."""
        with tempfile.TemporaryDirectory() as home:
            env = {'SB_HOME': home, 'SB_RECALLD_URL': DEAD_RECALLD, 'SB_CLAUDE_MEM_DIR': home}
            out = io.StringIO()
            with patch.dict(os.environ, env), redirect_stdout(out):
                # 다른 테스트가 남긴 층 경로 덮어쓰기가 있으면 '빈 SB_HOME' 이 아니게 된다 — 이 블록 안에서만 뺀다
                for key in ('SB_LOOPS_PATH', 'SB_RULES_DIR', 'SB_STATE_DB'):
                    os.environ.pop(key, None)
                code = sb_context.main(['--query', '견적 회신 일정', '--scope', 'work', '--project', 'work'])
                # 설치 직후엔 상태층 DB·미결 파일·규칙 폴더가 없어 실제 sb_search 가 unavailable 을 낸다 — 아직 안 만든
                # 것이라 '없음'([])으로 다룬다
                self.assertEqual([sb_context.mode(m, 'work') for m in ('current', 'next', 'rules')], [[], [], []])
                quiet = io.StringIO()
                with redirect_stdout(quiet):
                    sb_context.main(['--scope', 'work', '--project', 'work'])   # 회수 층 없이 새 사용자 상태만
            self.assertEqual(os.listdir(home), [])   # 읽기 전용 — 빈 SB_HOME 에 아무것도 만들지 않는다
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertTrue(text.startswith('[세컨브레인 맥락 — 읽기 전용, 기록 시점 자료. scope=work]'))
        self.assertIn('(일부 층 조회 실패', text)   # 회수 서버 불통은 '없음'과 구분된다
        self.assertNotIn('조회 실패', quiet.getvalue())   # 층 저장소를 아직 안 만든 것만으로는 실패 표시가 붙지 않는다


class HelpTests(unittest.TestCase):
    def test_sb_help_lists_context_options(self) -> None:
        # sb 도움말의 context 줄이 실제 인자와 어긋나지 않게
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            sb_context.main(['--help'])
        actual = set(re.findall(r'--[a-z][a-z-]+', out.getvalue())) - {'--help'}
        import sb
        lines = sb.__doc__.splitlines()
        start = next(i for i, line in enumerate(lines) if line.strip().startswith('sb context'))
        documented = set(re.findall(r'--[a-z][a-z-]+', lines[start]))
        self.assertEqual(documented, actual)


if __name__ == '__main__':
    unittest.main()
