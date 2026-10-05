"""העלאה אופציונלית ל-Google Drive וקבלת קישור הורדה ישיר.

משתמש בחשבון שירות (Service Account):
1. יוצרים Service Account ב-Google Cloud ומפעילים את Drive API.
2. מורידים מפתח JSON ושומרים אותו כ-Secret בשם GOOGLE_SERVICE_ACCOUNT_JSON.
3. יוצרים תיקייה ב-Drive *שלכם*, משתפים אותה עם כתובת המייל של חשבון השירות
   בהרשאת "עורך", ושומרים את מזהה התיקייה ב-DRIVE_FOLDER_ID.
   (חשוב: לחשבון שירות אין מכסת אחסון משלו, ולכן ההעלאה חייבת להיות לתיקייה
   ששותפה איתו באחסון שלכם או ב-Shared Drive.)
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path

import requests

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
SCOPES = ["https://www.googleapis.com/auth/drive"]
TIMEOUT = 60
UPLOAD_TIMEOUT = 3600


class DriveError(RuntimeError):
    pass


class DriveUploader:
    """מעלה קבצים ל-Drive ומחזיר קישור הורדה ישיר."""

    def __init__(self, service_account_json: str, folder_id: str = ""):
        if not service_account_json or not service_account_json.strip():
            raise DriveError("חסר GOOGLE_SERVICE_ACCOUNT_JSON")
        try:
            from google.auth.transport.requests import Request  # noqa: PLC0415
            from google.oauth2 import service_account  # noqa: PLC0415
        except ImportError as exc:
            raise DriveError(
                "חסרה החבילה google-auth. התקינו עם: pip install -r scripts/requirements.txt"
            ) from exc

        try:
            info = json.loads(service_account_json)
        except json.JSONDecodeError as exc:
            raise DriveError(f"GOOGLE_SERVICE_ACCOUNT_JSON אינו JSON תקין: {exc}") from exc

        self.client_email = info.get("client_email", "")
        self.folder_id = (folder_id or "").strip() or "root"
        try:
            self.credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            self.credentials.refresh(Request())
        except Exception as exc:  # noqa: BLE001 - google-auth זורק חריגות רבות
            raise DriveError(f"הזדהות מול Google נכשלה: {exc}") from exc
        self.token = self.credentials.token

    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def upload(self, path: Path, name: str | None = None) -> dict:
        """מעלה קובץ ב-Resumable Upload ומחזיר {'id','name','url'}."""
        name = (name or path.name).strip() or path.name
        size = path.stat().st_size
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"

        metadata: dict = {"name": name}
        if self.folder_id and self.folder_id != "root":
            metadata["parents"] = [self.folder_id]

        init = requests.post(
            f"{DRIVE_UPLOAD_API}/files",
            params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": "id,name,webViewLink"},
            headers={
                **self._headers(),
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime,
                "X-Upload-Content-Length": str(size),
            },
            json=metadata,
            timeout=TIMEOUT,
        )
        if init.status_code >= 400:
            raise DriveError(self._error_message("פתיחת ההעלאה נכשלה", init))
        session_url = init.headers.get("Location")
        if not session_url:
            raise DriveError("Google לא החזיר כתובת העלאה (Location)")

        with path.open("rb") as fh:
            put = requests.put(
                session_url,
                data=fh,
                headers={"Content-Type": mime, "Content-Length": str(size)},
                timeout=UPLOAD_TIMEOUT,
            )
        if put.status_code >= 400:
            raise DriveError(self._error_message("העלאת הקובץ נכשלה", put))
        payload = put.json()
        file_id = payload.get("id")
        if not file_id:
            raise DriveError("ההעלאה הסתיימה בלי מזהה קובץ")

        self._share_anyone(file_id)
        return {"id": file_id, "name": payload.get("name") or name, "url": self.direct_url(file_id)}

    def _share_anyone(self, file_id: str) -> None:
        """הופך את הקובץ לזמין לכל מי שיש לו הקישור. אם אין הרשאה – ממשיכים בלעדיו."""
        try:
            resp = requests.post(
                f"{DRIVE_API}/files/{file_id}/permissions",
                params={"supportsAllDrives": "true"},
                headers={**self._headers(), "Content-Type": "application/json"},
                json={"role": "reader", "type": "anyone"},
                timeout=TIMEOUT,
            )
            if resp.status_code >= 400:
                print(
                    "  [drive] לא הצלחתי לפתוח את הקובץ לשיתוף ציבורי – "
                    f"הקישור ידרוש התחברות: {resp.text[:200]}",
                    flush=True,
                )
        except requests.RequestException as exc:
            print(f"  [drive] שיתוף הקובץ נכשל: {exc}", flush=True)

    def delete(self, file_id: str) -> None:
        requests.delete(
            f"{DRIVE_API}/files/{file_id}",
            params={"supportsAllDrives": "true"},
            headers=self._headers(),
            timeout=TIMEOUT,
        )

    @staticmethod
    def _error_message(prefix: str, resp: requests.Response) -> str:
        detail = resp.text[:300]
        hint = ""
        if "storageQuotaExceeded" in detail:
            hint = (
                " (לחשבון שירות אין אחסון משלו – צריך לשתף איתו תיקייה באחסון שלך "
                "ולהגדיר את DRIVE_FOLDER_ID)"
            )
        return f"{prefix} ({resp.status_code}): {detail}{hint}"

    @staticmethod
    def direct_url(file_id: str) -> str:
        return f"https://drive.google.com/uc?export=download&id={file_id}"
