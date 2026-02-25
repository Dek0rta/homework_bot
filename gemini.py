import asyncio
import base64
import json
import logging
import re
from datetime import datetime

from mistralai import Mistral

from config import MISTRAL_API_KEY

logger = logging.getLogger(__name__)

_client = Mistral(api_key=MISTRAL_API_KEY)

_TEXT_MODEL   = "mistral-small-latest"
_VISION_MODEL = "pixtral-12b-2409"

_DAYS_RU      = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_DAYS_RU_FULL = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]


def _today_ctx() -> str:
    now = datetime.now()
    return f"{_DAYS_RU_FULL[now.weekday()]}, {now.strftime('%d.%m.%Y')}"


_GROUP_JSON_HINT = """\
Ответь СТРОГО одним из двух вариантов (без markdown, без пояснений):
— Если ДЗ: {"subject":"...","task":"...","due_day":<0-6 или null>,"due_date":"<YYYY-MM-DD или null>"}
— Если НЕ ДЗ: null

due_day: день недели из текста (0=Пн…6=Вс), null если не указан явно
due_date: конкретная дата из текста в формате YYYY-MM-DD, null если не указана\
"""

_GROUP_EXAMPLES = """\
ПРИМЕРЫ — ЧТО ЯВЛЯЕТСЯ ДЗ:
✅ «ДЗ по физике: §8 задачи 1-3»
✅ «На пятницу выучить стихотворение (литература)»
✅ «Математика — стр.45 упр.7, сдать в четверг»
✅ «Не забудьте параграф 12 прочитать к завтрашнему уроку химии»

ПРИМЕРЫ — ЧТО НЕ ЯВЛЯЕТСЯ ДЗ:
❌ «Кто сделал домашку по математике?» — вопрос, не задание
❌ «Завтра контрольная по физике» — объявление, не ДЗ
❌ «Привет всем!» — переписка
❌ «Спасибо за помощь» — переписка
❌ «Кто придет на дополнительное занятие?» — вопрос
❌ «Молодцы, все справились» — комментарий\
"""

_JSON_HINT = (
    'Ответь СТРОГО в виде JSON-объекта без markdown-блоков:\n'
    '{"subject": "...", "task": "...", "due_lesson_day": <0-6>, "due_lesson_time": "ЧЧ:ММ"}\n\n'
    "due_lesson_day: 0=Пн, 1=Вт, 2=Ср, 3=Чт, 4=Пт, 5=Сб, 6=Вс"
)


def _format_schedule(schedule: list[dict]) -> str:
    lines = []
    for e in schedule:
        day_name = _DAYS_RU[e["day_of_week"]]
        lines.append(f"{day_name}: {e['subject']} {e['start_time']}")
    return "\n".join(lines)


def _extract_json(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Не удалось найти JSON в ответе: {raw[:200]}")
    return json.loads(match.group())


async def _chat(messages: list[dict], model: str, max_retries: int = 4) -> str:
    """Отправляет запрос к Mistral с повтором при ошибке 429."""
    for attempt in range(max_retries):
        try:
            response = await _client.chat.complete_async(
                model=model,
                messages=messages,
                temperature=0.1,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            if "429" in str(e) and attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning("Mistral 429, повтор через %ds (попытка %d)", wait, attempt + 1)
                await asyncio.sleep(wait)
            else:
                raise


async def parse_homework_text(text: str, schedule: list[dict]) -> dict:
    """Парсит ДЗ из текста. Промпт строится конкатенацией — безопасно для любого текста."""
    prompt = (
        "Ты помощник школьника. Тебе дан текст с домашним заданием и расписание уроков.\n\n"
        "Расписание (формат «День: Предмет ЧЧ:ММ»):\n"
        + _format_schedule(schedule)
        + "\n\nТекст домашнего задания:\n"
        + text
        + "\n\nЗадача:\n"
        "1. Определи предмет из расписания (subject).\n"
        "2. Извлеки краткое описание задания (task).\n"
        "3. Найди ближайший следующий урок по этому предмету (due_lesson_day, due_lesson_time).\n\n"
        + _JSON_HINT
    )
    raw = await _chat([{"role": "user", "content": prompt}], _TEXT_MODEL)
    logger.debug("Mistral raw: %s", raw)
    return _extract_json(raw)


def _parse_group_result(raw: str) -> dict | None:
    """null → None, JSON → dict с нормализованными полями."""
    raw = raw.strip()
    if raw.lower() in ("null", "none", ""):
        return None
    try:
        data = _extract_json(raw)
        if str(data.get("due_day",  "")).lower() == "null": data["due_day"]  = None
        if str(data.get("due_date", "")).lower() == "null": data["due_date"] = None
        return data
    except Exception:
        return None


async def detect_group_homework(text: str, subjects: list[str]) -> dict | None:
    """Определяет ДЗ в тексте группового чата. Возвращает dict или None."""
    prompt = (
        "Ты определяешь, является ли сообщение из школьного чата домашним заданием.\n\n"
        "Сегодня: " + _today_ctx() + "\n"
        "Предметы класса: " + ", ".join(subjects) + "\n\n"
        + _GROUP_EXAMPLES + "\n\n"
        "Сообщение для анализа:\n«" + text + "»\n\n"
        + _GROUP_JSON_HINT
    )
    raw = await _chat([{"role": "user", "content": prompt}], _TEXT_MODEL)
    logger.debug("Group detect raw: %s", raw)
    return _parse_group_result(raw)


async def detect_group_homework_image(image_path: str, subjects: list[str]) -> dict | None:
    """Определяет ДЗ на фото из группы. Возвращает dict или None."""
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")
    mime = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
    prompt = (
        "Ты определяешь, содержит ли изображение домашнее задание из школы.\n\n"
        "Сегодня: " + _today_ctx() + "\n"
        "Предметы класса: " + ", ".join(subjects) + "\n\n"
        "Прочитай весь текст на изображении. "
        "Это домашнее задание по одному из предметов?\n\n"
        + _GROUP_JSON_HINT
    )
    messages = [{
        "role": "user",
        "content": [
            {"type": "text",      "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
        ],
    }]
    raw = await _chat(messages, _VISION_MODEL)
    logger.debug("Group image detect raw: %s", raw)
    return _parse_group_result(raw)


async def parse_homework_image(image_path: str, schedule: list[dict]) -> dict:
    """Читает изображение и парсит ДЗ."""
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    mime = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
    prompt = (
        "Ты помощник школьника. На изображении записано домашнее задание.\n\n"
        "Расписание уроков (формат «День: Предмет ЧЧ:ММ»):\n"
        + _format_schedule(schedule)
        + "\n\nЗадача:\n"
        "1. Прочитай текст на изображении.\n"
        "2. Определи предмет из расписания (subject).\n"
        "3. Извлеки краткое описание задания (task).\n"
        "4. Найди ближайший следующий урок по этому предмету (due_lesson_day, due_lesson_time).\n\n"
        + _JSON_HINT
    )

    messages = [{
        "role": "user",
        "content": [
            {"type": "text",      "text": prompt},
            # image_url должен быть dict с ключом url (Mistral API v1+)
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
        ],
    }]

    raw = await _chat(messages, _VISION_MODEL)
    logger.debug("Mistral raw: %s", raw)
    return _extract_json(raw)
