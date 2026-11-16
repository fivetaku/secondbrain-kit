# Windows Chroma 잠금 충돌과 claude-mem 15.x 확인 범위

doctor의 `Chroma 잠금 ⚠`나 claude-mem 로그의 `already owned by PID … refusing to start a second writer`, `chroma-mcp connection in backoff`가 많이 보일 때 읽는다. 원인은 아직 확정하지 못했다. 아래는 claude-mem 15.18.3 코드를 읽고 세운 가설과, 그 PC에서 원인을 가리는 읽기 전용 명령이다.

## 무슨 일이 일어나나

- claude-mem 워커는 Chroma 폴더 안에 쓰기 잠금 파일 `.claude-mem-chroma-writer.lock`을 둔다(13.34 이후, 15.18.3도 같다). 잠금 주인이 살아 있다고 보이면 다른 워커는 Chroma에 연결하지 않고, 10초 동안 다시 시도하지 않는다(backoff).
- 그동안 새 관측은 SQLite에는 저장되지만 벡터 색인에는 들어가지 않고 밀린다. 키워드 검색은 그대로 되고, 의미 검색 결과만 늦게 반영된다.
- claude-mem은 주인이 살아 있는지 PowerShell로 프로세스 이름과 시작 시각을 조회한다(각 5초 제한). 조회가 실패하거나 시간을 넘기면 "살아 있다"로 본다.

## 가설 (가능성 높은 순)

| 순위 | 가설 | 판별 |
|---|---|---|
| 1 | 워커가 자주 다시 뜨면서 이전 워커나 chroma-mcp가 잠금을 쥔 채 남는다. Windows에서는 claude-mem의 고아 chroma-mcp 정리가 꺼져 있고, 정상 종료에 최대 20초가 걸린다 | 아래 명령 3번에서 워커 프로세스가 2개 이상이거나 부모가 없는 chroma-mcp가 있다. 5·6번에서 잠금 충돌이 하루 종일 퍼져 있고 대부분 워커 시작 30초 안이다 |
| 2 | PowerShell 조회가 느려 5초를 넘기면, 이미 죽은 주인도 "살아 있다"로 판정되고 잠금이 지워지지 않는다. PID가 재사용돼도 같다 | 1번에서 잠금 주인 PID의 프로세스 이름이 bun/node가 아니거나 잠금보다 늦게 시작했다. 4번 조회가 3~5초 이상 걸린다 |
| 3 | 키트 야간 개인정보 정리(`sb pii-sweep --chroma`)가 워커를 강제 종료하고 2초 뒤 원본 Chroma를 열어, 새로 뜬 워커와 같은 폴더를 함께 썼다 | 잠금 충돌이 야간 작업 시각(05시대)에 몰린다. 이 키트 판에서 고쳤다(아래) |
| 4 | Codex용 claude-mem 플러그인과 Claude용 플러그인의 판이 달라, 훅마다 서로의 워커를 옛 판으로 보고 죽인다 | 3번에서 Codex 쪽 경로에서 뜬 워커가 섞여 있고, 8번의 두 판 버전이 다르다 |
| 5 | 키트 회수 서버의 벡터 스냅샷 복사 | 원인이 아닌 것으로 본다. 키트는 원본을 복사본으로만 열고 잠금 파일을 만들지 않는다. 잠금 주인이 python 프로세스로 나오면 다시 본다 |

## 이 키트 판에서 바뀐 것

- `sb pii-sweep --chroma`는 워커를 멈춘 뒤 claude-mem 잠금이 풀릴 때까지 최대 30초(`SB_PII_CHROMA_WAIT`) 기다린다. 원본을 여는 동안에는 키트가 같은 잠금을 쥔다. 계속 잡혀 있으면 벡터 정리만 건너뛰고 SQLite 마스킹은 그대로 한다.
- doctor에 `Chroma 잠금` 점검이 생겼다. 최근 7일 워커 로그에서 잠금 충돌·backoff 줄(ERROR·WARN 줄만)을 세고, 잠금 파일 주인 PID가 살아 있는지 본다. 잠금 파일에서는 pid만 읽는다.

## 지금 할 수 있는 조치

1. Claude·Codex 세션을 모두 닫는다.
2. 남은 chroma-mcp·uvx 프로세스를 확인해 끈다: `tasklist | findstr /i "uvx chroma"`
3. `sb health --fix`나 새 세션으로 워커를 다시 띄운다.
4. 다시 생기면 아래 판별 명령 결과를 키트 이슈에 붙인다.

## 판별 명령 (PowerShell에 그대로 붙여 넣기)

읽기 전용이다. 파일을 쓰거나 프로세스를 멈추지 않는다. 토큰·키 값, 기록 본문, 로그 본문은 출력하지 않고 개수·시각·프로세스 이름만 낸다. 명령줄은 홈 경로를 `<home>`으로 가리고 앞 60자만 보여 준다.

```powershell
$ErrorActionPreference = 'SilentlyContinue'
$cm   = Join-Path $HOME '.claude-mem'
$lock = Join-Path $cm 'chroma\.claude-mem-chroma-writer.lock'
"== 1. 쓰기 잠금 파일"
if (Test-Path $lock) {
  $l = Get-Content $lock -Raw | ConvertFrom-Json
  $p = Get-Process -Id $l.pid
  [pscustomobject]@{ lockPid=$l.pid; acquiredAt=$l.acquiredAt; hasStartToken=[bool]$l.startToken; processName=$l.processName;
                     pidAlive=[bool]$p; liveName=$p.ProcessName; liveStart=$p.StartTime;
                     lockAgeMin=[int]((Get-Date) - (Get-Item $lock).LastWriteTime).TotalMinutes } | Format-List
} else { "잠금 파일 없음" }
"== 2. worker.pid 와 health"
if (Test-Path (Join-Path $cm 'worker.pid')) {
  $w = Get-Content (Join-Path $cm 'worker.pid') -Raw | ConvertFrom-Json
  $wp = Get-Process -Id $w.pid
  [pscustomobject]@{ workerPid=$w.pid; port=$w.port; startedAt=$w.startedAt; alive=[bool]$wp; name=$wp.ProcessName; sameAsLock=($l -and $l.pid -eq $w.pid) } | Format-List
  try { $h = Invoke-RestMethod -TimeoutSec 3 ("http://127.0.0.1:{0}/api/health" -f $w.port); "health pid=$($h.pid) version=$($h.version) uptime=$($h.uptime)" } catch { "health 응답 없음" }
} else { "worker.pid 없음" }
"== 3. 워커·chroma-mcp 프로세스(명령줄은 앞 60자, 홈 경로 가림)"
$all = Get-CimInstance Win32_Process
$ids = @{}; $all | ForEach-Object { $ids[[int]$_.ProcessId] = $true }
$mask = { param($s) if ($s) { ($s -replace [regex]::Escape($HOME), '<home>').Substring(0, [Math]::Min(60, ($s -replace [regex]::Escape($HOME), '<home>').Length)) } }
$workers = $all | Where-Object { $_.CommandLine -match 'worker-service' }
$chroma  = $all | Where-Object { $_.CommandLine -match 'chroma[-_]mcp' }
"worker-service 프로세스 수: $($workers.Count)"
$workers | ForEach-Object { [pscustomobject]@{ pid=$_.ProcessId; ppid=$_.ParentProcessId; name=$_.Name; created=$_.CreationDate; fromCodexMarket=($_.CommandLine -match 'claude-mem-local|\.codex'); cmd=(& $mask $_.CommandLine) } } | Format-Table -AutoSize
"chroma-mcp 관련 프로세스 수: $($chroma.Count)"
$chroma | ForEach-Object { [pscustomobject]@{ pid=$_.ProcessId; ppid=$_.ParentProcessId; parentAlive=$ids.ContainsKey([int]$_.ParentProcessId); name=$_.Name; created=$_.CreationDate; sameDataDir=($_.CommandLine -match [regex]::Escape(($cm -replace '\\','/') + '/chroma')) } } | Format-Table -AutoSize
"== 4. claude-mem 이 쓰는 PowerShell 조회가 얼마나 걸리나(5초 제한)"
$t1 = Measure-Command { (Get-Process -Id $PID -ErrorAction Stop).ProcessName | Out-Null }
$t2 = Measure-Command { (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").CreationDate | Out-Null }
$t3 = Measure-Command { powershell.exe -NoProfile -NonInteractive -Command "(Get-CimInstance Win32_Process -Filter 'ProcessId=$PID').CreationDate.ToString('yyyyMMddHHmmss.ffffff')" | Out-Null }
"Get-Process {0:N2}s / Get-CimInstance {1:N2}s / 새 powershell.exe 로 CIM {2:N2}s" -f $t1.TotalSeconds, $t2.TotalSeconds, $t3.TotalSeconds
"== 5. 최근 7일 로그: 문구별 건수와 시각대 분포(본문 출력 없음)"
$logs = Get-ChildItem (Join-Path $cm 'logs') -Filter 'claude-mem-*.log' | Where-Object LastWriteTime -gt (Get-Date).AddDays(-7)
$pat = [ordered]@{ owned='already owned by PID'; backoff='connection in backoff'; stale='Removed stale Chroma writer lock'; started='\] Worker started'; discard='Discarding stale memory_session_id';
                   restart='Restarting worker|Restart successor'; mismatch='version mismatch'; orphanKill='Killing orphaned chroma-mcp'; idle='idle shutdown'; graceDeadline='Graceful shutdown deadline' }
foreach ($k in $pat.Keys) {
  $hits = $logs | Select-String -Pattern $pat[$k]
  $hours = $hits | ForEach-Object { if ($_.Line -match '^\[\d{4}-\d\d-\d\d (\d\d):') { $Matches[1] } } | Group-Object | Sort-Object Name | ForEach-Object { "$($_.Name)h:$($_.Count)" }
  "{0,-14} {1,5}  {2}" -f $k, $hits.Count, ($hours -join ' ')
}
"== 6. owned 직전 30초 안에 Worker started 가 있었던 비율"
$ev = $logs | Select-String -Pattern 'already owned by PID','\] Worker started' | ForEach-Object {
  if ($_.Line -match '^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)') { [pscustomobject]@{ t=[datetime]$Matches[1]; owned=($_.Line -match 'already owned') } } } | Sort-Object t
$lastStart = $null; $near = 0; $tot = 0
foreach ($e in $ev) { if ($e.owned) { $tot++; if ($lastStart -and ($e.t - $lastStart).TotalSeconds -le 30) { $near++ } } else { $lastStart = $e.t } }
"owned $tot 건 중 워커 시작 30초 안: $near 건"
"== 7. 키트 야간 pii 단계"
$sb = if ($env:SB_HOME) { $env:SB_HOME } else { Join-Path $HOME '.secondbrain' }
"pii 마스킹 모듈 파일 있음: $(Test-Path (Join-Path $sb 'local\pii_mask.py'))  SB_PII_MASK 설정: $([bool]$env:SB_PII_MASK)"
Get-ChildItem (Join-Path $cm 'backups') -Directory -Filter 'chroma-before-piisweep-*' | Sort-Object Name | Select-Object -Last 7 Name
if (Test-Path (Join-Path $sb 'logs\nightly.log')) { Select-String -Path (Join-Path $sb 'logs\nightly.log') -Pattern '^stage=pii ' | Select-Object -Last 7 | ForEach-Object Line }
Get-ScheduledTask -TaskPath '\secondbrain-kit\' | Select-Object TaskName, State
"== 8. 설치된 claude-mem 판(Claude 캐시·Codex 로컬 마켓)"
Get-ChildItem (Join-Path $HOME '.claude\plugins\cache\thedotmack\claude-mem') -Directory | Select-Object Name, LastWriteTime
Get-ChildItem (Join-Path $HOME '.codex') -Recurse -Filter package.json -Depth 6 | Where-Object { $_.FullName -match 'claude-mem' } |
  ForEach-Object { "{0}  version={1}" -f ($_.FullName -replace [regex]::Escape($HOME), '~'), (Get-Content $_.FullName -Raw | ConvertFrom-Json).version }
```

판정 읽는 법:
- 1번에서 `pidAlive=True`인데 `liveName`이 bun/node가 아니거나 `liveStart`가 `acquiredAt`보다 늦으면 PID가 재사용된 것이다(가설 2).
- 3번에서 `worker-service` 프로세스가 2개 이상이거나 `parentAlive=False`인 chroma-mcp가 있으면 가설 1이다. `fromCodexMarket=True`가 섞여 있으면 가설 4다.
- 4번에서 새 `powershell.exe` CIM 조회가 3~5초 이상 걸리면 가설 2가 강해진다.
- 5·6번에서 owned가 하루 종일 퍼져 있고 대부분 워커 시작 30초 안이면 가설 1, 05시대에 몰려 있으면 가설 3이다.

## claude-mem 15.x 확인 범위

| 범위 | 판 | 뜻 |
|---|---|---|
| 캡처까지 확인 | 13.24.23 ~ 13.34.2 | 설치·캡처·관측기·벡터 동기화·키트 읽기 경로를 함께 돌려 봤다 |
| 읽기 경로만 확인 | 15.18.3 | 15.18.3의 마이그레이션으로 만든 빈 DB에서 키트가 claude-mem DB를 읽는 25곳을 돌려 모두 통과했다. 재시작 명령과 워커 인자도 맞다. 캡처·관측기·벡터 동기화는 확인하지 않았다 |

알려진 차이: 15.x는 원격 동기화 장부(`sync_rev`)를 둔다. 키트의 `sb relabel`·`sb pii-sweep`은 기록을 고칠 때 이 값을 올리지 않으므로, claude-mem 원격 동기화를 켠 PC에서는 키트가 바꾼 project·마스킹이 다른 기기로 퍼지지 않거나 원격 내용으로 되덮일 수 있다. 원격 동기화를 쓰지 않으면 영향이 없다(doctor `claude-mem 원격 동기화` 점검이 켜져 있는지 알려 준다).
