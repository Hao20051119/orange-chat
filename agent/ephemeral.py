# -*- coding: utf-8 -*-
"""临时上下文：``memory.enabled=false`` 时替代数据库记忆。

只要进程内的短期上下文（默认 30 分钟 TTL），不落库、不摘要、不注入历史画像。
接口与 :class:`store.MemoryStore` 的最小集合对齐，Brain 不需要分支判断。
"""

from __future__ import annotations

import threading
import time


class EphemeralStore:
    # 只读属性，方便 Brain/日志标注当前用的是哪种记忆
    persistent = False

    def __init__(self, ttl: int = 1800, limit: int = 40):
        self.ttl = int(ttl or 1800)
        self.limit = int(limit or 40)
        self._data: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 内部
    def _alive(self, chat: str) -> list[dict]:
        cutoff = time.time() - self.ttl
        rows = [m for m in self._data.get(chat, []) if m["ts"] >= cutoff]
        if rows:
            self._data[chat] = rows[-self.limit:]
        else:
            self._data.pop(chat, None)
        return self._data.get(chat, [])

    # ------------------------------------------------------------ 写
    def add(self, chat, role, content, ts=None, src_key=None, sender=None) -> int:
        with self._lock:
            rows = self._alive(chat)
            rows.append({"role": role, "content": content,
                         "ts": int(ts or time.time()), "sender": sender or ""})
            self._data[chat] = rows[-self.limit:]
            return len(self._data[chat])

    def add_fact(self, chat, fact, topic=None, src=None) -> dict:
        return {"ok": True, "ephemeral": True}

    # ------------------------------------------------------------ 读
    def recent(self, chat, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(self._alive(chat)[-int(limit):])

    def count(self, chat) -> int:
        with self._lock:
            return len(self._alive(chat))

    def profile(self, chat) -> dict:
        return {"chat": chat, "summary": "", "portrait": "", "turns": 0,
                "updated_at": 0}

    def facts(self, chat, limit: int = 30) -> list[str]:
        return []

    def all_chats(self) -> list[dict]:
        return []

    # ------------------------------------------------------------ 压缩
    def should_summarize(self, chat, threshold: int = 60) -> bool:
        return False

    def summarize(self, chat, llm_chat, keep_recent: int = 20, batch: int = 40) -> dict:
        return {"ok": False, "reason": "临时上下文不摘要"}

    # ------------------------------------------------------------ 媒体缓存（不缓存）
    def media_desc(self, md5: str) -> str | None:
        return None

    def save_media_desc(self, md5: str, desc: str):
        return None

    # ------------------------------------------------------------ 长期观察（不持久化）
    def get_adapt(self, chat) -> dict:
        return {"chat": chat, "notes": "", "seen": 0, "updated_ts": 0}

    def save_adapt(self, chat, notes, seen):
        return None
