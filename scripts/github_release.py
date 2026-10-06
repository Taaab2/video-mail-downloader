"""העלאת קבצים כ-Assets של GitHub Release וקבלת קישור הורדה ישיר."""

from __future__ import annotations

import os
import re
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_ROOT = os.environ.get("GITHUB_API_URL", "https://api.github.com")
DEFAULT_TIMEOUT = 60
UPLOAD_TIMEOUT = 1800


class ReleaseError(RuntimeError):
    pass


def _session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "email-video-downloader",
        }
    )
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


class ReleaseUploader:
    """יוצר Release (או משתמש בקיים) ומעלה אליו קבצים."""

    def __init__(self, token: str, repo: str):
        if not token:
            raise ReleaseError("חסר GITHUB_TOKEN")
        if "/" not in repo:
            raise ReleaseError(f"שם מאגר לא תקין: {repo!r} (צריך להיות owner/repo)")
        self.repo = repo
        self.session = _session(token)

    def create_release(self, tag: str, name: str, body: str = "") -> dict:
        url = f"{API_ROOT}/repos/{self.repo}/releases"
        payload = {"tag_name": tag, "name": name, "body": body, "draft": False, "prerelease": False}
        resp = self.session.post(url, json=payload, timeout=DEFAULT_TIMEOUT)
        if resp.status_code == 422:
            # 422 מגיע בדרך כלל כשהתג כבר קיים – אז נשלוף את ה-Release הקיים.
            first_error = resp.text[:300]
            lookup = self.session.get(f"{url}/tags/{tag}", timeout=DEFAULT_TIMEOUT)
            if lookup.status_code < 400:
                return lookup.json()
            raise ReleaseError(
                "יצירת Release נכשלה (422): "
                f"{first_error}. אם המאגר ריק (בלי קומיטים) אי אפשר ליצור Release – "
                "צריך לדחוף את הקוד פעם אחת."
            )
        if resp.status_code >= 400:
            raise ReleaseError(f"יצירת Release נכשלה ({resp.status_code}): {resp.text[:400]}")
        return resp.json()

    def upload_asset(self, release: dict, path: Path, asset_name: str | None = None,
                     label: str | None = None) -> dict:
        # GitHub הופך כל תו שאינו ASCII לשם אסימון מקוצר, ולכן שומרים את השם
        # המקורי (עברית וכו') ב-label – שם שמוצג ב-UI ובממשקי ה-API.
        asset_name = self._safe_asset_name(asset_name or path.name)
        params = {"name": asset_name}
        if label:
            params["label"] = label[:200]
        upload_url = release["upload_url"].split("{")[0]
        with path.open("rb") as fh:
            resp = self.session.post(
                upload_url,
                params=params,
                data=fh,
                headers={"Content-Type": "application/octet-stream"},
                timeout=UPLOAD_TIMEOUT,
            )
        if resp.status_code >= 400:
            raise ReleaseError(f"העלאת הקובץ {asset_name} נכשלה ({resp.status_code}): {resp.text[:400]}")
        return resp.json()

    def clear_asset(self, asset_id: int) -> None:
        self.session.delete(f"{API_ROOT}/repos/{self.repo}/releases/assets/{asset_id}", timeout=DEFAULT_TIMEOUT)

    @staticmethod
    def _safe_asset_name(name: str) -> str:
        """שם אסימון בטוח ל-URL: ASCII בלבד, בלי תווים אסורים (כמו GitHub עושה)."""
        name = name.replace("/", "-").replace("\\", "-")
        suffix = Path(name).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,5}", suffix or ""):
            suffix = ""
        stem = name[: len(name) - len(suffix)] if suffix else name
        ascii_stem = stem.encode("ascii", "ignore").decode("ascii")
        ascii_stem = re.sub(r"[^A-Za-z0-9._-]+", ".", ascii_stem)
        ascii_stem = re.sub(r"\.{2,}", ".", ascii_stem).strip("._- ")
        return (ascii_stem[: 180 - len(suffix)] or "video") + suffix

    @staticmethod
    def direct_url(repo: str, tag: str, asset_name: str) -> str:
        return f"https://github.com/{repo}/releases/download/{tag}/{asset_name}"

    @staticmethod
    def release_page(repo: str, tag: str) -> str:
        return f"https://github.com/{repo}/releases/tag/{tag}"
