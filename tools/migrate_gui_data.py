# -*- coding: utf-8 -*-
"""把旧版 Orange 的数据库合并进 GUI 预览会话。

旧库（相对项目根）：
    agent/queue.db      队列、历史收发、提醒、主动聊天运行态
    memory/memory.db    长期记忆
    memory/knowledge.db 知识库

目标：GUI/.runtime/<会话>/ 下同名三个库。脚本可重复执行（幂等），
默认只打印将要导入的行数，加 --apply 才写库（写前自动备份目标库）。

队列里的 id 会被整体平移 SHIFT，避免与预览期间新产生的行冲突；
inbound / outbound / reminder_candidates / reminder_actions 之间的
引用关系一起平移。旧的采集游标（cursor）不导入：它已过期，
采集水位由新库的 collect_watermark 维护。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD = {
    "queue": ROOT / "agent" / "queue.db",
    "memory": ROOT / "memory" / "memory.db",
    "knowledge": ROOT / "memory" / "knowledge.db",
}
SESSION_ROOT = ROOT / "GUI" / ".runtime"
BACKUP_ROOT = ROOT / "backup"
SHIFT = 1_000_000


def pick_session(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = ROOT / path
        if not (path / "queue.db").is_file():
            raise SystemExit("会话目录里没有 queue.db：%s" % path)
        return path
    dirs = [d for d in SESSION_ROOT.iterdir()
            if d.is_dir() and (d / "queue.db").is_file()] if SESSION_ROOT.is_dir() else []
    if not dirs:
        raise SystemExit("找不到 GUI 预览会话目录（GUI/.runtime/<账号>/queue.db）")
    if len(dirs) > 1:
        dirs.sort(key=lambda d: (d / "queue.db").stat().st_size, reverse=True)
        print("发现多个会话，使用最大的一个：%s" % dirs[0].name)
    return dirs[0]


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute("PRAGMA table_info([%s])" % table)]


def shared(conn_a: sqlite3.Connection, conn_b: sqlite3.Connection, table: str) -> list[str]:
    """两库都存在的列，避免旧库缺列（如 attempt_dispatched）时插入失败。"""
    a = set(columns(conn_a, table))
    return [name for name in columns(conn_b, table) if name in a]


def put(dst: sqlite3.Connection, table: str, data: dict, verb: str = "INSERT OR IGNORE") -> int:
    keys = list(data)
    sql = "%s INTO %s(%s) VALUES(%s)" % (
        verb, table, ",".join(keys), ",".join("?" * len(keys)))
    return dst.execute(sql, [data[key] for key in keys]).rowcount


def make_room(dst: sqlite3.Connection, table: str):
    """显式插入大 id 后，保证自增序列不会回退。"""
    top = dst.execute("SELECT COALESCE(MAX(id), 0) FROM [%s]" % table).fetchone()[0]
    updated = dst.execute(
        "UPDATE sqlite_sequence SET seq=? WHERE name=? AND seq<?", (top, table, top)).rowcount
    if not updated and not dst.execute(
            "SELECT 1 FROM sqlite_sequence WHERE name=?", (table,)).fetchone():
        dst.execute("INSERT INTO sqlite_sequence(name, seq) VALUES(?, ?)", (table, top))


def merge_queue(old_path: Path, new_path: Path, apply: bool) -> dict[str, dict]:
    src = sqlite3.connect("file:%s?mode=ro" % old_path.as_posix(), uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(new_path, timeout=25)
    dst.row_factory = sqlite3.Row
    dst.execute("PRAGMA busy_timeout=25000")
    report: dict[str, dict] = {}

    def note(table, inserted, skipped):
        report[table] = {"new": inserted, "skip": skipped}

    def run(table, rows, build, verb="INSERT OR IGNORE"):
        inserted = skipped = 0
        try:
            for row in rows:
                if put(dst, table, build(row), verb):
                    inserted += 1
                else:
                    skipped += 1
        finally:
            note(table, inserted, skipped)

    try:
        dst.execute("BEGIN IMMEDIATE")

        # 历史入站 / 出站：id 平移，状态都是终态，不会被 worker 重发
        cols_in = shared(src, dst, "inbound")
        run("inbound", src.execute("SELECT * FROM inbound"),
            lambda row: {**{c: row[c] for c in cols_in if c != "id"}, "id": row["id"] + SHIFT})

        cols_out = shared(src, dst, "outbound")
        run("outbound", src.execute("SELECT * FROM outbound"),
            lambda row: {**{c: row[c] for c in cols_out if c != "id"}, "id": row["id"] + SHIFT})

        # 机器人发言记录：按 会话+时间+正文 去重，id 由新库自增
        def robot(row, _=None):
            return {"chat": row["chat"], "text": row["text"], "ts": row["ts"],
                    "outbound_id": None if row["outbound_id"] is None
                    else row["outbound_id"] + SHIFT,
                    "stage": row["stage"]}
        inserted = skipped = 0
        for row in src.execute("SELECT * FROM robot_sent"):
            hit = dst.execute(
                "SELECT 1 FROM robot_sent WHERE chat=? AND ts=? "
                "AND COALESCE(text,'')=COALESCE(?,'')",
                (row["chat"], row["ts"], row["text"])).fetchone()
            if hit:
                skipped += 1
            elif put(dst, "robot_sent", robot(row)):
                inserted += 1
        note("robot_sent", inserted, skipped)

        # 提醒：id 与其被引用关系保持不变（新库为空时最干净）
        cols_rem = shared(src, dst, "reminders")
        run("reminders", src.execute("SELECT * FROM reminders"),
            lambda row: {c: row[c] for c in cols_rem})

        cols_occ = shared(src, dst, "reminder_occurrences")
        run("reminder_occurrences", src.execute("SELECT * FROM reminder_occurrences"),
            lambda row: {**{c: row[c] for c in cols_occ if c != "outbound_id"},
                         "outbound_id": None if row["outbound_id"] is None
                         else row["outbound_id"] + SHIFT})

        cols_cand = shared(src, dst, "reminder_candidates")
        run("reminder_candidates", src.execute("SELECT * FROM reminder_candidates"),
            lambda row: {**{c: row[c] for c in cols_cand if c != "msg_id"},
                         "msg_id": row["msg_id"] + SHIFT})

        # 幂等键 request_key 就是 inbound.id，必须一起平移，否则会重复识别提醒
        def action(row):
            key = str(row["request_key"])
            if key.isdigit():
                key = str(int(key) + SHIFT)
            return {"account": row["account"], "chat": row["chat"], "request_key": key,
                    "reminder_id": row["reminder_id"], "result": row["result"],
                    "created_ts": row["created_ts"]}
        run("reminder_actions", src.execute("SELECT * FROM reminder_actions"), action)

        # 会话活跃时间取较新值
        inserted = skipped = 0
        for row in src.execute("SELECT * FROM chat_state"):
            changed = dst.execute(
                "INSERT INTO chat_state(chat, last_at_ts, last_at_by) VALUES(?,?,?) "
                "ON CONFLICT(chat) DO UPDATE SET "
                "last_at_ts=MAX(chat_state.last_at_ts, excluded.last_at_ts)",
                (row["chat"], row["last_at_ts"], row["last_at_by"])).rowcount
            inserted += 1 if changed else 0
        note("chat_state", inserted, skipped)

        # 主动聊天运行态：新库已有记录时不覆盖
        cols_pro = shared(src, dst, "proactive_state")
        run("proactive_state", src.execute("SELECT * FROM proactive_state"),
            lambda row: {c: row[c] for c in cols_pro})

        # 旧 cursor 不导入（旧值已过期，采集水位由 collect_watermark 维护）
        report["cursor"] = {"new": 0, "skip": "未导入：采集水位改用 collect_watermark"}

        for table in ("inbound", "outbound", "robot_sent", "reminders",
                      "reminder_candidates", "reminder_occurrences"):
            make_room(dst, table)
    finally:
        if apply:
            dst.commit()
        else:
            dst.rollback()
        src.close()
        dst.close()
    return report


def merge_memory(old_path: Path, new_path: Path, apply: bool) -> dict[str, dict]:
    src = sqlite3.connect("file:%s?mode=ro" % old_path.as_posix(), uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(new_path, timeout=25)
    dst.row_factory = sqlite3.Row
    dst.execute("PRAGMA busy_timeout=25000")
    report: dict[str, dict] = {}
    try:
        dst.execute("BEGIN IMMEDIATE")
        inserted = skipped = 0
        for row in src.execute("SELECT * FROM messages"):
            # 先按内容判重：新旧库同一段对话的 src_key 可能是各自生成的
            hit = dst.execute(
                "SELECT 1 FROM messages WHERE chat=? AND role=? AND ts=? AND content=?",
                (row["chat"], row["role"], row["ts"], row["content"])).fetchone()
            if not hit and row["src_key"]:
                hit = dst.execute("SELECT 1 FROM messages WHERE chat=? AND src_key=?",
                                  (row["chat"], row["src_key"])).fetchone()
            if hit:
                skipped += 1
            elif put(dst, "messages", {"chat": row["chat"], "role": row["role"],
                                       "content": row["content"], "ts": row["ts"],
                                       "src_key": row["src_key"], "sender": row["sender"]}):
                inserted += 1
        report["messages"] = {"new": inserted, "skip": skipped}

        inserted = skipped = 0
        for row in src.execute("SELECT * FROM facts"):
            if dst.execute("SELECT 1 FROM facts WHERE chat=? AND fact=?",
                           (row["chat"], row["fact"])).fetchone():
                skipped += 1
                continue
            if put(dst, "facts", {"chat": row["chat"], "fact": row["fact"], "ts": row["ts"],
                                  "topic": row["topic"], "active": row["active"],
                                  "src": row["src"]}):
                inserted += 1
        report["facts"] = {"new": inserted, "skip": skipped}

        for table, key in (("profiles", "chat"), ("adapt", "chat"), ("media_desc", "md5")):
            inserted = skipped = 0
            cols_ = shared(src, dst, table)
            for row in src.execute("SELECT * FROM [%s]" % table):
                data = {c: row[c] for c in cols_}
                if dst.execute("SELECT 1 FROM [%s] WHERE %s=?" % (table, key),
                               (row[key],)).fetchone():
                    skipped += 1
                elif put(dst, table, data):
                    inserted += 1
            report[table] = {"new": inserted, "skip": skipped}

        for table in ("messages", "facts"):
            make_room(dst, table)
    finally:
        if apply:
            dst.commit()
        else:
            dst.rollback()
        src.close()
        dst.close()
    return report


def merge_knowledge(old_path: Path, new_path: Path, apply: bool) -> dict[str, dict]:
    src = sqlite3.connect("file:%s?mode=ro" % old_path.as_posix(), uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(new_path, timeout=25)
    dst.row_factory = sqlite3.Row
    dst.execute("PRAGMA busy_timeout=25000")
    report: dict[str, dict] = {}
    try:
        dst.execute("BEGIN IMMEDIATE")
        for table in ("kb_documents", "kb_revisions", "kb_chunks", "kb_jobs"):
            inserted = skipped = 0
            cols_ = shared(src, dst, table)
            for row in src.execute("SELECT * FROM [%s]" % table):
                data = {c: row[c] for c in cols_}
                if put(dst, table, data):
                    inserted += 1
                else:
                    skipped += 1
            report[table] = {"new": inserted, "skip": skipped}
        # 全文索引是 external content 表，只能重建，不能直接插行
        dst.execute("INSERT INTO kb_chunks_fts(kb_chunks_fts) VALUES('rebuild')")
        for table in ("kb_documents", "kb_revisions", "kb_chunks", "kb_jobs"):
            make_room(dst, table)
    finally:
        if apply:
            dst.commit()
        else:
            dst.rollback()
        src.close()
        dst.close()
    return report


def backup(targets: list[Path]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = BACKUP_ROOT / ("%s-pre-migrate" % stamp)
    folder.mkdir(parents=True, exist_ok=True)
    for path in targets:
        if path.is_file():
            shutil.copy2(path, folder / path.name)
    return folder


def show(name: str, report: dict[str, dict]):
    print("\n[%s]" % name)
    for table, stat in report.items():
        if isinstance(stat["skip"], str):
            print("  %-22s %s" % (table, stat["skip"]))
        else:
            print("  %-22s 新增 %4d   已存在 %4d" % (table, stat["new"], stat["skip"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="把旧版 Orange 数据库合并进 GUI 预览会话")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只报告）")
    parser.add_argument("--session", help="GUI 会话目录，默认自动发现")
    args = parser.parse_args()

    session = pick_session(args.session)
    targets = {kind: session / ("%s.db" % kind) for kind in OLD}
    for kind, path in OLD.items():
        if not path.is_file():
            raise SystemExit("旧库不存在：%s" % path)
    print("目标会话：%s" % session)
    try:
        session.relative_to(ROOT)
        print("会话相对路径：%s" % session.relative_to(ROOT))
    except ValueError:
        pass

    if args.apply:
        folder = backup(list(targets.values()))
        print("已备份目标库：%s" % folder)

    started = time.time()
    show("queue.db", merge_queue(OLD["queue"], targets["queue"], args.apply))
    show("memory.db", merge_memory(OLD["memory"], targets["memory"], args.apply))
    show("knowledge.db", merge_knowledge(OLD["knowledge"], targets["knowledge"], args.apply))
    print("\n%s，用时 %.1fs" % ("已写入" if args.apply else "仅报告（加 --apply 执行）",
                              time.time() - started))
    if not args.apply:
        print("提示：worker 正在运行时也能在线合并，写库前请确认 GUI 已停止或接受短暂锁等待。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
