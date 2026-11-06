# -*- coding: utf-8 -*-
"""쿨메신저로 받은 문서를 프린터로 보낸다.

  python printfile.py "<파일>" --copies 30
  python printfile.py "<파일1>" "<파일2>" --copies 1 --duplex one
  python printfile.py "<파일>" --dry-run     # 어떤 경로로 인쇄할지만 출력

프린터는 환경변수 COOL_PRINTER → --printer → 윈도우 기본 프린터 순으로 고른다.
Excel·한글 COM은 ActivePrinter 인자를 조용히 무시하므로
**기본 프린터를 먼저 바꾸고** 인쇄한다 — 그러지 않으면 다른 프린터로 나가고도
오류가 안 난다(실제로 그 사고가 있었다).
"""
import argparse
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def _default_printer() -> str:
    """COOL_PRINTER 환경변수 → 윈도우 기본 프린터. 둘 다 없으면 그 자리에서 멈춘다."""
    env = os.environ.get("COOL_PRINTER")
    if env:
        return env
    got = ps("(Get-CimInstance Win32_Printer | Where-Object Default -eq $true).Name")
    if got:
        return got.splitlines()[0].strip()
    raise SystemExit(
        "프린터를 못 찾았습니다. 환경변수 COOL_PRINTER 에 프린터 이름을 넣거나 "
        'python printfile.py "<파일>" --printer "프린터이름" 으로 알려주세요.'
    )
ACROBAT = r"C:\Program Files\Adobe\Acrobat DC\Acrobat\Acrobat.exe"


def ps(cmd: str) -> str:
    # 한글 프린터 이름이 깨지지 않게 PowerShell 출력 인코딩을 UTF-8로 못 박는다.
    full = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + cmd
    r = subprocess.run(["powershell", "-NoProfile", "-Command", full],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (r.stdout or "").strip()


def set_default_printer(name: str):
    """윈도우가 '마지막에 쓴 프린터'를 기본으로 되돌리므로 인쇄 직전에 매번 지정한다."""
    ps("(New-Object -ComObject WScript.Network).SetDefaultPrinter('%s')" % name)
    time.sleep(0.4)
    dev = ps(r"(Get-ItemProperty 'HKCU:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows').Device")
    if not dev.startswith(name):
        raise SystemExit("기본 프린터를 %s 로 못 바꿨다 (현재: %s)" % (name, dev))
    return dev


def get_duplex(name):
    return ps("(Get-PrintConfiguration -PrinterName '%s').DuplexingMode" % name)


def set_duplex(name, mode):
    ps("Set-PrintConfiguration -PrinterName '%s' -DuplexingMode %s" % (name, mode))


# ── 형식별 인쇄 ──────────────────────────────────────────────────────────────

def print_hwp(path, printer, copies):
    from pyhwpx import Hwp
    hwp = Hwp(new=True, visible=False)
    try:
        hwp.open(path)
        act = hwp.CreateAction("Print")
        pset = act.CreateSet()
        act.GetDefault(pset)
        pset.SetItem("PrinterName", printer)
        pset.SetItem("Range", hwp.PrintRange("All"))
        pset.SetItem("Copies", copies)
        pset.SetItem("Collate", 1)
        act.Execute(pset)
        time.sleep(1.5)
    finally:
        try:
            hwp.clear(option=1)
            hwp.quit()
        except Exception:
            pass


def acrobat_pids():
    out = ps("Get-Process Acrobat -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty Id")
    return {int(x) for x in out.split() if x.strip().isdigit()}


def wait_for_job(printer, doc_hint, timeout=120):
    """스풀러에 그 문서 작업이 잡힐 때까지 기다린다."""
    end = time.time() + timeout
    while time.time() < end:
        names = ps("Get-PrintJob -PrinterName '%s' -ErrorAction SilentlyContinue | "
                   "Select-Object -ExpandProperty DocumentName" % printer)
        if doc_hint[:20] in names:
            return True
        time.sleep(1.0)
    return False


def print_pdf(path, printer, copies):
    """Acrobat /t 는 인쇄 뒤에도 창이 남아 subprocess.run 이 영원히 안 끝난다.
    → Popen 으로 띄우고, 스풀러에 작업이 잡히면 '우리가 띄운 인스턴스만' 닫는다."""
    if not os.path.exists(ACROBAT):
        raise SystemExit("Acrobat을 못 찾았다: %s" % ACROBAT)
    base = os.path.basename(path)
    for _ in range(copies):
        before = acrobat_pids()
        subprocess.Popen([ACROBAT, "/n", "/t", path, printer])
        if not wait_for_job(printer, base):
            print("  ! 스풀러에서 작업을 못 봤다 — 인쇄됐는지 직접 확인할 것")
        time.sleep(1.5)
        for pid in acrobat_pids() - before:
            ps("Stop-Process -Id %d -Force -ErrorAction SilentlyContinue" % pid)
        time.sleep(0.8)


def print_excel(path, printer, copies):
    import win32com.client as w
    xl = w.Dispatch("Excel.Application")
    xl.Visible = False
    xl.DisplayAlerts = False
    try:
        wb = xl.Workbooks.Open(os.path.abspath(path), ReadOnly=True)
        wb.PrintOut(Copies=copies)   # ActivePrinter 인자는 주지 않는다 (무시된다)
        time.sleep(1.5)
        wb.Close(False)
    finally:
        xl.Quit()


def print_word(path, printer, copies):
    import win32com.client as w
    wd = w.Dispatch("Word.Application")
    wd.Visible = False
    try:
        doc = wd.Documents.Open(os.path.abspath(path), ReadOnly=True)
        doc.PrintOut(Copies=copies)
        time.sleep(2.0)
        doc.Close(False)
    finally:
        wd.Quit()


def print_image(path, printer, copies):
    for _ in range(copies):
        subprocess.run(["mspaint", "/pt", os.path.abspath(path), printer], timeout=120)
        time.sleep(0.8)


HANDLERS = {
    ".hwp": print_hwp, ".hwpx": print_hwp,
    ".pdf": print_pdf,
    ".xlsx": print_excel, ".xls": print_excel, ".xlsm": print_excel,
    ".docx": print_word, ".doc": print_word,
    ".jpg": print_image, ".jpeg": print_image, ".png": print_image, ".bmp": print_image,
}


def main():
    ap = argparse.ArgumentParser(description="받은 문서 인쇄")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--copies", type=int, default=1)
    ap.add_argument("--printer", default=None)
    ap.add_argument("--duplex", choices=["one", "two"], default=None,
                    help="one=단면, two=양면(기본값은 프린터 현재 설정 유지)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.printer:
        args.printer = _default_printer()

    jobs = []
    for f in args.files:
        p = os.path.abspath(f)
        if not os.path.exists(p):
            raise SystemExit("파일이 없다: %s" % p)
        ext = os.path.splitext(p)[1].lower()
        if ext not in HANDLERS:
            raise SystemExit("인쇄 방법을 모르는 형식이다: %s" % ext)
        jobs.append((p, ext))
        print("· %s  (%s, %d부)" % (os.path.basename(p), ext, args.copies))

    if args.dry_run:
        print("\n[dry-run] 프린터: %s" % args.printer)
        return

    dev = set_default_printer(args.printer)
    print("\n기본 프린터 확인: %s" % dev)

    restore = None
    if args.duplex:
        restore = get_duplex(args.printer)
        set_duplex(args.printer, "OneSided" if args.duplex == "one" else "TwoSidedLongEdge")
        print("양면 설정: %s → %s" % (restore, args.duplex))

    try:
        for p, ext in jobs:
            print("인쇄 중: %s" % os.path.basename(p))
            HANDLERS[ext](p, args.printer, args.copies)
            print("  보냄")
    finally:
        if restore:
            set_duplex(args.printer, restore)
            print("양면 설정 복원: %s" % restore)

    left = ps("Get-PrintJob -PrinterName '%s' | Measure-Object | %% Count" % args.printer)
    print("\n대기 중인 작업 %s개 (0이면 이미 프린터로 넘어간 것)" % (left or "?"))


if __name__ == "__main__":
    main()
