# -*- coding: utf-8 -*-
"""清理测试残留：把历史测试写进生产记忆库的假会话删掉。

默认 dry-run，只打印将要删除的内容；确认后加 ``--apply``。

用法：
    python tools\\clean_test_data.py
    python tools\\clean_test_data.py --apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEM = os.path.join(ROOT, "memory", "memory.db")
QUEUE = os.path.join(ROOT, "agent", "queue.db")

EXACT = {
    "__draft__", "__end__", "__sens2__", "__sens__", "__tune__", "__vision_test__",
    "__web_test__", "p03", "wxid_some_friend", "stranger", "friend", "allowed_only",
    "c1", "c2", "c3", "c4", "chatA", "chatB", "chatC", "chatS", "chatT", "chatOld",
    "wxid_test", "123@chatroom",
}
PREFIXES = ("__", "wxid_test", "test_", "_test_")


def is_test_chat(chat: str) -> bool:
    if not chat:
        return False
    if chat in EXACT:
        return True
    return any(chat.startswith(p) for p in PREFIXES)


def inspect(db_path: str) -> list[dict]:
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT chat, COUNT(*) AS n FROM messages GROUP BY chat ORDER BY n DESC"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return [{"chat": r["chat"], "n": r["n"], "test": is_test_chat(r["chat"])}
            for r in rows]


def purge(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        chats = [r[0] for r in conn.execute(
            "SELECT DISTINCT chat FROM messages").fetchall()]
        targets = [c for c in chats if is_test_chat(c)]
        total = 0
        with conn:
            for c in targets:
                for table in ("messages", "profiles", "facts"):
                    cur = conn.execute("DELETE FROM %s WHERE chat=?" % table, (c,))
                    total += cur.rowcount
        return total
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只列出）")
    ap.add_argument("--db", default=MEM, help="记忆库路径")
    args = ap.parse_args()

    rows = inspect(args.db)
    if not rows:
        print("没有可检查的数据库：%s" % args.db)
        return 0

    print("会话清单（%s）：" % args.db)
    for r in rows:
        print("  %-28s %4d 条  %s" % (r["chat"], r["n"],
                                      "← 测试残留" if r["test"] else ""))
    targets = [r for r in rows if r["test"]]
    if not targets:
        print("\n没有发现测试残留。")
        return 0

    print("\n将删除 %d 个测试会话，共 %d 条消息（含 profiles/facts）："
          % (len(targets), sum(t["n"] for t in targets)))
    for t in targets:
        print("  - %s" % t["chat"])
    if not args.apply:
        print("\n这是 dry-run。确认无误后加 --apply 执行。")
        return 0

    n = purge(args.db)
    print("\n已删除 %d 行。" % n)
    print("提示：删完可跑 `python memory\\store.py` 看剩余会话。")
    out = sorted({r["chat"] for r in rows} - {t["chat"] for t in targets})
    if out:
        print("保留的会话：%s" % ", ".join(out))
    return 0


if __name__ == "__main__":
    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
