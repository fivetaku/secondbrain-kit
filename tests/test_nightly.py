"""nightly.py contracts with stubbed stage commands (no real indexing, sync or LLM)."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import nightly  # noqa: E402
import sb_lock  # noqa: E402

PY = sys.executable


def stub(code: str) -> list:
    return [PY, '-c', code]


def ok(marker: str = '') -> list:
    # repr 로 감싼다 — 윈도우 경로(C:\Users\...)를 그대로 넣으면 '\U' 이스케이프로 SyntaxError
    body = "print({0!r}); import sys; print({1!r}, file=sys.stderr)".format('out-' + marker, 'err-' + marker)
    if marker:
        body += "; open({!r}, 'w').close()".format(marker)
    return stub(body)


def fail(code: int = 3) -> list:
    return stub('raise SystemExit({})'.format(code))


class NightlyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix='sb-nightly-test-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / 'sbhome'
        self.logs = self.home / 'logs'
        self.markers = Path(temporary.name)
        env = patch.dict(os.environ, {'SB_HOME': str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('SB_NIGHTLY_STAGE_OVERRIDE', None)

    def override(self, **stages) -> None:
        os.environ['SB_NIGHTLY_STAGE_OVERRIDE'] = json.dumps(
            {name.replace('_', '-'): value for name, value in stages.items()})

    def all_ok(self, **changes) -> None:
        stages = {'ko_index': ok(str(self.markers / 'ko')),
                  'automemory': ok(str(self.markers / 'auto')),
                  'snapshot': ok(str(self.markers / 'snap')),
                  'consolidate': ok(str(self.markers / 'cons'))}
        stages.update(changes)
        self.override(**stages)

    def run_main(self, *args: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = nightly.main(list(args))
        self.stdout, self.stderr = out.getvalue(), err.getvalue()
        return code

    def status(self) -> dict:
        return json.loads((self.logs / 'nightly_status.json').read_text(encoding='utf-8'))

    def log(self) -> str:
        return (self.logs / 'nightly.log').read_text(encoding='utf-8')

    def test_all_stages_ok(self) -> None:
        self.all_ok()
        self.assertEqual(self.run_main(), 0)
        status = self.status()
        self.assertEqual(set(status), {'date', 'finished_at', 'status', 'core_failed',
                                       'failed_stages', 'exit_code'})
        self.assertEqual((status['status'], status['core_failed'], status['failed_stages'],
                          status['exit_code']), ('ok', False, [], 0))
        for name in ('ko', 'auto', 'snap'):
            self.assertTrue((self.markers / name).exists(), name)
        self.assertFalse((self.markers / 'cons').exists(), 'consolidate is opt-in')
        log = self.log()
        self.assertRegex(log, r'== \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} start')
        for name in ('ko-index', 'automemory', 'snapshot'):
            self.assertIn('stage={} exit=0 action=continue'.format(name), log)
        self.assertIn('done status=ok core_failed=0 failed_stages=[] exit=0', log)
        # Stage output is appended to the log; snapshot stdout is discarded like the bash job.
        self.assertIn('out-' + str(self.markers / 'auto'), log)
        self.assertNotIn('out-' + str(self.markers / 'snap'), log)
        self.assertIn('err-' + str(self.markers / 'snap'), log)

    def test_snapshot_failure_is_partial_exit_zero(self) -> None:
        self.all_ok(snapshot=fail())
        self.assertEqual(self.run_main(), 0)
        status = self.status()
        self.assertEqual((status['status'], status['core_failed'], status['failed_stages'],
                          status['exit_code']), ('partial', False, ['snapshot'], 0))
        self.assertIn('stage=snapshot exit=3', self.log())

    def test_core_failure_exits_one_and_later_stages_still_run(self) -> None:
        self.all_ok(ko_index=fail(2))
        self.assertEqual(self.run_main(), 1)
        status = self.status()
        self.assertEqual((status['status'], status['core_failed'], status['failed_stages'],
                          status['exit_code']), ('failed', True, ['ko-index'], 1))
        self.assertTrue((self.markers / 'auto').exists())
        self.assertTrue((self.markers / 'snap').exists())
        self.assertIn('failed_stages=[ko-index] exit=1', self.log())

    def test_automemory_and_snapshot_failure(self) -> None:
        self.all_ok(automemory=fail(), snapshot=fail())
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.status()['failed_stages'], ['automemory', 'snapshot'])

    def test_stage_timeout_is_recorded_as_124(self) -> None:
        self.all_ok(snapshot={'argv': stub('import time; time.sleep(30)'), 'timeout': 1})
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.status()['failed_stages'], ['snapshot'])
        self.assertIn('stage=snapshot exit=124 action=timeout', self.log())

    def test_with_consolidate_runs_last_and_failure_is_partial(self) -> None:
        self.all_ok()
        self.assertEqual(self.run_main('--with-consolidate'), 0)
        self.assertTrue((self.markers / 'cons').exists())
        log = self.log()
        self.assertLess(log.index('stage=snapshot'), log.index('stage=consolidate'))
        self.all_ok(consolidate=fail())
        self.assertEqual(self.run_main('--with-consolidate'), 0)
        self.assertEqual((self.status()['status'], self.status()['failed_stages']),
                         ('partial', ['consolidate']))

    def test_lock_busy_returns_75_and_runs_nothing(self) -> None:
        self.all_ok()
        self.logs.mkdir(parents=True)
        with open(self.logs / 'nightly.lock', 'a+') as held:
            sb_lock.lock(held, sb_lock.LOCK_EX | sb_lock.LOCK_NB)
            self.assertEqual(self.run_main(), 75)
        self.assertIn('already running', self.stderr)
        self.assertFalse((self.markers / 'ko').exists())
        self.assertFalse((self.logs / 'nightly_status.json').exists())
        self.assertEqual(self.run_main(), 0)  # released lock is reusable

    def test_dry_run_prints_plan_and_writes_nothing(self) -> None:
        self.assertEqual(self.run_main('--dry-run', '--with-consolidate'), 0)
        lines = self.stdout.splitlines()
        self.assertEqual([line.split()[0] for line in lines],
                         ['stage=preflight', 'stage=ko-index', 'stage=automemory', 'stage=snapshot',
                          'stage=consolidate', 'stage=health'])
        self.assertIn('timeout=180 core=False', lines[0])
        self.assertIn('timeout=900 core=True', lines[1])
        self.assertIn('timeout=600 core=False', lines[3])
        self.assertIn('timeout=7200 core=False', lines[4])
        self.assertFalse(self.home.exists())

    def test_default_stage_commands(self) -> None:
        stages = {stage.name: stage for stage in nightly.build_stages(with_consolidate=True)}
        self.assertEqual(stages['ko-index'].argv, [PY, str(BIN / 'sb_fts_ko.py'), 'build'])
        self.assertEqual(stages['automemory'].argv, [PY, str(BIN / 'sync_automemory.py'), '--json'])
        self.assertEqual(stages['snapshot'].argv[:3], [PY, str(BIN / 'sb_search.py'), 'warmup'])
        for flag in ('--global', '--fresh'):
            self.assertIn(flag, stages['snapshot'].argv)
        self.assertEqual(stages['consolidate'].argv[1:3], [str(BIN / 'consolidate.py'), 'run'])
        self.assertEqual([stages[n].timeout for n in ('ko-index', 'automemory', 'snapshot',
                                                      'consolidate')], [900, 600, 600, 7200])
        self.assertNotIn('consolidate', {s.name for s in nightly.build_stages()})

    def test_bad_override_returns_2(self) -> None:
        os.environ['SB_NIGHTLY_STAGE_OVERRIDE'] = '{not json'
        self.assertEqual(self.run_main(), 2)
        self.assertFalse(self.home.exists())

    def test_real_cli_subprocess(self) -> None:
        self.all_ok(automemory=fail())
        result = subprocess.run([PY, str(BIN / 'nightly.py')], capture_output=True, text=True,
                                timeout=60, check=False, env=dict(os.environ))
        self.assertEqual(result.returncode, 1, result.stderr)
        status = self.status()
        self.assertEqual((status['status'], status['failed_stages']), ('failed', ['automemory']))
        self.assertEqual(status['date'], status['finished_at'][:10])


if __name__ == '__main__':
    unittest.main()
