# -*- coding: utf-8 -*-
"""风格样例库（人设四层里的第三层）。

保存「匿名场景 → 原草稿 → 我认可的版本 → 为什么」，生成时只挑少量相关样例注入。
模型自己写的回复不会被自动当样例——必须 approved=true。

用法：
    python style_samples.py list
    python style_samples.py add
    python style_samples.py approve <id>
    python style_samples.py delete <id>
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
FILE = os.path.join(ROOT, "style_samples.json")

SCOPES = ("global", "scene", "contact")


def _load() -> list[dict]:
    if not os.path.exists(FILE):
        return []
    try:
        with open(FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: list[dict]):
    with open(FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def add(scene: str, context: str, draft: str, preferred: str,
        reason: str = "", scope: str = "scene", chat: str = "",
        approved: bool = False) -> dict:
    items = _load()
    rec = {
        "id": uuid.uuid4().hex[:8],
        "scene": scene,
        "context": context,
        "draft": draft,
        "preferred": preferred,
        "reason": reason,
        "scope": scope if scope in SCOPES else "scene",
        "chat": chat,
        "approved": bool(approved),
        "created": int(time.time()),
    }
    items.append(rec)
    _save(items)
    return rec


def approve(sid: str) -> bool:
    items = _load()
    hit = False
    for it in items:
        if it["id"] == sid:
            it["approved"] = True
            hit = True
    if hit:
        _save(items)
    return hit


def delete(sid: str) -> bool:
    items = _load()
    left = [x for x in items if x["id"] != sid]
    if len(left) != len(items):
        _save(left)
        return True
    return False


def pick(hint: str = "", chat: str = "", limit: int = 3) -> list[dict]:
    """挑和当前场景最相关的已认可样例。

    场景写成「关键词|关键词」或空格分隔；以前只做整串子串匹配，导致几乎永远
    命中不到，最后每次都把前 3 条全局样例喂进去——回复于是越来越像模板。
    现在：按关键词打分 + 用 (hint, chat) 做稳定打散，同分不同场景会挑到不同样例。
    """
    items = [x for x in _load() if x.get("approved")]
    if not items:
        return []

    def keywords(scene: str) -> list[str]:
        return [k for k in re.split(r"[|,，/、\s]+", scene or "") if k]

    def score(it: dict) -> int:
        s = 0
        if it.get("scope") == "contact" and chat and it.get("chat") == chat:
            s += 100
        scene = it.get("scene") or ""
        if hint and scene and scene in hint:
            s += 30
        if hint:
            for kw in keywords(scene):
                if kw and kw in hint:
                    s += 8
        if it.get("scope") == "global":
            s += 1
        return s

    seed = hashlib.md5(("%s|%s" % (hint, chat)).encode("utf-8")).hexdigest()

    def tie_key(it: dict) -> str:
        return hashlib.md5((seed + str(it.get("id"))).encode("utf-8")).hexdigest()

    items.sort(key=lambda it: (-score(it), tie_key(it)))
    return items[:max(1, int(limit))]


def build_prompt(hint: str = "", chat: str = "", limit: int = 3) -> str:
    got = pick(hint, chat, limit)
    if not got:
        return ""
    lines = ["【以前类似场景的语感参考：学语气和节奏，不要照抄原句，也别重复同一个开头】"]
    for it in got:
        lines.append("场景：%s" % it.get("scene"))
        if it.get("context"):
            lines.append("  对方说：%s" % it["context"])
        lines.append("  可以这样回：%s" % it.get("preferred"))
        if it.get("reason"):
            lines.append("  （因为：%s）" % it["reason"])
    return "\n".join(lines)


def main():
    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    action = sys.argv[1] if len(sys.argv) > 1 else "list"
    if action == "list":
        items = _load()
        print("共 %d 条，已认可 %d 条" % (len(items), sum(1 for x in items if x.get("approved"))))
        for it in items:
            print("  [%s]%s %s | %s -> %s" % (
                it["id"], "✓" if it.get("approved") else " ",
                it.get("scene"), it.get("draft")[:20], it.get("preferred")[:20]))
    elif action == "add":
        print("依次输入（直接回车跳过可选）")
        scene = input("场景（如：熟人吐槽加班）: ").strip()
        context = input("对方原话: ").strip()
        draft = input("模型原草稿: ").strip()
        preferred = input("你想改成: ").strip()
        reason = input("为什么（可空）: ").strip()
        rec = add(scene, context, draft, preferred, reason, approved=True)
        print("已保存:", rec["id"])
    elif action == "approve":
        if len(sys.argv) > 2:
            print("已认可" if approve(sys.argv[2]) else "没找到")
    elif action == "delete":
        if len(sys.argv) > 2:
            print("已删除" if delete(sys.argv[2]) else "没找到")
    else:
        print("用法: list | add | approve <id> | delete <id>")


if __name__ == "__main__":
    main()
