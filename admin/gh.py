"""לקוח GitHub API לממשק הניהול המקומי.

כולל: קריאה/כתיבה של קבצים במאגר, הרצת Workflow, צפייה בהרצות ולוגים,
קריאת Releases, וכתיבת Secrets (עם PyNaCl אם מותקן).
"""

from __future__ import annotations

import base64
import io
import os
import zipfile
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_ROOT = "https://api.github.com"
TIMEOUT = 30


class GitHubError(RuntimeError):
    pass


def _session(token: str) -> requests.Session:
    session = requests.Session()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "download-admin-console",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    session.headers.update(headers)
    retry = Retry(total=3, backoff_factor=1.0, status_forcelist=(500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class GitHubClient:
    def __init__(self, token: str, repo: str):
        self.token = token or ""
        self.repo = repo or ""
        self.session = _session(self.token)

    # ------------------------------------------------------------------ #
    # עזר
    # ------------------------------------------------------------------ #
    def _request(self, method: str, path: str, expect: tuple[int, ...] = (200,), **kwargs) -> Any:
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        resp = self.session.request(method, url, timeout=TIMEOUT, **kwargs)
        if resp.status_code not in expect:
            detail = ""
            try:
                detail = resp.json().get("message", "")
            except Exception:
                detail = resp.text[:200]
            raise GitHubError(f"{method} {path} -> {resp.status_code} {detail}".strip())
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def repo_info(self) -> dict:
        return self._request("GET", f"/repos/{self.repo}")

    def whoami(self) -> dict:
        return self._request("GET", "/user")

    def default_branch(self) -> str:
        try:
            return self.repo_info().get("default_branch") or "main"
        except GitHubError:
            return "main"

    # ------------------------------------------------------------------ #
    # קבצים במאגר
    # ------------------------------------------------------------------ #
    def get_file(self, path: str, ref: str | None = None) -> tuple[str | None, str | None]:
        """מחזיר (תוכן טקסטואלי, sha). אם הקובץ לא קיים – (None, None)."""
        url = f"/repos/{self.repo}/contents/{path}"
        if ref:
            url += f"?ref={ref}"
        try:
            data = self._request("GET", url)
        except GitHubError as exc:
            if "404" in str(exc):
                return None, None
            raise
        if isinstance(data, list):
            raise GitHubError(f"{path} הוא תיקייה ולא קובץ")
        content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        return content, data.get("sha")

    def put_file(self, path: str, content: str, message: str, sha: str | None = None,
                 branch: str | None = None) -> dict:
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if sha:
            payload["sha"] = sha
        if branch:
            payload["branch"] = branch
        return self._request("PUT", f"/repos/{self.repo}/contents/{path}", expect=(200, 201), json=payload)

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #
    def list_workflow_runs(self, workflow_file: str, limit: int = 8) -> list[dict]:
        data = self._request(
            "GET",
            f"/repos/{self.repo}/actions/workflows/{workflow_file}/runs?per_page={limit}",
            expect=(200, 404),
        )
        if not data or "workflow_runs" not in data:
            return []
        runs = []
        for run in data["workflow_runs"]:
            runs.append(
                {
                    "id": run["id"],
                    "number": run.get("run_number"),
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "event": run.get("event"),
                    "created_at": run.get("created_at"),
                    "html_url": run.get("html_url"),
                    "title": run.get("display_title") or (run.get("head_commit") or {}).get("message", ""),
                }
            )
        return runs

    def workflow_state(self, workflow_file: str) -> str:
        try:
            data = self._request("GET", f"/repos/{self.repo}/actions/workflows/{workflow_file}")
            return data.get("state", "unknown")
        except GitHubError:
            return "missing"

    def dispatch(self, workflow_file: str, inputs: dict[str, Any] | None = None,
                 ref: str | None = None) -> None:
        branch = ref or self.default_branch()
        self._request(
            "POST",
            f"/repos/{self.repo}/actions/workflows/{workflow_file}/dispatches",
            expect=(204,),
            json={"ref": branch, "inputs": inputs or {}},
        )

    def run_jobs(self, run_id: int) -> list[dict]:
        data = self._request("GET", f"/repos/{self.repo}/actions/runs/{run_id}/jobs")
        return data.get("jobs", [])

    def run_logs_text(self, run_id: int, max_chars: int = 40_000) -> str:
        """מוריד את קובץ הלוגים (zip) ומחזיר טקסט מרוכז של שלבי הכשל."""
        url = f"{API_ROOT}/repos/{self.repo}/actions/runs/{run_id}/logs"
        resp = self.session.get(url, timeout=60, allow_redirects=True)
        if resp.status_code != 200:
            raise GitHubError(f"לא ניתן להוריד לוגים ({resp.status_code})")
        chunks: list[str] = []
        with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
            for name in archive.namelist():
                with archive.open(name) as fh:
                    text = fh.read().decode("utf-8", errors="replace")
                chunks.append(f"===== {name} =====\n{text}")
        joined = "\n".join(chunks)
        if len(joined) > max_chars:
            joined = "…(קטע אחרון)…\n" + joined[-max_chars:]
        return joined

    # ------------------------------------------------------------------ #
    # Releases
    # ------------------------------------------------------------------ #
    def list_releases(self, limit: int = 10) -> list[dict]:
        data = self._request("GET", f"/repos/{self.repo}/releases?per_page={limit}")
        releases = []
        for rel in data:
            releases.append(
                {
                    "tag": rel.get("tag_name"),
                    "name": rel.get("name"),
                    "created_at": rel.get("created_at"),
                    "url": rel.get("html_url"),
                    "assets": [
                        {
                            "name": asset.get("name"),
                            "size": asset.get("size"),
                            "downloads": asset.get("download_count"),
                            "url": asset.get("browser_download_url"),
                        }
                        for asset in rel.get("assets", [])
                    ],
                }
            )
        return releases

    def delete_release(self, tag: str) -> None:
        rel = self._request("GET", f"/repos/{self.repo}/releases/tags/{tag}")
        self._request("DELETE", f"/repos/{self.repo}/releases/{rel['id']}", expect=(204,))
        try:
            ref = self._request("GET", f"/repos/{self.repo}/git/ref/tags/{tag}")
            self._request("DELETE", f"/repos/{self.repo}/git/refs/tags/{tag}", expect=(204,))
        except GitHubError:
            pass

    # ------------------------------------------------------------------ #
    # Secrets
    # ------------------------------------------------------------------ #
    def secret_names(self) -> list[str]:
        try:
            data = self._request("GET", f"/repos/{self.repo}/actions/secrets")
            return [item["name"] for item in data.get("secrets", [])]
        except GitHubError:
            return []

    def variable_names(self) -> list[str]:
        try:
            data = self._request("GET", f"/repos/{self.repo}/actions/variables")
            return [item["name"] for item in data.get("variables", [])]
        except GitHubError:
            return []

    def set_variable(self, name: str, value: str) -> None:
        """יוצר Actions Variable, ואם היא כבר קיימת – מעדכן אותה."""
        try:
            self._request(
                "POST",
                f"/repos/{self.repo}/actions/variables",
                expect=(201, 204, 409),
                json={"name": name, "value": value},
            )
        except GitHubError:
            self._request(
                "PATCH",
                f"/repos/{self.repo}/actions/variables/{name}",
                expect=(204,),
                json={"name": name, "value": value},
            )

    def set_secret(self, name: str, value: str) -> None:
        try:
            from nacl import encoding, public  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise GitHubError(
                "כדי לכתוב סודות מכאן צריך את החבילה pynacl. התקינו עם: pip install pynacl"
            ) from exc
        key_data = self._request("GET", f"/repos/{self.repo}/actions/secrets/public-key")
        public_key = public.PublicKey(key_data["key"].encode("utf-8"), encoding.Base64Encoder())
        sealed = public.SecretBox(public_key).encrypt(value.encode("utf-8"))
        encrypted = base64.b64encode(sealed).decode("ascii")
        self._request(
            "PUT",
            f"/repos/{self.repo}/actions/secrets/{name}",
            expect=(201, 204),
            json={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
        )


def detect_repo_from_git(project_root: str) -> str:
    """מנסה לזהות owner/repo מה-remote של git."""
    import subprocess  # noqa: PLC0415

    try:
        out = subprocess.run(
            ["git", "-C", project_root, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    if not out:
        return ""
    out = out.removesuffix(".git")
    if out.startswith("git@"):
        out = out.split(":", 1)[-1]
    elif "://" in out:
        out = out.split("://", 1)[1].split("/", 1)[-1]
    parts = [p for p in out.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else ""


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
