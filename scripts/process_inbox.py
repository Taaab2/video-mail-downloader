#!/usr/bin/env python3
"""תהליך ההורדה: מייל עם נושא "הורדה" -> הורדת הסרטון -> GitHub Release -> מייל תשובה עם קישור ישיר.

הסקריפט רץ ב-GitHub Actions (לפי טיימר או ידנית) ואפשר להריץ אותו גם מקומית לבדיקה.

שימוש:
    python scripts/process_inbox.py                 # עיבוד תיבת הדואר
    python scripts/process_inbox.py --dry-run       # בלי הורדה / העלאה / שליחה
    python scripts/process_inbox.py --check         # בדיקת הגדרות וחיבורים
    python scripts/process_inbox.py --eml file.eml  # בדיקת קובץ מייל שמור
"""

from __future__ import annotations

import argparse
import base64
import gzip
import html as html_mod
import imaplib
import importlib.util
import json
import os
import random
import re
import shutil
import smtplib
import ssl
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (  # noqa: E402
    ROOT,
    extract_urls,
    is_allowed,
    load_config,
    load_whitelist,
    normalize_email,
    subject_matches,
)
from drive_upload import DriveError, DriveUploader  # noqa: E402
from github_release import ReleaseError, ReleaseUploader, human_size  # noqa: E402

REPO = os.environ.get("GITHUB_REPOSITORY", "")
MAX_BODY_CHARS = 200_000

# אפשרויות האיכות שהתוסף (והמשתמש) יכולים לבחור. השמות מקבילים ל-extension/lib/quality.js
QUALITY_PRESETS: dict[str, dict] = {
    "best": {
        "label": "האיכות הכי טובה",
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
    },
    "1080p": {
        "label": "1080p",
        "format": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
    },
    "720p": {
        "label": "720p",
        "format": "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
    },
    "480p": {
        "label": "480p",
        "format": "bv*[height<=480]+ba/b[height<=480]/bv*+ba/b",
    },
    "360p": {
        "label": "360p",
        "format": "bv*[height<=360]+ba/b[height<=360]/bv*+ba/b",
    },
    "audio": {
        "label": "אודיו בלבד (mp3)",
        "format": "ba/b",
        "audio_only": True,
    },
}


def quality_preset(name: str | None) -> dict | None:
    """מחזיר את תבנית האיכות, או None אם לא צוין/לא מוכר."""
    if not name:
        return None
    return QUALITY_PRESETS.get(str(name).strip().lower())


def cookie_file_from_env(workdir: Path) -> Path | None:
    """בונה קובץ cookies.txt מ-YT_COOKIES_B64 (base64, אולי gzip) אם הוגדר."""
    direct = os.environ.get("YT_COOKIES_FILE", "").strip()
    if direct and Path(direct).is_file():
        log(f"משתמש בקובץ עוגיות קיים: {direct}")
        return Path(direct)

    raw = (os.environ.get("YT_COOKIES_B64") or os.environ.get("COOKIES_B64") or "").strip()
    if not raw:
        log("לא הוגדרו עוגיות (YT_COOKIES_B64) – ממשיך בלעדיהן")
        return None
    try:
        data = base64.b64decode(raw, validate=False)
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        text = data.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        log_error(f"פענוח העוגיות נכשל: {exc}")
        return None
    if "Netscape HTTP Cookie File" not in text and "\t" not in text:
        log_error("העוגיות שהתקבלו אינן בפורמט cookies.txt – מדלג")
        return None
    path = workdir / "cookies.txt"
    path.write_text(text, encoding="utf-8")
    log(f"נטענו עוגיות ({len(text)} תווים) לשימוש yt-dlp")
    return path


# --------------------------------------------------------------------------- #
# לוגים
# --------------------------------------------------------------------------- #
def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def log_error(message: str) -> None:
    log(f"שגיאה: {message}")
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::error::{message}", flush=True)


def log_notice(message: str) -> None:
    log(message)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice::{message}", flush=True)


_SUMMARY_LINES: list[str] = []


def summary(line: str) -> None:
    _SUMMARY_LINES.append(line)


def flush_summary() -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path or not _SUMMARY_LINES:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(_SUMMARY_LINES) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# פענוח מייל
# --------------------------------------------------------------------------- #
@dataclass
class IncomingRequest:
    uid: bytes | str
    sender: str
    sender_name: str
    subject: str
    message_id: str
    reply_to: str
    body_text: str
    links: list[str] = field(default_factory=list)


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:  # כותרות פגומות
        return str(value).strip()


def _payload_text(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    for enc in (charset, "utf-8", "cp1255", "latin-1"):
        try:
            return payload.decode(enc, errors="replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_message(raw: bytes, uid: bytes | str = "",
                 link_sources: list[str] | tuple[str, ...] = ("subject", "body")) -> IncomingRequest:
    msg = message_from_bytes(raw)
    sender_raw = _decode_header(msg.get("From"))
    sender = normalize_email(sender_raw)
    sender_name = parseaddr(sender_raw)[0] or sender

    subject = _decode_header(msg.get("Subject"))
    reply_to = normalize_email(_decode_header(msg.get("Reply-To"))) or sender

    plain_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain":
                plain_parts.append(_payload_text(part))
            elif ctype == "text/html":
                html_parts.append(_payload_text(part))
    else:
        if msg.get_content_type() == "text/html":
            html_parts.append(_payload_text(msg))
        else:
            plain_parts.append(_payload_text(msg))

    html_text = "\n".join(html_parts)[:MAX_BODY_CHARS]
    plain_text = "\n".join(plain_parts)[:MAX_BODY_CHARS]
    if not plain_text.strip():
        plain_text = _strip_html(html_text)

    # סדר המקורות קובע איזה קישור נבחר ראשון – ברירת המחדל: קודם הנושא, אחר כך הגוף.
    ordered: list[str] = []
    for source in link_sources or []:
        if source == "subject":
            ordered.append(subject)
        elif source == "body":
            ordered.extend([plain_text, html_text])

    return IncomingRequest(
        uid=uid,
        sender=sender,
        sender_name=sender_name,
        subject=subject,
        message_id=_decode_header(msg.get("Message-ID")) or make_msgid(),
        reply_to=reply_to,
        body_text=plain_text.strip(),
        links=extract_urls(*ordered),
    )


# --------------------------------------------------------------------------- #
# IMAP
# --------------------------------------------------------------------------- #
class Inbox:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.conn: imaplib.IMAP4 | None = None

    def connect(self) -> "Inbox":
        log(f"מתחבר לתיבת הדואר {self.host}:{self.port} …")
        self.conn = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ssl.create_default_context())
        self.conn.login(self.user, self.password)
        self.conn.select("INBOX")
        return self

    def close(self) -> None:
        if not self.conn:
            return
        try:
            self.conn.logout()
        except Exception:
            pass
        self.conn = None

    # -- חיפוש ------------------------------------------------------------- #
    def search(self, keywords: list[str], local_scan_limit: int = 50) -> list[bytes]:
        """מחזיר UIDs של הודעות שלא נקראו עם מילת מפתח בנושא.

        רץ בשני מסלולים ומאחד ביניהם, כדי שאף הודעה לא תיפול בין הכיסאות:
        1. חיפוש בצד השרת עם CHARSET UTF-8 (הדרך המהירה, נדרש לעברית).
        2. סריקה מקומית של הכותרות של עד `local_scan_limit` הודעות שלא נקראו.
        """
        assert self.conn is not None
        uids: list[bytes] = []
        for kw in keywords:
            try:
                criteria = ('"%s"' % kw).encode("utf-8")
                typ, data = self.conn.uid("SEARCH", "CHARSET", "UTF-8", "SUBJECT", criteria, "UNSEEN")
                if typ == "OK" and data and data[0]:
                    uids.extend(data[0].split())
                elif typ != "OK":
                    log("חיפוש השרת לא הצליח; מסתמך על הסריקה המקומית.")
            except Exception as exc:  # noqa: BLE001
                log(f"חיפוש בצד השרת לא נתמך ({exc}); מסתמך על הסריקה המקומית.")

        try:
            typ, data = self.conn.uid("SEARCH", None, "UNSEEN")
            all_unseen = data[0].split() if (typ == "OK" and data and data[0]) else []
        except Exception as exc:  # noqa: BLE001
            log(f"לא ניתן לקבל את רשימת ההודעות שלא נקראו: {exc}")
            all_unseen = []
        for uid in all_unseen[-local_scan_limit:]:
            if subject_matches(self._peek_subject(uid), keywords):
                uids.append(uid)

        seen: set[bytes] = set()
        ordered: list[bytes] = []
        for uid in uids:
            if uid not in seen:
                seen.add(uid)
                ordered.append(uid)
        return ordered

    def _peek_subject(self, uid: bytes) -> str:
        assert self.conn is not None
        try:
            typ, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
            if typ == "OK" and data and isinstance(data[0], tuple):
                header = data[0][1].decode("utf-8", errors="replace")
                value = header.split(":", 1)[1] if ":" in header else header
                return _decode_header(value)
        except Exception:
            pass
        return ""

    def fetch(self, uid: bytes) -> bytes:
        assert self.conn is not None
        typ, data = self.conn.uid("FETCH", uid, "(RFC822)")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError(f"שליפת ההודעה {uid!r} נכשלה")
        return data[0][1]

    # -- סימון ההודעה כטופלה ------------------------------------------------ #
    def archive(self, uid: bytes, folder: str) -> bool:
        """מעביר את ההודעה לתיקייה; אם אי אפשר – מסמן כנקראה."""
        assert self.conn is not None
        try:
            self.conn.create(folder)
        except Exception:
            pass
        try:
            typ, _ = self.conn.uid("MOVE", uid, folder)
            if typ == "OK":
                return True
        except Exception:
            pass
        try:
            typ, _ = self.conn.uid("COPY", uid, folder)
            if typ == "OK":
                self.conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                try:
                    self.conn.uid("EXPUNGE", uid)
                except Exception:
                    self.conn.expunge()
                return True
        except Exception:
            pass
        try:
            self.conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        except Exception:
            pass
        return False


# --------------------------------------------------------------------------- #
# הורדה
# --------------------------------------------------------------------------- #
class DownloadError(RuntimeError):
    pass


_LAST_PROGRESS_BUCKET = {"value": -1}


class Downloader:
    def __init__(self, cfg: dict, workdir: Path, quality: str | None = None,
                 cookiefile: Path | None = None):
        self.cfg = cfg
        self.workdir = workdir
        self.quality = quality
        self.cookiefile = cookiefile

    @staticmethod
    def yt_dlp_available() -> bool:
        return importlib.util.find_spec("yt_dlp") is not None

    def download(self, url: str, max_bytes: int | None) -> list[Path]:
        if not self.yt_dlp_available():
            raise DownloadError("חבילת yt-dlp לא מותקנת (pip install -r scripts/requirements.txt)")
        import yt_dlp  # noqa: PLC0415

        preset = quality_preset(self.quality) or {}
        before = {p for p in self.workdir.iterdir() if p.is_file()}
        target = self.workdir
        opts: dict = {
            "outtmpl": str(target / "%(title).150B [%(id)s].%(ext)s"),
            "format": preset.get("format") or self.cfg["video_format"],
            "merge_output_format": self.cfg["merge_output_format"],
            "noplaylist": not bool(self.cfg.get("allow_playlists")),
            "windowsfilenames": True,
            "restrictfilenames": bool(self.cfg.get("ascii_filenames")),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 3,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
            "ignoreerrors": False,
            "progress_hooks": [self._progress],
            "logger": _YdlLogger(),
        }
        if max_bytes:
            opts["max_filesize"] = max_bytes
        runtimes = self.cfg.get("js_runtimes") or []
        if runtimes:
            opts["js_runtimes"] = {str(name): {} for name in runtimes}
        if self.cookiefile:
            opts["cookiefile"] = str(self.cookiefile)
        if preset.get("audio_only"):
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ]
            log("  אודיו בלבד – יומר ל-mp3")

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        new_files = [p for p in self.workdir.iterdir() if p.is_file() and p not in before]
        if not new_files:
            # גיבוי: yt-dlp החזיר נתיב מדויק
            requested = (info or {}).get("requested_downloads") or []
            for item in requested:
                candidate = Path(item.get("filepath") or "")
                if candidate.is_file():
                    new_files.append(candidate)
        if not new_files:
            raise DownloadError("ההורדה הסתיימה אבל לא נמצא קובץ")
        return sorted(new_files, key=lambda p: p.stat().st_size, reverse=True)

    @staticmethod
    def _progress(data: dict) -> None:
        if data.get("status") != "downloading":
            return
        downloaded = data.get("downloaded_bytes") or 0
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        if not total:
            return
        pct = int(downloaded * 100 / total)
        bucket = pct // 20 * 20
        if bucket != _LAST_PROGRESS_BUCKET["value"]:
            _LAST_PROGRESS_BUCKET["value"] = bucket
            log(f"  מוריד… {pct}% ({human_size(downloaded)} / {human_size(total)})")


class _YdlLogger:
    def debug(self, msg: str) -> None:
        if msg.startswith("[debug]"):
            log(f"  [yt-dlp] {msg}")

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log(f"  [yt-dlp] אזהרה: {msg}")

    def error(self, msg: str) -> None:
        log(f"  [yt-dlp] שגיאה: {msg}")


# --------------------------------------------------------------------------- #
# שליחת מייל תשובה
# --------------------------------------------------------------------------- #
class Mailer:
    def __init__(self, host: str, port: int, user: str, password: str, sender: str):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.sender = sender or user

    def _connect(self) -> smtplib.SMTP:
        context = ssl.create_default_context()
        if int(self.port) == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(self.host, int(self.port), context=context, timeout=60)
        else:
            server = smtplib.SMTP(self.host, int(self.port), timeout=60)
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
        server.login(self.user, self.password)
        return server

    def send(self, to: str, subject: str, text: str, html: str | None = None,
             in_reply_to: str | None = None) -> None:
        msg = EmailMessage()
        msg["From"] = formataddr(("הורדות", self.sender))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.sender.split("@")[-1] if "@" in self.sender else None)
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        with self._connect() as server:
            server.send_message(msg)

    def verify(self) -> None:
        with self._connect():
            pass


# --------------------------------------------------------------------------- #
# בניית המייל החוזר
# --------------------------------------------------------------------------- #
def source_label(source: str) -> str:
    return {"github": "GitHub", "drive": "Google Drive", "dry-run": "בדיקה"}.get(source, source or "")


def build_reply(cfg: dict, results: list[dict], sender_name: str, failures: list[str]) -> tuple[str, str, str]:
    first_title = results[0]["asset_name"] if results else "הסרטון"
    if len(first_title) > 70:
        first_title = first_title[:67].rstrip() + "…"
    subject = f"{cfg['reply_subject_prefix']} – {first_title}"
    lines = [f"שלום {sender_name or ''},".rstrip(), ""]
    if results:
        lines.append("הסרטון הורד ומוכן להורדה בקישור הישיר הבא:" if len(results) == 1
                     else "הסרטונים הורדו ומוכנים להורדה בקישורים הישירים הבאים:")
        lines.append("")
        for item in results:
            lines.append(
                f"• {item['asset_name']} ({human_size(item['size'])}) – {source_label(item.get('source', ''))}"
            )
            lines.append(f"  {item['url']}")
            lines.append("")
    if failures:
        lines.append("הפריטים הבאים לא ירדו:")
        for err in failures:
            lines.append(f"• {err}")
        lines.append("")
    footer = cfg.get("reply_footer")
    if footer:
        lines.append(footer)
    text = "\n".join(lines).strip() + "\n"

    rows = []
    for item in results:
        rows.append(
            "<tr><td style='padding:8px 12px;border-bottom:1px solid #eee'>"
            f"{html_mod.escape(item['asset_name'])}<br>"
            f"<span style='color:#888;font-size:12px'>{human_size(item['size'])} · "
            f"{html_mod.escape(source_label(item.get('source', '')))}</span></td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee'>"
            f"<a href=\"{html_mod.escape(item['url'])}\" style='color:#0b6efd'>הורדה ישירה</a></td></tr>"
        )
    html = (
        "<div dir='rtl' style=\"font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#222\">"
        f"<p>שלום {html_mod.escape(sender_name or '')},</p>"
        + ("<p>הסרטון מוכן:</p>" if len(results) == 1 else "<p>הסרטונים מוכנים:</p>")
        + "<table style='border-collapse:collapse'>" + "".join(rows) + "</table>"
        + ("<p style='color:#b00'>לא הצלחנו להוריד: " + html_mod.escape("; ".join(failures)) + "</p>"
           if failures else "")
        + (f"<p style='color:#666;font-size:13px'>{html_mod.escape(footer)}</p>" if footer else "")
        + "</div>"
    )
    return subject, text, html


# --------------------------------------------------------------------------- #
# עיבוד בקשה בודדת
# --------------------------------------------------------------------------- #
@dataclass
class ProcessResult:
    ok: bool
    notes: list[str] = field(default_factory=list)


def process_request(req: IncomingRequest, cfg: dict, args, mailer: Mailer | None,
                    uploader: ReleaseUploader | None,
                    drive_uploader: DriveUploader | None = None,
                    quality: str | None = None,
                    target: str | None = None,
                    cookiefile: Path | None = None) -> ProcessResult:
    quality = (quality or "").strip().lower() or None
    notes = [f"שולח: {req.sender}", f"נושא: {req.subject}", f"נמצאו {len(req.links)} קישורים"]
    if quality:
        preset = quality_preset(quality)
        notes.append(f"איכות: {preset['label'] if preset else quality}")
    for note in notes:
        log(f"  {note}")

    links = req.links[: int(cfg["max_links_per_email"])]
    if not links:
        if cfg.get("notify_on_failure") and mailer and not args.dry_run:
            mailer.send(
                req.reply_to,
                f"{cfg['reply_subject_prefix']} – לא נמצא קישור",
                "לא מצאנו קישור בהודעה. נא לשלוח שוב כשהקישור לסרטון נמצא בנושא ההודעה או בגופה, "
                "יחד עם המילה 'הורדה'.\n",
                "<div dir='rtl'>לא מצאנו קישור בהודעה. נא לשלוח שוב כשהקישור נמצא בנושא או בגוף ההודעה.</div>",
                in_reply_to=req.message_id,
            )
        log_error("לא נמצא קישור בהודעה")
        return ProcessResult(ok=False, notes=["אין קישור"])

    max_bytes = int(cfg["max_file_size_mb"]) * 1024 * 1024
    results: list[dict] = []
    failures: list[str] = []
    tag = f"{cfg['release_tag_prefix']}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{random.randint(0, 0xFFFF):04x}"
    release: dict | None = None

    target = str(target or cfg.get("upload_target") or "github").lower()
    use_github = target in ("github", "both") and uploader is not None
    use_drive = target in ("drive", "both") and drive_uploader is not None
    if target in ("drive", "both") and drive_uploader is None:
        log_notice("העלאה ל-Drive לא מוגדרת (חסר GOOGLE_SERVICE_ACCOUNT_JSON) – חוזר ל-GitHub.")
        use_github = uploader is not None
    if not args.dry_run and not use_github and not use_drive:
        log_error("אין יעד העלאה זמין – אי אפשר לפרסם קישור")
        return ProcessResult(ok=False, notes=["אין יעד העלאה"])
    log(f"  יעד העלאה: {target} (GitHub={use_github}, Drive={use_drive})")

    with tempfile.TemporaryDirectory(prefix="dl-") as tmp:
        workdir = Path(tmp)
        downloader = Downloader(cfg, workdir, quality=quality, cookiefile=cookiefile)
        for index, url in enumerate(links, start=1):
            log(f"מוריד ({index}/{len(links)}): {url}")
            try:
                files = downloader.download(url, max_bytes)
            except DownloadError as exc:
                log_error(str(exc))
                failures.append(f"{url} – {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - yt-dlp זורק חריגות רבות
                log_error(f"הורדה נכשלה עבור {url}: {exc}")
                failures.append(f"{url} – {exc}")
                continue

            for path in files:
                size = path.stat().st_size
                if size > max_bytes:
                    log_error(f"הקובץ {path.name} גדול מהמותר ({human_size(size)})")
                    failures.append(f"{path.name} – גדול מדי ({human_size(size)})")
                    continue
                if args.dry_run:
                    log(f"  [dry-run] היה מועלה ({target}): {path.name} ({human_size(size)})")
                    results.append(
                        {"asset_name": path.name, "size": size, "url": "#dry-run", "source": "dry-run"}
                    )
                    continue

                uploaded: list[dict] = []
                if use_github:
                    try:
                        if release is None:
                            release = uploader.create_release(
                                tag,
                                f"הורדות מהמייל {datetime.now(timezone.utc):%Y-%m-%d %H:%M}",
                                f"הורדה עבור {req.sender}",
                            )
                        asset = uploader.upload_asset(release, path)
                        log_notice(f"הועלה ל-GitHub: {asset['name']} ({human_size(size)})")
                        uploaded.append(
                            {
                                "asset_name": asset["name"],
                                "size": size,
                                "source": "github",
                                "url": asset.get("browser_download_url")
                                or ReleaseUploader.direct_url(REPO, tag, asset["name"]),
                            }
                        )
                    except ReleaseError as exc:
                        log_error(f"העלאה ל-GitHub נכשלה עבור {path.name}: {exc}")
                if use_drive:
                    try:
                        item = drive_uploader.upload(path, path.name)
                        log_notice(f"הועלה ל-Drive: {item['name']} ({human_size(size)})")
                        uploaded.append(
                            {"asset_name": item["name"], "size": size, "source": "drive", "url": item["url"]}
                        )
                    except DriveError as exc:
                        log_error(f"העלאה ל-Drive נכשלה עבור {path.name}: {exc}")
                if uploaded:
                    results.extend(uploaded)
                else:
                    failures.append(f"{path.name} – ההעלאה נכשלה")

    for item in results:
        summary(
            f"- ✅ [{item['asset_name']}]({item['url']}) ({human_size(item['size'])}) · "
            f"{source_label(item.get('source', ''))}"
        )
        log(f"  קישור: {item['url']}")
    for err in failures:
        summary(f"- ⚠️ {err}")

    if results and mailer and not args.dry_run:
        subject, text, html = build_reply(cfg, results, req.sender_name, failures)
        mailer.send(req.reply_to, subject, text, html, in_reply_to=req.message_id)
        log_notice(f"נשלח מייל תשובה אל {req.reply_to}")
    if failures and not results and cfg.get("notify_on_failure") and mailer and not args.dry_run:
        subject, text, html = build_reply(cfg, [], req.sender_name, failures)
        mailer.send(req.reply_to, subject, text, html, in_reply_to=req.message_id)
        log_notice(f"נשלח מייל כשלון אל {req.reply_to}")

    return ProcessResult(ok=bool(results), notes=notes + failures)


def process_unauthorized(req: IncomingRequest, cfg: dict, mailer: Mailer | None, args) -> None:
    log(f"  השולח {req.sender} אינו ברשימה הלבנה – מדלג.")
    if cfg.get("notify_unauthorized") and mailer and not args.dry_run:
        mailer.send(
            req.reply_to,
            f"{cfg['reply_subject_prefix']} – אין הרשאה",
            "כתובת המייל שלך אינה מורשית לבקש הורדות.\n",
            "<div dir='rtl'>כתובת המייל שלך אינה מורשית לבקש הורדות.</div>",
            in_reply_to=req.message_id,
        )


# --------------------------------------------------------------------------- #
# בדיקות
# --------------------------------------------------------------------------- #
def run_checks(cfg: dict, whitelist: dict) -> int:
    problems = 0
    checks: list[tuple[bool, str]] = []

    checks.append((True, f"נטענו {len(cfg.get('subject_keywords', []))} מילות מפתח לנושא"))
    checks.append((True, f"ברשימה הלבנה {len(whitelist['users'])} משתמשים (allow_all={whitelist['allow_all']})"))
    checks.append((bool(REPO), f"מאגר GitHub: {REPO or 'לא הוגדר (GITHUB_REPOSITORY)'}"))
    token_ok = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    checks.append((token_ok, "טוקן GitHub לכתיבה ל-Releases"))
    checks.append((Downloader.yt_dlp_available(), "חבילת yt-dlp מותקנת"))
    checks.append((shutil.which("ffmpeg") is not None, "ffmpeg מותקן (נדרש למיזוג וידאו+אודיו)"))

    sources = cfg.get("link_sources") or []
    checks.append((bool(sources), f"מקורות הקישור: {', '.join(sources) if sources else 'לא הוגדר'}"))

    target = str(cfg.get("upload_target") or "github").lower()
    checks.append((True, f"יעד העלאה מוגדר: {target}"))
    if target in ("drive", "both"):
        drive_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        folder = os.environ.get("DRIVE_FOLDER_ID", "") or str(cfg.get("drive_folder_id") or "")
        checks.append((bool(drive_json), "מפתח חשבון שירות ל-Drive (GOOGLE_SERVICE_ACCOUNT_JSON)"))
        checks.append((bool(folder), "מזהה תיקייה ב-Drive (DRIVE_FOLDER_ID)"))
        if drive_json:
            try:
                DriveUploader(drive_json, folder)
                checks.append((True, "הזדהות מול Google Drive הצליחה"))
            except DriveError as exc:
                checks.append((False, f"הזדהות מול Google Drive נכשלה: {exc}"))

    mail_user = os.environ.get("MAIL_USER", "")
    mail_pass = os.environ.get("MAIL_PASSWORD", "")
    checks.append((bool(mail_user and mail_pass), "פרטי התחברות למייל (MAIL_USER / MAIL_PASSWORD)"))

    if mail_user and mail_pass and not os.environ.get("SKIP_MAIL_LOGIN_CHECK"):
        imap_host = os.environ.get("IMAP_HOST", "imap.gmail.com")
        imap_port = int(os.environ.get("IMAP_PORT", "993"))
        try:
            inbox = Inbox(imap_host, imap_port, mail_user, mail_pass).connect()
            inbox.close()
            checks.append((True, f"התחברות ל-IMAP הצליחה ({imap_host})"))
        except Exception as exc:  # noqa: BLE001
            checks.append((False, f"התחברות ל-IMAP נכשלה: {exc}"))
        try:
            Mailer(
                os.environ.get("SMTP_HOST", "smtp.gmail.com"),
                int(os.environ.get("SMTP_PORT", "465")),
                mail_user,
                mail_pass,
                os.environ.get("MAIL_FROM", mail_user),
            ).verify()
            checks.append((True, "התחברות ל-SMTP הצליחה"))
        except Exception as exc:  # noqa: BLE001
            checks.append((False, f"התחברות ל-SMTP נכשלה: {exc}"))

    for ok, message in checks:
        log(f"  {'✓' if ok else '✗'} {message}")
        if not ok:
            problems += 1
    log(f"סיכום בדיקות: {len(checks) - problems}/{len(checks)} תקינים")
    return 0 if problems == 0 else 1


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="הורדת סרטונים מבקשות במייל והעלאה ל-GitHub Releases")
    parser.add_argument("--dry-run", action="store_true", help="בלי הורדה בפועל, בלי העלאה ובלי שליחת מייל")
    parser.add_argument("--check", action="store_true", help="בדיקת הגדרות, חיבורים וכלים")
    parser.add_argument("--eml", type=Path, help="עיבוד קובץ .eml מקומי במקום תיבת הדואר")
    parser.add_argument("--url", help="הורדה ישירה של קישור בודד (בלי תיבת דואר ובלי מייל תשובה)")
    parser.add_argument(
        "--quality",
        choices=sorted(QUALITY_PRESETS),
        default=None,
        help="איכות ההורדה (best / 1080p / 720p / 480p / 360p / audio)",
    )
    parser.add_argument(
        "--target",
        choices=["github", "drive", "both"],
        default=None,
        help="יעד ההעלאה להרצה הזו",
    )
    parser.add_argument("--json", action="store_true", help="פלט JSON (בשילוב עם --check, --eml או --url)")
    parser.add_argument("--max", type=int, default=0, help="מקסימום הודעות לעיבוד בהרצה הזו")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = load_config()
    whitelist = load_whitelist(cfg)

    if args.check:
        code = run_checks(cfg, whitelist)
        if args.json:
            print(json.dumps({"ok": code == 0}, ensure_ascii=False))
        return code

    mail_user = os.environ.get("MAIL_USER", "")
    mail_pass = os.environ.get("MAIL_PASSWORD", "")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")

    mailer: Mailer | None = None
    if mail_user and mail_pass:
        mailer = Mailer(
            os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            int(os.environ.get("SMTP_PORT", "465")),
            mail_user,
            mail_pass,
            os.environ.get("MAIL_FROM", mail_user),
        )

    uploader: ReleaseUploader | None = None
    if token and REPO:
        uploader = ReleaseUploader(token, REPO)

    drive_uploader: DriveUploader | None = None
    drive_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if drive_json:
        try:
            drive_uploader = DriveUploader(
                drive_json,
                os.environ.get("DRIVE_FOLDER_ID", "") or str(cfg.get("drive_folder_id") or ""),
            )
            log("העלאה ל-Google Drive מוכנה")
        except DriveError as exc:
            log_error(f"העלאה ל-Drive לא זמינה: {exc}")

    link_sources = cfg.get("link_sources") or ["subject", "body"]
    cookie_dir = Path(tempfile.mkdtemp(prefix="yt-cookies-"))
    cookiefile = cookie_file_from_env(cookie_dir)

    # ---- הורדה ישירה של קישור בודד (מה שמפעיל התוסף) ---- #
    if args.url:
        url = args.url.strip()
        if not re.match(r"^https?://", url, re.IGNORECASE):
            log_error(f"קישור לא תקין: {url}")
            return 2
        log(f"הורדה ישירה: {url}")
        if args.quality:
            preset = quality_preset(args.quality) or {}
            log(f"  איכות מבוקשת: {preset.get('label', args.quality)}")
        req = IncomingRequest(
            uid="manual",
            sender="manual",
            sender_name="הפעלה ידנית",
            subject=f"הורדה {url}",
            message_id=make_msgid(),
            reply_to="",
            body_text="",
            links=[url],
        )
        result = process_request(
            req, cfg, args, None, uploader, drive_uploader,
            quality=args.quality, target=args.target, cookiefile=cookiefile,
        )
        flush_summary()
        if args.json:
            print(json.dumps({"ok": result.ok, "notes": result.notes}, ensure_ascii=False))
        return 0 if result.ok else 1

    # ---- מצב בדיקה מקומית מקובץ .eml ---- #
    if args.eml:
        req = parse_message(args.eml.read_bytes(), uid="local", link_sources=link_sources)
        log(f"נקרא קובץ {args.eml.name}")
        log(f"  נושא: {req.subject}")
        log(f"  האם הנושא תואם: {subject_matches(req.subject, cfg['subject_keywords'])}")
        log(f"  שולח מורשה: {is_allowed(req.sender, whitelist)}")
        for link in req.links:
            log(f"  קישור: {link}")
        if not is_allowed(req.sender, whitelist):
            return 1
        result = process_request(
            req, cfg, args, mailer, uploader, drive_uploader,
            quality=args.quality, target=args.target, cookiefile=cookiefile,
        )
        flush_summary()
        if args.json:
            print(json.dumps({"ok": result.ok, "notes": result.notes}, ensure_ascii=False))
        return 0 if result.ok else 1

    if not (mail_user and mail_pass):
        log_error("חסרים MAIL_USER / MAIL_PASSWORD")
        return 2

    inbox = Inbox(
        os.environ.get("IMAP_HOST", "imap.gmail.com"),
        int(os.environ.get("IMAP_PORT", "993")),
        mail_user,
        mail_pass,
    )
    processed = 0
    failed = 0
    try:
        inbox.connect()
        uids = inbox.search(cfg["subject_keywords"], int(cfg.get("local_scan_limit") or 50))
        if args.max:
            uids = uids[-args.max:]
        log(f"נמצאו {len(uids)} בקשות חדשות עם מילת המפתח בנושא")
        summary(f"### נסרקו {len(uids)} בקשות חדשות")
        for uid in uids:
            log(f"— הודעה {uid.decode(errors='replace')} —")
            try:
                req = parse_message(inbox.fetch(uid), uid=uid, link_sources=link_sources)
            except Exception as exc:  # noqa: BLE001
                log_error(f"פענוח ההודעה נכשל: {exc}")
                continue

            if not is_allowed(req.sender, whitelist):
                process_unauthorized(req, cfg, mailer, args)
                inbox.archive(uid, cfg["failed_folder"])
                continue

            try:
                result = process_request(
                    req, cfg, args, mailer, uploader, drive_uploader,
                    quality=args.quality, target=args.target, cookiefile=cookiefile,
                )
            except Exception as exc:  # noqa: BLE001
                log_error(f"עיבוד נכשל: {exc}")
                log(traceback.format_exc())
                result = ProcessResult(ok=False, notes=[str(exc)])

            if result.ok:
                processed += 1
                inbox.archive(uid, cfg["downloads_folder"])
            else:
                failed += 1
                inbox.archive(uid, cfg["failed_folder"])
    finally:
        inbox.close()

    summary(f"\n**הושלמו בהצלחה:** {processed} · **נכשלו:** {failed}")
    flush_summary()
    log(f"סיום. הצליחו {processed}, נכשלו {failed}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
