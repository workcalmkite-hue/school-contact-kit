#!/usr/bin/env python3
"""카카오톡 맥 앱 자동화 — 방 열기 / 발송 / 수정 / 읽기 / 캡처.

카카오톡은 애플스크립트 사전이 없고 메시지 DB도 암호화돼 있다.
그래서 **접근성(AX) API 를 직접** 두드리고, 꼭 필요한 곳에서만 Quartz 로 마우스·키를 흉내낸다.

⚠️ 실측으로 확인한 함정 (2026-08-25 / 2026-09-08 갱신)
  - System Events 의 keystroke 는 카카오톡이 무시한다 → CGEvent(kCGHIDEventTap) 필수
  - 애플스크립트 double click 은 한글 로케일에서 -2753, click 2회도 안 먹는다
    → CGEvent 의 clickCount=2 를 명시해야 방이 열린다
  - Escape 를 누르면 창이 통째로 닫힌다 (프로세스는 살아있음) → 쓰지 말 것
  - 카톡이 frontmost 가 아니면 창을 0개로 보고한다 (AX 로 직접 물어도 똑같다)
    → 무엇을 하든 activate() 부터. 실측: Code 가 앞이면 AXWindows == []

⚡️ 2026-09-08 속도 개선 — 실측 근거
  - osascript(System Events) 호출 1회 = 140~200ms. AX API 직접 호출 = 0.36ms (약 400배)
    → osascript 를 전부 걷어냈다. 앱 활성화도 NSRunningApplication 으로 (프로세스 안 띄움)
  - **메시지 본문이 AX 로 읽힌다.** 예전 문서엔 "행 안에 텍스트가 안 나온다"고 적혀 있었는데
    그건 System Events 경로의 한계였다. AX 로는 AXTextArea.AXValue 에 본문이 그대로 있다
    → 「보냈는지」를 스크린샷 없이 검증할 수 있다. 고정 대기(sleep)를 전부 폴링으로 바꾼 근거
  - **채팅 목록 행에 방 이름이 노출된다** → 검색창을 거치지 않고 방을 바로 찾아 연다
  - 입력칸은 AXFocused 로 포커스를 줄 수 있다 → 입력칸을 마우스로 클릭할 필요가 없다
  - 다만 **입력칸 AXValue 에 글자를 써넣으면 화면엔 보여도 전송은 안 된다**(카톡 내부 모델에
    안 들어간다. Enter 를 쳐도 무반응). 그래서 본문 입력은 클립보드 붙여넣기를 유지한다

  실측: send() 4.5초 → 0.06초 / open_room() 9.6초 → 아래 참조
"""
import json, re, subprocess, sys, time
from pathlib import Path

if sys.platform != "darwin":                            # ← 기기 확인 (2026-09-09)
    sys.exit("kt.py 는 맥 전용이다 (지금 sys.platform=%r). "
             "윈도우 카카오톡은 스킬 「카톡전송」의 kakao.ps1 을 쓴다 — "
             "첨부 방식부터 다르다: 맥=입력칸에 붙여넣기, 윈도우=종이 아이콘+파일 대화상자."
             % sys.platform)

import Quartz as Q

try:
    from ApplicationServices import (
        AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
        AXUIElementSetAttributeValue, AXValueCreate, AXValueGetValue,
        AXUIElementPerformAction, kAXValueCGPointType, kAXValueCGSizeType,
        kAXValueCFRangeType,
    )
    from CoreFoundation import CFRangeMake
    from AppKit import NSWorkspace, NSApplicationActivateIgnoringOtherApps
except ImportError:                                     # pragma: no cover
    sys.exit("pyobjc 가 없다 → python3 -m pip install "
             "pyobjc-framework-Cocoa pyobjc-framework-Quartz "
             "pyobjc-framework-ApplicationServices")

BUNDLE = "com.kakao.KakaoTalkMac"
APP = "KakaoTalk"
ROOM_ALIASES_FILE = Path(__file__).with_name("room_aliases.local.json")
ROOM_INDEX_FILE = Path(__file__).with_name("room_index.local.json")


def room_aliases():
    """사용자가 부르는 호칭 → 실제 카카오톡 채팅방 제목.

    개인 이름은 코드에 하드코딩하지 않고 gitignore 된 로컬 JSON에서 읽는다.
    """
    if not ROOM_ALIASES_FILE.exists():
        return {}
    try:
        data = json.loads(ROOM_ALIASES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"카카오톡 별칭 파일을 읽지 못했다: {e}") from e
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise RuntimeError("카카오톡 별칭 파일은 문자열: 문자열 형태의 JSON 객체여야 한다")
    return data


def resolve_room_alias(query):
    """별칭이면 실제 방 제목으로 바꾸고, 등록되지 않았으면 원문을 돌려준다."""
    query = query.strip()
    aliases = room_aliases()
    if query in aliases:
        return aliases[query]
    compact = "".join(query.split())
    for alias, title in aliases.items():
        if "".join(alias.split()) == compact:
            return title
    return query


def _compact_name(value):
    """띄어쓰기·구두점을 없앤 비교용 문자열."""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", str(value or "")).lower()


def _person_core(query):
    """매년 바뀔 수 있는 직책·호칭을 걷어 사람 이름 부분을 만든다."""
    core = _compact_name(query)
    for title in (
        "교무부장님", "교무부장", "부장선생님", "부장님", "부장",
        "선생님", "선생", "영어쌤", "영어샘", "쌤", "샘",
    ):
        core = core.replace(title, "")
    return core


def match_room_name(query, names):
    """목록 이름 중 유일한 대상. 없으면 None, 여러 개면 임의 선택하지 않는다."""
    names = [str(name) for name in names if name]
    resolved = resolve_room_alias(str(query or "").strip())

    if resolved in names:
        return resolved

    compact = _compact_name(resolved)
    direct = [name for name in names if compact and compact in _compact_name(name)]
    if len(direct) == 1:
        return direct[0]
    if len(direct) > 1:
        raise RuntimeError(f"'{query}' 후보가 여러 개다: {direct}")

    core = _person_core(resolved)
    fuzzy = [name for name in names if len(core) >= 2 and core in _compact_name(name)]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        raise RuntimeError(f"'{query}'의 이름 후보가 여러 개다: {fuzzy}")
    return None


def _resolve_row(query, rows):
    """AX 행 목록에서 안전하게 한 행만 고른다."""
    resolved = resolve_room_alias(str(query or "").strip())
    # 확인된 실제 저장 이름은 대개 최근 목록 위쪽에 있다. 정확히 같은 행이면
    # 전체 500여 행을 읽지 않고 즉시 반환한다.
    for row in rows:
        name = _room_name(row)
        if name == resolved:
            return row, name

    indexed = []
    for row in rows:
        name = _room_name(row)
        if name:
            indexed.append((row, name))
    name = match_room_name(query, [name for _, name in indexed])
    if name is None:
        return None
    hits = [(row, candidate) for row, candidate in indexed if candidate == name]
    if len(hits) != 1:
        raise RuntimeError(f"'{name}' 채팅방이 {len(hits)}개라 임의로 고를 수 없다")
    return hits[0]


def _room_name(row):
    """채팅 목록 행의 첫 정적 텍스트인 방 이름만 읽는다."""
    for el in _walk(row, maxd=4):
        if _attr(el, "AXRole") == "AXStaticText":
            value = _attr(el, "AXValue")
            if value:
                return str(value)
    texts = _texts(row)
    return texts[0] if texts else None


def _room_kind(row):
    descriptions = {
        _attr(el, "AXDescription")
        for el in _walk(row, maxd=4)
        if _attr(el, "AXDescription")
    }
    if "badge me" in descriptions:
        return "self"
    if "badge warehouse room" in descriptions:
        return "group"
    if any(_attr(el, "AXIdentifier") == "Count Label" for el in _walk(row, maxd=4)):
        return "group"
    return "chat"


# ── AX 저수준 ────────────────────────────────────────────────
def _attr(el, name):
    err, val = AXUIElementCopyAttributeValue(el, name, None)
    return val if err == 0 else None


def _set(el, name, val):
    return AXUIElementSetAttributeValue(el, name, val) == 0


def _geo(el):
    """(x, y, w, h) — 화면 좌표. 못 읽으면 None."""
    p, s = _attr(el, "AXPosition"), _attr(el, "AXSize")
    if not p or not s:
        return None
    okp, pv = AXValueGetValue(p, kAXValueCGPointType, None)
    oks, sv = AXValueGetValue(s, kAXValueCGSizeType, None)
    if not (okp and oks):
        return None
    return (int(pv.x), int(pv.y), int(sv.width), int(sv.height))


def _find_running():
    for a in NSWorkspace.sharedWorkspace().runningApplications():
        if a.bundleIdentifier() == BUNDLE:
            return a
    return None


def _running():
    """카카오톡 실행 중 앱. 꺼져 있으면 실행하고 최대 10초 기다린다."""
    app = _find_running()
    if app is not None:
        return app
    subprocess.Popen(
        ["open", "-b", BUNDLE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    app = _until(_find_running, timeout=10.0, step=0.05)
    if app is None:
        raise RuntimeError("카카오톡을 실행했지만 10초 안에 열리지 않았다")
    return app


_axapp = None


def _app():
    global _axapp
    if _axapp is None:
        _axapp = AXUIElementCreateApplication(_running().processIdentifier())
    return _axapp


def _until(pred, timeout=5.0, step=0.01):
    """pred() 가 참값을 돌려줄 때까지 기다렸다가 그 값을 준다. 시간 초과면 None."""
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        got = pred()
        if got:
            return got
        time.sleep(step)
    return None


def _texts(el, depth=0, acc=None, maxd=8):
    """el 아래의 모든 글자를 위에서부터 모은다."""
    if acc is None:
        acc = []
    if _attr(el, "AXRole") in ("AXTextArea", "AXStaticText"):
        v = _attr(el, "AXValue")
        if v:
            acc.append(str(v))
    if depth < maxd:
        for c in (_attr(el, "AXChildren") or []):
            _texts(c, depth + 1, acc, maxd)
    return acc


# ── 저수준 입력 (여기만 진짜 마우스·키보드) ──────────────────
def _post_mouse(kind, pt, clicks):
    e = Q.CGEventCreateMouseEvent(None, kind, pt, Q.kCGMouseButtonLeft)
    Q.CGEventSetIntegerValueField(e, Q.kCGMouseEventClickState, clicks)
    Q.CGEventPost(Q.kCGHIDEventTap, e)


def click(x, y, double=False):
    pt = Q.CGPointMake(float(x), float(y))
    Q.CGEventPost(Q.kCGHIDEventTap,
                  Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, pt, Q.kCGMouseButtonLeft))
    time.sleep(0.08)
    _post_mouse(Q.kCGEventLeftMouseDown, pt, 1); time.sleep(0.03)
    _post_mouse(Q.kCGEventLeftMouseUp, pt, 1)
    if double:
        time.sleep(0.05)
        _post_mouse(Q.kCGEventLeftMouseDown, pt, 2); time.sleep(0.03)
        _post_mouse(Q.kCGEventLeftMouseUp, pt, 2)
    time.sleep(0.1)


def key(code, cmd=False):
    flags = Q.kCGEventFlagMaskCommand if cmd else 0
    for down in (True, False):
        e = Q.CGEventCreateKeyboardEvent(None, code, down)
        Q.CGEventSetFlags(e, flags)
        Q.CGEventPost(Q.kCGHIDEventTap, e)
        time.sleep(0.008)


def scroll(x, y, amount, times):
    pt = Q.CGPointMake(float(x), float(y))
    Q.CGEventPost(Q.kCGHIDEventTap,
                  Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, pt, Q.kCGMouseButtonLeft))
    time.sleep(0.1)
    for _ in range(times):
        e = Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitPixel, 1, amount)
        Q.CGEventSetLocation(e, pt)
        Q.CGEventPost(Q.kCGHIDEventTap, e)
        time.sleep(0.03)


# ── 포커스 ──────────────────────────────────────────────────
_prev_app = None


def frontmost():
    a = NSWorkspace.sharedWorkspace().frontmostApplication()
    return a.localizedName() if a else None


def activate():
    """카톡을 앞으로 가져오고 창이 AX 에 보일 때까지 기다린다.

    ⚠️ 이 함수는 사용자에게서 포커스를 빼앗는다. 사용자가 다른 창에 타이핑하는 중이면
       그 글자가 카톡 입력칸으로 들어간다 — 최악의 경우 Enter 로 전송된다.
       그래서 모든 고수준 함수는 끝날 때 반드시 restore_focus()를 부른다.
    """
    global _prev_app
    cur = NSWorkspace.sharedWorkspace().frontmostApplication()
    if cur and cur.bundleIdentifier() != BUNDLE:
        _prev_app = cur
    _running().activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    if _until(lambda: _attr(_app(), "AXWindows"), timeout=3) is None:
        # 프로세스만 남고 모든 창을 닫은 상태. Dock 좌표를 누르지 않고 정확한
        # 번들 ID로 다시 열어 옆의 타이머 앱을 잘못 건드리지 않는다.
        subprocess.Popen(
            ["open", "-b", BUNDLE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if _until(lambda: _attr(_app(), "AXWindows"), timeout=6) is None:
            raise RuntimeError("카톡을 앞으로 가져왔는데도 창이 안 보인다")
    if not _in_login and _login_window() is not None:
        # 다른 기기(윈도우) 로그인으로 튕긴 상태 → send/read 등 무엇이든 로그인부터
        prev = _prev_app
        login()
        _prev_app = prev
        _running().activateWithOptions_(NSApplicationActivateIgnoringOtherApps)


def restore_focus():
    """카톡을 건드리기 직전에 쓰던 앱으로 포커스를 돌려준다."""
    global _prev_app
    if _prev_app is not None:
        _prev_app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    _prev_app = None


# ── 로그인 (2026-10-14) ──────────────────────────────────────
# PC 카톡은 한 대만 로그인된다 → 윈도우에서 들어가면 맥이 「로그인」 창으로 튕긴다.
# 비번은 키체인에만 둔다(서비스 kakaotalk-login). 파일·코드·출력·git 어디에도 남기지 않는다.
#   등록(사용자가 직접 친다): security add-generic-password -U -s kakaotalk-login -a kakaotalk -w
KEYCHAIN_SERVICE = "kakaotalk-login"
_in_login = False


def _keychain_password():
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            "키체인에 카톡 비번이 없다 — 사용자가 터미널에서 직접 등록해야 한다: "
            f"security add-generic-password -U -s {KEYCHAIN_SERVICE} -a kakaotalk -w")
    return r.stdout.rstrip("\n")


def _type_unicode(text):
    """키 입력으로 글자를 친다(클립보드 안 거침). 한/영 상태와 무관하게 유니코드로 들어간다."""
    for ch in text:
        for down in (True, False):
            e = Q.CGEventCreateKeyboardEvent(None, 0, down)
            Q.CGEventKeyboardSetUnicodeString(e, len(ch), ch)
            Q.CGEventPost(Q.kCGHIDEventTap, e)
            time.sleep(0.01)


def _all_elems(el, maxd=6):
    return list(_walk(el, maxd=maxd))


def _login_window():
    """비밀번호 칸(AXSecureTextField)이 있는 창. 없으면 None."""
    for w in _ax_windows():
        if any(_attr(e, "AXSubrole") == "AXSecureTextField" for e in _all_elems(w, 3)):
            return w
    return None


def _force_dialog():
    """「다른 기기에서 로그인 중입니다 … 강제 로그아웃」 확인창의 [확인] 버튼. 없으면 None."""
    for w in _ax_windows():
        els = _all_elems(w, 6)
        txt = " ".join(str(_attr(e, "AXValue") or _attr(e, "AXTitle") or "") for e in els)
        if "다른 기기" in txt or "강제 로그아웃" in txt:
            for e in els:
                if _attr(e, "AXRole") == "AXButton" and (_attr(e, "AXTitle") or "").strip() == "확인":
                    return e
    return None


def _list_ready():
    """★ 성공 판정 — 로그인 창이 사라진 것이 아니라 «채팅/친구 목록 행이 보이는 것»."""
    w = _win_el("카카오톡")
    if w is None or _login_window() is not None:
        return False
    for a in _scroll_areas(w):
        t = _table_of(a)
        if t is not None and (_attr(t, "AXRows") or []):
            return True
    return False


def logged_in():
    """로그인 창이 없으면 True (목록 창을 닫아둔 상태도 로그인으로 본다)."""
    return _login_window() is None


def login(timeout=30.0, shot_path="/tmp/kakao_login_unknown.png"):
    """맥 카톡 자동 로그인. 이미 로그인이면 바로 'already'.

    흐름: 비번 칸만 채운다(계정은 카톡이 기억한 값 그대로) → Enter →
          「다른 기기에서 로그인 중」 확인창이 뜨면 [확인] (상대 기기는 로그아웃된다 —
          이 명령을 부른 것 자체가 «여기서 쓰겠다»라서 묻지 않는다) → 목록 행이 보이면 성공.
    처음 보는 화면(휴대폰 인증 등)이면 멈추고 캡처 경로를 담아 RuntimeError.
    """
    global _in_login
    _in_login = True
    try:
        activate()
        win = _until(_login_window, timeout=2)
        if win is None:
            return "already"
        acct = next((e for e in _all_elems(win, 3)
                     if _attr(e, "AXRole") == "AXTextField"
                     and _attr(e, "AXSubrole") != "AXSecureTextField"), None)
        if acct is not None and not _attr(acct, "AXValue"):
            raise RuntimeError("카톡이 기억하는 계정이 비어 있다 — 계정 칸은 건드리지 않기로 했으니 멈춤")
        pw_field = next(e for e in _all_elems(win, 3)
                        if _attr(e, "AXSubrole") == "AXSecureTextField")
        btn = next((e for e in _all_elems(win, 3)
                    if _attr(e, "AXRole") == "AXButton" and (_attr(e, "AXTitle") or "") == "로그인"), None)
        g = _geo(pw_field)
        click(g[0] + g[2] // 2, g[1] + g[3] // 2)        # 포커스 (AXFocused 만으론 키가 안 갈 때 대비)
        _set(pw_field, "AXFocused", True)
        key(0, cmd=True)                                  # ⌘A — 남아 있던 글자 지우기
        key(51)                                           # Delete
        pw = _keychain_password()
        try:
            _type_unicode(pw)
        finally:
            pw = None
        if btn is not None and not _until(lambda: _attr(btn, "AXEnabled"), timeout=2):
            raise RuntimeError("비번을 쳤는데 「로그인」 버튼이 회색 그대로다 — 입력이 안 들어감")
        key(36)                                           # Enter

        end = time.perf_counter() + timeout
        pressed = False
        while time.perf_counter() < end:
            if _list_ready():
                return "ok (확인창 눌러 상대 기기 로그아웃)" if pressed else "ok"
            ok_btn = _force_dialog()
            if ok_btn is not None:
                AXUIElementPerformAction(ok_btn, "AXPress")
                pressed = True
                time.sleep(0.5)
                continue
            time.sleep(0.25)
        # 처음 보는 화면 — 캡처 후 멈춤
        try:
            title = _attr(_ax_windows()[0], "AXTitle") or "로그인"
            window_png(title, shot_path)
        except Exception:
            shot_path = "(캡처 실패)"
        texts = [t for w in _ax_windows() for t in _texts(w, maxd=6)]
        raise RuntimeError(f"{timeout:.0f}초 안에 목록이 안 보였다 — 처음 보는 화면. "
                           f"캡처={shot_path} 글자={texts[:12]}")
    finally:
        _in_login = False
        restore_focus()


# ── 창 ──────────────────────────────────────────────────────
def _ax_windows():
    return list(_attr(_app(), "AXWindows") or [])


def windows():
    """[(제목, x, y, w, h)] — activate() 뒤에 부를 것 (비활성이면 빈 리스트)."""
    out = []
    for w in _ax_windows():
        g = _geo(w)
        if g:
            out.append((_attr(w, "AXTitle") or "", *g))
    return out


def _win_el(title):
    for w in _ax_windows():
        if (_attr(w, "AXTitle") or "") == title:
            return w
    return None


def find_window(title):
    w = _win_el(title)
    if w is None:
        return None
    g = _geo(w)
    return (title, *g) if g else None


def list_window():
    """채팅 목록 창을 띄우고 좌표를 돌려준다."""
    activate()
    if _win_el("카카오톡") is None:
        # 창을 닫아도 프로세스는 남는다 → 창 메뉴 > 채팅 으로 다시 연다
        subprocess.run(["osascript", "-e",
                        f'tell application "System Events" to tell process "{APP}" to click '
                        f'menu item "채팅" of menu 1 of menu bar item "창" of menu bar 1'],
                       capture_output=True)
        if _until(lambda: _win_el("카카오톡"), timeout=6) is None:
            raise RuntimeError("채팅 목록 창을 못 열었다")
    return find_window("카카오톡")


# ── 방 구조 ─────────────────────────────────────────────────
def _scroll_areas(win):
    return [c for c in (_attr(win, "AXChildren") or []) if _attr(c, "AXRole") == "AXScrollArea"]


def _table_of(area):
    for c in (_attr(area, "AXChildren") or []):
        if _attr(c, "AXRole") == "AXTable":
            return c
    return None


def _room_input(win):
    """입력칸 AXTextArea — 메시지 테이블 «밖에» 있는 것. 없으면 None.

    ⚠️ 스크롤영역 인덱스로 찾지 않는다. 채팅 목록의 「조용한 채팅방」처럼
       채팅방이 아닌 창도 열리는데(2026-09-08 실제로 열렸다) 그건 스크롤영역이 1개다.
       테이블 안은 뒤지지 않는다 — 말풍선이 전부 AXTextArea 라서 오탐이고 느리다(3.5초).
    """
    for area in _scroll_areas(win):
        if _table_of(area) is not None:
            continue
        for c in (_attr(area, "AXChildren") or []):
            if _attr(c, "AXRole") == "AXTextArea":
                return c
    return None


def _room_parts(win):
    """(입력칸, 메시지 AXTable). 채팅방이 아니면 RuntimeError."""
    title = _attr(win, "AXTitle") or "?"
    inp = _room_input(win)
    if inp is None:
        raise RuntimeError(f"'{title}' 은 입력칸이 없다 — 채팅방 창이 아니다")
    tbl = next((_table_of(a) for a in _scroll_areas(win) if _table_of(a) is not None), None)
    return inp, tbl


# ── 고수준: 목록 읽기 ───────────────────────────────────────
def rooms(limit=None):
    """채팅 목록을 [(방이름, 참여자수, 마지막메시지)] 로 돌려준다 (79행 ≈ 100ms).

    ⚠️ 가운데 숫자는 **참여자 수**다 — 안 읽음이 아니다(2026-09-08 실측으로 정정.
       `104`·`34`·`1652` 를 «안 읽음»으로 읽어 «34건 밀렸다»고 잘못 보고한 적이 있다).
       안 읽음은 행 오른쪽 아래의 빨간 배지뿐이고 AX 에 없다 → `unread_badges()` 를 쓴다.
    ⚠️ 스크롤로 아직 안 불러온 방은 안 나온다. 「없다」고 단정하지 말 것.
    """
    lw = list_window()
    win = _win_el(lw[0])
    tbl = _table_of(_scroll_areas(win)[0])
    out = []
    for r in (_attr(tbl, "AXRows") or [])[:limit]:
        t = _texts(r)
        if not t:
            continue
        name = t[0]
        members = t[1] if len(t) > 2 and t[1].isdigit() else ""
        last = t[-1] if len(t) > 1 else ""
        out.append((name, members, last))
    return out


def refresh_room_index(limit=100):
    """대화 본문 없이 저장된 채팅방 이름·유형만 로컬 색인에 갱신한다."""
    activate()
    try:
        clear_search()
        lw = list_window()
        win = _win_el(lw[0])
        tbl = _table_of(_scroll_areas(win)[0])
        rows = list(_attr(tbl, "AXRows") or [])
        if limit is not None:
            rows = rows[:limit]
        indexed = []
        seen = set()
        for row in rows:
            name = _room_name(row)
            if not name or name in seen or name == "조용한 채팅방":
                continue
            seen.add(name)
            indexed.append({"name": name, "kind": _room_kind(row)})
        ROOM_INDEX_FILE.write_text(
            json.dumps(indexed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return indexed
    finally:
        restore_focus()


def self_room_name():
    """채팅 목록의 badge me로 나와의 채팅 방 이름을 찾는다."""
    activate()
    try:
        lw = list_window()
        win = _win_el(lw[0])
        tbl = _table_of(_scroll_areas(win)[0])
        for row in (_attr(tbl, "AXRows") or []):
            if any(_attr(el, "AXDescription") == "badge me" for el in _walk(row)):
                texts = _texts(row)
                if texts:
                    return texts[0]
        raise RuntimeError("채팅 목록에서 '나' 배지가 붙은 방을 못 찾았다")
    finally:
        restore_focus()


def read_room(room_title, n=15):
    """방의 마지막 n개 말풍선을 글자로 읽는다 — 스크린샷 없이. [[줄, 줄, …], …]"""
    win = _win_el(room_title)
    if win is None:
        activate(); win = _win_el(room_title)
    if win is None:
        raise RuntimeError(f"'{room_title}' 창이 없다")
    _, tbl = _room_parts(win)
    if tbl is None:
        return []
    # 테이블 끝에는 «글자 없는 빈 행»이 섞여 있다(구분자·여백). 넉넉히 훑어서 알맹이만 n개.
    rows = _attr(tbl, "AXRows") or []
    out = []
    for r in reversed(rows[-(n * 4 + 20):]):
        t = _texts(r)
        if t:
            out.append(t)
            if len(out) >= n:
                break
    return list(reversed(out))


# ── 고수준: 방 열기 ─────────────────────────────────────────
def raise_window(title):
    """창을 z순서 맨 앞으로. 다른 창이 덮고 있으면 클릭이 그 창으로 가버린다."""
    w = _win_el(title)
    if w is not None:
        AXUIElementPerformAction(w, "AXRaise")
        time.sleep(0.15)
    return w


def _open_by_list_row(query):
    """채팅 목록에서 별칭·호칭을 풀어 유일한 행을 연다. 못 찾으면 None."""
    lw = list_window()
    win = raise_window(lw[0]) or _win_el(lw[0])
    area = _scroll_areas(win)[0]
    ag = _geo(area)
    tbl = _table_of(area)
    rows = _attr(tbl, "AXRows") or []

    hit = _resolve_row(query, rows)
    if hit is None:
        return None

    row, name = hit
    g = _geo(row)
    # 화면 밖 행은 클릭해도 소용없다 → 검색 경로로 넘긴다
    if not g or not (ag[1] <= g[1] and g[1] + g[3] <= ag[1] + ag[3]):
        return None

    before = {w[0] for w in windows()}
    # AX 선택 + Return은 물리 마우스 포인터를 움직이지 않는다.
    # 선택을 받지 못하는 특수 행에서만 기존 더블클릭으로 되돌아간다.
    if _set(row, "AXSelected", True):
        key(0x24)
    else:
        click(g[0] + g[2] // 2, g[1] + g[3] // 2, double=True)
    new = _until(lambda: [w for w in windows() if w[0] not in before], timeout=6)
    if new:
        return new[0][0]
    # 이미 열려 있던 동명 창을 Return으로 올린 경우.
    return name if _win_el(name) is not None else None


def _open_by_search(query, search_term=None):
    """검색창 경로 — 목록에 안 보이는 방(스크롤 아래)용 대비책."""
    before = {w[0] for w in windows()}
    list_window()

    win = _win_el("카카오톡")
    btn = next((c for c in (_attr(win, "AXChildren") or [])
                if _attr(c, "AXRole") == "AXButton" and _attr(c, "AXDescription") == "검색"), None)
    if btn is None:
        raise RuntimeError("검색 버튼을 못 찾았다")
    AXUIElementPerformAction(btn, "AXPress")
    field = _until(lambda: next(
        (c for c in (_attr(win, "AXChildren") or []) if _attr(c, "AXRole") == "AXTextField"),
        None,
    ), timeout=2)
    if field is None:
        raise RuntimeError("채팅방 검색 입력칸을 못 열었다")
    _type_into(field, search_term or _person_core(query) or query)

    # 첫 결과를 맹목적으로 누르지 않는다. 호칭을 제거한 뒤에도 후보가
    # 하나일 때만 방향키로 선택하고, 선택된 이름을 다시 확인한 뒤 연다.
    area = _scroll_areas(win)[0]
    tbl = _table_of(area)
    hit = _until(
        lambda: _resolve_row(query, _attr(tbl, "AXRows") or []) if tbl else None,
        timeout=3,
        step=0.05,
    )
    if hit is None:
        return None
    row, name = hit
    rows = list(_attr(tbl, "AXRows") or [])
    index = next((i for i, candidate in enumerate(rows) if candidate == row), None)
    if index is None:
        return None
    _set(field, "AXFocused", True)
    for _ in range(index + 1):
        key(0x7D)                              # Down
    selected_name = _until(lambda: next(
        (_room_name(r) for r in (_attr(tbl, "AXSelectedRows") or []) if _room_name(r)),
        None,
    ), timeout=2)
    if selected_name != name:
        raise RuntimeError(f"검색 결과 선택 검증 실패: 기대={name!r}, 실제={selected_name!r}")
    key(0x24)                                  # Return
    new = _until(lambda: [w for w in windows() if w[0] not in before], timeout=6)
    return new[0][0] if new else None


def _is_open_chat(query):
    """room_index.local.json 에 kind "open" 으로 적힌 방이면 True."""
    try:
        rows = json.loads(ROOM_INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        return False
    return any(r.get("kind") == "open" and r.get("name") == query for r in rows)


def open_room(query):
    """방 이름으로 열고 창 제목을 돌려준다. 이미 열려 있으면 그 창을 그대로 쓴다."""
    original_query = query
    query = resolve_room_alias(query)
    activate()
    already = find_window(query)
    if already:
        restore_focus()
        return already[0]
    # 오픈채팅 방은 «채팅» 탭 목록·검색에 안 나온다 → 탭을 바꿔 가며 찾는다 (2026-10-13 사용자 지시:
    # 오픈채팅 방은 먼저 «오픈채팅» 탭을 누르고 찾는다). 오픈채팅으로 아는 방은 그 탭부터.
    tabs = ["오픈채팅", "채팅"] if _is_open_chat(query) else ["채팅", "오픈채팅"]
    title = None
    for tab in tabs:
        try:
            switch_tab(tab)
        except RuntimeError:
            pass
        title = _open_by_list_row(query)
        if not title:
            try:
                title = _open_by_search(
                    query,
                    search_term=_person_core(original_query) or original_query,
                )
            except RuntimeError:
                title = None
            clear_search()
        if title:
            break
    if tabs[0] == "오픈채팅" or title is None or tab == "오픈채팅":
        try:
            switch_tab("채팅")                 # 다음 호출을 위해 기본 탭으로 되돌린다
        except RuntimeError:
            pass
    # 엉뚱한 창(「조용한 채팅방」 폴더 등)을 열었을 수 있다 → 입력칸 유무로 판별
    if title and _room_input(_win_el(title)) is None:
        bad, title = title, None
    else:
        bad = None
    restore_focus()
    if not title:
        raise RuntimeError(f"'{query}' 방을 열지 못했다"
                           + (f" — 대신 '{bad}' 가 열렸다(채팅방이 아님)" if bad else ""))
    return title


# ── 경고창(시트) ────────────────────────────────────────────
def sheet_of(win):
    """창에 떠 있는 모달 시트를 돌려준다 (없으면 None).

    ⚠️ 카톡은 짧은 시간에 여러 통을 보내면 시트를 띄워 «막는다» — 서버 쪽 제한이다.
       실측 문구: "짧은 시간에 많은 메세지를 보낼 수 없습니다. 잠시 후 다시 시도해 주세요."
       시트가 떠 있는 동안은 붙여넣기도 Enter도 전부 무시된다(입력칸은 빈 채로 남는다).
    """
    for c in (_attr(win, "AXChildren") or []):
        if _attr(c, "AXRole") == "AXSheet":
            return c
    return None


def send_file(room_title, paths, verify=True, timeout=20.0):
    """방에 «파일»을 보낸다 (맥). 돌려주는 값: 보낸 파일명 리스트.

    맥 카톡에는 파일 첨부 «버튼»을 누를 필요가 없다 — 입력칸에 **파일을 붙여넣으면**
    「파일 전송」 시트가 뜨고 그 안의 `N개 전송` 을 누르면 나간다 (2026-09-09 실측).
    ⚠️ 윈도우 카톡과 절차가 다르다. 윈도우는 창 안 «종이(클립) 아이콘 → 파일 대화상자»
       좌표를 찾아 누르는 방식이고(스킬 `카톡전송`/`kakao.ps1`), 그 좌표는 맥에서 의미가 없다.
    """
    from AppKit import NSPasteboard, NSURL
    import os
    if isinstance(paths, str):
        paths = [paths]
    paths = [os.path.abspath(os.path.expanduser(p)) for p in paths]
    for p in paths:
        if not os.path.exists(p):
            raise FileNotFoundError(p)

    activate()
    win = _win_el(room_title)
    if win is None:
        room_title = open_room(room_title)
        activate()
        win = _until(lambda: _win_el(room_title), timeout=3)   # 로그인 직후엔 방 창이 AX 에 늦게 잡힌다 (2026-10-14)
    if win is None:
        raise RuntimeError(f"'{room_title}' 창이 없다")
    dismiss_sheet(win)

    inp, tbl = _room_parts(win)
    _set(inp, "AXFocused", True)
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    if not pb.writeObjects_([NSURL.fileURLWithPath_(p) for p in paths]):
        raise RuntimeError("클립보드에 파일을 못 올렸다")
    key(0x09, cmd=True)                                  # Cmd+V

    # 붙여넣기의 결과는 «입력칸 글자»가 아니라 「파일 전송」 시트다
    if not _until(lambda: sheet_of(win) is not None, timeout=5):
        raise RuntimeError("「파일 전송」 시트가 안 떴다 — 붙여넣기가 안 먹었다")
    sh = sheet_of(win)
    btn = next((c for c in (_attr(sh, "AXChildren") or [])
                if _attr(c, "AXRole") == "AXButton"
                and (_attr(c, "AXTitle") or "").endswith("개 전송")), None)
    if btn is None:
        raise RuntimeError(f"전송 버튼을 못 찾았다 — 시트 문구: {' '.join(_texts(sh))}")
    AXUIElementPerformAction(btn, "AXPress")
    if not _until(lambda: sheet_of(win) is None, timeout=10):
        raise RuntimeError("시트가 안 닫혔다 (전송이 접수되지 않았다)")

    names = [os.path.basename(p) for p in paths]
    if verify and tbl is not None:
        ok = _until(lambda: all(any(n in t for r in ((_attr(tbl, "AXRows") or [])[-4:])
                                    for t in _texts(r)) for n in names), timeout=timeout)
        if not ok:
            raise RuntimeError(f"보낸 걸 확인 못 했다: {names}")
    restore_focus()
    return names


def dismiss_sheet(win):
    """시트를 닫는다. 닫은 시트의 문구를 돌려준다. Escape 는 쓰지 않는다(창이 닫힌다)."""
    sh = sheet_of(win)
    if sh is None:
        return None
    msg = " ".join(_texts(sh))
    btn = next((c for c in _walk(sh) if _attr(c, "AXRole") == "AXButton"), None)
    if btn is not None:
        g = _geo(btn)
        if g:
            click(g[0] + g[2] // 2, g[1] + g[3] // 2)
            _until(lambda: sheet_of(win) is None, timeout=3)
    return msg


def _walk(el, depth=0, maxd=6):
    yield el
    if depth < maxd:
        for c in (_attr(el, "AXChildren") or []):
            yield from _walk(c, depth + 1, maxd)


def _message_bodies(row):
    """메시지 행 안의 본문 AXTextArea 값만 돌려준다."""
    return [
        str(_attr(el, "AXValue"))
        for el in _walk(row, maxd=6)
        if _attr(el, "AXRole") == "AXTextArea" and _attr(el, "AXValue")
    ]


def _message_row(tbl, old_text, lookback=120):
    """기존 전문 또는 고유한 일부 문구로 메시지 행과 본문 요소를 찾는다."""
    old_text = str(old_text or "").strip()
    if not old_text:
        raise ValueError("찾을 기존 문구가 비어 있다")
    rows = list(_attr(tbl, "AXRows") or [])[-lookback:]
    exact, partial = [], []
    for row in rows:
        for el in _walk(row, maxd=6):
            if _attr(el, "AXRole") != "AXTextArea":
                continue
            body = str(_attr(el, "AXValue") or "")
            if body == old_text:
                exact.append((row, el, body))
            elif old_text in body:
                partial.append((row, el, body))
    hits = exact or partial
    if not hits:
        raise RuntimeError(f"최근 메시지에서 기존 문구를 못 찾았다: {old_text[:40]!r}")
    if len(hits) > 1:
        previews = [body.replace("\n", " ")[:50] for _, _, body in hits]
        raise RuntimeError(
            f"기존 문구가 메시지 {len(hits)}개에 겹친다. 더 긴 문구로 지정: {previews}"
        )
    return hits[0]


def _context_menu_item(body_el, title):
    """AXShowMenu로 연 말풍선 메뉴에서 제목이 같은 항목을 찾는다."""
    return next(
        (
            el
            for el in _walk(body_el, maxd=4)
            if _attr(el, "AXRole") == "AXMenuItem"
            and (_attr(el, "AXTitle") or "") == title
        ),
        None,
    )


def _modify_input(win):
    """수정 모드의 입력칸. 일반 메시지 입력칸과 설명값으로 구분한다."""
    return next(
        (
            el
            for el in _walk(win, maxd=5)
            if _attr(el, "AXRole") == "AXTextArea"
            and _attr(el, "AXDescription") == "수정 메시지 입력"
        ),
        None,
    )


def _modify_button(win, title):
    return next(
        (
            el
            for el in _walk(win, maxd=5)
            if _attr(el, "AXRole") == "AXButton"
            and title in ((_attr(el, "AXTitle") or ""), (_attr(el, "AXDescription") or ""))
        ),
        None,
    )


def _cancel_modify(win):
    """수정 모드를 저장하지 않고 닫는다. Escape 는 창 자체를 닫으므로 쓰지 않는다."""
    btn = _modify_button(win, "닫기")
    if btn is not None:
        AXUIElementPerformAction(btn, "AXPress")
        _until(lambda: _modify_input(win) is None, timeout=2)


def edit(room_title, old_text, new_text, verify=True):
    """내가 보낸 메시지 한 건을 찾아 새 전문으로 수정한다.

    old_text는 기존 전문 또는 최근 메시지 중 하나만 식별하는 고유 문구여야 한다.
    상대 메시지이거나 수정 가능 시간이 지나면 우클릭 메뉴에 「수정」이 없으므로 중단한다.
    """
    new_text = str(new_text or "")
    if not new_text.strip():
        raise ValueError("새 메시지 본문이 비어 있다")

    activate()
    committed = False
    win = None
    try:
        win = _win_el(room_title)
        if win is None:
            room_title = open_room(room_title)
            activate()
            win = _until(lambda: _win_el(room_title), timeout=3)   # 로그인 직후엔 방 창이 AX 에 늦게 잡힌다 (2026-10-14)
        if win is None:
            raise RuntimeError(f"'{room_title}' 창이 없다")
        raise_window(room_title)
        _, tbl = _room_parts(win)
        if tbl is None:
            raise RuntimeError(f"'{room_title}' 대화 목록을 읽지 못했다")

        _, body_el, original = _message_row(tbl, old_text)
        if original == new_text:
            raise ValueError("기존 메시지와 새 메시지가 같다")
        if not _geo(body_el):
            raise RuntimeError("수정할 메시지가 현재 화면에 보이지 않는다")
        item = _context_menu_item(body_el, "수정")
        if item is None:
            if AXUIElementPerformAction(body_el, "AXShowMenu") != 0:
                raise RuntimeError("메시지 우클릭 메뉴를 열지 못했다")
            item = _until(
                lambda: _context_menu_item(body_el, "수정"), timeout=2, step=0.05
            )
        if item is None:
            # 메뉴를 닫되 Escape 는 쓰지 않는다.
            inp, _ = _room_parts(win)
            ig = _geo(inp)
            if ig:
                click(ig[0] + 8, ig[1] + 8)
            raise RuntimeError(
                "이 메시지는 수정할 수 없다 — 내가 보낸 메시지가 아니거나 수정 가능 시간이 지났다"
            )
        AXUIElementPerformAction(item, "AXPress")

        inp = _until(lambda: _modify_input(win), timeout=3, step=0.05)
        if inp is None:
            raise RuntimeError("「수정」을 눌렀지만 수정 입력칸이 열리지 않았다")
        loaded = str(_attr(inp, "AXValue") or "")
        if loaded != original:
            raise RuntimeError(
                f"수정 입력칸 원문이 다르다: 기대={original[:40]!r}, 실제={loaded[:40]!r}"
            )

        _set(inp, "AXFocused", True)
        whole = AXValueCreate(kAXValueCFRangeType, CFRangeMake(0, len(loaded.encode('utf-16-le')) // 2))   # 이모지는 UTF-16 2칸 (2026-10-15)
        if not _set(inp, "AXSelectedTextRange", whole):
            raise RuntimeError("수정 입력칸의 선택 범위를 설정하지 못했다")
        if not _until(lambda: (_attr(inp, "AXSelectedText") or "") == loaded, timeout=2):
            raise RuntimeError("수정 입력칸의 기존 본문을 전체 선택하지 못했다")
        _type_into(inp, new_text)
        if str(_attr(inp, "AXValue") or "") != new_text:
            raise RuntimeError("선택한 기존 본문을 새 전문으로 교체하지 못했다")

        btn = _modify_button(win, "수정")
        if btn is None:
            raise RuntimeError("수정 확정 버튼을 못 찾았다")
        AXUIElementPerformAction(btn, "AXPress")
        committed = True
        if not _until(lambda: _modify_input(win) is None, timeout=5):
            raise RuntimeError("수정 버튼을 눌렀지만 수정 모드가 닫히지 않았다")

        if verify:
            def edited_row():
                for row in reversed((_attr(tbl, "AXRows") or [])[-20:]):
                    if new_text in _message_bodies(row) and "수정됨" in _texts(row):
                        return row
                return None

            if _until(edited_row, timeout=5, step=0.05) is None:
                raise RuntimeError("수정된 본문과 「수정됨」 표시를 확인하지 못했다")
        return (original, new_text, True)
    finally:
        if win is not None and not committed and _modify_input(win) is not None:
            _cancel_modify(win)
        restore_focus()


def edit_self(old_text, new_text, verify=True):
    """나와의 채팅에서 내가 보낸 메시지 한 건을 수정한다."""
    return edit(self_room_name(), old_text, new_text, verify=verify)


# ── 고수준: 발송 ────────────────────────────────────────────
def paste_text(text):
    """클립보드는 원래 내용을 되돌려놓는다 (사용자 클립보드를 뺏지 않기 위해)."""
    old = subprocess.run(["pbpaste"], capture_output=True).stdout
    subprocess.run(["pbcopy"], input=text.encode())
    key(0x09, cmd=True)          # Cmd+V
    time.sleep(0.25)
    subprocess.run(["pbcopy"], input=old)


def _type_into(inp, text, timeout=3.0):
    """입력칸에 text 를 넣고 «실제로 들어갔는지» 확인한다.

    AXValue 직접 쓰기는 화면만 바뀌고 전송이 안 된다(2026-09-08 실측) → 붙여넣기를 쓴다.
    """
    _set(inp, "AXFocused", True)
    old = subprocess.run(["pbpaste"], capture_output=True).stdout
    try:
        subprocess.run(["pbcopy"], input=text.encode())
        key(0x09, cmd=True)
        ok = _until(lambda: text in (_attr(inp, "AXValue") or ""), timeout=timeout)
    finally:
        subprocess.run(["pbcopy"], input=old)
    if not ok:
        raise RuntimeError(f"입력칸에 글자가 안 들어갔다 (현재값 {_attr(inp,'AXValue')!r})")


def clear_input(room_title):
    """입력칸에 흘러든 글자를 지운다 (사용자가 다른 창에 치던 글자가 들어와 있을 수 있다)."""
    win = _win_el(room_title)
    if win is None:
        activate(); win = _win_el(room_title)
    if win is None:
        raise RuntimeError(f"'{room_title}' 창이 없다")
    inp, _ = _room_parts(win)
    if (_attr(inp, "AXValue") or "") == "":
        return ""
    stale = _attr(inp, "AXValue")
    _set(inp, "AXFocused", True)
    key(0x00, cmd=True); key(0x33)        # Cmd+A, Delete
    _until(lambda: (_attr(inp, "AXValue") or "") == "", timeout=2)
    return stale


def send(room_title, text, verify=True):
    """방에 메시지를 보내고 «입력칸이 비었는지»로 발송을 검증한다. (실측 ≈ 0.06초)"""
    return send_many(room_title, [text], verify=verify)[0]


def send_self(text, verify=True):
    """나와의 채팅으로 보내고 마지막 말풍선을 검증한다."""
    return send(self_room_name(), text, verify=verify)


def send_many(room_title, texts, verify=True, gap=1.0, cooldown=6.0):
    """여러 통을 한 번에 — 활성화·창찾기·포커스복구를 한 번만 한다.

    ⚠️ 여기서 빨라지는 데는 «카톡이 정한» 천장이 있다. 연달아 쏘면 카톡이
       "짧은 시간에 많은 메세지를 보낼 수 없습니다" 시트를 띄우고 그때부터 전부 무시한다
       (2026-09-08 실측: 간격 0.15초로 5통 → 2통만 나가고 막힘).
       그래서 기본 간격을 1초로 두고, 막히면 cooldown 만큼 쉬었다 한 번 더 시도한다.
       **여러 줄을 보낼 거면 여러 통이 아니라 «줄바꿈 넣은 한 통»이 훨씬 빠르고 안전하다.**

    돌려주는 값: 보낸 순서대로 [(보낸 글, 검증됐는지)] .
    """
    activate()
    win = _win_el(room_title)
    if win is None:
        room_title = open_room(room_title)
        activate()
        win = _until(lambda: _win_el(room_title), timeout=3)   # 로그인 직후엔 방 창이 AX 에 늦게 잡힌다 (2026-10-14)
    if win is None:
        raise RuntimeError(f"'{room_title}' 창이 없다")

    if dismiss_sheet(win):
        pass                                     # 이전에 남아 있던 경고창부터 치운다
    inp, tbl = _room_parts(win)
    results = []
    try:
        for i, text in enumerate(texts):
            if i:
                time.sleep(gap)
            sent, why = _send_one(win, inp, tbl, text, verify)
            if not sent and "짧은 시간" in (why or ""):
                # 카톡의 연속발송 제한 → 한 번만 쉬었다 다시
                time.sleep(cooldown)
                sent, why = _send_one(win, inp, tbl, text, verify)
            if not sent:
                raise RuntimeError(f"보낸 걸 확인 못 했다: {text[:30]!r}"
                                   + (f" — 카톡 안내: {why}" if why else ""))
            results.append((text, sent))
    finally:
        restore_focus()
    return results


def _send_one(win, inp, tbl, text, verify):
    """(보냈는지, 카톡이 띄운 경고문구). 예외를 던지지 않고 사실만 돌려준다."""
    try:
        _type_into(inp, text)
    except RuntimeError:
        return False, dismiss_sheet(win)
    key(0x24)                                        # Return
    if not _until(lambda: (_attr(inp, "AXValue") or "") == "", timeout=5):
        return False, dismiss_sheet(win)
    head = text.strip().splitlines()[0][:30]
    if not (verify and tbl is not None and head):
        return True, None
    ok = _until(lambda: any(head in t for r in ((_attr(tbl, "AXRows") or [])[-4:])
                            for t in _texts(r)), timeout=3)
    return (True, None) if ok else (False, dismiss_sheet(win))


# ── 캡처 ────────────────────────────────────────────────────
def shot(room_title, path, top_offset=88, bottom_margin=110):
    """방 창의 대화 영역만 캡처한다. (글자만 필요하면 read_room 이 훨씬 싸다)"""
    activate()
    w = find_window(room_title)
    if w is None:
        restore_focus()
        raise RuntimeError(f"'{room_title}' 창이 없다")
    _, wx, wy, ww, wh = w
    region = f"{wx},{wy+top_offset},{ww},{wh-top_offset-bottom_margin}"
    subprocess.run(["screencapture", "-x", "-o", "-R", region, path], check=True)
    restore_focus()
    return path


def reactions(room_title, out_png):
    """리액션 칩은 AX로 안 잡힌다 → 캡처해서 좌표를 눈으로 잡는 수밖에 없다.
    말풍선 아래 칩 줄: 💗3 ✅26 👍1 😊+(새 리액션) 👤(누른 사람 목록)
    파란 칩 = 내가 이미 누른 것. 확실치 않으면 👤을 눌러 명단에서 내 이름을 찾는다."""
    return shot(room_title, out_png)


def close_popup(room_title):
    """리액션 팝업 등은 Escape로 닫지 않는다(창이 통째로 닫힌다). 바깥을 클릭한다."""
    activate()
    w = find_window(room_title)
    click(w[1] + 40, w[2] + 200)
    restore_focus()


# ── 목록 탭 · 검색창 ────────────────────────────────────────
def _find_button(el, desc):
    if _attr(el, "AXRole") == "AXButton" and _attr(el, "AXDescription") == desc:
        return el
    for c in (_attr(el, "AXChildren") or []):
        r = _find_button(c, desc)
        if r:
            return r
    return None


def switch_tab(name):
    """목록 창의 «채팅» / «오픈채팅» 탭을 누른다. 오픈채팅 방은 채팅 탭에 안 보인다."""
    list_window()
    win = raise_window("카카오톡")
    b = _find_button(win, name)
    if b is None:
        raise RuntimeError(f"'{name}' 탭 버튼을 못 찾았다")
    if AXUIElementPerformAction(b, "AXPress") != 0:   # 마우스 안 움직이는 쪽 먼저
        g = _geo(b)
        click(g[0] + g[2] // 2, g[1] + g[3] // 2)
    time.sleep(0.8)
    return True


def clear_search():
    """목록 검색창에 남은 글자를 지운다 (남아 있으면 rooms() 가 걸러진 목록을 준다)."""
    win = _win_el("카카오톡")
    if win is None:
        return False
    field = next((c for c in (_attr(win, "AXChildren") or [])
                  if _attr(c, "AXRole") == "AXTextField"), None)
    if field is None or not (_attr(field, "AXValue") or ""):
        return False
    cancel = next((c for c in (_attr(field, "AXChildren") or [])
                   if _attr(c, "AXRole") == "AXButton"
                   and _attr(c, "AXDescription") == "취소"), None)
    if cancel is None:
        return False
    AXUIElementPerformAction(cancel, "AXPress")
    return bool(_until(lambda: not (_attr(field, "AXValue") or ""), timeout=2))


# ── 리액션 칩 좌표 ──────────────────────────────────────────
def window_png(title, path):
    """창 내용을 그대로 찍는다 — 다른 창이 덮고 있어도 된다. 이미지 좌표 = 창 기준 포인트(1:1)."""
    pid = _running().processIdentifier()
    lst = Q.CGWindowListCopyWindowInfo(
        Q.kCGWindowListOptionAll, Q.kCGNullWindowID)
    wid = next((w['kCGWindowNumber'] for w in lst
                if w.get('kCGWindowOwnerPID') == pid
                and (w.get('kCGWindowName') or '') == title), None)
    if wid is None:
        raise RuntimeError(f"'{title}' 창 id 를 못 찾았다")
    subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), path], check=True)
    return path


def chip_points(row_geo, n=1):
    """말풍선 행의 리액션 칩 좌표 (실측 규칙). [(x, y)] — 왼쪽 칩부터.

    칩 줄은 행의 오른쪽 아래에 붙는다: 👤 목록 = right-35, 😊+ = right-72,
    그 왼쪽으로 칩이 폭 44 씩. 칩 1개면 중심 ≈ right-90.
    """
    x, y, w, h = row_geo
    right, bottom = x + w, y + h
    return [(right - 90 - 44 * i, bottom - 12) for i in range(n)][::-1]


def reaction_list_point(row_geo):
    """👤(리액션 명단) 버튼 좌표 — 읽기 전용이라 눌러도 안전하다."""
    x, y, w, h = row_geo
    return (x + w - 35, y + h - 12)

# ── 안 읽음 배지 ────────────────────────────────────────────
def unread_badges(limit=None, png=None):
    """[(방이름, 안읽음수)] — **빨간 배지가 있는 방만**. 없으면 빈 리스트.

    ⚠️ 목록 행의 숫자(`104`, `34`, `1652`)는 **참여자 수**지 안 읽음이 아니다.
       안 읽음은 행 오른쪽 아래의 **빨간 동그라미**뿐이고, AX 에는 안 실린다 →
       창을 찍어 빨간 픽셀을 찾는다 (2026-09-08 실측: 배지 없는 방을 «34건 안 읽음»으로 오인했다).
    """
    import Quartz as _Q
    from Foundation import NSURL
    lw = list_window()
    win = raise_window(lw[0]) or _win_el(lw[0])
    wx, wy, _, _ = _geo(win)
    path = png or "/tmp/_kt_list.png"
    window_png(lw[0], path)
    src = _Q.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
    img = _Q.CGImageSourceCreateImageAtIndex(src, 0, None)
    buf = bytes(_Q.CGDataProviderCopyData(_Q.CGImageGetDataProvider(img)))
    bpr = _Q.CGImageGetBytesPerRow(img)
    bpp = _Q.CGImageGetBitsPerPixel(img) // 8
    W, H = _Q.CGImageGetWidth(img), _Q.CGImageGetHeight(img)

    tbl = _table_of(_scroll_areas(win)[0])
    out = []
    for r in (_attr(tbl, "AXRows") or [])[:limit]:
        t = _texts(r)
        g = _geo(r)
        if not t or not g:
            continue
        # 행 오른쪽 30% · 아래 절반에서 «빨간» 픽셀을 센다
        x0, x1 = int(g[0] - wx + g[2] * 0.70), min(int(g[0] - wx + g[2]), W)
        y0, y1 = int(g[1] - wy + g[3] * 0.45), min(int(g[1] - wy + g[3]), H)
        red = 0
        for py in range(max(y0, 0), max(y1, 0), 2):
            for px in range(max(x0, 0), max(x1, 0), 2):
                o = py * bpr + px * bpp
                rd, gr, b = buf[o], buf[o + 1], buf[o + 2]   # RGBA
                if rd > 200 and gr < 130 and b < 95:
                    red += 1
        if red >= 12:
            out.append((t[0], red))
    return out


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "open":
        print(open_room(sys.argv[2]))
    elif cmd == "send":
        print(send(sys.argv[2], sys.argv[3]))
    elif cmd == "login":
        print(login())
    elif cmd == "send-self":
        print(send_self(sys.argv[2]))
    elif cmd == "edit":
        print(edit(sys.argv[2], sys.argv[3], sys.argv[4]))
    elif cmd == "edit-self":
        print(edit_self(sys.argv[2], sys.argv[3]))
    elif cmd == "index":
        print(len(refresh_room_index(int(sys.argv[2]) if len(sys.argv) > 2 else 100)))
    elif cmd == "read":
        for line in read_room(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 15):
            print(" | ".join(line))
    elif cmd == "rooms":
        activate()
        for n, u, last in rooms(int(sys.argv[2]) if len(sys.argv) > 2 else 20):
            print(f"{n}\t{u}\t{last[:40]}")
        restore_focus()
    elif cmd == "shot":
        print(shot(sys.argv[2], sys.argv[3]))
    elif cmd == "clear":
        activate(); print(repr(clear_input(sys.argv[2]))); restore_focus()
    elif cmd == "scrollup":
        activate()
        w = find_window(sys.argv[2])
        scroll(w[1] + w[3] // 2, w[2] + w[4] // 2, 120, int(sys.argv[3]))
        restore_focus(); print("ok")
    elif cmd == "windows":
        activate()
        for w in windows():
            print(w)
        restore_focus()
