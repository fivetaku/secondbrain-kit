#!/usr/bin/env python3
"""Codex 훅 신뢰(trust) 등록·끄기 — TUI 의 /hooks 가 하는 일을 app-server JSON-RPC 로 재현한다.

Codex 는 사용자·플러그인 훅을 해시로 신뢰 등록해야 실행한다. 명령이 바뀌면 다시 신뢰해야 한다.

  codex_trust.py list                      훅 목록(키·상태)
  codex_trust.py trust --match <문자열>     명령에 문자열이 든 훅 중 미신뢰·변경된 것을 신뢰
  codex_trust.py disable --plugin <이름>    pluginId 에 이름이 든 플러그인 훅을 끔(enabled=false)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time


class AppServer:
    def __init__(self, codex: str):
        self.p = subprocess.Popen([codex, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding='utf-8')
        self.n = 0
        self.call('initialize', {'clientInfo': {'name': 'secondbrain-kit', 'version': '1'}})
        self.notify('initialized', {})

    def notify(self, method, params):
        self.p.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': method, 'params': params}) + '\n')
        self.p.stdin.flush()

    def call(self, method, params, timeout=30):
        self.n += 1
        rid = self.n
        self.p.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params}) + '\n')
        self.p.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get('id') == rid:
                if 'error' in msg:
                    raise RuntimeError('%s: %s' % (method, msg['error']))
                return msg.get('result')
        raise RuntimeError('%s: no response' % method)

    def close(self):
        try:
            self.p.kill()
        except OSError:
            pass


def hooks(srv: AppServer, cwd: str):
    res = srv.call('hooks/list', {'cwds': [cwd]})
    out = []
    for entry in res.get('data', []):
        out += entry.get('hooks', [])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=('list', 'trust', 'disable'))
    ap.add_argument('--match', help='trust: 명령 문자열에 포함될 부분')
    ap.add_argument('--plugin', help='disable: pluginId 에 포함될 부분')
    ap.add_argument('--cwd', default=os.path.expanduser('~'))
    ap.add_argument('--codex', default=shutil.which('codex') or 'codex')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--json', action='store_true', help='list: JSON 으로 출력')
    a = ap.parse_args()
    srv = AppServer(a.codex)
    try:
        hs = hooks(srv, a.cwd)
        if a.action == 'list':
            if a.json:
                print(json.dumps([{k: h.get(k) for k in ('key', 'trustStatus', 'enabled', 'command', 'pluginId')}
                                  for h in hs], ensure_ascii=False))
                return 0
            for h in hs:
                print('%-10s %-8s %s | %s' % (h.get('trustStatus'), 'on' if h.get('enabled') else 'off',
                                            h['key'], (h.get('command') or '')[:80]))
            return 0
        state = {}
        if a.action == 'trust':
            if not a.match:
                ap.error('--match 필요')
            for h in hs:
                if a.match in (h.get('command') or '') and h.get('trustStatus') != 'trusted':
                    state[h['key']] = {'trusted_hash': h['currentHash']}
        else:
            if not a.plugin:
                ap.error('--plugin 필요')
            for h in hs:
                if a.plugin in (h.get('pluginId') or '') and h.get('enabled'):
                    state[h['key']] = {'enabled': False}
        print(json.dumps({'action': a.action, 'keys': sorted(state)}, ensure_ascii=False, indent=1))
        if state and not a.dry_run:
            srv.call('config/batchWrite', {'edits': [{'keyPath': 'hooks.state', 'value': state,
                                                      'mergeStrategy': 'upsert'}]})
            after = {h['key']: h for h in hooks(srv, a.cwd)}
            bad = [k for k in state if (a.action == 'trust' and after.get(k, {}).get('trustStatus') != 'trusted')
                   or (a.action == 'disable' and after.get(k, {}).get('enabled'))]
            if bad:
                print('적용 실패: ' + ', '.join(bad), file=sys.stderr)
                return 1
            print('적용 %d건 확인' % len(state))
        return 0
    finally:
        srv.close()


if __name__ == '__main__':
    sys.exit(main())
