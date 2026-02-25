import asyncio
import logging
import os
from datetime import datetime, timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import CALENDAR_ID, CREDENTIALS_PATH, TIMEZONE, TOKEN_PATH

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def get_credentials() -> Credentials | None:
    """Возвращает сохранённые credentials. Возвращает None если нужна авторизация пользователем."""
    if not os.path.exists(TOKEN_PATH):
        return None
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)
        return creds
    return None


def get_auth_url() -> tuple[str, InstalledAppFlow]:
    """Возвращает (url, flow) для OAuth через copy-paste кода."""
    flow = InstalledAppFlow.from_client_secrets_file(
        CREDENTIALS_PATH,
        SCOPES,
        redirect_uri="urn:ietf:wg:oauth:2.0:oob",
    )
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    return auth_url, flow


def exchange_code(flow: InstalledAppFlow, code: str) -> Credentials:
    """Обменивает код из браузера и сохраняет токен."""
    flow.fetch_token(code=code)
    creds = flow.credentials
    _save_token(creds)
    return creds


def _save_token(creds: Credentials):
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())


async def add_homework_event(subject: str, task: str, lesson_dt: datetime) -> str:
    """
    Создаёт событие в Google Calendar.
    Возвращает ссылку на событие.
    Блокирующие вызовы Google API выполняются в executor, чтобы не блокировать event loop.
    """
    end_dt = lesson_dt + timedelta(minutes=45)
    event_body = {
        "summary": f"📚 {subject}: {task}",
        "description": f"Предмет: {subject}\nЗадание: {task}",
        "start": {"dateTime": lesson_dt.isoformat(), "timeZone": TIMEZONE},
        "end":   {"dateTime": end_dt.isoformat(),    "timeZone": TIMEZONE},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 60},
                {"method": "popup", "minutes": 1440},
            ],
        },
        "colorId": "9",
    }

    def _sync_insert():
        creds = get_credentials()
        if not creds:
            raise RuntimeError("not_authorized")
        service = build("calendar", "v3", credentials=creds)
        return service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _sync_insert)
    return result.get("htmlLink", "")
