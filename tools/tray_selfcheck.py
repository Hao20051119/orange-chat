# -*- coding: utf-8 -*-
"""托盘自检：换一台电脑后先跑这个，确认"微信收在托盘时能不能自动点开"。

用法（在项目目录下，pylibs 已就位）：

    set PYTHONPATH=.;.\\pylibs;.\\wxbot
    python tools\\tray_selfcheck.py            # 只检查，不点任何图标
    python tools\\tray_selfcheck.py --click     # 额外真点一次微信托盘图标，看窗口能否打开

退出码：0=能自动点开，1=找不到微信托盘图标，2=小号微信没在跑/未登录
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "wxbot"))
sys.path.insert(0, os.path.join(ROOT, "pylibs"))

import win32gui  # noqa: E402

import wx_engine as engine  # noqa: E402


def screen_language() -> str:
    try:
        return "0x%04X" % ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except Exception:
        return "?"


def pick_account() -> tuple[str, str] | None:
    account = ""
    cfg_path = os.path.join(ROOT, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            account = (json.load(fh) or {}).get("account") or ""
    except Exception:
        pass
    try:
        from wechatauto.db import list_accounts
        accounts = list_accounts()
    except Exception as exc:
        print("!! 读不到微信账号列表：%s: %s" % (type(exc).__name__, exc))
        return None
    if not accounts:
        print("!! 没有找到任何微信账号数据目录（本机没登录过微信？）")
        return None
    if account:
        for item in accounts:
            if item.get("account") == account:
                return account, item["path"]
        print("!! config.json 里的小号 %r 没在本机登录过，本机账号：%s"
              % (account, [a.get("account") for a in accounts]))
    first = accounts[0]
    print("（未指定小号，用本机唯一/第一个账号：%s）" % first.get("account"))
    return first.get("account"), first["path"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--click", action="store_true", help="真点一次微信托盘图标验证")
    args = parser.parse_args()

    try:
        ver = sys.getwindowsversion()
        build = "Windows %d.%d build %d" % (ver.major, ver.minor, ver.build)
    except Exception:
        build = "?"
    print("=" * 60)
    print("Windows :", build)
    print("语言    :", screen_language())
    print("Python  :", sys.version.split()[0])
    picked = pick_account()
    if picked is None:
        return 2
    account, path = picked
    print("小号    :", account)
    print("数据目录:", path)

    binding = engine.active_account_binding(account, path)
    print("实例绑定:", binding)
    if binding is None:
        print("!! 小号微信没在运行，或打开了多个账号库 —— 先登录并只登录它一个再试")
        return 2
    pid, hwnd = binding

    mgr = engine.WxWindowManager(account, log=lambda m: print("   [log]", m), account_dir=path)
    ok = mgr.connect()
    print("connect :", ok)
    if not ok:
        print("!! 连不上主窗（见上面的 log）")
        return 2

    print("窗口    : visible=%s iconic=%s rect=%s" % (
        win32gui.IsWindowVisible(hwnd), win32gui.IsIconic(hwnd), win32gui.GetWindowRect(hwnd)))
    print("mmui 树 :", mgr._tree_ready())
    print("会话列表:", "可读" if mgr._session_list() is not None else "读不到（大窗口可能不在聊天页）")
    print("聊天页tab:", "找到" if mgr._chat_tab() is not None else "没找到")

    tray = mgr._tray_icon_buttons()
    names = []
    for button in tray:
        try:
            names.append((button.Name or "").replace("\n", " ")[:32])
        except Exception:
            pass
    print("\n常显区托盘按钮 %d 个：%s" % (len(names), "、".join(names[:14])))
    hits = [b for b in tray if engine._looks_like_wechat(getattr(b, "Name", ""))]
    where = "常显区"
    if not hits:
        flyout = mgr._overflow_flyout()
        print("溢出面板 :", "打开失败" if flyout is None else "已打开")
        if flyout is not None:
            found, panel_names = [], []
            stack = [flyout]
            while stack:
                node = stack.pop()
                try:
                    name = node.Name or ""
                    kind = node.ControlTypeName
                except Exception:
                    continue
                if name and kind in ("ButtonControl", "ListItemControl", "MenuItemControl"):
                    panel_names.append(name.replace("\n", " ")[:32])
                    if engine._looks_like_wechat(name):
                        found.append(node)
                try:
                    stack.extend(node.GetChildren())
                except Exception:
                    pass
            print("溢出面板按钮:", "、".join(panel_names[:20]))
            hits, where = found, "溢出面板"
            if not args.click:          # 要真点就留着面板（关掉后元素坐标会变成 0）
                mgr._close_overflow()

    print("\n" + "=" * 60)
    if not hits:
        print("❌ 没找到微信的托盘图标 —— 托盘自动打开会失败")
        print("   排查：① 微信设置里是否关掉了托盘图标；② 任务栏是否被其他程序替换；")
        print("         ③ Windows 7/10 的托盘结构与 11 不同，需要按 output 里的按钮名适配。")
        mgr._close_overflow()
        return 1
    print("✅ 找到微信托盘图标（%s，%d 个）—— 托盘状态可以自动点开" % (where, len(hits)))
    if not args.click:
        print("   （加 --click 可以真点一次，验证窗口是否被微信自己打开）")
        return 0
    import time
    for attempt in range(2):
        try:
            hits[0].Click()
        except Exception as exc:
            print("!! 点击失败（第 %d 次）：%s: %s" % (attempt + 1, type(exc).__name__, exc))
            if attempt == 0:
                hits = mgr._wechat_tray_buttons() or []      # 面板可能被关掉了，重新取一次
                if not hits:
                    break
                continue
            mgr._close_overflow()
            return 1
        for _ in range(12):
            time.sleep(0.5)
            if win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd):
                print("✅ 点击后主窗已显示：rect=%s" % (win32gui.GetWindowRect(hwnd),))
                mgr._close_overflow()
                return 0
        break
    mgr._close_overflow()
    print("❌ 点了图标但主窗仍是隐藏/最小化")
    return 1


if __name__ == "__main__":
    sys.exit(main())
