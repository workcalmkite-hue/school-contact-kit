import asyncio
import json
import os
import threading
import time
import ctypes
from ctypes import wintypes
from collections import deque

import win32clipboard
import win32gui
import win32api
import win32con
import win32ui
import win32process
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

# numpy·PIL 은 서버 시작 때 메인 스레드에서 미리 불러 둔다.
# 발송 도중(작업 스레드)에 처음 import 하다가 numpy 확장 모듈 로딩에서 영영 멈춘 적이 있다
# (2026-10-28 py-spy 실측: _match_template 의 import numpy 에서 20분 넘게 정지 → 세션 전체가 멈춤).
import numpy  # noqa: F401
from PIL import Image, ImageGrab  # noqa: F401

server = Server("coolmessenger")

# ── Win32 DLL 래퍼 (64비트 포인터 안전) ────────────────────────────────────
_k32 = ctypes.WinDLL('kernel32', use_last_error=True)
_k32.OpenProcess.argtypes    = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype     = wintypes.HANDLE
_k32.CloseHandle.argtypes    = [wintypes.HANDLE]
_k32.CloseHandle.restype     = wintypes.BOOL
_k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                  ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
_k32.VirtualAllocEx.restype  = ctypes.c_void_p
_k32.VirtualFreeEx.argtypes  = [wintypes.HANDLE, ctypes.c_void_p,
                                  ctypes.c_size_t, wintypes.DWORD]
_k32.VirtualFreeEx.restype   = wintypes.BOOL
_k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                     ctypes.c_void_p, ctypes.c_size_t,
                                     ctypes.POINTER(ctypes.c_size_t)]
_k32.WriteProcessMemory.restype  = wintypes.BOOL
_k32.ReadProcessMemory.argtypes  = [wintypes.HANDLE, ctypes.c_void_p,
                                     ctypes.c_void_p, ctypes.c_size_t,
                                     ctypes.POINTER(ctypes.c_size_t)]
_k32.ReadProcessMemory.restype   = wintypes.BOOL
_k32.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
_k32.IsWow64Process.restype  = wintypes.BOOL

class _POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]

_u32 = ctypes.WinDLL('user32', use_last_error=True)
_u32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_POINT)]
_u32.ClientToScreen.restype  = wintypes.BOOL

# ── TreeView 상수 ────────────────────────────────────────────────────────────
TVM_GETNEXTITEM   = 0x110A
TVM_GETITEMW      = 0x113F
TVM_GETITEMRECT   = 0x1104
TVM_ENSUREVISIBLE = 0x1120
TVGN_ROOT  = 0x0000
TVGN_NEXT  = 0x0001
TVGN_CHILD = 0x0004
TVIF_TEXT   = 0x0001
TVIF_HANDLE = 0x0010
MEM_COMMIT     = 0x1000
MEM_RELEASE    = 0x8000
PAGE_READWRITE = 0x04
PROCESS_VM_READ      = 0x0010
PROCESS_VM_WRITE     = 0x0020
PROCESS_VM_OPERATION = 0x0008


# ── 기다리는 방식 ────────────────────────────────────────────────────────────
#
# 고정 time.sleep 은 «제일 느릴 때»에 맞춰 잡아야 해서, 평소에는 그만큼을 통째로 버린다.
# 그래서 «무엇을 기다리는지»가 분명한 자리는 전부 아래 _wait_for 로 바꿨다 —
# 최악의 경우 대기 시간은 예전과 같고(timeout), 평소에는 조건이 참이 되는 즉시 넘어간다.

_u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, ctypes.c_void_p]
_u32.SendMessageW.restype  = ctypes.c_ssize_t


def _wait_for(pred, timeout: float, interval: float = 0.04):
    """pred() 가 참인 값을 돌려줄 때까지 짧게 되물어본다. 시간이 다 되면 None."""
    end = time.perf_counter() + timeout
    while True:
        try:
            v = pred()
        except Exception:
            v = None
        if v:
            return v
        if time.perf_counter() >= end:
            return None
        time.sleep(interval)


def read_control_text(hwnd) -> str:
    """다른 프로세스의 입력란 내용을 읽는다.

    win32gui.GetWindowText 는 **다른 프로세스의 컨트롤 내용을 못 읽는다** (캡션만 준다).
    WM_GETTEXT 는 OS가 프로세스 경계를 넘겨주므로 Edit·RichEdit20W 모두 이걸로 읽는다.
    """
    buf = ctypes.create_unicode_buffer(1024)
    try:
        _u32.SendMessageW(hwnd, win32con.WM_GETTEXT, 1024,
                          ctypes.cast(buf, ctypes.c_void_p))
    except Exception:
        return ""
    return buf.value


def wait_control_text(hwnd, expect: str, timeout: float = 1.0) -> bool:
    """입력란에 expect 가 실제로 들어갈 때까지 기다린다. 못 읽는 환경이면 False."""
    return bool(_wait_for(lambda: expect in read_control_text(hwnd), timeout, 0.03))


# ── Win32 유틸리티 ──────────────────────────────────────────────────────────

def find_coolmessenger_pid() -> int:
    import psutil
    for p in psutil.process_iter(['name', 'pid']):
        if p.info['name'] and 'CoolMessenger' in p.info['name']:
            return p.info['pid']
    raise RuntimeError("CoolMessenger가 실행 중이지 않습니다.")


def get_window_by_title(pid: int, title: str):
    results = []
    def cb(hwnd, _):
        _, p = win32process.GetWindowThreadProcessId(hwnd)
        if p == pid and win32gui.GetWindowText(hwnd) == title:
            results.append(hwnd)
    win32gui.EnumWindows(cb, None)
    return results[0] if results else None


# ── 조용한 입력 (2026-11-12) ─────────────────────────────────────────────────
# 사용자 지시: «내가 마우스 쓰고 있잖아» — 발송 중에 **진짜 마우스·키보드를 쓰지 않는다.**
# 전부 «그 창에만 가는 메시지»(PostMessage)와 UI 자동화(UIA) 읽기로 한다. 실측(2026-11-12):
#   · 본문(Chromium 편집기): WM_CHAR 를 Chrome_RenderWidgetHostHWND 에 보내면 창이 뒤에 있어도 들어간다.
#     단 편집기가 «안쪽 포커스»를 잃었으면(툴바를 누른 뒤 등) 글자가 버려진다 → 편집기에 메시지 클릭을 먼저 준다
#   · 본문 되읽기: UIA TextPattern (Ctrl+A·Ctrl+C 불필요). 줄바꿈은 WM_CHAR 13
#   · 받는사람 입력란(RichEdit20W): WM_CHAR 로 이름 → 자동완성 뜸 → WM_KEYDOWN ↓·Enter 로 칩 생성
#   · 「예약전송」 오너드로우 체크: 메시지 클릭(WM_LBUTTONDOWN/UP)으로 꺼진다, 칩도 안 지워진다
#     (UIA Invoke 는 되지만 전송창이 앞으로 튀어나와 키보드를 뺏는다 → 쓰지 않는다)
#   · 굵게: 범위를 메시지 드래그로 고르고 「진하게」 버튼에 메시지 클릭 → 된다
#   · ⚠️ 빨간색은 아직 안 된다 — 색 팔레트가 진짜 커서 위치(GetCursorPos)로 칸을 고르는 듯, 메시지 클릭은 닫히기만 한다
#   · UIA SetValue·WM_PASTE 는 본문에 안 먹는다 (둘 다 실측)
# 진짜 입력이 필요한 옛 경로(전 교직원 발송 등)는 QUIET 일 때 InputNeeded 로 멈춘다.
# 꼭 옛 방식이 필요하면 환경변수 COOL_ALLOW_INPUT=1.
QUIET = os.environ.get("COOL_ALLOW_INPUT") != "1"


class InputNeeded(RuntimeError):
    pass


def _no_input(what: str):
    raise InputNeeded("%s 은(는) 진짜 마우스·키보드가 필요해서 멈췄습니다 (사용자 작업 방해 금지). "
                      "꼭 필요하면 COOL_ALLOW_INPUT=1 로 다시 실행하세요." % what)


def _lp(x: int, y: int) -> int:
    return win32api.MAKELONG(max(0, int(x)), max(0, int(y)))


def post_click(hwnd, x=None, y=None) -> None:
    """그 창에만 «여기를 눌렀다» 메시지를 보낸다. 진짜 커서는 안 움직인다. x,y 는 클라이언트 좌표."""
    _, _, r, b = win32gui.GetClientRect(hwnd)
    x = r // 2 if x is None else x
    y = b // 2 if y is None else y
    win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, _lp(x, y))
    time.sleep(0.03)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, _lp(x, y))
    time.sleep(0.05)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, _lp(x, y))
    time.sleep(0.15)


def post_drag(hwnd, p1, p2) -> None:
    win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, _lp(*p1))
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, _lp(*p1))
    time.sleep(0.05)
    win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON, _lp(*p2))
    time.sleep(0.05)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, _lp(*p2))
    time.sleep(0.2)


def post_key(hwnd, vk: int, scan: int) -> None:
    win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, 1 | (scan << 16))
    if vk == 0x0D:
        # Enter 는 keydown 만으로는 줄이 안 바뀌어 WM_CHAR 13 을 같이 보낸다.
        # «두 번 바뀌지 않나» — 2026-11-12 전송창을 PrintWindow 로 찍어 '첫줄/둘째줄/(빈 줄)/넷째줄' 그대로인 것 확인
        win32gui.PostMessage(hwnd, win32con.WM_CHAR, 13, 1 | (scan << 16))
    win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, 1 | (scan << 16) | 0xC0000000)


def post_chars(hwnd, text: str) -> None:
    """글자를 그 창에 WM_CHAR 로 보낸다. 줄바꿈은 Enter(13). 이모지 같은 4바이트 글자는 UTF-16 둘로 나눠 보낸다."""
    text = text.replace("\r\n", "\n")
    for ch in text:
        if ch == "\n":
            post_key(hwnd, 0x0D, 0x1C)
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            win32gui.PostMessage(hwnd, win32con.WM_CHAR, int.from_bytes(units[i:i + 2], "little"), 1)


_UIA = threading.local()   # COM 객체는 스레드마다 따로 (발송은 작업 스레드에서 돈다)
_BOLD = threading.local()  # 이번 본문에서 굵게를 못 한 말 (quiet_paste_body 가 채운다)


def _uia():
    if getattr(_UIA, "v", None) is None:
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U
        _UIA.v = (comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation), U)
    return _UIA.v


def _body_textpattern(send_hwnd: int):
    """본문 편집기(iframe 문서)의 TextPattern. 문서 요소 중 AutomationId 가 빈 것이 편집 영역이다 (2026-11-12 실측)."""
    uia, U = _uia()
    els = uia.ElementFromHandle(send_hwnd).FindAll(U.TreeScope_Descendants, uia.CreateTrueCondition())
    for i in range(els.Length):
        e = els.GetElement(i)
        if e.CurrentControlType == 50030 and e.CurrentAutomationId == "":
            p = e.GetCurrentPattern(10014)
            if p:
                return p.QueryInterface(U.IUIAutomationTextPattern)
    return None


def _norm(s: str) -> str:
    return " ".join((s or "").replace("\xa0", " ").split())


def _body_text(send_hwnd: int) -> str:
    try:
        tp = _body_textpattern(send_hwnd)
        return tp.DocumentRange.GetText(-1) if tp else ""
    except Exception:
        return ""


def _focus_body_end(send_hwnd: int) -> int:
    """편집기 빈 아래쪽을 메시지로 눌러 안쪽 포커스를 주고 커서를 끝으로 보낸다."""
    rw = find_body_target(send_hwnd)
    _, _, r, b = win32gui.GetClientRect(rw)
    post_click(rw, r // 2, max(1, b - 8))
    time.sleep(0.15)
    return rw


def _bold_span(send_hwnd: int, span: str, occurrence: int = 0) -> bool:
    """본문에서 span 을 메시지 드래그로 골라 「진하게」를 누른다. 굵게 됐으면 True."""
    tp = _body_textpattern(send_hwnd)
    if not tp:
        return False
    rg = tp.DocumentRange
    found = None
    for _ in range(occurrence + 1):
        found = rg.FindText(span, 0, 0)
        if not found:
            return False
        rg = tp.DocumentRange
        rg.MoveEndpointByRange(0, found, 1)    # 다음 찾기는 이 뒤부터 (Start ← found.End)
    rects = list(found.GetBoundingRectangles())
    if len(rects) < 4:
        return False
    rw = find_body_target(send_hwnd)
    first, last = rects[:4], rects[-4:]
    p1 = win32gui.ScreenToClient(rw, (int(first[0]) + 1, int(first[1] + first[3] / 2)))
    p2 = win32gui.ScreenToClient(rw, (int(last[0] + last[2]) - 1, int(last[1] + last[3] / 2)))
    post_drag(rw, p1, p2)
    sel = tp.GetSelection()
    if not sel.Length or _norm(sel.GetElement(0).GetText(-1)) != _norm(span):
        return False
    post_click(_find_child_by_id(send_hwnd, 70001))       # 「진하게」
    time.sleep(0.2)
    try:
        return int(found.GetAttributeValue(40007)) >= 600  # FontWeight
    except Exception:
        return False


def quiet_paste_body(send_hwnd: int, body: str) -> bool:
    """본문을 메시지로만 넣는다. **본문 전체가 실제로 들어갔는지** UIA 로 되읽어 확인한다.
    `**강조**` 는 굵게만 한다 (빨간색은 아직 못 한다 — 위 「조용한 입력」 설명)."""
    plain, _html = body_markup(body)
    # (강조할 말, 평문에서 그 말이 몇 번째로 나오는지). 표시 없이 앞에 같은 말이 있어도 맞는 자리를 고른다
    spans = []
    for i, m in enumerate(_RED.finditer(body)):
        at = m.start() - 4 * i                      # 앞선 ** ** 들을 뺀 평문 위치
        spans.append((m.group(1), plain[:at].count(m.group(1))))
    want = _norm(plain)
    if not want:
        return False
    for attempt in range(2):
        rw = _focus_body_end(send_hwnd)
        if _norm(_body_text(send_hwnd)):
            return False          # 빈 편집기가 아니면 덮어쓰지 않는다 (지울 방법이 키보드뿐)
        post_chars(rw, plain)
        if _wait_for(lambda: _norm(_body_text(send_hwnd)) == want, 2.0 + len(plain) * 0.01, 0.1):
            break
        if _norm(_body_text(send_hwnd)):
            return False          # 일부만 들어갔다 — 보내지 않게 실패로
    else:
        return False
    _BOLD.missed = []
    for s, nth in spans:
        for _try in range(3):          # 가끔 첫 드래그가 안 먹는다 (2026-11-12 녹화 때 한 번) — 굵게 됐는지 보고 다시
            if _bold_span(send_hwnd, s, nth):
                break
            _focus_body_end(send_hwnd)
            time.sleep(0.3)
        else:
            _BOLD.missed.append(s)     # 세 번 다 안 됐다 — 보내는 쪽에서 멈춘다
    if spans:
        _focus_body_end(send_hwnd)   # 선택 풀기 + 커서를 끝으로 (이모티콘이 끝에 붙게)
    return _norm(_body_text(send_hwnd)) == want


def _settle_checks(send_hwnd: int, still: float = 1.0, timeout: float = 4.0) -> None:
    """「예약전송」「나에게 보내기」 체크가 still 초 동안 안 바뀔 때까지 기다린다."""
    def state():
        return tuple(win32gui.GetWindowText(_find_child_by_id(send_hwnd, i) or 0) if _find_child_by_id(send_hwnd, i) else ""
                     for i in (1660, 1661))
    end = time.perf_counter() + timeout
    last, since = state(), time.perf_counter()
    while time.perf_counter() < end:
        time.sleep(0.1)
        cur = state()
        if cur != last or "" in cur:
            last, since = cur, time.perf_counter()
        elif time.perf_counter() - since >= still:
            return


def fg_guard():
    """발송 전에 앞에 있던 창을 기억해 두고, 쿨메신저가 앞을 가져갔으면 되돌려 주는 함수를 돌려준다."""
    u = ctypes.windll.user32
    orig = u.GetForegroundWindow()
    try:
        orig_pid = win32process.GetWindowThreadProcessId(orig)[1]
    except Exception:
        orig_pid = 0

    def give_back():
        fg = u.GetForegroundWindow()
        if not orig or fg == orig or not win32gui.IsWindow(orig):
            return
        try:
            cool = win32process.GetWindowThreadProcessId(fg)[1] == find_coolmessenger_pid()
        except Exception:
            cool = False
        if not cool or orig_pid == find_coolmessenger_pid():
            return
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        t = u.GetWindowThreadProcessId(fg, None)
        u.AttachThreadInput(cur, t, True)
        try:
            u.SetForegroundWindow(orig)
            u.BringWindowToTop(orig)
        finally:
            u.AttachThreadInput(cur, t, False)
    return give_back


def bring_to_front(hwnd) -> bool:
    """hwnd 를 맨 앞(키보드 입력을 받는 창)으로 꺼낸다. 실제로 앞에 나왔으면 True.
    QUIET 에서는 아무것도 안 한다 — 앞으로 꺼내면 사용자가 치던 글자가 그 창으로 들어간다 (2026-11-12 실제로 들어감)."""
    if QUIET:
        return False
    return _bring_to_front(hwnd)


def _bring_to_front(hwnd) -> bool:
    """hwnd 를 맨 앞(키보드 입력을 받는 창)으로 꺼낸다. 실제로 앞에 나왔으면 True.

    지금 앞에 있는 창의 스레드에도 붙어야 SetForegroundWindow 가 거절당하지 않는다
    (예전에는 대상 스레드에만 붙어서 실패할 때가 있었고, 실패해도 그대로 키 입력을 이어갔다).
    기다림은 0.3초까지만 — 키 입력 직전처럼 꼭 앞에 있어야 하는 자리는 require_front 가 더 버틴다.
    """
    u = ctypes.windll.user32
    if u.GetForegroundWindow() == hwnd:
        return True
    cur = ctypes.windll.kernel32.GetCurrentThreadId()
    tids = {u.GetWindowThreadProcessId(u.GetForegroundWindow(), None),
            u.GetWindowThreadProcessId(hwnd, None)} - {0, cur}
    for tid in tids:
        u.AttachThreadInput(cur, tid, True)
    try:
        # ⚠️ ShowWindow(SW_RESTORE) 는 «막 열린 전송창»에 걸면 1.6초를 잡아먹는다 (2026-09-08 실측).
        # 최소화된 창을 되살릴 때만 필요하므로 그때만 부른다.
        if win32gui.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
    finally:
        for tid in tids:
            u.AttachThreadInput(cur, tid, False)
    return bool(_wait_for(lambda: u.GetForegroundWindow() == hwnd, 0.3, 0.02))


def require_front(hwnd, what: str) -> None:
    """키보드로 입력하기 직전에 부른다. 앞에 못 꺼냈으면 **입력하지 않고** 멈춘다.

    키 입력(Ctrl+V·Enter)은 «지금 앞에 있는 창»으로 간다. 앞에 꺼내기가 실패한 채 이어가면
    본문이 사용자가 쓰던 창(VS Code 등)에 붙거나, Enter 가 엉뚱한 곳에서 눌린다.
    """
    if QUIET:
        return            # 조용한 입력은 창이 뒤에 있어도 된다
    if bring_to_front(hwnd):
        return
    # 거절당했으면 Alt 를 한 번 눌렀다 떼고 다시 청한다 (포그라운드 잠금이 풀린다, 2026-09-11 실측 성공)
    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    if not _wait_for(lambda: win32gui.GetForegroundWindow() == hwnd, 1.0, 0.02):
        fg = win32gui.GetForegroundWindow()
        raise RuntimeError("%s 창을 앞으로 꺼내지 못해 입력을 멈췄습니다 (지금 앞에 있는 창: %r). "
                           "다른 창을 누르지 말고 다시 시도해주세요." % (what, win32gui.GetWindowText(fg)))


def _visible_popups(pid: int, exclude=()) -> list:
    """같은 프로세스의 보이는 최상위 창 제목 (막고 있는 창을 알려줄 때 쓴다)."""
    out = []

    def cb(h, _):
        _, p = win32process.GetWindowThreadProcessId(h)
        if p == pid and h not in exclude and win32gui.IsWindowVisible(h):
            t = win32gui.GetWindowText(h)
            if t:
                out.append(t)

    win32gui.EnumWindows(cb, None)
    return out


def ensure_main_ready(main_hwnd: int, pid: int, timeout: float = 2.0) -> int:
    """메인창을 입력 가능한 상태로 만들고 조직도 트리 hwnd 를 돌려준다.

    ⚠️ 쿨메신저 메인창은 최소화가 아니라 **숨김**(IsIconic=0 · IsWindowVisible=0)이 되곤 한다.
    2026-09-11 실측: 이 상태에서 «조직도 트리를 찾을 수 없습니다»로 두 번 실패했고,
    전송창을 닫은 뒤 다시 숨은 것도 봤다. 예전 bring_to_front 는 최소화일 때만 되살렸다.
    ⚠️ 메인창 하나만 되살린다. 같은 프로세스의 광고 팝업까지 되살렸다가 전송창이 안 열렸다.
    """
    if win32gui.IsIconic(main_hwnd):
        win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
    elif not win32gui.IsWindowVisible(main_hwnd):
        win32gui.ShowWindow(main_hwnd, win32con.SW_SHOW)
    tree = _wait_for(lambda: find_child_by_class(main_hwnd, 'SysTreeView32'), timeout, 0.05)
    if not tree:
        raise RuntimeError("조직도 트리를 찾을 수 없습니다 (메인창을 되살려도 트리가 안 보입니다).")
    # 모달 창(광고·확인창)이 메인창을 막고 있으면 억지로 풀지 않고 알려준다
    if not win32gui.IsWindowEnabled(main_hwnd):
        raise RuntimeError("쿨메신저 메인창이 다른 창에 막혀 있습니다: %s — 그 창을 닫고 다시 시도해주세요."
                           % _visible_popups(pid, exclude=(main_hwnd,)))
    return tree


# ── 마우스 — SendInput 으로만 보낸다 ─────────────────────────────────────────
# ⚠️ 예전에는 win32api.mouse_event 를 썼는데, **2026-09-12 에 이 앱에 아예 안 닿게 됐다.**
# 증상: 커서는 SetCursorPos 로 제자리에 가는데 눌러도 조직도 트리 선택(caret)이 **전혀 안 바뀌고**,
# 전송창도 안 열렸다 (사람이 손으로 더블클릭하면 정상). 같은 자리를 SendInput 으로 누르니 바로 잡혔다.
# 좌표가 틀린 게 아니었다 — 실측으로 목표 줄(280~302)을 겨냥해 285~318 어디를 눌러도 그 줄이 잡힌다.
# 그래서 클릭은 전부 SendInput 으로 보내고, **누르기 전에 이동 이벤트(MOVE)를 같이 넣는다.**
_MOUSEEVENTF_MOVE, _MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", ctypes.POINTER(wintypes.ULONG))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("pad", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send_mouse(flags: int, x: int = 0, y: int = 0) -> bool:
    u32 = ctypes.windll.user32
    ax = ay = 0
    if flags & _MOUSEEVENTF_ABSOLUTE:
        # 모니터 여러 대 — 가상 데스크톱 전체 기준으로 맞춘다 (SM_X/Y/CX/CYVIRTUALSCREEN)
        vx, vy = u32.GetSystemMetrics(76), u32.GetSystemMetrics(77)
        vw, vh = u32.GetSystemMetrics(78), u32.GetSystemMetrics(79)
        ax = int((x - vx) * 65535 / max(vw - 1, 1))
        ay = int((y - vy) * 65535 / max(vh - 1, 1))
        flags |= 0x4000                               # MOUSEEVENTF_VIRTUALDESK
    inp = _INPUT(0, _INPUTUNION(mi=_MOUSEINPUT(ax, ay, 0, flags, 0, None)))
    return u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)) == 1


def _mouse_to(x: int, y: int) -> None:
    """커서를 옮긴다. SetCursorPos 만으로는 앱이 «지금 어느 줄 위인지»를 갱신하지 못해
    이동 이벤트를 같이 보낸다."""
    win32api.SetCursorPos((x, y))
    _send_mouse(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE, x, y)


def _press(down: int, up: int, times: int = 1, gap: float = 0.04) -> None:
    for _ in range(times):
        _send_mouse(down)
        time.sleep(gap)
        _send_mouse(up)
        time.sleep(0.08)


def click(x: int, y: int):
    if QUIET:
        _no_input("화면 좌표 클릭")
    _mouse_to(x, y)
    time.sleep(0.06)
    _press(_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP)
    time.sleep(0.04)


def double_click(x: int, y: int):
    if QUIET:
        _no_input("화면 좌표 더블클릭")
    _mouse_to(x, y)
    time.sleep(0.12)
    _press(_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP, times=2)


def click_hwnd(hwnd):
    if QUIET:
        post_click(hwnd)      # 그 컨트롤에만 가는 메시지 클릭
        return
    r = win32gui.GetWindowRect(hwnd)
    click((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)


def paste_text(text: str, settle: float = 0.15):
    """클립보드로 붙여넣는다. settle 은 붙여넣기가 반영되기를 기다리는 시간.

    입력란처럼 «들어갔는지 읽어서 확인할 수 있는» 곳은 settle 을 짧게 주고
    호출한 쪽에서 wait_control_text 로 확인한다. 본문(Chrome 기반 편집기)처럼
    읽어서 확인할 수 없는 곳만 settle 을 넉넉히 준다.
    """
    if QUIET:
        _no_input("Ctrl+V 붙여넣기")
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    win32clipboard.CloseClipboard()
    time.sleep(0.05)
    win32api.keybd_event(0x11, 0, 0, 0)
    win32api.keybd_event(0x56, 0, 0, 0)
    time.sleep(0.03)
    win32api.keybd_event(0x56, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(settle)


def find_child_by_text(parent_hwnd, text: str):
    results = []
    def cb(hwnd, _):
        if win32gui.GetWindowText(hwnd) == text:
            results.append(hwnd)
    win32gui.EnumChildWindows(parent_hwnd, cb, None)
    return results[0] if results else None


def find_child_by_class(parent_hwnd, class_name: str, visible_only=True):
    results = []
    def cb(hwnd, _):
        if win32gui.GetClassName(hwnd) == class_name:
            if not visible_only or win32gui.IsWindowVisible(hwnd):
                results.append(hwnd)
    win32gui.EnumChildWindows(parent_hwnd, cb, None)
    return results[0] if results else None


def _dialog_gone(hwnd) -> bool:
    """전송창이 닫혔는가. **없어졌거나 숨었으면** 닫힌 것이다.

    ⚠️ 쿨메신저는 전송창을 파괴하지 않고 숨겨 두기도 한다 (2026-09-11 실측: 숨은 메인창을 되살린 뒤에는
    닫기 확인을 눌러도 창이 IsWindow=True 인 채 안 보이게만 됐다). «IsWindow 가 거짓»만 기다리면 영영 안 닫힌 걸로 보고,
    발송 뒤라면 숨은 창의 좌표에 «보내기»를 다시 누르는 사고가 난다.
    """
    return not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd)


def get_send_dialog(pid: int):
    """'메시지 전송' 창. 같은 제목의 껍데기 창이 하나 더 뜰 때가 있어서
    실제로 '보내기' 버튼을 가진 창만 고른다 (2026-08-24: 껍데기를 잡아
    수신자 검증이 메인창 탭 이름을 읽는 사고가 있었다)."""
    cands = []

    def cb(hwnd, _):
        _, p = win32process.GetWindowThreadProcessId(hwnd)
        # 숨겨 둔 옛 전송창은 고르지 않는다 (_dialog_gone 설명 참고)
        if p == pid and win32gui.GetWindowText(hwnd) == '메시지 전송' and win32gui.IsWindowVisible(hwnd):
            cands.append(hwnd)

    win32gui.EnumWindows(cb, None)
    for hwnd in cands:
        if find_child_by_text(hwnd, '보내기') or find_child_by_text(hwnd, '파일첨부'):
            return hwnd
    return cands[0] if cands else None


def _close_confirm_dialog(pid: int):
    """'이 창을 닫으시겠습니까?' 확인창 (내용이 든 전송창을 닫을 때 뜬다)."""
    out = []

    def cb(h, _):
        _, p = win32process.GetWindowThreadProcessId(h)
        if (p == pid and win32gui.IsWindowVisible(h)
                and win32gui.GetClassName(h) == '#32770'
                and '닫으시겠습니까' in win32gui.GetWindowText(h)):
            out.append(h)

    win32gui.EnumWindows(cb, None)
    return out[0] if out else None


def close_send_dialog(hwnd: int, pid: int, timeout: float = 8.0) -> bool:
    """전송창을 닫는다.

    ⚠️ 내용이 들어 있는 전송창은 WM_CLOSE 만으로 안 닫힌다 — '이 창을 닫으시겠습니까?'
    확인창이 뜨고 원래 창은 disabled 상태로 남는다 (2026-09-08 실측).
    예전 코드는 WM_CLOSE 뒤 0.8초만 자고 넘어가서, 확인창이 화면에 뜬 채로 다음 단계가
    진행됐다. 여기서 확인을 눌러 실제로 닫는다.
    """
    # 숨은 메인창을 되살린 직후에는 닫히는 데 4초를 넘긴 적이 있어 8초까지 기다린다 (2026-09-11 실측)
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    end = time.perf_counter() + timeout
    pressed = {}              # 확인창 hwnd → 누름 메시지를 보낸 시각
    while True:
        if _dialog_gone(hwnd):
            return True
        confirm = _close_confirm_dialog(pid)
        if confirm:
            btn = find_child_by_text(confirm, '확인')
            if btn and confirm not in pressed:
                # 마우스 없이 «확인을 눌렀다»는 알림을 확인창에 직접 보낸다.
                # ⚠️ 확인창이 다른 창 뒤에 깔려 있으면 실제 클릭은 엉뚱한 창에 떨어진다 — 2026-09-11 실측:
                # 숨은 메인창을 되살린 뒤에는 확인창이 떠 있는데도 8초 동안 안 닫혔다.
                win32api.PostMessage(confirm, win32con.WM_COMMAND,
                                     win32api.MAKELONG(win32gui.GetDlgCtrlID(btn), win32con.BN_CLICKED), btn)
                pressed[confirm] = time.perf_counter()
            elif btn and time.perf_counter() - pressed[confirm] > 1.0:
                bring_to_front(confirm)       # 메시지가 안 먹은 경우에만 예전처럼 실제 클릭
                click_hwnd(btn)
                pressed[confirm] = time.perf_counter()
        if time.perf_counter() >= end:
            return _dialog_gone(hwnd)
        time.sleep(0.05)


# ── 트리 직접 순회 (BFS) ─────────────────────────────────────────────────────

class _TVITEMW(ctypes.Structure):
    _fields_ = [
        ('mask',           ctypes.c_uint),
        ('hItem',          ctypes.c_void_p),
        ('state',          ctypes.c_uint),
        ('stateMask',      ctypes.c_uint),
        ('pszText',        ctypes.c_void_p),
        ('cchTextMax',     ctypes.c_int),
        ('iImage',         ctypes.c_int),
        ('iSelectedImage', ctypes.c_int),
        ('cChildren',      ctypes.c_int),
        ('lParam',         ctypes.c_ssize_t),
    ]


class _TVITEMW32(ctypes.Structure):
    """32비트 프로세스용 TVITEMW. 포인터가 4바이트라 64비트판과 크기·정렬이 다르다.

    쿨메신저는 32비트 앱이라 64비트 파이썬의 _TVITEMW를 그대로 보내면
    필드가 어긋나 항목 텍스트가 전부 빈 문자열로 돌아온다.
    """
    _fields_ = [
        ('mask',           ctypes.c_uint32),
        ('hItem',          ctypes.c_uint32),
        ('state',          ctypes.c_uint32),
        ('stateMask',      ctypes.c_uint32),
        ('pszText',        ctypes.c_uint32),
        ('cchTextMax',     ctypes.c_int32),
        ('iImage',         ctypes.c_int32),
        ('iSelectedImage', ctypes.c_int32),
        ('cChildren',      ctypes.c_int32),
        ('lParam',         ctypes.c_int32),
    ]


def _target_is_32bit(proc_handle) -> bool:
    """대상 프로세스가 32비트인지 판별."""
    if ctypes.sizeof(ctypes.c_void_p) == 4:
        return True
    flag = wintypes.BOOL()
    if not _k32.IsWow64Process(proc_handle, ctypes.byref(flag)):
        return False
    return bool(flag.value)


class _RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


def _get_tree_item_text(proc_handle, tree_hwnd: int, hitem: int, item_cls=None) -> str:
    if item_cls is None:
        item_cls = _TVITEMW32 if _target_is_32bit(proc_handle) else _TVITEMW
    TEXT_BYTES = 512
    total = TEXT_BYTES + ctypes.sizeof(item_cls)

    remote = _k32.VirtualAllocEx(proc_handle, None, total, MEM_COMMIT, PAGE_READWRITE)
    if not remote:
        return ""
    try:
        remote_text = remote
        remote_item = remote + TEXT_BYTES

        item = item_cls()
        item.mask = TVIF_TEXT | TVIF_HANDLE
        item.hItem = hitem
        item.pszText = remote_text
        item.cchTextMax = TEXT_BYTES // 2

        written = ctypes.c_size_t(0)
        _k32.WriteProcessMemory(proc_handle, remote_item,
                                ctypes.addressof(item), ctypes.sizeof(item),
                                ctypes.byref(written))
        win32gui.SendMessage(tree_hwnd, TVM_GETITEMW, 0, remote_item)

        buf = ctypes.create_string_buffer(TEXT_BYTES)
        read = ctypes.c_size_t(0)
        _k32.ReadProcessMemory(proc_handle, remote_text,
                               ctypes.addressof(buf), TEXT_BYTES,
                               ctypes.byref(read))
        return buf.raw.decode('utf-16-le', errors='ignore').rstrip('\x00')
    finally:
        _k32.VirtualFreeEx(proc_handle, remote, 0, MEM_RELEASE)


def _get_tree_item_screen_pos(proc_handle, tree_hwnd: int, hitem: int):
    remote = _k32.VirtualAllocEx(proc_handle, None, ctypes.sizeof(_RECT),
                                  MEM_COMMIT, PAGE_READWRITE)
    if not remote:
        return None
    try:
        if _target_is_32bit(proc_handle):
            hitem_val = ctypes.c_uint32(hitem & 0xFFFFFFFF)
        else:
            hitem_val = ctypes.c_void_p(hitem)
        written = ctypes.c_size_t(0)
        _k32.WriteProcessMemory(proc_handle, remote,
                                ctypes.addressof(hitem_val), ctypes.sizeof(hitem_val),
                                ctypes.byref(written))
        # ⚠️ wParam 은 **True(글자 칸)** 여야 한다. False 면 «줄 전체» 칸이 와서
        # 가운데 x 가 이름 오른쪽 빈 자리에 떨어진다 — 눌러도 아무 일도 안 일어난다
        # (2026-09-12 실측: 줄 전체 가운데 655 는 헛돌고, 글자 칸 가운데 588 은 바로 잡혔다.
        #  트리 폭이 391~919 라 655 는 정확히 트리 한가운데 = 이름 끝 너머였다).
        if not win32gui.SendMessage(tree_hwnd, TVM_GETITEMRECT, True, remote):
            return None
        rect = _RECT()
        read = ctypes.c_size_t(0)
        _k32.ReadProcessMemory(proc_handle, remote,
                               ctypes.addressof(rect), ctypes.sizeof(rect),
                               ctypes.byref(read))
        pt = _POINT((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        _u32.ClientToScreen(tree_hwnd, ctypes.byref(pt))
        return pt.x, pt.y
    finally:
        _k32.VirtualFreeEx(proc_handle, remote, 0, MEM_RELEASE)


def dump_tree_items(tree_hwnd: int, pid: int) -> list[str]:
    """트리 전체 BFS 순회 후 모든 항목 텍스트 반환 (디버그용)."""
    proc_handle = _k32.OpenProcess(
        PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION, False, pid
    )
    if not proc_handle:
        return []
    texts = []
    item_cls = _TVITEMW32 if _target_is_32bit(proc_handle) else _TVITEMW
    try:
        queue = deque()
        root = win32gui.SendMessage(tree_hwnd, TVM_GETNEXTITEM, TVGN_ROOT, 0)
        if root:
            queue.append(root)
        while queue:
            hitem = queue.popleft()
            text = _get_tree_item_text(proc_handle, tree_hwnd, hitem, item_cls)
            texts.append(text)
            child = win32gui.SendMessage(tree_hwnd, TVM_GETNEXTITEM, TVGN_CHILD, hitem)
            if child:
                queue.append(child)
            sibling = win32gui.SendMessage(tree_hwnd, TVM_GETNEXTITEM, TVGN_NEXT, hitem)
            if sibling:
                queue.append(sibling)
    finally:
        _k32.CloseHandle(proc_handle)
    return texts


def find_and_dblclick_tree_item(tree_hwnd: int, pid: int, name: str) -> bool:
    """트리를 BFS로 순회해 name이 포함된 항목을 찾아 더블클릭."""
    proc_handle = _k32.OpenProcess(
        PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION, False, pid
    )
    if not proc_handle:
        return False
    item_cls = _TVITEMW32 if _target_is_32bit(proc_handle) else _TVITEMW
    try:
        queue = deque()
        root = win32gui.SendMessage(tree_hwnd, TVM_GETNEXTITEM, TVGN_ROOT, 0)
        if root:
            queue.append(root)

        found = None
        while queue:
            hitem = queue.popleft()
            text = _get_tree_item_text(proc_handle, tree_hwnd, hitem, item_cls)
            if name in text:
                found = hitem
                break
            child = win32gui.SendMessage(tree_hwnd, TVM_GETNEXTITEM, TVGN_CHILD, hitem)
            if child:
                queue.append(child)
            sibling = win32gui.SendMessage(tree_hwnd, TVM_GETNEXTITEM, TVGN_NEXT, hitem)
            if sibling:
                queue.append(sibling)

        if not found:
            return False

        win32gui.SendMessage(tree_hwnd, TVM_ENSUREVISIBLE, 0, found)
        time.sleep(0.2)

        pos = _get_tree_item_screen_pos(proc_handle, tree_hwnd, found)
        if not pos:
            return False

        double_click(*pos)
        return True
    finally:
        _k32.CloseHandle(proc_handle)


# 조직도 검색으로 수신자 고르기
#
# 조직도 트리 항목은 커스텀 드로우라 TVM_GETITEMW 로는 이름이 늘 빈 문자열로 온다
# (2026-08-24 실측: SendMessage 는 1을 돌려주는데 버퍼가 전부 0).
# 그래서 "이름으로 트리를 뒤지는" 방식은 쓸 수 없고,
# 조직도 검색창(id 1708)에 이름을 넣어 트리를 거른 뒤 남은 잎(leaf) 행을 더블클릭한다.
# 실제로 누구에게 잡혔는지는 전송창의 "받는사람" 칩 텍스트로 검증한다 (이건 읽힌다).

SEARCH_EDIT_ID = 1708
TVGN_CARET = 0x0009
_last_clear = [-1e9]      # 마지막으로 검색을 초기화한 시각 (perf_counter) — search_org_tree 가 본다


def _find_child_by_id(parent_hwnd, ctrl_id: int, class_name: str = ""):
    found = None

    def cb(hwnd, _):
        nonlocal found
        if found or not win32gui.IsWindowVisible(hwnd):
            return
        if win32gui.GetDlgCtrlID(hwnd) == ctrl_id:
            if not class_name or win32gui.GetClassName(hwnd) == class_name:
                found = hwnd

    win32gui.EnumChildWindows(parent_hwnd, cb, None)
    return found


def _tree_leaf_items(tree_hwnd: int) -> list:
    """트리를 BFS로 훑어 자식이 없는 항목(=사람 행)만 모은다."""
    leaves = []
    root = win32gui.SendMessage(tree_hwnd, TVM_GETNEXTITEM, TVGN_ROOT, 0)
    if not root:
        return leaves
    queue = deque([root])
    while queue:
        hitem = queue.popleft()
        child = win32gui.SendMessage(tree_hwnd, TVM_GETNEXTITEM, TVGN_CHILD, hitem)
        if child:
            queue.append(child)
        else:
            leaves.append(hitem)
        sibling = win32gui.SendMessage(tree_hwnd, TVM_GETNEXTITEM, TVGN_NEXT, hitem)
        if sibling:
            queue.append(sibling)
    return leaves


def _tree_settled(tree_hwnd: int, timeout: float = 0.8, rounds: int = 1) -> list:
    """트리 항목 구성이 rounds 번 연속 같게 읽힐 때까지 기다린다 (그리기가 끝난 시점)."""
    end = time.perf_counter() + timeout
    last, same = None, 0
    while True:
        cur = _tree_leaf_items(tree_hwnd)
        same = same + 1 if (cur and cur == last) else 0
        if same >= rounds:
            return cur
        last = cur
        if time.perf_counter() >= end:
            return cur
        time.sleep(0.06)


def _search_by_message(edit: int, name: str) -> bool:
    """검색창에 이름을 WM_SETTEXT 로 넣고 Enter 를 **그 입력란에만** 보낸다.

    마우스 클릭도 전역 키 입력(keybd_event)도 쓰지 않아서, 사용자가 마우스를 움직이거나
    다른 창이 앞에 있어도 영향을 받지 않는다. 2026-09-11 실측: 트리가 0.8초 만에 걸러졌다.
    """
    win32gui.SendMessage(edit, win32con.WM_SETTEXT, 0, name)
    if read_control_text(edit) != name:
        return False
    win32api.PostMessage(edit, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0x001C0001)
    win32api.PostMessage(edit, win32con.WM_KEYUP, win32con.VK_RETURN, 0xC01C0001)
    return True


def search_org_tree(main_hwnd: int, name: str, tree_hwnd: int = 0) -> None:
    """조직도 검색창에 이름을 넣어 트리를 거른다.

    ① 먼저 메시지로 넣는다(_search_by_message). 트리가 실제로 바뀌면 끝.
    ② 안 바뀌면 예전 방식(검색창 클릭 → Ctrl+V → Enter)으로 다시 한다.
    어느 쪽이든 실제 신호 둘을 본다 —
      ① 검색창에 이름이 다 들어갔는가 (반쪽짜리 글자로 Enter 를 치면 엉뚱한 사람이 걸린다)
      ② Enter 뒤 트리 구성이 실제로 바뀌고 멈췄는가
    입력란을 못 읽는 환경이면 예전과 같은 고정 대기로 되돌아간다.
    """
    edit = _find_child_by_id(main_hwnd, SEARCH_EDIT_ID, 'Edit')
    if not edit:
        raise RuntimeError("조직도 검색창(id 1708)을 찾을 수 없습니다.")
    before = _tree_leaf_items(tree_hwnd) if tree_hwnd else None
    # 검색창에 **같은 이름이 이미 들어 있으면** 트리가 이미 그 이름으로 걸러져 있다.
    # 이때는 Enter 를 쳐도 트리가 안 바뀌므로 «바뀔 때까지» 기다리면 5초를 통째로 버린다
    # (2026-09-08 실측: 이것 때문에 두 번째 발송부터 매번 5.5초씩 더 걸렸다).
    query_now = read_control_text(edit).strip()
    same_query = query_now == name
    if before is not None and same_query:
        _tree_settled(tree_hwnd)
        return
    if before is not None and time.perf_counter() - _last_clear[0] < 3.0:
        # «거르기 전» 트리가 아직 바뀌는 중이면(직전 발송이 검색을 초기화한 직후) 그 변화를 검색 결과로 착각한다.
        # 2026-09-11 실측: 연달아 작성할 때 이 착각으로 걸러지지 않은 트리의 첫 사람(교장)이 두 번 잡혔다 — 칩 검증이 막았다.
        # 초기화한 지 3초가 지났으면 이미 다 돌아와 있으므로 기다리지 않는다.
        before = _tree_settled(tree_hwnd, rounds=2)
    if before is not None and _search_by_message(edit, name):
        # 검색은 서버를 한 번 다녀오는 듯하다 — 실측 0.6~0.9초, 앱이 식어 있으면 2초를 넘었다.
        if _wait_for(lambda: _tree_leaf_items(tree_hwnd) != before, 5.0, 0.03):
            after = _tree_settled(tree_hwnd, rounds=2)
            # 전체 트리에서 걸렀는데 사람 수가 줄지 않았다면 검색 결과가 아니라 다른 변화였다 → 아래 방식으로 다시
            if query_now or len(after) < len(before):
                return

    require_front(main_hwnd, "쿨메신저 메인")
    r = win32gui.GetWindowRect(edit)
    click((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
    time.sleep(0.08)
    win32api.keybd_event(0x11, 0, 0, 0)
    win32api.keybd_event(0x41, 0, 0, 0)
    win32api.keybd_event(0x41, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)
    paste_text(name, settle=0.05)
    if not wait_control_text(edit, name, 1.2):
        time.sleep(0.5)          # 못 읽는 경우 — 예전과 같은 고정 대기
    win32api.keybd_event(0x0D, 0, 0, 0)
    win32api.keybd_event(0x0D, 0, win32con.KEYEVENTF_KEYUP, 0)
    if before is None:
        time.sleep(1.5)
        return
    # 넉넉히 5초까지 기다리되, 트리가 바뀌는 즉시 넘어간다.
    # (여기서 못 기다리고 넘어가면 «거르기 전» 트리의 첫 사람을 눌러 엉뚱한 수신자가 잡힌다)
    _wait_for(lambda: _tree_leaf_items(tree_hwnd) != before, 5.0, 0.03)
    _tree_settled(tree_hwnd)


def clear_org_search(main_hwnd: int, wait: bool = False) -> None:
    """검색창을 비워 조직도를 원래 상태로 되돌린다.

    wait=True 면 트리가 원래대로 다 돌아올 때까지 기다린다 — 곧바로 다시 검색할 때 쓴다
    (덜 돌아온 변화를 검색 결과로 착각하지 않게). 발송 끝의 정리는 기다리지 않는다.
    """
    reset = _find_child_by_id(main_hwnd, 3278)
    if reset:
        tree = find_child_by_class(main_hwnd, 'SysTreeView32') if wait else None
        edit = _find_child_by_id(main_hwnd, SEARCH_EDIT_ID, 'Edit') if wait else None
        before = _tree_leaf_items(tree) if (tree and edit and read_control_text(edit).strip()) else None
        # ⚠️ **마우스로 누르면 안 된다.** 이 버튼은 메인창에 있는데 그 좌표가
        # 전송창에 가려져 있을 때가 있다 (실측: 버튼 중심 (870,253)이 전송창 영역 안이고,
        # 그 자리는 전송창의 «받는사람» 줄이다). 그래서 마우스 클릭이 받는사람 칩을 건드려
        # **수신자가 통째로 지워진 채 전송창만 남는 사고**가 났다 (2026-09-08).
        # BM_CLICK 은 좌표와 무관하게 그 버튼에만 간다.
        win32api.SendMessage(reset, win32con.BM_CLICK, 0, 0)
        _last_clear[0] = time.perf_counter()
        if before is not None:
            _wait_for(lambda: _tree_leaf_items(tree) != before, 2.0, 0.03)
            _tree_settled(tree, timeout=1.5, rounds=2)
        else:
            time.sleep(0.1)


def _item_pos_settled(proc_handle, tree_hwnd: int, hitem: int, timeout: float = 0.5):
    """스크롤이 끝나 항목 좌표가 두 번 연속 같아졌을 때의 화면 좌표 (예전에는 무조건 0.15초를 잤다)."""
    last = None
    end = time.perf_counter() + timeout
    while True:
        pos = _get_tree_item_screen_pos(proc_handle, tree_hwnd, hitem)
        if pos and pos == last:
            return pos
        if time.perf_counter() >= end:
            return pos
        last = pos
        time.sleep(0.04)


def _post_dblclick(hwnd: int, screen_pos) -> None:
    """실제 커서를 옮기지 않고 그 컨트롤에 더블클릭 메시지만 보낸다.

    2026-09-11 실측: 조직도 트리에 보냈더니 0.16초 만에 전송창이 열렸고 커서는 그대로였다.
    사용자가 마우스를 움직이거나 광고 창이 그 자리를 덮고 있어도 트리에만 간다.
    순서는 실제 더블클릭과 같다: DOWN → UP → DBLCLK → UP.
    """
    cx, cy = win32gui.ScreenToClient(hwnd, screen_pos)
    lp = win32api.MAKELONG(cx & 0xFFFF, cy & 0xFFFF)
    for msg, wp in ((win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON),
                    (win32con.WM_LBUTTONUP, 0),
                    (win32con.WM_LBUTTONDBLCLK, win32con.MK_LBUTTON),
                    (win32con.WM_LBUTTONUP, 0)):
        win32api.PostMessage(hwnd, msg, wp, lp)


def open_send_dialog_for(main_hwnd: int, tree_hwnd: int, pid: int, name: str) -> int:
    """이름으로 조직도를 거른 뒤 첫 사람 행을 더블클릭해 메시지 전송 창을 연다.

    더블클릭은 먼저 메시지로 보내고(_post_dblclick), 안 열렸을 때만 예전처럼 실제 마우스로 친다.
    실제 마우스로 칠 때는 **누르기 직전에 그 좌표가 정말 트리인지** 확인한다 — 다른 창이 덮고 있으면
    누르지 않고 멈춘다. 누구에게 잡혔는지는 호출한 쪽이 받는사람 칩으로 검증한다.
    """
    search_org_tree(main_hwnd, name, tree_hwnd)

    proc_handle = _k32.OpenProcess(
        PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION, False, pid
    )
    if not proc_handle:
        raise RuntimeError("쿨메신저 프로세스를 열 수 없습니다.")
    try:
        leaves = _tree_leaf_items(tree_hwnd)
        if not leaves:
            raise RuntimeError("'%s' 검색 결과가 조직도에 없습니다." % name)
        target, pos = None, None
        for hitem in leaves:
            win32gui.SendMessage(tree_hwnd, TVM_ENSUREVISIBLE, 0, hitem)
            pos = _item_pos_settled(proc_handle, tree_hwnd, hitem)   # 스크롤이 끝난 뒤의 좌표
            if pos:
                target = hitem
                break
        if not pos:
            raise RuntimeError("조직도 항목의 화면 위치를 구하지 못했습니다.")

        # 메인창을 먼저 앞으로 꺼내 둔다 — 뒤에 있는 창에서 열린 전송창은 한동안 앞으로 안 나와서
        # 키 입력 직전에 1~2.5초를 기다렸다 (2026-09-11 실측). 꺼내기가 실패해도 메시지 더블클릭은 그대로 된다.
        bring_to_front(main_hwnd)
        _post_dblclick(tree_hwnd, pos)
        dlg = _wait_for(lambda: get_send_dialog(pid), 3.0, 0.05)
        if dlg:
            return dlg
        if QUIET:
            # 진짜 마우스로 넘어가지 않고 메시지 더블클릭만 몇 번 더 (2026-11-12: 가끔 첫 번이 안 먹었다)
            for _ in range(3):
                win32gui.SendMessage(tree_hwnd, TVM_ENSUREVISIBLE, 0, target)
                pos = _item_pos_settled(proc_handle, tree_hwnd, target) or pos
                _post_dblclick(tree_hwnd, pos)
                dlg = _wait_for(lambda: get_send_dialog(pid), 3.0, 0.05)
                if dlg:
                    return dlg

        # 메시지가 안 먹었을 때만 실제 마우스로 (예전 방식).
        # 더블클릭이 «그 줄에» 맞았는지 트리 선택(caret)으로 확인하고, 빗나갔으면 한 번 더 친다.
        # (좌표를 읽은 뒤 트리가 조금이라도 움직이면 빈 곳을 눌러 전송창이 안 열린다)
        for attempt in range(2):
            require_front(main_hwnd, "쿨메신저 메인")
            pos = _get_tree_item_screen_pos(proc_handle, tree_hwnd, target) or pos
            cover = win32gui.WindowFromPoint(pos)
            if cover != tree_hwnd:
                raise RuntimeError("조직도의 '%s' 자리를 다른 창(%r)이 덮고 있어 누르지 않았습니다. "
                                   "그 창을 닫고 다시 시도해주세요." % (name, win32gui.GetWindowText(cover)))
            double_click(*pos)
            dlg = _wait_for(lambda: get_send_dialog(pid), 4.0 if attempt == 0 else 8.0, 0.05)
            if dlg:
                return dlg
            caret = win32gui.SendMessage(tree_hwnd, TVM_GETNEXTITEM, TVGN_CARET, 0)
            if caret == target:
                break                 # 줄은 제대로 눌렸는데 창이 안 뜬 것 — 다시 쳐도 같다
    finally:
        _k32.CloseHandle(proc_handle)

    raise RuntimeError("메시지 전송 창이 열리지 않았습니다.")


# ── 전 교직원 발송 (조직도 루트 체크 → 우클릭 「메시지 보내기」) ─────────────────
#
# 조직도 «검색»으로는 전체를 못 잡는다 — 검색 결과가 전부 Empty Name 으로 떠서
# 더블클릭이 안 먹기 때문이다 (바로 위 「조직도 검색으로 수신자 고르기」 설명 참고).
# 전체 발송은 **루트(학교 이름) 체크박스를 켜고 그 줄에서 우클릭 → 「메시지 보내기(M)」**
# 로만 된다. 2026-09-09에 이 경로로 73명에게 실제로 보냈다(연수 안내).
#
# ⚠️ 좌표를 상수로 박지 않는다. 창을 옮기면 바로 깨진다.
# 루트 행의 «텍스트 사각형»을 TVM_GETITEMRECT(wParam=True)로 읽어 거기서 계산한다.
# 실측(2026-09-09): 텍스트 사각형 client (66,0,135,22), 체크박스를 눌러 먹혔던 자리는
# client x=33 → **체크박스 중심 = 텍스트 왼쪽 - 33px**.

CHECKBOX_DX = 33                       # 텍스트 왼쪽 → 체크박스 중심 (실측)
ALL_STAFF_WORDS = ("전체", "전교직원", "전직원", "모두", "전체메시지", "전체발송")


def _is_all_staff(recipients) -> bool:
    """받는사람이 «전체» 한 단어면 전 교직원 발송으로 본다."""
    return (len(recipients) == 1
            and recipients[0].replace(" ", "") in ALL_STAFF_WORDS)


def _checkbox_is_checked(x: int, y: int) -> bool:
    """조직도 체크박스가 켜졌는지 **화면 픽셀**로 본다.

    ⚠️ TVM_GETITEMSTATE 로는 못 읽는다 — 이 트리에서 항상 0x0 을 돌려준다
    (2026-09-09 실측: 눈으로는 분명히 체크돼 있는데 state=0x0. 항목 «텍스트»가
    커스텀 드로우라 안 읽히는 것과 같은 이유다).
    그래서 체크박스 자리를 13×13 으로 훑어 어두운 픽셀 수로 판별한다.
    실측: 체크됨 = 어두운 픽셀 22개(min 63) / 꺼짐 = 0개(min 201). 경계는 5개로 잡았다.
    """
    hdc = win32gui.GetDC(0)
    try:
        dark = 0
        for yy in range(y - 6, y + 7):
            for xx in range(x - 6, x + 7):
                c = win32gui.GetPixel(hdc, xx, yy)
                r, g, b = c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF
                if (r * 299 + g * 587 + b * 114) // 1000 < 140:
                    dark += 1
        return dark >= 5
    except Exception:
        return False
    finally:
        win32gui.ReleaseDC(0, hdc)


def _tree_item_text_rect(proc_handle, tree_hwnd: int, hitem: int):
    """항목 «텍스트»의 화면 사각형. 실패하면 None."""
    remote = _k32.VirtualAllocEx(proc_handle, None, ctypes.sizeof(_RECT),
                                 MEM_COMMIT, PAGE_READWRITE)
    if not remote:
        return None
    try:
        if _target_is_32bit(proc_handle):
            hv = ctypes.c_uint32(hitem & 0xFFFFFFFF)
        else:
            hv = ctypes.c_void_p(hitem)
        written = ctypes.c_size_t(0)
        _k32.WriteProcessMemory(proc_handle, remote, ctypes.addressof(hv),
                                ctypes.sizeof(hv), ctypes.byref(written))
        if not win32gui.SendMessage(tree_hwnd, TVM_GETITEMRECT, True, remote):
            return None
        r = _RECT()
        read = ctypes.c_size_t(0)
        _k32.ReadProcessMemory(proc_handle, remote, ctypes.addressof(r),
                               ctypes.sizeof(r), ctypes.byref(read))
        org = _POINT(0, 0)
        _u32.ClientToScreen(tree_hwnd, ctypes.byref(org))
        return (org.x + r.left, org.y + r.top, org.x + r.right, org.y + r.bottom)
    finally:
        _k32.VirtualFreeEx(proc_handle, remote, 0, MEM_RELEASE)


def _right_click(x: int, y: int) -> None:
    if QUIET:
        _no_input("전 교직원 발송(조직도 우클릭)")
    _mouse_to(x, y)
    time.sleep(0.08)
    _press(_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP)


def _visible_context_menu():
    """조직도 우클릭 메뉴. 진짜 윈도우 메뉴(#32768)가 **아니다** — 앱이 직접 그린
    캡션 'CMenuEx' 짜리 #32770 대화상자다 (2026-09-09 실측). 그 덕에 항목이
    자식 컨트롤로 잡히고 텍스트도 그대로 읽힌다.
    """
    out = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == 'CMenuEx':
            out.append(hwnd)

    win32gui.EnumWindows(cb, None)
    return out[0] if out else None


def _open_org_context_menu(x: int, y: int):
    """(x, y)에서 우클릭해 조직도 메뉴를 연다. 못 열면 None.

    ⚠️ **메뉴가 이미 떠 있으면 우클릭은 그걸 «닫는다».** 그래서 최대 3번 되풀이한다
    (실측: 1번째가 남아 있던 메뉴를 닫고 2번째에 새로 열렸다. 메뉴 창은 열 때마다
    새로 만들어져서 핸들도 매번 바뀐다).
    ⚠️ 남은 메뉴를 **ESC 로 닫으면 안 된다** — 쿨메신저 창이 통째로 숨어버린다
    (2026-09-09에 실제로 사라져서 ShowWindow 로 되살렸다).
    """
    for _ in range(3):
        _right_click(x, y)
        menu = _wait_for(_visible_context_menu, 2.0, 0.05)
        if menu:
            return menu
        time.sleep(0.4)
    return None


def _click_menu_item(menu_hwnd: int, label: str) -> bool:
    """메뉴 항목을 **텍스트로** 찾아 누른다 (좌표로 찍지 않으므로 메뉴가 바뀌어도 안 깨진다)."""
    hit = []

    def cb(hwnd, _):
        if label in win32gui.GetWindowText(hwnd):
            hit.append(hwnd)

    win32gui.EnumChildWindows(menu_hwnd, cb, None)
    if not hit:
        return False
    if QUIET:
        post_click(hit[0])
        return True
    l, t, r, b = win32gui.GetWindowRect(hit[0])
    click((l + r) // 2, (t + b) // 2)
    return True


def _click_select_all_confirm(pid: int) -> bool:
    """「모두 선택되었습니다. 계속 진행하시겠습니까?」 알림의 «확인»을 누른다."""
    def find():
        out = []

        def cb(hwnd, _):
            _, p = win32process.GetWindowThreadProcessId(hwnd)
            if (p == pid and win32gui.IsWindowVisible(hwnd)
                    and win32gui.GetClassName(hwnd) == '#32770'
                    and '계속 진행' in win32gui.GetWindowText(hwnd)):
                out.append(hwnd)

        win32gui.EnumWindows(cb, None)
        return out[0] if out else None

    dlg = _wait_for(find, 3.0, 0.05)
    if not dlg:
        return False
    btn = find_child_by_text(dlg, '확인')
    if not btn:
        return False
    click_hwnd(btn)
    _wait_for(lambda: not win32gui.IsWindow(dlg), 2.0, 0.05)
    return True


def _quiet_checked(tree_hwnd: int, x: int, y: int) -> bool:
    """조직도 체크박스 켜짐 여부 — 화면 대신 트리 창 자체를 PrintWindow 로 찍어 본다 (가려져 있어도 된다)."""
    im = _print_window(tree_hwnd)
    if im is None:
        return False
    cx, cy = win32gui.ScreenToClient(tree_hwnd, (x, y))
    px = im.load()
    dark = 0
    for yy in range(cy - 6, cy + 7):
        for xx in range(cx - 6, cx + 7):
            if 0 <= xx < im.size[0] and 0 <= yy < im.size[1]:
                r, g, b = px[xx, yy]
                if (r * 299 + g * 587 + b * 114) // 1000 < 140:
                    dark += 1
    return dark >= 5


def _quiet_tree_click(tree_hwnd: int, x: int, y: int, right: bool = False) -> None:
    """조직도에 메시지 클릭(화면 좌표 x,y → 트리 클라이언트 좌표). 진짜 커서는 안 움직인다."""
    cx, cy = win32gui.ScreenToClient(tree_hwnd, (x, y))
    if not right:
        post_click(tree_hwnd, cx, cy)
        return
    win32gui.PostMessage(tree_hwnd, win32con.WM_MOUSEMOVE, 0, _lp(cx, cy))
    win32gui.PostMessage(tree_hwnd, win32con.WM_RBUTTONDOWN, win32con.MK_RBUTTON, _lp(cx, cy))
    time.sleep(0.05)
    win32gui.PostMessage(tree_hwnd, win32con.WM_RBUTTONUP, 0, _lp(cx, cy))


def open_send_dialog_for_all(main_hwnd: int, tree_hwnd: int, pid: int) -> int:
    """조직도 루트를 통째로 체크해 «전 교직원» 전송창을 연다."""
    clear_org_search(main_hwnd)
    require_front(main_hwnd, "쿨메신저 메인")

    proc_handle = _k32.OpenProcess(
        PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION, False, pid)
    if not proc_handle:
        raise RuntimeError("쿨메신저 프로세스를 열 수 없습니다.")
    try:
        root = win32gui.SendMessage(tree_hwnd, TVM_GETNEXTITEM, TVGN_ROOT, 0)
        if not root:
            raise RuntimeError("조직도 루트를 찾을 수 없습니다.")
        win32gui.SendMessage(tree_hwnd, TVM_ENSUREVISIBLE, 0, root)
        rect = _tree_item_text_rect(proc_handle, tree_hwnd, root)
        if not rect:
            raise RuntimeError("조직도 루트의 화면 위치를 구하지 못했습니다.")
        left, top, right, bottom = rect
        cy = (top + bottom) // 2

        cbx = left - CHECKBOX_DX
        # 체크 여부를 «화면 픽셀»로 보고 실제 마우스로 누르므로, 그 자리를 다른 창이 덮고 있으면 둘 다 틀린다
        cover = tree_hwnd if QUIET else win32gui.WindowFromPoint((cbx, cy))
        if cover != tree_hwnd:
            raise RuntimeError("조직도 루트 자리를 다른 창(%r)이 덮고 있어 누르지 않았습니다. "
                               "그 창을 닫고 다시 시도해주세요." % win32gui.GetWindowText(cover))
        is_on = (lambda: _quiet_checked(tree_hwnd, cbx, cy)) if QUIET else (lambda: _checkbox_is_checked(cbx, cy))
        if not is_on():
            if QUIET:
                _quiet_tree_click(tree_hwnd, cbx, cy)
            else:
                click(cbx, cy)
            if not _wait_for(is_on, 2.0, 0.05):
                raise RuntimeError(
                    "조직도 루트 체크박스가 켜지지 않았습니다 — 전 교직원이 선택되지 않아 중단합니다.")

        if QUIET:
            menu = None
            for _ in range(3):
                _quiet_tree_click(tree_hwnd, (left + right) // 2, cy, right=True)
                menu = _wait_for(_visible_context_menu, 2.0, 0.05)
                if menu:
                    break
        else:
            menu = _open_org_context_menu((left + right) // 2, cy)
        if not menu:
            raise RuntimeError("조직도 우클릭 메뉴가 열리지 않았습니다.")
        if not _click_menu_item(menu, '메시지 보내기'):
            raise RuntimeError("우클릭 메뉴에서 「메시지 보내기」를 찾지 못했습니다.")

        # 「모두 선택되었습니다」 알림 — 안 뜨는 경우도 있으므로 실패는 그냥 넘어간다
        _click_select_all_confirm(pid)

        dlg = _wait_for(lambda: get_send_dialog(pid), 8.0, 0.05)
        if not dlg:
            raise RuntimeError("메시지 전송 창이 열리지 않았습니다.")
        return dlg
    finally:
        _k32.CloseHandle(proc_handle)


def uncheck_org_root(tree_hwnd: int, pid: int) -> None:
    """발송 뒤 조직도 전체 체크를 되돌린다 (다음 사람 보낼 때 딸려가지 않게)."""
    proc_handle = _k32.OpenProcess(
        PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION, False, pid)
    if not proc_handle:
        return
    try:
        root = win32gui.SendMessage(tree_hwnd, TVM_GETNEXTITEM, TVGN_ROOT, 0)
        if not root:
            return
        rect = _tree_item_text_rect(proc_handle, tree_hwnd, root)
        if not rect:
            return
        left, top, _r, bottom = rect
        cbx, cy = left - CHECKBOX_DX, (top + bottom) // 2
        is_on = (lambda: _quiet_checked(tree_hwnd, cbx, cy)) if QUIET else (lambda: _checkbox_is_checked(cbx, cy))
        if not is_on():
            return
        if QUIET:
            _quiet_tree_click(tree_hwnd, cbx, cy)
        else:
            click(cbx, cy)
        _wait_for(lambda: not is_on(), 1.5, 0.05)
    finally:
        _k32.CloseHandle(proc_handle)


def get_dialog_recipients(send_hwnd: int) -> list:
    """전송창 받는사람에 실제로 들어간 사람 칩의 텍스트."""
    names = []

    def cb(child, _):
        if win32gui.IsWindowVisible(child) and win32gui.GetClassName(child).startswith('Afx:'):
            text = win32gui.GetWindowText(child)
            if text and text not in names:
                names.append(text)

    # EnumChildWindows 는 손자 창까지 다 돈다 — 콜백 안에서 다시 재귀하면 같은 창을 여러 번 본다
    win32gui.EnumChildWindows(send_hwnd, cb, None)
    return names


# 받는사람 / 참조
#
# ⚠️ 두 입력란이 **같은 컨트롤 id(8009)** 를 쓴다 (2026-09-03 실측).
# id로는 구분이 안 되고 화면 위치(top)로만 갈린다 — 위가 받는사람, 아래가 참조.
# 참조 행은 '참    조' 버튼(id 1657)을 누르기 전까지 숨어 있어서(IsWindowVisible=0)
# 목록에 아예 안 나온다. 그래서 "받는사람 먼저 다 넣고 → 그다음 참조를 연다" 순서를 지켜야
# 받는사람 칸을 잘못 집지 않는다.

TO_BUTTON_ID = 1656      # '받는사람'
CC_BUTTON_ID = 1657      # '참    조'
RCPT_EDIT_ID = 8009      # 받는사람·참조 입력란 (RichEdit20W)


def _recipient_edits(send_hwnd: int) -> list:
    """보이는 수신자 입력란을 위→아래로 정렬해 돌려준다. [0]=받는사람, [1]=참조."""
    seen = {}

    def cb(c, _):
        if (win32gui.GetDlgCtrlID(c) == RCPT_EDIT_ID
                and win32gui.GetClassName(c) == 'RichEdit20W'
                and win32gui.IsWindowVisible(c)):
            seen[c] = win32gui.GetWindowRect(c)

    win32gui.EnumChildWindows(send_hwnd, cb, None)   # 손자 창까지 돈다 — 재귀 불필요
    return [hwnd for hwnd, _ in sorted(seen.items(), key=lambda kv: kv[1][1])]


def open_cc_row(send_hwnd: int) -> int:
    """'참    조' 버튼을 눌러 참조 입력란을 꺼내고 그 hwnd를 준다.

    ⚠️ 전송창이 막 열린 직후의 **첫 클릭은 창을 활성화하는 데 먹히고 버튼에 안 닿을 때가 있다**
    (2026-09-08 실측: 같은 버튼을 손으로 누르면 참조 줄이 즉시 나오는데,
    작성 흐름 안에서는 «참조 입력란이 나타나지 않았습니다»로 실패했다).
    그래서 창을 앞으로 꺼낸 뒤 눌러 보고, 안 나오면 다시 누른다.
    """
    if len(_recipient_edits(send_hwnd)) >= 2:
        return _recipient_edits(send_hwnd)[-1]

    btn = _find_child_by_id(send_hwnd, CC_BUTTON_ID)
    if not btn:
        raise RuntimeError("참조 버튼(id %d)을 찾을 수 없습니다." % CC_BUTTON_ID)
    for _ in range(3):
        bring_to_front(send_hwnd)
        time.sleep(0.15)
        click_hwnd(btn)
        if _wait_for(lambda: len(_recipient_edits(send_hwnd)) >= 2, 1.2, 0.05):
            break
    edits = _recipient_edits(send_hwnd)
    if len(edits) < 2:
        raise RuntimeError("참조 입력란이 나타나지 않았습니다.")
    return edits[-1]


def _chip_count(send_hwnd: int) -> int:
    chips = get_dialog_chips(send_hwnd)
    return len(chips["to"]) + len(chips["cc"])


def _popup_state(pid: int):
    """자동완성 드롭다운의 «현재 모습». 이게 바뀌면 목록이 떴다는 뜻이다.

    드롭다운은 tooltips_class32 + SysShadow 짝으로 뜨고(2026-09-08 실측),
    다음 이름을 칠 때는 **같은 창을 위치만 옮겨** 재사용하기도 한다.
    그래서 hwnd 목록이 아니라 (hwnd, 위치) 묶음으로 비교한다.
    """
    out = set()

    def cb(w, _):
        _, p = win32process.GetWindowThreadProcessId(w)
        if (p == pid and win32gui.IsWindowVisible(w)
                and win32gui.GetClassName(w) in ('tooltips_class32', 'SysShadow')):
            out.add((w, win32gui.GetWindowRect(w)))

    win32gui.EnumWindows(cb, None)
    return frozenset(out)


def focus_control(hwnd) -> bool:
    """다른 프로세스의 컨트롤에 키보드 포커스를 준다 (마우스 클릭 대신).

    ⚠️ **수신자 입력란은 마우스로 클릭하면 안 된다** (2026-09-08 실측).
    이름 칩이 하나라도 들어가면 칩 창(Afx:…)이 입력란 위를 덮어서,
    입력란 한가운데를 눌러도 칩이 눌리고 포커스가 그리로 간다 —
    그래서 Ctrl+V 가 허공으로 가고 **두 번째 참조·수신자가 조용히 빠졌다.**
    AttachThreadInput + SetFocus 는 좌표와 무관해서 이 문제가 없고 더 빠르다.
    """
    u = ctypes.windll.user32
    tid = u.GetWindowThreadProcessId(hwnd, None)
    cur = ctypes.windll.kernel32.GetCurrentThreadId()
    u.AttachThreadInput(cur, tid, True)
    u.SetFocus(hwnd)
    focused = u.GetFocus()
    u.AttachThreadInput(cur, tid, False)
    return focused == hwnd


def _put_name_in_edit(edit_hwnd: int, name: str) -> bool:
    """입력란을 비우고 이름을 붙여넣는다. 실제로 들어갔으면 True.

    ⚠️ 방금 만들어진 입력란(전송창 직후·참조 줄을 막 연 직후)은 **첫 붙여넣기를 먹는다**
    (2026-09-08 실측: 포커스는 정상인데 Ctrl+V 뒤 입력란이 빈 채였고, 그대로 Enter 를 쳐서
    수신자가 조용히 빠졌다). 그래서 넣은 뒤 반드시 읽어서 확인한다.
    """
    if QUIET:
        # 입력란 비우기 + 이름을 그 창에 WM_CHAR 로 (2026-11-12 실측: 자동완성까지 정상으로 뜬다)
        win32gui.SendMessage(edit_hwnd, win32con.WM_SETTEXT, 0, "")
        post_chars(edit_hwnd, name)
        return wait_control_text(edit_hwnd, name, 1.0)
    if not focus_control(edit_hwnd):
        click_hwnd(edit_hwnd)
        time.sleep(0.2)
    win32api.keybd_event(0x11, 0, 0, 0)
    win32api.keybd_event(0x41, 0, 0, 0)
    win32api.keybd_event(0x41, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.03)
    paste_text(name, settle=0.05)
    return wait_control_text(edit_hwnd, name, 0.8)


def type_names_into(edit_hwnd: int, names: list, send_hwnd: int = 0) -> None:
    """입력란에 이름을 하나씩 넣고 자동완성 첫 항목을 고른다 (↓ 그리고 Enter).

    사람당 1.9초를 무조건 자던 것을 **실제 신호 두 개**로 바꿨다 —
      ① 자동완성 목록이 떴는가 (실측 0.15초쯤. 예전에는 0.8초를 무조건 기다렸다)
      ② 받는사람 칩이 실제로 생겼는가
    ②가 안 되면 이름을 처음부터 다시 넣어 본다.
    **Enter 는 입력란에 이름이 남아 있는 것을 확인한 뒤에만 친다** —
    빈 칸에서 Enter 를 치면 기본 버튼(보내기)이 눌릴 수 있기 때문이다.
    """
    _, pid = win32process.GetWindowThreadProcessId(edit_hwnd)
    for name in names:
        for attempt in range(3):
            if send_hwnd:
                require_front(send_hwnd, "메시지 전송")   # ↓·Enter 는 앞에 있는 창으로 간다
            base = _popup_state(pid)
            typed = _put_name_in_edit(edit_hwnd, name)
            if not typed and attempt < 2 and read_control_text(edit_hwnd).strip() == "":
                continue          # 붙여넣기가 안 먹었다 — Enter 치지 말고 다시 넣는다
            before = _chip_count(send_hwnd) if send_hwnd else None
            # 목록이 뜨는 데 실측 0.15초쯤 걸린다. 못 잡아도(같은 자리에 그대로 떠 있는 경우 등)
            # 아래 «칩이 생겼나» 확인이 받쳐 주므로 오래 붙들지 않는다.
            got_popup = _wait_for(lambda: _popup_state(pid) != base, 0.7, 0.03)
            time.sleep(0.12 if got_popup else 0.35)
            if typed and name not in read_control_text(edit_hwnd):
                continue          # 글자가 사라졌으면 Enter 치지 말고 다시 넣는다
            if QUIET:
                post_key(edit_hwnd, 0x28, 0x50)      # ↓ (그 입력란에만)
                time.sleep(0.1)
                post_key(edit_hwnd, 0x0D, 0x1C)      # Enter
            else:
                win32api.keybd_event(0x28, 0, 0, 0)
                win32api.keybd_event(0x28, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.06)
                win32api.keybd_event(0x0D, 0, 0, 0)
                win32api.keybd_event(0x0D, 0, win32con.KEYEVENTF_KEYUP, 0)
            if before is None:
                time.sleep(0.35)
                break
            if _wait_for(lambda: _chip_count(send_hwnd) > before, 1.5, 0.05):
                break


import re as _re

_ATTACH_CHIP = _re.compile(r"\(\s*[\d.,]+\s*(B|KB|MB|GB)\s*\)\s*$", _re.I)


def _is_attachment_chip(text: str) -> bool:
    """첨부파일 칩(`점검표.hwp (38 KB)`)도 사람 칩과 같은 Afx: 클래스라 섞여 읽힌다.
    그걸 «요청하지 않은 사람»으로 세서 발송을 막은 적이 있다 (2026-09-17 · 2026-09-30 실측)."""
    return bool(_ATTACH_CHIP.search(text))


def get_dialog_chips(send_hwnd: int) -> dict:
    """받는사람 / 참조에 실제로 들어간 칩을 행별로 나눠 읽는다.

    칩은 두 행 모두 같은 Afx: 클래스라 텍스트만으로는 구분이 안 된다.
    '참    조' 버튼의 화면 위치를 기준선으로 삼아 그보다 아래를 참조로 본다 —
    받는사람이 여러 줄로 늘어나면 참조 버튼도 같이 내려가므로 이 기준은 안 깨진다.

    ⚠️ **보이는 칩만 세면 안 된다** (2026-09-03 실측). 받는사람 줄은 두 줄까지만 펼쳐 보이고
    나머지는 접혀서 IsWindowVisible=0 이 된다(오른쪽 'Expand' 버튼 id 8011).
    12명을 넣었는데 6명만 들어갔다고 잘못 보고한 적이 있다 — 넣기는 다 됐는데 읽기가 틀렸던 것.
    접힌 칩은 자기 줄의 시작 좌표로 모이므로(받는사람은 받는사람 줄, 참조는 참조 줄)
    위치로 행을 가르는 것 자체는 그대로 통한다.
    """
    cc_btn = _find_child_by_id(send_hwnd, CC_BUTTON_ID)
    # 참조 줄을 안 열었으면 칩은 전부 받는사람이다. 접힌 받는사람 칩이 참조 버튼보다 아래로 모일 때가 있어서
    # 위치로만 가르면 틀린다 (2026-09-11 실측: 전 교직원 73명 중 3명이 참조로 분류돼 «칩 70개»로 보고됐다).
    cc_open = len(_recipient_edits(send_hwnd)) >= 2
    line = (win32gui.GetWindowRect(cc_btn)[1] - 4) if (cc_btn and cc_open) else 10 ** 9
    to, cc = [], []

    def cb(child, _):
        if win32gui.GetClassName(child).startswith('Afx:'):
            text = win32gui.GetWindowText(child)
            if text and not _is_attachment_chip(text):
                bucket = cc if win32gui.GetWindowRect(child)[1] >= line else to
                if text not in bucket:
                    bucket.append(text)

    win32gui.EnumChildWindows(send_hwnd, cb, None)   # 손자 창까지 돈다 — 재귀 불필요
    return {"to": to, "cc": cc}


def attach_files(send_hwnd: int, paths: list) -> int:
    """파일첨부 버튼 -> 열기 대화상자에 경로를 따옴표로 묶어 한 번에 넣는다."""
    import os
    paths = [os.path.abspath(p) for p in paths]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise RuntimeError("첨부할 파일이 없습니다: " + ", ".join(missing))

    btn = find_child_by_text(send_hwnd, '파일첨부') or _find_child_by_id(send_hwnd, 1449)
    if not btn:
        raise RuntimeError("파일첨부 버튼을 찾을 수 없습니다.")
    click_hwnd(btn)

    def _find_open_dialog():
        cands = []

        def cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == '#32770':
                if win32gui.GetWindowText(hwnd) in ('열기', 'Open'):
                    cands.append(hwnd)

        win32gui.EnumWindows(cb, None)
        return cands[0] if cands else None

    file_dlg = _wait_for(_find_open_dialog, 6.0, 0.05)
    if not file_dlg:
        raise RuntimeError("파일 선택 대화상자가 열리지 않았습니다.")

    bring_to_front(file_dlg)
    time.sleep(0.1)
    name_edit = _find_child_by_id(file_dlg, 1148, 'Edit')
    open_btn = _find_child_by_id(file_dlg, 1)
    if not name_edit or not open_btn:
        raise RuntimeError("파일 선택 대화상자의 입력란을 찾을 수 없습니다.")

    win32api.SendMessage(name_edit, win32con.WM_SETTEXT, 0,
                         " ".join('"%s"' % p for p in paths))
    time.sleep(0.15)
    if QUIET:
        win32gui.PostMessage(file_dlg, win32con.WM_COMMAND, win32con.IDOK, open_btn)   # 「열기」
    else:
        click_hwnd(open_btn)

    closed = _wait_for(
        lambda: not (win32gui.IsWindow(file_dlg) and win32gui.IsWindowVisible(file_dlg)),
        6.0, 0.05)
    if not closed:
        raise RuntimeError("파일 선택 대화상자가 닫히지 않았습니다. 경로를 확인하세요.")
    time.sleep(0.5)
    return len(paths)


SCHEDULE_BUTTON_ID = 1660     # '예약전송' 체크박스
SELF_COPY_BUTTON_ID = 1661    # '나에게 보내기' 체크박스


def _checkbox_on(send_hwnd: int, ctrl_id: int) -> bool:
    """전송창 아래쪽 체크박스가 켜져 있는지.

    ⚠️ BM_GETCHECK 는 이 체크박스들에 대해 **항상 0을 돌려준다** (오너드로우, 2026-09-08 실측).
    대신 컨트롤 텍스트가 상태를 그대로 말해 준다 — 켜짐 '선택 예약전송' / 꺼짐 '해제 예약전송'.
    (양방향으로 눌러 가며 화면으로 확인함)
    """
    b = _find_child_by_id(send_hwnd, ctrl_id)
    return bool(b) and win32gui.GetWindowText(b).startswith('선택')


def ensure_not_scheduled(send_hwnd: int) -> bool:
    """'예약전송'을 끈다. 실제로 껐으면 True (= 받는사람이 지워졌다는 뜻이기도 하다).

    ⚠️ 이 전송창은 **열 때마다 「예약전송」과 「나에게 보내기」가 켜진 채로 뜬다** (2026-09-08 실측,
    화면으로 확인). 켜져 있으면 «보내기»를 눌러도 그 자리에서 안 나가고 예약 시각까지 묶인다 —
    16:50에 보낸 테스트가 17:00에 도착했고, 그 뒤로는 아예 전송이 안 됐다.

    ⚠️ 그런데 이 체크를 **마우스로 끄면 「받는사람」 칩이 통째로 지워진다.**
    체크박스가 「참    조」 줄 위에 겹쳐 있어서 클릭이 그 아래 컨트롤까지 건드린다
    (BM_CLICK 메시지는 이 오너드로우 버튼에 안 먹는다 — 실측). 그래서 **수신자를 넣기 전에**
    먼저 끄고, 그다음에 이름을 입력해 채운다.
    """
    b = _find_child_by_id(send_hwnd, SCHEDULE_BUTTON_ID)
    if not b:
        return False
    # 창이 막 열렸을 때는 이 컨트롤 텍스트가 빈 문자열이라 상태를 못 읽는다 — 잠깐 기다린다
    _wait_for(lambda: win32gui.GetWindowText(b) != "", 1.5, 0.05)
    if not _checkbox_on(send_hwnd, SCHEDULE_BUTTON_ID):
        return False
    if QUIET:
        # 체크 네모가 있는 왼쪽 끝(x=8)을 누른다 — 2026-11-12 실측: 이 자리는 칩이 남았고, 가운데는 칩이 지워진 적이 있다
        _, _, _, bh = win32gui.GetClientRect(b)
        post_click(b, 8, bh // 2)
    else:
        bring_to_front(send_hwnd)
        time.sleep(0.15)
        click_hwnd(b)
    _wait_for(lambda: not _checkbox_on(send_hwnd, SCHEDULE_BUTTON_ID), 1.5, 0.05)
    return True


def _key_combo(mod: int, key: int) -> None:
    if QUIET:
        _no_input("단축키")
    win32api.keybd_event(mod, 0, 0, 0)
    win32api.keybd_event(key, 0, 0, 0)
    time.sleep(0.03)
    win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(mod, 0, win32con.KEYEVENTF_KEYUP, 0)


def set_clipboard_text(text: str) -> bool:
    """클립보드에 글을 넣는다. 다른 프로그램이 붙잡고 있어 끝내 못 넣으면 False."""
    for _ in range(6):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
                return True
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)
    return False


_RED = _re.compile(r"\*\*(.+?)\*\*", _re.S)


def body_markup(body: str):
    """본문 속 `**중요한 말**` 을 빨간 굵은 글씨로 바꾼다. (평문, HTML|None) 을 돌려준다.

    사용자는 쿨메신저 안내문에서 **마감일처럼 꼭 봐야 할 말을 빨간색(#ff0000)+굵게** 칠한다
    (2026-09-30 9월분 근태신고서철 안내를 직접 고쳐 보낸 것에서 확인: `<font color="#ff0000"><b>10월 8일(목) 퇴청 전</b></font>까지`).
    표시가 없으면 HTML 없이 예전처럼 평문만 붙인다.
    """
    import html as _html
    plain = _RED.sub(lambda m: m.group(1), body)
    if plain == body:
        return body, None
    out = []
    last = 0
    for m in _RED.finditer(body):
        out.append(_html.escape(body[last:m.start()]))
        out.append('<font color="#ff0000"><b>%s</b></font>' % _html.escape(m.group(1)))
        last = m.end()
    out.append(_html.escape(body[last:]))
    lines = "".join(out).replace("\r\n", "\n").split("\n")
    return plain, "".join("<div>%s</div>" % (ln if ln else "<br>") for ln in lines)


def set_clipboard_html(plain: str, fragment: str) -> bool:
    """클립보드에 HTML(CF_HTML)과 평문을 같이 넣는다. 본문 편집기(Chromium)는 HTML 쪽을 붙인다."""
    head = ("Version:0.9\r\nStartHTML:%010d\r\nEndHTML:%010d\r\n"
            "StartFragment:%010d\r\nEndFragment:%010d\r\n")
    pre = "<html><body><!--StartFragment-->"
    post = "<!--EndFragment--></body></html>"
    h = len((head % (0, 0, 0, 0)).encode("utf-8"))
    sf = h + len(pre.encode("utf-8"))
    ef = sf + len(fragment.encode("utf-8"))
    eh = ef + len(post.encode("utf-8"))
    data = ((head % (h, eh, sf, ef)) + pre + fragment + post).encode("utf-8")
    fmt = win32clipboard.RegisterClipboardFormat("HTML Format")
    for _ in range(6):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(fmt, data)
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, plain)
                return True
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)
    return False


def read_clipboard_text() -> str:
    for _ in range(6):
        try:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or ""
                return ""
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)
    return ""


def find_body_target(send_hwnd: int) -> int:
    """본문 입력 영역. **쓸 때마다 다시 찾는다** — Chromium 편집기 창은 전송창이 열린 뒤
    잠깐 사이에 **다시 만들어져서** 먼저 받아둔 핸들이 무효가 된다 (2026-09-08 실측:
    GetWindowRect 가 «잘못된 창 핸들»로 죽었다). 이게 본문이 빈 채로 나가던 원인 중 하나다.
    """
    target = find_child_by_class(send_hwnd, 'Chrome_RenderWidgetHostHWND')
    if target:
        return target
    biggest, area = None, 0

    def cb(hwnd, _):
        nonlocal biggest, area
        if win32gui.GetClassName(hwnd) == 'AfxWnd140u' and win32gui.IsWindowVisible(hwnd):
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            if (r - l) * (b - t) > area:
                biggest, area = hwnd, (r - l) * (b - t)

    win32gui.EnumChildWindows(send_hwnd, cb, None)
    if not biggest:
        raise RuntimeError("본문 입력 영역을 찾을 수 없습니다.")
    return biggest


def _read_body_back(timeout: float) -> str:
    """본문 편집기 내용을 Ctrl+A · Ctrl+C 로 되가져온다. 복사가 실제로 일어났을 때만 그 글을 돌려준다.

    ⚠️ 되읽기 전에 클립보드를 **매번 다른 값으로 덮어써야** 한다. 안 그러면 Ctrl+C 가 아무것도 못 복사해도
    붙여넣기용으로 넣어둔 본문이 남아 «들어갔다»고 잘못 판정한다 (2026-09-08에 실제로 그랬다).
    """
    sentinel = "쿨메신저확인-%d" % time.perf_counter_ns()
    if not set_clipboard_text(sentinel):
        return ""
    _key_combo(0x11, 0x41)        # Ctrl+A
    time.sleep(0.08)
    _key_combo(0x11, 0x43)        # Ctrl+C
    got = _wait_for(lambda: (lambda t: t if t != sentinel else None)(read_clipboard_text()), timeout, 0.03)
    _key_combo(0x11, 0x23)        # Ctrl+End — 선택 해제
    return got or ""


def paste_body(send_hwnd: int, body: str) -> bool:
    """본문을 붙여넣고 **본문 전체가 실제로 들어갔는지** 확인한다.

    ⚠️ 본문은 Chrome 기반 편집기라 WM_GETTEXT 로 못 읽는다. 그래서 Ctrl+A · Ctrl+C 로 되가져와 대조한다.
    (2026-09-08 실측: 전송창이 막 열린 직후에는 편집기가 아직 준비가 안 돼서
    붙여넣기가 통째로 사라졌고, **본문이 빈 채로 «보내기»가 눌려 전송이 조용히 실패**했다.)

    예전에는 붙여넣기 뒤 0.35초·복사 뒤 0.3초를 무조건 잤다 (첫 시도에 성공해도 1.3초, 2026-09-11 실측).
    지금은 짧게 기다리고 **되읽은 글이 본문과 같아질 때까지만** 되풀이한다 —
    - 붙여넣기 전에 Ctrl+A 를 친다: 앞 시도의 붙여넣기가 늦게 들어갔어도 **덮어써서** 두 번 붙지 않는다
    - 대조는 앞 20자가 아니라 공백을 정리한 **본문 전체가 똑같은지**로 한다 (잘림·중복·확인용 글자 섞임도 잡는다)
    """
    if QUIET:
        return quiet_paste_body(send_hwnd, body)
    body, html_body = body_markup(body)
    want = " ".join(body.split())
    if not want:
        return False
    for attempt in range(3):
        require_front(send_hwnd, "메시지 전송")
        click_hwnd(find_body_target(send_hwnd))
        # 클릭 뒤 편집기가 키를 받기까지 기다린다. 0.12초로 줄였더니 Ctrl+C 가 아무것도 못 복사해 오히려 느려졌고,
        # 포커스(GetGUIThreadInfo)로 기다려 보니 이 편집기에서는 한 번도 참이 안 나왔다 (둘 다 2026-09-11 실측).
        # 그래서 예전에 검증된 0.25초를 그대로 쓴다.
        time.sleep(0.25 + 0.3 * attempt)
        if not (set_clipboard_html(body, html_body) if html_body else set_clipboard_text(body)):
            continue
        _key_combo(0x11, 0x41)        # Ctrl+A
        time.sleep(0.08)              # 단축키를 붙여 치면 편집기가 뒤엣것을 흘린다 (2026-09-11 실측, 예전 코드도 0.08 간격)
        _key_combo(0x11, 0x56)        # Ctrl+V
        # 붙여넣기가 클립보드를 조금 늦게 읽을 수 있다 — 곧바로 확인용 글로 덮어쓰면 그게 붙는다
        time.sleep(0.3)
        end = time.perf_counter() + 0.8 + 0.6 * attempt
        while True:
            if " ".join(_read_body_back(0.4).split()) == want:
                return True
            if time.perf_counter() >= end:
                break
            time.sleep(0.08)
    return False


# 메시지 전송 핵심 로직

GROUPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "groups.json")


def _expand_groups(names) -> list:
    """groups.json 의 그룹 이름(예: 1학년담임)을 사람 이름으로 풀고 중복을 뺀다.

    조직도의 그룹 행은 커스텀 드로우라 이름을 프로그램이 못 읽는다 (TVM_GETITEMW 빈 문자열,
    2026-09-11 화면 글자 인식도 대부분 빈 결과). 그래서 그룹은 이름 목록으로 두고, 한 명씩 칩으로 검증한다.
    매번 파일을 읽으므로 사람이 바뀌면 파일만 고치면 된다 (서버 재시작 불필요).
    """
    try:
        with open(GROUPS_FILE, encoding="utf-8") as f:
            groups = {k.replace(" ", ""): v for k, v in json.load(f).items() if not k.startswith("_")}
    except FileNotFoundError:
        groups = {}
    out = []
    for n in names or []:
        for m in groups.get(n.replace(" ", ""), [n]):
            if m not in out:
                out.append(m)
    return out


SELF_ALIASES = ("나", "나에게", "나한테", "본인")


def _owner_name() -> str:
    """로그인한 사용자 이름 — 쿨메신저가 사용자마다 만드는 Memo\\<이름>.udb 중 가장 최근에 쓰인 것.

    이 PC에는 다른 사람 이름의 .udb 도 남아 있어서 «최근»으로 가른다. 못 찾으면 빈 문자열이고,
    그러면 «나에게 보내기» 판단을 하지 않는다 (받는사람이 비면 보내지 않고 멈춘다).
    """
    import glob
    d = os.path.join(os.environ.get("LOCALAPPDATA", ""), "CoolMessenger", "Memo")
    files = glob.glob(os.path.join(d, "*.udb"))
    if not files:
        return ""
    return os.path.splitext(os.path.basename(max(files, key=os.path.getmtime)))[0]


EMOTICON_BUTTON_ID = 1845     # 전송창 「이모티콘」 버튼
EMOTICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "emoticons")


def _emoticon_popup(pid: int, send_hwnd: int):
    """이모티콘 팝업 — 제목 없는 #32770 안에 AfxWnd140u 격자가 있는 창.
    전송창·메인창도 AfxWnd140u 를 가지므로 «제목 없음»으로 가른다 (2026-10-15 실측)."""
    found = []
    def cb(h, _):
        if (h != send_hwnd and win32gui.IsWindowVisible(h)
                and win32gui.GetClassName(h) == "#32770" and not win32gui.GetWindowText(h)
                and win32process.GetWindowThreadProcessId(h)[1] == pid
                and find_child_by_class(h, "AfxWnd140u")):
            found.append(h)
    win32gui.EnumWindows(cb, None)
    return found[0] if found else None


def _match_template(shot, tpl):
    """격자 캡처 안에서 tpl 이 있는 자리(가운데, 격자 좌표)와 평균 차이를 돌려준다. 반 해상도로 비교."""
    import numpy as np
    a = np.asarray(shot.convert("L"), dtype=np.float32)[::2, ::2]
    t = np.asarray(tpl.convert("L"), dtype=np.float32)[::2, ::2]
    if a.shape[0] < t.shape[0] or a.shape[1] < t.shape[1]:
        return None, 999.0
    win = np.lib.stride_tricks.sliding_window_view(a, t.shape)
    diff = np.abs(win - t).mean(axis=(2, 3))
    y, x = np.unravel_index(np.argmin(diff), diff.shape)
    return ((x + t.shape[1] // 2) * 2, (y + t.shape[0] // 2) * 2), float(diff[y, x])


def _print_window(hwnd):
    """창 하나를 (가려져 있어도) 그 창 내용대로 찍는다. 실패하면 None."""
    try:
        _, _, w, h = win32gui.GetClientRect(hwnd)
        hdc = win32gui.GetDC(hwnd)
        dc = win32ui.CreateDCFromHandle(hdc)
        mem = dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(dc, w, h)
        mem.SelectObject(bmp)
        ok = ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), 1 | 2)   # CLIENTONLY | RENDERFULLCONTENT
        info = bmp.GetInfo()
        im = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
        mem.DeleteDC(); dc.DeleteDC(); win32gui.ReleaseDC(hwnd, hdc); win32gui.DeleteObject(bmp.GetHandle())
        return im if ok else None
    except Exception:
        return None


def insert_emoticon(send_hwnd: int, pid: int, name: str) -> str:
    """본문 끝에 이모티콘을 넣는다 (마우스 안 씀). 성공하면 "", 아니면 경고 문구.

    「최근사용」 탭은 순서가 바뀌므로 좌표가 아니라 emoticons/<name>.png 그림을 찾아 누른다.
    """
    from PIL import Image, ImageGrab
    path = os.path.join(EMOTICON_DIR, name + ".png")
    if not os.path.exists(path):
        return "  ⚠️ 이모티콘 그림이 없습니다: " + name
    tpl = Image.open(path)
    btn = _find_child_by_id(send_hwnd, EMOTICON_BUTTON_ID)
    if not btn:
        return "  ⚠️ 이모티콘 버튼을 찾지 못했습니다"
    bring_to_front(send_hwnd)
    popup = None
    for _ in range(2):   # 2026-11-02 교감 선생님께 보낼 때 창이 안 열렸다 — 한 번 더 눌러 본다
        win32gui.PostMessage(btn, win32con.BM_CLICK, 0, 0)
        popup = _wait_for(lambda: _emoticon_popup(pid, send_hwnd), 3.0, 0.05)
        if popup:
            break
    if not popup:
        return "  ⚠️ 이모티콘 창이 열리지 않았습니다"
    grid = find_child_by_class(popup, "AfxWnd140u")
    time.sleep(0.3)   # 스티커 그림이 다 그려질 때까지
    r = win32gui.GetWindowRect(grid)
    # 전송창이 다른 창 뒤에 있으면 화면 캡처엔 덮은 창이 찍힌다 → 그 창 자체를 PrintWindow 로 찍는다
    shot = _print_window(grid) if QUIET else None
    pos, score = _match_template(shot or ImageGrab.grab(bbox=r, all_screens=True), tpl)
    if pos is None or score > 20:
        win32gui.PostMessage(popup, win32con.WM_CLOSE, 0, 0)
        return "  ⚠️ 이모티콘 「%s」을 최근사용 탭에서 못 찾아 넣지 않았습니다 (차이 %.0f)" % (name, score)
    lp = win32api.MAKELONG(pos[0], pos[1])
    win32gui.PostMessage(grid, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(0.05)
    win32gui.PostMessage(grid, win32con.WM_LBUTTONUP, 0, lp)
    if not _wait_for(lambda: not win32gui.IsWindowVisible(popup), 2.0, 0.05):
        return "  ⚠️ 이모티콘을 눌렀는데 창이 안 닫혔습니다 — 들어갔는지 확인해주세요"
    return ""


def send_coolmessenger_message(*args, **kw) -> str:
    """발송하는 동안 쿨메신저가 앞(키보드)을 가져가면 0.05초 안에 원래 창으로 돌려준다 (QUIET 일 때)."""
    if not QUIET:
        return _send_coolmessenger_message(*args, **kw)
    give_back = fg_guard()
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            try:
                give_back()
            except Exception:
                pass
            stop.wait(0.05)
    t = threading.Thread(target=watch, daemon=True)
    t.start()
    try:
        return _send_coolmessenger_message(*args, **kw)
    finally:
        stop.set()
        t.join(0.5)
        give_back()


def _send_coolmessenger_message(recipients: list, title: str, body: str,
                                attachments=None, send: bool = True, cc=None, emoticon=None) -> str:
    """쿨메신저 메시지를 작성한다.

    send=False 면 보내기를 누르지 않고 작성된 상태로 창을 남긴다 (사람이 확인 후 발송).
    cc 는 참조로 넣을 이름 목록 (예: ['교감', '교장']).
    recipients·cc 에 groups.json 의 그룹 이름을 넣으면 사람 이름으로 풀어서 넣는다.
    """
    attachments = attachments or []
    # «나»는 로그인한 사용자 이름으로 바꾼다 — 화면(릴스 녹화 등)에 실명이 찍히지 않게 (2026-09-11)
    recipients = [(_owner_name() or n) if n in SELF_ALIASES else n for n in (recipients or [])]
    if not _is_all_staff(recipients or []):
        recipients = _expand_groups(recipients)
    cc = [n for n in _expand_groups(cc) if n not in recipients]
    pid = find_coolmessenger_pid()
    main_hwnd = get_window_by_title(pid, 'COOLMESSENGER')
    if not main_hwnd:
        raise RuntimeError("쿨메신저 메인 창이 없습니다.")

    # ⚠️ 쿨메신저는 전송창을 **하나만** 쓴다 (2026-09-08 실측: 전송창이 열려 있는 상태에서
    # 조직도의 다른 사람을 더블클릭해도 새 창이 안 생기고 그 창을 그대로 재사용한다).
    # 그래서 남아 있던 창을 못 닫으면 «남의 초안 위에» 제목·본문을 덮어쓰게 된다 —
    # 조용히 진행하지 않고 여기서 멈춘다.
    existing = get_send_dialog(pid)
    if existing:
        if not close_send_dialog(existing, pid):
            raise RuntimeError(
                "이미 열려 있는 '메시지 전송' 창이 닫히지 않았습니다. "
                "그 창을 화면에서 닫은 뒤 다시 시도해주세요 "
                "(그대로 진행하면 그 창의 내용 위에 덮어쓰게 됩니다).")

    # 숨은 메인창을 되살리고 트리를 찾는다. 앞으로 꺼내지는 않는다 —
    # 검색·더블클릭은 메시지로 하므로 필요 없고, 실제 마우스가 필요한 경로는 그 자리에서 꺼낸다.
    tree_hwnd = ensure_main_ready(main_hwnd, pid)
    if not recipients:
        raise RuntimeError("수신자가 없습니다.")

    all_staff = _is_all_staff(recipients)
    quiet_self = False

    if all_staff:
        # 전 교직원 — 조직도 루트를 통째로 체크해서 연다 (검색 경로로는 안 된다)
        send_hwnd = open_send_dialog_for_all(main_hwnd, tree_hwnd, pid)
        bring_to_front(send_hwnd)
        time.sleep(0.1)
        # ⚠️ 이 경로는 받는사람을 «이름으로 다시 채울 수 없다» (70명 넘는 이름을 칠 수는 없다).
        # 「예약전송」을 끄면 칩이 통째로 지워지므로, 끈 다음 조직도에서 **다시 연다**.
        wiped = False
        if _checkbox_on(send_hwnd, SCHEDULE_BUTTON_ID):
            ensure_not_scheduled(send_hwnd)
            send_hwnd = open_send_dialog_for_all(main_hwnd, tree_hwnd, pid)
            bring_to_front(send_hwnd)
            time.sleep(0.1)
            if _checkbox_on(send_hwnd, SCHEDULE_BUTTON_ID):
                return ("[중단] 「예약전송」이 켜져 있어 지금 보내도 그 자리에서 안 나갑니다. "
                        "전송창에서 직접 끄고 다시 시도해주세요. 전송창은 열어 두었습니다.")
    else:
        for attempt in range(2):
            send_hwnd = open_send_dialog_for(main_hwnd, tree_hwnd, pid, recipients[0])
            # (앞으로 꺼내기는 키 입력 직전에 require_front 가 한다 — 여기서 미리 기다리면 시간만 버린다)
            # 칩은 창보다 늦게 생긴다 (2026-09-11 실측: 뜬 직후 0개, 보통 1.3초 안팎, 숨은 창을 되살린 직후엔 1.5초를 넘었다)
            got = _wait_for(lambda: get_dialog_recipients(send_hwnd), 3.0, 0.05) or []
            if any(recipients[0] in g for g in got):
                break
            # 엉뚱한 사람이 잡혔으면 창을 닫고, 트리가 원래대로 돌아온 걸 확인한 뒤 검색부터 한 번만 다시 한다
            closed = close_send_dialog(send_hwnd, pid)
            clear_org_search(main_hwnd, wait=True)
            if attempt or not closed:
                raise RuntimeError(
                    "수신자 검증 실패: '%s'를 기대했는데 전송창에는 %s 가 들어갔습니다 (%d번 시도, 전송창 닫힘: %s)."
                    % (recipients[0], got, attempt + 1, closed))

        # 「예약전송」은 **수신자를 채우기 전에** 끈다 (끄면 받는사람이 지워지기 때문 — 위 설명 참고)
        if QUIET:
            # ⚠️ 전송창은 뜬 뒤 0.5초쯤 지나 스스로 「예약전송」을 켰다 껐다 한다 (2026-11-12 실측:
            # 0.5초에 끈 것이 그 뒤 앱 초기화로 다시 켜져 10분 예약으로 나갔다). 체크 상태가 1초 동안 그대로일 때까지 기다린다
            _settle_checks(send_hwnd)
        quiet_self = (QUIET and len(recipients) == 1 and recipients[0] == _owner_name()
                      and any(recipients[0] in c for c in get_dialog_chips(send_hwnd)["to"]))
        # 나에게 보낼 때는 예약전송을 건드리지 않는다 — 끄면 받는사람(나)이 지워지고, 「나에게 보내기」를 켜면
        # 예약전송이 다시 켜진다 (2026-11-12 실측, 순환). 켜진 채로 보내면 약 10분 뒤 도착한다 — 결과에 적는다
        wiped = False if quiet_self else ensure_not_scheduled(send_hwnd)

    if title:
        title_edit = _find_child_by_id(send_hwnd, 4362, 'Edit')
        if title_edit:
            win32api.SendMessage(title_edit, win32con.WM_SETTEXT, 0, title)
            time.sleep(0.05)

    # 전체 발송은 조직도가 이미 다 채워 넣었으므로 이름을 더 칠 게 없다
    need = [] if all_staff else (list(recipients) if wiped else list(recipients[1:]))
    if need:
        edits = _recipient_edits(send_hwnd)
        if edits:
            type_names_into(edits[0], need, send_hwnd)

    # 자기 자신은 이름을 쳐서는 못 넣는다 (조직도 더블클릭으로만 들어가고, 자동완성에는 안 나온다).
    # 조직도로는 분명히 잡혔는데 다시 채우기에서 빠졌다면 그건 «나 자신»이라는 뜻이므로,
    # 이 앱이 원래 그 용도로 두고 있는 「나에게 보내기」로 보낸다 (2026-09-08 실측으로 확인).
    # ⚠️ «칩이 비었다»만으로 자기 자신이라고 보면 안 된다 — 남의 이름을 다시 채우다 실패해도 칩이 빈다
    # (코덱스 검토 2026-09-11). 로그인한 사용자 이름과 같을 때만 «나에게 보내기»로 본다.
    self_send = False
    if (not all_staff and len(recipients) == 1 and recipients[0] == _owner_name()
            and not get_dialog_chips(send_hwnd)["to"]):
        # ⚠️ 「나에게 보내기」를 누르면 그 아래 겹쳐 있는 「예약전송」이 **다시 켜진다**
        # (2026-09-11 릴스 녹화 프레임으로 확인: 30초 프레임에서는 꺼져 있었는데 「나에게 보내기」를
        #  누른 뒤 다시 켜져 있었고, 23:59에 누른 메시지가 예약 시각 00:09에 도착했다).
        # 그래서 누른 «다음에» 예약전송을 다시 끄고, 끄면서 「나에게 보내기」가 풀렸으면 다시 켠다.
        # 자기 자신에게 보낼 때는 받는사람 칩이 원래 비어 있으므로 예약전송을 꺼도 지워질 칩이 없다.
        btn = _find_child_by_id(send_hwnd, SELF_COPY_BUTTON_ID)
        for _ in range(3):
            if btn and not _checkbox_on(send_hwnd, SELF_COPY_BUTTON_ID):
                bring_to_front(send_hwnd)
                time.sleep(0.15)
                click_hwnd(btn)
                _wait_for(lambda: _checkbox_on(send_hwnd, SELF_COPY_BUTTON_ID), 1.5, 0.05)
            if not _checkbox_on(send_hwnd, SCHEDULE_BUTTON_ID):
                break
            ensure_not_scheduled(send_hwnd)
        self_send = _checkbox_on(send_hwnd, SELF_COPY_BUTTON_ID)

    # 참조는 받는사람을 다 넣은 **뒤에** 연다 (같은 id를 쓰는 입력란이 둘이 되기 때문)
    if cc:
        type_names_into(open_cc_row(send_hwnd), cc, send_hwnd)

    body_ok = paste_body(send_hwnd, body)

    attached = 0
    if attachments:
        attached = attach_files(send_hwnd, attachments)

    emo_warn = insert_emoticon(send_hwnd, pid, emoticon) if (emoticon and body_ok) else ""

    clear_org_search(main_hwnd)

    chips = get_dialog_chips(send_hwnd)
    if all_staff:
        # ⚠️ 칩 읽기는 접힌 줄을 다 못 세서 앱 표시보다 적게 나온다
        # (2026-09-09 실측: 칩 70개로 읽혔는데 전송창 배지와 보낸메시지함은 73명).
        # 그래서 «몇 명»을 단정하지 않고 읽힌 개수만 그대로 적는다.
        summary = "받는사람 전 교직원 (조직도 전체 · 칩 %d개 확인)" % len(chips["to"])
    else:
        summary = "받는사람 %d명 %s" % (len(chips["to"]), chips["to"])
    if cc:
        summary += " / 참조 %d명 %s" % (len(chips["cc"]), chips["cc"])
    summary += ", 첨부 %d개" % attached
    if emoticon:
        summary += emo_warn or (", 이모티콘 「%s」" % emoticon)
    bold_missed = list(getattr(_BOLD, "missed", None) or [])
    _BOLD.missed = []
    if bold_missed:
        summary += "  ⚠️ 굵게 못 한 말: %s — 전송창에서 직접 굵게 한 뒤 보내 주세요" % bold_missed

    # 요청한 사람이 «그 줄(받는사람/참조)에» 실제로 들어갔는지, 요청 안 한 사람이 끼었는지 줄별로 대조한다.
    # 예전에는 못 넣은 이름을 경고만 하고 그대로 보냈다 (코덱스 검토 2026-09-11) — 이제 발송을 막는다.
    def _in_row(name, row):
        return any(name in c for c in chips[row])

    missing = [] if (all_staff or self_send) else [n for n in recipients if not _in_row(n, "to")]
    missing += [n for n in cc if not _in_row(n, "cc")]
    wanted = list(recipients) + list(cc)
    unexpected = [] if all_staff else [c for c in chips["to"] + chips["cc"]
                                       if not any(n in c for n in wanted)]
    if missing:
        summary += "  ⚠️ 못 넣은 이름: " + ", ".join(missing)
    if unexpected:
        summary += "  ⚠️ 요청하지 않은 사람이 들어감: " + ", ".join(unexpected)

    if wiped:
        summary += "  (켜져 있던 '예약전송'을 껐습니다)"
    if _checkbox_on(send_hwnd, SELF_COPY_BUTTON_ID):
        summary += "  (※ '나에게 보내기'가 켜져 있습니다)"

    if self_send:
        summary += "  (자기 자신에게 → '나에게 보내기'로 보냅니다)"
    elif not chips["to"]:
        summary += "  ⚠️ 받는사람이 비어 있습니다"
    if not body_ok:
        summary += "  ⚠️ 본문이 입력되지 않았습니다"

    if not send:
        return "[작성 완료 / 보내지 않음] " + summary + ". 내용 확인 후 보내기를 눌러주세요."

    # 이모티콘을 달라고 했는데 못 넣었으면 빼고 보내지 않는다 (2026-11-02 사용자 «결재 부탁엔 꼭»).
    if not body_ok or not (chips["to"] or self_send) or missing or unexpected or emo_warn or bold_missed:
        return ("[중단] 전송창이 제대로 채워지지 않아 보내지 않았습니다. "
                "전송창은 열어 두었으니 확인해주세요. " + summary)

    # ⚠️ 「예약전송」을 **보내기 직전에 한 번 더 본다** (2026-09-12).
    # 앞에서 껐어도 다시 켜져 있을 수 있다 — 2026-09-11 「나에게 보내기」 녹화에서 실제로 그랬다
    # (SELF_COPY_BUTTON_ID 를 누른 뒤 예약전송이 되살아나, 23:59:50 께 누른 것이 00:09 에 도착했다).
    if quiet_self and _checkbox_on(send_hwnd, SCHEDULE_BUTTON_ID):
        summary += "  (나에게 보내기라 예약전송이 켜진 채 나갑니다 — 약 10분 뒤 도착)"
    elif _checkbox_on(send_hwnd, SCHEDULE_BUTTON_ID):
        if (chips["to"] or chips["cc"]) and not self_send:
            # 받는사람이 칩으로 들어가 있으면 여기서 끌 수 없다 — 끄면 칩이 통째로 지워진다
            # (ensure_not_scheduled 설명 참고). 조용히 예약으로 내보내지 말고 멈춘다.
            return ("[중단] 보내기 직전에 보니 「예약전송」이 켜져 있습니다 — 지금 눌러도 그 자리에서 "
                    "안 나가고 예약 시각까지 묶입니다. 전송창은 열어 두었으니 「예약전송」을 직접 끄고 "
                    "보내주세요. " + summary)
        # 「나에게 보내기」는 그 체크만으로 본인에게 간다 — 칩이 지워져도 잃을 것이 없으므로 그 자리에서 끈다.
        # 둘이 서로를 되살릴 수 있어(오너드로우 버튼이 겹쳐 있다) 몇 번만 번갈아 맞춘다.
        for _ in range(3):
            if _checkbox_on(send_hwnd, SCHEDULE_BUTTON_ID):
                ensure_not_scheduled(send_hwnd)
            if self_send and not _checkbox_on(send_hwnd, SELF_COPY_BUTTON_ID):
                btn = _find_child_by_id(send_hwnd, SELF_COPY_BUTTON_ID)
                if btn:
                    bring_to_front(send_hwnd)
                    time.sleep(0.15)
                    click_hwnd(btn)
                    _wait_for(lambda: _checkbox_on(send_hwnd, SELF_COPY_BUTTON_ID), 1.5, 0.05)
            if (not _checkbox_on(send_hwnd, SCHEDULE_BUTTON_ID)
                    and (not self_send or _checkbox_on(send_hwnd, SELF_COPY_BUTTON_ID))):
                break
        else:
            return ("[중단] 「예약전송」과 「나에게 보내기」가 서로 되살아나 맞추지 못했습니다. "
                    "전송창은 열어 두었으니 화면에서 확인해주세요. " + summary)
        summary += "  (보내기 직전에 다시 켜져 있던 '예약전송'을 껐습니다)"

    send_btn = find_child_by_text(send_hwnd, '보내기') or _find_child_by_id(send_hwnd, 3249)
    if not send_btn:
        return "[경고] 작성은 됐는데 보내기 버튼을 찾지 못했습니다. 수동으로 전송해주세요."
    # 「보내기」는 **한 번에 안 먹을 때가 있다** (2026-09-08 실측: 눌러도 창이 그대로였고,
    # 다시 누르니 그때 나갔다). 창이 닫히는 것이 곧 전송 확인이므로, 안 닫히면 다시 누른다.
    # ⚠️ 단, 창이 **비활성**이면(전송 처리 중이거나 확인창이 떠 있음) 다시 누르지 않고 기다리기만 한다 —
    # 접수는 됐는데 창만 늦게 닫히는 경우 두 번 나갈 수 있다 (코덱스 검토 2026-09-11).
    # 닫힘은 «없어졌거나 숨었음»으로 본다 — 숨은 창을 안 닫힌 걸로 보고 다시 누르면 엉뚱한 자리를 누른다
    for attempt in range(3):
        if _dialog_gone(send_hwnd):
            break
        if attempt and not win32gui.IsWindowEnabled(send_hwnd):
            _wait_for(lambda: _dialog_gone(send_hwnd), 8.0, 0.1)
            break
        require_front(send_hwnd, "메시지 전송")
        time.sleep(0.25)
        click_hwnd(send_btn)
        if _wait_for(lambda: _dialog_gone(send_hwnd), 2.5, 0.05):
            break
    if _dialog_gone(send_hwnd):
        if all_staff:
            uncheck_org_root(tree_hwnd, pid)   # 조직도 전체 체크를 되돌린다
        return "[전송 완료] " + summary
    return ("[확인 필요] 보내기를 눌렀지만 전송창이 아직 열려 있습니다. "
            "화면을 확인해주세요. " + summary)


# ── MCP 도구 등록 ────────────────────────────────────────────────────────────

_SEND_LOCK = threading.Lock()
# 한 번 발송이 이보다 오래 걸리면 어딘가 멈춘 것이다 — 세션이 무한정 기다리지 않게 오류로 돌려준다.
SEND_TIMEOUT = 120


def _send_serialized(*args) -> str:
    """발송은 한 번에 하나만 — 전송창·클립보드·키보드를 전부 같이 쓰기 때문이다."""
    if not _SEND_LOCK.acquire(timeout=10):
        raise RuntimeError("이전 발송이 멈춘 채 남아 있습니다. 쿨메신저 전송창을 확인하고 클로드 창을 새로 열어 주세요.")
    try:
        return send_coolmessenger_message(*args)
    finally:
        _SEND_LOCK.release()


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="send_coolmessenger",
            description="쿨메신저로 메시지를 작성하고 전송합니다. 학교 랜 환경에서 쿨메신저가 실행 중이어야 합니다.",
            inputSchema={
                "type": "object",
                "required": ["recipients", "title", "body"],
                "properties": {
                    "recipients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "수신자 이름 또는 부서 키워드 목록 (예: ['교감', '교무부장', '홍길동']). **전 교직원에게 보내려면 ['전체'] 하나만 넣는다** — 조직도 루트를 통째로 체크해서 보낸다. **나에게 보낼 때는 ['나']** — 실명을 쓰지 않아도 「나에게 보내기」로 보낸다",
                    },
                    "title": {
                        "type": "string",
                        "description": "메시지 제목",
                    },
                    "body": {
                        "type": "string",
                        "description": "메시지 본문 (줄바꿈 \\n 가능). 마감일처럼 꼭 봐야 할 말은 **이렇게** 감싸면 굵은 글씨로 들어간다(빨간색은 아직 안 됨) — 날짜·기한은 항상 이렇게 표시한다",
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "첨부할 파일의 절대 경로 목록 (선택)",
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "참조로 넣을 이름 목록 (예: ['교감', '교장']). 받는사람을 다 넣은 뒤 참조 행이 열린다",
                    },
                    "emoticon": {
                        "type": "string",
                        "description": "본문 끝에 넣을 이모티콘 이름 (선택). 지금 있는 것: '감사합니다'(물개). 부탁·감사 메시지에 쓴다. 그림은 emoticons/<이름>.png",
                    },
                    "send": {
                        "type": "boolean",
                        "description": "false면 보내기를 누르지 않고 작성만 해둔다 (기본 true)",
                    },
                },
            },
        ),
        types.Tool(
            name="check_coolmessenger_status",
            description="쿨메신저 실행 상태와 현재 열린 창을 확인합니다.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "send_coolmessenger":
        try:
            result = await asyncio.wait_for(asyncio.to_thread(
                _send_serialized,
                arguments["recipients"],
                arguments["title"],
                arguments["body"],
                arguments.get("attachments"),
                arguments.get("send", True),
                arguments.get("cc"),
                arguments.get("emoticon"),
            ), timeout=SEND_TIMEOUT)
            return [types.TextContent(type="text", text=result)]
        except asyncio.TimeoutError:
            return [types.TextContent(type="text", text=(
                f"❌ {SEND_TIMEOUT}초가 지나도 발송이 끝나지 않아 기다리기를 멈췄습니다. "
                "보내졌는지 「메시지 관리함 > 보낸메시지」에서 확인하고, 다시 보내지 말고 사용자에게 알리세요."))]
        except Exception as e:
            return [types.TextContent(type="text", text=f"❌ 오류: {e}")]

    if name == "check_coolmessenger_status":
        try:
            pid = find_coolmessenger_pid()
            windows = []
            def cb(hwnd, _):
                _, p = win32process.GetWindowThreadProcessId(hwnd)
                if p == pid and win32gui.IsWindowVisible(hwnd):
                    t = win32gui.GetWindowText(hwnd)
                    if t:
                        windows.append(t)
            win32gui.EnumWindows(cb, None)
            main = get_window_by_title(pid, 'COOLMESSENGER')
            # 메인창은 트레이로 숨어서 windows 목록에 안 나올 때가 있다 — 숨음과 없음을 구분해 보여준다
            info = {"pid": pid, "windows": windows,
                    "main_visible": bool(main and win32gui.IsWindowVisible(main)),
                    "main_enabled": bool(main and win32gui.IsWindowEnabled(main))}
            return [types.TextContent(type="text", text=json.dumps(info, ensure_ascii=False))]
        except Exception as e:
            return [types.TextContent(type="text", text=f"❌ {e}")]

    raise ValueError(f"Unknown tool: {name}")


async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
