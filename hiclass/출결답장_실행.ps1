# 출결 자동 답장 — 정해 둔 시각에 클로드가 혼자 하이클래스를 보고 출결 연락을 처리한다 (윈도우)
#   한 번 돌리기:        powershell -NoProfile -ExecutionPolicy Bypass -File .\출결답장_실행.ps1
#   예약 걸기(평일):     powershell -NoProfile -ExecutionPolicy Bypass -File .\출결답장_실행.ps1 -Register -At "07:50"
#                        시각 여러 개:  -At "07:50,12:30"
#   예약 끄기:           powershell -NoProfile -ExecutionPolicy Bypass -File .\출결답장_실행.ps1 -Unregister
# 세션을 열어 둘 필요 없다. 컴퓨터가 켜져 있고 윈도우에 로그인돼 있으면 된다.
param([switch]$Register, [switch]$Unregister, [string]$At = "07:50")
$ErrorActionPreference = 'Continue'
$here = $PSScriptRoot
$task = '출결 자동 답장'

if ($Unregister) {
  Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
  "예약을 껐습니다: $task"; exit 0
}
if ($Register) {
  $me = Join-Path $here '출결답장_실행.ps1'
  # conhost --headless: 까만 창이 안 뜬다. powershell -WindowStyle Hidden 은 예약 실행에서 바로 죽은 적이 있다
  $act = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$me`""
  $days = 'Monday','Tuesday','Wednesday','Thursday','Friday'
  $trg = @($At.Split(',') | ForEach-Object { New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $_.Trim() })
  # 놓치면(컴퓨터가 꺼져 있었으면) 켜질 때 한 번 돈다
  $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
  Register-ScheduledTask -TaskName $task -Action $act -Trigger $trg -Settings $set -Force | Out-Null
  "예약했습니다: 평일 $At — 작업 스케줄러 «$task»"; exit 0
}

# 클로드 위치 — 설치 방법마다 다르다
$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
foreach ($c in @("$env:USERPROFILE\.local\bin\claude.exe", "$env:APPDATA\npm\claude.cmd")) {
  if (-not $claude -and (Test-Path $c)) { $claude = $c }
}
$log = Join-Path $here '출결답장_실행로그.txt'
if (-not $claude) { Add-Content $log -Encoding UTF8 "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm') 클로드를 못 찾음"; exit 1 }

# 지시문은 파이프로 넘긴다 (인자로 넘기면 예약 실행에서 입력을 기다리며 멈춘다). 한글이 ? 로 깨지지 않게 UTF-8
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$prompt = Get-Content -Raw -Encoding UTF8 (Join-Path $here '출결답장_지시문.md')
Set-Location $here
$env:PYTHONUTF8 = '1'
# 허락한 것만 한다 — 하이클래스 프로그램 · 출결 캘린더 · 이 폴더 파일 읽고 쓰기. 나머지는 막힌다
$allow = 'Read,Edit,Write,Bash(python -X utf8 talk.py:*),Bash(python talk.py:*),mcp__google__add_attendance,mcp__google__list_attendance,mcp__google__make_attendance_calendars,mcp__google__check_attendance_calendars'
Add-Content $log -Encoding UTF8 "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 시작"
$prompt | & $claude -p --allowedTools $allow 2>&1 | ForEach-Object { "$_" } |
  Where-Object { $_ -notmatch 'NativeCommandError|^At line|^\+ |CategoryInfo' } |
  ForEach-Object { Add-Content $log -Encoding UTF8 $_ }
Add-Content $log -Encoding UTF8 "=== 끝 $(Get-Date -Format 'HH:mm:ss') (exit $LASTEXITCODE)"
