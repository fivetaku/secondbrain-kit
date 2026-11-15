#!/usr/bin/env python3
"""세컨브레인 셋업 단계 엔진: 다섯 단계를 순서대로 돌리고 단계마다 판정을 남긴다.

  sb setup [run] [--stage N] [--only N] [--non-interactive] [--json]   current_stage(또는 N)부터 이어서 실행
  sb setup status [--json]                                             단계별 마지막 판정·다음 할 일
  sb setup accept N --reason R      (= sb setup --accept-stage N --reason R)   멈춘 단계를 이유를 적고 넘긴다
  sb setup approve HASH             (= sb setup --approve HASH)                4단계 구성안 승인(해시 앞 12자 이상)
  sb setup judge --stage 3 [--judgments 'ID:ok,ID:noise,…' | --judgments-file F]   3단계 표본 판정 입력
  sb setup answer [--agents a,b] [--sources p1,p2] [--llm-route R] [--embedding y|n] [--lang ko|en]
                                                                       첫 실행 질문 답을 대화로 받아 기록(출처 user)

단계: 1 install_check(doctor) → 2 register → 3 classify(sb_sources) → 4 layers(sb_vault 구성안·승인·적용) → 5 verify.
판정: pass·accepted 면 다음 단계, stop(문턱 미달)·pending(사람 승인·판정 필요)이면 그 자리에서 멈춘다.
기록: $SB_HOME/setup.json(진행 정본, 0600), $SB_HOME/logs/setup-<stage>-<시각>.json(판정 근거, 0600).
대화 본문은 읽지도 쓰지도 않는다. 비대화 환경(CI·파이프·에이전트·윈도우)에서는 입력을 기다리지 않는다.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

BIN = Path(__file__).resolve().parent
KIT = BIN.parent
sys.path.insert(0, str(BIN))
import sb_config  # noqa: E402
import sb_lock  # noqa: E402
import sb_vault  # noqa: E402

SCHEMA = 1
STAGES = [(1, 'install_check'), (2, 'register'), (3, 'classify'), (4, 'layers'), (5, 'verify')]
STAGE_NAMES = dict(STAGES)
LAST_STAGE = STAGES[-1][0]
VERDICTS = ('pass', 'stop', 'pending', 'accepted')
SAMPLE_SEED = 20261116          # 3단계 표본 seed 기본값 — 같은 입력이면 같은 표본(재현성)
SAMPLE_N = 50
VERIFY_SESSIONS = 10            # 5단계 검증 데이터가 쌓이기까지 권하는 세션 수
MAX_RUNS = 200                  # setup.json stage_runs 보관 상한
SETUP_UVX_TIMEOUT = 90          # 1단계에서 chroma-mcp 기동 확인 상한(초)
ANSWER_KEYS = ('agents', 'sources', 'llm_route', 'embedding', 'lang')
# 에이전트 셸 도구 안에서 돈다는 표시 — pty 가 붙어 있어도 사람이 질문을 볼 수 없다(--non-interactive 를 빠뜨려도 묻지 않게)
AGENT_ENV = ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CODEX_SANDBOX', 'CODEX_SANDBOX_NETWORK_DISABLED', 'CODEX_THREAD_ID',
             'GEMINI_CLI', 'OPENCODE')
# 실행할 때마다 생겼다 없어졌다 하는 점검 행(실행 시간) — 지문에서 뺀다(같은 설치면 같은 지문)
VOLATILE_METRICS = ('회수 훅 소요 시간',)


# ---------------------------------------------------------------- 경로·파일

def setup_path() -> Path:
    return sb_config.home() / 'setup.json'


def logs_dir() -> Path:
    return sb_config.home() / 'logs'


def injections_log() -> Path:
    """회수 훅(hooks/sb_recall.py recall_log)이 쓰는 기록. 주입은 ids, 자동화 건너뜀은 automation, 지연은 ms."""
    return logs_dir() / 'recall.jsonl'


def _private_dirs(*extra: Path) -> None:
    """SB_HOME·logs 를 새로 만들 때는 본인만 열게(0700). 이미 있는 폴더의 권한은 건드리지 않는다."""
    for d in (sb_config.home(),) + extra:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True, mode=0o700)


def _short(p: Any) -> str:
    return sb_vault._short(str(p))


def _empty() -> Dict[str, Any]:
    return {'version': SCHEMA, 'answers': {}, 'answer_sources': {}, 'stage_runs': [], 'current_stage': 1,
            'plan_hash': None, 'plan_path': None}


def load() -> Dict[str, Any]:
    """setup.json. 없으면 빈 기록. 깨졌으면 덮어쓰지 않고 멈춘다(사용자 기록 보호)."""
    p = setup_path()
    if not p.exists():
        return _empty()
    data = sb_vault.read_json_file(p)
    if not isinstance(data, dict):
        raise SystemExit('%s 를 읽을 수 없어 멈췄습니다 — 파일을 고치거나 옮긴 뒤 다시 하세요(덮어쓰지 않았습니다)' % _short(p))
    base = _empty()
    base.update(data)
    for k in ('answers', 'answer_sources'):
        if not isinstance(base.get(k), dict):
            base[k] = {}
    if not isinstance(base.get('stage_runs'), list):
        base['stage_runs'] = []
    return base


def _trim_runs(runs: List[Any]) -> List[Any]:
    """보관 상한을 넘으면 오래된 기록부터 버리되, 단계마다 마지막 판정은 남긴다(status·지문이 그 값을 본다)."""
    if len(runs) <= MAX_RUNS:
        return runs
    last = {}
    for i, r in enumerate(runs):
        if isinstance(r, dict) and r.get('stage') in STAGE_NAMES:
            last[r['stage']] = i
    keep = set(last.values())
    room = MAX_RUNS - len(keep)
    for i in range(len(runs) - 1, -1, -1):
        if room <= 0:
            break
        if i not in keep:
            keep.add(i)
            room -= 1
    return [r for i, r in enumerate(runs) if i in keep]


def save(doc: Dict[str, Any]) -> None:
    doc['stage_runs'] = _trim_runs(doc.get('stage_runs', []))
    doc['updated_at'] = sb_vault.now_iso()
    _private_dirs()
    sb_vault._atomic_write(setup_path(), json.dumps(doc, ensure_ascii=False, indent=1) + '\n', private=True, mode=0o600)


class _SetupLock:
    """sb setup 은 한 번에 하나만(설치기와 에이전트 스킬이 겹칠 수 있다). 볼트 적용 잠금과는 다른 파일이다 —
    4단계가 안에서 sb_vault.apply_plan 의 잠금을 다시 잡기 때문이다."""

    def __init__(self):
        self.path = sb_config.home() / '.setup.lock'
        self.f = None

    def __enter__(self):
        _private_dirs()
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        self.f = os.fdopen(fd, 'r+b')
        try:
            sb_lock.lock(self.f, sb_lock.LOCK_EX | sb_lock.LOCK_NB)
        except OSError:
            self.f.close()
            self.f = None
            raise SystemExit('다른 sb setup 이 진행 중입니다(%s) — 끝난 뒤 다시 하세요' % _short(self.path))
        return self

    def __exit__(self, *a):
        try:
            sb_lock.lock(self.f, sb_lock.LOCK_UN)
        except OSError:
            pass
        finally:
            self.f.close()
        return False


def _write_log(stage: int, record: Dict[str, Any]) -> Path:
    d = logs_dir()
    _private_dirs(d)
    base = 'setup-%d-%s' % (stage, sb_vault.stamp())
    p = d / (base + '.json')
    n = 2
    while p.exists():           # 같은 초에 두 번 돌아도 앞 기록을 덮지 않는다
        p = d / ('%s-%d.json' % (base, n))
        n += 1
    sb_vault._atomic_write(p, json.dumps(record, ensure_ascii=False, indent=1) + '\n', private=True, mode=0o600)
    return p


# ---------------------------------------------------------------- 판정 형식

def metric(name: str, value: Any, threshold: str = '', ok: Optional[bool] = None, evidence: str = '') -> Dict[str, Any]:
    return {'name': name, 'value': value, 'threshold': threshold, 'pass': ok, 'evidence': evidence}


def result(stage: int, verdict: str, metrics: List[Dict[str, Any]], hint: str = '') -> Dict[str, Any]:
    return {'stage': stage, 'name': STAGE_NAMES[stage], 'verdict': verdict, 'metrics': metrics, 'hint': hint}


def _valid(res: Any, stage: int) -> Dict[str, Any]:
    if not isinstance(res, dict) or res.get('verdict') not in VERDICTS:
        return result(stage, 'stop', [metric('stage_result', type(res).__name__, 'verdict ∈ pass/stop/pending/accepted', False)],
                      '단계 %d 결과 형식이 잘못됐습니다 — 키트를 갱신하세요' % stage)
    out = dict(res)
    out['stage'], out['name'] = stage, STAGE_NAMES[stage]
    out['metrics'] = [m for m in (res.get('metrics') or []) if isinstance(m, dict)]
    out['hint'] = res.get('hint') or ''
    return out


# ---------------------------------------------------------------- 답(첫 실행 질문)

def flat_answers(doc: Dict[str, Any]) -> Dict[str, Any]:
    """단계 함수에 넘기는 답(값만). sources 는 경로·링크 목록."""
    return dict(doc.get('answers') or {})


def detect_agents() -> List[str]:
    home = Path.home()
    found = []
    for name, exe, folder in (('claude', 'claude', '.claude'), ('codex', 'codex', '.codex'),
                              ('gemini', 'gemini', '.gemini'), ('omo', 'opencode', '.omo')):
        if shutil.which(exe) or (home / folder).is_dir():
            found.append(name)
    return found


def auto_answers() -> Dict[str, Any]:
    s = sb_config.claude_mem_settings() or {}
    return {'agents': detect_agents(), 'sources': [], 'llm_route': str(s.get('CLAUDE_MEM_PROVIDER') or 'claude'),
            'embedding': bool(shutil.which('ollama')), 'lang': 'ko'}


def _split(text: str) -> List[str]:
    return [x.strip() for x in text.split(',') if x.strip()]


def parse_answer(key: str, text: str, auto: Any) -> Any:
    text = text.strip()
    if key in ('agents',):
        return [x.lower() for x in _split(text)]
    if key == 'sources':
        return _split(text)
    if key == 'embedding':
        t = text.lower()
        if t in ('y', 'yes', 'ㅇ', '예', '네', 'true', '1', 'on'):
            return True
        if t in ('n', 'no', 'ㄴ', '아니', '아니오', 'false', '0', 'off'):
            return False
        return auto
    return text


def _foreground(stream) -> bool:
    """제어 터미널의 포그라운드 프로세스 그룹인가. 백그라운드(`./install.sh &`)에서 터미널을 읽으면 SIGTTIN 으로
    멈춘다. 제어 터미널이 아닌 tty(그 tty 로는 SIGTTIN 이 오지 않는다)·fileno 없는 객체는 True."""
    try:
        return os.tcgetpgrp(stream.fileno()) == os.getpgrp()
    except (AttributeError, ValueError, TypeError, OSError):
        return True


def in_agent() -> bool:
    return any(os.environ.get(k) for k in AGENT_ENV)


def can_ask(stdin=None, stdout=None) -> bool:
    """묻는 조건: stdin·stdout 모두 tty, 포그라운드, CI·SB_NO_SETUP·에이전트 셸 표시 없음,
    윈도우 아님(select 로 시간 제한을 걸 수 없다)."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if os.environ.get('CI') or os.environ.get('SB_NO_SETUP') or os.name == 'nt' or in_agent():
        return False
    try:
        if not (stdin.isatty() and stdout.isatty()):
            return False
    except (AttributeError, ValueError, OSError):
        return False
    return _foreground(stdin)


def _read_line(stdin, timeout: float) -> Optional[str]:
    """한 줄 읽기. 시간 초과·EOF 면 None."""
    import select
    try:
        ready, _, _ = select.select([stdin], [], [], timeout)
    except (OSError, ValueError):
        return None
    if not ready or not _foreground(stdin):     # 묻는 사이 백그라운드로 갔으면 읽지 않는다(SIGTTIN)
        return None
    line = stdin.readline()
    return None if line == '' else line


QUESTIONS = [
    ('agents', '쓰는 에이전트 (자동 감지: %s) — Enter=그대로, 쉼표로 고치기: '),
    ('sources', '넣을 자료 위치(폴더·회의 폴더·링크, 쉼표 구분) — Enter=없음: '),
    ('llm_route', '요약 LLM 경로 (자동: %s) — Enter=그대로: '),
    ('embedding', '한국어 임베딩 쓰기 y/n (자동: %s) — Enter=그대로: '),
]


def _shown(key: str, v: Any) -> str:
    if isinstance(v, list):
        return ','.join(v) or '없음'
    if isinstance(v, bool):
        return 'y' if v else 'n'
    return str(v)


def ask_first_run(timeout: float = 120, force: bool = False, stdin=None, stdout=None) -> Dict[str, Any]:
    """첫 실행 질문. 이미 답이 있으면(force 아니면) 그대로 둔다. 묻지 못하는 환경이면 자동값을 source:'default'로 채운다.
    질문당 timeout 초. Enter → 자동값(source:auto), 입력 → source:user, 시간 초과 → 그 질문과 남은 질문 모두 default."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    with _SetupLock():
        doc = load()
        if doc['answers'] and not force:
            return flat_answers(doc)
        auto = auto_answers()
        answers, sources = {}, {}
        asking = can_ask(stdin, stdout)
        if asking:
            stdout.write('\n세컨브레인 첫 설정 — 질문 %d개(질문당 %d초, Enter 는 자동값)\n' % (len(QUESTIONS), int(timeout)))
            stdout.flush()
        for key, prompt in QUESTIONS:
            if not asking:
                answers[key], sources[key] = auto[key], 'default'
                continue
            stdout.write(prompt % _shown(key, auto[key]) if '%s' in prompt else prompt)
            stdout.flush()
            line = _read_line(stdin, timeout)
            if line is None:                          # 자리를 비웠다 — 남은 질문도 기다리지 않는다
                stdout.write('\n(응답 없음 — 기본값으로 진행)\n')
                stdout.flush()
                asking = False
                answers[key], sources[key] = auto[key], 'default'
            elif not line.strip():
                answers[key], sources[key] = auto[key], 'auto'
            else:
                answers[key], sources[key] = parse_answer(key, line, auto[key]), 'user'
        answers['lang'], sources['lang'] = auto['lang'], 'default'
        for k in ('approved_plan_hash', 'seed'):       # 이미 받은 승인·seed 는 지키기
            if k in doc['answers']:
                answers[k] = doc['answers'][k]
                sources[k] = doc['answer_sources'].get(k, 'user')
        doc['answers'], doc['answer_sources'] = answers, sources
        save(doc)
        return dict(answers)


def set_answers(values: Dict[str, Any], source: str = 'user') -> Dict[str, Any]:
    """에이전트가 대화로 받은 답을 기록한다(키 값·비밀번호는 받지 않는다 — 여기 항목에는 없다)."""
    with _SetupLock():
        doc = load()
        if not doc['answers']:
            doc['answers'] = auto_answers()
            doc['answer_sources'] = {k: 'default' for k in doc['answers']}
        changed = False
        for k, v in values.items():
            if v is None:
                continue
            if k == 'sources' and doc['answers'].get(k) != v:
                changed = True
            doc['answers'][k] = v
            doc['answer_sources'][k] = source
        cur = int(doc.get('current_stage') or 1)
        if changed and cur > 2:
            # 자료 답이 바뀌었다 — 이미 지난 2단계(등록)부터 다시 해야 sources.json 에 반영된다
            doc['current_stage'] = 2
        save(doc)
        return flat_answers(doc)


# ---------------------------------------------------------------- 단계 1: 설치 점검(doctor)

def _load_doctor():
    """매번 새로 읽는다 — doctor 는 import 할 때 SB_HOME(venv 경로)을 정하므로, 캐시하면 다른 SB_HOME 을 본다."""
    spec = importlib.util.spec_from_file_location('sbkit_setup_doctor', str(KIT / 'installer' / 'doctor.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _doctor_rows() -> List[Tuple[str, str, str]]:
    """doctor 의 점검 함수들을 그대로 돌려 (수준, 이름, 내용) 목록을 얻는다(읽기 전용)."""
    d = _load_doctor()
    if hasattr(d, 'UVX_TIMEOUT'):
        d.UVX_TIMEOUT = min(d.UVX_TIMEOUT, SETUP_UVX_TIMEOUT)   # 설치 안에서 다시 돌 때는 짧게(⚠ 로 남고 나중에 doctor)
    del d.rows[:]
    for fn in d.CHECKS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — doctor.main 과 같은 처리
            d.rec('❌', fn.__name__, '%s: %s' % (type(exc).__name__, exc))
    return list(d.rows)


def stage1(answers: Dict[str, Any]) -> Dict[str, Any]:
    rows = _doctor_rows()
    metrics = [metric(name, level, '❌ 아님', level != '❌', detail) for level, name, detail in rows]
    bad = [name for level, name, _ in rows if level == '❌']
    if bad:
        return result(1, 'stop', metrics, '고칠 항목: %s — 설치기를 다시 돌리거나(python installer/install.py) '
                      '`python installer/install.py --doctor`로 확인한 뒤 sb setup' % ', '.join(bad[:5]))
    return result(1, 'pass', metrics)


# ---------------------------------------------------------------- 단계 2·3: 자료 등록·분류(sb_sources, M2)

def _sources_mod():
    try:
        import sb_sources  # noqa: F401
        return sb_sources
    except ImportError:
        return None


def _not_ready(stage: int) -> Dict[str, Any]:
    return result(stage, 'pending', [metric('sb_sources', None, '모듈 있음', None, 'bin/sb_sources.py import 실패')],
                  '미구현 — 자료 등록·분류(sb_sources)가 이 키트 판에 아직 없습니다')


def stage2(answers: Dict[str, Any]) -> Dict[str, Any]:
    mod = _sources_mod()
    if mod is None or not hasattr(mod, 'stage2'):
        return _not_ready(2)
    return mod.stage2(answers)


def stage3(answers: Dict[str, Any]) -> Dict[str, Any]:
    mod = _sources_mod()
    if mod is None or not hasattr(mod, 'stage3'):
        return _not_ready(3)
    return mod.stage3(seed=answers.get('seed', SAMPLE_SEED), n=SAMPLE_N)


# ---------------------------------------------------------------- 단계 4: 레이어 설계·적용(sb_vault)

def _hash_ok(approved: Optional[str], full: Optional[str]) -> bool:
    h = (approved or '').strip().lower()
    return bool(full) and len(h) >= 12 and bool(re.fullmatch(r'[0-9a-f]+', h)) and full.startswith(h)


def stage4(answers: Dict[str, Any], doc: Dict[str, Any]) -> Dict[str, Any]:
    st = sb_vault.status()
    v = sb_vault.read_json_file(sb_vault.vault_json_path())
    if st['configured'] and isinstance(v, dict) and v.get('plan_hash') and st.get('last_apply_ok') is not False:
        # 이미 승인·적용한 볼트가 있다(sb vault apply 로 직접 했어도). 재분석 권유는 세션 시작 알림(nudge)이 맡는다
        doc['plan_hash'] = v['plan_hash']
        return result(4, 'pass', [metric('vault_configured', True, '승인된 구성 적용됨', True, _short(sb_vault.vault_json_path())),
                                  metric('scopes', len(v.get('scopes') or {}), '', None)])
    approved = answers.get('approved_plan_hash')
    saved = Path(doc['plan_path']) if doc.get('plan_path') else None
    if approved and saved and _hash_ok(approved, doc.get('plan_hash')):
        plan = sb_vault.read_json_file(saved)
        if isinstance(plan, dict) and plan.get('plan_hash') == doc['plan_hash']:
            rec = sb_vault.apply_plan(plan, approved, 'sb_setup_approve')
            ok = rec['verify']['ok']
            # 승인은 한 번만 쓴다 — 남겨 두면 사용자가 되돌린(rollback) 볼트를 다음 run 이 말없이 다시 적용한다
            doc['answers'].pop('approved_plan_hash', None)
            doc['answer_sources'].pop('approved_plan_hash', None)
            ms = [metric('plan_hash', plan['plan_hash'][:12], '승인 해시와 같음', True, _short(saved)),
                  metric('applied', ok, 'True', ok, _short(rec.get('apply_path', '')))]
            if ok:
                return result(4, 'pass', ms)
            return result(4, 'stop', ms, '적용 검증 실패 — 자동으로 되돌렸습니다. 기록: %s' % _short(rec.get('apply_path', '')))
    analysis = sb_vault.analyze()
    plan = sb_vault.make_plan(analysis, None, 'all')     # analysis_ref 를 비워 같은 기록이면 같은 해시
    ms = [metric('data_volume', plan.get('data_volume'), '', None),
          metric('scopes', len(plan['scopes']), '', None),
          metric('new_aliases', len(plan['aliases']), '', None),
          metric('conflicts', len(plan['conflicts']), '막는 충돌 0',
                 not any(c.get('blocking') for c in plan['conflicts']))]
    if not plan['scopes'] and not plan['aliases']:
        # 승인할 것이 없다(기록이 비었거나 적다) — 기본 볼트로 시작(설치기 11단계와 같은 처리)
        sb_vault.init_default()
        doc['plan_hash'], doc['plan_path'] = None, None
        return result(4, 'pass', ms + [metric('default_vault', True, '구성할 프로젝트 없음', True)])
    prev = Path(doc['plan_path']) if doc.get('plan_path') else None
    if prev and doc.get('plan_hash') == plan['plan_hash'] and prev.is_file():
        path = prev                                       # 같은 구성안이면 다시 저장하지 않는다(파일이 쌓이지 않게)
    else:
        sb_vault._save('analysis', analysis)
        path = sb_vault._save('plan', plan)
    doc['plan_hash'], doc['plan_path'] = plan['plan_hash'], str(path)
    h12 = plan['plan_hash'][:12]
    ms.append(metric('plan_hash', h12, '사용자 승인', None, _short(path)))
    if any(c.get('blocking') for c in plan['conflicts']):
        return result(4, 'stop', ms, '별칭 파일에 겹치는 이름이 있습니다 — %s 를 정리한 뒤 sb setup'
                      % _short(plan.get('aliases_path')))
    return result(4, 'pending', ms, '구성안 확인(sb vault plan 요약 또는 %s) 뒤 승인: sb setup approve %s '
                  '(또는 sb vault apply --plan %s --yes --hash %s)' % (_short(path), h12, _short(path), h12))


# ---------------------------------------------------------------- 단계 5: 검증(M3 에서 채운다)

def _p95(values: List[float]) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def _obs_projects(ids: List[int]) -> Dict[int, str]:
    """주입된 관측 id → project 꼬리표(claude-mem DB 읽기 전용, 본문은 읽지 않음). DB 가 없거나 못 읽으면 {}."""
    db = Path(str(sb_config.claude_mem_db()))
    if not ids or not db.is_file():
        return {}
    import sqlite3
    out: Dict[int, str] = {}
    try:
        con = sqlite3.connect('file:%s?mode=ro' % db, uri=True, timeout=5)
        try:
            ids = sorted(set(ids))
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                q = 'SELECT id, project FROM observations WHERE id IN (%s)' % ','.join('?' * len(chunk))
                for oid, proj in con.execute(q, chunk):
                    out[int(oid)] = proj or ''
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return out


def stage5(answers: Dict[str, Any]) -> Dict[str, Any]:
    p = injections_log()
    rows = []
    if p.is_file():
        with p.open(encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
    inj = [r for r in rows if isinstance(r.get('ids'), list) and r['ids']]
    if not inj:
        return result(5, 'pending', [metric('recall_injections', 0, '> 0', None, _short(p))],
                      '검증 데이터 대기: 세션 %d회 뒤 sb setup --stage 5' % VERIFY_SESSIONS)
    ms = [metric('recall_injections', len(inj), '> 0', True, _short(p))]
    auto = sum(1 for r in inj if r.get('automation'))
    ms.append(metric('automation_injections', auto, '= 0', auto == 0,
                     '자동화 건너뜀 %d건' % sum(1 for r in rows if r.get('automation') and not r.get('ids'))))
    projs = _obs_projects([int(i) for r in inj for i in r['ids'] if isinstance(i, int)])
    if projs:
        import sb_scope
        cross = 0
        for r in inj:
            ev = sb_scope.project_key(r.get('project') or '')
            keys = {sb_scope.project_key(projs[i]) for i in r['ids'] if isinstance(i, int) and projs.get(i)}
            if ev and any(k and k != ev for k in keys):
                cross += 1
        ratio = round(cross / len(inj), 4)
        ms.append(metric('cross_scope_ratio', ratio, '≤ 0.2', ratio <= 0.2,
                         '주입 이벤트 중 다른 프로젝트 관측이 하나라도 섞인 비율'))
    lat = [float(r['ms']) for r in rows if isinstance(r.get('ms'), (int, float)) and not isinstance(r.get('ms'), bool)]
    if lat:
        p95 = _p95(lat)
        ms.append(metric('p95_ms', p95, '≤ 3000', p95 <= 3000))
    if any(m['pass'] is False for m in ms):
        return result(5, 'stop', ms, '회수 품질 문턱 미달 — 원인을 고친 뒤 sb setup --stage 5')
    return result(5, 'pass', ms)


STAGE_FUNCS: Dict[int, Callable[..., Dict[str, Any]]] = {1: stage1, 2: stage2, 3: stage3, 4: stage4, 5: stage5}


# ---------------------------------------------------------------- 실행·넘김·승인

def _run_stage(n: int, doc: Dict[str, Any]) -> Dict[str, Any]:
    answers = flat_answers(doc)
    try:
        res = STAGE_FUNCS[n](answers, doc) if n == 4 else STAGE_FUNCS[n](answers)
    except (Exception, SystemExit) as e:  # noqa: BLE001 — 한 단계의 오류가 설치·셋업 전체를 죽이지 않게
        msg = str(e.code if isinstance(e, SystemExit) else e)[:300]
        res = result(n, 'stop', [metric('error', type(e).__name__, '오류 없음', False, msg)], msg or type(e).__name__)
    return _valid(res, n)


def _record(doc: Dict[str, Any], res: Dict[str, Any], started: str, reason: Optional[str] = None,
            metrics_ref: Optional[str] = None) -> Dict[str, Any]:
    finished = sb_vault.now_iso()
    if metrics_ref is None:
        log = dict(res, started_at=started, finished_at=finished)
        if reason:
            log['reason'] = reason
        metrics_ref = str(_write_log(res['stage'], log))
    run = {'stage': res['stage'], 'name': res['name'], 'started_at': started, 'finished_at': finished,
           'verdict': res['verdict'], 'metrics_ref': metrics_ref, 'reason': reason, 'hint': res.get('hint') or '',
           'metrics': [{'name': m.get('name'), 'value': m.get('value'), 'pass': m.get('pass')} for m in res['metrics']]}
    doc['stage_runs'].append(run)
    cur = int(doc.get('current_stage') or 1)
    if res['verdict'] in ('pass', 'accepted'):
        doc['current_stage'] = max(cur, res['stage'] + 1)
    else:                       # 앞 단계를 다시 돌려 멈췄으면 그 단계부터 다시 한다
        doc['current_stage'] = min(cur, res['stage'])
    return run


def _ensure_answers(doc: Dict[str, Any]) -> None:
    if not doc['answers']:
        auto = auto_answers()
        doc['answers'] = auto
        doc['answer_sources'] = {k: 'default' for k in auto}


def run(from_stage: Optional[int] = None, only: Optional[int] = None, non_interactive: bool = False,
        stop_before: Optional[int] = None) -> Dict[str, Any]:
    """current_stage(또는 from_stage)부터 순서대로. stop·pending 이면 멈춘다. 지나지 않은 단계로 건너뛸 수 없다.
    stop_before 를 주면 그 단계 앞에서 멈춘다(설치기 --no-vault: 4단계 볼트를 만들지 않는다)."""
    if not non_interactive and not load()['answers']:
        ask_first_run()               # 묻지 못하는 환경이면 알아서 기본값으로 채운다
    with _SetupLock():
        doc = load()
        _ensure_answers(doc)
        cur = int(doc.get('current_stage') or 1)
        target = only or from_stage
        if target is not None:
            if target not in STAGE_NAMES:
                raise SystemExit('단계는 1~%d 입니다' % LAST_STAGE)
            if target > cur:
                raise SystemExit('단계 %d 는 아직 차례가 아닙니다(지금 %d단계) — 순서를 건너뛸 수 없습니다' % (target, cur))
        stages = [only] if only else list(range(from_stage or cur, LAST_STAGE + 1))
        if stop_before is not None:
            stages = [n for n in stages if n < stop_before]
        results = []
        for n in stages:
            started = sb_vault.now_iso()
            res = _run_stage(n, doc)
            _record(doc, res, started)
            results.append(res)
            if res['verdict'] not in ('pass', 'accepted'):
                break
        save(doc)
        return summary(doc, results)


def accept(stage: int, reason: str) -> Dict[str, Any]:
    """멈춘(stop·pending) 단계를 이유와 함께 넘긴다. 넘긴 사실은 stage_runs 에 accepted 로 남는다."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('넘기는 이유(--reason)를 적어야 합니다')
    with _SetupLock():
        doc = load()
        if stage not in STAGE_NAMES:
            raise ValueError('단계는 1~%d 입니다' % LAST_STAGE)
        last = _last_runs(doc).get(stage)
        if not last:
            raise ValueError('단계 %d 는 아직 돌린 적이 없습니다 — sb setup run 먼저' % stage)
        if last['verdict'] not in ('stop', 'pending'):
            raise ValueError('단계 %d 의 마지막 판정이 %s 라 넘길 것이 없습니다' % (stage, last['verdict']))
        if stage != int(doc.get('current_stage') or 1):
            raise ValueError('지금 멈춘 단계는 %s 입니다' % doc.get('current_stage'))
        res = result(stage, 'accepted', [metric(m['name'], m['value'], '', m.get('pass')) for m in last.get('metrics', [])],
                     '')
        _record(doc, res, sb_vault.now_iso(), reason=reason.strip())
        save(doc)
        return summary(doc, [res])


def approve(h: str) -> Dict[str, Any]:
    """4단계 구성안 승인. 지금 기록된 구성안 해시의 앞 12자 이상이어야 한다. 적용은 다음 sb setup run 이 한다."""
    with _SetupLock():
        doc = load()
        if not doc.get('plan_hash'):
            raise ValueError('승인할 구성안이 없습니다 — sb setup run 으로 4단계 구성안을 먼저 만드세요')
        if not _hash_ok(h, doc['plan_hash']):
            raise ValueError('승인한 해시가 지금 구성안(%s…)과 다릅니다(12자 이상)' % doc['plan_hash'][:12])
        doc['answers']['approved_plan_hash'] = h.strip().lower()
        doc['answer_sources']['approved_plan_hash'] = 'user'
        save(doc)
        return summary(doc, [])


def latest_sample() -> Optional[Path]:
    files = sorted(logs_dir().glob('setup-3-sample-*.json'), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def parse_judgments(text: str) -> Dict[str, str]:
    out = {}
    for part in _split(text):
        k, _, v = part.partition(':')
        v = v.strip().lower()
        if not k.strip() or v not in ('ok', 'noise', 'dup'):
            raise ValueError('판정 형식: ID:ok|noise|dup (쉼표 구분) — %r' % part)
        out[k.strip()] = v
    return out


def judge(judgments: Dict[str, str], sample_path: Optional[Path] = None) -> Dict[str, Any]:
    """3단계 표본에 사람·에이전트 판정을 합친다(sb_sources.judge). 결과가 판정 형식이면 3단계 기록으로 남긴다."""
    mod = _sources_mod()
    if mod is None or not hasattr(mod, 'judge'):
        raise ValueError('미구현 — 자료 분류(sb_sources)가 이 키트 판에 아직 없습니다')
    cur = int(load().get('current_stage') or 1)
    if cur != 3:      # 앞 단계가 멈춰 있는데 3단계 판정으로 차례를 건너뛰지 않게(accept 와 같은 규칙)
        raise ValueError('지금 차례는 %s단계입니다 — 3단계 판정은 3단계 차례에만 받습니다' % cur)
    sample_path = sample_path or latest_sample()
    if not sample_path:
        raise ValueError('3단계 표본이 없습니다 — sb setup run 으로 3단계를 먼저 돌리세요')
    res = mod.judge(str(sample_path), judgments)
    with _SetupLock():
        doc = load()
        if int(doc.get('current_stage') or 1) != 3:
            raise ValueError('지금 차례는 %s단계입니다 — 3단계 판정은 3단계 차례에만 받습니다' % doc.get('current_stage'))
        results = []
        if isinstance(res, dict) and res.get('verdict') in VERDICTS:
            res = _valid(res, 3)
            _record(doc, res, sb_vault.now_iso())
            results.append(res)
        save(doc)
        return summary(doc, results)


# ---------------------------------------------------------------- 상태·지문

def _last_runs(doc: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    last: Dict[int, Dict[str, Any]] = {}
    for r in doc.get('stage_runs', []):
        if isinstance(r, dict) and r.get('stage') in STAGE_NAMES:
            last[r['stage']] = r
    return last


def fingerprint(doc: Optional[Dict[str, Any]] = None) -> str:
    """단계별 마지막 판정 (stage, verdict, metrics name·value) + plan_hash 의 sha256. 시각·경로 기록은 뺀다 —
    같은 입력이면 몇 번 돌려도 같다."""
    doc = doc if doc is not None else load()
    last = _last_runs(doc)
    body = {'runs': [[n, last[n]['verdict'], [[m.get('name'), m.get('value')] for m in last[n].get('metrics', [])
                                              if m.get('name') not in VOLATILE_METRICS]]
                     for n in sorted(last)],
            'plan_hash': doc.get('plan_hash')}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')).hexdigest()


def summary(doc: Dict[str, Any], results: List[Dict[str, Any]]) -> Dict[str, Any]:
    last = _last_runs(doc)
    cur = int(doc.get('current_stage') or 1)
    stopped = next((r for r in results if r['verdict'] not in ('pass', 'accepted')), None)
    if stopped is None and cur <= LAST_STAGE and cur in last and last[cur]['verdict'] in ('stop', 'pending'):
        stopped = last[cur]
    return {'current_stage': cur, 'complete': cur > LAST_STAGE,
            'stages': {n: (last[n]['verdict'] if n in last else None) for n, _ in STAGES},
            'results': results,
            'stopped_at': stopped['stage'] if stopped else None,
            'verdict': stopped['verdict'] if stopped else ('pass' if cur > LAST_STAGE else None),
            'hint': (stopped or {}).get('hint') or ('' if cur > LAST_STAGE else 'sb setup run'),
            'plan_hash': doc.get('plan_hash'), 'fingerprint': fingerprint(doc), 'setup_json': str(setup_path())}


def status() -> Dict[str, Any]:
    doc = load()
    out = summary(doc, [])
    out['answers'] = {k: {'value': doc['answers'].get(k), 'source': doc['answer_sources'].get(k)}
                      for k in ANSWER_KEYS if k in doc['answers']}
    out['approved'] = bool(doc['answers'].get('approved_plan_hash'))
    return out


MARK = {'pass': '✅', 'accepted': '↷', 'stop': '❌', 'pending': '⏸', None: '·'}


def render(s: Dict[str, Any]) -> str:
    lines = ['세컨브레인 셋업 — 다음 단계 %s' % ('없음(완료)' if s['complete'] else s['current_stage'])]
    for n, name in STAGES:
        lines.append('  %s %d %s' % (MARK.get(s['stages'].get(n), '·'), n, name))
    for r in s.get('results', []):
        bad = [m for m in r['metrics'] if m.get('pass') is False]
        lines.append('  [%d %s] %s%s' % (r['stage'], r['name'], r['verdict'],
                                         (' — 미달: ' + ', '.join('%s=%s' % (m['name'], m['value']) for m in bad[:5])) if bad else ''))
    if s.get('hint') and not s['complete']:
        lines.append('다음: ' + s['hint'])
    return '\n'.join(lines)


# ---------------------------------------------------------------- CLI

def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(prog='sb setup', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', nargs='?', default='run', choices=('run', 'status', 'accept', 'approve', 'judge', 'answer'))
    ap.add_argument('arg', nargs='?', help='accept 의 단계 번호 / approve 의 해시')
    ap.add_argument('--stage', type=int, help='run: 이 단계부터 / judge: 대상 단계(3)')
    ap.add_argument('--only', type=int, help='이 단계 하나만 다시 돌린다')
    ap.add_argument('--reason', help='accept 이유(필수)')
    ap.add_argument('--accept-stage', type=int, help='= accept N')
    ap.add_argument('--approve', dest='approve_hash', help='= approve HASH')
    ap.add_argument('--non-interactive', action='store_true', help='묻지 않고 기본값으로')
    ap.add_argument('--judgments', help="judge: 'ID:ok,ID:noise,ID:dup'")
    ap.add_argument('--judgments-file', help='judge: {ID: ok|noise|dup} JSON 파일')
    ap.add_argument('--sample', help='judge: 표본 파일(기본: 가장 최근 setup-3-sample-*.json)')
    ap.add_argument('--agents')
    ap.add_argument('--sources')
    ap.add_argument('--llm-route')
    ap.add_argument('--embedding', choices=('y', 'n'))
    ap.add_argument('--lang', choices=('ko', 'en'))
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    cmd = 'accept' if a.accept_stage is not None else 'approve' if a.approve_hash else a.cmd
    try:
        if cmd == 'run':
            out = run(from_stage=a.stage, only=a.only, non_interactive=a.non_interactive)
        elif cmd == 'status':
            out = status()
        elif cmd == 'accept':
            n = a.accept_stage if a.accept_stage is not None else int(a.arg or 0)
            out = accept(n, a.reason or '')
        elif cmd == 'approve':
            out = approve(a.approve_hash or a.arg or '')
        elif cmd == 'judge':
            if a.stage not in (None, 3):
                raise ValueError('판정 입력은 3단계만 받습니다')
            if a.judgments_file:
                j = sb_vault.read_json_file(Path(a.judgments_file))
                if not isinstance(j, dict):
                    raise ValueError('--judgments-file 은 {ID: 판정} JSON 객체여야 합니다')
                j = parse_judgments(','.join('%s:%s' % kv for kv in j.items()))
            elif a.judgments:
                j = parse_judgments(a.judgments)
            else:
                raise ValueError('--judgments 또는 --judgments-file 이 필요합니다')
            out = judge(j, Path(a.sample) if a.sample else None)
        else:   # answer
            vals = {'agents': [x.lower() for x in _split(a.agents)] if a.agents is not None else None,
                    'sources': _split(a.sources) if a.sources is not None else None,
                    'llm_route': a.llm_route, 'lang': a.lang,
                    'embedding': (a.embedding == 'y') if a.embedding else None}
            set_answers(vals)
            out = status()
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str) if a.json else render(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
