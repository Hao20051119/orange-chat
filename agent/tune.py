# -*- coding: utf-8 -*-
"""性格调试台：改参数 → 立刻看回复效果，不真的发微信。

用法
----
    python tune.py                       交互模式，输入一句话看回复
    python tune.py "在吗"                 单次
    python tune.py --tone warm "在吗"     临时覆盖风格参数
    python tune.py --persona "..." "在吗"  临时换人设
    python tune.py --show                 只看当前配置

可覆盖参数：--persona --length --tone --emojis --typos --name --temp
"""

from __future__ import annotations

import copy
import json
import os
import sys

os.system("chcp 65001 >nul 0>&1" if False else "chcp 65001 >nul 2>&1")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(ROOT, "memory"))
sys.path.insert(0, os.path.join(ROOT, "stickers"))
sys.path.insert(0, os.path.join(ROOT, "pylibs"))

from brain import Brain
from llm import load_config

TUNE_CHAT = "__tune__"


def parse_overrides(argv):
    """把 --key value 抽出来，剩下的当文本。"""
    ov = {}
    text_parts = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and i + 1 < len(argv):
            key = a[2:]
            val = argv[i + 1]
            if key in ("emojis", "typos", "no_question"):
                ov[key] = val.lower() in ("1", "true", "yes", "on")
            elif key == "temp":
                ov["temperature"] = float(val)
            else:
                ov[key] = val
            i += 2
            continue
        text_parts.append(a)
        i += 1
    return ov, " ".join(text_parts).strip()


def apply_overrides(cfg, ov):
    cfg = copy.deepcopy(cfg)
    agent = cfg.setdefault("agent", {})
    style = agent.setdefault("style", {})
    for k, v in ov.items():
        if k == "persona":
            agent["persona"] = v
        elif k == "name":
            agent["name"] = v
        elif k == "temperature":
            cfg.setdefault("llm", {})["temperature"] = v
        else:
            style[k] = v
    return cfg


def show(cfg):
    a = cfg.get("agent", {})
    s = a.get("style", {})
    print("昵称    :", a.get("name"))
    print("人设    :", a.get("persona"))
    print("长度    :", s.get("length"), " 语气:", s.get("tone"))
    print("emoji   :", s.get("emojis"), " 错别字:", s.get("typos"),
          " 少反问:", s.get("no_question"))
    print("回复率  :", s.get("reply_probability"))
    print("温度    :", cfg.get("llm", {}).get("temperature"))
    print("模型    :", cfg.get("llm", {}).get("model"))


def reply_once(cfg, text):
    brain = Brain(cfg)
    brain.agent["auto_reply"] = True
    brain.agent["reply_whitelist"] = []      # 调试台绕过白名单
    brain.agent["reply_blacklist"] = []
    brain.agent["group_reply"] = True
    r = brain.handle(TUNE_CHAT, text, dry_run=True)
    if r.get("ok"):
        return r.get("reply"), r.get("delay")
    return "[失败] %s" % (r.get("skipped") or r.get("error")), 0


def clear_tune_memory():
    try:
        from store import MemoryStore
        m = MemoryStore()
        import sqlite3
        with sqlite3.connect(m.db_path) as c:
            c.execute("DELETE FROM messages WHERE chat=?", (TUNE_CHAT,))
            c.execute("DELETE FROM profiles WHERE chat=?", (TUNE_CHAT,))
            c.execute("DELETE FROM facts WHERE chat=?", (TUNE_CHAT,))
    except Exception:
        pass


def main():
    argv = sys.argv[1:]
    if "--show" in argv:
        show(load_config())
        return

    ov, text = parse_overrides(argv)
    base = load_config()
    cfg = apply_overrides(base, ov) if ov else base

    if ov:
        print("（临时覆盖: %s）" % ", ".join("%s=%s" % (k, v) for k, v in ov.items()))
    show(cfg)
    print("")

    if text:
        clear_tune_memory()
        r, d = reply_once(cfg, text)
        print("对方: %s" % text)
        print("Orange: %s   (延迟 %.1fs)" % (r, d or 0))
        return

    print("输入消息回车看回复，直接回车退出。")
    print("")
    while True:
        try:
            line = input("对方 > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break
        r, d = reply_once(cfg, line)
        print("Orange > %s   (延迟 %.1fs)" % (r, d or 0))
        print("")


if __name__ == "__main__":
    main()
