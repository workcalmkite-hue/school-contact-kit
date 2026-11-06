import email
import imaplib
import os
import smtplib
from email.header import decode_header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

_ENV_PATH = Path(__file__).parent / ".env"
if _ENV_PATH.exists():
    for line in _ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())

NAVER_USER = os.environ["NAVER_USER"]
NAVER_APP_PASSWORD = os.environ["NAVER_APP_PASSWORD"]
IMAP_HOST = "imap.naver.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.naver.com"
SMTP_PORT = 465


def _connect() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(NAVER_USER, NAVER_APP_PASSWORD)
    return conn


def _from_address() -> str:
    return NAVER_USER if "@" in NAVER_USER else f"{NAVER_USER}@naver.com"


def send_mail(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    attachments: list[str] | None = None,
) -> dict:
    from_addr = _from_address()
    msg = MIMEMultipart()
    msg["From"] = formataddr((None, from_addr))
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    attached = []
    for path_str in attachments or []:
        path = Path(path_str).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"첨부파일을 찾을 수 없습니다: {path}")
        part = MIMEApplication(path.read_bytes())
        part.add_header(
            "Content-Disposition", "attachment", filename=("utf-8", "", path.name)
        )
        msg.attach(part)
        attached.append(path.name)

    recipients = [addr.strip() for addr in to.split(",")]
    if cc:
        recipients += [addr.strip() for addr in cc.split(",")]

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as conn:
        conn.login(NAVER_USER, NAVER_APP_PASSWORD)
        conn.sendmail(from_addr, recipients, msg.as_string())

    return {
        "status": "sent",
        "from": from_addr,
        "to": to,
        "cc": cc,
        "subject": subject,
        "attachments": attached,
    }


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            decoded += text.decode(enc or "utf-8", errors="replace")
        else:
            decoded += text
    return decoded


def _get_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "")
            if part.get_content_type() == "text/plain" and "attachment" not in disp:
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                return payload.decode(charset, errors="replace") if payload else ""
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                return payload.decode(charset, errors="replace") if payload else ""
        return ""
    charset = msg.get_content_charset() or "utf-8"
    payload = msg.get_payload(decode=True)
    return payload.decode(charset, errors="replace") if payload else ""


def _fetch_summaries(conn: imaplib.IMAP4_SSL, uids: list[bytes]) -> list[dict]:
    results = []
    for uid in uids:
        status, msg_data = conn.fetch(uid, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        body = _get_body(msg)
        snippet = " ".join(body.split())[:200]
        results.append({
            "uid": uid.decode(),
            "subject": _decode(msg.get("Subject")),
            "from": _decode(msg.get("From")),
            "date": msg.get("Date", ""),
            "snippet": snippet,
        })
    return results


def list_recent_mails(limit: int = 10, mailbox: str = "INBOX") -> list[dict]:
    conn = _connect()
    try:
        conn.select(mailbox, readonly=True)
        status, data = conn.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()[-limit:][::-1]
        return _fetch_summaries(conn, uids)
    finally:
        conn.logout()


def search_mails(query: str, limit: int = 10, mailbox: str = "INBOX", scan: int = 100) -> list[dict]:
    conn = _connect()
    try:
        conn.select(mailbox, readonly=True)
        status, data = conn.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()[-scan:][::-1]
        summaries = _fetch_summaries(conn, uids)
        q = query.lower()
        matched = [
            m for m in summaries
            if q in m["subject"].lower() or q in m["from"].lower() or q in m["snippet"].lower()
        ]
        return matched[:limit]
    finally:
        conn.logout()


def _list_attachment_parts(msg: email.message.Message) -> list[email.message.Message]:
    parts = []
    if not msg.is_multipart():
        return parts
    for part in msg.walk():
        disp = str(part.get("Content-Disposition") or "")
        filename = part.get_filename()
        if "attachment" in disp or filename:
            if filename:
                parts.append(part)
    return parts


def get_mail(uid: str, mailbox: str = "INBOX") -> dict:
    conn = _connect()
    try:
        conn.select(mailbox, readonly=True)
        status, msg_data = conn.fetch(uid.encode(), "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError(f"메일을 찾을 수 없습니다 (uid: {uid})")
        msg = email.message_from_bytes(msg_data[0][1])
        attachments = [_decode(part.get_filename()) for part in _list_attachment_parts(msg)]
        return {
            "uid": uid,
            "subject": _decode(msg.get("Subject")),
            "from": _decode(msg.get("From")),
            "to": _decode(msg.get("To")),
            "date": msg.get("Date", ""),
            "body": _get_body(msg),
            "attachments": attachments,
        }
    finally:
        conn.logout()


def list_attachments(uid: str, mailbox: str = "INBOX") -> list[dict]:
    conn = _connect()
    try:
        conn.select(mailbox, readonly=True)
        status, msg_data = conn.fetch(uid.encode(), "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError(f"메일을 찾을 수 없습니다 (uid: {uid})")
        msg = email.message_from_bytes(msg_data[0][1])
        results = []
        for part in _list_attachment_parts(msg):
            payload = part.get_payload(decode=True)
            results.append({
                "filename": _decode(part.get_filename()),
                "content_type": part.get_content_type(),
                "size_bytes": len(payload) if payload else 0,
            })
        return results
    finally:
        conn.logout()


_DOWNLOAD_DIR = Path(__file__).parent / "downloads"


def download_attachment(uid: str, filename: str, mailbox: str = "INBOX") -> str:
    conn = _connect()
    try:
        conn.select(mailbox, readonly=True)
        status, msg_data = conn.fetch(uid.encode(), "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            raise RuntimeError(f"메일을 찾을 수 없습니다 (uid: {uid})")
        msg = email.message_from_bytes(msg_data[0][1])
        for part in _list_attachment_parts(msg):
            part_filename = _decode(part.get_filename())
            if part_filename == filename:
                payload = part.get_payload(decode=True)
                if not payload:
                    raise RuntimeError(f"첨부파일 내용을 읽을 수 없습니다 (filename: {filename})")
                _DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
                save_path = _DOWNLOAD_DIR / f"{uid}_{part_filename}"
                save_path.write_bytes(payload)
                return str(save_path)
        raise RuntimeError(f"첨부파일을 찾을 수 없습니다 (uid: {uid}, filename: {filename})")
    finally:
        conn.logout()
