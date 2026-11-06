# 카카오톡 PC(윈도우) 자동화 — 파일 전송 / 메시지 / 클릭 / 화면 캡처
#
# 카카오톡은 EVA_* 커스텀 렌더링이라 UIA로 내부 컨트롤이 안 잡힌다.
# 그래서 "창을 찾고 → 좌표로 누르고 → 화면 픽셀로 확인"하는 방식으로 간다.
# 유일한 예외가 파일 열기 대화상자(#32770)로, 이건 표준 컨트롤이라 WM_SETTEXT가 먹는다.
#
# ★ 두 가지가 핵심이다.
#   ① 정확성 — «누르기·입력 직전에 그 자리에 진짜 그 창이 있는지» 매번 확인한다.
#      화면 자동화는 포커스를 뺏기는 순간 남의 창에 글자를 쏟아붓는다.
#   ② 속도 — 고정 Sleep 을 조건 대기(polling)로 바꿨다. 맥 쪽 kt.py 가 2026-09-08 에
#      같은 이유로 osascript·sleep 을 걷어낸 것과 같은 방향이다.
#
# 사용법
#   .\kakao.ps1 -List
#   .\kakao.ps1 -Room "나와의 채팅" -Attach a.mp4,b.mp4 -Send      # ← 한 방 (권장)
#   .\kakao.ps1 -Room "나와의 채팅" -Attach a.mp4                   # 패널까지만
#   .\kakao.ps1 -Room "나와의 채팅" -Msg "안녕하세요" -Send
#   .\kakao.ps1 -Room "나와의 채팅" -Shot out.png
#   .\kakao.ps1 -Click 349,601
#   .\kakao.ps1 -Wake      # 트레이에 접혀 안 뜰 때 (창을 다시 띄운다)
#   .\kakao.ps1 -Restore

[CmdletBinding()]
param(
  [switch]$List,
  [string]$Room,
  [string[]]$Attach,
  [switch]$Send,
  [string]$Shot,
  [string]$Click,
  [switch]$Restore,
  [switch]$Wake,
  [switch]$Chat,
  [ValidateRange(1, 500)][int]$Row,
  [string]$Msg
)

$ErrorActionPreference = 'Stop'
# ⚠️ 아래 실행부는 Invoke-Main 함수 안에 있다. 함수 안의 $PSBoundParameters 는 «함수»의 것이라
#    항상 비어 있다 — 스크립트 인자는 여기서 붙잡아 둬야 한다. (2026-09-11 -Row 10 이 안 먹어서 발견)
$BOUND = $PSBoundParameters
$STATE = Join-Path $env:TEMP 'kakao_win_state.json'
$sw = [Diagnostics.Stopwatch]::StartNew()

Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;using System.Drawing;
using System.Runtime.InteropServices;using System.Text;
public class KT {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h,StringBuilder s,int n);
  [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h,StringBuilder s,int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h,int x,int y,int w,int ht,bool rp);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int n);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x,int y);
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
  [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(POINT p);
  [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h,uint flags);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr h,uint cmd);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint from,uint to,bool attach);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk,byte scan,uint flags,IntPtr extra);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();

  // ★ 창을 앞으로 «확실히» 세운다.
  //   윈도우는 포커스 가로채기를 막는다 — 부르는 쪽이 전경이 아니면 SetForegroundWindow 가
  //   그냥 false 를 돌려주고 아무 일도 안 일어난다. (2026-09-11 실측: 숨김에서 깨운 직후 실패)
  //   ① 그냥 시도 → ② 전경 스레드에 입력 큐를 붙이고 시도 → ③ ALT 를 한 번 툭 쳐서
  //   «방금 사용자 입력이 있었다»로 만들고 시도. 셋 다 실패하면 false.
  public static bool Front(IntPtr h){
    ShowWindow(h,9);                                   // SW_RESTORE
    if(SetForegroundWindow(h) && GetForegroundWindow()==h) return true;

    IntPtr fg = GetForegroundWindow();
    uint me = GetCurrentThreadId();
    uint other = (fg==IntPtr.Zero) ? 0 : GetWindowThreadProcessId(fg, out _dummy);
    if(other!=0 && other!=me){
      AttachThreadInput(me, other, true);
      BringWindowToTop(h); SetForegroundWindow(h);
      AttachThreadInput(me, other, false);
      if(GetForegroundWindow()==h) return true;
    }
    keybd_event(0x12,0,0,IntPtr.Zero);                 // ALT down
    keybd_event(0x12,0,2,IntPtr.Zero);                 // ALT up (KEYEVENTF_KEYUP)
    BringWindowToTop(h); SetForegroundWindow(h);
    return GetForegroundWindow()==h;
  }
  static uint _dummy;

  // ★ 카카오톡의 팝업은 «그 채팅창이 주인»이다. 2026-09-11 실측:
  //     「파일 전송」 패널   GW_OWNER = 채팅창(24254258)
  //     파일 열기 대화상자   GW_OWNER = 채팅창(24254258)   (#32770, 제목 「열기」)
  //   그래서 «같은 프로세스인가»가 아니라 «이 방의 것인가»를 핏줄로 물을 수 있다.
  //   방이 여러 개 열려 있어도 남의 방 팝업을 집지 않는다.
  public static bool OwnedBy(IntPtr popup, IntPtr owner){
    IntPtr h = popup;
    for(int i=0;i<64;i++){
      h = GetWindow(h, 4);              // GW_OWNER
      if(h==IntPtr.Zero) return false;
      if(h==owner) return true;
    }
    return false;
  }
  [DllImport("user32.dll")] public static extern void mouse_event(uint f,uint x,uint y,uint d,IntPtr e);
  [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern IntPtr FindWindowEx(IntPtr p,IntPtr c,string cls,string win);
  [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr h,uint m,IntPtr w,string l);
  [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr h,uint m,IntPtr w,StringBuilder l);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h,uint m,IntPtr w,IntPtr l);
  [DllImport("user32.dll")] public static extern IntPtr GetDlgItem(IntPtr h,int id);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left,Top,Right,Bottom; }
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X,Y; }

  // ── 클릭 ──────────────────────────────────────────────────
  // 커서를 실제로 움직여 누른다. PostMessage 로 만든 클릭은 카카오톡 EVA 창이 받지 않는다
  // (2026-09-04 실측). mouse_event 자체도 «합성» 입력 API 이지만, 커서가 실제로 그 자리에
  // 있는 상태여야 카톡이 처리한다.
  //
  // ★ 누르기 직전에 그 자리에 정말 대상 창이 있는지 확인한다.
  //   expectRoot = 대상 최상위 창, expectPid = 그 창의 프로세스.
  //   툴팁·메뉴처럼 같은 프로세스가 띄운 것은 통과시킨다(카톡은 tooltips_class32 를 많이 쓴다).
  static void Aim(int x,int y,IntPtr expectRoot,uint expectPid){
    // 커서를 그 자리에 «실제로» 놓는다. 사람이 마우스를 건드리면 어긋나므로 세 번까지 다시 놓는다.
    // (2026-09-11 실측: 요청 1190,736 인데 1186,740 에 있었다 — 사람 손이 닿은 것)
    // 세 번 다 어긋나면 «지금 사람이 마우스를 쓰고 있다»는 뜻이니 누르지 않고 멈춘다.
    POINT p = new POINT();
    bool placed = false;
    for(int i=0;i<3 && !placed;i++){
      if(!SetCursorPos(x,y)) throw new InvalidOperationException("커서를 " + x + "," + y + " 로 옮기지 못했습니다");
      System.Threading.Thread.Sleep(i==0 ? 70 : 120);
      if(!GetCursorPos(out p)) throw new InvalidOperationException("커서 위치를 읽지 못했습니다");
      placed = (p.X==x && p.Y==y);
    }
    if(!placed)
      throw new InvalidOperationException("커서가 " + x + "," + y + " 가 아니라 " + p.X + "," + p.Y + " 에 있습니다 — 지금 마우스를 쓰고 계신 것 같습니다");
    IntPtr hit = WindowFromPoint(p);
    if(hit==IntPtr.Zero) throw new InvalidOperationException("그 자리에 창이 없습니다: " + x + "," + y);
    IntPtr root = GetAncestor(hit,2); // GA_ROOT
    if(root!=expectRoot){
      // 우리 창이 «주인»인 팝업(툴팁·메뉴·패널)이면 통과시킨다.
      // ⚠️ 예전엔 «같은 PID면 통과»였는데 그건 너무 헐렁하다 — 다른 채팅방 창도 같은 PID라서
      //    그걸 눌러 버린다. 뿌리 주인(GA_ROOTOWNER)이 같은지를 본다.
      //    실측(2026-09-11): 패널의 GA_ROOTOWNER = 그 채팅창.
      uint hitPid=0; GetWindowThreadProcessId(hit, out hitPid);
      bool sameOwner = GetAncestor(root,3)==GetAncestor(expectRoot,3);  // GA_ROOTOWNER
      if(hitPid!=expectPid || !sameOwner)
        throw new InvalidOperationException("그 자리를 다른 창이 덮고 있습니다 (" + x + "," + y + ")");
    }
  }
  public static void Tap(int x,int y,IntPtr expectRoot,uint expectPid){
    Aim(x,y,expectRoot,expectPid);
    mouse_event(0x02,0,0,0,IntPtr.Zero); System.Threading.Thread.Sleep(40);
    mouse_event(0x04,0,0,0,IntPtr.Zero); System.Threading.Thread.Sleep(80);
  }
  public static void DblTap(int x,int y,IntPtr expectRoot,uint expectPid){
    Aim(x,y,expectRoot,expectPid);
    mouse_event(0x02,0,0,0,IntPtr.Zero); System.Threading.Thread.Sleep(40);
    mouse_event(0x04,0,0,0,IntPtr.Zero); System.Threading.Thread.Sleep(90);
    mouse_event(0x02,0,0,0,IntPtr.Zero); System.Threading.Thread.Sleep(40);
    mouse_event(0x04,0,0,0,IntPtr.Zero); System.Threading.Thread.Sleep(80);
  }
  // 검증 없이 누른다 — -Click 로 사람이 직접 좌표를 줄 때만
  public static void TapRaw(int x,int y){
    SetCursorPos(x,y); System.Threading.Thread.Sleep(120);
    mouse_event(0x02,0,0,0,IntPtr.Zero); System.Threading.Thread.Sleep(40);
    mouse_event(0x04,0,0,0,IntPtr.Zero); System.Threading.Thread.Sleep(80);
  }
  public static void Hover(int x,int y){ SetCursorPos(x,y); System.Threading.Thread.Sleep(180); }

  // ── 화면 ──────────────────────────────────────────────────
  public static void Grab(string path,int x,int y,int w,int ht){
    using(var b=Shot(x,y,w,ht)) b.Save(path,System.Drawing.Imaging.ImageFormat.Png);
  }
  public static Bitmap Shot(int x,int y,int w,int ht){
    var b=new Bitmap(w,ht);
    using(var g=Graphics.FromImage(b)) g.CopyFromScreen(x,y,0,0,new Size(w,ht));
    return b;
  }
}
'@

# 카카오톡은 SYSTEM_AWARE 다 (실측: GetProcessDpiAwareness(PID) = 1, 2026-09-11).
# 그래서 우리도 system-DPI-aware 로 맞춘다 — 배율이 다른 모니터에서도 «같은 좌표계»가 되어
# 아래 실측 상수(107·39·69 등)가 그대로 맞는다.
#
# ⚠️ per-monitor v2 로 올리면 안 된다. 125% 모니터에서 같은 창이 1.25배로 읽혀
#    (실측: system=312x512 vs PMv2=390x640) 상수가 전부 어긋난다.
# ⚠️ 예전 주석의 «음수 좌표에서 캡처 좌표가 어긋난다»는 틀린 진단이었다. 2026-09-11 에
#    보조 모니터(-1920..0, 125%)에서 재보니 GetWindowRect·SetCursorPos·CopyFromScreen 이
#    셋 다 같은 좌표계로 맞아떨어졌다(클릭 적중·캡처 정상). 그래서 «주 모니터로 옮기는»
#    우회를 걷어냈다.
[KT]::SetProcessDPIAware() | Out-Null

# ── 공통 ─────────────────────────────────────────────────────

# 조건이 참이 될 때까지 기다린다. 고정 Sleep 대신 이걸 쓴다.
function Wait-For([scriptblock]$Test, [int]$TimeoutMs = 3000, [int]$IntervalMs = 40) {
  $t = [Diagnostics.Stopwatch]::StartNew()
  while ($true) {
    $r = & $Test
    if ($r) { return $r }
    if ($t.ElapsedMilliseconds -ge $TimeoutMs) { return $null }
    Start-Sleep -Milliseconds $IntervalMs
  }
}

function Get-KakaoWindows {
  $ids = @((Get-Process -Name KakaoTalk -ErrorAction SilentlyContinue).Id)
  if (-not $ids) { throw '카카오톡이 실행 중이 아닙니다. KakaoTalk.exe 를 먼저 띄우세요.' }
  $script:acc = @()
  $cb = [KT+EnumProc]{
    param($h, $p)
    [uint32]$o = 0; [KT]::GetWindowThreadProcessId($h, [ref]$o) | Out-Null
    if (($ids -contains [int]$o) -and [KT]::IsWindowVisible($h)) {
      $t = New-Object System.Text.StringBuilder 512; [KT]::GetWindowText($h, $t, 512) | Out-Null
      $c = New-Object System.Text.StringBuilder 128; [KT]::GetClassName($h, $c, 128) | Out-Null
      $r = New-Object KT+RECT; [KT]::GetWindowRect($h, [ref]$r) | Out-Null
      if (($r.Right - $r.Left) -gt 150 -and $t.Length -gt 0) {
        $script:acc += [PSCustomObject]@{
          H = $h.ToInt64(); Title = $t.ToString(); Cls = $c.ToString(); OwnerPid = [uint32]$o
          L = $r.Left; T = $r.Top; W = $r.Right - $r.Left; Ht = $r.Bottom - $r.Top
        }
      }
    }
    return $true
  }
  $ok = [KT]::EnumWindows($cb, [IntPtr]::Zero)
  [GC]::KeepAlive($cb)
  if (-not $ok) { throw '창 목록을 끝까지 훑지 못했습니다.' }

  # ★ 보이는 창이 하나도 없으면 «트레이에 접혀 숨겨진» 것일 수 있다 — 한 번 깨워 보고 다시 훑는다.
  #   (자주 나는 「카톡을 눌러도 안 뜬다」가 이 상태다. 2026-09-11 실측)
  if ($script:acc.Count -eq 0 -and -not $script:woken) {
    $script:woken = $true
    if ((Wake-Kakao) -gt 0) {
      Write-Host '  ※ 트레이에 접혀 있던 카카오톡 창을 깨웠습니다.'
      return (Get-KakaoWindows)
    }
  }
  return $script:acc
}

# ★ 트레이에 접힌 카카오톡 창을 다시 띄운다.
#
# 실측 2026-09-11: 「카톡 아이콘을 눌러도 창이 안 뜬다」가 자주 난다.
#   그때 상태 — 프로세스는 살아 있고(3일째), 메인 창
#   (EVA_Window_Dblclk 「카카오톡」 691,0 560x1032)이 «IsWindowVisible=False» 인 채로 남아 있다.
#   최소화(IsIconic)가 아니라 «숨김»이라 작업표시줄·트레이로는 안 깨어난다.
#   → ShowWindow(SW_SHOW=5) 한 번에 떴다. SW_RESTORE 만으로는 부족했다.
#
# ⚠️ 로그아웃 상태에서는 이걸 해도 카톡이 도로 숨긴다 — 그때는 로그인 창만 뜬다.
function Wake-Kakao {
  $ids = @((Get-Process -Name KakaoTalk -ErrorAction SilentlyContinue).Id)
  if (-not $ids) { throw '카카오톡이 실행 중이 아닙니다. KakaoTalk.exe 를 먼저 띄우세요.' }
  $script:hidden = @()
  $cb = [KT+EnumProc]{
    param($h, $p)
    [uint32]$o = 0; [KT]::GetWindowThreadProcessId($h, [ref]$o) | Out-Null
    if (($ids -contains [int]$o) -and (-not [KT]::IsWindowVisible($h))) {
      $c = New-Object System.Text.StringBuilder 128; [KT]::GetClassName($h, $c, 128) | Out-Null
      if ($c.ToString() -like 'EVA_Window*') {
        $r = New-Object KT+RECT; [KT]::GetWindowRect($h, [ref]$r) | Out-Null
        if (($r.Right - $r.Left) -gt 300 -and ($r.Bottom - $r.Top) -gt 300) { $script:hidden += $h.ToInt64() }
      }
    }
    return $true
  }
  [KT]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
  [GC]::KeepAlive($cb)

  $woke = 0
  foreach ($hh in $script:hidden) {
    $h = [IntPtr][int64]$hh
    [KT]::ShowWindow($h, 5) | Out-Null      # SW_SHOW  ← 이게 핵심이다
    [KT]::ShowWindow($h, 9) | Out-Null      # SW_RESTORE (최소화돼 있었을 경우)
    if (Wait-For { [KT]::IsWindowVisible($h) } 800 60) { $woke++ }
  }
  return $woke
}

# 창 사각형을 «지금» 다시 읽어 온다. 창이 움직였을 수 있으므로 좌표를 쓰기 직전에 부른다.
function Sync-Rect($win) {
  $h = [IntPtr][int64]$win.H
  $r = New-Object KT+RECT
  if (-not [KT]::GetWindowRect($h, [ref]$r)) { throw '창 위치를 읽지 못했습니다.' }
  $win.L = $r.Left; $win.T = $r.Top
  $win.W = $r.Right - $r.Left; $win.Ht = $r.Bottom - $r.Top
  return $win
}

# ★ 대상 확인 — 두 단계로 나눈다. (2026-09-11 실측으로 갈랐다)
#
#   Assert-Window   창이 그대로인가 (핸들·프로세스·제목). 마우스 클릭 전에 쓴다.
#   Assert-Front    거기에 «지금 앞에 나와 있는가»까지. 키보드 입력 전에만 쓴다.
#
# 왜 나눴나: 키보드(SendKeys)는 HWND 가 아니라 «그때 활성화된 창»으로 간다.
# 그래서 ^a·{DEL}·{ENTER} 앞에서는 포커스까지 확인해야 남의 창에 쏟지 않는다.
# 반면 마우스는 «커서 밑에 무엇이 있는가»가 전부다 — KT::Aim 이 그걸 본다.
# 실제로 「파일 전송」 패널이 뜨면 포커스가 그 패널 창으로 넘어가는데,
# 그때 포커스를 따지면 정작 눌러야 할 전송 버튼을 못 누른다 (2026-09-11 실행하다 걸림).
function Assert-Window($win, [string]$what) {
  $h = [IntPtr][int64]$win.H
  if (-not [KT]::IsWindow($h)) { throw "대상 창이 사라졌습니다 — $what" }
  [uint32]$owner = 0; [KT]::GetWindowThreadProcessId($h, [ref]$owner) | Out-Null
  if ($owner -ne $win.OwnerPid) { throw "대상 창의 프로세스가 바뀌었습니다 — $what" }
  $t = New-Object System.Text.StringBuilder 512; [KT]::GetWindowText($h, $t, 512) | Out-Null
  if (-not [string]::Equals($t.ToString(), $win.Title, [StringComparison]::Ordinal)) {
    throw "대상 창 제목이 '$($t.ToString())' 로 바뀌었습니다 — $what"
  }
}

function Assert-Front($win, [string]$what) {
  Assert-Window $win $what
  if ([KT]::GetForegroundWindow() -ne [IntPtr][int64]$win.H) {
    throw "포커스를 잃었습니다 (다른 창이 앞에 있음) — $what. 입력을 중단합니다."
  }
}

# ★ 사람이 마우스를 쓰고 있으면 «멎을 때까지» 잠깐 기다린다.
#   이 스크립트는 커서를 실제로 움직여 누르므로, 사람 손과 부딪히면 서로 방해한다.
#   기다리지 않고 바로 실패시키면 「그냥 안 되는 도구」가 되고,
#   무시하고 누르면 엉뚱한 데를 누른다. 그래서 «잠깐 기다렸다가, 그래도 움직이면 멈춘다».
function Wait-MouseIdle([int]$TimeoutMs = 3000) {
  $prev = New-Object KT+POINT
  if (-not [KT]::GetCursorPos([ref]$prev)) { return $true }
  $t = [Diagnostics.Stopwatch]::StartNew()
  $announced = $false
  while ($t.ElapsedMilliseconds -lt $TimeoutMs) {
    Start-Sleep -Milliseconds 120
    $now = New-Object KT+POINT
    if (-not [KT]::GetCursorPos([ref]$now)) { return $true }
    if ($now.X -eq $prev.X -and $now.Y -eq $prev.Y) { return $true }
    if (-not $announced) { Write-Host '  ※ 마우스가 움직이고 있어 잠깐 기다립니다 — 손을 떼 주세요'; $announced = $true }
    $prev = $now
  }
  return $false
}

# 창을 앞으로 세운다. 고정 300ms 대기 대신 «진짜 앞에 왔는지» 폴링한다.
function Set-Front($win) {
  $null = Wait-MouseIdle 3000
  $h = [IntPtr][int64]$win.H
  [KT]::ShowWindow($h, 9) | Out-Null      # SW_RESTORE
  $got = Wait-For { [KT]::Front($h) } 3000 80
  if (-not $got) {
    throw "창을 앞으로 세우지 못했습니다: $($win.Title) — 다른 앱이 포커스를 놓지 않습니다. 그 앱을 최소화하고 다시 해 보세요."
  }
}

# 창 안 좌표를 누른다 — 창 밖이면 거부, 그 자리를 남이 덮고 있으면 거부.
function Tap-In($win, [int]$x, [int]$y, [string]$what) {
  Assert-Window $win $what
  if ($x -lt $win.L -or $x -ge ($win.L + $win.W) -or $y -lt $win.T -or $y -ge ($win.T + $win.Ht)) {
    throw "클릭 좌표 $x,$y 가 창 밖입니다 ($($win.L),$($win.T) $($win.W)x$($win.Ht)) — $what"
  }
  [KT]::Tap($x, $y, [IntPtr][int64]$win.H, $win.OwnerPid)
}

# ── 채팅 목록 ────────────────────────────────────────────────
# 방 제목은 사람 이름과 다르다 (예: 홍길동 → 「○○중 홍길동 선생님」).
# 그래서 «친구 검색»으로 찾지 않는다 — 검색창에 이름을 넣으면 결과가 없을 때
# 「친구 추가」 팝업에 걸려 빠져나오기 어렵다 (2026-09-09 실제로 걸렸다).
# 채팅 탭 목록을 눈으로 보고 행 번호로 연다.
#
# 좌표는 «메인 창 좌상단 기준 오프셋». 2026-09-09 창 893x816 에서 실측.
$TAB_CHAT   = @(31, 117)   # 왼쪽 사이드바 두 번째 아이콘(말풍선)
$ROW_X      = 200          # 행 가운데 (프로필 오른쪽 이름 영역)
$ROW1_Y     = 104          # 첫 행 중심
$ROW_PITCH  = 69           # 행 간격
$INPUT_UP   = 107          # 입력칸: 창 바닥에서 이만큼 위, 가로 중앙
$SENDBTN_UP = 41           # 「N개 전송」: 패널 «창» 바닥에서 이만큼 위, 패널 가로 중앙
                           # (2026-09-11 실측: 300x185 패널에서 버튼 테두리가 y 126~163,
                           #  중심 144 → 바닥에서 41. 버튼 높이 37px 이라 ±18px 여유)

function Find-Main {
  $ws = Get-KakaoWindows
  $m = @($ws | Where-Object { $_.Title -eq '카카오톡' })
  if ($m.Count -eq 0) {
    throw '메인 카카오톡 창이 없습니다 — 트레이에 접혀 있거나 로그아웃 상태입니다.'
  }
  # 최소화돼 -32000 에 접힌 작은 창(160x28)은 빼고 다시 센다 (2026-10-15 실측)
  if ($m.Count -gt 1) { $m = @($m | Where-Object { $_.L -gt -30000 }) }
  if ($m.Count -gt 1) {
    throw "'카카오톡' 제목의 창이 $($m.Count)개입니다 — 어느 것인지 정할 수 없습니다. -List 로 확인하세요."
  }
  return $m[0]
}

function Get-RowPoint($win, [int]$n) {
  return @(($win.L + $ROW_X), ($win.T + $ROW1_Y + $ROW_PITCH * ($n - 1)))
}

# 방 찾기 — «정확히 같은 제목»을 먼저 본다. 없을 때만 부분일치로 내려간다.
# ⚠️ 부분일치는 «의도한 방이 닫혀 있고 이름이 겹치는 다른 방만 열려 있을 때»
#    그 방을 골라 버린다. 그래서 부분일치로 정했으면 고른 제목을 반드시 찍어 준다.
function Find-Room([string]$needle) {
  if ([string]::IsNullOrWhiteSpace($needle)) { throw '-Room 에 채팅방 이름을 지정하세요.' }
  $wins = @(Get-KakaoWindows | Where-Object { $_.Title -ne '카카오톡' })

  $exact = @($wins | Where-Object { [string]::Equals($_.Title, $needle, [StringComparison]::Ordinal) })
  if ($exact.Count -eq 1) { return $exact[0] }
  if ($exact.Count -gt 1) { throw "제목이 정확히 '$needle' 인 창이 $($exact.Count)개입니다." }

  # 부분일치. -like 는 [ ] ? * 를 와일드카드로 먹으므로 반드시 이스케이프한다.
  $esc = [Management.Automation.WildcardPattern]::Escape($needle)
  $hit = @($wins | Where-Object { $_.Title -like "*$esc*" })
  if ($hit.Count -eq 0) {
    Write-Host '열려 있는 카카오톡 창:'
    $wins | Format-Table Title, Cls, W, Ht -AutoSize | Out-String | Write-Host
    throw "채팅창을 못 찾았습니다: '$needle' — 카카오톡에서 그 방을 먼저 열어 주세요."
  }
  if ($hit.Count -gt 1) {
    throw "'$needle' 에 여러 창이 걸립니다: $(($hit | ForEach-Object { $_.Title }) -join ' / ')"
  }
  Write-Host "  ※ 부분일치로 골랐습니다 — 실제 방 제목: 「$($hit[0].Title)」"
  return $hit[0]
}

# ── 파일 열기 대화상자 ────────────────────────────────────────
# 카카오톡 프로세스가 띄운 #32770 만 본다.
# ⚠️ 폭만 보면 «쿨메신저» 창(#32770, 492px)을 잡는다. 2026-09-09에 실제로 그래서
#    「파일 이름 입력칸을 못 찾았습니다」가 났다.
# ⚠️ «클릭 전부터 떠 있던» 대화상자도 잡으면 안 된다 → 미리 목록을 떠 두고 새로 생긴 것만 받는다.
function Get-KakaoDialogs([uint32]$ownerPid) {
  $script:dlgs = @()
  $cb = [KT+EnumProc]{
    param($h, $p)
    if ([KT]::IsWindowVisible($h)) {
      $c = New-Object System.Text.StringBuilder 64; [KT]::GetClassName($h, $c, 64) | Out-Null
      if ($c.ToString() -eq '#32770') {
        [uint32]$o = 0; [KT]::GetWindowThreadProcessId($h, [ref]$o) | Out-Null
        $r = New-Object KT+RECT; [KT]::GetWindowRect($h, [ref]$r) | Out-Null
        if ((($r.Right - $r.Left) -gt 300) -and ($o -eq $ownerPid)) { $script:dlgs += $h.ToInt64() }
      }
    }
    return $true
  }
  $enumOk = [KT]::EnumWindows($cb, [IntPtr]::Zero)
  [GC]::KeepAlive($cb)
  if (-not $enumOk) { throw '창 열거가 끝까지 안 됐습니다 — 부분 목록으로 판단하지 않습니다.' }
  return @($script:dlgs)
}

# $before 에 없던 대화상자가 «정확히 하나» 새로 뜨고, 그게 «이 채팅방 소유»면 그 핸들을 돌려준다.
# 실측 2026-09-11: 파일 열기 대화상자(#32770 「열기」 471,186 1449x511)의 GW_OWNER = 그 채팅창.
# 그래서 «같은 카톡 프로세스»가 아니라 «이 방이 연 것»인지까지 물을 수 있다.
function Wait-NewDialog($win, $before, [int]$ms) {
  return Wait-For {
    $now = @(Get-KakaoDialogs $win.OwnerPid | Where-Object { $before -notcontains $_ })
    if ($now.Count -gt 1) { throw "파일 대화상자가 $($now.Count)개 새로 떴습니다 — 어느 것인지 정할 수 없습니다." }
    if ($now.Count -eq 0) { return $null }
    $dh = [IntPtr][int64]$now[0]
    if (-not [KT]::OwnedBy($dh, [IntPtr][int64]$win.H)) {
      throw '새로 뜬 파일 대화상자가 이 채팅방 것이 아닙니다 — 중단합니다.'
    }
    return $now[0]
  } $ms 60
}

# ── 「파일 전송」 패널 ────────────────────────────────────────
# ★ 패널은 «그림»이 아니라 별도의 최상위 창이다. (2026-09-11 실측)
#     클래스 EVA_Window_Dblclk · 제목 없음 · 카카오톡과 같은 PID · 채팅창 한가운데
#     예: 채팅창 1166,121 475x640 일 때 패널 1253,348 300x185
#
# 예전에는 «창 안 순백 픽셀의 경계상자»로 찾았다. 그건
#   ① 테마·배율·HDR·가림에 다 흔들리고
#   ② 폴링할 때마다 창 전체를 캡처해야 해서 느리고
#   ③ «흰 픽셀이 안 보인다»를 «전송됐다»로 잘못 읽었다.
# 창으로 찾으면 셋 다 사라진다 — 있으면 있고 없으면 없다.
function Find-SendPanel($win, [int]$ms = 8000) {
  return Wait-For {
    $null = Sync-Rect $win
    $winCx = $win.L + [int]($win.W / 2)
    $winCy = $win.T + [int]($win.Ht / 2)
    $script:panels = @()
    $cb = [KT+EnumProc]{
      param($h, $p)
      if ([KT]::IsWindowVisible($h)) {
        [uint32]$o = 0; [KT]::GetWindowThreadProcessId($h, [ref]$o) | Out-Null
        if ($o -eq $win.OwnerPid) {
          $t = New-Object System.Text.StringBuilder 8; [KT]::GetWindowText($h, $t, 8) | Out-Null
          if ($t.Length -eq 0) {
            $c = New-Object System.Text.StringBuilder 64; [KT]::GetClassName($h, $c, 64) | Out-Null
            if ($c.ToString() -eq 'EVA_Window_Dblclk') {
              $r = New-Object KT+RECT; [KT]::GetWindowRect($h, [ref]$r) | Out-Null
              $w = $r.Right - $r.Left; $ht = $r.Bottom - $r.Top
              $cx = $r.Left + [int]($w / 2)
              $cy = $r.Top + [int]($ht / 2)
              # 패널로 인정할 조건 — «이 채팅방의» 패널인지까지 본다.
              #   ① 주인이 이 채팅창일 것 (GW_OWNER 체인) ← 이게 결정적이다
              #   ② 크기: 폭 200~420, 높이 150~420
              #   ③ 이 채팅창 «두 축 모두» 중앙 정렬 (±25px) · 채팅창 안에 들어갈 것
              # 실측 2026-09-11: 채팅창 1166,121 475x640 (핸들 24254258)
              #   패널 1253,348 300x185, GW_OWNER = 24254258, 중심 1403,440 (채팅창 중심 1403,441)
              if ([KT]::OwnedBy($h, [IntPtr][int64]$win.H) -and
                  $w -ge 200 -and $w -le 420 -and $ht -ge 150 -and $ht -le 420 -and
                  [Math]::Abs($cx - $winCx) -le 25 -and [Math]::Abs($cy - $winCy) -le 25 -and
                  $r.Left -ge $win.L -and $r.Top -ge $win.T -and
                  $r.Right -le ($win.L + $win.W) -and $r.Bottom -le ($win.T + $win.Ht)) {
                $script:panels += [PSCustomObject]@{
                  H = $h.ToInt64(); Title = ''; Cls = 'EVA_Window_Dblclk'; OwnerPid = $win.OwnerPid
                  L = $r.Left; T = $r.Top; W = $w; Ht = $ht
                }
              }
            }
          }
        }
      }
      return $true
    }
    $enumOk = [KT]::EnumWindows($cb, [IntPtr]::Zero)
    [GC]::KeepAlive($cb)
    if (-not $enumOk) { throw '창 열거가 끝까지 안 됐습니다 — 부분 목록으로 판단하지 않습니다.' }
    if ($script:panels.Count -eq 0) { return $null }
    if ($script:panels.Count -gt 1) {
      throw "「파일 전송」 패널 후보가 $($script:panels.Count)개입니다 — 어느 것인지 정할 수 없습니다."
    }
    $pn = $script:panels[0]
    Add-Member -InputObject $pn -NotePropertyName BtnX -NotePropertyValue ($pn.L + [int]($pn.W / 2))
    Add-Member -InputObject $pn -NotePropertyName BtnY -NotePropertyValue ($pn.T + $pn.Ht - $SENDBTN_UP)
    return $pn
  } $ms 60
}

# ── 첨부 ─────────────────────────────────────────────────────
# 창을 건드리기 «전에» 파일부터 검증한다. Test-Path 는 폴더도 통과시키므로 Get-Item 으로 본다.
function Resolve-Attachments([string[]]$paths) {
  $full = @()
  foreach ($p in $paths) {
    $item = Get-Item -LiteralPath $p -Force -ErrorAction Stop
    if ($item.PSProvider.Name -ne 'FileSystem') { throw "파일 시스템 경로가 아닙니다: $p" }
    if ($item.PSIsContainer) { throw "폴더는 보낼 수 없습니다: $p" }
    $full += $item.FullName
  }
  return $full
}

function Invoke-Attach($win, [string[]]$full) {
  # 하단 바 아이콘은 창 왼쪽 24px 부터 32px 간격으로 놓인다.
  # ⚠️ 아이콘 «순서»는 창마다 다르다 (계정·기능에 따라 개수가 달라짐).
  #    2026-09-04 어느 1:1 방(475px) = 종이가 2번째(+56)
  #    2026-09-09 다른 1:1 방(380px)     = 😊 ☑ 💬 📄 ✂ 라 종이가 4번째(+120)
  #    그래서 고정 좌표로 찍지 않고 «대화상자가 열릴 때까지» 후보를 훑는다.
  # 한 번 찾으면 창 «폭x높이»별로 기억해 두고 다음부터 그 자리부터 시도한다.
  #   (폭만 키로 쓰면 폭이 같고 아이콘 배치가 다른 방에서 헛클릭한다)
  $null = Sync-Rect $win
  $py = $win.T + $win.Ht - 25

  $memo = @{}
  $memoPath = Join-Path $env:TEMP 'kakao_papericon.json'
  if (Test-Path $memoPath) {
    try {
      (Get-Content $memoPath -Raw | ConvertFrom-Json).PSObject.Properties |
        ForEach-Object { $memo[$_.Name] = [int]$_.Value }
    } catch {}
  }
  $key = '{0}x{1}' -f $win.W, $win.Ht
  $cands = @(120, 56, 88, 152, 24, 184, 216)
  if ($memo.ContainsKey($key)) { $cands = @($memo[$key]) + ($cands | Where-Object { $_ -ne $memo[$key] }) }

  Set-Front $win
  $before = @(Get-KakaoDialogs $win.OwnerPid)
  if ($before.Count -gt 0) {
    throw "카카오톡 파일 대화상자가 이미 $($before.Count)개 떠 있습니다 — 닫고 다시 실행하세요."
  }

  $dlg = $null; $hitOffset = $null
  foreach ($ox in $cands) {
    $null = Sync-Rect $win
    $px = $win.L + $ox
    $py = $win.T + $win.Ht - 25
    [KT]::Hover($px, $py)                       # hover 먼저 (첫 클릭이 씹힌다)
    Tap-In $win $px $py "종이 아이콘 후보 +$ox"
    $dlg = Wait-NewDialog $win $before 1100
    if ($dlg) { $hitOffset = $ox; break }

    # 엉뚱한 아이콘이면 메뉴·패널이 열렸을 수 있다. 입력칸을 눌러 닫는다.
    # (Escape 는 채팅창을 통째로 닫아 버리므로 절대 쓰지 않는다)
    #
    # ★ 여기가 까다롭다. 후보당 대기를 2500ms → 1100ms 로 줄인 대신,
    #   «대화상자가 조금 늦게 뜨는 경우»를 두 겹으로 받는다.
    #   ⓐ 닫기 클릭 «직전»에 한 번 더 본다 (대기 0)
    #   ⓑ 그래도 클릭 순간에 떠 버리면 Tap-In 이 「다른 창이 덮고 있습니다」로 막는데,
    #      그때 통째로 실패시키지 않고 «그게 우리가 기다리던 대화상자인지» 다시 본다.
    $dlg = Wait-NewDialog $win $before 0
    if ($dlg) { $hitOffset = $ox; Write-Host "  (+$ox 이 맞았다 — 대화상자가 늦게 열림)"; break }

    $null = Sync-Rect $win
    try {
      Tap-In $win ($win.L + [int]($win.W / 2)) ($win.T + $win.Ht - $INPUT_UP) '메뉴 닫기'
    } catch {
      $dlg = Wait-NewDialog $win $before 400
      if ($dlg) { $hitOffset = $ox; Write-Host "  (+$ox 이 맞았다 — 닫으려는 순간 대화상자가 떴다)"; break }
      throw
    }
    $dlg = Wait-NewDialog $win $before 250
    if ($dlg) { $hitOffset = $ox; Write-Host "  (+$ox 이 맞았다 — 대화상자가 늦게 열림)"; break }
    Write-Host "  종이 아이콘 아님 (+$ox) — 다음 자리 시도"
  }
  if (-not $dlg) {
    throw '파일 열기 대화상자가 안 열렸습니다. -Shot 으로 하단 아이콘 줄을 보고 종이 아이콘 위치를 확인하세요.'
  }
  Write-Host "종이 아이콘: 창 왼쪽 +$hitOffset (창 $key)"
  $memo[$key] = $hitOffset
  try { $memo | ConvertTo-Json | Set-Content $memoPath -Encoding utf8 } catch {}

  # 파일 이름 칸에 전체 경로를 따옴표로 묶어 한 번에 넣는다 (다중 선택)
  # ⚠️ FindWindowEx 는 부모가 0 이면 «데스크톱 전체»를 뒤진다 → 단계마다 확인한다
  $dlgH = [IntPtr][int64]$dlg
  $parent = $dlgH
  foreach ($cls in @('ComboBoxEx32', 'ComboBox', 'Edit')) {
    $parent = [KT]::FindWindowEx($parent, [IntPtr]::Zero, $cls, $null)
    if ($parent -eq [IntPtr]::Zero) { throw "파일 대화상자에서 $cls 를 못 찾았습니다." }
  }
  $edit = $parent
  $text = ($full | ForEach-Object { '"' + $_ + '"' }) -join ' '
  [KT]::SendMessage($edit, 0x000C, [IntPtr]::Zero, $text) | Out-Null      # WM_SETTEXT

  # 되읽어 확인한다. 표준 Edit 이라 WM_GETTEXT 는 프로세스를 건너서도 먹는다.
  # (GetWindowText 는 프로세스가 다르면 빈 문자열이 온다 — 그래서 WM_GETTEXT 를 쓴다)
  $buf = New-Object System.Text.StringBuilder ($text.Length + 8)
  [KT]::SendMessage($edit, 0x000D, [IntPtr]$buf.Capacity, $buf) | Out-Null
  if (-not [string]::Equals($buf.ToString(), $text, [StringComparison]::Ordinal)) {
    throw "파일 이름 칸에 경로가 제대로 안 들어갔습니다 (넣은 $($text.Length)자 / 읽은 $($buf.Length)자)."
  }

  $ok = [KT]::GetDlgItem($dlgH, 1)                                        # IDOK = 열기(O)
  if ($ok -eq [IntPtr]::Zero) { throw '파일 대화상자에 「열기」 버튼이 없습니다.' }
  [KT]::SendMessage($ok, 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null   # BM_CLICK

  # 대화상자가 닫히는 것까지 확인한다
  $closed = Wait-For { -not [KT]::IsWindow($dlgH) } 4000 80
  if (-not $closed) { throw '「열기」를 눌렀지만 파일 대화상자가 안 닫혔습니다.' }
  return $full.Count
}

# ── 실행 ─────────────────────────────────────────────────────
function Invoke-Main {

  if ($List) {
    $ws = Get-KakaoWindows
    if (-not $ws) { Write-Host '보이는 카카오톡 창이 없습니다 — 트레이에 접혀 있거나 로그아웃 상태입니다.' }
    else { $ws | Format-Table H, Title, Cls, L, T, W, Ht -AutoSize | Out-String -Width 200 | Write-Host }
    return
  }

  if ($Click) {
    $xy = $Click -split ','
    if ($xy.Count -ne 2) { throw '-Click 은 "x,y" 형식입니다.' }
    [KT]::TapRaw([int]$xy[0].Trim(), [int]$xy[1].Trim())
    return
  }

  if ($Wake) {
    $woke = Wake-Kakao
    if ($woke -eq 0) {
      Write-Host '깨울 숨은 창이 없습니다 — 이미 떠 있거나, 로그아웃 상태입니다. -List 로 확인하세요.'
    } else {
      Write-Host "카카오톡 창 $woke 개를 다시 띄웠습니다."
      Get-KakaoWindows | Format-Table H, Title, Cls, L, T, W, Ht -AutoSize | Out-String -Width 200 | Write-Host
    }
    return
  }

  if ($Restore) {
    if (-not (Test-Path $STATE)) { Write-Host '되돌릴 위치 기록이 없습니다.'; return }
    $s = Get-Content $STATE -Raw | ConvertFrom-Json
    $h = [IntPtr][int64]$s.H
    if (-not [KT]::IsWindow($h)) { throw '기록된 창이 이미 없습니다. 기록은 그대로 둡니다.' }
    if (-not [KT]::MoveWindow($h, $s.L, $s.T, $s.W, $s.Ht, $true)) {
      throw '창을 되돌리지 못했습니다. 기록은 그대로 둡니다.'
    }
    Remove-Item $STATE -Force
    Write-Host "창 원위치: $($s.L),$($s.T) $($s.W)x$($s.Ht)"
    return
  }

  if ($Chat) {
    $m = Find-Main
    Set-Front $m
    $null = Sync-Rect $m
    Tap-In $m ($m.L + $TAB_CHAT[0]) ($m.T + $TAB_CHAT[1]) '채팅 탭'
    Start-Sleep -Milliseconds 600     # 목록이 그려지는 시간 — 화면 말고는 확인할 방법이 없다
    $out = if ($Shot) { $Shot } else { Join-Path $env:TEMP 'kakao_chatlist.png' }
    $null = Sync-Rect $m
    [KT]::Grab($out, $m.L, $m.T, $m.W, $m.Ht)
    Write-Host "채팅 목록 캡처: $out  (창 $($m.L),$($m.T) $($m.W)x$($m.Ht))"
    Write-Host "행 N 열기:  -Row N   (1행 중심 y=+$ROW1_Y, 간격 $ROW_PITCH)"
    return
  }

  if ($BOUND.ContainsKey('Row')) {
    $m = Find-Main
    Set-Front $m
    $null = Sync-Rect $m
    $before = @(Get-KakaoWindows | Where-Object { $_.Title -ne '카카오톡' } | ForEach-Object { $_.H })
    $pt = Get-RowPoint $m $Row
    Assert-Window $m "$Row 행"
    if ($pt[1] -ge ($m.T + $m.Ht)) {
      throw "행 $Row 의 좌표가 창 아래로 넘어갑니다 — 목록에 그만큼 행이 없습니다."
    }
    [KT]::DblTap($pt[0], $pt[1], [IntPtr][int64]$m.H, $m.OwnerPid)
    $opened = Wait-For {
      $now = @(Get-KakaoWindows | Where-Object { $_.Title -ne '카카오톡' -and $before -notcontains $_.H })
      if ($now.Count -ge 1) { return $now } else { return $null }
    } 4000 100
    if (-not $opened) {
      Write-Host "행 $Row 을 눌렀지만 «새로» 열린 채팅창이 없습니다 — 이미 열려 있었거나 목록이 달라졌을 수 있습니다."
      Get-KakaoWindows | Where-Object { $_.Title -ne '카카오톡' } |
        Format-Table Title, W, Ht -AutoSize | Out-String | Write-Host
    } else {
      Write-Host '새로 열린 채팅창:'
      $opened | Format-Table Title, W, Ht -AutoSize | Out-String | Write-Host
    }
    return
  }

  if (-not $Room) { throw '-Room 으로 채팅방 이름을 지정하세요. (-List 로 목록 확인)' }

  # ★ 두 가지를 한 번에 시키지 않는다.
  #   예전에는 -Attach 와 -Msg 를 같이 주면 «파일 패널이 떠 있는 채로» 메시지 입력이 이어져서
  #   ^a·{DEL}·{ENTER} 가 엉뚱한 데로 갔다. 아예 막는다.
  if ($Attach -and $BOUND.ContainsKey('Msg')) {
    throw '-Attach 와 -Msg 는 같이 쓸 수 없습니다. 두 번 나눠 실행하세요.'
  }

  # 창을 건드리기 «전에» 파일부터 검증한다
  $full = $null
  if ($Attach) { $full = Resolve-Attachments $Attach }

  $win = Find-Room $Room
  $null = Sync-Rect $win
  Write-Host ("방: {0}  ({1},{2} {3}x{4})" -f $win.Title, $win.L, $win.T, $win.W, $win.Ht)

  if ($Attach) {
    $n = Invoke-Attach $win $full
    $panel = Find-SendPanel $win
    if (-not $panel) {
      throw '「파일 전송」 패널 창을 못 찾았습니다. -Shot 으로 화면을 확인하세요. (카톡이 패널을 창이 아닌 그림으로 그리도록 바뀌었을 수 있습니다)'
    }
    Write-Host ("패널 창 {0},{1} {2}x{3} / 전송버튼 {4},{5}" -f `
                $panel.L, $panel.T, $panel.W, $panel.Ht, $panel.BtnX, $panel.BtnY)

    if ($Send) {
      $panelH = [IntPtr][int64]$panel.H
      # ★ 패널이 그 사이 움직였을 수 있다 — 누르기 «직전에» 다시 재서 계산한다.
      #   («창 안이긴 한데 딴 데»를 누르는 것은 창 검사로는 못 걸러진다)
      Assert-Window $win '전송 직전 채팅방'
      Assert-Window $panel '전송 직전 패널'
      $null = Sync-Rect $panel
      $bx = $panel.L + [int]($panel.W / 2)
      $by = $panel.T + $panel.Ht - $SENDBTN_UP
      Tap-In $panel $bx $by '전송 버튼'
      # ★ 패널 «창»이 사라지는 것을 확인한다. 예전처럼 «흰 픽셀이 안 보인다»가 아니라
      #   창 핸들이 죽었는지를 보는 것이라, 창이 가려지거나 화면이 바뀌어도 흔들리지 않는다.
      $closed = Wait-For { -not [KT]::IsWindow($panelH) } 5000 60
      if (-not $closed) {
        throw '전송 결과 미확인: 「파일 전송」 패널이 안 닫혔습니다. -Shot 으로 확인하세요.'
      }
      Write-Host "전송했습니다 ($n 개) — 패널 창이 닫힌 것까지 확인. 업로드 진행은 백그라운드입니다."
    }
    else { Write-Host "패널까지 열었습니다. 보내려면 -Send 를 붙이거나 -Click $($panel.BtnX),$($panel.BtnY)" }
  }

  # ── 텍스트 메시지 ────────────────────────────────────────
  # 한글은 키 입력으로 치지 않는다 — 클립보드에 넣고 Ctrl+V.
  # 입력칸은 창 아래에서 약 107px 위, 가로 중앙 (2026-09-09 실측, 창 380x1032).
  if ($BOUND.ContainsKey('Msg')) {
    if ([string]::IsNullOrEmpty($Msg)) { throw '-Msg 가 비어 있습니다.' }
    Add-Type -AssemblyName System.Windows.Forms

    Set-Front $win
    $null = Sync-Rect $win
    Tap-In $win ($win.L + [int]($win.W / 2)) ($win.T + $win.Ht - $INPUT_UP) '입력칸'

    # 사용자 클립보드를 빌려 쓰는 것이므로 끝나면 돌려놓는다
    $savedClip = $null
    try { $savedClip = Get-Clipboard -Raw -ErrorAction SilentlyContinue } catch {}

    try {
      Set-Clipboard -Value $Msg
      $back = Get-Clipboard -Raw
      if (-not [string]::Equals($back, $Msg, [StringComparison]::Ordinal)) {
        throw '클립보드에 본문이 제대로 들어가지 않았습니다.'
      }

      # ★ 입력칸을 «먼저 비운다». 2026-09-09에 이걸 안 해서 남아 있던 글자
      #   («홍길동») 뒤에 본문이 붙어 「홍길동부장님~!」으로 발송됐다.
      #   사람이 쓰다 만 글이 남아 있을 수 있으므로 항상 전체선택 후 지운다.
      #   ★ 매 키 직전에 Assert-Front — 포커스를 뺏겼으면 여기서 멈춘다.
      Assert-Front $win '전체선택'
      [System.Windows.Forms.SendKeys]::SendWait('^a')
      Start-Sleep -Milliseconds 80
      Assert-Front $win '지우기'
      [System.Windows.Forms.SendKeys]::SendWait('{DEL}')
      Start-Sleep -Milliseconds 100
      # ★ 클립보드를 넣어 둔 뒤 ^a·{DEL} 를 거치는 동안 남이 바꿔치기할 수 있다.
      #   붙여넣기 직전에 «아직 내 본문인지» 다시 본다.
      if (-not [string]::Equals((Get-Clipboard -Raw), $Msg, [StringComparison]::Ordinal)) {
        throw '클립보드가 중간에 바뀌었습니다 — 발송을 중단합니다.'
      }
      Assert-Front $win '붙여넣기'
      [System.Windows.Forms.SendKeys]::SendWait('^v')
      # EVA 입력칸은 내용을 읽을 방법이 없다. 붙여넣기가 끝났는지 확인할 수단이 없으므로
      # 이 대기만은 줄이지 않는다 — 덜 붙은 채로 Enter 가 나가면 잘린 글이 발송된다.
      Start-Sleep -Milliseconds 400

      if ($Send) {
        Assert-Front $win '전송(Enter)'
        [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
        Start-Sleep -Milliseconds 300
        Write-Host "Enter 를 보냈습니다 ($($Msg.Length)자). 실제 발송 여부는 화면으로 확인하세요."
      } else {
        Write-Host "입력칸에 넣어만 뒀습니다 ($($Msg.Length)자) — 보내려면 -Send"
      }
    }
    finally {
      if ($null -ne $savedClip -and $savedClip -ne '') {
        try { Set-Clipboard -Value $savedClip } catch {}
      }
    }
  }

  if ($Shot) {
    $null = Sync-Rect $win
    [KT]::Grab($Shot, $win.L, $win.T, $win.W, $win.Ht)
    Write-Host "저장: $Shot"
  }
}

try {
  Invoke-Main
  Write-Host ("소요 {0:N1}초" -f ($sw.Elapsed.TotalSeconds))
  exit 0
}
catch {
  [Console]::Error.WriteLine('✗ ' + $_.Exception.Message)
  Write-Host ("소요 {0:N1}초" -f ($sw.Elapsed.TotalSeconds))
  exit 1
}
