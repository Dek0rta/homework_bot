"""
Файловое хранилище FSM-состояний.
Пишет на диск асинхронно (не блокирует event loop).
"""

import asyncio
import json
import os
from typing import Any, Dict, Optional

import aiofiles
from aiogram.fsm.storage.base import BaseStorage, StorageKey


class JsonStorage(BaseStorage):
    def __init__(self, path: str = "data/fsm.json"):
        self._path = path
        self._data: dict = self._load_sync()
        self._save_task: Optional[asyncio.Task] = None

    # ── синхронная загрузка при старте ──────────────────────────
    def _load_sync(self) -> dict:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}
        return {}

    # ── асинхронная запись (дебаунс 300 мс) ─────────────────────
    def _schedule_save(self):
        """Откладывает запись на диск — несколько быстрых изменений дают один write."""
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
        try:
            loop = asyncio.get_running_loop()
            self._save_task = loop.create_task(self._debounced_save())
        except RuntimeError:
            pass  # нет event loop — запишем при close()

    async def _debounced_save(self):
        await asyncio.sleep(0.3)
        await self._flush()

    async def _flush(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        content = json.dumps(self._data, ensure_ascii=False, indent=2)
        async with aiofiles.open(self._path, "w", encoding="utf-8") as f:
            await f.write(content)

    # ── BaseStorage interface ────────────────────────────────────
    def _key(self, key: StorageKey) -> str:
        return f"{key.chat_id}:{key.user_id}:{key.destiny}"

    async def set_state(self, key: StorageKey, state=None) -> None:
        if state is None:
            state_str = None
        elif isinstance(state, str):
            state_str = state
        else:
            state_str = state.state
        self._data.setdefault(self._key(key), {})["state"] = state_str
        self._schedule_save()

    async def get_state(self, key: StorageKey) -> Optional[str]:
        return self._data.get(self._key(key), {}).get("state")

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        self._data.setdefault(self._key(key), {})["data"] = data
        self._schedule_save()

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        return dict(self._data.get(self._key(key), {}).get("data", {}))

    async def close(self) -> None:
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
        await self._flush()
