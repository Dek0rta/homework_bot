import asyncio
import itertools
import logging
import os
import tempfile
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReactionTypeEmoji,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import calendar_api
import analytics
import db
import gemini
import schedule as sched_module
from config import BOT_TOKEN
from storage import JsonStorage


async def safe_delete(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


async def _delete_after(message: Message, delay: int = 10):
    await asyncio.sleep(delay)
    await safe_delete(message)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

router = Router()

_auth_flows: dict[int, object] = {}

# Ожидающие подтверждения ДЗ в группах (key → {subject, task, chat_id})
_hw_counter = itertools.count()
_pending_group_hw: dict[int, dict] = {}

# ──────────────────────────────────────────────
# Константы
# ──────────────────────────────────────────────

DAYS_RU    = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Расписание звонков 2025-2026
LESSON_TIMES = [
    "8:15",  # 1 урок  8:15 – 8:55
    "9:15",  # 2 урок  9:15 – 9:55
    "10:10", # 3 урок  10:10 – 10:50
    "11:05", # 4 урок  11:05 – 11:45
    "12:00", # 5 урок  12:00 – 12:40
    "12:50", # 6 урок  12:50 – 13:30
    "13:40", # 7 урок  13:40 – 14:20
    "14:30", # 8 урок  14:30 – 15:10
]

BTN_SCHEDULE     = "📅 Моё расписание"
BTN_SET_SCHEDULE = "✏️ Изменить расписание"
BTN_CALENDAR     = "🔗 Google Calendar"
BTN_STATS        = "📊 Нагрузка"
BUTTON_TEXTS     = {BTN_SCHEDULE, BTN_SET_SCHEDULE, BTN_CALENDAR, BTN_STATS}

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_SET_SCHEDULE), KeyboardButton(text=BTN_SCHEDULE)],
        [KeyboardButton(text=BTN_CALENDAR),     KeyboardButton(text=BTN_STATS)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Отправь фото или текст с ДЗ...",
)


# ──────────────────────────────────────────────
# FSM
# ──────────────────────────────────────────────

class ScheduleSetup(StatesGroup):
    choosing_day          = State()  # выбор дня
    entering_lesson_name  = State()  # ввод названия предмета для слота


class CalendarAuth(StatesGroup):
    waiting_for_code = State()


class HomeworkConfirm(StatesGroup):
    choosing_day = State()


# ──────────────────────────────────────────────
# Вспомогательные функции для temp-расписания
#
# Формат temp: {day_str: {slot_str: subject}}
#   day_str  — "0".."6"  (0=Пн)
#   slot_str — "0".."7"  (номер урока, 0-based)
#   subject  — название предмета или "" (пусто)
# ──────────────────────────────────────────────

def temp_has_lessons(temp: dict, day: int) -> bool:
    return any(v for v in temp.get(str(day), {}).values())


def temp_get_subject(temp: dict, day: int, slot: int) -> str:
    return temp.get(str(day), {}).get(str(slot), "")


def temp_set_subject(temp: dict, day: int, slot: int, subject: str):
    temp.setdefault(str(day), {})[str(slot)] = subject


# ──────────────────────────────────────────────
# Клавиатуры
# ──────────────────────────────────────────────

def kb_days(temp: dict) -> InlineKeyboardMarkup:
    """Экран выбора дня: 7 кнопок + Сохранить."""
    rows = []
    for i, name in enumerate(DAYS_SHORT):
        has   = temp_has_lessons(temp, i)
        label = f"{'✅' if has else '◻️'} {name}"
        rows.append(InlineKeyboardButton(text=label, callback_data=f"sched:day:{i}"))

    keyboard = [rows[i:i + 3] for i in range(0, len(rows), 3)]
    keyboard.append([InlineKeyboardButton(text="💾 Сохранить", callback_data="sched:save")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def kb_lessons(day: int, temp: dict) -> InlineKeyboardMarkup:
    """Экран дня: кнопки 1–8 урок по 2 в ряд."""
    keyboard = []
    for slot in range(8):
        subject = temp_get_subject(temp, day, slot)
        time    = LESSON_TIMES[slot]
        if subject:
            label = f"{slot + 1} ✅ {subject}"
        else:
            label = f"{slot + 1} урок  {time}"
        keyboard.append(
            InlineKeyboardButton(text=label, callback_data=f"sched:slot:{day}:{slot}")
        )

    # По 2 кнопки в ряд
    rows = [keyboard[i:i + 2] for i in range(0, len(keyboard), 2)]
    rows.append([InlineKeyboardButton(text="◀️ К дням", callback_data="sched:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_hw_due_day(hw_key: int, entries: list[dict] | None = None) -> InlineKeyboardMarkup:
    """
    Кнопки выбора дня сдачи ДЗ.
    entries — слоты расписания по предмету (day_of_week + start_time).
      Если переданы — показываем ближайшие 2 даты для каждого слота.
      Если None/пусто — fallback: следующие 7 дней без воскресенья.
    """
    rows: list[list[InlineKeyboardButton]] = []

    if entries:
        for e in entries:
            for dt in sched_module.future_lesson_datetimes(e["day_of_week"], e["start_time"], n=2):
                label = (
                    f"{DAYS_RU[e['day_of_week']]} {dt.day} {_MONTHS_SHORT[dt.month - 1]}"
                    f" · {e['start_time']}"
                )
                rows.append([InlineKeyboardButton(
                    text=label,
                    callback_data=f"hw|cd|{hw_key}|{dt.strftime('%Y%m%d')}",
                )])
    else:
        today = datetime.today().date()
        row: list[InlineKeyboardButton] = []
        for delta in range(7):
            d = today + timedelta(days=delta)
            if d.weekday() == 6:  # воскресенье пропускаем
                continue
            if delta == 0:
                label = f"Сегодня {DAYS_SHORT[d.weekday()]} {d.day}"
            elif delta == 1:
                label = f"Завтра {DAYS_SHORT[d.weekday()]} {d.day}"
            else:
                label = f"{DAYS_SHORT[d.weekday()]} {d.day}"
                if d.month != today.month:
                    label += f" {_MONTHS_SHORT[d.month - 1]}"
            row.append(InlineKeyboardButton(
                text=label,
                callback_data=f"hw|cd|{hw_key}|{d.strftime('%Y%m%d')}",
            ))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

    rows.append([InlineKeyboardButton(text="📆 Без даты", callback_data=f"hw|cd|{hw_key}|none")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ──────────────────────────────────────────────
# /start
# ──────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    await safe_delete(message)
    await message.answer(
        f"Привет, <b>{message.from_user.first_name}</b>! Я добавляю домашние задания прямо в Google Calendar.\n\n"
        "<b>Быстрый старт:</b>\n"
        "1. <b>✏️ Изменить расписание</b> — укажи уроки по дням\n"
        "2. <b>🔗 Google Calendar</b> — подключи аккаунт\n"
        "3. Отправь фото тетради или текст с ДЗ — готово!\n\n"
        "<b>Команды:</b>\n"
        "/schedule — редактор расписания\n"
        "/my_schedule — посмотреть расписание\n"
        "/auth — подключить Google Calendar\n"
        "/stats — график нагрузки класса",
        parse_mode="HTML",
        reply_markup=MAIN_KB,
    )


# ──────────────────────────────────────────────
# /cancel
# ──────────────────────────────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await safe_delete(message)
    await state.clear()
    await message.answer("Отменено.", reply_markup=MAIN_KB)


# ──────────────────────────────────────────────
# Редактор расписания — вход
# ──────────────────────────────────────────────

async def open_schedule_editor(message: Message, state: FSMContext):
    existing = db.get_schedule(message.from_user.id)

    # Конвертируем DB-записи в temp-формат, сопоставляя время со слотами
    temp: dict[str, dict] = {}
    for e in existing:
        day_key = str(e["day_of_week"])
        # Ищем ближайший слот по времени
        try:
            slot = LESSON_TIMES.index(e["start_time"])
        except ValueError:
            # Если время нестандартное — берём первый свободный слот дня
            day_slots = temp.get(day_key, {})
            slot = next((s for s in range(8) if str(s) not in day_slots), None)
            if slot is None:
                continue
        temp_set_subject(temp, int(day_key), slot, e["subject"])

    await state.set_state(ScheduleSetup.choosing_day)
    await state.update_data(temp=temp)

    # Шлём пустышку с ReplyKeyboardRemove и сразу удаляем — без мусора в чате
    tmp = await message.answer(".", reply_markup=ReplyKeyboardRemove())
    await safe_delete(tmp)
    await message.answer("📆 Расписание:", reply_markup=kb_days(temp))


@router.message(Command("schedule"))
@router.message(F.text == BTN_SET_SCHEDULE)
async def btn_set_schedule(message: Message, state: FSMContext):
    logger.info("btn_set_schedule вызван от user_id=%s text=%r", message.from_user.id, message.text)
    await safe_delete(message)
    try:
        await open_schedule_editor(message, state)
    except Exception as e:
        logger.exception("Ошибка в open_schedule_editor")
        await message.answer(f"⚠️ Ошибка: {e}", reply_markup=MAIN_KB)


# ──────────────────────────────────────────────
# Callbacks — выбор дня и слота
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("sched:day:"))
async def cb_select_day(call: CallbackQuery, state: FSMContext):
    await call.answer()
    day  = int(call.data.split(":")[2])
    data = await state.get_data()
    temp = data["temp"]

    await call.message.edit_text(
        f"📚 <b>{DAYS_RU[day]}</b>\n\n"
        "Нажми на урок чтобы задать или изменить предмет.\n"
        "Нажми ещё раз — чтобы очистить.",
        parse_mode="HTML",
        reply_markup=kb_lessons(day, temp),
    )


@router.callback_query(F.data == "sched:back")
async def cb_back_to_days(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    await state.set_state(ScheduleSetup.choosing_day)
    await call.message.edit_text("📆 Расписание:", reply_markup=kb_days(data["temp"]))


@router.callback_query(F.data.startswith("sched:slot:"))
async def cb_select_slot(call: CallbackQuery, state: FSMContext):
    await call.answer()
    _, _, day_str, slot_str = call.data.split(":")
    day, slot = int(day_str), int(slot_str)
    data      = await state.get_data()
    temp      = data["temp"]
    subject   = temp_get_subject(temp, day, slot)
    time      = LESSON_TIMES[slot]

    await state.set_state(ScheduleSetup.entering_lesson_name)
    await state.update_data(editing_day=day, editing_slot=slot, sched_msg_id=call.message.message_id)

    if subject:
        text = (
            f"<b>{slot + 1} урок</b> · {time}\n"
            f"Сейчас: <b>{subject}</b>\n\n"
            f"Напиши новое название или отправь <code>-</code> чтобы очистить:"
        )
    else:
        text = (
            f"<b>{slot + 1} урок</b> · {time}\n\n"
            "Напиши название предмета (например: Математика, Классный час, Физ-ра):"
        )

    await call.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"sched:day:{day}")
        ]]),
    )


@router.message(ScheduleSetup.entering_lesson_name, ~F.text.in_(BUTTON_TEXTS))
async def handle_lesson_name(message: Message, state: FSMContext):
    await safe_delete(message)
    data  = await state.get_data()
    day   = data.get("editing_day")
    slot  = data.get("editing_slot")
    temp  = data.get("temp", {})

    # Если состояние зависло без данных (например после перезапуска) — сбрасываем
    if day is None or slot is None:
        await state.clear()
        await open_schedule_editor(message, state)
        return

    text  = message.text.strip()

    if text == "-":
        temp_set_subject(temp, day, slot, "")
    else:
        temp_set_subject(temp, day, slot, text)

    await state.update_data(temp=temp)
    await state.set_state(ScheduleSetup.choosing_day)

    sched_msg_id = data.get("sched_msg_id")
    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=sched_msg_id,
            text=f"📚 <b>{DAYS_RU[day]}</b>",
            parse_mode="HTML",
            reply_markup=kb_lessons(day, temp),
        )
    except Exception:
        await message.answer(
            f"📚 <b>{DAYS_RU[day]}</b>",
            parse_mode="HTML",
            reply_markup=kb_lessons(day, temp),
        )


# ──────────────────────────────────────────────
# Сохранение
# ──────────────────────────────────────────────

@router.callback_query(F.data == "sched:save")
async def cb_save_schedule(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    temp = data.get("temp", {})

    entries = []
    for day_str, slots in temp.items():
        for slot_str, subject in slots.items():
            if subject:
                entries.append({
                    "day": int(day_str),
                    "subject": subject,
                    "start_time": LESSON_TIMES[int(slot_str)],
                })

    if not entries:
        await call.answer("Расписание пустое — добавь хотя бы один урок!", show_alert=True)
        return

    db.save_schedule(call.from_user.id, entries)
    await state.clear()

    # Формируем красивый итог по дням
    by_day: dict[int, list] = {}
    for e in entries:
        by_day.setdefault(e["day"], []).append(e)

    lines = [f"✅ <b>Расписание сохранено</b> — {len(entries)} урок(ов)\n"]
    for day_idx in sorted(by_day):
        lines.append(f"<b>{DAYS_RU[day_idx]}</b>")
        for e in sorted(by_day[day_idx], key=lambda x: _time_key(x["start_time"])):
            slot = LESSON_TIMES.index(e["start_time"]) if e["start_time"] in LESSON_TIMES else -1
            prefix = f"{slot + 1} ур." if slot >= 0 else ""
            lines.append(f"  {prefix} {e['start_time']} — {e['subject']}")
        lines.append("")

    summary = "\n".join(lines).rstrip()
    await safe_delete(call.message)
    await call.message.answer(summary, parse_mode="HTML", reply_markup=MAIN_KB)


# ──────────────────────────────────────────────
# Просмотр расписания
# ──────────────────────────────────────────────

@router.message(Command("my_schedule"))
@router.message(F.text == BTN_SCHEDULE)
async def btn_my_schedule(message: Message):
    await safe_delete(message)
    schedule = db.get_schedule(message.from_user.id)
    if not schedule:
        await message.answer(
            "Расписание ещё не настроено.\nНажми <b>✏️ Изменить расписание</b>.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    # Группируем по дню
    by_day: dict[int, list] = {}
    for e in schedule:
        by_day.setdefault(e["day_of_week"], []).append(e)

    lines = ["<b>📅 Расписание</b>\n"]
    for day_idx in sorted(by_day):
        lines.append(f"<b>{DAYS_RU[day_idx]}</b>")
        day_entries = sorted(by_day[day_idx], key=lambda x: _time_key(x["start_time"]))
        for e in day_entries:
            try:
                slot_num = LESSON_TIMES.index(e["start_time"]) + 1
                lines.append(f"  {slot_num} ур. {e['start_time']} — {e['subject']}")
            except ValueError:
                lines.append(f"  {e['start_time']} — {e['subject']}")
        lines.append("")

    await message.answer("\n".join(lines).rstrip(), parse_mode="HTML", reply_markup=MAIN_KB)


def _time_key(t: str) -> int:
    """Сортировочный ключ для времени «Ч:ММ»."""
    h, m = t.split(":")
    return int(h) * 60 + int(m)


# ──────────────────────────────────────────────
# Google Calendar
# ──────────────────────────────────────────────

@router.message(Command("auth"))
@router.message(F.text == BTN_CALENDAR)
async def btn_calendar(message: Message, state: FSMContext):
    await safe_delete(message)
    if calendar_api.get_credentials() is not None:
        await message.answer("Google Calendar уже подключён!", reply_markup=MAIN_KB)
        return

    if not os.path.exists(calendar_api.CREDENTIALS_PATH):
        await message.answer(
            "Файл <code>data/credentials.json</code> не найден.\n\n"
            "Как получить:\n"
            "1. Google Cloud Console → APIs & Services\n"
            "2. Enable → Google Calendar API\n"
            "3. Credentials → Create → OAuth 2.0 → Desktop app\n"
            "4. Download JSON → переименуй в <code>credentials.json</code>\n"
            "5. Положи в папку <code>data/</code>",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    auth_url, flow = calendar_api.get_auth_url()
    _auth_flows[message.from_user.id] = flow

    await state.set_state(CalendarAuth.waiting_for_code)
    await message.answer(
        "1. Перейди по ссылке и разреши доступ:\n"
        f"<code>{auth_url}</code>\n\n"
        "2. Скопируй полученный код и отправь его сюда.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(CalendarAuth.waiting_for_code)
async def handle_auth_code(message: Message, state: FSMContext):
    flow = _auth_flows.pop(message.from_user.id, None)
    if not flow:
        await message.answer(
            "Сессия авторизации истекла. Нажми 🔗 Google Calendar снова.",
            reply_markup=MAIN_KB,
        )
        await state.clear()
        return

    try:
        calendar_api.exchange_code(flow, message.text.strip())
        await state.clear()
        await message.answer("Google Calendar подключён! Теперь можешь отправлять ДЗ.", reply_markup=MAIN_KB)
    except Exception as e:
        logger.exception("Auth error")
        await message.answer(f"Ошибка авторизации: {e}\n\nПопробуй снова.", reply_markup=MAIN_KB)
        await state.clear()


# ──────────────────────────────────────────────
# Обработка домашнего задания
# ──────────────────────────────────────────────

def _find_subject_days(subject: str, schedule: list[dict]) -> list[dict]:
    """Все уроки расписания с совпадающим предметом (без учёта регистра)."""
    s = subject.strip().lower()
    return [e for e in schedule if e["subject"].strip().lower() == s]


_MONTHS_SHORT = ["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"]


def kb_pick_hw_day(entries: list[dict]) -> InlineKeyboardMarkup:
    """
    Для каждого слота из entries показываем 2 ближайшие даты.
    Формат callback: hw|pick|{day}|{HH:MM}|{YYYYMMDD}
    """
    rows = []
    for e in entries:
        for dt in sched_module.future_lesson_datetimes(e["day_of_week"], e["start_time"], n=2):
            date_str = f"{dt.day} {_MONTHS_SHORT[dt.month - 1]}"
            rows.append([InlineKeyboardButton(
                text=f"{DAYS_RU[e['day_of_week']]} · {e['start_time']}  ({date_str})",
                callback_data=f"hw|pick|{e['day_of_week']}|{e['start_time']}|{dt.strftime('%Y%m%d')}",
            )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _do_add_to_calendar(status: Message, parsed: dict):
    """Добавляет ДЗ в Calendar и редактирует статусное сообщение."""
    subject  = parsed.get("subject", "Неизвестно")
    task     = parsed.get("task", "")
    day      = parsed.get("due_lesson_day")
    time_str = parsed.get("due_lesson_time")

    if day is None or not time_str:
        await status.edit_text(
            f"Предмет: <b>{subject}</b>\n"
            f"Задание: {task}\n\n"
            "Не смог определить ближайший урок. Проверь расписание.",
            parse_mode="HTML",
        )
        return

    # Если пользователь выбрал конкретную дату — используем её, иначе берём ближайшую
    if "lesson_dt_iso" in parsed:
        lesson_dt = datetime.fromisoformat(parsed["lesson_dt_iso"])
    else:
        lesson_dt = sched_module.next_lesson_datetime(day, time_str)

    try:
        event_link = await calendar_api.add_homework_event(subject, task, lesson_dt)
    except RuntimeError as e:
        if "not_authorized" in str(e):
            await status.edit_text(
                f"Предмет: <b>{subject}</b>\n"
                f"Задание: {task}\n\n"
                "Google Calendar не подключён. Нажми 🔗 Google Calendar.",
                parse_mode="HTML",
            )
        else:
            await status.edit_text(f"Ошибка Calendar: {e}")
        return
    except Exception as e:
        logger.exception("Calendar error")
        await status.edit_text(f"Ошибка при добавлении в Calendar: {e}")
        return

    kb_rows = []
    if event_link:
        kb_rows.append([InlineKeyboardButton(text="📅 Открыть в календаре", url=event_link)])
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main:menu")])

    await status.edit_text(
        f"✅ <b>Добавлено в Google Calendar!</b>\n\n"
        f"📚 <b>Предмет:</b> {subject}\n"
        f"📝 <b>Задание:</b> {task}\n"
        f"📅 <b>Урок:</b> {DAYS_RU[day]}, {lesson_dt.strftime('%d.%m')} в {time_str}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


@router.callback_query(F.data == "main:menu")
async def cb_main_menu(call: CallbackQuery):
    await call.answer()
    await safe_delete(call.message)
    await call.bot.send_message(call.message.chat.id, "Главное меню", reply_markup=MAIN_KB)


async def _apply_parsed(status: Message, parsed: dict, state: FSMContext, schedule: list[dict]):
    """Проверяет совпадения предмета в расписании и добавляет ДЗ."""
    subject  = parsed.get("subject", "Неизвестно")
    task     = parsed.get("task", "")
    matching = _find_subject_days(subject, schedule)

    if len(matching) == 0:
        await status.edit_text(
            f"Предмет <b>{subject}</b> не найден в расписании.\n"
            "Проверь расписание командой /my_schedule.",
            parse_mode="HTML",
        )
        return

    if len(matching) > 1:
        await state.set_state(HomeworkConfirm.choosing_day)
        await state.update_data(pending_parsed=parsed)
        await status.edit_text(
            f"📚 <b>{subject}</b>\n"
            f"📝 {task}\n\n"
            "Этот предмет есть в нескольких днях — выбери нужный урок:",
            parse_mode="HTML",
            reply_markup=kb_pick_hw_day(matching),
        )
        return

    # Ровно 1 совпадение — берём день/время из расписания, а не от Mistral
    parsed["due_lesson_day"]  = matching[0]["day_of_week"]
    parsed["due_lesson_time"] = matching[0]["start_time"]
    await _do_add_to_calendar(status, parsed)


@router.callback_query(HomeworkConfirm.choosing_day, F.data.startswith("hw|pick|"))
async def cb_pick_hw_day(call: CallbackQuery, state: FSMContext):
    await call.answer()
    # формат: hw|pick|{day}|{HH:MM}|{YYYYMMDD}
    _, _, day_str, time_str, date_raw = call.data.split("|")
    day       = int(day_str)
    lesson_dt = datetime.combine(
        datetime.strptime(date_raw, "%Y%m%d").date(),
        datetime.strptime(time_str, "%H:%M").time(),
    )

    data   = await state.get_data()
    parsed = data.get("pending_parsed", {})
    parsed["due_lesson_day"]  = day
    parsed["due_lesson_time"] = time_str
    parsed["lesson_dt_iso"]   = lesson_dt.isoformat()

    await state.clear()
    await safe_delete(call.message)
    status = await call.bot.send_message(call.message.chat.id, "Добавляю в Calendar...")
    await _do_add_to_calendar(status, parsed)


async def _analyze_hw_async(chat_id: int, hw_id: int, subject: str, task: str, due_date: str) -> None:
    """
    Фоновый анализ сложности ДЗ через LLM.
    Не блокирует ответ пользователю — ошибки логируются и игнорируются.
    """
    try:
        est_time = await analytics.estimate_hw_time(subject, task)
        if est_time is not None:
            db.update_hw_estimated_time(hw_id, est_time)
        analytics.update_daily_metrics(chat_id, due_date)
    except Exception:
        logger.exception("Background analytics error for hw_id=%d", hw_id)


async def process_homework(message: Message, text: str, state: FSMContext):
    await safe_delete(message)

    if not db.has_schedule(message.from_user.id):
        await message.answer(
            "Сначала настрой расписание — нажми ✏️ Изменить расписание.",
            reply_markup=MAIN_KB,
        )
        return

    schedule = db.get_schedule(message.from_user.id)
    status   = await message.answer("Анализирую задание...")

    try:
        parsed = await gemini.parse_homework_text(text, schedule)
    except Exception as e:
        logger.exception("Gemini error")
        await status.edit_text(f"Ошибка при анализе текста: {e}")
        return

    await _apply_parsed(status, parsed, state, schedule)


@router.message(F.chat.type == "private", F.photo)
async def handle_photo(message: Message, state: FSMContext):
    await safe_delete(message)

    if not db.has_schedule(message.from_user.id):
        await message.answer(
            "Сначала настрой расписание — нажми ✏️ Изменить расписание.",
            reply_markup=MAIN_KB,
        )
        return

    status = await message.answer("Читаю фото...")
    photo  = message.photo[-1]
    file   = await message.bot.get_file(photo.file_id)

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await message.bot.download_file(file.file_path, tmp_path)
        schedule = db.get_schedule(message.from_user.id)

        try:
            parsed = await gemini.parse_homework_image(tmp_path, schedule)
        except Exception as e:
            logger.exception("Gemini image error")
            await status.edit_text(f"Ошибка при анализе фото: {e}")
            return

        await _apply_parsed(status, parsed, state, schedule)
    finally:
        os.unlink(tmp_path)


@router.message(F.chat.type == "private", F.text & ~F.text.startswith("/"))
async def handle_text(message: Message, state: FSMContext):
    if message.text in BUTTON_TEXTS or await state.get_state() is not None:
        return
    await process_homework(message, message.text, state)


# ──────────────────────────────────────────────
# Группы и каналы — мониторинг ДЗ
# ──────────────────────────────────────────────

_GROUP_TYPES = {"group", "supergroup"}


def _fmt_due_date(iso: str) -> str:
    """'YYYY-MM-DD' → 'Понедельник, 3 мар'"""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        return f"{DAYS_RU[d.weekday()]}, {d.day} {_MONTHS_SHORT[d.month - 1]}"
    except Exception:
        return iso


async def _is_chat_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


async def _handle_group_message(message: Message, text: str | None = None, image_path: str | None = None):
    """Общая логика: проверяем предметы → детектируем ДЗ → спрашиваем день."""
    subjects = db.get_chat_subjects(message.chat.id)
    if not subjects:
        return

    try:
        if image_path:
            result = await gemini.detect_group_homework_image(image_path, subjects)
        else:
            if not text or len(text) < 10:
                return
            result = await gemini.detect_group_homework(text, subjects)
    except Exception:
        logger.exception("Group homework detection error")
        return

    if result:
        subject = result.get("subject", "")
        task    = result.get("task", "")

        # Реакция 👀 на исходное сообщение — бот видит ДЗ
        try:
            await message.react([ReactionTypeEmoji(emoji="👀")])
        except Exception:
            pass

        # Ищем уроки с этим предметом в расписании владельца чата
        sched_entries: list[dict] = []
        owner_id = db.get_chat_schedule_owner(message.chat.id)
        if owner_id:
            full_schedule = db.get_schedule(owner_id)
            sched_entries = [
                e for e in full_schedule
                if e["subject"].strip().lower() == subject.strip().lower()
            ]

        key = next(_hw_counter)
        _pending_group_hw[key] = {
            "subject": subject,
            "task":    task,
            "chat_id": message.chat.id,
        }

        if sched_entries:
            hint = ""
        elif owner_id:
            hint = "\n\n<i>⚠️ Предмет не найден в расписании — выбери день вручную.</i>"
        else:
            hint = "\n\n<i>⚠️ Расписание не привязано. Запусти /link_schedule чтобы даты подбирались автоматически.</i>"

        await message.reply(
            f"📚 <b>ДЗ найдено</b>\n"
            f"<b>{subject}</b> — {task}\n\n"
            f"📅 На какой день задали?{hint}",
            parse_mode="HTML",
            reply_markup=kb_hw_due_day(key, sched_entries or None),
        )


# ── Настройка предметов (/setup_subjects) ────────────────────────────────────

@router.message(Command("setup_subjects"), F.chat.type.in_(_GROUP_TYPES))
@router.channel_post(Command("setup_subjects"))
async def cmd_setup_subjects(message: Message):
    # В каналах from_user=None → считаем, что только канал-admin постит
    if message.from_user and message.chat.type in _GROUP_TYPES:
        if not await _is_chat_admin(message.bot, message.chat.id, message.from_user.id):
            await message.reply("Только администраторы чата могут настраивать предметы.")
            return

    args = message.text.partition(" ")[2].strip() if message.text else ""
    if not args:
        current = db.get_chat_subjects(message.chat.id)
        if current:
            await message.reply(
                "Текущие предметы:\n" + "\n".join(f"• {s}" for s in current)
                + "\n\nЧтобы изменить: /setup_subjects Математика, Физика, ...",
            )
        else:
            await message.reply(
                "Предметы не настроены.\n"
                "Использование: <code>/setup_subjects Математика, Физика, Химия</code>",
                parse_mode="HTML",
            )
        return

    subjects = [s.strip() for s in args.replace("\n", ",").split(",") if s.strip()]
    db.set_chat_subjects(message.chat.id, subjects)
    # Сохраняем отправителя как владельца расписания
    if message.from_user:
        db.set_chat_schedule_owner(message.chat.id, message.from_user.id)
    await message.reply(
        "✅ <b>Предметы сохранены</b>\n" + "\n".join(f"• {s}" for s in subjects)
        + "\n\n<i>Твоё расписание будет использоваться для определения дат ДЗ.</i>",
        parse_mode="HTML",
    )


# ── Привязка расписания к группе (/link_schedule) ───────────────────────────

@router.message(Command("link_schedule"), F.chat.type.in_(_GROUP_TYPES))
async def cmd_link_schedule(message: Message):
    if not db.has_schedule(message.from_user.id):
        await message.reply(
            "У тебя нет личного расписания.\n"
            "Настрой его в личном чате с ботом командой /schedule.",
        )
        return

    db.set_chat_schedule_owner(message.chat.id, message.from_user.id)
    await message.reply(
        f"✅ Расписание <b>{message.from_user.first_name}</b> привязано к группе.\n"
        "Теперь при обнаружении ДЗ бот будет предлагать даты из расписания.",
        parse_mode="HTML",
    )


# ── Список ДЗ (/hw) ──────────────────────────────────────────────────────────

def _build_hw_list(homework: list[dict], group_chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Формирует текст и клавиатуру с кнопками удаления для /hw."""
    by_subject: dict[str, list] = {}
    for hw in homework:
        by_subject.setdefault(hw["subject"], []).append(hw)

    lines = [f"📚 <b>Домашние задания</b> ({len(homework)})\n"]
    for subject, items in by_subject.items():
        lines.append(f"📌 <b>{subject}</b>")
        for item in items[:10]:
            due = f" → {_fmt_due_date(item['due_date'])}" if item.get("due_date") else ""
            lines.append(f"  • {item['task']}{due}  <i>({item['added_at']})</i>")
        lines.append("")

    text = "\n".join(lines).rstrip()

    del_rows: list[list[InlineKeyboardButton]] = []
    for hw in homework:
        label = f"🗑 {hw['subject'][:15]} — {hw['task'][:25]}"
        del_rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"hw|del|{hw['id']}|{group_chat_id}",
        )])
    del_rows.append([InlineKeyboardButton(
        text="🗑 Очистить всё",
        callback_data=f"hw|clear_all|{group_chat_id}",
    )])
    del_rows.append([InlineKeyboardButton(
        text="📊 Нагрузка класса",
        callback_data=f"hw|stats|{group_chat_id}",
    )])

    return text, InlineKeyboardMarkup(inline_keyboard=del_rows)


@router.message(Command("hw"), F.chat.type.in_(_GROUP_TYPES))
@router.channel_post(Command("hw"))
async def cmd_hw(message: Message):
    homework = db.get_chat_homework(message.chat.id)

    if not homework:
        subjects = db.get_chat_subjects(message.chat.id)
        msg = await message.reply(
            "Предметы не настроены. Выполни /setup_subjects" if not subjects
            else "ДЗ пока нет — буду отслеживать сообщения!"
        )
        if message.from_user:
            await safe_delete(message)
            asyncio.create_task(_delete_after(msg))
        return

    text, kb = _build_hw_list(homework, message.chat.id)

    # Канал — отвечаем прямо в канале (нет from_user)
    if not message.from_user:
        await message.reply(text, parse_mode="HTML", reply_markup=kb)
        return

    # Группа — удаляем команду, список шлём в ЛС
    await safe_delete(message)
    try:
        await message.bot.send_message(message.from_user.id, text, parse_mode="HTML", reply_markup=kb)
        notify = await message.answer("📬 Список ДЗ отправлен тебе в личные сообщения.")
    except Exception:
        # Бот заблокирован или нет диалога с пользователем
        notify = await message.answer(text, parse_mode="HTML", reply_markup=kb)
    asyncio.create_task(_delete_after(notify))


# ── Очистка (/clear_hw) ───────────────────────────────────────────────────────

@router.message(Command("clear_hw"), F.chat.type.in_(_GROUP_TYPES))
@router.channel_post(Command("clear_hw"))
async def cmd_clear_hw(message: Message):
    if message.from_user and message.chat.type in _GROUP_TYPES:
        if not await _is_chat_admin(message.bot, message.chat.id, message.from_user.id):
            await message.reply("Только администраторы могут очищать список ДЗ.")
            return

    db.clear_chat_homework(message.chat.id)
    await message.reply("✅ Список ДЗ очищен.")


# ── Удаление одного ДЗ (inline-кнопки из /hw) ───────────────────────────────

@router.callback_query(F.data.startswith("hw|del|"))
async def cb_delete_hw(call: CallbackQuery):
    parts         = call.data.split("|")
    hw_id         = int(parts[2])
    group_chat_id = int(parts[3])

    if call.message.chat.type in _GROUP_TYPES:
        if not await _is_chat_admin(call.bot, call.message.chat.id, call.from_user.id):
            await call.answer("Только администраторы могут удалять ДЗ.", show_alert=True)
            return

    db.delete_chat_homework(hw_id)
    await call.answer("✅ ДЗ удалено")

    homework = db.get_chat_homework(group_chat_id)
    if not homework:
        await call.message.edit_text("📚 Список ДЗ пуст.")
        return

    text, kb = _build_hw_list(homework, group_chat_id)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("hw|clear_all"))
async def cb_hw_clear_all(call: CallbackQuery):
    group_chat_id = int(call.data.split("|")[2])

    if call.message.chat.type in _GROUP_TYPES:
        if not await _is_chat_admin(call.bot, call.message.chat.id, call.from_user.id):
            await call.answer("Только администраторы могут очищать список ДЗ.", show_alert=True)
            return

    db.clear_chat_homework(group_chat_id)
    await call.answer("✅ Список ДЗ очищен")
    await call.message.edit_text("📚 Список ДЗ очищен.")


# ── Подтверждение дня для pending ДЗ ────────────────────────────────────────

@router.callback_query(F.data.startswith("hw|cd|"))
async def cb_confirm_hw_day(call: CallbackQuery):
    parts    = call.data.split("|")
    key      = int(parts[2])
    date_raw = parts[3]

    pending = _pending_group_hw.pop(key, None)
    if not pending:
        await call.answer("Задание уже обработано.", show_alert=True)
        return

    await call.answer()

    # Проверка дубликата
    if db.chat_homework_exists(pending["chat_id"], pending["subject"], pending["task"]):
        msg = await call.message.edit_text(
            f"⚠️ <b>Это ДЗ уже есть в списке</b>\n"
            f"<b>{pending['subject']}</b> — {pending['task']}",
            parse_mode="HTML",
        )
        asyncio.create_task(_delete_after(msg))
        return

    if date_raw == "none":
        due_date = None
        due_text = ""
    else:
        d        = datetime.strptime(date_raw, "%Y%m%d")
        due_date = d.strftime("%Y-%m-%d")
        due_text = f"\n📅 Сдать: <b>{_fmt_due_date(due_date)}</b>"

    hw_id = db.save_chat_homework(pending["chat_id"], pending["subject"], pending["task"], due_date)

    # Запускаем фоновый анализ сложности (не блокируем пользователя)
    if due_date:
        asyncio.create_task(_analyze_hw_async(
            pending["chat_id"], hw_id, pending["subject"], pending["task"], due_date,
        ))

    kb_confirm = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📊 Нагрузка класса", callback_data=f"hw|stats|{pending['chat_id']}"),
    ]])

    msg = await call.message.edit_text(
        f"📚 <b>ДЗ сохранено</b>\n"
        f"<b>{pending['subject']}</b> — {pending['task']}{due_text}",
        parse_mode="HTML",
        reply_markup=kb_confirm,
    )
    asyncio.create_task(_delete_after(msg, delay=20))


# ── Аналитика нагрузки — inline-кнопка из /hw или подтверждения ──────────────

@router.callback_query(F.data.startswith("hw|stats|"))
async def cb_hw_stats(call: CallbackQuery):
    await call.answer()
    chat_id    = int(call.data.split("|")[2])
    chat_title = getattr(call.message.chat, "title", None) or "Класс"
    try:
        img_bytes = analytics.generate_weekly_chart(chat_id, chat_title)
        await call.message.answer_photo(
            BufferedInputFile(img_bytes, filename="load.png"),
            caption=(
                "📊 <b>Нагрузка класса на 2 недели</b>\n\n"
                "🟢 В норме  🟠 Повышенная  🔴 Перегрузка\n"
                "Пунктир — безопасная норма (3 ч/день)"
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("cb_hw_stats error")
        await call.message.answer("Ошибка генерации графика. Попробуй позже.")


# ── /stats — график нагрузки (личный чат) ────────────────────────────────────

@router.message(Command("stats"), F.chat.type == "private")
@router.message(F.text == BTN_STATS, F.chat.type == "private")
async def cmd_stats_private(message: Message):
    await safe_delete(message)
    user_id   = message.from_user.id
    group_ids = db.get_groups_for_owner(user_id)

    if not group_ids:
        await message.answer(
            "📊 <b>Нагрузка класса</b>\n\n"
            "Данных пока нет. Добавь бота в классный чат, затем:\n"
            "1. /setup_subjects — укажи предметы\n"
            "2. /link_schedule — привяжи своё расписание\n\n"
            "После этого бот начнёт собирать статистику нагрузки.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    status = await message.answer("📊 Генерирую график нагрузки...")
    try:
        chat_id   = group_ids[0]
        img_bytes = analytics.generate_weekly_chart(chat_id, "Мой класс")
        await safe_delete(status)
        await message.answer_photo(
            BufferedInputFile(img_bytes, filename="load.png"),
            caption=(
                "📊 <b>Нагрузка класса на 2 недели</b>\n\n"
                "🟢 В норме  🟠 Повышенная  🔴 Перегрузка\n"
                "Пунктир — безопасная норма (3 ч/день)\n\n"
                "<i>Данные анонимизированы — имена учеников не сохраняются.</i>"
            ),
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
    except Exception:
        logger.exception("Private stats error")
        await status.edit_text("Ошибка генерации графика. Попробуй позже.")


# ── /stats — график нагрузки (группа / канал) ────────────────────────────────

@router.message(Command("stats"), F.chat.type.in_(_GROUP_TYPES))
@router.channel_post(Command("stats"))
async def cmd_stats_group(message: Message):
    await safe_delete(message)
    status = await message.answer("📊 Генерирую график нагрузки...")
    try:
        chat_id    = message.chat.id
        chat_title = getattr(message.chat, "title", None) or "Класс"
        img_bytes  = analytics.generate_weekly_chart(chat_id, chat_title)
        await safe_delete(status)
        await message.answer_photo(
            BufferedInputFile(img_bytes, filename="load.png"),
            caption=(
                "📊 <b>Нагрузка класса на 2 недели</b>\n\n"
                "🟢 В норме  🟠 Повышенная  🔴 Перегрузка\n"
                "Пунктир — безопасная норма (3 ч/день)"
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Group stats error")
        await status.edit_text("Ошибка генерации графика. Попробуй позже.")


# ── /export_csv — экспорт метрик в CSV (только admin) ───────────────────────

@router.message(Command("export_csv"), F.chat.type.in_(_GROUP_TYPES))
@router.channel_post(Command("export_csv"))
async def cmd_export_csv(message: Message):
    if message.from_user and message.chat.type in _GROUP_TYPES:
        if not await _is_chat_admin(message.bot, message.chat.id, message.from_user.id):
            await message.reply("Только администраторы могут экспортировать данные.")
            return

    await safe_delete(message)
    try:
        csv_bytes  = analytics.export_csv(message.chat.id)
        today_str  = datetime.today().strftime("%Y%m%d")
        await message.answer_document(
            BufferedInputFile(csv_bytes, filename=f"load_{today_str}.csv"),
            caption=(
                "📊 <b>Экспорт данных о нагрузке</b>\n\n"
                "Колонки: date, task_count, total_time_minutes, total_time_hours, stress_index\n\n"
                "<i>Файл содержит только агрегированные данные класса.\n"
                "Имена учеников и user_id не включены.</i>"
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Export CSV error")
        await message.answer("Ошибка при экспорте данных.")


# ── Справка (/help) ───────────────────────────────────────────────────────────

@router.message(Command("help"))
@router.channel_post(Command("help"))
async def cmd_help(message: Message):
    if message.chat.type == "private":
        await message.answer(
            "📱 <b>Команды (личный чат)</b>\n\n"
            "/start — главное меню\n"
            "/schedule — редактор расписания по дням\n"
            "/my_schedule — посмотреть своё расписание\n"
            "/auth — подключить Google Calendar\n"
            "/stats — график нагрузки класса\n"
            "/cancel — отменить текущее действие\n\n"
            "<b>Как пользоваться:</b>\n"
            "1. Настрой расписание — /schedule\n"
            "2. Подключи Google Calendar — /auth\n"
            "3. Отправь фото ДЗ или текст — бот сам добавит\n\n"
            "<b>В группе/канале:</b>\n"
            "Добавь бота, выдай права администратора,\n"
            "затем выполни /setup_subjects и /help покажет подробности.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
    else:
        subjects = db.get_chat_subjects(message.chat.id)
        owner_id = db.get_chat_schedule_owner(message.chat.id)
        subj_str  = ", ".join(subjects) if subjects else "не настроены"
        owner_str = f"ID {owner_id}" if owner_id else "не привязано"
        await message.reply(
            "📚 <b>Команды (группа / канал)</b>\n\n"
            "/hw — список домашних заданий\n"
            "/stats — график нагрузки класса на 2 недели\n"
            "/export_csv — экспорт метрик нагрузки в CSV <i>(только admin)</i>\n"
            "/setup_subjects — настроить предметы <i>(только admin)</i>\n"
            "/link_schedule — привязать своё расписание <i>(только admin)</i>\n"
            "/clear_hw — очистить список ДЗ <i>(только admin)</i>\n"
            "/help — эта справка\n\n"
            f"<b>Предметы:</b> {subj_str}\n"
            f"<b>Расписание:</b> {owner_str}\n\n"
            "<b>Как работает:</b>\n"
            "• Бот читает каждое сообщение и фото\n"
            "• Если видит ДЗ по известному предмету — спрашивает день\n"
            "• Кнопками можно удалить любое ДЗ из /hw",
            parse_mode="HTML",
        )


# ── Мониторинг сообщений ─────────────────────────────────────────────────────

@router.message(F.chat.type.in_(_GROUP_TYPES), F.text & ~F.text.startswith("/"))
@router.channel_post(F.text & ~F.text.startswith("/"))
async def handle_group_text(message: Message):
    await _handle_group_message(message, text=message.text)


@router.message(F.chat.type.in_(_GROUP_TYPES), F.photo)
@router.channel_post(F.photo)
async def handle_group_photo(message: Message):
    photo = message.photo[-1]
    file  = await message.bot.get_file(photo.file_id)

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await message.bot.download_file(file.file_path, tmp_path)
        await _handle_group_message(message, image_path=tmp_path)
    finally:
        os.unlink(tmp_path)


# ──────────────────────────────────────────────
# Запуск
# ──────────────────────────────────────────────

async def _start_keepalive():
    """Keep-alive HTTP-сервер для Replit (предотвращает засыпание)."""
    from aiohttp import web
    port = int(os.getenv("PORT", 8080))
    app  = web.Application()
    app.router.add_get("/", lambda _r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Keep-alive сервер запущен на порту %d", port)


async def main():
    db.init_db()
    analytics.migrate_analytics_schema()

    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=JsonStorage("data/fsm.json"))
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="start",       description="Главное меню"),
        BotCommand(command="schedule",    description="Изменить расписание"),
        BotCommand(command="my_schedule", description="Посмотреть расписание"),
        BotCommand(command="stats",       description="График нагрузки класса"),
        BotCommand(command="auth",        description="Подключить Google Calendar"),
        BotCommand(command="cancel",      description="Отменить текущее действие"),
        BotCommand(command="help",        description="Справка по командам"),
    ], scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands([
        BotCommand(command="hw",              description="Список домашних заданий"),
        BotCommand(command="stats",           description="График нагрузки класса"),
        BotCommand(command="export_csv",      description="Экспорт данных нагрузки в CSV"),
        BotCommand(command="setup_subjects",  description="Настроить предметы (только admin)"),
        BotCommand(command="link_schedule",   description="Привязать расписание к группе (только admin)"),
        BotCommand(command="clear_hw",        description="Очистить список ДЗ (только admin)"),
        BotCommand(command="help",            description="Справка"),
    ], scope=BotCommandScopeAllGroupChats())

    logger.info("Бот запущен")
    await _start_keepalive()
    await dp.start_polling(
        bot,
        allowed_updates=["message", "callback_query", "channel_post"],
    )


if __name__ == "__main__":
    asyncio.run(main())
