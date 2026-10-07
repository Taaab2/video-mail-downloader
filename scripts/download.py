#!/usr/bin/env python3
"""הורדת סרטון: yt-dlp מוריד -> אריזה כ-ZIP -> Release ב-GitHub עם קישור ישיר.

הסקריפט רץ ב-GitHub Actions (מופעל מהתוסף) ואפשר להריץ אותו גם מקומית לבדיקה.

שימוש:
    python scripts/download.py --url "https://youtu.be/XXXX" --quality 720p
    python scripts/download.py --url "https://youtu.be/XXXX" --local   # רק מוריד, בלי להעלות
"""

from __future__ import annotations

import argparse
import base64
import gzip
import importlib.util
import json
import os
import random
import re
import shutil
import sys
import tempfile
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# הפלט יכול להכיל כותרות בכל שפה; מוודאים שהדפסה לא תקרוס על קידוד הקונסולה
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - קונסולה ישנה בלי reconfigure
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from github_release import ReleaseError, ReleaseUploader, human_size  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
REPO = os.environ.get("GITHUB_REPOSITORY", "")

# אפשרויות האיכות שהתוסף בוחר. השמות מקבילים ל-extension/lib/quality.js
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

DEFAULTS: dict = {
    "max_file_size_mb": 1900,
    "video_format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
    "merge_output_format": "mp4",
    "allow_playlists": False,
    "js_runtimes": [],
    # רכיבים מרוחקים של yt-dlp: "ejs:github" מוריד את פותר ה-JS של YouTube
    # (נדרש כדי לפתור את אתגר ה-n). בלעדיו סרטוני יוטיוב נכשלים ב-"הדף צריך רענון".
    "remote_components": ["ejs:github"],
    "ascii_filenames": False,
    "release_tag_prefix": "dl",
    # כמה שעות לשמור Release לפני שהניקוי היומי מוחק אותו (0 = לעולם)
    "delete_releases_after_hours": 24,
    # תקרה למספר הסרטונים שנשלפים מרשימת פלייליסט/ערוץ (בחירה להורדה)
    "max_playlist_items": 200,
}


def load_config(path: Path | None = None) -> dict:
    """מחזיר את ההגדרות עם השלמת ברירות מחדל לכל מפתח חסר."""
    raw: dict = {}
    file = path or CONFIG_PATH
    if file.exists():
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise SystemExit(f"לא ניתן לקרוא את {file}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit("config.json חייב להכיל אובייקט JSON")
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in raw.items() if v is not None})
    return merged


def quality_preset(name: str | None) -> dict | None:
    if not name:
        return None
    return QUALITY_PRESETS.get(str(name).strip().lower())


# --------------------------------------------------------------------------- #
# שליפת רשימת סרטונים מפלייליסט/ערוץ (בלי להוריד)
# --------------------------------------------------------------------------- #
# כתובת שורש של ערוץ יוטיוב בלי לשונית מחזירה "טאבים" ולא סרטונים, ולכן
# מפנים אותה אוטומטית ללשונית הסרטונים (/videos).
_CHANNEL_RE = re.compile(
    r"^https?://(?:www\.|m\.)?youtube\.com/"
    r"(?P<kind>@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)/?"
    r"(?:\?.*)?$",
    re.IGNORECASE,
)


def _normalize_collection_url(url: str) -> str:
    match = _CHANNEL_RE.match(url.strip())
    if match:
        return f"https://www.youtube.com/{match.group('kind')}/videos"
    return url


# תווי בקרה/כיווניות נסתרים (למשל U+2066) שמקלקלים תצוגה ועלולים להפיל הדפסה
_UNWANTED_CHARS = re.compile(
    r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)


def _clean_text(text: str) -> str:
    """מנקה תווים נסתרים ומצמצם רווחים – כותרות טיקטוק מכילות תווים כאלה."""
    return re.sub(r"\s+", " ", _UNWANTED_CHARS.sub("", str(text or ""))).strip()


def _impersonate_target(cfg: dict):
    """בונה ImpersonateTarget לחיקוי דפדפן. נדרש לטיקטוק משרתי ענן.

    מחזיר None אם החיקוי כבוי או ש-curl_cffi לא מותקן (ואז ייתכן חסימה).
    """
    name = str(cfg.get("impersonate") or "").strip()
    if not name:
        return None
    try:
        import curl_cffi  # noqa: F401, PLC0415
        from yt_dlp.networking.impersonate import ImpersonateTarget  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - curl_cffi אופציונלי
        log("curl_cffi לא מותקן – בלי חיקוי דפדפן (טיקטוק עלול להיחסם)")
        return None
    try:
        return ImpersonateTarget.from_str(name)
    except Exception:  # noqa: BLE001
        log(f"יעד חיקוי לא מוכר ({name}) – מדלג")
        return None


def _entry_to_video(entry: dict | None) -> dict | None:
    """ממיר רשומת yt-dlp (מ-flat extraction) לפריט סרטון מתומצת."""
    if not isinstance(entry, dict):
        return None
    # טאבים של ערוץ ("סרטונים", "שורטים"…) חוזרים כרשימות מקוננות – מדלגים
    if entry.get("_type") == "playlist":
        return None
    vid = str(entry.get("id") or "").strip()
    title = _clean_text(entry.get("title") or entry.get("fulltitle"))
    if not title:
        # לטקטוק אין כיתוב ב-flat extraction – נופלים לשם הטראק ואז למזהה
        title = _clean_text(entry.get("track") or entry.get("description"))
    if not title:
        title = vid
    url = str(entry.get("webpage_url") or entry.get("url") or "").strip()
    ie = str(entry.get("ie_key") or entry.get("extractor_key") or "").lower()
    if not url.startswith("http"):
        # ב-flat extraction של יוטיוב מאגר ה-url הוא רק מזהה הסרטון
        if "youtu" in ie and vid:
            url = f"https://www.youtube.com/watch?v={vid}"
        else:
            return None
    return {
        "id": vid,
        "title": title or vid,
        "url": url,
        "duration": entry.get("duration"),
        "uploader": entry.get("uploader") or entry.get("channel") or "",
    }


def enumerate_playlist(url: str, cfg: dict, cookiefile: Path | None,
                      limit: int | None = None) -> dict:
    """שולף (בלי להוריד) את רשימת הסרטונים בפלייליסט/ערוץ."""
    if not Downloader.yt_dlp_available():
        raise DownloadError("חבילת yt-dlp לא מותקנת (pip install -r scripts/requirements.txt)")
    import yt_dlp  # noqa: PLC0415

    if limit is None:
        limit = int(cfg.get("max_playlist_items") or 200)
    target = _normalize_collection_url(url)
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "playlistend": int(limit),
        "logger": _YdlLogger(),
    }
    components = cfg.get("remote_components")
    if components is None:
        components = ["ejs:github"]
    if components:
        opts["remote_components"] = [str(name) for name in components]
    impersonate = _impersonate_target(cfg)
    if impersonate is not None:
        opts["impersonate"] = impersonate
    if cookiefile:
        opts["cookiefile"] = str(cookiefile)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)

    entries = (info or {}).get("entries")
    if entries is None:
        entries = [info]
    videos: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        item = _entry_to_video(entry)
        if not item or item["id"] in seen:
            continue
        seen.add(item["id"])
        videos.append(item)
    return {
        "url": url,
        "resolved_url": target,
        "title": (info or {}).get("title") or "",
        "count": len(videos),
        "videos": videos,
    }


def _auto_tag(cfg: dict) -> str:
    return (
        f"{cfg['release_tag_prefix']}-"
        f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{random.randint(0, 0xFFFF):04x}"
    )


def list_tag(run_id: str | None) -> str:
    """תג Release של רשימת סרטונים – כולל מזהה ההרצה כדי שהתוסף ימצא אותו."""
    run_id = str(run_id or "").strip()
    if run_id:
        return f"list-{run_id}"
    return f"list-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{random.randint(0, 0xFFFF):04x}"


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


# --------------------------------------------------------------------------- #
# עוגיות
# --------------------------------------------------------------------------- #
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
        components = self.cfg.get("remote_components")
        if components is None:
            components = ["ejs:github"]
        if components:
            opts["remote_components"] = [str(name) for name in components]
        impersonate = _impersonate_target(self.cfg)
        if impersonate is not None:
            opts["impersonate"] = impersonate
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
# אריזה כ-ZIP
# --------------------------------------------------------------------------- #
def zip_files(files: list[Path], workdir: Path) -> Path:
    """אורז את הקבצים שהורדו לקובץ ZIP אחד, ששמו כשם הסרטון שבתוכו.

    שם הקובץ נגזר מהקובץ הגדול ביותר (הסרטון/האודיו עצמו), כך שהזיפ תמיד
    נקרא כמו הסרטון – גם אם להורדה התלווים קבצים נוספים.
    """
    video = max(files, key=lambda p: p.stat().st_size)
    base = video.stem
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip() or "video"
    archive_path = workdir / f"{base}.zip"
    index = 1
    while archive_path.exists():
        archive_path = workdir / f"{base} ({index}).zip"
        index += 1
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            archive.write(path, arcname=path.name)
    log(f"  נארז ל-ZIP: {archive_path.name} ({human_size(archive_path.stat().st_size)})")
    return archive_path


# --------------------------------------------------------------------------- #
# עיבוד בקשה בודדת
# --------------------------------------------------------------------------- #
def download_url(url: str, cfg: dict, args, uploader: ReleaseUploader | None,
                 quality: str | None = None, cookiefile: Path | None = None,
                 release: dict | None = None, tag: str | None = None) -> dict:
    """מוריד קישור אחד, אורז ל-ZIP ומעלה כ-Release. מחזיר תוצאת JSON.

    כשמעבירים release קיים – מעלים אליו את האסימון (בחירה של כמה סרטונים
    בהרצה אחת). אחרת נוצר Release חדש לכל בקשה.
    """
    max_bytes = int(cfg["max_file_size_mb"]) * 1024 * 1024
    tag = tag or _auto_tag(cfg)
    result: dict = {"ok": False, "url": url, "quality": quality, "asset_name": None,
                    "size": 0, "release_url": None, "error": None}

    with tempfile.TemporaryDirectory(prefix="dl-") as tmp:
        workdir = Path(tmp)
        downloader = Downloader(cfg, workdir, quality=quality, cookiefile=cookiefile)
        try:
            files = downloader.download(url, max_bytes)
        except DownloadError as exc:
            log_error(str(exc))
            result["error"] = str(exc)
            return result
        except Exception as exc:  # noqa: BLE001 - yt-dlp זורק חריגות רבות
            log_error(f"הורדה נכשלה: {exc}")
            result["error"] = str(exc)
            return result

        archive = zip_files(files, workdir)
        size = archive.stat().st_size
        if size > max_bytes:
            result["error"] = f"הקובץ גדול מהמותר ({human_size(size)})"
            log_error(result["error"])
            return result
        result["asset_name"] = archive.name
        result["size"] = size

        if args.local:
            log_notice(f"[local] מוכן: {archive.name} ({human_size(size)}) ב-{archive}")
            result["ok"] = True
            return result

        if uploader is None:
            result["error"] = "אין טוקן/מאגר להעלאה"
            log_error(result["error"])
            return result
        try:
            if release is None:
                release = uploader.create_release(
                    tag,
                    f"הורדה {datetime.now(timezone.utc):%Y-%m-%d %H:%M}",
                    f"הורדה של {url}",
                )
            # ה-label נושא את שם הסרטון המקורי (עברית נשמרת), גם אם שם האסימון
            # עצמו מומר ל-ASCII על ידי GitHub.
            asset = uploader.upload_asset(release, archive, label=archive.name)
            log_notice(
                f"הועלה ל-GitHub: {asset['name']} · שם מלא: {archive.name} ({human_size(size)})"
            )
            result["ok"] = True
            result["asset_name"] = archive.name
            result["release_url"] = release.get("html_url")
            result["download_url"] = (
                asset.get("browser_download_url") or ReleaseUploader.direct_url(REPO, tag, asset["name"])
            )
            log(f"  קישור ישיר: {result['download_url']}")
        except ReleaseError as exc:
            log_error(f"ההעלאה נכשלה: {exc}")
            result["error"] = str(exc)
    return result


# --------------------------------------------------------------------------- #
# בדיקות
# --------------------------------------------------------------------------- #
def run_checks(cfg: dict) -> int:
    problems = 0
    checks: list[tuple[bool, str]] = []
    checks.append((bool(REPO), f"מאגר GitHub: {REPO or 'לא הוגדר (GITHUB_REPOSITORY)'}"))
    token_ok = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    checks.append((token_ok, "טוקן GitHub לכתיבה ל-Releases"))
    checks.append((Downloader.yt_dlp_available(), "חבילת yt-dlp מותקנת"))
    checks.append((shutil.which("ffmpeg") is not None, "ffmpeg מותקן (נדרש למיזוג וידאו+אודיו)"))
    for ok, message in checks:
        log(f"  {'✓' if ok else '✗'} {message}")
        if not ok:
            problems += 1
    log(f"סיכום בדיקות: {len(checks) - problems}/{len(checks)} תקינים")
    return 0 if problems == 0 else 1


# --------------------------------------------------------------------------- #
# שליפת רשימה ופרסומה (מצב list)
# --------------------------------------------------------------------------- #
def _friendly_list_error(exc: Exception, url: str) -> str:
    """מתרגם שגיאות נפוצות של yt-dlp להודעה מובנת למשתמש."""
    text = str(exc)
    host = urlparse(url).netloc.lower()
    if ("Failed to parse JSON" in text or "Unexpected response" in text) \
            and "tiktok" in host:
        return (
            "טיקטוק חסם את הבקשה (הגנת anti-bot). ודאו ש-yt-dlp מעודכן ומותקן curl_cffi "
            "לחיקוי דפדפן, או נסו שוב מאוחר יותר. " + text
        )
    return text


def run_list(url: str, cfg: dict, args, cookiefile: Path | None) -> int:
    """שולף את רשימת הסרטונים מאריך/פלייליסט.

    מקומית מדפיס את הרשימה; בענן מפרסם אותה כקובץ JSON ב-Release שתומת ב-
    list-<run_id>, כדי שהתוסף יוכל לשלוף אותה ולהציג בחירה למשתמש.
    """
    limit = int(cfg.get("max_playlist_items") or 200)
    log(f"שולף רשימת סרטונים: {url} (עד {limit} סרטונים)")
    try:
        payload = enumerate_playlist(url, cfg, cookiefile, limit)
    except DownloadError as exc:
        log_error(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - yt-dlp זורק חריגות רבות
        log_error(f"שליפת הרשימה נכשלה: {_friendly_list_error(exc, url)}")
        return 1

    count = payload["count"]
    log(f"נמצאו {count} סרטונים ברשימה")
    if count == 0:
        log_error("לא נמצאו סרטונים בקישור הזה. נסו קישור לפלייליסט או ללשונית הסרטונים של הערוץ.")
        return 1

    if args.local:
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            for item in payload["videos"]:
                log(f"  • {item['title']}")
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    if not (token and REPO):
        log_error("חסרים GITHUB_TOKEN / GITHUB_REPOSITORY לפרסום הרשימה")
        return 2
    uploader = ReleaseUploader(token, REPO)
    tag = list_tag(os.environ.get("GITHUB_RUN_ID"))

    with tempfile.TemporaryDirectory(prefix="list-") as tmp:
        json_path = Path(tmp) / "list.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            release = uploader.create_release(
                tag,
                f"רשימת סרטונים ({count})",
                f"רשימת הסרטונים של {payload.get('resolved_url') or url}",
            )
            uploader.upload_asset(release, json_path, asset_name="list.json", label="רשימת סרטונים")
        except ReleaseError as exc:
            log_error(f"פרסום הרשימה נכשל: {exc}")
            return 1

    log_notice(f"הרשימה פורסמה כ-Release בתג {tag} ({count} סרטונים)")
    if args.json:
        print(json.dumps({"ok": True, "count": count, "tag": tag, "title": payload["title"]},
                         ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- #
# פרמטרים
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="הורדת סרטון, אריזה כ-ZIP ופרסום כ-Release")
    parser.add_argument("--url", action="append", default=[],
                        help="קישור לסרטון (אפשר לחזור עליו לבחירה מרובה)")
    parser.add_argument("--list", action="store_true",
                        help="שולף רשימת סרטונים מפלייליסט/ערוץ בלי להוריד")
    parser.add_argument("--whole", action="store_true",
                        help="מוריד את כל הסרטונים שבערוץ/פלייליסט (רשימה שלמה)")
    parser.add_argument("--limit", type=int, default=None,
                        help="תקרה למספר הסרטונים במצב --whole")
    parser.add_argument("--check", action="store_true", help="בדיקת הגדרות וכלים")
    parser.add_argument(
        "--quality",
        choices=sorted(QUALITY_PRESETS),
        default=None,
        help="איכות ההורדה (best / 1080p / 720p / 480p / 360p / audio)",
    )
    parser.add_argument("--local", action="store_true", help="מוריד ואורז מקומית בלי להעלות")
    parser.add_argument("--json", action="store_true", help="פלט JSON")
    return parser


def _collect_urls(raw_urls: list[str]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for raw in raw_urls or []:
        url = str(raw or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = load_config()

    if args.check:
        code = run_checks(cfg)
        if args.json:
            print(json.dumps({"ok": code == 0}, ensure_ascii=False))
        return code

    urls = _collect_urls(args.url)
    if not urls:
        log_error("חסר --url. ההורדה מופעלת רק עם קישור.")
        return 2
    for url in urls:
        if not re.match(r"^https?://", url, re.IGNORECASE):
            log_error(f"קישור לא תקין: {url}")
            return 2

    cookie_dir = Path(tempfile.mkdtemp(prefix="yt-cookies-"))
    cookiefile = cookie_file_from_env(cookie_dir)

    if args.list:
        return run_list(urls[0], cfg, args, cookiefile)

    # ערוץ/פלייליסט שלם: שולפים את כל הסרטונים בענן ומורידים כל אחד מהם.
    # שולפים כאן ולא מהתוסף כדי לעקוף את תקרת הרשימה (max_playlist_items).
    if args.whole:
        limit = args.limit or int(cfg.get("max_channel_items") or 1000)
        log(f"מצב ערוץ שלם: שולף את כל הסרטונים (עד {limit})")
        try:
            collection = enumerate_playlist(urls[0], cfg, cookiefile, limit)
        except DownloadError as exc:
            log_error(str(exc))
            return 1
        except Exception as exc:  # noqa: BLE001 - yt-dlp זורק חריגות רבות
            log_error(f"שליפת רשימת הערוץ נכשלה: {_friendly_list_error(exc, urls[0])}")
            return 1
        videos = collection.get("videos") or []
        if not videos:
            log_error("לא נמצאו סרטונים בקישור הזה להורדה שלמה.")
            return 1
        log(f"נמצאו {len(videos)} סרטונים – מוריד את כולם (זה עשוי לקחת זמן)")
        urls = [item["url"] for item in videos]

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    uploader: ReleaseUploader | None = None
    if not args.local:
        if token and REPO:
            uploader = ReleaseUploader(token, REPO)
        else:
            log_error("חסרים GITHUB_TOKEN / GITHUB_REPOSITORY להעלאה (אפשר --local לבדיקה)")
            return 2

    if args.quality:
        preset = quality_preset(args.quality) or {}
        log(f"איכות מבוקשת: {preset.get('label', args.quality)}")
    log(f"מוריד {len(urls)} סרטונים" if len(urls) > 1 else f"מוריד: {urls[0]}")

    # כל הסרטונים של הרצה אחת נארזים יחד ל-Release אחד (נבחרו כמה – מופיעים
    # כתוצאה אחת עם כמה קבצים).
    shared_tag = _auto_tag(cfg)
    release: dict | None = None
    if uploader is not None:
        if len(urls) == 1:
            title = f"הורדה {datetime.now(timezone.utc):%Y-%m-%d %H:%M}"
            body = f"הורדה של {urls[0]}"
        else:
            title = f"הורדה של {len(urls)} סרטונים {datetime.now(timezone.utc):%Y-%m-%d %H:%M}"
            body = "\n".join(f"- {u}" for u in urls)
        try:
            release = uploader.create_release(shared_tag, title, body)
        except ReleaseError as exc:
            log_error(f"יצירת ה-Release נכשלה: {exc}")
            return 1

    results = [
        download_url(url, cfg, args, uploader, quality=args.quality,
                     cookiefile=cookiefile, release=release, tag=shared_tag)
        for url in urls
    ]
    ok_count = sum(1 for r in results if r["ok"])

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as fh:
                for item in results:
                    if item["ok"]:
                        fh.write(f"- ✅ [{item['asset_name']}]({item.get('download_url', '')}) "
                                 f"({human_size(item['size'])})\n")
                    else:
                        fh.write(f"- ❌ {item['url']} – {item.get('error') or 'נכשל'}\n")
        except OSError:
            pass

    if args.json:
        print(json.dumps({"ok": ok_count == len(results), "count": len(results),
                          "ok_count": ok_count, "results": results}, ensure_ascii=False))
    if ok_count != len(results):
        log_error(f"{len(results) - ok_count} מתוך {len(results)} הורדות נכשלו")
    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
