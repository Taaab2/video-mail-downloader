"""טעינת הגדרות (config.json) ורשימה לבנה (whitelist.json).

הקובץ משותף לתהליך שרץ ב-GitHub Actions ולכלים שרצים מקומית.
"""

from __future__ import annotations

import json
import re
from email.utils import parseaddr
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
WHITELIST_PATH = ROOT / "whitelist.json"

DEFAULTS: dict[str, Any] = {
    "subject_keywords": ["הורדה", "download"],
    "link_sources": ["subject", "body"],
    "upload_target": "github",
    "drive_folder_id": "",
    "whitelist_file": "whitelist.json",
    "downloads_folder": "Downloaded",
    "failed_folder": "Downloads-Failed",
    "max_links_per_email": 3,
    "local_scan_limit": 50,
    "max_file_size_mb": 1900,
    "video_format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
    "merge_output_format": "mp4",
    "allow_playlists": False,
    "js_runtimes": [],
    "notify_unauthorized": False,
    "notify_on_failure": True,
    "reply_subject_prefix": "ההורדה שלך מוכנה",
    "reply_footer": "",
    "release_tag_prefix": "dl",
    "ascii_filenames": False,
}

_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)\u200f\u200e]+", re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

# קישורים שאין טעם לנסות להוריד מהם
_SKIP_HOSTS = (
    "unsubscribe",
    "mail.google.com",
    "accounts.google.com",
    "support.google.com",
    "policies.google.com",
    "facebook.com/help",
    "twitter.com/settings",
)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"לא ניתן לקרוא את {path}: {exc}") from exc


def load_config(path: Path | None = None) -> dict[str, Any]:
    """מחזיר את ההגדרות, עם השלמה של ברירות מחדל לכל מפתח חסר."""
    raw = _read_json(path or CONFIG_PATH, {})
    if not isinstance(raw, dict):
        raise SystemExit("config.json חייב להכיל אובייקט JSON")
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in raw.items() if v is not None})
    return merged


def load_whitelist(cfg: dict[str, Any] | None = None, path: Path | None = None) -> dict[str, Any]:
    """מחזיר את הרשימה הלבנה בצורה אחידה."""
    if path is None:
        name = (cfg or {}).get("whitelist_file") or "whitelist.json"
        path = ROOT / name
    raw = _read_json(path, {"allow_all": False, "users": []})
    if isinstance(raw, list):  # תמיכה בפורמט פשוט: ["a@b.com", ...]
        raw = {"allow_all": False, "users": [{"email": e, "active": True} for e in raw]}
    users = []
    for entry in raw.get("users", []):
        if isinstance(entry, str):
            entry = {"email": entry}
        email = normalize_email(entry.get("email", ""))
        if not email:
            continue
        users.append(
            {
                "email": email,
                "name": str(entry.get("name", "") or ""),
                "active": bool(entry.get("active", True)),
                "note": str(entry.get("note", "") or ""),
            }
        )
    return {"allow_all": bool(raw.get("allow_all", False)), "users": users}


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(path)


def normalize_email(value: str) -> str:
    """מוציא כתובת מייל נקייה מ-'שם <כתובת>'."""
    if not value:
        return ""
    addr = parseaddr(str(value))[1] or str(value)
    return addr.strip().strip("<>").lower()


def is_allowed(sender: str, whitelist: dict[str, Any]) -> bool:
    """בודק אם השולח מורשה. תומך בכתובת מלאה, או בדומיין שלם (@example.com / *@example.com)."""
    email = normalize_email(sender)
    if not email or "@" not in email:
        return False
    if whitelist.get("allow_all"):
        return True
    for entry in whitelist.get("users", []):
        if not entry.get("active", True):
            continue
        pattern = normalize_email(entry.get("email", ""))
        if not pattern:
            continue
        if pattern.startswith("@") and email.endswith(pattern):
            return True
        if pattern.startswith("*@") and email.endswith(pattern[1:]):
            return True
        if email == pattern:
            return True
    return False


def subject_matches(subject: str, keywords: list[str] | None) -> bool:
    """האם הנושא מכיל אחת ממילות המפתח (למשל 'הורדה')."""
    if not keywords:
        return False
    haystack = (subject or "").lower()
    # ניקוי קידומות תשובה/העברה כדי שגם "Re: הורדה" יעבוד
    haystack = re.sub(r"^\s*((re|fwd|fw|השב|הועבר)\s*:\s*)+", "", haystack, flags=re.IGNORECASE)
    return any(kw.strip().lower() in haystack for kw in keywords if kw and kw.strip())


def extract_urls(*texts: str, limit: int = 10) -> list[str]:
    """מוציא קישורי http/https מתוך גוף המייל, בלי כפילויות."""
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        candidates = _URL_RE.findall(text) + _HREF_RE.findall(text)
        for url in candidates:
            url = _clean_url(url)
            if not url:
                continue
            low = url.lower()
            if any(skip in low for skip in _SKIP_HOSTS):
                continue
            key = low.rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            found.append(url)
            if len(found) >= limit:
                return found
    return found


def _clean_url(url: str) -> str:
    url = (url or "").strip()
    url = url.replace("&amp;", "&").replace("\u200f", "").replace("\u200e", "")
    url = url.rstrip(".,;:!?>)]}")
    if not url.lower().startswith(("http://", "https://")):
        return ""
    if len(url) < 12:
        return ""
    return url
