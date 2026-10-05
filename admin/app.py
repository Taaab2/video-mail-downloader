#!/usr/bin/env python3
"""ממשק הניהול המקומי – ניהול הרשימה הלבנה, ההגדרות, ההרצות וההורדות.

רץ על המחשב שלך בלבד (127.0.0.1) ומדבר עם GitHub API.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gh import GitHubClient, GitHubError, detect_repo_from_git  # noqa: E402

ADMIN_DIR = Path(__file__).resolve().parent
ROOT = ADMIN_DIR.parent
UI_DIR = ADMIN_DIR / "ui"
ENV_PATH = ADMIN_DIR / ".env"
WORKFLOW_FILE = "downloader.yml"

SECRET_KEYS = ["MAIL_USER", "MAIL_PASSWORD", "GOOGLE_SERVICE_ACCOUNT_JSON"]
VARIABLE_KEYS = [
    "MAIL_FROM",
    "IMAP_HOST",
    "IMAP_PORT",
    "SMTP_HOST",
    "SMTP_PORT",
    "DRIVE_FOLDER_ID",
]

app = Flask(__name__, static_folder=None)


# --------------------------------------------------------------------------- #
# הגדרות מקומיות (admin/.env)
# --------------------------------------------------------------------------- #
def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def save_env_file(path: Path, values: dict[str, str]) -> None:
    lines = ["# נוצר על ידי ממשק הניהול – הקובץ הזה לא נכנס ל-Git", ""]
    for key, value in values.items():
        if value is None:
            continue
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class Settings:
    """הגדרות שנטענות מחדש בכל בקשה, כדי שעדכון מהממשק ייכנס לתוקף מיד."""

    @property
    def env(self) -> dict[str, str]:
        values = load_env_file(ENV_PATH)
        merged = dict(values)
        for key in ("GITHUB_TOKEN", "GITHUB_REPO", "ADMIN_PORT"):
            if os.environ.get(key):
                merged.setdefault(key, os.environ[key])
        return merged

    @property
    def token(self) -> str:
        return self.env.get("GITHUB_TOKEN", "")

    @property
    def repo(self) -> str:
        return self.env.get("GITHUB_REPO", "") or detect_repo_from_git(str(ROOT))


settings = Settings()


def client() -> GitHubClient:
    if not settings.token:
        raise GitHubError("לא הוגדר טוקן GitHub. הדביקו טוקן בלשונית 'הגדרות'.")
    if not settings.repo:
        raise GitHubError("לא זוהה מאגר GitHub. הזינו owner/repo בלשונית 'הגדרות'.")
    return GitHubClient(settings.token, settings.repo)


# --------------------------------------------------------------------------- #
# קבצי הגדרות מקומיים
# --------------------------------------------------------------------------- #
def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ok(**payload: Any):
    return jsonify({"ok": True, **payload})


def fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


# --------------------------------------------------------------------------- #
# ממשק
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return send_from_directory(UI_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename: str):
    return send_from_directory(UI_DIR, filename)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/state")
def api_state():
    whitelist = read_json(ROOT / "whitelist.json", {"allow_all": False, "users": []})
    config = read_json(ROOT / "config.json", {})
    state: dict[str, Any] = {
        "ok": True,
        "repo": settings.repo,
        "repo_detected_from_git": detect_repo_from_git(str(ROOT)),
        "token_set": bool(settings.token),
        "whitelist": whitelist,
        "config": config,
        "workflow_file": WORKFLOW_FILE,
        "remote": {},
        "runs": [],
        "releases": [],
        "problems": [],
    }

    if settings.token and settings.repo:
        try:
            gh = client()
            info = gh.repo_info()
            state["remote"] = {
                "full_name": info.get("full_name"),
                "private": info.get("private"),
                "default_branch": info.get("default_branch"),
                "permissions": info.get("permissions", {}),
                "html_url": info.get("html_url"),
                "workflow_state": gh.workflow_state(WORKFLOW_FILE),
                "secret_names": gh.secret_names(),
                "variable_names": gh.variable_names(),
                "whitelist_sha": (gh.get_file("whitelist.json")[1] or ""),
            }
            state["remote"]["whitelist"] = gh.get_file("whitelist.json")[0]
            state["runs"] = gh.list_workflow_runs(WORKFLOW_FILE)
            state["releases"] = gh.list_releases()
            if info.get("private"):
                state["problems"].append(
                    "המאגר פרטי – קישורי ההורדה של GitHub לא יעבדו בלי התחברות. "
                    "כדאי להפוך את המאגר לציבורי (Public)."
                )
            if state["remote"]["workflow_state"] == "missing":
                state["problems"].append("קובץ ה-Workflow לא נמצא במאגר. דחפו את הקוד ל-GitHub.")
        except GitHubError as exc:
            state["problems"].append(str(exc))
    else:
        missing = []
        if not settings.token:
            missing.append("טוקן GitHub")
        if not settings.repo:
            missing.append("שם המאגר (owner/repo)")
        state["problems"].append("חסרים: " + ", ".join(missing))

    return jsonify(state)


@app.put("/api/whitelist")
def api_save_whitelist():
    payload = request.get_json(force=True, silent=True) or {}
    push = bool(payload.pop("push", False))
    users = []
    for entry in payload.get("users", []):
        email = str(entry.get("email", "")).strip()
        if not email:
            continue
        users.append(
            {
                "email": email,
                "name": str(entry.get("name", "")).strip(),
                "active": bool(entry.get("active", True)),
                "note": str(entry.get("note", "")).strip(),
            }
        )
    data = {"allow_all": bool(payload.get("allow_all", False)), "users": users}
    write_json(ROOT / "whitelist.json", data)

    result: dict[str, Any] = {"local": True, "pushed": False}
    if push:
        try:
            gh = client()
            content, sha = gh.get_file("whitelist.json")
            commit = gh.put_file(
                "whitelist.json",
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                "עדכון הרשימה הלבנה מממשק הניהול",
                sha,
            )
            result["pushed"] = True
            result["commit"] = commit.get("commit", {}).get("html_url")
        except GitHubError as exc:
            return fail(f"נשמר מקומית, אבל ההעלאה ל-GitHub נכשלה: {exc}")
    return ok(**result)


@app.put("/api/config")
def api_save_config():
    payload = request.get_json(force=True, silent=True) or {}
    push = bool(payload.pop("push", False))
    current = read_json(ROOT / "config.json", {})
    current.update(payload)
    write_json(ROOT / "config.json", current)

    result: dict[str, Any] = {"pushed": False, "config": current}
    if push:
        try:
            gh = client()
            _, sha = gh.get_file("config.json")
            commit = gh.put_file(
                "config.json",
                json.dumps(current, ensure_ascii=False, indent=2) + "\n",
                "עדכון הגדרות ההרצה מממשק הניהול",
                sha,
            )
            result["pushed"] = True
            result["commit"] = commit.get("commit", {}).get("html_url")
        except GitHubError as exc:
            return fail(f"נשמר מקומית, אבל ההעלאה ל-GitHub נכשלה: {exc}")
    return ok(**result)


@app.post("/api/pull")
def api_pull():
    try:
        gh = client()
        pulled: list[str] = []
        for name in ("whitelist.json", "config.json"):
            content, _ = gh.get_file(name)
            if content:
                (ROOT / name).write_text(content, encoding="utf-8")
                pulled.append(name)
        return ok(pulled=pulled)
    except GitHubError as exc:
        return fail(str(exc))


@app.put("/api/settings")
def api_save_settings():
    payload = request.get_json(force=True, silent=True) or {}
    env = load_env_file(ENV_PATH)
    for key in ("GITHUB_TOKEN", "GITHUB_REPO", "ADMIN_PORT"):
        if key in payload:
            value = str(payload[key]).strip()
            if value:
                env[key] = value
            else:
                env.pop(key, None)
    save_env_file(ENV_PATH, env)
    return ok(repo=settings.repo, token_set=bool(settings.token))


@app.post("/api/secrets")
def api_set_secrets():
    payload = request.get_json(force=True, silent=True) or {}
    secrets_map = {
        key: str(payload.get(key, "") or "").strip()
        for key in SECRET_KEYS
        if str(payload.get(key, "") or "").strip()
    }
    variables_map = {
        key: str(payload.get(key, "") or "").strip()
        for key in VARIABLE_KEYS
        if str(payload.get(key, "") or "").strip()
    }
    if not secrets_map and not variables_map:
        return fail("אין מה לשמור")
    if "GOOGLE_SERVICE_ACCOUNT_JSON" in secrets_map:
        try:
            parsed = json.loads(secrets_map["GOOGLE_SERVICE_ACCOUNT_JSON"])
        except json.JSONDecodeError as exc:
            return fail(f"מפתח חשבון השירות אינו JSON תקין: {exc}")
        if not parsed.get("client_email"):
            return fail("במפתח חסר client_email – ודאו שזה קובץ המפתח של חשבון שירות")
    try:
        gh = client()
        for key, value in secrets_map.items():
            gh.set_secret(key, value)
        for key, value in variables_map.items():
            gh.set_variable(key, value)
        # שומר גם מקומית כדי שבדיקת החיבור תוכל להשתמש בהם
        env = load_env_file(ENV_PATH)
        env.update(secrets_map)
        env.update(variables_map)
        save_env_file(ENV_PATH, env)
        return ok(configured=gh.secret_names(), variables=gh.variable_names())
    except GitHubError as exc:
        return fail(str(exc))


@app.post("/api/run")
def api_run():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        gh = client()
        inputs = {
            "dry_run": bool(payload.get("dry_run", False)),
            "max_messages": str(payload.get("max_messages", 0) or 0),
        }
        gh.dispatch(WORKFLOW_FILE, inputs)
        return ok(message="ההרצה נשלחה ל-GitHub. התוצאה תופיע תוך מספר שניות.")
    except GitHubError as exc:
        return fail(str(exc))


@app.get("/api/runs")
def api_runs():
    try:
        return ok(runs=client().list_workflow_runs(WORKFLOW_FILE))
    except GitHubError as exc:
        return fail(str(exc))


@app.get("/api/runs/<int:run_id>/logs")
def api_run_logs(run_id: int):
    try:
        return ok(logs=client().run_logs_text(run_id))
    except GitHubError as exc:
        return fail(str(exc))


@app.get("/api/releases")
def api_releases():
    try:
        return ok(releases=client().list_releases())
    except GitHubError as exc:
        return fail(str(exc))


@app.post("/api/releases/delete")
def api_delete_release():
    payload = request.get_json(force=True, silent=True) or {}
    tag = str(payload.get("tag", "")).strip()
    if not tag:
        return fail("חסר tag")
    try:
        client().delete_release(tag)
        return ok()
    except GitHubError as exc:
        return fail(str(exc))


@app.post("/api/selftest")
def api_selftest():
    """מריץ בדיקות מקומיות של תהליך ההורדה (הגדרות, IMAP, SMTP, כלים)."""
    env = dict(os.environ)
    local = load_env_file(ENV_PATH)
    for key, value in local.items():
        env[key] = value
    env["GITHUB_REPOSITORY"] = settings.repo
    env["GITHUB_TOKEN"] = settings.token or env.get("GITHUB_TOKEN", "")
    env["SKIP_MAIL_LOGIN_CHECK"] = "0" if payload_check_login() else "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "process_inbox.py"), "--check"],
            capture_output=True, text=True, timeout=120, cwd=str(ROOT), env=env,
            encoding="utf-8", errors="replace",
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return ok(output=output.strip(), code=proc.returncode)
    except subprocess.TimeoutExpired:
        return fail("הבדיקה נתקעה (timeout)")
    except OSError as exc:
        return fail(f"לא ניתן להריץ את הבדיקה: {exc}")


def payload_check_login() -> bool:
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        return False
    return bool(payload.get("check_login", True))


# --------------------------------------------------------------------------- #
# הרצה
# --------------------------------------------------------------------------- #
def open_browser_later(url: str) -> None:
    if os.environ.get("NO_BROWSER"):
        return
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()


def main() -> None:
    port = int(settings.env.get("ADMIN_PORT") or os.environ.get("ADMIN_PORT") or 8765)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 62)
    print("  ממשק ניהול ההורדות")
    print(f"  {url}")
    print("  לעצירה: Ctrl+C")
    print("=" * 62)
    if settings.repo:
        print(f"  מאגר: {settings.repo}")
    else:
        print("  מאגר: לא זוהה – הזינו בלשונית 'הגדרות'")
    print()
    open_browser_later(url)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
