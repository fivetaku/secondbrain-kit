"""M3 Pro 리뷰 P1-10(sb_state 줄 범위 밖·잘림 근거를 반박으로 보지 않기)·P2-3(sb_context 생략 안내문도 --max-bytes 안) 회귀 시험."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'bin'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sb_context  # noqa: E402
import sb_state  # noqa: E402
from test_state_operations import StateOpsBase  # noqa: E402


class OutOfRangeEvidenceTests(StateOpsBase):
    """P1-10: 근거가 없는 것(줄 범위 밖·200KB 잘림)을 근거가 주장과 어긋난 것(contradicted)과 구분한다."""

    def fc(self, target, body='port=37701'):
        r = self.propose(fact_key='svc.port', body=body, value_json=body)
        return r['candidate_id'], sb_state.verify(r['candidate_id'], 'file_contains', target)

    def one_line(self):
        f = self.tmp / 'one.txt'
        f.write_text('port=37701\n', encoding='utf-8')
        return f

    def test_span_in_range_still_confirms(self):
        f = self.one_line()
        self.assertEqual(self.fc(str(f) + '#L1')[1]['result'], 'confirmed')
        two = self.tmp / 'two.txt'
        two.write_text('x\nport=37701\n', encoding='utf-8')
        self.assertEqual(self.fc(str(two) + '#L2-L50')[1]['result'], 'confirmed')   # 끝만 넘는 범위는 그대로 둔다

    def test_span_in_range_without_text_still_contradicts(self):
        f = self.tmp / 'other.txt'
        f.write_text('port=1\n', encoding='utf-8')
        self.assertEqual(self.fc(str(f) + '#L1')[1]['result'], 'contradicted')

    def test_span_start_past_end_is_unknown(self):
        f = self.one_line()
        for span in ('#L999', '#L5-L9'):
            self.assertEqual(self.fc(str(f) + span)[1]['result'], 'unknown', span)

    def test_text_beyond_read_limit_is_unknown(self):
        big = self.tmp / 'big.txt'
        big.write_text('a' * 99 + '\n' + ('b' * 99 + '\n') * 2500 + 'port=37701\n', encoding='utf-8')
        self.assertEqual(self.fc(str(big) + '#L2502')[1]['result'], 'unknown')
        self.assertEqual(self.fc(str(big))[1]['result'], 'unknown')
        self.assertEqual(self.fc(str(big) + '#L1')[1]['result'], 'contradicted')   # 잘림 앞, 범위 안 비교는 그대로

    def test_switch_off_restores_contradicted(self):
        f = self.one_line()
        with patch.dict(os.environ, {'SB_VERIFY_RANGE_UNKNOWN': '0'}):
            self.assertEqual(self.fc(str(f) + '#L999')[1]['result'], 'contradicted')

    def test_expire_does_not_close_out_of_range_candidates(self):
        f = self.one_line()
        self.fc(str(f) + '#L999')
        self.fc(str(f) + '#L5-L9')
        with patch.dict(os.environ, {'SB_L2_EXPIRE': '1'}):
            out = sb_state.expire(str(self.db))
        self.assertEqual(out['changed'], 0)

    def test_user_utterance_span_past_end_is_unknown(self):
        record = self.tmp / 'utterances.txt'
        record.write_text('사용자: 배포는 staging-2로\n', encoding='utf-8')
        self.register_utterance(record)
        r = self.propose(kind='user_decision', fact_key='deploy.target', body='staging-2', value_json='staging-2')
        ok = sb_state.verify(r['candidate_id'], 'user_utterance_check', str(record) + '#L1')
        self.assertEqual(ok['result'], 'confirmed')
        far = sb_state.verify(r['candidate_id'], 'user_utterance_check', str(record) + '#L999')
        self.assertEqual(far['result'], 'unknown')


class FitIncludesNoteTests(unittest.TestCase):
    """P2-3: '(용량 한도로 이하 생략)' 안내문까지 포함해 출력 전체가 max_bytes 이하여야 한다."""

    def size(self, s):
        return len(s.encode('utf-8'))

    def test_small_budgets_bounded(self):
        lines = ['a' * 50, 'b' * 50, 'c' * 50]
        for mb in (0, 5, 10, 20, 31, 32, 33, 40):
            self.assertLessEqual(self.size(sb_context._fit(lines, mb)), mb, mb)
        self.assertLessEqual(self.size(sb_context._fit(['x' * 10], 5)), 5)

    def test_note_kept_when_it_fits(self):
        out = sb_context._fit(['a' * 100, 'b' * 100], 101)
        self.assertLessEqual(self.size(out), 101)
        for mb in (120, 133, 150):
            out = sb_context._fit(['a' * 100, 'b' * 100], mb)
            self.assertLessEqual(self.size(out), mb, mb)
            self.assertTrue(out.endswith('(용량 한도로 이하 생략)'), mb)
        self.assertLessEqual(self.size(sb_context._fit(['a' * 30] * 4, 100)), 100)

    def test_no_cut_unchanged(self):
        lines = ['a' * 10, 'b' * 10]
        self.assertEqual(sb_context._fit(lines, 1000), 'a' * 10 + '\n' + 'b' * 10)

    def test_build_with_rules_reserved_bounded(self):
        facts = [{'fact_key': 'k%d' % i, 'body': '사실 %d ' % i + 'x' * 60, 'evidence_ref': 'prompt:1', 'checked_at': '2026-11-01'}
                 for i in range(40)]
        rules = [{'body': '\n'.join('## 규칙 %d' % i for i in range(5))}]

        def mode(name, scope):
            return {'current': facts, 'next': [], 'rules': rules}[name]
        with patch.object(sb_context, 'mode', side_effect=mode), patch.object(sb_context, 'recall', return_value=[]):
            for mb in (250, 1000, 3000):
                block = sb_context.build('', 'work', mb, '')
                self.assertLessEqual(self.size(block), mb, mb)
                self.assertIn('(용량 한도로 이하 생략)', block)

    def test_cli_tiny_budget_bounded(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with patch.object(sb_context, 'mode', return_value=[{'fact_key': 'k', 'body': 'v' * 80, 'evidence_ref': 'p:1',
                                                             'checked_at': 'd'}]), \
                patch.object(sb_context, 'recall', return_value=[]), redirect_stdout(buf):
            sb_context.main(['--scope', 'demo', '--max-bytes', '20'])
        self.assertLessEqual(self.size(buf.getvalue().rstrip('\n')), 20)


if __name__ == '__main__':
    unittest.main()
