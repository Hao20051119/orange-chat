# -*- coding: utf-8 -*-
"""测试支持：临时库、临时日志、假模型。

任何测试都不允许碰生产库（``agent/queue.db`` / ``memory/memory.db``）：
导入本模块时就把 ``WXBOT_MEMORY_DB`` / ``WXBOT_QUEUE_DB`` / ``WXBOT_LOG``
指向临时目录，之后 import 的业务模块会自动使用这些路径。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "agent")
MEMDIR = os.path.join(ROOT, "memory")
STICKERS = os.path.join(ROOT, "stickers")
WXBOT = os.path.join(ROOT, "wxbot")
PYLIB = os.environ.get("WXBOT_PYLIB") or os.path.join(ROOT, "pylibs")

_TMP = tempfile.mkdtemp(prefix="wxbot_test_")
os.environ["WXBOT_MEMORY_DB"] = os.path.join(_TMP, "memory.db")
os.environ["WXBOT_QUEUE_DB"] = os.path.join(_TMP, "queue.db")
os.environ["WXBOT_LOG"] = os.path.join(_TMP, "reply.log")
os.environ["WXBOT_KB_DB"] = os.path.join(_TMP, "knowledge.db")

for _p in (AGENT, MEMDIR, STICKERS, WXBOT, PYLIB):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

TMP = _TMP
PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✅ %s" % name)
    else:
        FAIL += 1
        FAILURES.append("%s %s" % (name, detail))
        print("  ❌ %s %s" % (name, detail))


def section(title: str):
    print("\n" + "=" * 60)
    print(title)


def finish() -> int:
    print("\n" + "=" * 60)
    print("通过 %d / 失败 %d" % (PASS, FAIL))
    if FAILURES:
        for f in FAILURES:
            print("  - " + f)
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if FAIL == 0 else 1


# ---------------------------------------------------------------- 工厂
def make_queue(name: str = "q.db"):
    from wxqueue import Queue
    return Queue(os.path.join(TMP, name))


def make_memory(name: str = "m.db"):
    from store import MemoryStore
    return MemoryStore(os.path.join(TMP, name))


def base_cfg(**over) -> dict:
    """一份最小可用配置，测试里按需覆盖。"""
    cfg = {
        "account": "wxid_test",
        "llm": {"api_key": "sk-test", "model": "deepseek-chat"},
        "agent": {
            "name": "🍊", "persona": "测试人设", "auto_reply": True,
            "reply_whitelist": [], "reply_blacklist": [], "group_reply": False,
            "group_mode": "at_only", "group_whitelist": [], "at_names": ["🍊"],
            "group_at_reply": True, "group_followup_minutes": 5,
            "group_analysis": True, "group_member_memory": True,
            "min_delay": 0, "max_delay": 0,
            "style": {"length": "short", "tone": "casual", "emojis": True,
                      "typos": False, "no_question": False, "reply_probability": 1.0},
        },
        "bot": {"max_parallel_chats": 2},
        "send": {"bubble": True, "bubble_max_chars": 40, "max_bubbles": 3,
                 "min_bubble_gap": 0, "max_bubble_gap": 0,
                 "max_draft_age": 300, "verify_seconds": 1},
        "memory": {"enabled": True, "max_turns": 20, "summary_threshold": 30,
                   "record_skipped": True, "ephemeral_ttl": 60, "adapt_every": 30,
                   "history_msg_chars": 600, "max_context_chars": 12000},
        "merge": {"quiet_seconds": 2.0, "max_wait_seconds": 8.0,
                  "invalidate_on_new": True, "max_regen": 2,
                  "max_age_seconds": 21600, "backfill_max": 5},
        "takeover": {"pause_minutes": 30, "enabled": False, "auto_resume": True},
        "web": {"enabled": False, "max_queries_per_turn": 2, "max_pages_per_turn": 3,
                "timeout": 8, "deadline_ms": 8000, "daily_budget": 200},
        "contacts": {},
        "logging": {"keep_days": 14, "max_mb": 5, "chat_content": False},
    }
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k] = {**cfg[k], **v}
        else:
            cfg[k] = v
    return cfg
