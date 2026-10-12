#!/usr/bin/env python3
"""기록층 건강 점검 — 막힌 길을 조용히 두지 않는다.

  sb health                 점검 결과(사람용). 문제 없으면 한 줄
  sb health --json          JSON
  sb health --fix           고칠 수 있는 것은 고친다(워커·Ollama 기동, 밀린 메모리 동기화, 놓친 야간 배치)
  sb health ensure          워커·Ollama만 띄운다(야간 배치 첫 단계)

점검 항목
  worker      claude-mem 워커 응답 (/api/health)
  ollama      벡터 임베딩(bge-m3) 서버 응답 — 꺼지면 벡터 저장·검색이 멈춘다
  nightly     야간 배치 결과·신선도 (nightly_status.json)
  automemory  메모리 파일 중 기록층에 아직 안 들어간 것
  vector      벡터 색인에 빠진 관측 수 (SQLite 대비)
  ko-index    한국어 형태소 색인 지연
  spool       claude-mem 훅 대기열 정체·만료(만료 = 세션 기록 유실)
  decisions   상태층 마지막 채택 이후 쌓인 [결정] 저장 수

결과는 SB_HOME/logs/health.json 에 남는다. 세션 시작 훅(session_context.py)이 경고 줄을 브리핑에 붙이고,
고칠 수 있는 문제가 있으면 백그라운드로 `--fix` 를 띄운다. 끄기: SB_HEALTH=0 / 자동 복구만 끄기: SB_HEALTH_AUTOFIX=0.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import sb_config  # noqa: E402

OLLAMA_URL = os.environ.get('SB_OLLAMA_URL', 'http://127.0.0.1:11434')
VECTOR_WARN = int(os.environ.get('SB_HEALTH_VECTOR_WARN', '30'))     # 이 수를 넘게 빠지면 경고
KO_WARN = int(os.environ.get('SB_HEALTH_KO_WARN', '50'))
NIGHTLY_MAX_AGE_DAYS = int(os.environ.get('SB_HEALTH_NIGHTLY_DAYS', '1'))
DECISION_WARN = int(os.environ.get('SB_HEALTH_DECISION_WARN', '3'))


def _get(url: str, timeout: float = 2.0) -> Optional[int]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except Exception:  # noqa: BLE001
        return None


def _ro(path) -> sqlite3.Connection:
    return sqlite3.connect('file:%s?mode=ro' % Path(path).as_posix(), uri=True, timeout=2)


def _detached() -> Dict[str, Any]:
    kw: Dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == 'nt':
        kw['creationflags'] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kw['start_new_session'] = True
    return kw


# ---------------------------------------------------------------- 복구 동작

def worker_up() -> bool:
    return _get(sb_config.worker_base_url() + '/api/health') == 200


def plugin_root() -> Optional[Path]:
    """설치된 claude-mem 플러그인 중 가장 높은 버전(고아 표시 제외)."""
    base = Path(os.environ.get('CLAUDE_CONFIG_DIR') or '~/.claude').expanduser() / 'plugins' / 'cache' / 'thedotmack' / 'claude-mem'
    best = None
    for d in base.glob('*') if base.is_dir() else []:
        if (d / '.orphaned_at').exists() or not (d / 'scripts' / 'worker-service.cjs').is_file():
            continue
        try:
            key = tuple(int(x) for x in d.name.split('-')[0].split('.'))
        except ValueError:
            continue
        if best is None or key > best[0]:
            best = (key, d)
    return best[1] if best else None


def ensure_worker(wait: float = 30.0) -> bool:
    """워커가 꺼져 있으면 claude-mem 자체 기동 명령으로 띄운다. 테스트 환경(SB_MEM_BASE_URL)에서는 띄우지 않는다."""
    if worker_up():
        return True
    if os.environ.get('SB_WORKER_AUTOSTART', '1') == '0' or os.environ.get('SB_MEM_BASE_URL'):
        return False
    root, node = plugin_root(), shutil.which('node')
    if not root or not node:
        return False
    runner = root / 'scripts' / 'bun-runner.js'
    cmd = [node, str(runner), str(root / 'scripts' / 'worker-service.cjs'), 'start'] if runner.is_file() \
        else [node, str(root / 'scripts' / 'worker-service.cjs'), 'start']
    try:
        subprocess.run(cmd, timeout=90, **{k: v for k, v in _detached().items() if k != 'close_fds'})
    except Exception:  # noqa: BLE001
        pass
    if _wait_worker(min(wait, 25.0)):   # 느린 PC 는 정상 기동에도 10초 넘게 걸린다 — 대체 경로로 이중 기동하지 않게 충분히 기다린다
        return True
    # Windows 에서 claude-mem 은 PowerShell Start-Process 로 데몬을 띄우는데, 프로세스 기동이 느린 PC 에서는
    # 그 호출이 시간 제한(ETIMEDOUT)에 걸리고 2분간 재시도를 막는다(2026-10-05 확인). 같은 데몬 명령을 직접 띄운다.
    bun = shutil.which('bun') or str(Path.home() / '.bun' / 'bin' / ('bun.exe' if os.name == 'nt' else 'bun'))
    if Path(bun).is_file():
        try:
            subprocess.Popen([bun, str(root / 'scripts' / 'worker-service.cjs'), '--daemon'],
                             cwd=str(root), **_detached())
        except Exception:  # noqa: BLE001
            return False
    return _wait_worker(wait)


def _wait_worker(wait: float) -> bool:
    deadline = time.time() + wait
    while time.time() < deadline:
        if worker_up():
            return True
        time.sleep(1)
    return worker_up()


def ollama_up() -> bool:
    return _get(OLLAMA_URL + '/api/tags', 1.5) == 200


def _ollama_exe() -> Optional[Path]:
    if os.name == 'nt':
        app = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Ollama' / 'ollama app.exe'
        if app.is_file():
            return app
    found = shutil.which('ollama')
    return Path(found) if found else None


def ensure_ollama(wait: float = 20.0) -> bool:
    if ollama_up():
        return True
    if os.environ.get('SB_OLLAMA_AUTOSTART', '1') == '0':
        return False
    exe = _ollama_exe()
    if not exe:
        return False
    cmd = [str(exe)] if exe.name == 'ollama app.exe' else [str(exe), 'serve']
    try:
        subprocess.Popen(cmd, **_detached())
    except Exception:  # noqa: BLE001
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if ollama_up():
            return True
        time.sleep(1)
    return False


def observer_proxy() -> Optional[str]:
    """claude-mem .env 의 ANTHROPIC_BASE_URL(관찰기 프록시, 예: teamclaude). 없으면 None."""
    try:
        for line in (sb_config.claude_mem_dir() / '.env').read_text(encoding='utf-8').splitlines():
            if line.strip().startswith('ANTHROPIC_BASE_URL='):
                return line.split('=', 1)[1].strip() or None
    except OSError:
        pass
    return None


def proxy_up(url: str) -> bool:
    import socket
    from urllib.parse import urlparse
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname or '127.0.0.1', u.port or 80), timeout=1.5):
            return True
    except OSError:
        return False


def ensure_proxy(url: str, wait: float = 20.0) -> bool:
    """관찰기 프록시가 teamclaude(로컬 다계정 프록시)이면 headless 로 띄운다. 끄기: SB_PROXY_AUTOSTART=0."""
    if proxy_up(url):
        return True
    shim = shutil.which('teamclaude') or str(Path(os.environ.get('APPDATA', '')) / 'npm' / 'teamclaude.cmd')
    if os.environ.get('SB_PROXY_AUTOSTART', '1') == '0' or not Path(shim).is_file():
        return False
    try:
        subprocess.Popen([shim, 'server', '--headless'], **_detached())
    except Exception:  # noqa: BLE001
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if proxy_up(url):
            return True
        time.sleep(1)
    return False


# ---------------------------------------------------------------- 점검

def _max_obs(db) -> int:
    return db.execute('SELECT COALESCE(MAX(id),0) FROM observations').fetchone()[0]


def check_nightly() -> Dict[str, Any]:
    p = Path(sb_config.sb_path('logs', 'nightly_status.json'))
    try:
        s = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'ok': False, 'msg': '야간 배치 기록 없음', 'fix': 'nightly'}
    try:
        age = (date.today() - date.fromisoformat(s.get('date'))).days
    except (TypeError, ValueError):
        age = 99
    ok = s.get('status') == 'ok' and age <= NIGHTLY_MAX_AGE_DAYS
    msg = []
    if s.get('status') != 'ok':
        msg.append('야간 배치 {}({})'.format(s.get('status'), ','.join(s.get('failed_stages') or [])))
    if age > NIGHTLY_MAX_AGE_DAYS:
        msg.append('야간 배치 {}일째 미실행'.format(age))
    return {'ok': ok, 'age_days': age, 'status': s.get('status'), 'msg': ' · '.join(msg),
            'fix': None if ok else 'nightly'}


def check_automemory() -> Dict[str, Any]:
    try:
        import sync_automemory
        r = sync_automemory.sync(dry_run=True)
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'msg': '메모리 동기화 점검 실패: {}'.format(e)[:120], 'pending': None}
    pending = [Path(d['path']).name for d in r['details'] if d.get('action') == 'would_save']
    return {'ok': not pending, 'pending': len(pending), 'files': pending[:20],
            'msg': '메모리 동기화 대기 {}건'.format(len(pending)) if pending else '', 'fix': 'automemory' if pending else None}


def check_vector(max_obs_ids) -> Dict[str, Any]:
    chroma = Path(os.environ.get('SB_CHROMA_PATH') or sb_config.claude_mem_dir() / 'chroma') / 'chroma.sqlite3'
    if not chroma.is_file():
        return {'ok': True, 'msg': '', 'skipped': 'no chroma'}
    try:
        with closing(_ro(chroma)) as c:
            rows = c.execute(
                "SELECT DISTINCT m1.int_value FROM embedding_metadata m1 JOIN embedding_metadata m2 "
                "ON m1.id=m2.id AND m2.key='doc_type' AND m2.string_value='observation' "
                "WHERE m1.key='sqlite_id'").fetchall()
    except sqlite3.Error as e:
        return {'ok': False, 'msg': '벡터 색인 읽기 실패: {}'.format(e)[:120]}
    have = {r[0] for r in rows if r[0] is not None}
    missing = [i for i in max_obs_ids if i not in have]
    ok = len(missing) <= VECTOR_WARN
    return {'ok': ok, 'missing': len(missing), 'max_vector_id': max(have) if have else 0,
            'msg': '' if ok else '벡터 미반영 {}건'.format(len(missing)),
            'fix': None if ok else ('ollama' if uses_ollama() else 'worker')}


def check_ko(max_obs: int) -> Dict[str, Any]:
    ko = Path(os.environ.get('SB_KO_INDEX') or sb_config.sb_path('index', 'ko_fts.sqlite'))
    try:
        with closing(_ro(ko)) as c:
            m = int(dict(c.execute('SELECT key, value FROM ko_meta').fetchall()).get('max_id') or 0)
    except (sqlite3.Error, ValueError, OSError):
        return {'ok': False, 'msg': '한국어 색인 없음', 'lag': None}
    lag = max_obs - m
    ok = lag <= KO_WARN
    return {'ok': ok, 'lag': lag, 'msg': '' if ok else '한국어 색인 {}건 지연'.format(lag), 'fix': None if ok else 'ko'}


def check_spool(prev: Dict[str, Any]) -> Dict[str, Any]:
    spool = sb_config.claude_mem_dir() / 'state' / 'hook-spool'
    if not spool.is_dir():
        return {'ok': True, 'msg': ''}
    now = time.time()
    stuck = [p for p in spool.glob('*.json') if now - p.stat().st_mtime > 3600]
    expired = len(list((spool / 'expired').glob('*.json'))) if (spool / 'expired').is_dir() else 0
    new_exp = max(0, expired - int((prev.get('spool') or {}).get('expired') or 0))
    msg = []
    if stuck:
        msg.append('훅 대기열 정체 {}건'.format(len(stuck)))
    if new_exp:
        msg.append('세션 기록 만료(유실) {}건'.format(new_exp))
    return {'ok': not msg, 'stuck': len(stuck), 'expired': expired, 'new_expired': new_exp,
            'msg': ' · '.join(msg), 'fix': 'worker' if stuck else None}


def check_decisions(db) -> Dict[str, Any]:
    try:
        with closing(_ro(sb_config.sb_path('state.db'))) as s:
            last = s.execute('SELECT MAX(accepted_at) FROM state_acceptance').fetchone()[0]
    except sqlite3.Error:
        return {'ok': True, 'msg': ''}
    if not last:
        return {'ok': True, 'msg': ''}
    n = db.execute("SELECT COUNT(*) FROM observations WHERE title LIKE '%·결정]%' AND created_at > ?",
                   (last,)).fetchone()[0]
    ok = n < DECISION_WARN
    return {'ok': ok, 'since': last, 'count': n,
            'msg': '' if ok else '상태층 미반영 [결정] {}건 (마지막 채택 {})'.format(n, last[:10])}


def check_recall(db) -> Dict[str, Any]:
    """찾아다 쓰기 자가 시험: 회수 대상이 될 최근 기록 하나를 골라 그 제목으로 실제 회수(sb_recalld)를 돌리고,
    그 기록이 결과에 나오는지 본다. 색인 누락·회수 서버 고장·필터 과잉 중 무엇이 생겨도 여기서 실패한다."""
    try:
        import sb_recalld
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'msg': '회수 모듈 적재 실패: {}'.format(e)[:120]}
    if not sb_recalld.is_up():
        return {'ok': False, 'msg': '회수 서버 꺼짐(프롬프트별 회수 주입 중단)', 'fix': 'recalld'}
    dead = set()
    try:
        dead = sb_recalld.superseded_ids()
    except Exception:  # noqa: BLE001
        pass
    cutoff = int((time.time() - (sb_recalld.FRESH_MINUTES + 5) * 60) * 1000)
    rows = db.execute('SELECT id, project, title FROM observations WHERE created_at_epoch < ? AND length(title) >= 12 '
                      'ORDER BY id DESC LIMIT 40', (cutoff,)).fetchall()
    sample = next((r for r in rows if r[0] not in dead), None)
    if not sample:
        return {'ok': True, 'msg': '', 'skipped': 'no sample'}
    oid, proj, title = sample
    import urllib.parse
    url = sb_recalld.base_url() + '/recall?' + urllib.parse.urlencode({'q': title, 'project': proj or '', 'limit': '8'})
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            got = [it.get('id') for it in json.loads(r.read().decode('utf-8')).get('items', [])]
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'msg': '회수 질의 실패: {}'.format(e)[:120], 'fix': 'recalld'}
    ok = oid in got
    return {'ok': ok, 'sample_id': oid, 'returned': got[:8],
            'msg': '' if ok else '회수 자가 시험 실패 — 최근 기록 #{}이 제목으로도 안 나옴(색인·필터 점검)'.format(oid),
            'fix': None if ok else 'ko'}


def recall_usage(days: int = 7) -> Dict[str, Any]:
    """활용 통계(정보용, 실패 판정 없음): 최근 N일 회수 주입·빈 결과·기간 질문 안내·확인 게이트 판정 수."""
    since = time.time() - days * 86400
    out = {'ok': True, 'days': days, 'injections': 0, 'empty': 0, 'history_hints': 0, 'gate_block': 0, 'gate_pass': 0}

    def rows(name):
        try:
            with open(sb_config.sb_path('logs', name), encoding='utf-8') as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                        if time.mktime(time.strptime(ev.get('ts', '')[:19], '%Y-%m-%dT%H:%M:%S')) >= since:
                            yield ev
                    except (ValueError, OverflowError):
                        continue
        except OSError:
            return

    for ev in rows('recall.jsonl'):
        out['injections'] += 1
        out['empty'] += 0 if ev.get('ids') else 1
        out['history_hints'] += 1 if ev.get('hint') else 0
    for ev in rows('recall-gate.jsonl'):
        if ev.get('decision') == 'block':
            out['gate_block'] += 1
        elif ev.get('decision') == 'pass':
            out['gate_pass'] += 1
    return out


def uses_ollama() -> bool:
    """설치기가 Ollama 임베딩(bge-m3)을 켠 PC 인가 — ~/.chroma_env 의 CHROMA_OPENAI_API_KEY=ollama 표시.
    기본 임베딩을 쓰는 PC 에서 'Ollama 꺼짐' 경고가 늘 뜨지 않게 한다. SB_HEALTH_OLLAMA=1|0 으로 강제."""
    forced = os.environ.get('SB_HEALTH_OLLAMA')
    if forced in ('0', '1'):
        return forced == '1'
    try:
        return 'CHROMA_OPENAI_API_KEY=ollama' in (Path.home() / '.chroma_env').read_text(encoding='utf-8')
    except OSError:
        return False


def run_checks(services: bool = True, worker: bool = True, recall: bool = True) -> Dict[str, Any]:
    """worker=False: 세션 시작 훅용 — 그 시점엔 claude-mem 이 워커를 막 띄우는 중이라 '꺼짐'이 오탐이 되고,
    같은 순간 복구가 워커를 또 띄우면 충돌한다. 워커는 claude-mem 자체 훅에 맡긴다."""
    prev = load_last()
    out: Dict[str, Any] = {'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    if services:
        if worker:
            w = worker_up()
            out['worker'] = {'ok': w, 'msg': '' if w else 'claude-mem 워커 꺼짐', 'fix': None if w else 'worker'}
        px = observer_proxy()
        if px:
            up = proxy_up(px)
            out['observer-proxy'] = {'ok': up, 'url': px, 'msg': '' if up else '관찰기 프록시(teamclaude) 꺼짐 — 새 기록 생성 중단',
                                     'fix': None if up else 'proxy'}
        if uses_ollama():
            o = ollama_up()
            out['ollama'] = {'ok': o, 'msg': '' if o else 'Ollama 꺼짐(벡터 저장 중단)', 'fix': None if o else 'ollama'}
    out['nightly'] = check_nightly()
    out['automemory'] = check_automemory()
    try:
        with closing(_ro(sb_config.claude_mem_db())) as db:
            ids = [r[0] for r in db.execute('SELECT id FROM observations')]
            out['vector'] = check_vector(ids)
            out['ko-index'] = check_ko(_max_obs(db))
            out['decisions'] = check_decisions(db)
            if recall:
                out['recall'] = check_recall(db)
    except sqlite3.Error as e:
        out['db'] = {'ok': False, 'msg': 'claude-mem DB 읽기 실패: {}'.format(e)[:120]}
    out['spool'] = check_spool(prev)
    out['usage'] = recall_usage()
    problems = [k for k, v in out.items() if isinstance(v, dict) and v.get('ok') is False]
    out['ok'] = not problems
    out['problems'] = problems
    return out


# ---------------------------------------------------------------- 고치기

def installed_home() -> bool:
    """실제 설치된 SB_HOME 인지(야간 배치를 한 번이라도 돌린 흔적). 임시·테스트 SB_HOME 에서 실제 워커에
    쓰는 사고를 막는다 — 2026-10-05 테스트 중 자동 복구가 매핑 없는 프로젝트명으로 메모리를 재저장한 사례."""
    return Path(sb_config.sb_path('logs', 'nightly.log')).exists() and not os.environ.get('SB_HEALTH_NO_WRITE')


def fix(result: Dict[str, Any]) -> List[str]:
    done = []
    if not installed_home():
        return ['skipped: not an installed SB_HOME']
    wants = {v.get('fix') for v in result.values() if isinstance(v, dict) and v.get('fix')}
    if 'proxy' in wants:
        done.append('proxy:' + ('ok' if ensure_proxy(observer_proxy() or '') else 'fail'))
    if wants & {'worker', 'automemory'}:
        done.append('worker:' + ('ok' if ensure_worker() else 'fail'))
    if 'ollama' in wants:
        done.append('ollama:' + ('ok' if ensure_ollama() else 'fail'))
    if 'automemory' in wants and worker_up():
        try:
            import sync_automemory
            r = sync_automemory.sync()
            done.append('automemory:saved={} failed={}'.format(r['saved'], r['failed']))
        except Exception as e:  # noqa: BLE001
            done.append('automemory:error {}'.format(e)[:80])
    if 'recalld' in wants:
        try:
            import sb_recalld
            sb_recalld.ensure_running()
            deadline = time.time() + 30   # 색인 적재까지 기다려야 재점검이 '꺼짐'으로 다시 나오지 않는다
            while time.time() < deadline and not sb_recalld.is_up():
                time.sleep(1)
            done.append('recalld:' + ('ok' if sb_recalld.is_up() else 'starting'))
        except Exception:  # noqa: BLE001
            done.append('recalld:fail')
    if 'ko' in wants:
        try:
            r = subprocess.run([sys.executable, str(BIN / 'sb_fts_ko.py'), 'build'], cwd=str(BIN), timeout=900,
                               capture_output=True, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
            done.append('ko-index:' + ('ok' if r.returncode == 0 else 'exit {}'.format(r.returncode)))
        except Exception:  # noqa: BLE001
            done.append('ko-index:fail')
    if 'nightly' in wants and (result.get('nightly') or {}).get('age_days', 0) > NIGHTLY_MAX_AGE_DAYS:
        try:   # 놓친 야간 배치를 지금 백그라운드로 — 잠금이 있어 중복 실행되지 않는다
            subprocess.Popen([sys.executable, str(BIN / 'nightly.py')], cwd=str(BIN), **_detached())
            done.append('nightly:started')
        except Exception:  # noqa: BLE001
            done.append('nightly:fail')
    return done


def health_path() -> Path:
    return Path(sb_config.sb_path('logs', 'health.json'))


def load_last() -> Dict[str, Any]:
    try:
        return json.loads(health_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def write(result: Dict[str, Any]) -> None:
    p = health_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def warning_line(result: Dict[str, Any]) -> str:
    msgs = [v['msg'] for k, v in result.items() if isinstance(v, dict) and v.get('ok') is False and v.get('msg')]
    return ('⚠ 기록층 점검: ' + ' · '.join(msgs) + ' — 상세 `sb health`') if msgs else ''


def spawn_fix() -> None:
    try:
        subprocess.Popen([sys.executable, str(BIN / 'sb_health.py'), '--fix', '--quiet', '--startup'],
                         cwd=str(BIN), **_detached())
    except Exception:  # noqa: BLE001
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='sb health', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', nargs='?', choices=('check', 'ensure'), default='check')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--fix', action='store_true')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--startup', action='store_true',
                    help='세션 시작 훅이 띄운 복구 — claude-mem 이 워커를 다 띄울 시간을 먼저 준다(동시 기동 충돌 방지)')
    a = ap.parse_args(argv)
    if a.action == 'ensure':
        px = observer_proxy()
        p = ensure_proxy(px) if px else None
        w, o = ensure_worker(), (ensure_ollama() if uses_ollama() else None)
        print(json.dumps({'worker': w, 'ollama': o, 'observer_proxy': p}))
        return 0
    if a.startup:
        time.sleep(float(os.environ.get('SB_HEALTH_STARTUP_WAIT', '20')))
    result = run_checks()
    if a.fix and not result['ok']:
        result['fixed'] = fix(result)
        time.sleep(3)
        after = run_checks()
        after['fixed'] = result['fixed']
        result = after
    write(result)
    if a.quiet:
        return 0
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        line = warning_line(result)
        print(line or '✓ 기록층 정상 (워커·임베딩·야간 배치·메모리 동기화·벡터·색인·회수 자가 시험)')
        u = result.get('usage') or {}
        if u.get('injections') or u.get('gate_block') or u.get('gate_pass'):
            print('  회수 활용(최근 {}일): 주입 {}회 · 빈 결과 {}회 · 기간 질문 안내 {}회 · 확인 게이트 차단 {}회/통과 {}회'.format(
                u['days'], u['injections'], u['empty'], u['history_hints'], u['gate_block'], u['gate_pass']))
        if result.get('fixed'):
            print('  조치: ' + ', '.join(result['fixed']))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
