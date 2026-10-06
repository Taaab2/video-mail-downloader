#!/usr/bin/env python3
"""ניקוי Releases ישנים: מוחק Release (והתג שלו) שעברו יותר מכמה שעות.

ההורדות מפורסמות כ-Release כדי לאפשר קישור הורדה ישיר. כדי שלא יצטברו
שם קבצים לנצח, ריצה מתוזמנת פעם ביום מוחקת הכול מלבד התוצאות הטריות.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from download import load_config, log, log_error, log_notice  # noqa: E402

API_ROOT = os.environ.get("GITHUB_API_URL", "https://api.github.com")
TIMEOUT = 60


def _session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "video-downloader-cleanup",
        }
    )
    return session


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def cleanup(token: str, repo: str, hours: int) -> int:
    """מוחק Releases ותגים ישנים. מחזיר מספר הפריטים שנמחקו."""
    session = _session(token)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    resp = session.get(f"{API_ROOT}/repos/{repo}/releases?per_page=100", timeout=TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"שליפת ה-Releases נכשלה ({resp.status_code}): {resp.text[:300]}")

    deleted = 0
    for rel in resp.json():
        created = _parse_time(rel.get("created_at"))
        if created > cutoff:
            continue
        tag = rel.get("tag_name")
        del_resp = session.delete(f"{API_ROOT}/repos/{repo}/releases/{rel['id']}", timeout=TIMEOUT)
        if del_resp.status_code not in (204, 404):
            log_error(f"מחיקת ה-Release {tag} נכשלה ({del_resp.status_code})")
            continue
        # התג נשאר אחרי מחיקת ה-Release – מנקים גם אותו
        session.delete(f"{API_ROOT}/repos/{repo}/git/refs/tags/{tag}", timeout=TIMEOUT)
        log_notice(f"נמחק Release ישן: {tag} ({created:%Y-%m-%d %H:%M})")
        deleted += 1

    log(f"הניקוי הסתיים: נמחקו {deleted} Releases ישנים (מעל {hours} שעות).")
    return deleted


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ניקוי Releases ישנים")
    parser.add_argument("--hours", type=int, default=None, help="גיל מינימלי בשעות למחיקה")
    parser.add_argument("--json", action="store_true", help="פלט JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = load_config()
    hours = args.hours if args.hours is not None else int(cfg.get("delete_releases_after_hours") or 0)
    if hours <= 0:
        log("ניקוי Releases מושבת (delete_releases_after_hours=0).")
        if args.json:
            print(json.dumps({"ok": True, "deleted": 0}, ensure_ascii=False))
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or "/" not in repo:
        log_error("חסרים GITHUB_TOKEN / GITHUB_REPOSITORY לניקוי")
        return 2

    try:
        deleted = cleanup(token, repo, hours)
    except Exception as exc:  # noqa: BLE001
        log_error(f"הניקוי נכשל: {exc}")
        return 1
    if args.json:
        print(json.dumps({"ok": True, "deleted": deleted}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
