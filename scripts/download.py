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
    """אורז את הקבצים שהורדו לקובץ ZIP אחד. תמיד מחזיר קובץ .zip."""
    if len(files) == 1:
        base = files[0].stem
    else:
        base = "download"
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip() or "download"
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
                 quality: str | None = None, cookiefile: Path | None = None) -> dict:
    """מוריד קישור אחד, אורז ל-ZIP ומעלה כ-Release. מחזיר תוצאת JSON."""
    max_bytes = int(cfg["max_file_size_mb"]) * 1024 * 1024
    tag = f"{cfg['release_tag_prefix']}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{random.randint(0, 0xFFFF):04x}"
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
            release = uploader.create_release(
                tag,
                f"הורדה {datetime.now(timezone.utc):%Y-%m-%d %H:%M}",
                f"הורדה של {url}",
            )
            asset = uploader.upload_asset(release, archive)
            log_notice(f"הועלה ל-GitHub: {asset['name']} ({human_size(size)})")
            result["ok"] = True
            result["asset_name"] = asset["name"]
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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="הורדת סרטון, אריזה כ-ZIP ופרסום כ-Release")
    parser.add_argument("--url", help="קישור לסרטון")
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


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = load_config()

    if args.check:
        code = run_checks(cfg)
        if args.json:
            print(json.dumps({"ok": code == 0}, ensure_ascii=False))
        return code

    if not args.url:
        log_error("חסר --url. ההורדה מופעלת רק עם קישור.")
        return 2
    url = args.url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        log_error(f"קישור לא תקין: {url}")
        return 2

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
    log(f"מוריד: {url}")

    cookie_dir = Path(tempfile.mkdtemp(prefix="yt-cookies-"))
    cookiefile = cookie_file_from_env(cookie_dir)

    result = download_url(url, cfg, args, uploader, quality=args.quality, cookiefile=cookiefile)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path and result["ok"]:
        try:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write(f"- ✅ [{result['asset_name']}]({result.get('download_url', '')}) "
                         f"({human_size(result['size'])})\n")
        except OSError:
            pass

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
