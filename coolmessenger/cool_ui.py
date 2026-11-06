# -*- coding: utf-8 -*-
"""쿨메신저 첨부파일 내려받기 — 「메시지 관리함」 창을 Win32로 조작한다.

첨부 자체는 DB에 없고 CoolFile2 서버에 있다. 받는 유일한 방법이 클라이언트 UI라
여기만 화면을 건드린다(메시지 읽기는 cool.py 가 DB로 처리 — 화면 안 씀).

  python cool_ui.py download 6534 6526        # 지정한 메시지의 첨부 저장
  python cool_ui.py download --new            # 새 메시지 중 첨부 있는 것 전부
  python cool_ui.py download --new --dry-run  # 무엇을 받을지만 출력

저장 위치는 쿨메신저 옵션의 「받은 파일 폴더」이고 우리가 못 고른다.
(그 경로를 COOL_DOWNLOAD_DIR 환경변수에 넣어 두면 cool.py 가 거기를 본다.)
이미 같은 이름이 있으면 크기가 같으면 [넘어가기], 다르면 [다시받기](덮어쓰기)를 누른다.
"""
import argparse
import ctypes
import os
import sys
import time
from ctypes import wintypes

import win32api
import win32con
import win32gui
import win32process

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cool  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = wintypes.HANDLE
k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                               wintypes.DWORD, wintypes.DWORD]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


class LVITEMW32(ctypes.Structure):
    """32비트 프로세스용 LVITEMW. 쿨메신저가 32비트라 포인터가 4바이트다 —
    64비트 구조체를 그대로 보내면 필드가 어긋나 텍스트가 전부 빈 문자열로 온다."""
    _fields_ = [("mask", ctypes.c_uint32), ("iItem", ctypes.c_int32),
                ("iSubItem", ctypes.c_int32), ("state", ctypes.c_uint32),
                ("stateMask", ctypes.c_uint32), ("pszText", ctypes.c_uint32),
                ("cchTextMax", ctypes.c_int32), ("iImage", ctypes.c_int32),
                ("lParam", ctypes.c_int32), ("iIndent", ctypes.c_int32),
                ("iGroupId", ctypes.c_int32), ("cColumns", ctypes.c_uint32),
                ("puColumns", ctypes.c_uint32), ("piColFmt", ctypes.c_uint32),
                ("iGroup", ctypes.c_int32)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


LVM_GETITEMCOUNT = 0x1004
LVM_ENSUREVISIBLE = 0x1013
LVM_GETITEMRECT = 0x100E
LVM_GETITEMTEXTW = 0x1073


# ── 창 찾기 ─────────────────────────────────────────────────────────────────

def find_pid() -> int:
    import psutil
    for p in psutil.process_iter(["name", "pid"]):
        if p.info["name"] and "CoolMessenger" in p.info["name"]:
            return p.info["pid"]
    raise SystemExit("쿨메신저가 실행 중이지 않다. 먼저 실행하고 로그인할 것.")


def windows_of(pid, title=None, cls=None, visible_only=True):
    out = []

    def cb(h, _):
        _, p = win32process.GetWindowThreadProcessId(h)
        if p != pid:
            return
        if visible_only and not win32gui.IsWindowVisible(h):
            return
        t, c = win32gui.GetWindowText(h), win32gui.GetClassName(h)
        if title is not None and title not in t:
            return
        if cls is not None and c != cls:
            return
        out.append(h)

    win32gui.EnumWindows(cb, None)
    return out


def children(hwnd):
    out = []
    win32gui.EnumChildWindows(
        hwnd,
        lambda h, _: out.append((h, win32gui.GetClassName(h), win32gui.GetWindowText(h),
                                 bool(win32gui.IsWindowVisible(h)))),
        0)
    return out


def child_by(hwnd, cls=None, text_has=None, visible=True):
    for h, c, t, v in children(hwnd):
        if visible and not v:
            continue
        if cls and c != cls:
            continue
        if text_has and text_has not in t:
            continue
        return h
    return None


def bring_to_front(h):
    cur = k32.GetCurrentThreadId()
    tid = u32.GetWindowThreadProcessId(h, None)
    u32.AttachThreadInput(cur, tid, True)
    u32.ShowWindow(h, win32con.SW_RESTORE)
    u32.BringWindowToTop(h)
    u32.SetForegroundWindow(h)
    u32.AttachThreadInput(cur, tid, False)
    time.sleep(0.35)


def click_at(x, y):
    win32api.SetCursorPos((x, y))
    time.sleep(0.10)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
    time.sleep(0.35)


def click_hwnd(h):
    r = win32gui.GetWindowRect(h)
    click_at((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)


def ctrl_text(h) -> str:
    """다른 프로세스의 표준 컨트롤 텍스트 (WM_GETTEXT 로 마샬링됨)."""
    n = u32.SendMessageW(h, win32con.WM_GETTEXTLENGTH, 0, 0)
    buf = ctypes.create_unicode_buffer(n + 2)
    u32.SendMessageW(h, win32con.WM_GETTEXT, n + 1, buf)
    return buf.value


# ── 리스트뷰 읽기 ────────────────────────────────────────────────────────────

class ListReader:
    def __init__(self, pid, lv):
        self.lv = lv
        self.h = k32.OpenProcess(0x0008 | 0x0010 | 0x0020, False, pid)
        if not self.h:
            raise SystemExit("쿨메신저 프로세스를 열 수 없다 (권한).")
        self.TB = 1024
        self.rem = k32.VirtualAllocEx(self.h, None, self.TB + 256, 0x1000, 0x04)

    def count(self):
        return win32gui.SendMessage(self.lv, LVM_GETITEMCOUNT, 0, 0)

    def text(self, row, sub):
        it = LVITEMW32()
        it.mask = 1
        it.iItem, it.iSubItem = row, sub
        it.pszText, it.cchTextMax = self.rem, self.TB // 2
        w = ctypes.c_size_t(0)
        k32.WriteProcessMemory(self.h, self.rem + self.TB, ctypes.addressof(it),
                               ctypes.sizeof(it), ctypes.byref(w))
        win32gui.SendMessage(self.lv, LVM_GETITEMTEXTW, row, self.rem + self.TB)
        buf = ctypes.create_string_buffer(self.TB)
        r = ctypes.c_size_t(0)
        k32.ReadProcessMemory(self.h, self.rem, ctypes.addressof(buf), self.TB, ctypes.byref(r))
        return buf.raw.decode("utf-16-le", errors="ignore").split("\x00")[0]

    def click_row(self, row):
        win32gui.SendMessage(self.lv, LVM_ENSUREVISIBLE, row, 0)
        time.sleep(0.25)
        rc = RECT()
        w = ctypes.c_size_t(0)
        k32.WriteProcessMemory(self.h, self.rem + self.TB, ctypes.addressof(rc),
                               ctypes.sizeof(rc), ctypes.byref(w))
        win32gui.SendMessage(self.lv, LVM_GETITEMRECT, row, self.rem + self.TB)
        out = RECT()
        r = ctypes.c_size_t(0)
        k32.ReadProcessMemory(self.h, self.rem + self.TB, ctypes.addressof(out),
                              ctypes.sizeof(out), ctypes.byref(r))
        pt = POINT((out.left + out.right) // 2, (out.top + out.bottom) // 2)
        u32.ClientToScreen(self.lv, ctypes.byref(pt))
        click_at(pt.x, pt.y)

    def close(self):
        if self.rem:
            k32.VirtualFreeEx(self.h, self.rem, 0, 0x8000)
        k32.CloseHandle(self.h)


# ── 메시지 관리함 열기 ───────────────────────────────────────────────────────

def open_manager(pid):
    """(관리함 hwnd, 우리가 열었는지, 메인창이 원래 최소화였는지)"""
    existing = windows_of(pid, title="메시지 관리함", cls="#32770")
    mains = windows_of(pid, title="COOLMESSENGER", cls="#32770")
    if not mains:
        raise SystemExit("쿨메신저 메인 창을 못 찾았다. 로그인 상태인지 확인할 것.")
    main = mains[0]
    was_min = bool(u32.IsIconic(main))
    if existing:
        return existing[0], False, was_min

    bring_to_front(main)
    btn = child_by(main, cls="Button", text_has="메시지관리함 열기")
    if not btn:
        raise SystemExit("메인 창에서 '메시지관리함 열기' 버튼을 못 찾았다.")
    click_hwnd(btn)
    for _ in range(40):
        got = windows_of(pid, title="메시지 관리함", cls="#32770")
        if got:
            time.sleep(0.8)
            return got[0], True, was_min
        time.sleep(0.25)
    raise SystemExit("메시지 관리함이 열리지 않았다.")


def attachment_labels(mgr):
    """지금 선택된 메시지의 첨부 라벨들. '파일명 (63 KB)' 형태의 자식 창."""
    out = []
    for h, c, t, v in children(mgr):
        if v and t.endswith(")") and ("Afx" in c) and (" KB)" in t or " MB)" in t or " Bytes)" in t):
            out.append(t[:t.rfind("(")].strip())
    return out


# ── 다운로드 중 뜨는 창 처리 ────────────────────────────────────────────────

def handle_popups(pid, expected, timeout=180):
    """저장 버튼을 누른 뒤 뜨는 창들을 정리한다.

    · '같은 이름을 가진 파일이 이미 존재합니다' → 크기 같으면 [넘어가기], 다르면 [다시받기]
    · '파일 다운로드 중'                        → 끝날 때까지 대기
    · 저장이 끝나면 탐색기 창이 뜬다            → 닫는다
    """
    want = {a["name"]: a.get("size") for a in expected}
    deadline = time.time() + timeout
    quiet = 0
    while time.time() < deadline:
        acted = False

        for h in windows_of(pid, title="이미 존재", cls="#32770"):
            path = ""
            for ch, c, t, v in children(h):
                if c == "Edit" and v:
                    val = ctrl_text(ch)
                    if "\\" in val:
                        path = val
                        break
            name = os.path.basename(path)
            same = False
            if name in want and os.path.exists(path):
                same = (want[name] is None or os.path.getsize(path) == want[name])
            label = "넘어가기" if same else "다시받기"
            btn = child_by(h, cls="Button", text_has=label)
            if btn:
                bring_to_front(h)
                click_hwnd(btn)
                print("    · 이미 있음 → [%s] %s" % (label, name or "?"))
                acted = True
            time.sleep(0.4)

        if windows_of(pid, title="파일 다운로드 중", cls="#32770"):
            acted = True
            time.sleep(0.6)

        # 저장이 끝나면 탐색기가 열린다 (별도 프로세스라 pid로 못 찾는다)
        for h in win32_explorer_windows():
            win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
            acted = True
            time.sleep(0.3)

        missing = [n for n, s in want.items()
                   if not os.path.exists(os.path.join(cool.DOWNLOAD_DIR, n))]
        if not acted:
            quiet += 1
            if not missing or quiet >= 6:
                return
        else:
            quiet = 0
        time.sleep(0.5)


_explorer_seen = set()


def snapshot_explorers():
    _explorer_seen.clear()
    _explorer_seen.update(_all_explorers())


def _all_explorers():
    out = []
    win32gui.EnumWindows(
        lambda h, _: (win32gui.GetClassName(h) == "CabinetWClass"
                      and win32gui.IsWindowVisible(h) and out.append(h)), None)
    return out


def win32_explorer_windows():
    """저장 직후 새로 뜬 탐색기 창만."""
    return [h for h in _all_explorers() if h not in _explorer_seen]


# ── 본작업 ──────────────────────────────────────────────────────────────────

def download(msgs, dry_run=False, keep_open=False):
    todo = [m for m in msgs if m["attachments"]]
    if not todo:
        print("첨부가 있는 메시지가 없다.")
        return []

    print("첨부 있는 메시지 %d건" % len(todo))
    plan = []
    for m in todo:
        missing = [a for a in m["attachments"]
                   if not (a["saved"] and a["size_match"])]
        tag = "받을 것 %d개" % len(missing) if missing else "이미 다 있음 — 건너뜀"
        print("  [%s] %s %s · %s" % (m["key"], m["date"][:16], m["sender"]["name"], tag))
        for a in m["attachments"]:
            print("      %s %s (%s)" % ("·" if a["saved"] else "▶", a["name"],
                                        cool.fmt_size(a["size"])))
        if missing:
            plan.append(m)
    if dry_run:
        return plan
    if not plan:
        print("\n모두 이미 받아져 있다. 내려받을 것 없음.")
        return []

    pid = find_pid()
    mgr, we_opened, was_min = open_manager(pid)
    main = windows_of(pid, title="COOLMESSENGER", cls="#32770")[0]
    old_cursor = win32api.GetCursorPos()

    tab = child_by(mgr, cls="Button", text_has="받은메시지")
    if tab:
        bring_to_front(mgr)
        click_hwnd(tab)
        time.sleep(0.6)

    lv = child_by(mgr, cls="SysListView32", text_has="메시지 리스트")
    if not lv:
        raise SystemExit("메시지 리스트를 못 찾았다.")
    lr = ListReader(pid, lv)
    n = lr.count()

    # 날짜+보낸사람으로 행 번호를 찾아 둔다 (목록은 최신이 0번)
    index = {}
    for row in range(n):
        index.setdefault((lr.text(row, 2)[:19], lr.text(row, 0)), row)

    done = []
    try:
        for m in plan:
            key = (m["date"][:19], m["sender"]["raw"])
            row = index.get(key)
            if row is None:
                print("  ! [%s] 목록에서 못 찾음 (%s) — 건너뜀" % (m["key"], m["date"][:19]))
                continue
            print("\n  [%s] %s — %s" % (m["key"], m["sender"]["name"], m["date"][:16]))
            bring_to_front(mgr)
            lr.click_row(row)
            time.sleep(1.0)

            labels = attachment_labels(mgr)
            want = [a["name"] for a in m["attachments"]]
            if not labels:
                print("    ! 첨부 패널이 안 뜬다 — 건너뜀")
                continue
            if set(labels) != set(want):
                print("    ! 선택된 메시지의 첨부가 다르다: %s (기대: %s) — 건너뜀"
                      % (labels, want))
                continue

            btn = child_by(mgr, cls="Button", text_has="저장 (Ctrl+S)")
            if not btn:
                print("    ! '모든파일 저장' 버튼이 안 보인다 — 건너뜀")
                continue
            snapshot_explorers()
            click_hwnd(btn)
            handle_popups(pid, m["attachments"])

            ok = []
            for a in m["attachments"]:
                p = os.path.join(cool.DOWNLOAD_DIR, a["name"])
                if os.path.exists(p):
                    sz = os.path.getsize(p)
                    match = (a["size"] is None or sz == a["size"])
                    print("    %s %s (%s)" % ("✔" if match else "△", a["name"],
                                              cool.fmt_size(sz)))
                    ok.append(p)
                else:
                    print("    ✖ %s — 저장 실패" % a["name"])
            done.append({"key": m["key"], "files": ok})
    finally:
        lr.close()
        if we_opened and not keep_open:
            close_btn = child_by(mgr, cls="Button", text_has="닫기 (ESC)")
            if close_btn:
                try:
                    bring_to_front(mgr)
                    click_hwnd(close_btn)
                except Exception:
                    win32gui.PostMessage(mgr, win32con.WM_CLOSE, 0, 0)
        if was_min:
            u32.ShowWindow(main, win32con.SW_MINIMIZE)
        try:
            win32api.SetCursorPos(old_cursor)
        except Exception:
            pass

    print("\n저장 폴더: %s" % cool.DOWNLOAD_DIR)
    return done


def main():
    ap = argparse.ArgumentParser(description="쿨메신저 첨부 내려받기")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("keys", nargs="*", type=int)
    d.add_argument("--new", action="store_true", help="마지막 처리 이후 새 메시지 전부")
    d.add_argument("--since")
    d.add_argument("--hours", type=int)
    d.add_argument("--limit", type=int, default=200)
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--keep-open", action="store_true", help="메시지 관리함을 닫지 않는다")
    args = ap.parse_args()

    if args.keys:
        msgs = cool.fetch(keys=args.keys, limit=len(args.keys))
    else:
        since, after_key = cool.resolve_window(args)
        msgs = cool.fetch(since=since, after_key=after_key, limit=args.limit)
    download(msgs, dry_run=args.dry_run, keep_open=args.keep_open)


if __name__ == "__main__":
    main()
