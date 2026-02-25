"""
Скрипт для первичной авторизации Google Calendar.
Запусти его ОДИН РАЗ на любом компьютере с браузером:

    python setup_calendar.py

Он создаст файл data/token.json.
Если настраиваешь бот на VPS — скопируй token.json на сервер в папку data/.
"""

import os
from google_auth_oauthlib.flow import InstalledAppFlow
from config import CREDENTIALS_PATH, TOKEN_PATH

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def main():
    if not os.path.exists(CREDENTIALS_PATH):
        print(f"Файл {CREDENTIALS_PATH} не найден.")
        print("\nКак получить credentials.json:")
        print("1. Открой https://console.cloud.google.com/")
        print("2. APIs & Services → Enable APIs → Google Calendar API")
        print("3. Credentials → + Create Credentials → OAuth 2.0 Client ID")
        print("4. Application type: Desktop app")
        print("5. Download JSON → переименуй в credentials.json → положи в data/")
        return

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    creds = flow.run_local_server(port=0)

    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())

    print(f"\nГотово! Токен сохранён в {TOKEN_PATH}")
    print("Если бот на VPS — скопируй этот файл туда.")


if __name__ == "__main__":
    main()
