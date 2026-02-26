import json
import os

from dotenv import load_dotenv

load_dotenv()  # на Replit ничего не делает — там секреты уже в env

BOT_TOKEN       = os.getenv("BOT_TOKEN")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
ADMIN_USER_ID   = int(os.getenv("ADMIN_USER_ID", 0))
CALENDAR_ID     = os.getenv("CALENDAR_ID", "primary")
TIMEZONE        = os.getenv("TIMEZONE", "Europe/Moscow")

# Директория для хранения данных.
# Локально: "data/" (по умолчанию)
# Railway:  задай переменную DATA_DIR=/data и подключи Volume с mount path = /data
_DATA_DIR = os.getenv("DATA_DIR", "data")

DB_PATH          = os.path.join(_DATA_DIR, "homework.db")
TOKEN_PATH       = os.path.join(_DATA_DIR, "token.json")
CREDENTIALS_PATH = os.path.join(_DATA_DIR, "credentials.json")
FSM_PATH         = os.path.join(_DATA_DIR, "fsm.json")

# Railway/Replit: credentials.json и token.json можно передать через переменные окружения
_creds_env = os.getenv("GOOGLE_CREDENTIALS_JSON")
if _creds_env and not os.path.exists(CREDENTIALS_PATH):
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(CREDENTIALS_PATH, "w", encoding="utf-8") as _f:
        _f.write(_creds_env if _creds_env.strip().startswith("{") else json.dumps(json.loads(_creds_env)))

_token_env = os.getenv("GOOGLE_TOKEN_JSON")
if _token_env and not os.path.exists(TOKEN_PATH):
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(TOKEN_PATH, "w", encoding="utf-8") as _f:
        _f.write(_token_env if _token_env.strip().startswith("{") else json.dumps(json.loads(_token_env)))
