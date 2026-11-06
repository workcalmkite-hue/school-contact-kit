#!/bin/bash
# 출결 자동 답장 — 정해 둔 시각에 클로드가 혼자 하이클래스를 보고 출결 연락을 처리한다 (맥)
#   한 번 돌리기:     bash 출결답장_실행.sh
#   예약 걸기(평일):  bash 출결답장_실행.sh --register 07:50        (여러 개: --register 07:50,12:30)
#   예약 끄기:        bash 출결답장_실행.sh --unregister
# 이 폴더는 홈 폴더(~/하이클래스)에 둔다. 바탕화면·문서 폴더 안이면 예약 실행이 맥 보안에 막힌다.
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="kr.anssaem.attendance-reply"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "$1" = "--unregister" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null; rm -f "$PLIST"; echo "예약을 껐습니다"; exit 0
fi
if [ "$1" = "--register" ]; then
  AT="${2:-07:50}"
  case "$HERE" in "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*)
    echo "이 폴더가 바탕화면·문서·다운로드 안에 있어 예약 실행이 막힙니다. 홈 폴더(~/하이클래스)로 옮긴 뒤 다시 하세요."; exit 1;; esac
  CAL=""
  for t in ${AT//,/ }; do
    h=$((10#${t%%:*})); m=$((10#${t##*:}))
    for d in 1 2 3 4 5; do CAL="$CAL<dict><key>Weekday</key><integer>$d</integer><key>Hour</key><integer>$h</integer><key>Minute</key><integer>$m</integer></dict>"; done
  done
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>ProgramArguments</key><array><string>/bin/bash</string><string>$HERE/출결답장_실행.sh</string></array>
<key>StartCalendarInterval</key><array>$CAL</array>
</dict></plist>
PL
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null
  launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "예약했습니다: 평일 $AT (맥이 잠자기 중이면 깨어날 때 돕니다)"
  exit 0
fi

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
LOG="$HERE/출결답장_실행로그.txt"
CLAUDE="$(command -v claude)"
[ -z "$CLAUDE" ] && { echo "=== $(date '+%F %R') 클로드를 못 찾음" >> "$LOG"; exit 1; }
cd "$HERE"
ALLOW='Read,Edit,Write,Bash(python3 talk.py:*),mcp__google__add_attendance,mcp__google__list_attendance,mcp__google__make_attendance_calendars,mcp__google__check_attendance_calendars'
echo "=== $(date '+%F %T') 시작" >> "$LOG"
"$CLAUDE" -p --allowedTools "$ALLOW" < "$HERE/출결답장_지시문.md" >> "$LOG" 2>&1
echo "=== 끝 $(date '+%T') (exit $?)" >> "$LOG"
