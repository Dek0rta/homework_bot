import sqlite3
import os
from datetime import datetime
from config import DB_PATH


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            day_of_week INTEGER NOT NULL,
            subject     TEXT    NOT NULL,
            start_time  TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_homework (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id  INTEGER NOT NULL,
            subject  TEXT    NOT NULL,
            task     TEXT    NOT NULL,
            added_at TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_subjects (
            chat_id INTEGER NOT NULL,
            subject TEXT    NOT NULL,
            PRIMARY KEY (chat_id, subject)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_config (
            chat_id           INTEGER PRIMARY KEY,
            schedule_owner_id INTEGER
        )
    """)
    # Миграция: добавляем due_date если таблица уже существует без неё
    try:
        conn.execute("ALTER TABLE chat_homework ADD COLUMN due_date TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()


def save_schedule(user_id: int, entries: list[dict]):
    """entries = [{"day": 0, "subject": "Математика", "start_time": "8:00"}, ...]"""
    conn = get_connection()
    conn.execute("DELETE FROM schedule WHERE user_id = ?", (user_id,))
    conn.executemany(
        "INSERT INTO schedule (user_id, day_of_week, subject, start_time) VALUES (?, ?, ?, ?)",
        [(user_id, e["day"], e["subject"], e["start_time"]) for e in entries],
    )
    conn.commit()
    conn.close()


def get_schedule(user_id: int) -> list[dict]:
    conn = get_connection()
    cur = conn.execute(
        "SELECT day_of_week, subject, start_time FROM schedule "
        "WHERE user_id = ? ORDER BY day_of_week, start_time",
        (user_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def has_schedule(user_id: int) -> bool:
    conn = get_connection()
    cur = conn.execute("SELECT 1 FROM schedule WHERE user_id = ? LIMIT 1", (user_id,))
    result = cur.fetchone()
    conn.close()
    return result is not None


# ── Групповое ДЗ ────────────────────────────────────────────────────────────

def save_chat_homework(chat_id: int, subject: str, task: str, due_date: str | None = None):
    conn = get_connection()
    conn.execute(
        "INSERT INTO chat_homework (chat_id, subject, task, added_at, due_date) VALUES (?, ?, ?, ?, ?)",
        (chat_id, subject, task, datetime.now().strftime("%d.%m %H:%M"), due_date),
    )
    conn.commit()
    conn.close()


def get_chat_homework(chat_id: int) -> list[dict]:
    conn = get_connection()
    cur = conn.execute(
        "SELECT id, subject, task, added_at, due_date FROM chat_homework "
        "WHERE chat_id = ? ORDER BY id DESC LIMIT 50",
        (chat_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def chat_homework_exists(chat_id: int, subject: str, task: str) -> bool:
    conn = get_connection()
    cur = conn.execute(
        "SELECT 1 FROM chat_homework WHERE chat_id=? AND subject=? AND task=? LIMIT 1",
        (chat_id, subject, task),
    )
    result = cur.fetchone()
    conn.close()
    return result is not None


def delete_chat_homework(hw_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM chat_homework WHERE id = ?", (hw_id,))
    conn.commit()
    conn.close()


def clear_chat_homework(chat_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM chat_homework WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def get_chat_subjects(chat_id: int) -> list[str]:
    conn = get_connection()
    cur = conn.execute(
        "SELECT subject FROM chat_subjects WHERE chat_id = ? ORDER BY subject",
        (chat_id,),
    )
    rows = [r["subject"] for r in cur.fetchall()]
    conn.close()
    return rows


def set_chat_schedule_owner(chat_id: int, owner_id: int):
    conn = get_connection()
    conn.execute(
        "INSERT INTO chat_config (chat_id, schedule_owner_id) VALUES (?, ?)"
        " ON CONFLICT(chat_id) DO UPDATE SET schedule_owner_id=excluded.schedule_owner_id",
        (chat_id, owner_id),
    )
    conn.commit()
    conn.close()


def get_chat_schedule_owner(chat_id: int) -> int | None:
    conn = get_connection()
    cur = conn.execute("SELECT schedule_owner_id FROM chat_config WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    return row["schedule_owner_id"] if row else None


def set_chat_subjects(chat_id: int, subjects: list[str]):
    conn = get_connection()
    conn.execute("DELETE FROM chat_subjects WHERE chat_id = ?", (chat_id,))
    conn.executemany(
        "INSERT INTO chat_subjects (chat_id, subject) VALUES (?, ?)",
        [(chat_id, s) for s in subjects],
    )
    conn.commit()
    conn.close()
