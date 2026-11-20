# -*- coding: utf-8 -*-
"""预热：把所有会话的独立聊天窗口一次性开好并挪到屏幕外。

之后无论谁发消息，发送都走纯后台路径（SetValue + PostMessage），
不再需要激活窗口，全屏游戏/视频也不会被打断。
"""

from __future__ import annotations

import os
import sys
import time

os.system("chcp 65001 >nul 2>&1")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, os.path.join(ROOT, "wxbot"))
sys.path.insert(0, os.path.join(ROOT, "pylibs"))

from wechatauto.db import WeChatDB
import win32gui
import win32process
import psutil

import wx_engine
from wx_engine import WxWindowManager, OFFSCREEN_X

# 预热阶段忽略全屏检测（此时用户在场，我们主动开窗）
wx_engine._is_fullscreen_foreground = lambda: False


def load_cfg():
    import json
    with open(os.path.join(ROOT, "config.json"), "r", encoding="utf-8-sig") as f:
        return json.load(f)


def main():
    cfg = load_cfg()
    acc = cfg.get("account")
    db = WeChatDB(account=acc)
    info = db.get_self_info()
    print("账号: %s (%s)" % (info.get("nick_name"), info.get("username")))

    engine = WxWindowManager(acc, log=print)
    if not engine.connect():
        print("绑定失败")
        return

    # 会话列表（跳过群、跳过系统号）
    chats = []
    for s in db.get_sessions(limit=50):
        u = s.get("username") or ""
        if not u or u.endswith("@chatroom"):
            continue
        if u in ("weixin", "brandsessionholder", "brandservicesessionholder",
                 "fmessage", "floatbottle", "medianote", "newsapp"):
            continue
        chats.append((u, (s.get("summary") or "")[:20]))

    print("待预热会话 %d 个" % len(chats))

    ok, fail = 0, 0
    for i, (u, summ) in enumerate(chats, 1):
        # username -> 微信里显示的会话名
        shown = u
        try:
            hits = db.search_contact(u)
            if hits:
                h = hits[0]
                shown = h.get("remark") or h.get("nick_name") or u
        except Exception:
            pass
        try:
            hwnd = engine.open_chat(shown, force=True)
        except Exception as e:
            print("  [%d/%d] %s 异常: %s" % (i, len(chats), shown, e))
            hwnd = None
        if hwnd:
            print("  [%d/%d] %s (%s) -> hwnd=%s" % (i, len(chats), shown, u, hwnd))
            ok += 1
        else:
            print("  [%d/%d] %s (%s) 失败" % (i, len(chats), shown, u))
            fail += 1
        time.sleep(0.4)

    print("")
    print("完成：成功 %d，失败 %d" % (ok, fail))
    # 把所有已开窗口收进屏幕外
    moved = 0
    for chat, hwnd in engine.chat_windows.items():
        try:
            engine._move_offscreen(hwnd)
            moved += 1
        except Exception:
            pass
    # 主窗口也收起来
    try:
        engine._move_offscreen(engine.main_hwnd)
    except Exception:
        pass
    print("已挪到屏幕外 %d 个窗口" % moved)


if __name__ == "__main__":
    main()
