#!/usr/bin/env python3
"""세컨브레인 볼트 구성: 쌓인 기록을 읽어 프로젝트(scope)·별칭·레이어 대상을 제안하고, 승인한 것만 적용한다.

  sb vault analyze [--json]                  기록 분석(읽기 전용) → $SB_HOME/vault/analysis-<시각>.json
  sb vault plan [--analysis F] [--json]      분석으로 구성안 → $SB_HOME/vault/plan-<시각>.json + 요약
  sb vault apply --plan F [--yes --hash H]   구성안 적용(백업 → 쓰기 → 검증 → 실패 시 자동 되돌리기)
  sb vault rollback [--apply F]              마지막(또는 지정한) 적용을 되돌린다
  sb vault status [--json]                   데이터 양·현재 볼트·마지막 적용
  sb vault init                              기본 볼트(기록이 없거나 적을 때). 이미 있으면 그대로

원칙
- 분석은 이 PC 안에서만 한다. LLM 은 기본으로 끄며, SB_VAULT_LLM=on 과 볼트 전용 주소(SB_VAULT_LLM_URL)를 둘 다 줄 때만 쓴다.
  LLM 에는 폴더 이름 조각만 보낸다(대화 내용·관측 본문은 보내지 않는다).
- 분류 체계를 새로 만들지 않는다. 관측 type 분포와 폴더·git·자동 메모리 신호만 쓴다.
- 적용은 기존 값을 덮어쓰지 않는다. 같은 키가 다른 값이면 기존 값을 두고 conflicts 로 보고한다.
- 수집 제외(claude-mem CLAUDE_MEM_EXCLUDED_PROJECTS)는 보고만 한다 — 바꾸면 기록이 사라진다.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.request
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import sb_config  # noqa: E402
import sb_lock  # noqa: E402
import sb_scope  # noqa: E402
import sync_automemory  # noqa: E402

SCHEMA = 1
# 데이터 양 문턱 — 운영본 실측(11-15): 하루 관측 2천~1만8천 건이라 관측 수만으로는 하루 만에 넘는다.
# 그래서 홈·임시를 뺀 작업 폴더 수를 같이 본다(M1 baseline_vault.md §1-4).
ENOUGH_OBS, ENOUGH_FOLDERS, ENOUGH_SESSIONS = 500, 3, 30
MIN_SCOPE_OBS, MIN_SCOPE_SESSIONS = 20, 3     # 이보다 적은 폴더는 scope 로 제안하지 않는다(보고만)
ACTIVE_DAYS = 30                              # 최근 이 기간에 기록이 있어야 상태층 쓰기 대상으로 제안
GENERIC_NAMES = {'src', 'app', 'apps', 'code', 'project', 'projects', 'workspace', 'work', 'repo', 'repos', 'tmp',
                 'test', 'tests', 'dev', 'main', 'new', 'old', 'backup', 'output', 'build', 'dist', 'docs'}
STATE_ENV = 'SB_STATE_PILOT_SCOPES'


# ---------------------------------------------------------------- 경로·시각

def vault_dir() -> Path:
    return sb_config.home() / 'vault'


def vault_json_path() -> Path:
    return sb_config.home() / 'config' / 'vault.json'


def loops_path() -> Path:
    return Path(os.environ.get('SB_LOOPS_PATH') or sb_config.sb_path('loops', 'loops.jsonl'))


def claude_settings_path() -> Path:
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or '~/.claude').expanduser() / 'settings.json'


def stamp() -> str:
    return time.strftime('%Y%m%d-%H%M%S')


def now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def _home() -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser('~')))


def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(p)))


def _short(p: str) -> str:
    """보고용 경로 — 홈은 ~ 로 줄인다."""
    h = os.path.expanduser('~')
    return '~' + p[len(h):] if p == h or p.startswith(h + os.sep) else p


def classify_path(p: str) -> str:
    """project | home | temp | automation. 홈·시스템 뿌리·임시·에이전트 내부 폴더는 scope 로 쓰지 않는다."""
    real = _norm(p)
    home = _home()
    if real == home or real in (os.path.normcase(os.path.realpath(os.sep)),) or len(real) <= 3:
        return 'home'
    for d in ('.cache', 'tmp', '.tmp', 'Temp', os.path.join('AppData', 'Local', 'Temp')):   # 홈 아래 임시 폴더
        if sb_scope.path_within(real, os.path.normcase(os.path.join(home, d))):
            return 'temp'
    for d in ('.claude', '.claude-mem', '.codex', '.gemini', '.omo', 'Library'):
        if sb_scope.path_within(real, _norm(os.path.join(home, d))):   # 심볼릭 링크로 옮긴 폴더도 같은 뿌리로 본다
            return 'automation'
    # 임시 폴더를 홈 판정보다 먼저 본다(%TEMP% 가 홈 아래인 윈도우). 단 홈 자체를 품은 임시 뿌리는 빼고 본다 —
    # 홈이 임시 폴더 아래면(격리 시험·일회용 계정) 그 홈 아래 전부가 임시가 되어 버린다. 그 홈 밖의 임시 폴더는 그대로 임시다.
    temp = [_norm(r) for r in sync_automemory.temp_roots()]
    if any(sb_scope.path_within(real, r) and not (sb_scope.path_within(home, r) and sb_scope.path_within(real, home))
           for r in temp):
        return 'temp'
    if sb_scope.path_within(real, home):
        return 'project'
    # 홈 밖(다른 볼륨·D:\work 등)은 git 저장소일 때만 프로젝트로 본다 — 아니면 보통 도구가 연 폴더다
    return 'project' if os.path.isdir(real) and git_root(real) else 'automation'


def git_root(p: str) -> Optional[str]:
    try:
        r = subprocess.run(['git', '-C', p, 'rev-parse', '--show-toplevel'], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return os.path.realpath(out) if r.returncode == 0 and out else None


# ---------------------------------------------------------------- 원자료 읽기(읽기 전용)

def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect('file:%s?mode=ro' % Path(path).as_posix(), uri=True, timeout=10)


def read_claude_mem(db_path: Optional[str] = None) -> Dict[str, Any]:
    """세션 cwd·관측 꼬리표·관측 type 분포. DB 가 없으면 빈 결과."""
    db_path = db_path or sb_config.claude_mem_db()
    out: Dict[str, Any] = {'readable': False, 'sessions': [], 'tags': {}, 'types': {}, 'obs_total': 0,
                           'session_total': 0, 'span': [None, None]}
    if not Path(db_path).is_file():
        out['reason'] = 'claude-mem.db 없음'
        return out
    try:
        with closing(_ro(db_path)) as db:
            cols = {r[1] for r in db.execute('PRAGMA table_info(sdk_sessions)')}
            cwd_col = 'cwd' if 'cwd' in cols else 'NULL'
            per_session: Dict[str, Tuple[int, int]] = {}
            for sid, n, last in db.execute('SELECT memory_session_id, COUNT(*), MAX(created_at_epoch) '
                                           'FROM observations GROUP BY memory_session_id'):
                per_session[sid] = (n, last or 0)
            for sid, project, cwd, started in db.execute(
                    'SELECT memory_session_id, project, %s, started_at_epoch FROM sdk_sessions' % cwd_col):
                n, last = per_session.get(sid, (0, 0))
                out['sessions'].append({'project': project, 'cwd': cwd, 'obs': n, 'last': max(last, started or 0)})
            for tag, n, last in db.execute('SELECT project, COUNT(*), MAX(created_at_epoch) FROM observations GROUP BY project'):
                out['tags'][tag] = {'obs': n, 'last': last or 0}
            out['types'] = {t: n for t, n in db.execute('SELECT type, COUNT(*) FROM observations GROUP BY type ORDER BY 2 DESC')}
            lo, hi = db.execute('SELECT MIN(created_at_epoch), MAX(created_at_epoch) FROM observations').fetchone()
            out['span'] = [lo, hi]
            out['max_id'] = _max_obs_id(db)
    except sqlite3.Error as e:
        out['reason'] = 'claude-mem.db 읽기 실패: %s' % e
        return out
    out['readable'] = True
    out['obs_total'] = sum(v['obs'] for v in out['tags'].values())
    out['session_total'] = len(out['sessions'])
    return out


def read_project_dirs(root: Optional[str] = None) -> List[Dict[str, Any]]:
    """Claude Code 프로젝트 폴더별 (작업 폴더, 자동 메모리 수, feedback 수). 임시 폴더 슬러그는 건너뛴다."""
    import sb_rules
    base = sync_automemory.projects_root(root)
    out = []
    for d in sorted(base.glob('*')) if base.is_dir() else []:
        if not d.is_dir() or sync_automemory.is_temp_slug(d.name):
            continue
        mem = d / 'memory'
        files = [p for p in mem.glob('*.md') if p.name != 'MEMORY.md'] if mem.is_dir() else []
        feedback = 0
        for p in files:
            try:
                head = p.read_text(encoding='utf-8', errors='replace')[:600]
            except OSError:
                continue
            if re.search(r'^\s*type:\s*feedback\b', head, re.M):
                feedback += 1
        cwd = sb_rules.session_cwd(d)
        out.append({'slug': d.name, 'cwd': cwd, 'memories': len(files), 'feedback': feedback,
                    'name': sync_automemory.project_from_slug(d.name)})
    return out


def capture_excluded() -> List[str]:
    raw = (sb_config.claude_mem_settings() or {}).get('CLAUDE_MEM_EXCLUDED_PROJECTS') or ''
    return [x.strip() for x in str(raw).split(',') if x.strip()]


# ---------------------------------------------------------------- 분석

def _suggest_key(root: str, taken: Dict[str, str]) -> str:
    base = os.path.basename(root.rstrip(os.sep)) or root
    key = base
    if key.lower() in GENERIC_NAMES or (key in taken and taken[key] != root):
        parent = os.path.basename(os.path.dirname(root.rstrip(os.sep)))
        key = '%s-%s' % (parent, base) if parent else base
    return key


def analyze(db_path: Optional[str] = None, projects_root: Optional[str] = None, use_git: bool = True,
            llm: Optional[str] = None) -> Dict[str, Any]:
    cm = read_claude_mem(db_path)
    dirs = read_project_dirs(projects_root)
    aliases = sb_scope._load_aliases()

    def alias_of(p: str) -> Optional[str]:
        # sb_scope 와 같은 규칙: 경로 별칭(가장 긴 상위) > 이름 별칭(경로 항목 없는 키, 하나만 맞을 때)
        key, method, _ = sb_scope.resolve_scope_id(p, aliases=aliases, use_git=False)
        return key if method in ('alias-path', 'alias-name') else None

    # 1) 작업 폴더별 집계
    folders: Dict[str, Dict[str, Any]] = defaultdict(lambda: {'obs': 0, 'sessions': 0, 'last': 0, 'tags': defaultdict(int),
                                                            'memories': 0, 'feedback': 0, 'signals': set()})
    for s in cm['sessions']:
        if not s['cwd']:
            continue
        f = folders[os.path.realpath(s['cwd'])]
        f['obs'] += s['obs']
        f['sessions'] += 1
        f['last'] = max(f['last'], s['last'])
        f['tags'][s['project']] += s['obs']
        f['signals'].add('session_cwd')
    for d in dirs:
        if not d['cwd']:
            continue
        f = folders[os.path.realpath(d['cwd'])]
        f['memories'] += d['memories']
        f['feedback'] += d['feedback']
        f['signals'].add('memory_dir')
    # 꼬리표만 있고 cwd 가 없는 관측(옛 세션)은 이름이 같은 폴더에 붙인다: 홈 바로 아래 같은 이름 폴더,
    # 없으면 이미 아는 폴더 중 이름이 같은 것이 하나뿐일 때 그 폴더. 둘 다 없으면 보고만 한다.
    known_tags = {t for f in folders.values() for t in f['tags']}
    home = os.path.expanduser('~')
    by_base: Dict[str, List[str]] = defaultdict(list)
    for fp in list(folders):
        by_base[os.path.basename(fp.rstrip(os.sep))].append(fp)
    unmapped = {}
    for tag, v in cm['tags'].items():
        if tag in known_tags or not tag or os.sep in tag:
            if tag not in known_tags:
                unmapped[tag] = v['obs']
            continue
        same = by_base.get(tag, [])
        # 홈 바로 아래 같은 이름 폴더가 먼저다(워커가 cwd 이름으로 꼬리표를 단 옛 세션 대부분이 여기다)
        cand = os.path.join(home, tag) if os.path.isdir(os.path.join(home, tag)) else same[0] if len(same) == 1 else None
        if not cand:
            unmapped[tag] = v['obs']
            continue
        f = folders[os.path.realpath(cand)]
        f['obs'] += v['obs']
        f['last'] = max(f['last'], v['last'])
        f['tags'][tag] += v['obs']
        f['signals'].add('tag_name')

    # 2) 폴더 → 프로젝트 뿌리: 기존 별칭 > git 최상위 > 이미 뿌리인 상위 폴더 > 자기 자신
    classes = {p: classify_path(p) for p in folders}
    roots: Dict[str, Tuple[str, str]] = {}   # 폴더 → (뿌리, 방법)
    for p in folders:
        if classes[p] != 'project':
            continue
        key = alias_of(p)
        if key:
            roots[p] = ('alias:' + key, 'alias')
            continue
        g = git_root(p) if use_git else None
        base = os.path.basename(p.rstrip(os.sep))
        # 상위 저장소 안이어도 자기 이름 꼬리표로 관측이 충분히 쌓인 폴더는 따로 선 프로젝트다
        # (운영본 실측: 플러그인 저장소 안 플러그인, 워크스페이스 공장 산출물 폴더)
        if g and _norm(g) != _norm(p) and folders[p]['tags'].get(base, 0) >= MIN_SCOPE_OBS:
            roots[p] = (p, 'own_tag')
        elif g and classify_path(g) == 'project':
            roots[p] = (g, 'git')
    project_folders = sorted((p for p in folders if classes[p] == 'project' and p not in roots), key=len)
    for p in project_folders:   # 짧은 것(상위)부터 — 상위가 먼저 뿌리를 얻고, 하위는 가장 가까운 상위의 뿌리를 따른다
        ancestors = [r for r in project_folders if r != p and r in roots and sb_scope.path_within(_norm(p), _norm(r))]
        parent = max(ancestors, key=len) if ancestors else None
        roots[p] = (roots[parent][0], 'parent') if parent else (p, 'folder')

    # 3) 뿌리별 후보
    groups: Dict[str, Dict[str, Any]] = {}
    for p, (root, method) in roots.items():
        g = groups.setdefault(root, {'paths': [], 'obs': 0, 'sessions': 0, 'last': 0, 'tags': defaultdict(int),
                                     'memories': 0, 'feedback': 0, 'signals': set()})
        f = folders[p]
        g['paths'].append(p)
        for k in ('obs', 'sessions', 'memories', 'feedback'):
            g[k] += f[k]
        g['last'] = max(g['last'], f['last'])
        for t, n in f['tags'].items():
            g['tags'][t] += n
        g['signals'] |= f['signals'] | {method}
    taken: Dict[str, str] = {k: next((os.path.expanduser(e) for e in v if sb_scope.is_path_entry(e)), '') or ('alias:' + k)
                             for k, v in aliases.items()}   # 기존 별칭 키는 이미 쓰인 이름이다
    projects = []
    for root, g in sorted(groups.items(), key=lambda kv: -kv[1]['obs']):
        if root.startswith('alias:'):
            key = root.split(':', 1)[1]
            # 기존 별칭 경로 중 지금 있는 폴더(없으면 실제로 관측된 가장 짧은 폴더) — 지워진 경로를 뿌리로 쓰지 않는다
            live = [os.path.expanduser(e) for e in aliases.get(key, []) if sb_scope.is_path_entry(e)
                    and os.path.isdir(os.path.expanduser(e))]
            real_root = live[0] if live else sorted(g['paths'], key=len)[0]
            conf = 1.0
        else:
            real_root = root
            key = _suggest_key(root, taken)
            conf = 1.0 if 'git' in g['signals'] else 0.7
        norm_taken = {sb_scope._norm_name(k): k for k in taken}
        if root.startswith('alias:'):
            pass
        elif sb_scope._norm_name(key) in norm_taken and taken[norm_taken[sb_scope._norm_name(key)]] != real_root:
            # sb_scope 의 이름 비교 규칙(대소문자·_/- 무시)으로도 겹치면 같은 이름이다 — 결정적인 꼬리 번호를 붙인다
            n = 2
            while sb_scope._norm_name('%s-%d' % (key, n)) in norm_taken:
                n += 1
            key = '%s-%d' % (key, n)
        taken.setdefault(key, real_root)
        projects.append({
            'scope_id': sb_scope.resolve_scope_id(real_root, use_git=use_git)[0] if os.path.isdir(real_root) else key,
            'suggested_key': key, 'root': real_root, 'paths': sorted(g['paths'], key=len)[:20],
            'path_count': len(g['paths']), 'obs_count': g['obs'], 'session_count': g['sessions'],
            'last_seen': time.strftime('%Y-%m-%d', time.localtime(g['last'] / 1000)) if g['last'] else None,
            'last_epoch_ms': g['last'], 'memories': g['memories'], 'feedback': g['feedback'],
            'observed_tags': sorted(g['tags'], key=lambda t: -g['tags'][t])[:10],
            'signals': sorted(g['signals']), 'class': 'project', 'confidence': conf,
            'existing_alias': root.startswith('alias:'),
        })
    excluded = []
    for p, c in classes.items():
        if c != 'project':
            f = folders[p]
            excluded.append({'path': p, 'class': c, 'obs_count': f['obs'], 'session_count': f['sessions']})
    excluded.sort(key=lambda x: -x['obs_count'])

    llm_info = {'used': False, 'route': None, 'calls': 0}
    if llm_mode(llm) == 'on':
        llm_info = refine_names(projects, llm)

    work_folders = sum(1 for p in projects if p['obs_count'] >= MIN_SCOPE_OBS or p['session_count'] >= MIN_SCOPE_SESSIONS)
    obs, sess = cm['obs_total'], cm['session_total']
    volume = 'empty' if obs == 0 and sess == 0 and not dirs else \
        'enough' if (obs >= ENOUGH_OBS and work_folders >= ENOUGH_FOLDERS) or sess >= ENOUGH_SESSIONS else 'thin'
    return {
        'version': SCHEMA, 'created_at': now_iso(),
        'sources': [
            {'name': 'claude-mem', 'path': _short(db_path or sb_config.claude_mem_db()), 'readable': cm['readable'],
             'count': obs, 'sessions': sess, 'reason': cm.get('reason'), 'max_id': cm.get('max_id', 0),
             'span': [time.strftime('%Y-%m-%d', time.localtime(x / 1000)) if x else None for x in cm['span']]},
            {'name': 'claude-projects', 'path': _short(str(sync_automemory.projects_root(projects_root))),
             'readable': bool(dirs), 'count': len(dirs),
             'memories': sum(d['memories'] for d in dirs), 'feedback': sum(d['feedback'] for d in dirs)},
        ],
        'projects': projects, 'excluded': excluded[:200], 'excluded_count': len(excluded),
        'unmapped_tags': dict(sorted(unmapped.items(), key=lambda kv: -kv[1])[:30]),
        'kinds': dict(list(cm['types'].items())[:12]),
        'kinds_other': max(0, len(cm['types']) - 12),
        'rule_candidates': sum(d['feedback'] for d in dirs),
        'capture_excluded': capture_excluded(),
        'existing_aliases': len(aliases),
        'llm': llm_info, 'data_volume': volume, 'work_folders': work_folders,
    }


# ---------------------------------------------------------------- LLM(선택)

def llm_mode(value: Optional[str] = None) -> str:
    """기본은 끔. SB_VAULT_LLM=on 일 때만 쓴다(폴더 이름도 업무 정보일 수 있어 명시적으로 켠다)."""
    v = (value or os.environ.get('SB_VAULT_LLM') or 'off').strip().lower()
    return 'on' if v in ('on', '1', 'true', 'yes') else 'off'


def llm_route() -> Optional[Tuple[str, str, str]]:
    """(url, model, key). 볼트 전용 설정만 쓴다 — 다른 기능의 LLM 설정을 몰래 재사용하지 않는다."""
    url, model = os.environ.get('SB_VAULT_LLM_URL'), os.environ.get('SB_VAULT_MODEL')
    return (url, model, os.environ.get('SB_VAULT_LLM_KEY') or '') if url and model else None


def needs_name(p: Dict[str, Any]) -> bool:
    base = os.path.basename(p['root'].rstrip(os.sep)).lower()
    return not p['existing_alias'] and (base in GENERIC_NAMES or bool(re.fullmatch(r'[\d._-]+', base))
                                        or p['suggested_key'] != os.path.basename(p['root'].rstrip(os.sep)))


def refine_names(projects: List[Dict[str, Any]], llm: Optional[str] = None) -> Dict[str, Any]:
    """이름이 애매한 후보(흔한 폴더 이름·충돌)만 LLM 에 짧은 이름을 묻는다. 경로 조각만 보낸다."""
    route = llm_route()
    info = {'used': False, 'route': None, 'calls': 0}
    if not route:
        return info
    url, model, key = route
    todo = [p for p in projects if needs_name(p)][:20]
    if not todo:
        return info
    info['route'] = re.sub(r'^(https?://[^/]+).*$', r'\1', url)
    items = [{'i': i, 'path_parts': [x for x in _short(p['root']).split(os.sep) if x][-3:]} for i, p in enumerate(todo)]
    prompt = ('다음 작업 폴더마다 프로젝트를 가리키는 짧은 영문 소문자 kebab-case 이름 하나를 정해 JSON 배열 '
              '[{"i":번호,"name":"이름"}] 로만 답하라.\n' + json.dumps(items, ensure_ascii=False))
    body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'temperature': 0}).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json',
                                                          **({'Authorization': 'Bearer ' + key} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            text = json.loads(r.read().decode('utf-8'))['choices'][0]['message']['content']
        info['calls'] = 1
        names = json.loads(re.search(r'\[.*\]', text, re.S).group(0))
    except Exception as e:  # noqa: BLE001
        info['error'] = type(e).__name__
        return info
    used = {p['suggested_key'] for p in projects}
    for n in names if isinstance(names, list) else []:
        try:
            p, name = todo[int(n['i'])], str(n['name']).strip()
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if re.fullmatch(r'[a-z0-9][a-z0-9_-]{1,40}', name) and name not in used:
            used.add(name)
            p['suggested_key'], p['confidence'] = name, 0.6
            p['signals'] = sorted(set(p['signals']) | {'llm_label'})
    info['used'] = True
    return info


# ---------------------------------------------------------------- 구성안

def plan_hash(plan: Dict[str, Any]) -> str:
    body = {k: v for k, v in plan.items() if k not in ('plan_hash', 'created_at')}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def read_json_file(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def current_env_value(name: str) -> Optional[str]:
    s = read_json_file(claude_settings_path()) or {}
    v = (s.get('env') or {}).get(name)
    return v if isinstance(v, str) else None


def make_plan(analysis: Dict[str, Any], analysis_ref: Optional[str] = None, state_write_mode: str = 'all',
              now_ms: Optional[int] = None) -> Dict[str, Any]:
    now_ms = now_ms or int(time.time() * 1000)
    active_ms = ACTIVE_DAYS * 86400 * 1000
    aliases = sb_scope._load_aliases()
    alias_path_owner = {_norm(e): k for k, v in aliases.items() for e in v if sb_scope.is_path_entry(e)}
    scopes, new_aliases, conflicts, minor = [], {}, [], []
    by_norm: Dict[str, List[str]] = {}
    for k in aliases:
        by_norm.setdefault(sb_scope._norm_name(k), []).append(k)
    for keys in by_norm.values():
        if len(keys) > 1:   # sb_scope 는 대소문자·_/- 를 무시해 비교한다 — 기존 별칭끼리 이미 겹치면 어느 쪽인지 모른다
            conflicts.append({'target': 'project_aliases', 'key': ','.join(sorted(keys)), 'existing': sorted(keys),
                              'proposed': None, 'blocking': True})
    for p in analysis.get('projects', []):
        if p['obs_count'] < MIN_SCOPE_OBS and p['session_count'] < MIN_SCOPE_SESSIONS:
            minor.append(p['suggested_key'])
            continue
        key = p['suggested_key']
        active = bool(p.get('last_epoch_ms')) and now_ms - p['last_epoch_ms'] <= active_ms
        scopes.append({'key': key, 'root': p['root'], 'paths': [p['root']], 'obs_count': p['obs_count'],
                       'state_write': active, 'rules': p.get('feedback', 0) > 0, 'loops': True,
                       'last_seen': p.get('last_seen'), 'existing_alias': p.get('existing_alias', False)})
        if p.get('existing_alias'):
            continue
        owner = alias_path_owner.get(_norm(p['root']))
        if key in aliases:
            paths = [_norm(e) for e in aliases[key] if sb_scope.is_path_entry(e)]
            if _norm(p['root']) not in paths:
                conflicts.append({'target': 'project_aliases', 'key': key, 'existing': aliases[key], 'proposed': [p['root']]})
            continue
        if owner and owner != key:
            conflicts.append({'target': 'project_aliases', 'key': key, 'existing': {owner: p['root']}, 'proposed': [p['root']]})
            continue
        resolved = sb_scope.resolve_scope_id(p['root'], use_git=True)[0] if os.path.isdir(p['root']) else None
        if resolved != key:
            new_aliases[key] = [p['root']]
    state_list = sorted(s['key'] for s in scopes if s['state_write'])
    env_cur = current_env_value(STATE_ENV)
    env_new = ','.join(state_list) if state_write_mode == 'listed' and state_list else None
    if env_new is not None and env_cur and set(env_cur.split(',')) != set(state_list):
        conflicts.append({'target': 'settings.env.' + STATE_ENV, 'key': STATE_ENV, 'existing': env_cur, 'proposed': env_new})
        env_new = None
    plan = {
        'version': SCHEMA, 'created_at': now_iso(), 'analysis_ref': analysis_ref,
        'data_volume': analysis.get('data_volume'),
        'aliases_path': sb_scope.aliases_path(),
        'scopes': scopes, 'aliases': new_aliases,
        'kind_policy': {'default': 'normal'},
        'state_write_mode': state_write_mode,
        'state_env': env_new,
        'scope_excluded': sorted({_short(e['path']) for e in analysis.get('excluded', []) if e['class'] in ('home', 'temp')}
                                 | {'~', '/tmp', '/private/tmp', '/private/var/folders'}),
        'capture_excluded': analysis.get('capture_excluded', []),
        'minor_folders': minor[:50], 'conflicts': conflicts,
        'obs_seen': (analysis.get('sources') or [{}])[0].get('max_id') or (analysis.get('sources') or [{}])[0].get('count', 0),
    }
    plan['plan_hash'] = plan_hash(plan)
    return plan


def render_plan(plan: Dict[str, Any]) -> str:
    lines = ['볼트 구성안 (데이터 양: %s, 해시 %s)' % (plan.get('data_volume'), plan['plan_hash'][:12]), '']
    lines.append('프로젝트 %d개 (상태층 쓰기 대상 %d개, 규칙층 대상 %d개)' % (
        len(plan['scopes']), sum(s['state_write'] for s in plan['scopes']), sum(s['rules'] for s in plan['scopes'])))
    for s in plan['scopes'][:30]:
        lines.append('  %-28s 관측 %6d  최근 %s  %s%s' % (
            s['key'], s['obs_count'], s.get('last_seen') or '-', '쓰기' if s['state_write'] else '읽기',
            '' if s.get('existing_alias') else '  (새 이름)' if s['key'] in plan['aliases'] else ''))
    if len(plan['scopes']) > 30:
        lines.append('  … %d개 더' % (len(plan['scopes']) - 30))
    lines.append('')
    lines.append('새 별칭 %d개 → %s' % (len(plan['aliases']), _short(plan['aliases_path'])))
    if plan['state_env']:
        lines.append('%s = %s' % (STATE_ENV, plan['state_env']))
    else:
        lines.append('%s: 바꾸지 않음(%s)' % (STATE_ENV, '전 scope 쓰기 허용 유지' if plan['state_write_mode'] == 'all' else '충돌 또는 대상 없음'))
    lines.append('scope 제외: ' + ', '.join(plan['scope_excluded'][:8]))
    if plan['capture_excluded']:
        lines.append('수집 제외(보고만, 바꾸지 않음): ' + ', '.join(plan['capture_excluded'][:8]))
    if plan['conflicts']:
        lines.append('충돌 %d건 — 기존 값을 유지한다:' % len(plan['conflicts']))
        for c in plan['conflicts'][:10]:
            lines.append('  %s %s: 기존 %s / 제안 %s' % (c['target'], c['key'], c['existing'], c['proposed']))
    return '\n'.join(lines)


# ---------------------------------------------------------------- 적용·되돌리기

def _sha(p: Path) -> Optional[str]:
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _real(p: Path) -> Path:
    """쓰기 대상의 실제 파일. 심볼릭 링크(dotfiles 저장소로 연결한 settings.json 등)는 링크를 남기고 대상을 바꾼다."""
    return Path(os.path.realpath(p)) if Path(p).is_symlink() else Path(p)


class Changed(RuntimeError):
    """쓰기 직전 대상 파일이 예상한 내용이 아니다(다른 프로세스가 고쳤다)."""


_KEEP = object()


def _bytes(data: Any) -> bytes:
    return data if isinstance(data, bytes) else data.encode('utf-8')


def _atomic_write(p: Path, data: Any, private: bool = False, expect_sha: Any = _KEEP,
                  mode: Optional[int] = None) -> None:
    """같은 폴더의 고유 임시 파일(O_EXCL)에 쓰고 fsync 뒤 바꿔 끼운다. 기존 파일 권한은 유지한다
    (mode 를 주면 기존 권한과 상관없이 그 권한으로 — 본인 전용 기록이 0644 로 바뀌어 있어도 0600 으로 되돌린다).
    바이트 그대로 쓴다(윈도우 텍스트 모드의 개행 변환이 없어 기록한 해시와 실제 파일 해시가 같다).
    expect_sha 를 주면 바꿔 끼우기 바로 전에 대상의 해시를 다시 보고 다르면 Changed 로 멈춘다(None = 없어야 함).
    한계: 해시 확인과 rename 사이의 짧은 틈은 남는다. 키트 프로세스끼리는 _Lock 이 직렬화하지만, 잠금 규약을
    모르는 다른 도구(편집기 등)가 바로 그 순간에 쓰는 것은 표준 라이브러리로 막을 수 없다. 단일 사용자의 로컬
    설정 파일이 전제다. 적용 뒤에는 쓴 파일의 해시를 다시 확인해 어긋나면 실패로 보고 되돌린다(apply_plan)."""
    p = _real(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = p.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o600 if private else 0o644
    fd, tmp = tempfile.mkstemp(prefix='.' + p.name + '.', suffix='.sbvault-tmp', dir=str(p.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(_bytes(data))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        if expect_sha is not _KEEP and _sha(p) != expect_sha:
            raise Changed(str(p))
        os.replace(tmp, p)
        if os.name != 'nt':   # 이름 바꾸기 자체도 디스크에 남긴다
            try:
                dfd = os.open(str(p.parent), os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _Lock:
    """볼트 적용·되돌리기를 한 번에 하나만 돌린다(같은 SB_HOME). 잡지 못하면 바로 실패한다.
    OS 파일 잠금(POSIX flock, 윈도우 LockFileEx — sb_lock)이라 잡은 프로세스가 죽으면 OS 가 풀어 준다.
    오래 걸리는 정상 작업의 잠금을 나이로 빼앗는 일이 없다. 잠금 파일은 지우지 않는다(지우면 두 프로세스가
    옛 파일과 새 파일을 따로 잠글 수 있다)."""

    def __init__(self):
        self.path = vault_dir() / '.apply.lock'
        self.f = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        self.f = os.fdopen(fd, 'r+b')
        try:
            sb_lock.lock(self.f, sb_lock.LOCK_EX | sb_lock.LOCK_NB)
        except OSError:
            self.f.close()
            self.f = None
            raise SystemExit('다른 sb vault 적용·되돌리기가 진행 중입니다(%s) — 끝난 뒤 다시 하세요' % _short(str(self.path)))
        return self

    def __exit__(self, *a):
        try:
            sb_lock.lock(self.f, sb_lock.LOCK_UN)
        except OSError:
            pass
        finally:
            self.f.close()
        return False


def _targets(plan: Dict[str, Any]) -> Dict[str, Path]:
    t = {'vault_json': vault_json_path(), 'aliases': Path(plan.get('aliases_path') or sb_scope.aliases_path()),
         'loops': loops_path()}
    if plan.get('state_env'):
        t['settings'] = claude_settings_path()
    return t


def _load_obj(p: Path, what: str) -> Dict[str, Any]:
    """있으면 JSON 객체여야 한다. 없으면 {}. 깨졌거나 읽을 수 없으면 적용 전에 멈춘다(P0-1: 빈 객체로 덮지 않는다)."""
    if not Path(p).exists():
        return {}
    try:
        data = json.loads(Path(p).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise SystemExit('%s 를 읽을 수 없어 적용하지 않았습니다(%s: %s) — 파일을 고친 뒤 다시 하세요'
                         % (what, _short(str(p)), type(e).__name__))
    if not isinstance(data, dict):
        raise SystemExit('%s 가 JSON 객체가 아니라 적용하지 않았습니다: %s' % (what, _short(str(p))))
    return data


def verify_applied(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    checks = []
    v = read_json_file(vault_json_path())
    checks.append({'check': 'vault.json', 'ok': isinstance(v, dict) and v.get('plan_hash') == plan['plan_hash']})
    path = plan.get('aliases_path') or sb_scope.aliases_path()
    table = sb_scope._load_aliases(path)
    for key, paths in plan['aliases'].items():
        got = sb_scope.resolve_scope_id(paths[0], aliases=table, use_git=False)[0] if os.path.isdir(paths[0]) else key
        checks.append({'check': 'alias ' + key, 'ok': got == key})
    if plan.get('state_env'):
        checks.append({'check': STATE_ENV, 'ok': current_env_value(STATE_ENV) == plan['state_env']})
    checks.append({'check': 'loops.jsonl', 'ok': loops_path().is_file()})
    return checks


def _check_hash(plan: Dict[str, Any], approved_hash: str) -> None:
    if plan_hash(plan) != plan.get('plan_hash'):
        raise SystemExit('구성안 파일이 만든 뒤 바뀌었습니다(plan_hash 불일치) — sb vault plan 을 다시 돌리세요')
    h = (approved_hash or '').strip().lower()
    if len(h) < 12 or not re.fullmatch(r'[0-9a-f]+', h) or not hmac.compare_digest(plan['plan_hash'][:len(h)], h):
        raise SystemExit('승인한 해시와 구성안이 다릅니다(12자 이상) — 요약을 다시 보고 --hash 로 승인하세요')
    keys = [sc['key'] for sc in plan.get('scopes', [])]
    if len(keys) != len(set(keys)):
        raise SystemExit('구성안에 같은 프로젝트 이름이 둘 이상 있어 적용하지 않았습니다 — sb vault plan 을 다시 돌리세요')


def apply_plan(plan: Dict[str, Any], approved_hash: str, approved_by: str = 'user_cli',
               _fail_after_write: bool = False) -> Dict[str, Any]:
    _check_hash(plan, approved_hash)
    targets = _targets(plan)
    configured = _real(Path(sb_scope.aliases_path()))
    if _real(targets['aliases']) != configured:
        # 볼트는 sb_scope 가 실제로 읽는 별칭 파일에만 쓴다. 다른 파일을 쓰려면 SB_PROJECT_ALIASES 로 바꾼 뒤 다시 plan.
        raise SystemExit('구성안의 별칭 파일(%s)이 지금 설정된 별칭 파일(%s)과 달라 적용하지 않았습니다 — '
                         'SB_PROJECT_ALIASES 를 확인하고 sb vault plan 을 다시 돌리세요'
                         % (_short(str(targets['aliases'])), _short(str(configured))))
    reals = [os.path.normcase(str(_real(p))) for p in targets.values()]
    if len(reals) != len(set(reals)):
        # 두 대상이 같은 실제 파일이면(예: SB_LOOPS_PATH 를 별칭 파일로 잘못 지정) 앞 쓰기가 뒤 쓰기의 해시 확인을
        # 깨뜨려 일부만 적용된다 — 쓰기 전에 멈춘다
        raise SystemExit('볼트 대상 파일 두 개가 같은 파일을 가리켜 적용하지 않았습니다 — SB_LOOPS_PATH·'
                         'SB_PROJECT_ALIASES·SB_HOME 설정을 확인하세요')
    blocking = [c for c in plan.get('conflicts', []) if c.get('blocking')]
    if blocking:
        raise SystemExit('구성안에 적용을 막는 충돌이 있습니다(%s) — 별칭 파일을 정리한 뒤 sb vault plan 을 다시 돌리세요'
                         % ', '.join(sorted({str(c.get('key')) for c in blocking})))
    with _Lock():
        # 0) 쓰기 전 검증 — 깨진 파일이 하나라도 있으면 아무것도 쓰지 않는다
        _load_obj(targets['vault_json'], 'vault.json')
        cur_aliases = _load_obj(targets['aliases'], '별칭 파일')
        cur_settings = _load_obj(targets['settings'], 'Claude 설정') if 'settings' in targets else None
        if cur_settings is not None and not isinstance(cur_settings.get('env', {}), dict):
            raise SystemExit('Claude 설정의 env 가 객체가 아니라 적용하지 않았습니다')
        aid = '%s-%s' % (stamp(), uuid.uuid4().hex[:8])
        bdir = vault_dir() / 'backup' / aid
        bdir.mkdir(parents=True, exist_ok=False)
        os.chmod(bdir, 0o700)
        apply_path = vault_dir() / ('apply-%s.json' % aid)
        record: Dict[str, Any] = {'version': SCHEMA, 'id': aid, 'plan_hash': plan['plan_hash'], 'approved_by': approved_by,
                                  'applied_at': now_iso(), 'backups': [], 'writes': [], 'rolled_back': False,
                                  'state': 'prepared'}
        for name, p in targets.items():
            real = _real(p)
            before = _sha(real)
            bpath = None
            if before is not None:
                bpath = bdir / (name + '.bak')
                shutil.copy2(real, bpath)
                os.chmod(bpath, 0o600)
            record['backups'].append({'name': name, 'target': str(real), 'backup_path': str(bpath) if bpath else None,
                                      'sha256_before': before})
        # 1) 쓰기 전에 기록을 먼저 남긴다(write-ahead) — 쓰는 도중 죽어도 되돌릴 근거가 있다
        _atomic_write(apply_path, json.dumps(record, ensure_ascii=False, indent=1), private=True)
        before_map = {b['target']: b['sha256_before'] for b in record['backups']}

        def guarded(name, data):
            # 쓰기 전에 '이 내용으로 바꾼다'를 기록에 남기고(의도 저널), 바꿔 끼우기 직전에 백업 때 해시와 다시 비교한다.
            # 중간에 죽어도 기록에 예정 해시가 있어, 지금 내용이 그 해시면 우리가 쓴 것으로 알고 되돌릴 수 있다.
            real = _real(targets[name])
            planned = hashlib.sha256(_bytes(data)).hexdigest()
            record['writes'].append({'name': name, 'target': str(real), 'sha256_after': planned})
            _atomic_write(apply_path, json.dumps(record, ensure_ascii=False, indent=1), private=True)
            _atomic_write(real, data, private=True, expect_sha=before_map[str(real)])

        try:
            for k, v in plan['aliases'].items():      # 별칭 병합 — 기존 키·경로는 건드리지 않는다
                cur_aliases.setdefault(k, v)
            guarded('aliases', json.dumps(cur_aliases, ensure_ascii=False, indent=1) + '\n')
            guarded('vault_json', json.dumps({
                'version': SCHEMA,
                'scopes': {sc['key']: {'root': sc['root'], 'state_write': sc['state_write'], 'rules': sc['rules'],
                                       'loops': sc['loops']} for sc in plan['scopes']},
                'kind_policy': plan['kind_policy'], 'state_write_mode': plan['state_write_mode'],
                'scope_excluded': plan['scope_excluded'], 'updated_at': now_iso(), 'plan_hash': plan['plan_hash'],
                'obs_seen': plan.get('obs_seen', 0),
            }, ensure_ascii=False, indent=1) + '\n')
            if before_map[str(_real(targets['loops']))] is None:   # 미결 원장 — 없을 때만 빈 파일
                guarded('loops', '')
            if cur_settings is not None:              # listed 모드이고 기존 값이 없을 때만(make_plan 이 충돌을 걸렀다)
                env = cur_settings.setdefault('env', {})
                if env.get(STATE_ENV) in (None, '', plan['state_env']):
                    env[STATE_ENV] = plan['state_env']
                guarded('settings', json.dumps(cur_settings, ensure_ascii=False, indent=2) + '\n')
            checks = verify_applied(plan)
            for w in record['writes']:   # 쓴 파일이 지금도 우리가 쓴 내용인가(적용 중 다른 도구가 고쳤는지)
                checks.append({'check': 'hash ' + w['name'], 'ok': _sha(Path(w['target'])) == w['sha256_after']})
            if _fail_after_write:
                checks.append({'check': 'forced_failure', 'ok': False})
            record['verify'] = {'ok': all(c['ok'] for c in checks), 'checks': checks}
        except Exception as e:  # noqa: BLE001
            record['verify'] = {'ok': False, 'checks': [{'check': 'changed_by_other' if isinstance(e, Changed) else 'exception',
                                                         'ok': False, 'error': str(e)[:200]}]}
        record['state'] = 'applied' if record['verify']['ok'] else 'failed'
        try:
            _atomic_write(apply_path, json.dumps(record, ensure_ascii=False, indent=1), private=True)
        except Exception as e:  # noqa: BLE001 — 기록 저장 실패도 되돌린다(P0-4)
            record['verify'] = {'ok': False, 'checks': [{'check': 'record_write', 'ok': False, 'error': str(e)[:200]}]}
        if not record['verify']['ok']:
            record = _restore(record, apply_path, only_ours=True)
    record['apply_path'] = str(apply_path)
    return record


def latest(prefix: str) -> Optional[Path]:
    files = sorted(vault_dir().glob(prefix + '-*.json'), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _restore(rec: Dict[str, Any], apply_path: Path, only_ours: bool) -> Dict[str, Any]:
    """기록의 백업으로 되돌린다. 대상마다 따로 시도해 끝까지 간다. only_ours 면 우리가 쓴 그대로인 파일만 되돌린다."""
    after = {w['target']: w['sha256_after'] for w in rec.get('writes', [])}
    restored = []
    for b in rec['backups']:
        target = Path(b['target'])
        try:
            cur = _sha(target)
            if cur == b['sha256_before']:
                restored.append({'target': b['target'], 'ok': True, 'note': 'unchanged'})
                continue
            if only_ours and (b['target'] not in after or cur != after[b['target']]):
                # 기록에 예정이 없거나, 예정한 내용이 아니다 — 우리가 쓴 것이 아니므로 건드리지 않는다
                restored.append({'target': b['target'], 'ok': False, 'note': 'changed_by_other'})
                continue
            if b['backup_path']:
                _atomic_write(target, Path(b['backup_path']).read_bytes(), expect_sha=cur)   # 원래 바이트 그대로
            elif target.exists():
                # 적용이 새로 만든 파일 — 지우지 않고 이 적용의 백업 폴더로 옮긴다. 기록이 틀렸어도(손으로 고친 기록 등)
                # 내용을 잃지 않는다. 한계: 해시 확인과 이동 사이의 짧은 틈은 _atomic_write 와 같다.
                if _sha(target) != cur:
                    raise Changed(str(target))
                keep = vault_dir() / 'backup' / str(rec.get('id') or 'unknown') / ('removed-%s-%s' % (b.get('name') or 'file', stamp()))
                keep.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(target), str(keep))
            restored.append({'target': b['target'], 'ok': _sha(target) == b['sha256_before']})
        except Exception as e:  # noqa: BLE001
            restored.append({'target': b['target'], 'ok': False, 'error': str(e)[:160]})
    ok = all(r['ok'] for r in restored)
    rec['rolled_back'] = ok                       # 일부라도 못 되돌렸으면 다시 시도할 수 있게 남긴다
    rec['state'] = 'rolled_back' if ok else 'rollback_partial'
    rec.setdefault('rollback_attempts', []).append({'at': now_iso(), 'restored': restored, 'ok': ok})
    rec['rollback'] = {'at': now_iso(), 'restored': restored, 'ok': ok}
    try:
        _atomic_write(apply_path, json.dumps(rec, ensure_ascii=False, indent=1), private=True)
    except Exception:  # noqa: BLE001
        pass
    return rec


_HEX64 = re.compile(r'[0-9a-f]{64}')


def _validate_record(rec: Any, apply_path: Path) -> Dict[str, Any]:
    """--apply 로 받은 기록이 이 볼트가 만든 것이고 앞뒤가 맞는지 확인한다. 하나라도 어긋나면 되돌리지 않고 멈춘다.
    - 이름과 대상이 고정 대응(vault_json·aliases·loops·settings → 지금 설정된 실제 경로)
    - 백업은 이 기록의 backup/<id>/ 아래에 있고, 내용 해시가 sha256_before 와 같다
    - 백업이 없으면 적용 전 파일도 없었다(sha256_before = None)
    - writes 항목은 backups 에 있는 대상이고 이름·해시 형식이 맞다"""
    bad = '적용 기록 형식이 아닙니다: %s' % apply_path
    if not isinstance(rec, dict) or not isinstance(rec.get('backups'), list) \
            or not isinstance(rec.get('writes', []), list) or not isinstance(rec.get('id'), str) \
            or not re.fullmatch(r'[0-9A-Za-z_-]+', rec['id']):
        raise SystemExit(bad)
    if Path(os.path.realpath(apply_path)).parent != Path(os.path.realpath(vault_dir())):
        raise SystemExit('이 볼트의 적용 기록이 아닙니다: %s' % apply_path)
    fixed = {'vault_json': vault_json_path(), 'aliases': Path(sb_scope.aliases_path()), 'loops': loops_path(),
             'settings': claude_settings_path()}
    fixed = {k: str(_real(v)) for k, v in fixed.items()}
    bdir = os.path.realpath(vault_dir() / 'backup' / rec['id'])
    seen: Dict[str, Dict[str, Any]] = {}
    for b in rec['backups']:
        if not isinstance(b, dict) or not isinstance(b.get('name'), str) or not isinstance(b.get('target'), str) \
                or not isinstance(b.get('backup_path'), (str, type(None))) \
                or not isinstance(b.get('sha256_before'), (str, type(None))):
            raise SystemExit(bad)
        if fixed.get(b['name']) != b['target']:
            raise SystemExit('적용 기록에 볼트 밖 대상이 있어 되돌리지 않았습니다: %s' % b['target'])
        if b['target'] in seen or (b['backup_path'] is None) != (b['sha256_before'] is None):
            raise SystemExit(bad)
        if b['sha256_before'] is not None:
            if not _HEX64.fullmatch(b['sha256_before']):
                raise SystemExit(bad)
            if not os.path.realpath(b['backup_path']).startswith(bdir + os.sep):
                raise SystemExit('적용 기록의 백업 경로가 이 적용의 백업 폴더 밖입니다: %s' % b['backup_path'])
            if _sha(Path(b['backup_path'])) != b['sha256_before']:
                raise SystemExit('백업 파일이 기록과 달라 되돌리지 않았습니다: %s' % _short(b['backup_path']))
        seen[b['target']] = b
    for w in rec.get('writes', []):
        if not isinstance(w, dict) or not isinstance(w.get('target'), str) or w['target'] not in seen \
                or not isinstance(w.get('sha256_after'), str) or not _HEX64.fullmatch(w['sha256_after']) \
                or w.get('name') != seen[w['target']]['name']:
            raise SystemExit(bad)
    return rec


def rollback(apply_path: Optional[Path] = None, force: bool = False) -> Dict[str, Any]:
    apply_path = Path(apply_path) if apply_path else latest('apply')
    if not apply_path or not apply_path.exists():
        raise SystemExit('되돌릴 적용 기록이 없습니다')
    with _Lock():
        rec = _validate_record(read_json_file(apply_path), apply_path)
        if rec.get('rolled_back'):
            raise SystemExit('이미 되돌린 적용입니다: %s' % apply_path)
        after = {w['target']: w['sha256_after'] for w in rec.get('writes', [])}
        before = {bk['target']: bk['sha256_before'] for bk in rec['backups']}
        # 예정만 기록되고 쓰기 전에 멈춘 대상(지금도 적용 전 내용)은 '바뀐 파일'이 아니다
        changed = [t for t, h in after.items() if _sha(Path(t)) not in (h, before.get(t))]
        if changed and not force:   # 적용 뒤 사용자·다른 도구가 고친 파일을 되돌리기로 덮지 않는다
            raise SystemExit('적용 뒤 바뀐 파일이 있어 되돌리지 않았습니다(--force 로 강제): ' + ', '.join(_short(c) for c in changed))
        return _restore(rec, apply_path, only_ours=not force)


def status() -> Dict[str, Any]:
    v = read_json_file(vault_json_path())
    last = latest('apply')
    rec = read_json_file(last) if last else None
    return {'vault_json': str(vault_json_path()), 'configured': isinstance(v, dict) and not v.get('default'),
            'default': bool(isinstance(v, dict) and v.get('default')),
            'scopes': len((v or {}).get('scopes', {})) if isinstance(v, dict) else 0,
            'last_apply': str(last) if last else None,
            'last_apply_ok': (rec or {}).get('verify', {}).get('ok'), 'rolled_back': (rec or {}).get('rolled_back'),
            'last_analysis': str(latest('analysis') or '') or None, 'last_plan': str(latest('plan') or '') or None}


# ---------------------------------------------------------------- 기본 볼트·재분석 알림

DEFAULT_SCOPE_EXCLUDED = ['~', '/tmp', '/private/tmp', '/private/var/folders']
NUDGE_GROWTH = 1.5          # 마지막 구성 때보다 관측이 이만큼 늘면 다시 분석하라고 알린다
NUDGE_MIN_NEW = 2000        # 그리고 최소 이만큼은 늘어야 한다(작은 볼트가 매일 알림을 받지 않게)


def _max_obs_id(db) -> int:
    """관측 id 의 최댓값(정수 기본키라 인덱스로 바로 읽힌다). 관측 수의 근사값으로 쓴다."""
    try:
        return int(db.execute('SELECT MAX(id) FROM observations').fetchone()[0] or 0)
    except (sqlite3.Error, ValueError, TypeError):
        return 0


def init_default() -> Dict[str, Any]:
    """기록이 없거나 적을 때의 기본 볼트. 이미 vault.json 이 있으면 아무것도 바꾸지 않는다."""
    vj = vault_json_path()
    created = []
    if not vj.exists():
        _atomic_write(vj, json.dumps({
            'version': SCHEMA, 'scopes': {}, 'kind_policy': {'default': 'normal'}, 'state_write_mode': 'all',
            'scope_excluded': DEFAULT_SCOPE_EXCLUDED, 'updated_at': now_iso(), 'plan_hash': None, 'obs_seen': 0,
            'default': True}, ensure_ascii=False, indent=1) + '\n', private=True)
        created.append(str(vj))
    if not loops_path().exists():
        _atomic_write(loops_path(), '', private=True)
        created.append(str(loops_path()))
    vault_dir().mkdir(parents=True, exist_ok=True)
    return {'created': created, 'vault_json': str(vj)}


def _nudge_state() -> Path:
    return vault_dir() / 'nudge.json'


def nudge_line(today: Optional[str] = None, db_path: Optional[str] = None) -> str:
    """세션 시작 알림 한 줄(하루 1회 이하). 볼트가 기본 상태인데 기록이 쌓였거나, 구성 뒤 기록이 크게 늘었을 때만."""
    if os.environ.get('SB_VAULT_NUDGE', '1') == '0':
        return ''
    today = today or time.strftime('%Y-%m-%d')
    st = read_json_file(_nudge_state()) or {}
    if st.get('last_shown') == today:
        return ''
    db_path = db_path or sb_config.claude_mem_db()
    if not Path(db_path).is_file():
        return ''
    try:   # 세션 시작 훅에서 돈다 — DB 가 잠겨 있으면 기다리지 않고 건너뛴다
        with closing(sqlite3.connect('file:%s?mode=ro' % Path(db_path).as_posix(), uri=True, timeout=0.2)) as db:
            now_obs = _max_obs_id(db)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return ''
    v = read_json_file(vault_json_path())
    msg = ''
    if not isinstance(v, dict) or v.get('default') or not v.get('scopes'):
        if now_obs >= ENOUGH_OBS:
            msg = '볼트가 아직 기본 상태다 — 기록 %d건이 쌓였으니 `sb vault analyze` → `sb vault plan`으로 프로젝트·레이어 구성안을 만들어 사용자에게 보여 줄 수 있다' % now_obs
    else:
        try:
            seen = int(v.get('obs_seen') or 0)
        except (TypeError, ValueError):
            seen = 0
        if seen and now_obs >= seen * NUDGE_GROWTH and now_obs - seen >= NUDGE_MIN_NEW:
            msg = '볼트 구성 뒤 기록이 %d건 → %d건으로 늘었다 — `sb vault analyze`·`plan`으로 새 프로젝트를 확인할 수 있다' % (seen, now_obs)
    if msg:
        try:
            _atomic_write(_nudge_state(), json.dumps({'last_shown': today, 'obs': now_obs}), private=True)
        except OSError:
            pass
    return msg


# ---------------------------------------------------------------- CLI

def _save(prefix: str, data: Dict[str, Any]) -> Path:
    p = vault_dir() / ('%s-%s-%s.json' % (prefix, stamp(), uuid.uuid4().hex[:6]))
    _atomic_write(p, json.dumps(data, ensure_ascii=False, indent=1), private=True)   # 경로·꼬리표가 들어 있어 본인만 읽게
    try:
        os.chmod(vault_dir(), 0o700)
    except OSError:
        pass
    return p


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(prog='sb vault', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('analyze')
    a.add_argument('--json', action='store_true')
    a.add_argument('--db')
    a.add_argument('--projects-root')
    a.add_argument('--no-git', action='store_true')
    p = sub.add_parser('plan')
    p.add_argument('--analysis')
    p.add_argument('--state-write', choices=('all', 'listed'), default='all',
                   help='all: 상태층 쓰기 허용을 바꾸지 않음(전 scope 허용, 기본) / listed: 최근 활동 scope 만 허용')
    p.add_argument('--json', action='store_true')
    x = sub.add_parser('apply')
    x.add_argument('--plan')
    x.add_argument('--yes', action='store_true', help='묻지 않고 적용(에이전트용 — --hash 필수)')
    x.add_argument('--hash', default='')
    r = sub.add_parser('rollback')
    r.add_argument('--apply')
    r.add_argument('--force', action='store_true', help='적용 뒤 바뀐 파일이 있어도 되돌린다')
    s = sub.add_parser('status')
    s.add_argument('--json', action='store_true')
    sub.add_parser('init', help='기본 볼트(vault.json·빈 미결 원장)를 만든다. 이미 있으면 그대로 둔다')
    args = ap.parse_args(argv)

    if args.cmd == 'analyze':
        res = analyze(args.db, args.projects_root, use_git=not args.no_git)
        path = _save('analysis', res)
        if args.json:
            print(json.dumps(dict(res, path=str(path)), ensure_ascii=False, indent=1))
        else:
            print('분석 저장: %s' % path)
            print('데이터 양: %s (관측 %d, 세션 %d, 작업 폴더 %d, 제외 폴더 %d)' % (
                res['data_volume'], res['sources'][0]['count'], res['sources'][0]['sessions'], res['work_folders'],
                res['excluded_count']))
            print('다음: sb vault plan')
        return 0
    if args.cmd == 'plan':
        src = Path(args.analysis) if args.analysis else latest('analysis')
        if not src:
            print('분석 결과가 없습니다 — sb vault analyze 먼저', file=sys.stderr)
            return 2
        plan = make_plan(read_json_file(src), str(src), args.state_write)
        path = _save('plan', plan)
        if args.json:
            print(json.dumps(dict(plan, path=str(path)), ensure_ascii=False, indent=1))
        else:
            print(render_plan(plan))
            print('\n저장: %s\n적용: sb vault apply --plan %s --yes --hash %s' % (path, path, plan['plan_hash'][:12]))
        return 0
    if args.cmd == 'apply':
        src = Path(args.plan) if args.plan else latest('plan')
        if not src:
            print('구성안이 없습니다 — sb vault plan 먼저', file=sys.stderr)
            return 2
        plan = read_json_file(src)
        approved = args.hash
        if not args.yes:
            print(render_plan(plan))
            try:
                ans = input('\n이 구성안을 적용할까요? 적용하려면 yes: ').strip().lower()
            except EOFError:
                ans = ''
            if ans != 'yes':
                print('적용하지 않았습니다')
                return 1
            approved = plan['plan_hash']
        rec = apply_plan(plan, approved, 'user_cli' if not args.yes else 'agent_user_utterance')
        ok = rec['verify']['ok']
        print(('적용 완료' if ok else '검증 실패 — 되돌렸습니다') + ': ' + rec['apply_path'])
        if ok:
            print('되돌리기: sb vault rollback --apply %s' % rec['apply_path'])
        return 0 if ok else 1
    if args.cmd == 'rollback':
        rec = rollback(Path(args.apply) if args.apply else None, force=args.force)
        print('되돌림 %s: %s' % ('완료' if rec['rollback']['ok'] else '일부 실패', ', '.join(
            '%s=%s' % (_short(r['target']), 'ok' if r['ok'] else 'FAIL') for r in rec['rollback']['restored'])))
        return 0 if rec['rollback']['ok'] else 1
    if args.cmd == 'init':
        r = init_default()
        print('기본 볼트: ' + (', '.join(_short(c) for c in r['created']) + ' 생성' if r['created'] else '이미 있음 — 그대로 둠'))
        return 0
    if args.cmd == 'status':
        st = status()
        print(json.dumps(st, ensure_ascii=False, indent=1) if args.json else
              '볼트: %s (scope %d개) / 마지막 적용: %s' % ('구성됨' if st['configured'] else '기본(default)' if st['default'] else '없음', st['scopes'],
                                                  st['last_apply'] or '없음'))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
