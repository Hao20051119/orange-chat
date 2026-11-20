r"""「消息收到了但没回复、概览/日志也空」——一键定位（只读，不改任何东西）。

在一台新电脑上跑：
    set PYTHONPATH=.;.\pylibs;.\wxbot
    python tools\why_no_reply.py

它会告诉你三件事：
  1. 回复进程（reply_worker）到底有没有在跑；
  2. GUI 那次会话的 process.log / reply.log 里写了什么；
  3. 队列里有没有"进队却没生成草稿"的消息，以及 API Key / 自动回复开关状态。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pylibs"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "wxbot"))


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def tail(path: Path, lines: int) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            data = fh.readlines()
        return [line.rstrip("\n") for line in data[-lines:]]
    except OSError:
        return []


def read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def processes() -> list[tuple[int, str]]:
    try:
        import psutil
    except Exception:
        return []
    out = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not name.startswith("python"):
                continue
            cmd = " ".join(proc.info.get("cmdline") or [])
            if "reply_worker.py" in cmd or "wx_service.py" in cmd or "app.py" in cmd:
                started = time.strftime("%H:%M:%S", time.localtime(proc.info.get("create_time") or 0))
                if "reply_worker.py" in cmd:
                    role = "回复进程 reply_worker"
                elif "wx_service.py" in cmd:
                    role = "发送服务 wx_service"
                else:
                    role = "GUI 界面"
                out.append((proc.info["pid"], "%s（pid=%s，启动于 %s）" % (role, proc.info["pid"], started)))
        except Exception:
            continue
    return out


def queue_counts(db: Path) -> dict:
    result = {"ok": False, "error": ""}
    if not db.is_file():
        result["error"] = "queue.db 不存在"
        return result
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=2)
        try:
            cur = conn.cursor()
            result["inbound"] = dict(cur.execute(
                "SELECT status, COUNT(*) FROM inbound GROUP BY status").fetchall())
            result["outbound"] = dict(cur.execute(
                "SELECT status, COUNT(*) FROM outbound GROUP BY status").fetchall())
            row = cur.execute("SELECT MAX(msg_ts) FROM inbound").fetchone()
            result["last_inbound"] = row[0] if row else None
            row = cur.execute("SELECT COUNT(*) FROM pause").fetchone()
            result["paused"] = row[0] if row else 0
            result["ok"] = True
        finally:
            conn.close()
    except Exception as exc:
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
    return result


def key_status() -> dict:
    try:
        import apikeys
        return {"path": str(apikeys.path()), "exists": apikeys.path().is_file(),
                "items": len(apikeys.items()), "default": bool(apikeys.default_key())}
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def main() -> int:
    print("=" * 66)
    print("项目目录 :", ROOT)
    print("Python   :", sys.version.split()[0])
    print("时间     :", time.strftime("%Y-%m-%d %H:%M:%S"))

    root_cfg = read_json(ROOT / "config.json")
    agent = root_cfg.get("agent") or {}
    print("\n--- 1. 进程 ---")
    procs = processes()
    if not procs:
        print("!! 没有找到 reply_worker.py / wx_service.py 进程")
    for pid, text in procs:
        print("  ", text)

    keys = key_status()
    print("\n--- 2. 模型 API Key（换机器最容易漏的东西）---")
    print("  Key 记录库:", keys.get("path"), "存在" if keys.get("exists") else "不存在")
    print("  已保存条数:", keys.get("items"), "  默认 Key 可用:",
          keys.get("default") if "error" not in keys else keys["error"])

    print("\n--- 3. 根目录 config.json ---")
    print("  agent.auto_reply:", agent.get("auto_reply"),
          " 黑名单人数:", len(agent.get("reply_blacklist") or []),
          " 群聊:", agent.get("group_reply"), agent.get("group_mode"))

    print("\n--- 4. GUI 会话目录（GUI\\.runtime\\<hash>）---")
    runtime = ROOT / "GUI" / ".runtime"
    sessions = []
    if runtime.is_dir():
        sessions = sorted([p for p in runtime.iterdir() if p.is_dir()],
                          key=lambda p: p.stat().st_mtime, reverse=True)[:2]
    if not sessions:
        print("!! 一个会话目录都没有：GUI 从没成功「连接并启动」过")
    for folder in sessions:
        print("\n  会话:", rel(folder), "  最后修改:",
              time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(folder.stat().st_mtime)))
        ready = folder / "worker.ready"
        print("    worker.ready:", "有（worker 曾成功启动）" if ready.is_file() else "没有（worker 没起来过）")
        cfg = read_json(folder / "config.json")
        sag = (cfg.get("agent") or {})
        print("    会话配置: auto_reply=%s 群聊=%s 黑名单=%d 模型=%s" % (
            sag.get("auto_reply"), sag.get("group_reply"),
            len(sag.get("reply_blacklist") or []), (cfg.get("llm") or {}).get("model")))
        for name in ("process.log", "reply.log"):
            path = folder / name
            size = path.stat().st_size if path.is_file() else -1
            print("    %-12s %s" % (name, "不存在" if size < 0 else "%d 字节" % size))
            for line in tail(path, 12 if name == "process.log" else 10):
                print("       |", line[:160])
        counts = queue_counts(folder / "queue.db")
        if counts.get("ok"):
            print("    队列: 入站=%s  出站=%s  暂停会话=%s" % (
                counts.get("inbound"), counts.get("outbound"), counts.get("paused")))
            if counts.get("last_inbound"):
                print("    最近入站:", time.strftime("%Y-%m-%d %H:%M:%S",
                                                 time.localtime(counts["last_inbound"] / 1000
                                                                if counts["last_inbound"] > 1e11
                                                                else counts["last_inbound"])))
        else:
            print("    队列: 读不到（%s）" % counts.get("error"))

    print("\n" + "=" * 66)
    print("结论 / 按顺序修：")
    hints = []
    if not procs:
        hints.append("① 后台没起来：GUI 里重新「连接并启动」。若弹窗报错，去看 process.log（上面已打印）。")
    if not keys.get("default"):
        hints.append("② 没有可用 API Key：GUI 设置页填一次 Key。Key 存在 %USERPROFILE%\\.orange\\api_keys.json，"
                     "不在项目文件夹里，所以换机器后必须重填。")
    if agent.get("auto_reply") is False or any(
            (read_json(f / "config.json").get("agent") or {}).get("auto_reply") is False for f in sessions):
        hints.append("③ 自动回复是关的：GUI 概览页点「开启自动回复」（新会话默认关闭）。")
    for folder in sessions:
        plog = tail(folder / "process.log", 40)
        if any(("Traceback" in line or "Error" in line) for line in plog):
            hints.append("④ process.log 里有报错（多半是换机器后缺依赖）：先跑一遍「安装.bat」。")
            break
    for folder in sessions:
        counts = queue_counts(folder / "queue.db")
        if counts.get("ok"):
            pending = (counts.get("inbound") or {}).get("pending", 0)
            drafts = sum(v for k, v in (counts.get("outbound") or {}).items()
                         if k in ("draft", "hold"))
            if pending and not drafts:
                hints.append("⑤ 消息进队了但没生成草稿：reply.log 里搜「生成失败 / 跳过 / 过期」，"
                             "通常是模型 Key 无效或消息已超过合并窗口（merge）过期。")
            break
    if not hints:
        hints.append("没有发现明显断点：把上面「会话」那一段完整贴回来，我按日志定位。")
    for line in hints:
        print(" ", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
