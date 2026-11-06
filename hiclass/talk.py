#!/usr/bin/env python3
"""하이톡(하이클래스) 읽기·보내기 — 창 없이(헤드리스) 혼자 도는 프로그램. 선생님 배포판.

처음 한 번:  python talk.py --login   (창이 뜨면 평소 하시던 방법으로 하이클래스에 로그인)
별칭(«학부모» 같은 짧은 이름)은 같은 폴더 rooms.json 에 적는다 — 없어도 방 이름 그대로 쓰면 된다.

playwright-mcp 를 안 거친다. 그래서 마우스를 뺏지 않고, 명령 한 줄로 끝난다.
(상벌점 mcp/ealimi/points.py 도 이제 헤드리스다 — 이 줄은 예전에 잘못 적혀 있었다. 하이톡도 마찬가지 —
 2026-09-08 실측: 로그인 유지된 채 /hitalk 정상 로드, Incapsula 봇 차단 안 걸림)

이 파일에는 학생·학부모 이름을 적지 않는다. 방 별칭은 rooms.json 에만 둔다.

사용:
  python3 mcp/hiclass/talk.py --room 학생   --msg "내일 준비물은 실습복입니다" --dry
  python3 mcp/hiclass/talk.py --room 학부모 --msg "$(cat 안내문.txt)"
  python3 mcp/hiclass/talk.py --room 학부모 --msg "..." --images 1쪽.png 2쪽.png --doc 안내.pdf
      # 글 → 사진 → 문서. 문서는 넣는 순간 발송되고 방에 파일 이름이 그대로 보인다.
      # 쓰기를 막고 문서 첨부를 시험하면 실패 말풍선이 남는다 — 재전송 말고 X → 「삭제」
  python3 mcp/hiclass/talk.py --rooms                 # 방 목록만 보기
  python3 mcp/hiclass/talk.py --read 학생 -n 20       # 방 안 대화 20건 읽기

--dry 는 입력창에 실제로 타이핑만 하고 멈춘다. 누구에게 무엇이 갈지 확인용.
"""
import argparse, json, os, re, sys, time
from pathlib import Path

# 하이클래스 전용 크롬 프로필을 따로 둔다 — 다른 자동화와 같이 쓰면 «already in use» 로 죽는다.
if sys.platform == "win32":
    _local = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))
    DEFAULT_PROFILE = str(Path(_local) / "ms-playwright-hiclass")
    DEFAULT_PROFILE_DIR = "Default"
else:
    DEFAULT_PROFILE = str(Path.home() / "Library/Caches/ms-playwright-hiclass")
    DEFAULT_PROFILE_DIR = "Default"
PROFILE = os.environ.get("HICLASS_PROFILE", DEFAULT_PROFILE)
PROFILE_DIR = os.environ.get("HICLASS_PROFILE_DIR", DEFAULT_PROFILE_DIR)

# 별칭 -> (방 이름 정확히, 기대 인원). 인원이 다르면 발송 직전에 멈춘다 (잘못된 방으로 가는 것을 막는다).
# 같은 폴더 rooms.json 에 적는다. 예: {"학부모": ["○학년 ○반 학부모 단체 공지방", 30]}
_ROOMS_FILE = Path(__file__).with_name("rooms.json")
ROOMS = {}
if _ROOMS_FILE.is_file():
    ROOMS = {k: (v[0], v[1] if len(v) > 1 else None)
             for k, v in json.loads(_ROOMS_FILE.read_text(encoding="utf-8")).items() if not k.startswith("_")}

NORM = "s => s.replace(/\\u00a0/g,' ').replace(/\\s+/g,' ').trim()"


def norm(s):
    return " ".join(s.replace(" ", " ").split()).strip()


def open_hitalk(p, headless):
    ctx = p.chromium.launch_persistent_context(
        PROFILE, headless=headless, channel="chrome",
        viewport={"width": 1440, "height": 900},
        args=[f"--profile-directory={PROFILE_DIR}"])
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    # 항상 새로 연다. 「이미 하이톡이면 생략」 같은 최적화를 넣지 않는다 —
    # 앞 실행에서 열어둔 방·입력창에 남은 글자가 다음 발송에 딸려 들어간다.
    pg.goto("https://www.hiclass.net/hitalk", timeout=45000)
    # 로그인이 없으면 /hitalk 가 «/» (마케팅 홈)로 튕긴다 — /login 만 보면 30초 타임아웃이 난다.
    pg.wait_for_function(
        "() => !!localStorage.getItem('idToken') || location.pathname === '/'"
        " || /login/.test(location.pathname)",
        timeout=30000)
    if not pg.evaluate("() => !!localStorage.getItem('idToken')"):
        # 사람을 부르기 전에 엣지에서 토큰을 옮겨와 본다 (평소 쓰는 브라우저에 로그인이 살아 있다)
        data = grab_from_edge(p)
        if not data:
            ctx.close()
            sys.exit("하이클래스 로그인이 없다. 엣지에서 하이클래스에 한 번 로그인해 두면"
                     " 그다음부터는 자동으로 가져온다.\n"
                     f"  (프로필: {PROFILE})")
        put_tokens(pg, data)
        pg.goto("https://www.hiclass.net/hitalk", timeout=45000)
        pg.wait_for_timeout(2500)
        if not pg.evaluate("() => !!localStorage.getItem('idToken')"):
            ctx.close()
            sys.exit("엣지에서 토큰을 가져왔는데도 로그인이 안 붙는다 — 엣지에서 하이클래스를 다시 열어 볼 것")
    # 왼쪽 레일 「대화목록」 (기본은 「대화상대」라 방 목록이 안 보인다)
    pg.wait_for_function(
        "() => !![...document.querySelectorAll('button')].find(b => b.title === '대화목록')",
        timeout=30000)
    pg.evaluate("() => [...document.querySelectorAll('button')]"
                ".find(b => b.title === '대화목록').click()")
    return ctx, pg


# 방 목록에서 «같은 이름의 행 전부» 를 위에서부터 순서대로 집는다.
#
# ⚠️ 이름이 같은 방이 여러 개 있다. 기간 채팅방은 기간이 끝나면 새로 만들어져서,
#    「○학년 ○반 단체 채팅방」이 기간 끝난 것과 지금 쓰는 것 두 개로 남아 있다.
#    종료된 방을 열면 입력창 자체가 없다 — 그래서 «입력창이 있는 방» 을 찾을 때까지 훑는다.
#    (2026-09-08: 마지막 것을 집었다가 6/1 종료 방을 열어 30초 타임아웃이 났다)
#
# ⚠️ 좌표로 행을 고르지 않는다 — 헤드리스와 창모드는 레이아웃 x 가 다르다
#    (창모드 x=140, 헤드리스 x=80). 이름 텍스트를 찾은 뒤 클릭이 먹는 조상으로 올라간다.
ROWS = """(room) => {
  const rows = [];
  for (const e of document.querySelectorAll('div,li,span,p')) {
    if (e.children.length || (e.innerText || '').trim() !== room) continue;
    const lb = e.getBoundingClientRect();
    if (lb.x >= 430 || lb.width <= 40) continue;
    let n = e;
    for (let i = 0; i < 8 && n; i++, n = n.parentElement) {
      const b = n.getBoundingClientRect();
      if (b.width >= 250 && b.height >= 50 && b.height <= 170) { rows.push(n); break; }
    }
  }
  const uniq = [...new Set(rows)];
  uniq.sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
  return uniq;
}"""


# 대화목록 한 행의 innerText 는 이런 모양이다:
#   클래스 이미지 / ○학년 ○반 단체 채팅방 / 31 / 36분전 / (빈줄) / 마지막 메시지 / (빈줄) / 1(안읽음)
#   프로필 이미지 / OOO 학부모(OOO) /       56분전 / (빈줄) / 마지막 메시지 / (빈줄) / 1(안읽음)
# 그래서 «이미지» 줄을 버리고 앞에서부터 이름 → (인원) → 시각 → 본문 순으로 읽는다.
# ⚠️ 2026-10-15 하이클래스 화면이 바뀌었다 — «이미지» 줄이 없어지고 단체방은 «administrator», 1:1 방은
#    이름이 첫 줄이다. 둘 다 받게 «방 행 = 대화목록 칸의 LI»로 잡고, 머리 줄(이미지·administrator)만 떼어 낸다.
# ⚠️ 클릭 직후 바로 읽으면 「대화목록」 제목만 잡힌다 — 진짜 방 행이 뜰 때까지 기다려야 한다.
ROW_JS = """() => [...document.querySelectorAll('li,div')]
  .map(e => ({ e, b: e.getBoundingClientRect() }))
  .filter(o => o.b.x >= 40 && o.b.x < 460 && o.b.width > 250
            && o.b.height > 55 && o.b.height < 200)
  .filter(o => ROW_HEAD.test((o.e.innerText || '').split(String.fromCharCode(10))[0]) || o.e.tagName === 'LI')
  .filter(o => !/^대화목록/.test((o.e.innerText || '').trim()))
  .sort((a, b) => a.b.y - b.b.y)
  .map(o => { const l = (o.e.innerText || '').split(String.fromCharCode(10)).map(t => t.trim()).filter(Boolean);
              return ROW_HEAD.test(l[0] || '') ? l.slice(1) : l; })"""
ROW_JS = "() => { const ROW_HEAD = /이미지|^administrator$/; return (" + ROW_JS + ")(); }"


# 방 안 대화 읽기. 말풍선·보낸이·시각·날짜를 클래스 «모양» 으로 집는다.
#   말풍선 본문 : 부모 class 에 whitespace-pre-wrap  (둥근 말풍선 상자)
#   보낸이 이름 : font-medium + 부모 pl-12          (내 메시지엔 아예 없다)
#   시각        : leading-none 이고 텍스트가 07:55 꼴. 내 것이면 class 에 text-right
#   날짜 구분선 : 「2026년 9월 9일 수요일」
# ⚠️ 좌표로 나/상대를 가르지 않는다 — 헤드리스와 창모드의 x 가 다르다(ROWS 주석 참고).
#    시각 엘리먼트의 text-right 유무로 판단하고, 시각을 못 찾았을 때만 x 를 쓴다.
# ⚠️ 사진·파일 메시지는 텍스트가 없어 안 잡힌다. 「(사진)」으로도 안 나온다.
READ_JS = r"""(limit) => {
  const leaf = [...document.querySelectorAll('div,p,span')]
    .map(e => ({ e, b: e.getBoundingClientRect(), c: (e.className || '').toString(),
                 pc: (e.parentElement && e.parentElement.className || '').toString() }))
    .filter(o => o.b.x > 415 && o.b.y > 60 && o.e.children.length === 0
              && !o.e.closest('[contenteditable="true"]'))
    .map(o => Object.assign(o, {
           t: (o.e.innerText || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim() }))
    .filter(o => o.t);
  const bubbles = leaf.filter(o => /whitespace-pre-wrap/.test(o.pc));
  const names   = leaf.filter(o => /font-medium/.test(o.c) && /pl-12/.test(o.pc));
  const times   = leaf.filter(o => /^\d{1,2}:\d{2}$/.test(o.t) && /leading-none/.test(o.c));
  const dates   = leaf.filter(o => /^\d{4}년\s*\d{1,2}월\s*\d{1,2}일/.test(o.t));
  const last = (arr, y) => arr.filter(o => o.b.y <= y + 4)
                              .sort((a, b) => b.b.y - a.b.y)[0] || null;
  const out = bubbles.map(b => {
    let tm = null, best = 45;
    for (const t of times) {
      const d = Math.abs(t.b.bottom - b.b.bottom);
      if (d < best) { best = d; tm = t; }
    }
    const mine = tm ? /text-right/.test(tm.c) : b.b.x > 900;
    const nm = last(names, b.b.y), dt = last(dates, b.b.y);
    return { 날짜: dt ? dt.t : null, 시각: tm ? tm.t : null,
             보낸이: mine ? '나' : (nm ? nm.t : null), 내용: b.t, y: Math.round(b.b.y) };
  }).sort((a, b) => a.y - b.y);
  const seen = new Set(), uniq = [];
  for (const m of out) {
    const k = m.y + '|' + m.내용;
    if (seen.has(k)) continue;
    seen.add(k); uniq.push(m);
  }
  return limit ? uniq.slice(-limit) : uniq;
}"""

# 위로 올려 옛 메시지를 더 불러온다. 스크롤 되는 조상을 말풍선에서 거슬러 찾는다.
SCROLL_UP = r"""() => {
  const b = [...document.querySelectorAll('div,p,span')].find(
    e => !e.children.length && e.getBoundingClientRect().x > 415
      && /whitespace-pre-wrap/.test((e.parentElement && e.parentElement.className || '').toString()));
  if (!b) return false;
  for (let n = b; n; n = n.parentElement) {
    if (n.scrollHeight > n.clientHeight + 50) { n.scrollTop = 0; return true; }
  }
  return false;
}"""


def read_room(pg, limit=30):
    """열린 방의 대화를 [{날짜, 시각, 보낸이, 내용}] 로. 위로 스크롤해 limit 만큼 채운다."""
    # 말풍선이 다 그려질 때까지 (방을 열자마자 세면 0 이 나온다 — 실측 2.5초쯤)
    try:
        pg.wait_for_function(f"() => ({READ_JS})(0).length > 0", timeout=12000)
    except Exception:
        return []                       # 대화가 하나도 없는 새 방
    msgs = pg.evaluate(READ_JS, 0)
    for _ in range(12):                 # 12번이면 대략 수백 건. 무한루프는 만들지 않는다
        if len(msgs) >= limit or not pg.evaluate(SCROLL_UP):
            break
        pg.wait_for_timeout(1200)
        grown = pg.evaluate(READ_JS, 0)
        if len(grown) <= len(msgs):     # 더 안 늘면 맨 위까지 온 것
            msgs = grown
            break
        msgs = grown
    for m in msgs:
        m.pop("y", None)
    return msgs[-limit:] if limit else msgs


def read_list(pg):
    """대화목록 행을 [{이름, 인원, 시각, 마지막, 안읽음}] 로 돌려준다."""
    pg.wait_for_function(
        "() => [...document.querySelectorAll('li,div')].filter(e => {"
        " const b = e.getBoundingClientRect();"
        " return b.x >= 40 && b.x < 460 && b.width > 250 && b.height > 55 && b.height < 200"
        "   && (e.tagName === 'LI' || /이미지/.test((e.innerText || '').split(String.fromCharCode(10))[0])); }).length >= 2",
        timeout=30000)
    pg.wait_for_timeout(800)          # 나머지 행이 붙을 틈
    out = []
    for parts in pg.evaluate(ROW_JS):
        if not parts:
            continue
        name = parts[0]
        rest = parts[1:]
        people = None
        if rest and rest[0].isdigit() and len(rest[0]) <= 3:
            people = int(rest[0]); rest = rest[1:]
        when = rest[0] if rest else ""
        rest = rest[1:]
        unread = None
        if rest and rest[-1].isdigit() and len(rest[-1]) <= 3:
            unread = int(rest[-1]); rest = rest[:-1]
        last = " ".join(rest)
        out.append({"이름": name, "인원": people, "시각": when,
                    "안읽음": unread, "마지막": last})
    # 같은 행이 여러 겹으로 잡히면 이름+시각으로 한 번만 남긴다
    seen, uniq = set(), []
    for r in out:
        k = (r["이름"], r["시각"])
        if k in seen:
            continue
        seen.add(k); uniq.append(r)
    return uniq


def list_rooms(pg):
    return [r["이름"] for r in read_list(pg)]


# ── 로그인 토큰을 엣지에서 옮겨온다 ────────────────────────────────────────
# 구글은 playwright 가 띄운 크롬에서의 로그인을 막는다
# («브라우저 또는 앱이 안전하지 않을 수 있습니다» — 2026-09-09 윈도우에서 실제로 막혔다).
# 그래서 사람이 평소 쓰는 엣지의 하이클래스 로그인을 그대로 가져다 쓴다.
#
# 토큰은 쿠키가 아니라 localStorage(idToken·refreshToken)라 파일만 있으면 된다.
# 엣지를 닫을 필요도 없다 — Local Storage 의 leveldb 를 통째로 «복사»해서
# 그 복사본을 프로필 삼아 크롬 헤드리스로 열어 값을 읽는다.
EDGE_LS = (Path(os.environ.get("LOCALAPPDATA", "")) /
           "Microsoft/Edge/User Data/Default/Local Storage/leveldb")


# 맥은 엣지 대신 평소 쓰는 크롬 프로필에서 가져온다 (2026-10-22: playwright-mcp 프로필 로그인이
# 풀려 있었는데, 크롬 Profile 9 에 하이클래스 로그인이 살아 있었다). hiclass.net 이 든 프로필만 본다.
def _ls_candidates():
    if sys.platform == "win32":
        return [EDGE_LS]
    out = []
    for ls in sorted((Path.home() / "Library/Application Support/Google/Chrome").glob(
            "*/Local Storage/leveldb")):
        for f in ls.iterdir():
            if f.suffix in (".ldb", ".log"):
                try:
                    if b"hiclass.net" in f.read_bytes():
                        out.append(ls)
                        break
                except OSError:
                    pass
    return out


def grab_from_edge(p):
    """엣지(윈도우)·크롬(맥) 프로필에서 하이클래스 로그인을 찾아 온다. 실패하면 None."""
    for ls in _ls_candidates():
        data = _grab_from(p, ls)
        if data:
            return data
    return None


def _grab_from(p, ls_dir):
    """leveldb 폴더 하나에서 하이클래스 localStorage 를 통째로 읽어 온다. 실패하면 None."""
    import shutil, tempfile
    if not ls_dir.is_dir():
        return None
    tmp = Path(tempfile.mkdtemp(prefix="hiclass-edge-"))
    dst = tmp / "Default/Local Storage/leveldb"
    dst.mkdir(parents=True)
    try:
        for f in ls_dir.iterdir():
            if f.is_file() and f.name != "LOCK":
                try:
                    shutil.copy2(f, dst / f.name)
                except OSError:
                    pass
        ctx = p.chromium.launch_persistent_context(
            str(tmp), headless=True, channel="chrome",
            args=["--profile-directory=Default"])
        try:
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto("https://www.hiclass.net/", timeout=60000)
            pg.wait_for_timeout(2000)
            data = pg.evaluate("() => { const o = {};"
                               " for (const k of Object.keys(localStorage))"
                               " o[k] = localStorage.getItem(k); return o; }")
        finally:
            ctx.close()
        return data if data.get("idToken") else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def put_tokens(pg, data):
    pg.evaluate("(d) => { for (const [k, v] of Object.entries(d)) localStorage.setItem(k, v); }",
                data)


GOOGLE_ACCOUNT = os.environ.get("HICLASS_GOOGLE", "")


def do_login(p):
    """창을 띄워 하이클래스 로그인 화면을 열어 둔다 (기기당 한 번). HICLASS_GOOGLE 이 있으면 구글 로그인까지 데려다 준다.

    토큰은 쿠키가 아니라 localStorage 의 idToken/refreshToken 이라 프로필을 복사해도 안 따라온다.
    새 기기에서는 이 경로로 한 번 로그인해야 하고, 그다음부터는 이 프로필에 남아서 헤드리스로 돈다.

    ⚠️ 로그인 화면 경로: 홈의 「선생님, 교직원 로그인 하기」 → /login/teacher/v2 → 「구글로 시작하기」.
       /login 을 바로 열면 빈 화면이 뜬다 (2026-09-09 윈도우에서 실제로 겪음).
    """
    ctx = p.chromium.launch_persistent_context(
        PROFILE, headless=False, channel="chrome",
        viewport={"width": 1400, "height": 900},
        args=[f"--profile-directory={PROFILE_DIR}", "--window-position=200,60"])
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    pg.goto("https://www.hiclass.net/login/teacher/v2", timeout=60000)
    pg.wait_for_timeout(3000)
    if not GOOGLE_ACCOUNT:
        # 선생님마다 로그인 방법이 다르다(구글·카카오·네이버·아이디). 고르지 않고 창만 열어 둔다.
        print("창이 떴다 — 평소 하시던 방법으로 하이클래스 «선생님» 로그인을 하면 된다. 비밀번호는 창에만 친다.")
    else:
        pg.evaluate("""() => {
          const t = [...document.querySelectorAll('a,button,div,span,li')]
            .filter(x => !x.children.length && /구글로 시작하기/.test(x.innerText || ''));
          if (!t.length) return null;
          let n = t[0];
          for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
            const b = n.getBoundingClientRect();
            if (b.width >= 150 && b.height >= 30) { n.click(); return true; }
          }
          t[0].click(); return true; }""")
        pg.wait_for_timeout(5000)

        # 구글 계정 입력칸이 있으면 아이디까지 넣어 준다 (비밀번호는 사람이 친다)
        try:
            g = [q for q in ctx.pages if "accounts.google.com" in q.url]
            gp = g[-1] if g else pg
            if gp.locator("input[type=email]").count():
                gp.fill("input[type=email]", GOOGLE_ACCOUNT)
                gp.keyboard.press("Enter")
                print(f"구글 아이디({GOOGLE_ACCOUNT})까지 넣었다 — 창에서 비밀번호만 입력하면 된다.")
            else:
                print("창에서 구글 계정을 선택하면 된다.")
        except Exception:
            print("창에서 구글 로그인을 진행하면 된다.")

    print(f"(프로필: {PROFILE}) 최대 15분 기다린다…", flush=True)
    for _ in range(180):
        try:
            tgt = [q for q in ctx.pages if "hiclass.net" in q.url]
            tgt = tgt[-1] if tgt else ctx.pages[-1]
            if tgt.evaluate("() => !!localStorage.getItem('idToken')"):
                tgt.wait_for_timeout(2500)      # refreshToken 까지 떨어지게 잠깐 둔다
                print("로그인 완료 — idToken 확보. 이제 헤드리스로 돈다.")
                ctx.close()
                return True
        except Exception:
            if not ctx.pages:
                print("창이 닫혔다 — 다시 실행할 것")
                return False
        time.sleep(5)
    ctx.close()
    print("15분 안에 로그인이 확인되지 않았다.")
    return False


def open_room(pg, room, expect=None):
    """방을 열고 «정말 그 방인가» 까지 확인한다. (헤더정보, 인원) 을 돌려준다.

    보내기와 읽기가 같은 길을 쓴다 — 한쪽만 고쳐져서 어긋나는 일이 없도록.
    """
    # 방 열기 — 고정 sleep 대신 «그 방이 목록에 뜰 때까지»
    pg.wait_for_function(f"(room) => ({ROWS})(room).length > 0", arg=room, timeout=30000)
    n_rows = pg.evaluate(f"(room) => ({ROWS})(room).length", room)

    # 위에서부터 열어 보고, «입력창이 붙는 방» 을 만나면 거기서 멈춘다.
    # 방이 열렸다 = 입력창이 있고 «오른쪽 헤더» 에 그 방 이름이 떴다.
    # body.innerText 로 보면 왼쪽 목록에도 이름이 있어 항상 참이 된다 — 오른쪽만 본다.
    opened, tried = False, []
    for i in range(n_rows):
        pg.evaluate(f"([room, i]) => ({ROWS})(room)[i].click()", [room, i])
        try:
            pg.wait_for_function(
                """(room) => !!document.querySelector('[contenteditable="true"]')
                   && [...document.querySelectorAll('div,span,h1,h2')].some(e => {
                        const b = e.getBoundingClientRect();
                        const sq = t => (t || '').replace(/\\s+/g, '');
                return b.x > 415 && b.y < 100 && sq(e.innerText).includes(sq(room));
                      })""", arg=room, timeout=8000)
            opened = True
            break
        except Exception:
            tried.append(pg.evaluate(
                """(room) => { const h = [...document.querySelectorAll('div')]
                     .map(e => ({ e, b: e.getBoundingClientRect() }))
                     .filter(o => o.b.x > 415 && o.b.y < 120 && o.b.width > 100)
                     .map(o => (o.e.innerText || '').replace(/\\s+/g, ' ').trim())
                     .filter(t => t.replace(/\\s+/g, '').includes(room.replace(/\\s+/g, '')));
                   return h.length ? h[0].slice(0, 70) : '(헤더 없음)'; }""", room))
    if not opened:
        sys.exit(f"「{room}」 {n_rows}개를 다 열어봤지만 입력창이 없다 (전부 종료된 방).\n"
                 + "\n".join(f"  - {t}" for t in tried))

    # ── 나가는 동작 직전 확인: 「지금 열린 방이 정말 그 방인가」 ──────────
    # 헤더 후보 중 «가장 긴 것» 을 본다 — 짧은 조각에는 인원수 (31) 가 빠져 있다.
    state = pg.evaluate("""(room) => {
      const hdr = [...document.querySelectorAll('div,span,h1,h2')]
        .map(e => ({ e, b: e.getBoundingClientRect() }))
        .filter(o => o.b.x > 415 && o.b.y < 120 && o.b.width > 150)
        .map(o => (o.e.innerText || '').replace(/\\s+/g, ' ').trim())
        .filter(t => t.replace(/\\s+/g, '').includes(room.replace(/\\s+/g, '')));
      hdr.sort((a, b) => b.length - a.length);
      const head = hdr.length ? hdr[0] : '';
      return { head: head.slice(0, 140),
               count: (head.match(/\\((\\d+)\\)/) || [])[1] || null,
               end: (head.match(/종료일\\s*:\\s*(.+?)\\s*(?:종료|$)/) || [])[1] || null };
    }""", room)
    # ⚠️ 목록의 방 이름과 헤더의 방 이름이 «공백» 하나로 다르다.
    #    목록 「OOO 학부모(OOO)」 vs 헤더 「OOO 학부모 (OOO)」 —
    #    그대로 비교하면 1:1 방을 열어놓고도 «입력창이 없다» 로 죽는다 (2026-09-09).
    if room.replace(" ", "") not in state["head"].replace(" ", ""):
        sys.exit(f"열린 방이 다르다: {state['head']!r} (기대 {room!r})")
    cnt = int(state["count"]) if state["count"] else None
    if expect is not None and cnt != expect:
        sys.exit(f"인원 불일치: {cnt}명 (기대 {expect}명)\n"
                 f"  전입·전출이면 --expect {cnt} 로 덮어쓰고 talk.py 의 ROOMS 도 고칠 것")

    return state, cnt


# ── 사진·문서 첨부 (2026-09-15) ──────────────────────────────────────────
# 입력창 아래 버튼 「사진」「영상」「문서」 뒤에 숨은 input[type=file] 이 세 개 있다.
#   사진 accept="image/*,.heic,.heif" (multiple) · 영상 accept=".wmv,…" · 문서 accept=""
# ⚠️ 둘의 동작이 다르다 (쓰기 요청을 전부 막아 두고 실측):
#   사진 — 파일을 넣으면 편집 창(「1 / 2 · 취소 · 전송」)이 뜬다. 「전송」을 눌러야 나간다
#   문서 — 파일을 넣는 «순간» file.hiclass.net/multipart 로 올라가며 바로 발송된다. 확인 창 없음
# 그래서 문서는 --dry 에서 절대 넣지 않는다. 방에는 파일 «이름» 이 그대로 보이니 이름을 먼저 정리해 둔다.
FILE_INPUT = """(kind) => [...document.querySelectorAll('input[type=file]')].findIndex(e =>
  kind === 'image' ? /^image\\//.test(e.accept) : kind === 'video' ? /\\.mp4/.test(e.accept) : !e.accept)"""
# 영상 accept=".wmv,…,.mp4,…" (한 개씩) — 문서처럼 넣는 순간 바로 올라간다 (2026-09-18, 쓰기 요청 막고 실측)


def send_now(pg, kind, f):
    """영상·문서 — 넣는 순간 발송된다. 업로드 응답과 방에 뜬 파일 이름으로 확인한다."""
    idx = pg.evaluate(FILE_INPUT, kind)
    if idx < 0:
        sys.exit(f"{kind} 첨부 입력을 못 찾았다")
    with pg.expect_response(lambda r: "file.hiclass.net" in r.url and r.request.method == "POST",
                            timeout=180000) as resp:
        pg.locator("input[type=file]").nth(idx).set_input_files(f)
    if not resp.value.ok:
        sys.exit(f"{kind} 업로드 실패: {resp.value.status} ({Path(f).name})")
    pg.wait_for_timeout(3000)
    return f"{Path(f).name} 업로드 {resp.value.status}"


def send_attachments(pg, images, doc, videos=None):
    out = {}
    if images:
        idx = pg.evaluate(FILE_INPUT, "image")
        if idx < 0:
            sys.exit("사진 첨부 입력을 못 찾았다")
        with pg.expect_response(lambda r: "file.hiclass.net" in r.url and r.request.method == "POST",
                                timeout=60000) as resp:
            pg.locator("input[type=file]").nth(idx).set_input_files(images)
            pg.wait_for_function(
                "() => [...document.querySelectorAll('button')].some(b => (b.innerText||'').trim() === '전송')",
                timeout=15000)
            # 편집 창 머리의 「1 / 2」 = <span><em>1</em>&nbsp;/&nbsp;2</span>
            # 채팅 본문에 «2/3» 같은 글이 있을 수 있어 그 글자만 든 span 을 본다
            n = pg.evaluate(r"""() => { const e = [...document.querySelectorAll('span')].find(e =>
                     e.children.length <= 1 && /^\d+\s*\/\s*\d+$/.test((e.innerText||'').replace(/ /g,' ').trim()));
                   return e ? +e.innerText.replace(/ /g,' ').split('/')[1] : null; }""")
            if n != len(images):
                sys.exit(f"사진 편집 창 장수가 다르다: {n} (넣은 것 {len(images)})")
            pg.evaluate("() => [...document.querySelectorAll('button')]"
                        ".find(b => (b.innerText||'').trim() === '전송').click()")
        out["사진"] = f"{len(images)}장 업로드 {resp.value.status}"
        if not resp.value.ok:
            sys.exit(f"사진 업로드 실패: {resp.value.status}")
        pg.wait_for_timeout(3000)
    if doc:
        idx = pg.evaluate(FILE_INPUT, "doc")
        if idx < 0:
            sys.exit("문서 첨부 입력을 못 찾았다")
        with pg.expect_response(lambda r: "file.hiclass.net" in r.url and r.request.method == "POST",
                                timeout=60000) as resp:
            pg.locator("input[type=file]").nth(idx).set_input_files(doc)
        if not resp.value.ok:
            sys.exit(f"문서 업로드 실패: {resp.value.status}")
        name = Path(doc).name
        # 올라간 뒤 말풍선에 파일 이름이 뜨고, 실패하면 옆에 재전송·X 버튼이 붙는다
        pg.wait_for_function("(n) => document.body.innerText.includes(n)", arg=name, timeout=20000)
        pg.wait_for_timeout(3000)
        out["문서"] = f"{name} 업로드 {resp.value.status}"
    for v in videos or []:
        out.setdefault("영상", []).append(send_now(pg, "video", v))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", nargs="+", help="글을 보낸 뒤 사진으로 올릴 파일들 (편집 창 「전송」까지)")
    ap.add_argument("--doc", help="글·사진 뒤에 문서로 올릴 파일 하나 (넣는 순간 발송된다)")
    ap.add_argument("--videos", nargs="+", help="글·사진·문서 뒤에 영상으로 하나씩 올릴 파일들 (넣는 순간 발송된다)")
    ap.add_argument("--room", help="별칭(" + " | ".join(ROOMS) + ") 또는 방 이름 그대로")
    ap.add_argument("--msg", help="보낼 내용. 여러 줄이면 줄바꿈 그대로 유지")
    ap.add_argument("--expect", type=int, default=None, help="기대 인원 — 다르면 안 보낸다")
    ap.add_argument("--dry", action="store_true", help="입력창에 타이핑만 하고 멈춤 (발송 안 함)")
    ap.add_argument("--headed", action="store_true", help="창을 띄워서 실행 (재로그인·디버그용)")
    ap.add_argument("--rooms", action="store_true", help="방 목록만 출력")
    ap.add_argument("--inbox", action="store_true",
                    help="대화목록 전체 — 안읽음·시각·마지막 메시지까지")
    ap.add_argument("--unread", action="store_true", help="--inbox 중 안읽음 있는 것만")
    ap.add_argument("--read", nargs="?", const=True, metavar="방",
                    help="방 안 대화를 읽는다. --read 학생 또는 --room 학생 --read")
    ap.add_argument("-n", type=int, default=30, help="--read 로 읽을 개수 (기본 30)")
    ap.add_argument("--login", action="store_true",
                    help="창을 띄워 구글 로그인 (새 기기에서 한 번만)")
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright
    t0 = time.time()

    with sync_playwright() as p:
        if a.login:
            sys.exit(0 if do_login(p) else 1)
        try:
            ctx, pg = open_hitalk(p, headless=not a.headed)
        except Exception as e:
            if "already in use" in str(e):
                sys.exit("크롬 프로필을 playwright-mcp 가 쓰고 있다.\n"
                         "  그 브라우저를 닫고 다시 실행하거나, HICLASS_PROFILE 로 다른 프로필을 쓸 것")
            raise

        try:
            if a.inbox or a.unread:
                rows = read_list(pg)
                if a.unread:
                    rows = [r for r in rows if r["안읽음"]]
                print(json.dumps(rows, ensure_ascii=False, indent=1))
                print(f"({len(rows)}건 / {time.time() - t0:.1f}초)")
                return
            if a.read:
                if isinstance(a.read, str):
                    a.room = a.read
                if not a.room:
                    sys.exit("어느 방을 읽을지 알려줄 것 — --read 학생")
                room, expect = ROOMS.get(a.room, (a.room, None))
                if a.expect is not None:
                    expect = a.expect
                state, cnt = open_room(pg, room, expect)
                msgs = read_room(pg, a.n)
                print(json.dumps({"방": state["head"], "인원": cnt, "대화": msgs},
                                 ensure_ascii=False, indent=1))
                print(f"({len(msgs)}건 / {time.time() - t0:.1f}초)")
                return
            if a.rooms or not a.room:
                print(json.dumps({"방목록": list_rooms(pg)}, ensure_ascii=False, indent=1))
                return
            if not a.msg:
                sys.exit("--msg 가 필요하다")

            room, expect = ROOMS.get(a.room, (a.room, None))
            if a.expect is not None:
                expect = a.expect
            msg = a.msg.strip()
            if not msg:
                sys.exit("빈 메시지는 보내지 않는다")

            state, cnt = open_room(pg, room, expect)

            # ⚠️ 말풍선을 클래스명으로 찾지 않는다 — `text-b2` 는 이 방에서 0개였다
            #    (2026-09-08: 실제로 보내졌는데 «실패» 로 보고했다).
            #    채팅 영역의 «자식 없는 텍스트 노드» 를 본다. 입력창 안은 제외한다 —
            #    안 빼면 방금 타이핑한 글이 말풍선으로 세어져 검증이 어긋난다.
            bubbles = """() => [...document.querySelectorAll('div,p,span')]
              .map(e => ({ e, b: e.getBoundingClientRect() }))
              .filter(o => o.b.x > 415 && o.b.y > 100 && o.e.children.length === 0)
              .filter(o => !o.e.closest('[contenteditable="true"]'))
              .map(o => (o.e.innerText||'').replace(/\\u00a0/g,' ').replace(/\\s+/g,' ').trim())
              .filter(Boolean)"""
            # 말풍선이 다 그려진 뒤에 센다. 방을 열자마자 세면 목록이 아직 비어 있어
            # 0 이 나온다(실측 2.5초쯤 걸린다). 0 으로 세면 «이미 있던 같은 글» 을 못 보고
            # 발송 실패를 성공으로 잘못 읽는다 — 시각 말풍선이 뜰 때까지 기다린다.
            try:
                pg.wait_for_function(f"() => ({bubbles})().some(t => /^\\d{{1,2}}:\\d{{2}}$/.test(t))",
                                     timeout=12000)
            except Exception:
                pass  # 대화가 하나도 없는 새 방이면 0 이 맞다
            before = pg.evaluate(bubbles)
            # ⚠️ 통째로 같은 말풍선을 찾지 않는다 — 링크가 든 메시지는 <a> 로 쪼개져
            #    「내용 전체」와 같은 잎 노드가 아예 없다. 그래서 «실제로 보내졌는데 실패» 로
            #    보고했고, 다시 보내서 같은 글이 두 번 나갔다 (2026-09-16 실제 사고).
            #    첫 줄만 검증 열쇠(needle)로 쓴다 — 말풍선 한 개당 한 번만 나온다.
            # ⚠️ 링크는 말풍선에서 «https://» 가 빠지고 40자쯤에서 «...» 로 잘린다.
            #    본문과 링크가 한 말풍선에 섞이면 본문이 «자식 없는 잎» 에 안 들어간다.
            #    (2026-10-13: 링크 메시지를 «실패» 로 읽고 5번 다시 보냈다 → 지우느라 고생)
            #    그래서 scheme 을 떼고 앞 30자만 열쇠로 쓰고, 잎이 아니라 «가장 안쪽에서
            #    그 글을 품은 요소» 를 센다.
            first = next(l for l in msg.splitlines() if l.strip())
            needle = norm(re.sub(r"https?://", "", first))[:30]
            count_js = r"""(want) => [...document.querySelectorAll('div,p,span,a')]
              .filter(e => { const b = e.getBoundingClientRect(); return b.x > 415 && b.y > 100; })
              .filter(e => !e.closest('[contenteditable="true"]'))
              .filter(e => (e.innerText||'').replace(/\s+/g,' ').includes(want))
              .filter(e => ![...e.children].some(c => (c.innerText||'').replace(/\s+/g,' ').includes(want)))
              .length"""
            n_before = pg.evaluate(count_js, needle)

            # ── 입력 ─────────────────────────────────────────────────────────
            # Enter 를 누르지 않는다. contenteditable 이라 Enter 가 «전송» 일 수 있다.
            # execCommand('insertText') 는 input 이벤트만 내보내서 안전하다.
            pg.click('[contenteditable="true"]')
            pg.evaluate("""(t) => {
              const el = document.querySelector('[contenteditable="true"]');
              el.focus();
              document.execCommand('selectAll', false, null);
              document.execCommand('insertText', false, t);
            }""", msg)
            typed = pg.evaluate("() => document.querySelector('[contenteditable=\"true\"]').innerText")
            if norm(typed) != norm(msg):
                sys.exit(f"입력이 어긋났다\n  넣은 것: {norm(msg)}\n  들어간 것: {norm(typed)}")

            pg.wait_for_function(
                "() => { const b = [...document.querySelectorAll('button')]"
                ".find(b => (b.innerText||'').trim() === '보내기'); return b && !b.disabled; }",
                timeout=15000)

            result = {"방": state["head"], "인원": cnt, "종료일": state["end"], "내용": msg}

            for f in (a.images or []) + ([a.doc] if a.doc else []) + (a.videos or []):
                if not Path(f).is_file():
                    sys.exit(f"첨부 파일이 없다: {f}")
            if a.images or a.doc or a.videos:
                result["첨부"] = [Path(f).name for f in (a.images or []) + ([a.doc] if a.doc else []) + (a.videos or [])]

            if a.dry:
                result |= {"dry": True, "보냄": False, "같은내용_기존": n_before, "초": round(time.time() - t0, 1)}
                print(json.dumps(result, ensure_ascii=False, indent=1))
                return

            pg.evaluate("() => [...document.querySelectorAll('button')]"
                        ".find(b => (b.innerText||'').trim() === '보내기').click()")

            # 「보냈습니다」류 표시를 믿지 않는다 — 방 안에 그 말풍선이 실제로 하나 늘었는지 본다.
            ok = True
            try:
                pg.wait_for_function(
                    f"""([want, n]) => {{
                      const empty = !document.querySelector('[contenteditable="true"]').innerText.trim();
                      return empty && ({count_js})(want) > n;
                    }}""", arg=[needle, n_before], timeout=25000)
            except Exception:
                ok = False

            result |= {"보냄": ok,
                       "확인": f"{pg.evaluate(count_js, needle)}건"
                               f" (보내기 전 {n_before}건)",
                       "초": round(time.time() - t0, 1)}
            if not ok:
                print(json.dumps(result, ensure_ascii=False, indent=1))
                sys.exit(1)
            if a.images or a.doc or a.videos:
                result |= send_attachments(pg, a.images, a.doc, a.videos)
                result["초"] = round(time.time() - t0, 1)
            print(json.dumps(result, ensure_ascii=False, indent=1))
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
