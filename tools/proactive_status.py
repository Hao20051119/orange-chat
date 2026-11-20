r"""主动聊天 / 定时提醒 迁移后的状态体检（只读，不改任何东西）。

导入数据到另一台机器后，用它直接看清"下次还会不会主动发"：

    set PYTHONPATH=.;.\pylibs;.\wxbot
    python tools\\proactive_status.py

它会逐个联系人对齐代码里的判定顺序（开关 → 白名单 → 时段 → 静默 → 冷却 →
无回应计数 → 当日配额 → next_at），告诉你现在这一秒如果 tick 一次，
到底会发、还是会等、还是永远不发。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pick_queue() -> Path | None:
    env = (os.environ.get("WXBOT_QUEUE_DB") or "").strip()
    if env:
        return Path(env)
    runtime = ROOT / "GUI" / ".runtime"
    if runtime.is_dir():
        sessions = sorted([p for p in runtime.iterdir() if (p / "queue.db").is_file()],
                          key=lambda p: (p / "queue.db").stat().st_mtime, reverse=True)
        if sessions:
            return sessions[0] / "queue.db"
    legacy = ROOT / "agent" / "queue.db"
    return legacy if legacy.is_file() else None


def clock(ts) -> str:
    try:
        ts = float(ts or 0)
    except (TypeError, ValueError):
        return "—"
    if ts <= 0:
        return "—"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def ago(ts, now) -> str:
    ts = float(ts or 0)
    if ts <= 0:
        return ""
    delta = ts - now
    if abs(delta) < 60:
        return "刚刚"
    unit, span = ("分钟", 60) if abs(delta) < 3600 else ("小时", 3600)
    if abs(delta) >= 86400:
        unit, span = "天", 86400
    return ("还差 %.1f %s" % (delta / span, unit)) if delta > 0 else \
           ("已过 %.1f %s" % (-delta / span, unit))


def rows(db: Path, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def in_hours(hours, now: float) -> bool:
    if not isinstance(hours, (list, tuple)) or len(hours) != 2:
        return True
    try:
        lo, hi = int(hours[0]), int(hours[1])
    except (TypeError, ValueError):
        return True
    if lo == hi:
        return True
    h = time.localtime(now).tm_hour
    return (lo <= h < hi) if lo < hi else (h >= lo or h < hi)


def reset_chat(db: Path, chat: str, confirmed: bool) -> int:
    """把某个联系人的主动节奏清零：立刻可再次尝试（迁移后冷却卡住时用）。"""
    rows_before = rows(db, "SELECT * FROM proactive_state WHERE chat=?", (chat,))
    if not rows_before:
        print("!! 这个会话没有 proactive_state（还没跑过主动调度）")
        return 1
    print("重置前：")
    for r in rows_before:
        print("  %s next_at=%s 冷却=%s 静默=%s 无回应=%s 今日=%s/%s"
              % (r["account"], clock(r["next_at"]), clock(r["cooldown_until"]),
                 clock(r["quiet_until"]), r["no_reply_streak"], r["day_n"], r["day"]))
    if not confirmed:
        print("\n只是预览。确认要写请再加 --yes")
        return 0
    import time as _t
    today = _t.strftime("%Y-%m-%d")
    conn = sqlite3.connect(str(db), timeout=10)
    try:
        cur = conn.execute(
            "UPDATE proactive_state SET next_at=0, quiet_until=0, cooldown_until=0, "
            "await_reply_until=0, no_reply_streak=0, day_n=0, day=?, updated_ts=? "
            "WHERE chat=?", (today, int(_t.time()), chat))
        conn.commit()
        print("\n已重置 %d 行（next_at=0、冷却/静默/无回应计数清零、今日配额归零）" % cur.rowcount)
        print("服务在跑的话，下一个 15 秒 tick 就会重新尝试（仍要看开关、时段和白名单）。")
    finally:
        conn.close()
    after = rows(db, "SELECT * FROM proactive_state WHERE chat=?", (chat,))
    for r in after:
        print("  %s → next_at=%s 冷却=%s 无回应=%s"
              % (r["account"], clock(r["next_at"]), clock(r["cooldown_until"]),
                 r["no_reply_streak"]))
    return 0


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="主动聊天/提醒状态体检（默认只读）")
    parser.add_argument("--reset-chat", help="清掉这个联系人的节奏（next_at/冷却/无回应计数/今日配额）")
    parser.add_argument("--yes", action="store_true", help="配合 --reset-chat 确认写入")
    args = parser.parse_args()

    db = pick_queue()
    if db is None or not db.is_file():
        print("!! 找不到 queue.db：先在 GUI 里「连接并启动」一次，或用 WXBOT_QUEUE_DB 指定")
        return 2
    if args.reset_chat:
        return reset_chat(db, args.reset_chat, args.yes)
    cfg = {}
    cfg_path = db.parent / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except Exception:
        cfg_path = ROOT / "config.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        except Exception:
            cfg = {}
    agent = cfg.get("agent") or {}
    pro = cfg.get("proactive") or {}
    rem_cfg = pro.get("reminders") or {}
    now = time.time()

    print("=" * 70)
    print("队列库 :", db)
    print("会话配置:", cfg_path, "（存在）" if cfg_path.is_file() else "（不存在，回退根配置）")
    print("现在   :", time.strftime("%Y-%m-%d %H:%M:%S"))
    print("\n--- 开关（缺一不可）---")
    print("  agent.auto_reply      :", agent.get("auto_reply"))
    print("  proactive.enabled     :", pro.get("enabled"))
    print("  proactive.whitelist   :", pro.get("whitelist"))
    print("  active_hours          :", pro.get("active_hours"), "→ 当前", "在时段内"
          if in_hours(pro.get("active_hours", [9, 23]), now) else "不在时段内")
    print("  per_chat_daily        :", pro.get("per_chat_daily"), " global_daily:",
          pro.get("global_daily"), " global_guard:", pro.get("global_guard"))
    print("  reminders.enabled     :", rem_cfg.get("enabled"),
          " grace_seconds:", rem_cfg.get("grace_seconds", 7200))

    print("\n--- 主动聊天：逐个联系人判定 ---")
    states = rows(db, "SELECT * FROM proactive_state ORDER BY next_at")
    if not states:
        print("  （没有任何 proactive_state：这台机器还没跑过主动调度）")
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    contacts = cfg.get("contacts") or {}
    for st in states:
        chat = st["chat"]
        reasons, waits, forever = [], [], []
        if not agent.get("auto_reply"):
            forever.append("auto_reply 关")
        if not pro.get("enabled"):
            forever.append("proactive.enabled 关")
        if chat not in (pro.get("whitelist") or []):
            forever.append("不在白名单")
        if not (contacts.get(chat) or {}).get("proactive"):
            forever.append("联系人主动开关未开")
        if st["muted"]:
            forever.append("该联系人被静音")
        if float(st["next_at"] or 0) > now:
            waits.append("时间未到（%s %s）" % (clock(st["next_at"]), ago(st["next_at"], now)))
        if float(st["quiet_until"] or 0) > now:
            waits.append("静默到 %s（%s）" % (clock(st["quiet_until"]), ago(st["quiet_until"], now)))
        if float(st["cooldown_until"] or 0) > now:
            waits.append("冷却到 %s（%s）" % (clock(st["cooldown_until"]),
                                            ago(st["cooldown_until"], now)))
        await_until = float(st["await_reply_until"] or 0)
        streak = int(st["no_reply_streak"] or 0)
        if await_until and now > await_until:
            streak += 1
            if streak >= 3:
                waits.append("下次 tick 会把无回应计数记到 %d → 立刻冷却 3 天" % streak)
            else:
                reasons.append("上次主动后 24h 没回话（计数将 %d/3）" % streak)
        used = int(st["day_n"] or 0) if st["day"] == day else 0
        limit = int(pro.get("per_chat_daily", 3) or 0)
        if limit > 0 and used >= limit:
            waits.append("今日配额已满（%d/%d，明天恢复）" % (used, limit))
        verdict = "❌ 不会发" if forever else ("⏳ 会等" if waits else "✅ 下一个 tick（≤15s）就会发")
        print("\n  %s" % chat)
        print("    next_at=%s  %s" % (clock(st["next_at"]), ago(st["next_at"], now)))
        print("    今日已发 %d/%s  连续无回应 %d  上次主动 %s"
              % (used, limit or "不限", streak, clock(st["last_self_ts"])))
        print("    结论：%s" % verdict)
        for item in forever:
            print("      · 永久不发：%s" % item)
        for item in waits:
            print("      · 现在不发：%s" % item)
        for item in reasons:
            print("      · 提示：%s" % item)

    print("\n--- 定时提醒 ---")
    active = rows(db, "SELECT COUNT(*) n FROM reminders WHERE status='active'")
    due = rows(db, "SELECT * FROM reminders WHERE status='active' AND next_ts<=? "
                   "ORDER BY next_ts LIMIT 5", (int(now),))
    missed = rows(db, "SELECT COUNT(*) n FROM reminder_occurrences WHERE status='missed'")
    print("  active 提醒:", active[0]["n"] if active else 0,
          " 已判定错过:", missed[0]["n"] if missed else 0,
          " 开关:", rem_cfg.get("enabled"))
    for r in due:
        grace = int(rem_cfg.get("grace_seconds", 7200))
        if r["short_delay"]:
            grace = int(rem_cfg.get("short_grace_seconds", 60))
        late = now - float(r["next_ts"])
        state = "超过 %ds 宽限 → 记 missed，不补发" % grace if late > grace else \
                "在宽限内 → 会补发（迟到 %d 分钟）" % (late // 60)
        print("  · #%s [%s] 计划 %s，%s" % (r["id"], r["chat"], clock(r["next_ts"]), state))
    if not due:
        print("  （当前没有到点的提醒）")

    print("\n--- 钩子（主动聊天的话题素材）---")
    hooks = rows(db, "SELECT status, COUNT(*) n, MIN(expires_ts) mint, MAX(expires_ts) maxt "
                     "FROM hooks GROUP BY status")
    if not hooks:
        print("  （没有钩子）")
    for h in hooks:
        extra = ""
        if h["status"] == "open":
            live = rows(db, "SELECT COUNT(*) n FROM hooks WHERE status='open' AND expires_ts>?",
                        (int(now),))[0]["n"]
            extra = " 其中未过期 %d 条（过期钩子不会发，会退化成普通解闷）" % live
        print("  %-8s %d 条 %s" % (h["status"], h["n"], extra))
    return 0


if __name__ == "__main__":
    sys.exit(main())
