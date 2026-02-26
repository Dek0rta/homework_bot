"""
Academic Load Analytics — модуль анализа учебной нагрузки.

Архитектура (Decoupled):
  Модуль не импортирует ничего из bot.py и независим от Telegram-логики.
  Он работает только с db.get_connection() и gemini._chat().
  Данные можно экспортировать в CSV для научного анализа.

Приватность (Privacy by Design):
  - В таблице daily_load_metrics хранятся ТОЛЬКО агрегированные данные
    на уровне чата (tenant_id = chat_id). Личные данные учеников
    (user_id, имена, текст переписки) в таблицах аналитики НЕ сохраняются.
  - LLM-оценка получает только текст задания и предмет — без идентификаторов.
  - При экспорте в CSV chat_id остаётся, но его невозможно связать
    с конкретными учениками без доступа к БД Telegram.
"""

import csv
import io
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # headless backend — работает без GUI / дисплея
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

import gemini
from db import get_connection

logger = logging.getLogger(__name__)

# Безопасная суточная норма домашних заданий (часы)
SAFE_DAILY_HOURS: float = 3.0

_DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# ── Цвета нагрузки ────────────────────────────────────────────────────────────
_COLOR_NONE   = "#b0bec5"  # серый     — нет ДЗ
_COLOR_OK     = "#66bb6a"  # зелёный   — в норме (≤ SAFE_DAILY_HOURS)
_COLOR_WARN   = "#ffa726"  # оранжевый — повышенная (≤ 1.5 × норма)
_COLOR_DANGER = "#ef5350"  # красный   — перегрузка (> 1.5 × норма)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Миграция схемы БД
# ══════════════════════════════════════════════════════════════════════════════

def migrate_analytics_schema() -> None:
    """
    Добавляет поля и таблицы для аналитики нагрузки.
    Идемпотентна — безопасна для повторного запуска.

    Новые поля в chat_homework:
      estimated_time_minutes — оценка LLM (мин), NULL до первого анализа
      priority_level         — приоритет 1 (низкий) … 5 (высокий), NULL по умолчанию

    Новая таблица daily_load_metrics:
      Агрегированные метрики по чату на каждый день.
      Не содержит user_id — только chat_id (tenant_id) и числовые показатели.
    """
    conn = get_connection()

    # Новые поля в chat_homework (миграция через ALTER TABLE)
    for col, definition in [
        ("estimated_time_minutes", "INTEGER DEFAULT NULL"),
        ("priority_level",         "INTEGER DEFAULT NULL"),
    ]:
        try:
            conn.execute(f"ALTER TABLE chat_homework ADD COLUMN {col} {definition}")
        except Exception:
            pass  # колонка уже существует — ок

    # Таблица агрегированных метрик (без личных данных учеников)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_load_metrics (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id    INTEGER NOT NULL,
            metric_date  TEXT    NOT NULL,
            task_count   INTEGER NOT NULL DEFAULT 0,
            total_time   INTEGER NOT NULL DEFAULT 0,   -- суммарное время (мин)
            stress_index REAL    NOT NULL DEFAULT 0.0,
            UNIQUE(tenant_id, metric_date)
        )
    """)

    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Оценка сложности задания через LLM
# ══════════════════════════════════════════════════════════════════════════════

async def estimate_hw_time(subject: str, task: str) -> Optional[int]:
    """
    Запрашивает Mistral оценить время выполнения задания в минутах.
    Возвращает int (5–240) или None при ошибке / невозможности оценить.

    Приватность: в LLM передаётся только текст задания и предмет,
    без каких-либо идентификаторов пользователя.
    """
    prompt = (
        "Ты опытный учитель средней школы. Оцени примерное время выполнения "
        "домашнего задания для среднего ученика 10 класса.\n\n"
        f"Предмет: {subject}\n"
        f"Задание: {task}\n\n"
        "Ответь ТОЛЬКО одним целым числом — количество минут (от 5 до 240). "
        "Никаких пояснений, только число."
    )
    try:
        raw = await gemini._chat([{"role": "user", "content": prompt}], gemini._TEXT_MODEL)
        m = re.search(r"\d+", raw.strip())
        if m:
            return max(5, min(240, int(m.group())))
    except Exception:
        logger.warning("LLM: не удалось оценить время ДЗ «%s» — «%s»", subject, task[:50])
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 3. Формула Stress Index
# ══════════════════════════════════════════════════════════════════════════════

def calculate_stress_index(
    task_count: int,
    avg_minutes: float,
    days_until_deadline: int,
) -> float:
    """
    Stress Index = (task_count × avg_time_hours) / days_until_deadline

    Методология:
      Числитель — суммарная нагрузка в часах (кол-во задач × среднее время).
      Знаменатель — время на подготовку: чем ближе дедлайн, тем выше индекс.

    Зоны:
      SI < 1.0  — низкая нагрузка  (зелёная зона)
      1.0–2.0   — умеренная        (жёлтая зона)
      2.0–3.0   — высокая          (оранжевая зона)
      SI > 3.0  — критическая      (красная зона, риск выгорания)

    Ссылка на методологию: README-ANALYTICS.md
    """
    if task_count == 0 or avg_minutes == 0:
        return 0.0
    return (task_count * avg_minutes / 60.0) / max(days_until_deadline, 1)


# ══════════════════════════════════════════════════════════════════════════════
# 4. Обновление агрегированных метрик
# ══════════════════════════════════════════════════════════════════════════════

def update_daily_metrics(chat_id: int, due_date: str) -> None:
    """
    Пересчитывает агрегированные метрики для дня due_date в чате chat_id.
    Метрики вычисляются из актуального состояния chat_homework (не инкрементально),
    поэтому устойчивы к удалению и редактированию заданий.

    tenant_id = chat_id — без привязки к конкретному пользователю.
    """
    conn = get_connection()

    # Агрегируем из реальных данных (30 мин — fallback если LLM ещё не оценил)
    row = conn.execute(
        "SELECT COUNT(*) as cnt, "
        "COALESCE(SUM(COALESCE(estimated_time_minutes, 30)), 0) as total "
        "FROM chat_homework WHERE chat_id=? AND due_date=?",
        (chat_id, due_date),
    ).fetchone()

    task_count = row["cnt"]
    total_time = row["total"]

    try:
        deadline_date = datetime.strptime(due_date, "%Y-%m-%d").date()
        days_left = max((deadline_date - date.today()).days, 1)
    except Exception:
        days_left = 1

    avg_minutes = total_time / task_count if task_count > 0 else 0
    stress = calculate_stress_index(task_count, avg_minutes, days_left)

    conn.execute(
        """
        INSERT INTO daily_load_metrics (tenant_id, metric_date, task_count, total_time, stress_index)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(tenant_id, metric_date) DO UPDATE SET
            task_count   = excluded.task_count,
            total_time   = excluded.total_time,
            stress_index = excluded.stress_index
        """,
        (chat_id, due_date, task_count, total_time, stress),
    )
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. Получение данных за 2 недели
# ══════════════════════════════════════════════════════════════════════════════

def get_weekly_load(chat_id: int) -> list[dict]:
    """
    Возвращает метрики нагрузки на 14 дней начиная с сегодня.
    Дни без ДЗ включаются с нулевыми значениями.
    """
    today = date.today()
    conn = get_connection()
    rows = conn.execute(
        "SELECT metric_date, task_count, total_time, stress_index "
        "FROM daily_load_metrics "
        "WHERE tenant_id=? AND metric_date >= ? "
        "ORDER BY metric_date",
        (chat_id, today.strftime("%Y-%m-%d")),
    ).fetchall()
    conn.close()

    by_date = {r["metric_date"]: dict(r) for r in rows}

    result = []
    for i in range(14):
        d = today + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        m = by_date.get(ds, {})
        result.append({
            "date":         d,
            "date_str":     ds,
            "task_count":   m.get("task_count",   0),
            "total_time":   m.get("total_time",   0),    # минуты
            "stress_index": m.get("stress_index", 0.0),
            "weekday":      d.weekday(),
        })
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 6. Генерация графика нагрузки
# ══════════════════════════════════════════════════════════════════════════════

def generate_weekly_chart(chat_id: int, chat_title: str = "Класс") -> bytes:
    """
    Генерирует PNG-график нагрузки на 2 недели и возвращает байты.

    График показывает:
      - Столбцы нагрузки по дням (цвет = уровень нагрузки)
      - Пики нагрузки (самые высокие столбцы)
      - Синяя линия — безопасная норма (SAFE_DAILY_HOURS)
      - Сегодняшний день выделен синей рамкой

    Приватность:
      Файл НЕ сохраняется на диск. На графике нет имён учеников —
      только агрегированные данные класса.
    """
    # Фильтруем воскресенья — в школе нет уроков
    data = [d for d in get_weekly_load(chat_id) if d["weekday"] != 6]
    today = date.today()

    labels: list[str] = []
    hours:  list[float] = []
    colors: list[str] = []

    for d in data:
        labels.append(f"{_DAYS_SHORT[d['weekday']]}\n{d['date'].strftime('%d.%m')}")
        h = d["total_time"] / 60.0
        hours.append(h)
        # Цвет по уровню нагрузки
        if h == 0:
            colors.append(_COLOR_NONE)
        elif h <= SAFE_DAILY_HOURS:
            colors.append(_COLOR_OK)
        elif h <= SAFE_DAILY_HOURS * 1.5:
            colors.append(_COLOR_WARN)
        else:
            colors.append(_COLOR_DANGER)

    # Найти индекс сегодняшнего дня в отфильтрованных данных
    today_idx = next((i for i, d in enumerate(data) if d["date"] == today), -1)

    fig, ax = plt.subplots(figsize=(12, 5))
    fig.patch.set_facecolor("#f8f9fa")
    ax.set_facecolor("#ffffff")

    has_data = any(h > 0 for h in hours)

    if not has_data:
        # Нет данных — информационное сообщение вместо графика
        ax.text(
            0.5, 0.5,
            "Нет данных о нагрузке.\n"
            "Добавляйте ДЗ с датами сдачи — график появится автоматически.",
            ha="center", va="center", fontsize=13, color="#78909c",
            transform=ax.transAxes,
        )
        ax.set_title(f"Нагрузка: {chat_title}", fontsize=13, fontweight="bold")
        ax.axis("off")
    else:
        x = np.arange(len(labels))
        bars = ax.bar(x, hours, color=colors, width=0.65, zorder=3)

        # Линия безопасной нормы
        norm_line = ax.axhline(
            SAFE_DAILY_HOURS,
            color="#1565c0", linestyle="--", linewidth=1.8, zorder=4,
            label=f"Норма ({SAFE_DAILY_HOURS:.0f} ч/день)",
        )

        # Выделяем сегодня синей рамкой
        if 0 <= today_idx < len(bars):
            bars[today_idx].set_edgecolor("#1565c0")
            bars[today_idx].set_linewidth(2.5)

        # Подписи значений на столбцах
        for bar, h in zip(bars, hours):
            if h > 0:
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + 0.06,
                    f"{h:.1f}ч",
                    ha="center", va="bottom",
                    fontsize=9, fontweight="bold", color="#212121",
                )

        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=9)
        ax.set_ylabel("Часов ДЗ", fontsize=11)
        ax.set_ylim(0, max(max(hours) * 1.35, SAFE_DAILY_HOURS * 1.6))
        ax.set_title(
            f"Нагрузка: {chat_title}  |  следующие 2 недели",
            fontsize=13, fontweight="bold",
        )
        ax.yaxis.grid(True, linestyle="--", alpha=0.4, zorder=0)
        ax.set_axisbelow(True)

        legend_patches = [
            mpatches.Patch(color=_COLOR_OK,     label=f"В норме (≤{SAFE_DAILY_HOURS:.0f} ч)"),
            mpatches.Patch(color=_COLOR_WARN,   label=f"Повышенная (≤{SAFE_DAILY_HOURS * 1.5:.0f} ч)"),
            mpatches.Patch(color=_COLOR_DANGER, label=f"Перегрузка (>{SAFE_DAILY_HOURS * 1.5:.0f} ч)"),
            mpatches.Patch(color=_COLOR_NONE,   label="Нет ДЗ"),
        ]
        ax.legend(
            handles=legend_patches + [norm_line],
            loc="upper right", fontsize=8.5, framealpha=0.85,
        )

    plt.tight_layout(pad=1.5)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ══════════════════════════════════════════════════════════════════════════════
# 7. Экспорт в CSV для научного анализа
# ══════════════════════════════════════════════════════════════════════════════

def export_csv(chat_id: int) -> bytes:
    """
    Экспортирует агрегированные метрики нагрузки в CSV (UTF-8 с BOM для Excel).

    Колонки:
      date                — дата (YYYY-MM-DD)
      task_count          — количество заданий
      total_time_minutes  — суммарное время выполнения (мин)
      total_time_hours    — то же в часах (для удобства)
      stress_index        — индекс нагрузки (формула см. calculate_stress_index)

    Приватность:
      CSV содержит ТОЛЬКО агрегированные числовые данные.
      Имена учеников, user_id и тексты заданий НЕ включены.
      Файл пригоден для анонимного научного анализа.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT metric_date, task_count, total_time, stress_index "
        "FROM daily_load_metrics WHERE tenant_id=? ORDER BY metric_date",
        (chat_id,),
    ).fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "task_count", "total_time_minutes", "total_time_hours", "stress_index"])
    for r in rows:
        writer.writerow([
            r["metric_date"],
            r["task_count"],
            r["total_time"],
            f"{r['total_time'] / 60:.2f}",
            f"{r['stress_index']:.4f}",
        ])

    # UTF-8 BOM — для корректного открытия в Microsoft Excel
    return buf.getvalue().encode("utf-8-sig")
