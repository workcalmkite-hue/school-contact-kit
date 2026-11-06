# -*- coding: utf-8 -*-
"""쿨메신저 받은 메시지 읽기 — 로컬 SQLite DB를 직접 조회한다.

쿨메신저는 받은/보낸 쪽지를 %LOCALAPPDATA%\\CoolMessenger\\Memo\\<이름>.udb
(SQLite3, UTF-8) 에 그대로 쌓아 둔다. UI를 열지 않고 여기서 읽는다.

  python cool.py list                     # 마지막 처리 이후(없으면 24시간) 받은 메시지
  python cool.py list --hours 18 --json
  python cool.py list --since 2026-09-02  # 그 날 00:00 이후
  python cool.py show 6534                # 한 건 전문
  python cool.py html --out <경로>        # 보기용 HTML (--summary <json> 로 요약 삽입)
  python cool.py mark 6544                # 여기까지 처리했다고 기록
  python cool.py staff                    # 주고받은 쪽지로 교직원 명단(이름·내선·업무) 만들기
  python cool.py who 3학년부장            # 명단에서 업무·이름으로 사람 찾기
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(SKILL_DIR, "state.json")
STAFF_PATH = os.path.join(SKILL_DIR, "우리학교_교직원.txt")

# 쿨메신저 「받은 파일 폴더」. 첨부를 저장하면 여기로 떨어진다.
# 내 PC 경로를 박아두지 않고 환경변수 COOL_DOWNLOAD_DIR 를 먼저 본다.
def _default_download_dir() -> str:
    """쿨메신저 [옵션]의 「받은 파일 폴더」. 환경변수 COOL_DOWNLOAD_DIR 가 먼저다.

    환경변수가 없으면 바탕화면의 「쿨첨부」 폴더를 쓴다. 쿨메신저 옵션이 다른 곳을
    가리키고 있으면 그 경로를 COOL_DOWNLOAD_DIR 에 넣어야 첨부를 찾는다.
    """
    env = os.environ.get("COOL_DOWNLOAD_DIR")
    if env:
        return env
    home = os.path.expanduser("~")
    for cand in ("Desktop", os.path.join("OneDrive", "Desktop"), "바탕 화면"):
        p = os.path.join(home, cand)
        if os.path.isdir(p):
            return os.path.join(p, "쿨첨부")
    return os.path.join(home, "쿨첨부")


DOWNLOAD_DIR = _default_download_dir()


# ── DB 열기 ──────────────────────────────────────────────────────────────────

def find_db() -> str:
    """Memo 폴더에서 현재 로그인 사용자의 .udb 를 고른다.

    같은 PC를 여러 사람이 쓰면 udb가 여러 개다. -wal 이 붙어 있는(=지금 열려 있는)
    것을 우선하고, 없으면 가장 최근에 수정된 것을 쓴다.
    """
    base = os.path.join(os.environ["LOCALAPPDATA"], "CoolMessenger", "Memo")
    cands = [os.path.join(base, f) for f in os.listdir(base) if f.endswith(".udb")]
    if not cands:
        raise SystemExit("쿨메신저 DB를 못 찾았다: " + base)
    live = [p for p in cands if os.path.exists(p + "-wal")]
    pool = live or cands
    return max(pool, key=os.path.getmtime)


def open_db(path: str) -> sqlite3.Connection:
    """WAL까지 통째로 임시폴더에 복사해 읽는다 (쿨메신저가 켜져 있어도 안전)."""
    tmp = tempfile.mkdtemp(prefix="coolmsg_")
    dst = os.path.join(tmp, "memo.udb")
    shutil.copy2(path, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            shutil.copy2(path + suffix, dst + suffix)
    conn = sqlite3.connect(dst)
    conn.row_factory = sqlite3.Row
    return conn


# ── 파싱 ────────────────────────────────────────────────────────────────────

def parse_sender(raw: str) -> dict:
    """'홍길동(123)안전(홍길동)' → {name:'홍길동', ext:'123', role:'안전'}

    쿨메신저 표기는 `이름(내선)업무(로그인명)`. 맨 끝 괄호는 로그인명이라 버린다.
    `김철수/이영희(456)인성상담부기획(김철수)`처럼 앞뒤 이름이 다른 경우도 있어서
    '마지막 괄호를 떼고 나머지를 파싱'하는 방식으로 처리한다.
    """
    raw = (raw or "").strip()
    head = raw
    if head.endswith(")"):
        cut = head.rfind("(")
        if cut > 0:
            head = head[:cut].strip()
    m = re.match(r"^([^(]+)\(([^)]*)\)(.*)$", head)
    if m:
        return {"name": m.group(1).strip(), "ext": m.group(2).strip(),
                "role": m.group(3).strip(), "raw": raw}
    return {"name": head or raw, "ext": "", "role": "", "raw": raw}


def parse_attachments(filepath: str) -> list:
    """FilePath 컬럼을 첨부 목록으로.

    형식: |N|합계;크기1;크기2;…|보낸이폴더|파일명|코드|보낸이폴더|파일명|코드|
    (폴더는 '보낸 사람 PC의 경로'라 우리에겐 의미 없다 — 파일명·크기만 쓴다)
    """
    if not filepath:
        return []
    parts = filepath.split("|")
    if len(parts) < 4:
        return []
    try:
        count = int(parts[1])
    except ValueError:
        return []
    sizes = [int(x) for x in parts[2].split(";") if x.strip().isdigit()]
    each = sizes[1:] if len(sizes) == count + 1 else sizes
    out = []
    rest = parts[3:]
    for i in range(count):
        base = i * 3
        if base + 1 >= len(rest):
            break
        name = rest[base + 1]
        size = each[i] if i < len(each) else None
        out.append({"name": name, "size": size})
    return out


# 인용 머리글: '…님이 보낸글 >>2026/09/01 19:07:27 (화)'
# 보낸사람 표기가 잘려 오는 경우가 있어(예: '304)교무부장님이 보낸글') 이름 부분은 안 본다.
_QUOTE_RE = re.compile(r">>\s*\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}")


def clean_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    out, blank = [], 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(ln)
    return "\n".join(out).strip()


def split_quote(text: str):
    """인용된 이전 대화를 본문에서 떼어낸다. (본문, 인용) 튜플."""
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if _QUOTE_RE.search(ln):
            return "\n".join(lines[:i]).strip(), "\n".join(lines[i:]).strip()
    return text, ""


def attachment_status(att: dict) -> dict:
    """첨부가 이미 「받은 파일 폴더」에 내려와 있는지."""
    path = os.path.join(DOWNLOAD_DIR, att["name"])
    if os.path.exists(path):
        actual = os.path.getsize(path)
        return {"saved": True, "path": path, "local_size": actual,
                "size_match": (att["size"] is None or actual == att["size"])}
    return {"saved": False, "path": path, "local_size": None, "size_match": False}


# ── 조회 ────────────────────────────────────────────────────────────────────

def parse_date(s: str):
    try:
        return datetime.strptime((s or "")[:19], "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(d: dict):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def fetch(since: datetime = None, after_key: int = None, limit: int = 200,
          keys: list = None) -> list:
    conn = open_db(find_db())
    rows = conn.execute(
        "select MessageKey, Title, Sender, ReceiveDate, FilePath, MessageText, "
        "IsUnRead, MessageType from tbl_recv "
        "where DeletedDate is null order by MessageKey desc limit 2000"
    ).fetchall()
    out = []
    for r in rows:
        key = r["MessageKey"]
        if keys is not None and key not in keys:
            continue
        dt = parse_date(r["ReceiveDate"])
        if keys is None:
            if after_key is not None and key <= after_key:
                continue
            if since is not None and (dt is None or dt < since):
                continue
        atts = parse_attachments(r["FilePath"])
        for a in atts:
            a.update(attachment_status(a))
        body, quote = split_quote(clean_text(r["MessageText"]))
        # MessageType 28 = 본문·제목·첨부가 모두 빈 시스템 흔적. 브리핑에서 제외한다.
        if not body and not quote and not atts and not (r["Title"] or "").strip():
            continue
        out.append({
            "key": key,
            "date": r["ReceiveDate"],
            "dt": dt.isoformat(sep=" ") if dt else None,
            "sender": parse_sender(r["Sender"]),
            "preview": (r["Title"] or "").strip(),
            "body": body,
            "quote": quote,
            "attachments": atts,
            "unread": bool(r["IsUnRead"]),
        })
        if len(out) >= limit:
            break
    out.reverse()  # 오래된 것부터 (읽는 순서대로)
    return out


def resolve_window(args):
    """--since / --hours / 상태파일 중 무엇으로 자를지 결정."""
    if args.since:
        s = args.since
        fmt = "%Y-%m-%d %H:%M" if " " in s else "%Y-%m-%d"
        return datetime.strptime(s, fmt), None
    if args.hours:
        return datetime.now() - timedelta(hours=args.hours), None
    st = load_state()
    if st.get("last_key"):
        return None, int(st["last_key"])
    return datetime.now() - timedelta(hours=24), None


# ── 출력 ────────────────────────────────────────────────────────────────────

def fmt_size(n):
    if not n:
        return ""
    return "%.0f KB" % (n / 1024) if n < 1048576 else "%.1f MB" % (n / 1048576)


def print_list(msgs, full=False):
    if not msgs:
        print("새로 온 메시지 없음")
        return
    print("새 메시지 %d건\n" % len(msgs))
    for m in msgs:
        s = m["sender"]
        head = "[%s] %s  %s" % (m["key"], m["date"][:16], s["name"])
        if s["role"]:
            head += " (%s)" % s["role"]
        if m["unread"]:
            head += "  ●안읽음"
        print(head)
        body = m["body"] if full else m["body"][:400]
        for ln in body.split("\n"):
            print("    " + ln)
        if not full and len(m["body"]) > 400:
            print("    … (show 로 전문)")
        for a in m["attachments"]:
            mark = "✔저장됨" if a["saved"] else "✖미저장"
            print("    [첨부] %s (%s) %s" % (a["name"], fmt_size(a["size"]), mark))
        print()


# ── HTML ────────────────────────────────────────────────────────────────────

def esc(s):
    return (str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def nl2br(s):
    return esc(s).replace("\n", "<br>")


def file_url(path):
    return "file:///" + path.replace("\\", "/").replace(" ", "%20")


HTML_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#fff;color:#374151;
  font-family:"Pretendard","Malgun Gothic",-apple-system,sans-serif;
  font-size:15px;line-height:1.7;word-break:keep-all;overflow-wrap:break-word;
  text-wrap:pretty}
.wrap{max-width:940px;margin:0 auto;padding:36px 28px 80px}
h1{font-size:26px;color:#1f2328;margin:0 0 4px;letter-spacing:-.02em}
.sub{color:#5e6773;font-size:13px;margin-bottom:32px}
h2{font-size:18px;color:#1f2328;margin:38px 0 14px;padding-bottom:8px;
  border-bottom:2px solid #eceff3}
.lead{max-width:790px}
.box{border:1px solid #e3e6ea;border-radius:10px;padding:16px 18px;margin:0 0 12px;background:#fff}
.box.blue{background:#f0f6ff;border-color:#cddffb}
.box.green{background:#f2fbf5;border:2px solid #34a853}
.tag{display:inline-block;font-size:12px;font-weight:700;padding:2px 9px;border-radius:999px;
  background:#1f52b8;color:#fff;margin-right:8px;vertical-align:2px}
.tag.j{background:#0a6c3c}
.tag.p{background:#8a3a00}
.msg{border:1px solid #e3e6ea;border-radius:10px;padding:14px 18px 16px;margin-bottom:14px}
.msg.hasfile{border-left:4px solid #1f52b8}
.mh{display:flex;justify-content:space-between;align-items:baseline;gap:12px;
  margin-bottom:8px;flex-wrap:wrap}
.who{font-weight:700;color:#1f2328;font-size:15px}
.who span{font-weight:400;color:#5e6773;font-size:13px;margin-left:6px}
.when{color:#5e6773;font-size:12.5px;white-space:nowrap}
.body{max-width:790px;font-size:14.5px}
details.quote{margin-top:10px;padding:9px 12px;background:#f6f7f9;border-radius:7px;
  color:#5e6773;font-size:12.5px;white-space:pre-wrap}
details.quote summary{cursor:pointer;color:#1a4fb4;font-size:12.5px;white-space:normal}
.files{margin-top:12px;display:flex;flex-wrap:wrap;gap:8px}
a.dl{display:inline-flex;align-items:center;gap:8px;text-decoration:none;
  background:#0a6c3c;color:#fff;padding:7px 13px;border-radius:7px;font-size:13px;font-weight:600}
a.dl .u{font-weight:400;opacity:.85;font-size:11.5px}
span.miss{display:inline-flex;align-items:center;gap:8px;background:#fff7f5;
  border:1px solid #f5c9bd;color:#8a3a00;padding:7px 13px;border-radius:7px;
  font-size:13px;font-weight:600}
span.miss .u{font-weight:400;font-size:11.5px}
ul{padding-left:20px;margin:8px 0}
li{margin:5px 0}
.empty{color:#5e6773;padding:20px 0}
@media print{
  body{font-size:11pt}
  .wrap{padding:0;max-width:none}
  a.dl{background:#fff;color:#0a6c3c;border:1px solid #0a6c3c}
  a.dl::after{content:" (" attr(href) ")";font-size:9pt;font-weight:400}
  details.quote[open]{display:block}
}
"""

WEEKDAY = "월화수목금토일"


def build_html(msgs, summary=None, title_date=None):
    d = title_date or datetime.now()
    head = "%s (%s)" % (d.strftime("%Y-%m-%d"), WEEKDAY[d.weekday()])
    n_file = sum(1 for m in msgs if m["attachments"])
    p = ["<!doctype html><html lang='ko'><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         "<title>쿨 브리핑 %s</title><style>%s</style></head><body><div class='wrap'>"
         % (esc(head), HTML_CSS),
         "<h1>쿨메신저 브리핑</h1>",
         "<div class='sub'>%s · 메시지 %d건 · 첨부 있는 것 %d건</div>"
         % (esc(head), len(msgs), n_file)]

    if summary:
        notices = summary.get("notices") or []
        if notices:
            p.append("<h2>학생들에게 전달 (조회·종례)</h2>")
            for it in notices:
                when = it.get("when", "조종례")
                cls = "j" if ("종" in when and "조" not in when) else ""
                p.append("<div class='box blue'><span class='tag %s'>%s</span><b>%s</b>"
                         "<div class='body' style='margin-top:8px'>%s</div></div>"
                         % (cls, esc(when), esc(it.get("title", "")), nl2br(it.get("body", ""))))
        todos = summary.get("todos") or []
        if todos:
            p.append("<h2>내가 해야 할 일</h2><div class='box'><ul>")
            for it in todos:
                due = (" <b style='color:#1a4fb4'>· %s</b>" % esc(it["due"])) if it.get("due") else ""
                note = ("<br><span style='color:#5e6773;font-size:13px'>%s</span>"
                        % esc(it["note"])) if it.get("note") else ""
                p.append("<li>%s%s%s</li>" % (esc(it.get("title", "")), due, note))
            p.append("</ul></div>")
        prints = summary.get("prints") or []
        if prints:
            p.append("<h2>출력물</h2>")
            for it in prints:
                p.append("<div class='box green'><span class='tag p'>인쇄</span><b>%s</b> — %s부"
                         "<div style='color:#5e6773;font-size:13px;margin-top:4px'>%s</div></div>"
                         % (esc(it.get("file", "")), esc(it.get("copies", "")), esc(it.get("why", ""))))
        if summary.get("note"):
            p.append("<h2>메모</h2><div class='box lead'>%s</div>" % nl2br(summary["note"]))

    p.append("<h2>받은 메시지 원문</h2>")
    if not msgs:
        p.append("<div class='empty'>새로 온 메시지가 없다.</div>")
    for m in reversed(msgs):  # 최신이 위로
        s = m["sender"]
        role = ("<span>%s</span>" % esc(s["role"])) if s["role"] else ""
        cls = " hasfile" if m["attachments"] else ""
        p.append("<div class='msg%s'><div class='mh'><div class='who'>%s%s</div>"
                 "<div class='when'>%s</div></div>"
                 % (cls, esc(s["name"]), role, esc(m["date"][:19])))
        p.append("<div class='body'>%s</div>" % nl2br(m["body"]))
        if m["quote"]:
            p.append("<details class='quote'><summary>이전 대화 보기</summary>%s</details>"
                     % esc(m["quote"]))
        if m["attachments"]:
            p.append("<div class='files'>")
            for a in m["attachments"]:
                sz = fmt_size(a["size"])
                if a["saved"]:
                    p.append("<a class='dl' href='%s'>[첨부] %s<span class='u'>%s</span></a>"
                             % (file_url(a["path"]), esc(a["name"]), esc(sz)))
                else:
                    p.append("<span class='miss'>[첨부] %s<span class='u'>%s · 아직 안 받음</span></span>"
                             % (esc(a["name"]), esc(sz)))
            p.append("</div>")
        p.append("</div>")

    p.append("</div></body></html>")
    return "\n".join(p)


# ── main ────────────────────────────────────────────────────────────────────

def collect_staff() -> list:
    """주고받은 쪽지에 찍힌 `이름(내선)업무` 표기를 모아 사람마다 가장 최근 것 하나를 남긴다.

    쿨메신저 조직도 트리는 직접 그린 화면이라 프로그램이 이름을 못 읽는다.
    대신 DB의 보낸 사람·받는 사람·회원(tbl_member) 칸에 같은 표기가 남아 있어 여기서 모은다.
    쪽지를 한 번도 주고받지 않은 분은 빠진다.
    """
    conn = open_db(find_db())
    seen = {}

    def add(raw, when):
        for part in re.split(r"[;,]", raw or ""):
            part = part.strip()
            if not re.match(r"^[^()]+\(\d+\)", part):
                continue
            p = parse_sender(part)
            if not p["name"] or p["name"] == "전체":
                continue
            old = seen.get(p["name"])
            if old is None or (when or "") >= (old["when"] or ""):
                seen[p["name"]] = {"name": p["name"], "ext": p["ext"], "role": p["role"], "when": when or ""}

    for r in conn.execute("SELECT MemberName FROM tbl_member"):
        add(r[0], "")
    for r in conn.execute("SELECT Sender, ReceiveDate FROM tbl_recv"):
        add(r[0], str(r[1] or ""))
    for r in conn.execute("SELECT Receiver, ReferenceList, CCList, SendDate FROM tbl_send"):
        for col in r[:3]:
            add(col, str(r[3] or ""))
    return sorted(seen.values(), key=lambda x: (x["role"], x["name"]))


def load_staff() -> list:
    if not os.path.exists(STAFF_PATH):
        raise SystemExit("명단 파일이 없다. 먼저 python -X utf8 cool.py staff 를 실행해.")
    out = []
    with open(STAFF_PATH, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            name, ext, role = (line.rstrip("\n").split("\t") + ["", ""])[:3]
            out.append({"name": name, "ext": ext, "role": role})
    return out


def main():
    ap = argparse.ArgumentParser(description="쿨메신저 받은 메시지 읽기")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(x):
        x.add_argument("--since", help="YYYY-MM-DD 또는 'YYYY-MM-DD HH:MM' 이후")
        x.add_argument("--hours", type=int, help="최근 N시간")
        x.add_argument("--limit", type=int, default=200)

    q = sub.add_parser("list"); common(q)
    q.add_argument("--json", action="store_true")
    q.add_argument("--full", action="store_true", help="본문 전문 출력")

    q = sub.add_parser("show"); q.add_argument("keys", nargs="+", type=int)
    q.add_argument("--json", action="store_true")

    q = sub.add_parser("html"); common(q)
    q.add_argument("--out", required=True)
    q.add_argument("--summary", help="요약 JSON 파일 경로")

    q = sub.add_parser("mark"); q.add_argument("key", type=int)
    sub.add_parser("state")
    q = sub.add_parser("staff"); q.add_argument("--out", default=STAFF_PATH)
    q = sub.add_parser("who"); q.add_argument("words", nargs="+")

    args = ap.parse_args()

    if args.cmd == "state":
        st = load_state()
        print(json.dumps(st, ensure_ascii=False, indent=2) if st else "기록 없음")
        return

    if args.cmd == "staff":
        people = collect_staff()
        out = os.path.abspath(args.out)
        with open(out, "w", encoding="utf-8") as f:
            f.write("# 이름\t내선\t업무 — 쿨메신저 쪽지에서 모은 명단 (%s). 학교 밖으로 내보내지 말 것\n"
                    % datetime.now().strftime("%Y-%m-%d %H:%M"))
            for p in people:
                f.write("%s\t%s\t%s\n" % (p["name"], p["ext"], p["role"]))
        roles = {p["role"] for p in people if p["role"]}
        print("저장: %s  (%d명 · 업무 %d가지)" % (out, len(people), len(roles)))
        return

    if args.cmd == "who":
        people = load_staff()
        words = [w.replace("선생님", "").replace("님", "").replace("께", "").strip() for w in args.words]
        words = [w for w in words if w]
        hits = [p for p in people if all(w in p["name"] + " " + p["role"] for w in words)]
        if not hits:
            print("못 찾음: %s  (명단 %d명 중)" % (" ".join(words), len(people)))
        for p in hits:
            print("%s (%s) %s" % (p["name"], p["ext"], p["role"]))
        return

    if args.cmd == "mark":
        st = load_state()
        st["last_key"] = args.key
        st["last_run"] = datetime.now().isoformat(sep=" ", timespec="seconds")
        save_state(st)
        print("기록: last_key=%d" % args.key)
        return

    if args.cmd == "show":
        msgs = fetch(keys=args.keys, limit=len(args.keys))
        if args.json:
            print(json.dumps(msgs, ensure_ascii=False, indent=2))
        else:
            print_list(msgs, full=True)
        return

    since, after_key = resolve_window(args)
    msgs = fetch(since=since, after_key=after_key, limit=args.limit)

    if args.cmd == "list":
        if args.json:
            print(json.dumps(msgs, ensure_ascii=False, indent=2))
        else:
            src = ("--since %s" % since.strftime("%Y-%m-%d %H:%M")) if since \
                else ("MessageKey > %s" % after_key)
            print("# 기준: " + src)
            print_list(msgs, full=args.full)
        return

    if args.cmd == "html":
        summary = None
        if args.summary:
            with open(args.summary, encoding="utf-8") as f:
                summary = json.load(f)
        html = build_html(msgs, summary)
        out = os.path.abspath(args.out)
        d = os.path.dirname(out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print("저장: %s  (%d건, %d bytes)" % (out, len(msgs), len(html)))


if __name__ == "__main__":
    main()
