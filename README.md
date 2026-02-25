# Homework Bot

Телеграм-бот: фото/текст с ДЗ → EasyOCR → Gemini Flash → Google Calendar.

## Установка

```bash
pip install -r requirements.txt
```

## Настройка

### 1. Токены

Скопируй `.env.example` в `.env` и заполни:

```
BOT_TOKEN=      # токен от @BotFather
GEMINI_API_KEY= # ключ с aistudio.google.com
ADMIN_USER_ID=  # твой Telegram ID (узнай у @userinfobot)
```

### 2. Google Calendar

**Шаг 1 — получи credentials.json:**
1. [Google Cloud Console](https://console.cloud.google.com/) → создай проект
2. APIs & Services → Library → Google Calendar API → Enable
3. APIs & Services → Credentials → + Create Credentials → OAuth 2.0 Client ID
4. Application type: **Desktop app**
5. Download JSON → переименуй в `credentials.json` → положи в папку `data/`

**Шаг 2 — авторизуйся (на компьютере с браузером):**

```bash
python setup_calendar.py
```

Откроется браузер, разреши доступ. Будет создан `data/token.json`.

Если бот на VPS — скопируй `data/token.json` на сервер:
```bash
scp data/token.json user@your-vps:/path/to/bot/data/
```

### 3. Запуск

```bash
python bot.py
```

## Использование

1. `/schedule` — задай расписание (один раз)
2. `/auth` — подключи Calendar (если не запускал `setup_calendar.py`)
3. Отправь фото или текст с ДЗ — бот добавит в Calendar

## Формат расписания

```
Пн: Математика 8:00, Физика 9:45, Литература 11:30
Вт: Алгебра 8:00, История 9:45
Ср: Математика 8:00, Химия 10:35
Чт: Физика 8:00, Геометрия 9:45
Пт: Алгебра 8:00, Биология 9:45
```
