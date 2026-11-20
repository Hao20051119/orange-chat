r"""回退采集水位，重放被误判丢弃的消息（修「消息被误判丢弃、水位已越过」这类事故用）。

背景：`collect_watermark` 是 MAX 语义，只会前进。如果某条消息在入库前被误判丢弃，
水位却记到了它的 sort_seq，那么 `get_new_messages(since_seq=水位)` 永远读不到它 ——
重启也无效。这个工具把水位显式回退，采集器下一轮就会重扫，把漏掉的消息补进队列。

用法（在项目目录下、后台服务正在运行）：

    set PYTHONPATH=.;.\pylibs;.\wxbot

    # 1) 先看现状：每个会话的水位、最后一条入站
    python tools\\replay_chat.py --list

    # 2) 回退某个会话的水位到指定 sort_seq 之前（--seq 之后的消息会被重扫）
    python tools\\replay_chat.py --chat wxid_联系人 --seq <sort_seq> --yes

    # 3) 不知道 seq 也行：指定"从这条时间点开始重扫"（本地时间）
    python tools\\replay_chat.py --chat wxid_联系人 --since "2026-11-21 00:30" --yes

回退是安全的：已经入过库的消息会命中 UNIQUE(account, chat, local_id, msg_ts) 而幂等
跳过，不会重复入队、也不会重复回复。回退后 1~2 秒内看 reply.log 应出现
「入队 [...]」。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pylibs"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "wxbot"))


def pick_db() -> Path | None:
    env = (os.environ.get("WXBOT_QUEUE_DB") or "").strip()
    if env:
        return Path(env)
    runtime = ROOT / "GUI" / ".runtime"
    if runtime.is_dir():
        sessions = sorted([p for p in runtime.iterdir() if (p / "queue.db").is_file()],
                          key=lambda p: (p / "queue.db").stat().st_mtime, reverse=True)
        if sessions:
            return sessions[0] / "queue.db"
    legacy = ROOT / "queue.db"
    return legacy if legacy.is_file() else None


def read_rows(db: Path, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def show(db: Path):
    print("队列库:", db)
    rows = read_rows(db, "SELECT account, chat, sort_seq FROM collect_watermark "
                         "ORDER BY sort_seq DESC")
    if not rows:
        print("（还没有任何采集水位）")
    for r in rows:
        last = read_rows(db, "SELECT MAX(msg_ts) AS ts, COUNT(*) AS n FROM inbound "
                             "WHERE account=? AND chat=?", (r["account"], r["chat"]))[0]
        ts = last["ts"]
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(ts / 1000 if ts > 1e11 else ts)) \
            if ts else "—"
        print("  %-28s %-34s 水位=%s  入站=%s条 最后=%s"
              % (r["account"], r["chat"], r["sort_seq"], last["n"], when))
        newer = read_rows(db, "SELECT COUNT(*) AS n FROM inbound WHERE account=? AND chat=? "
                              "AND sort_seq > ?",
                          (r["account"], r["chat"], r["sort_seq"]))[0]["n"]
        if newer:
            print("     注意：有 %d 条入站的 sort_seq 比水位还大（异常）" % newer)


def rewind(db: Path, account: str, chat: str, seq: int) -> None:
    sys.path.insert(0, str(ROOT / "agent"))
    from wxqueue import Queue  # 延迟导入：避免 --list 时顺带建表
    queue = Queue(str(db))
    old = queue.rewind_collect_watermark(account, chat, seq)
    print("已回退水位：%s → %s（旧值 %s）" % (chat, seq, "无记录" if old is None else old))
    print("采集器会在 1~2 秒内重扫 %s 之后的消息；看 reply.log 里的「入队 [...]」。" % seq)


def main() -> int:
    parser = argparse.ArgumentParser(description="回退采集水位，重放漏掉的消息")
    parser.add_argument("--db", help="queue.db 路径（默认取最新的 GUI 会话库）")
    parser.add_argument("--account", help="小号 wxid（--list 时只显示它）")
    parser.add_argument("--list", action="store_true", help="只列出各会话水位")
    parser.add_argument("--chat", help="要重放的会话（联系人的 wxid）")
    parser.add_argument("--seq", type=int, help="回退到这个 sort_seq（之后的消息会被重扫）")
    parser.add_argument("--since", help='本地时间，例如 "2026-11-21 00:30"，等价于它的毫秒时间戳-1')
    parser.add_argument("--yes", action="store_true", help="确认写入（不加只预览）")
    args = parser.parse_args()

    db = Path(args.db) if args.db else pick_db()
    if db is None or not db.is_file():
        print("!! 找不到 queue.db：用 --db 指定，或先启动一次 GUI 会话")
        return 2
    if args.list or not args.chat:
        show(db)
        if not args.chat:
            print("\n要回退就用：--chat <wxid> --seq <sort_seq>（或 --since \"YYYY-MM-DD HH:MM\"）--yes")
            return 0

    account = args.account
    if not account:
        cfg = ROOT / "config.json"
        try:
            import json
            account = json.loads(cfg.read_text(encoding="utf-8-sig")).get("account") or ""
        except Exception:
            account = ""
        if not account:
            rows = read_rows(db, "SELECT DISTINCT account FROM collect_watermark LIMIT 1")
            account = rows[0]["account"] if rows else ""
    if not account:
        print("!! 不知道小号 wxid：用 --account 指定")
        return 2

    seq = args.seq
    if seq is None and args.since:
        try:
            stamp = datetime.strptime(args.since, "%Y-%m-%d %H:%M")
        except ValueError:
            print('!! --since 格式要像 "2026-11-21 00:30"')
            return 2
        seq = int(stamp.timestamp() * 1000) - 1
    if seq is None:
        print("!! 需要 --seq 或 --since")
        return 2

    current = read_rows(db, "SELECT sort_seq FROM collect_watermark WHERE account=? AND chat=?",
                        (account, args.chat))
    old = current[0]["sort_seq"] if current else None
    print("小号    :", account)
    print("会话    :", args.chat)
    print("当前水位:", old)
    print("回退到  :", seq)
    if old is not None and seq >= old:
        print("水位已经 ≤ 目标值，不需要回退（不会改）")
        return 0
    if not args.yes:
        print("\n只是预览。确认要写入请再加 --yes")
        return 0
    rewind(db, account, args.chat, seq)
    return 0


if __name__ == "__main__":
    sys.exit(main())
