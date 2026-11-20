# -*- coding: utf-8 -*-
"""表情包库：目录 + manifest 标签管理。

目录结构
--------
stickers/
├─ manifest.json        {文件名: {tags: [...], added: ts}}
├─ 开心/                （也可直接平铺）
└─ *.gif / *.png / *.jpg

标签约定（可自由扩展）
--------------------
开心 得意 无语 震惊 生气 疲惫 卖萌 敷衍 疑问 赞同 拒绝 晚安

用法
----
    python sticker_lib.py scan            扫描目录，补齐 manifest
    python sticker_lib.py list [tag]      列出（可按标签过滤）
    python sticker_lib.py pick 开心       随机取一张开心的
    python sticker_lib.py tag 文件名 开心,得意
"""

from __future__ import annotations

import json
import os
import random
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(BASE, "manifest.json")
EXTS = (".gif", ".png", ".jpg", ".jpeg", ".webp")

DEFAULT_TAGS = ["开心", "得意", "无语", "震惊", "生气", "疲惫",
                "卖萌", "敷衍", "疑问", "赞同", "拒绝", "晚安"]


def _load() -> dict:
    if not os.path.exists(MANIFEST):
        return {}
    try:
        with open(MANIFEST, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def scan() -> dict:
    """扫目录，把新文件补进 manifest。"""
    data = _load()
    added = 0
    for root, _, files in os.walk(BASE):
        for fn in files:
            if not fn.lower().endswith(EXTS):
                continue
            if fn == "manifest.json":
                continue
            rel = os.path.relpath(os.path.join(root, fn), BASE).replace("\\", "/")
            if rel in data:
                continue
            tags = []
            # 父目录名若在标签表里，自动打上
            parent = os.path.basename(os.path.dirname(rel))
            if parent in DEFAULT_TAGS:
                tags.append(parent)
            data[rel] = {"tags": tags, "added": int(time.time())}
            added += 1
    if added:
        _save(data)
    return {"ok": True, "total": len(data), "added": added}


def list_stickers(tag: str | None = None) -> list[dict]:
    data = _load()
    out = []
    for name, meta in data.items():
        path = os.path.join(BASE, name.replace("/", os.sep))
        if not os.path.exists(path):
            continue
        tags = meta.get("tags") or []
        if tag and tag not in tags:
            continue
        out.append({"name": name, "path": path, "tags": tags})
    return out


def pick(mood: str, n: int = 1) -> list[str]:
    cands = list_stickers(mood)
    if not cands:
        cands = list_stickers()
    if not cands:
        return []
    random.shuffle(cands)
    return [c["path"] for c in cands[:n]]


def tag(name: str, tags: list[str]):
    data = _load()
    if name not in data:
        data[name] = {"tags": [], "added": int(time.time())}
    data[name]["tags"] = sorted(set(tags))
    _save(data)
    return data[name]


def main():
    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    os.makedirs(BASE, exist_ok=True)
    action = sys.argv[1] if len(sys.argv) > 1 else "scan"

    if action == "scan":
        print(scan())
    elif action == "list":
        tagf = sys.argv[2] if len(sys.argv) > 2 else None
        items = list_stickers(tagf)
        print("共 %d 张%s" % (len(items), ("（标签 %s）" % tagf) if tagf else ""))
        for it in items[:50]:
            print("  %-30s %s" % (it["name"], ",".join(it["tags"]) or "-"))
    elif action == "pick":
        mood = sys.argv[2] if len(sys.argv) > 2 else "开心"
        print(pick(mood, 1))
    elif action == "tag":
        if len(sys.argv) < 4:
            print("用法: tag <文件名> <标签1,标签2>")
            return
        print(tag(sys.argv[2], [x.strip() for x in sys.argv[3].split(",") if x.strip()]))
    else:
        print("未知动作：", action)


if __name__ == "__main__":
    main()
