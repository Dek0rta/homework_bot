import re
from datetime import datetime, timedelta

# Маппинг названий дней → номер (0=Пн)
_DAY_MAP: dict[str, int] = {
    "пн": 0, "понедельник": 0,
    "вт": 1, "вторник": 1,
    "ср": 2, "среда": 2, "среду": 2,
    "чт": 3, "четверг": 3,
    "пт": 4, "пятница": 4, "пятницу": 4,
    "сб": 5, "суббота": 5, "субботу": 5,
    "вс": 6, "воскресенье": 6,
}


def parse_schedule_text(text: str) -> list[dict]:
    """
    Парсит расписание из текста вида:
        Пн: Математика 8:00, Физика 9:45
        Вт: Алгебра 8:00, История 9:45

    Возвращает список:
        [{"day": 0, "subject": "Математика", "start_time": "8:00"}, ...]
    """
    entries = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue

        colon_idx = line.index(":")
        day_str = line[:colon_idx].strip().lower()
        subjects_str = line[colon_idx + 1 :].strip()

        day = _DAY_MAP.get(day_str)
        if day is None:
            continue

        for part in subjects_str.split(","):
            part = part.strip()
            m = re.match(r"^(.+?)\s+(\d{1,2}:\d{2})$", part)
            if m:
                entries.append(
                    {
                        "day": day,
                        "subject": m.group(1).strip(),
                        "start_time": m.group(2),
                    }
                )

    return entries


def future_lesson_datetimes(day_of_week: int, time_str: str, n: int = 3) -> list[datetime]:
    """Возвращает n ближайших дат одного и того же урока (с шагом 1 неделя)."""
    first = next_lesson_datetime(day_of_week, time_str)
    return [first + timedelta(weeks=i) for i in range(n)]


def next_lesson_datetime(day_of_week: int, time_str: str) -> datetime:
    """
    Возвращает datetime ближайшего урока (day_of_week, time_str).
    Если сегодня тот же день, но урок уже прошёл — берём следующую неделю.
    """
    now = datetime.now()
    today = now.date()
    today_wd = today.weekday()  # 0=Пн

    days_ahead = (day_of_week - today_wd) % 7
    if days_ahead == 0:
        lesson_time = datetime.strptime(time_str, "%H:%M").time()
        if now.time() >= lesson_time:
            days_ahead = 7

    target_date = today + timedelta(days=days_ahead)
    lesson_time = datetime.strptime(time_str, "%H:%M").time()
    return datetime.combine(target_date, lesson_time)
