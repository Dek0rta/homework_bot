# 📚 Homework Bot

> Telegram-бот, который превращает фото с домашним заданием в задачи в Google Calendar

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-009ADF?style=flat)](https://docs.aiogram.dev)
[![Gemini](https://img.shields.io/badge/Gemini_Flash-API-4285F4?style=flat&logo=google&logoColor=white)](https://aistudio.google.com)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat)](LICENSE)

---

## ✨ Как это работает

```
📸 Фото ДЗ  →  🔍 OCR (EasyOCR)  →  🤖 AI-парсинг (Gemini)  →  📅 Google Calendar
```

Отправляешь боту фото или текст с домашним заданием — он автоматически распознаёт предмет, дату сдачи и добавляет задачу в нужный день календаря, учитывая твоё расписание.

---

## 🚀 Возможности

- **OCR** — распознаёт рукописный и печатный текст на фото
- **AI-парсинг** — Gemini Flash извлекает предмет, задание и дедлайн
- **Google Calendar** — автоматически создаёт события по расписанию
- **Расписание** — настраивается один раз через `/schedule`
- **Аналитика** — статистика по выполненным заданиям
- **Мульти-формат** — принимает фото и текстовые сообщения

---

## 🛠 Стек технологий

| Компонент | Технология |
|-----------|-----------|
| Telegram Bot | `aiogram 3.x` |
| OCR | `EasyOCR` |
| AI-парсинг | `Google Gemini Flash` |
| Календарь | `Google Calendar API` |
| База данных | `SQLite` + `aiosqlite` |
| Деплой | `Railway` / `VPS` |
| Конфиг | `python-dotenv` |

---

## 📁 Структура проекта

```
homework_bot/
├── bot.py              # точка входа
├── config.py           # настройки из .env
├── gemini.py           # AI-парсинг через Gemini API
├── ocr.py              # распознавание текста (EasyOCR)
├── calendar_api.py     # интеграция с Google Calendar
├── schedule.py         # логика расписания
├── storage.py          # работа с базой данных
├── analytics.py        # статистика использования
├── db.py               # инициализация БД
├── setup_calendar.py   # первичная авторизация Calendar
├── requirements.txt
├── .env.example
├── Procfile            # для Railway
└── railway.json
```

---

## ⚡ Быстрый старт

### 1. Клонируй репозиторий

```bash
git clone https://github.com/Dek0rta/homework_bot.git
cd homework_bot
```

### 2. Установи зависимости

```bash
pip install -r requirements.txt
```

### 3. Настрой переменные окружения

```bash
cp .env.example .env
```

Открой `.env` и заполни:

```env
BOT_TOKEN=        # получить у @BotFather
GEMINI_API_KEY=   # получить на aistudio.google.com (бесплатно)
ADMIN_USER_ID=    # твой Telegram ID — узнать у @userinfobot
```

### 4. Подключи Google Calendar

**Шаг 1** — Получи `credentials.json`:
1. Зайди на [Google Cloud Console](https://console.cloud.google.com/)
2. Создай проект → APIs & Services → Library → **Google Calendar API** → Enable
3. Credentials → Create Credentials → **OAuth 2.0 Client ID** → Desktop app
4. Скачай JSON → переименуй в `credentials.json` → положи в папку `data/`

**Шаг 2** — Авторизуйся:
```bash
python setup_calendar.py
```
Откроется браузер — разреши доступ. Появится `data/token.json`.

> **На VPS?** Запусти `setup_calendar.py` локально, затем скопируй `token.json` на сервер:
> ```bash
> scp data/token.json user@your-vps:/path/to/bot/data/
> ```

### 5. Запусти бота

```bash
python bot.py
```

---

## 💬 Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Запуск и приветствие |
| `/schedule` | Настроить расписание уроков |
| `/auth` | Подключить Google Calendar |
| `/help` | Справка |

**Пример расписания:**
```
Пн: Математика 8:00, Физика 9:45
Вт: Алгебра 8:00, История 9:45
Ср: Математика 8:00, Химия 10:35
Чт: Физика 8:00, Геометрия 9:45
Пт: Алгебра 8:00, Биология 9:45
```

---

## 🚢 Деплой на Railway

1. Форкни репозиторий
2. Подключи к [Railway](https://railway.app)
3. Добавь переменные окружения в настройках проекта
4. Загрузи `data/token.json` и `data/credentials.json` через Volume
5. Railway автоматически запустит бота через `Procfile`

---

## 📦 Зависимости

```
aiogram>=3.0
easyocr
google-generativeai
google-auth-oauthlib
google-api-python-client
aiosqlite
python-dotenv
```

---

## 🤝 Контакты

Нужен похожий бот под ваши задачи?

- **Telegram:** [@Dek0rta](https://t.me/Dek0rta)
- **Авито:** [объявление]([https://avito.ru](https://www.avito.ru/bryansk/predlozheniya_uslug/telegram-bot_na_zakaz_razrabotka_pod_vashi_zadachi_7884576247?utm_campaign=native&utm_medium=item_page_ios&utm_source=soc_sharing_seller)

> Разрабатываю Telegram-ботов на заказ: от простых до сложных с AI, OCR и интеграциями.

---

## 📄 Лицензия

MIT — используй свободно, звёздочку поставь 🌟
