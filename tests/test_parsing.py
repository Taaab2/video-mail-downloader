#!/usr/bin/env python3
"""בדיקות לפענוח המייל ולסינון ההרשאות – רצות בלי רשת ובלי תיבת דואר.

הרצה:  python tests/test_parsing.py
"""

from __future__ import annotations

import sys
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import imaplib  # noqa: E402

from config import extract_urls, is_allowed, load_config, load_whitelist, subject_matches  # noqa: E402
from process_inbox import Inbox, parse_message  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(condition: bool, label: str) -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ✓ {label}")
    else:
        FAILED.append(label)
        print(f"  ✗ {label}")


def test_subject() -> None:
    print("נושא ההודעה:")
    keywords = ["הורדה", "download"]
    check(subject_matches("הורדה", keywords), "נושא בדיוק 'הורדה'")
    check(subject_matches("Re: הורדה", keywords), "תשובה 'Re: הורדה'")
    check(subject_matches("Fwd: הורדה דחוף", keywords), "העברה עם טקסט נוסף")
    check(subject_matches("DOWNLOAD now", keywords), "אנגלית באותיות גדולות")
    check(not subject_matches("שלום", keywords), "נושא לא רלוונטי לא מתאים")
    check(not subject_matches("", keywords), "נושא ריק")
    check(not subject_matches("הורדה", []), "רשימת מילות מפתח ריקה")


def test_whitelist() -> None:
    print("הרשאות:")
    wl = {
        "allow_all": False,
        "users": [
            {"email": "me@gmail.com", "active": True},
            {"email": "@company.co.il", "active": True},
            {"email": "blocked@x.com", "active": False},
        ],
    }
    check(is_allowed("me@gmail.com", wl), "כתובת מדויקת")
    check(is_allowed("ME@Gmail.com", wl), "לא תלוי רישיות")
    check(is_allowed("משה <me@gmail.com>", wl), "פורמט 'שם <כתובת>'")
    check(is_allowed("anyone@company.co.il", wl), "דומיין שלם")
    check(not is_allowed("blocked@x.com", wl), "משתמש כבוי לא מורשה")
    check(not is_allowed("stranger@other.com", wl), "זר לא מורשה")
    check(not is_allowed("", wl), "שולח ריק")
    check(is_allowed("stranger@other.com", {"allow_all": True, "users": []}), "allow_all מאשר את כולם")


def test_urls() -> None:
    print("חילוץ קישורים:")
    text = "הנה הסרטון: https://www.youtube.com/watch?v=abc123. תודה!"
    urls = extract_urls(text)
    check(urls == ["https://www.youtube.com/watch?v=abc123"], "מנקה סימן פיסוק בסוף")

    html = '<a href="https://youtu.be/xyz">קישור</a>'
    check(extract_urls("", html) == ["https://youtu.be/xyz"], "מחלץ href מתוך HTML")

    dup = "https://youtu.be/xyz https://youtu.be/xyz/"
    check(len(extract_urls(dup)) == 1, "מסיר כפילויות")

    with_unsub = "https://youtu.be/xyz https://example.com/unsubscribe?id=1"
    check(extract_urls(with_unsub) == ["https://youtu.be/xyz"], "מדלג על קישור הסרה")

    check(extract_urls("אין כאן קישור") == [], "ללא קישור")


def build_sample_email(subject_link: bool = False) -> bytes:
    msg = EmailMessage()
    msg["From"] = "משה כהן <me@gmail.com>"
    msg["To"] = "inbox@gmail.com"
    if subject_link:
        msg["Subject"] = "הורדה https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    else:
        msg["Subject"] = "הורדה"
    msg["Message-ID"] = "<test-123@example.com>"
    msg.set_content("בבקשה תוריד לי את הסרטון הזה:\nhttps://www.youtube.com/watch?v=dQw4w9WgXcQ\nתודה!")
    return msg.as_bytes()


def build_html_only_email() -> bytes:
    msg = EmailMessage()
    msg["From"] = "דנה <dana@company.co.il>"
    msg["Subject"] = "Fwd: הורדה"
    msg.set_content("<div dir='rtl'><p>הקישור: <a href='https://youtu.be/aaa'>כאן</a></p></div>", subtype="html")
    return msg.as_bytes()


def test_parse() -> None:
    print("פענוח הודעה:")
    req = parse_message(build_sample_email(), uid="1")
    check(req.subject == "הורדה", "נושא בעברית מפוענח נכון")
    check(req.sender == "me@gmail.com", "שולח מפוענח")
    check(req.sender_name == "משה כהן", "שם השולח מפוענח")
    check(req.links == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"], "קישור מתוך גוף ההודעה")

    req2 = parse_message(build_html_only_email(), uid="2")
    check(req2.subject.startswith("Fwd:"), "נושא עם קידומת העברה")
    check(req2.links == ["https://youtu.be/aaa"], "קישור מתוך מייל HTML בלבד")
    check("כאן" in req2.body_text, "גוף ההודעה מומר מטקסט HTML")


class _FakeImap:
    """חיבור IMAP מדומה: מדמה שרת שלא מחזיר תוצאות לחיפוש בעברית."""

    def __init__(self, unseen: list[bytes], subjects: dict[bytes, str], server_search: str = "empty"):
        self.unseen = unseen
        self.subjects = subjects
        self.server_search = server_search

    def uid(self, command, *args):
        if command == "SEARCH" and args and args[0] == "CHARSET":
            if self.server_search == "error":
                raise imaplib.IMAP4.error("CHARSET not supported")
            if self.server_search == "empty":
                return ("OK", [b""])
            raise AssertionError("לא אמור להגיע לכאן בבדיקה")
        if command == "SEARCH":
            return ("OK", [b" ".join(self.unseen)])
        if command == "FETCH":
            uid = args[0]
            subject = self.subjects.get(uid, "")
            return ("OK", [(b"header", f"Subject: {subject}\r\n".encode("utf-8"))])
        raise AssertionError(f"פקודה לא צפויה: {command}")


def test_link_from_subject() -> None:
    print("קישור מתוך נושא ההודעה:")
    video = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    req = parse_message(build_sample_email(subject_link=True), uid="3")
    check(req.subject == f"הורדה {video}", "הנושא כולל גם את המילה וגם את הקישור")
    check(subject_matches(req.subject, ["הורדה"]), "הנושא עם קישור מזוהה כבקשת הורדה")
    check(req.links[:1] == [video], "הקישור נמצא בנושא")

    # סדר המקורות: כשיש קישור גם בנושא וגם בגוף – קודם הנושא
    subject_only = parse_message(build_sample_email(subject_link=True), uid="4", link_sources=["subject"])
    check(all(video in link for link in subject_only.links), "link_sources=['subject'] מתעלם מהגוף")
    body_only = parse_message(build_sample_email(subject_link=True), uid="5", link_sources=["body"])
    check(body_only.links == [video], "link_sources=['body'] לוקח רק מהגוף")

    no_links = parse_message(
        b"From: me@gmail.com\r\nSubject: \xd7\x94\xd7\x95\xd7\xa8\xd7\x93\xd7\x94\r\n\r\n\xd7\xa9\xd7\x9c\xd7\x95\xd7\x9d\r\n",
        uid="6",
    )
    check(no_links.links == [], "בלי קישור בכלל – רשימה ריקה")


def test_labels_and_targets() -> None:
    print("יעדים ותוויות:")
    from process_inbox import source_label

    check(source_label("github") == "GitHub", "תווית GitHub")
    check(source_label("drive") == "Google Drive", "תווית Drive")
    check(source_label("") == "", "תווית ריקה בסטייה לא מוכרת")

    from drive_upload import DriveError, DriveUploader

    check(DriveUploader.direct_url("abc123") == "https://drive.google.com/uc?export=download&id=abc123",
          "קישור הורדה ישיר של Drive")
    try:
        DriveUploader("", "")
    except DriveError:
        check(True, "בלי מפתח חשבון שירות מתקבלת שגיאה ברורה")
    else:
        check(False, "בלי מפתח חשבון שירות מתקבלת שגיאה ברורה")
    try:
        DriveUploader("not json", "")
    except DriveError as exc:
        check("JSON" in str(exc), "מפתח לא תקין מזוהה")
    else:
        check(False, "מפתח לא תקין מזוהה")


def test_imap_search_union() -> None:
    print("חיפוש בתיבה (כולל סריקה מקומית):")
    subjects = {b"1": "שלום", b"2": "download please", b"3": "הורדה"}
    inbox = Inbox("imap.test", 993, "u", "p")
    inbox.conn = _FakeImap([b"1", b"2", b"3"], subjects)
    found = inbox.search(["הורדה", "download"], 50)
    check(found == [b"2", b"3"], "מוצא הודעה שהשרת פספס (סריקה מקומית)")

    inbox2 = Inbox("imap.test", 993, "u", "p")
    inbox2.conn = _FakeImap([b"3"], subjects, server_search="error")
    check(inbox2.search(["הורדה"], 50) == [b"3"], "עובד גם כשחיפוש השרת נכשל")

    inbox3 = Inbox("imap.test", 993, "u", "p")
    inbox3.conn = _FakeImap([b"1"], subjects)
    check(inbox3.search(["הורדה"], 50) == [], "לא מחזיר הודעות לא רלוונטיות")


def test_config_files() -> None:
    print("קבצי הגדרות:")
    cfg = load_config()
    check(isinstance(cfg.get("subject_keywords"), list) and cfg["subject_keywords"], "config.json נטען")
    check("הורדה" in cfg["subject_keywords"], "מילת המפתח 'הורדה' מוגדרת")
    check(int(cfg["max_file_size_mb"]) > 0, "מגבלת גודל מוגדרת")
    wl = load_whitelist(cfg)
    check(isinstance(wl["users"], list), "whitelist.json נטען")
    check(cfg.get("link_sources") and "subject" in cfg["link_sources"], "הקישור נלקח מהנושא כברירת מחדל")
    check(cfg.get("upload_target") in ("github", "drive", "both"), "יעד העלאה תקין")


def main() -> int:
    test_subject()
    test_whitelist()
    test_urls()
    test_parse()
    test_link_from_subject()
    test_labels_and_targets()
    test_imap_search_union()
    test_config_files()
    print()
    print(f"עברו {PASSED} בדיקות, נכשלו {len(FAILED)}")
    for label in FAILED:
        print(f"  ✗ {label}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
