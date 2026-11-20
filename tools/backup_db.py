# -*- coding: utf-8 -*-
"""备份：两个 SQLite 库（VACUUM INTO）+ 配置与人设文件。

用法：
    python tools\\backup_db.py            # 备份到 backup/<时间>/
    python tools\\backup_db.py --list     # 只列已有备份
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP = os.path.join(ROOT, "backup")
FILES = [
    (os.path.join(ROOT, "agent", "queue.db"), "queue.db"),
    (os.path.join(ROOT, "memory", "memory.db"), "memory.db"),
]
COPY = ["config.json", "persona_card.json", "style_samples.json"]


def snapshot(folder: str) -> list[str]:
    out = []
    os.makedirs(folder, exist_ok=True)
    for src, name in FILES:
        if not os.path.exists(src):
            continue
        dst = os.path.join(folder, name)
        try:
            with sqlite3.connect(src) as c:
                c.execute("VACUUM INTO ?", (dst,))
            out.append("%s (%d 字节)" % (name, os.path.getsize(dst)))
        except sqlite3.Error as e:
            out.append("%s 备份失败：%s" % (name, e))
    for name in COPY:
        src = os.path.join(ROOT, name)
        if os.path.exists(src):
            try:
                shutil.copy2(src, os.path.join(folder, name))
                out.append(name)
            except OSError as e:
                out.append("%s 复制失败：%s" % (name, e))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--keep", type=int, default=20, help="只保留最近 N 份备份")
    args = ap.parse_args()

    if args.list:
        if not os.path.isdir(BACKUP):
            print("还没有备份")
            return 0
        for name in sorted(os.listdir(BACKUP), reverse=True):
            p = os.path.join(BACKUP, name)
            size = sum(os.path.getsize(os.path.join(p, f))
                       for f in os.listdir(p) if os.path.isfile(os.path.join(p, f)))
            print("  %s  %.1f KB" % (name, size / 1024.0))
        return 0

    folder = os.path.join(BACKUP, time.strftime("%Y%m%d-%H%M%S"))
    print("备份到：%s" % folder)
    for line in snapshot(folder):
        print("  ✅ " + line)

    olds = sorted(os.listdir(BACKUP), reverse=True)
    for name in olds[args.keep:]:
        shutil.rmtree(os.path.join(BACKUP, name), ignore_errors=True)
        print("  🗑 清理旧备份 %s" % name)
    return 0


if __name__ == "__main__":
    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
