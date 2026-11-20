# -*- coding: utf-8 -*-
"""本机 API Key 记录库：填过的 Key 记下来、可选用、可删除，但不进仓库。

为什么要单独一个文件
--------------------
原来 ``config.json`` 里存着明文 ``llm.api_key``，整个项目拷到另一台电脑时密钥
会跟着走。现在改成：

* ``config.json`` 的 ``llm.api_key`` 留空（也可以是旧文件残留，仅作最后兜底）；
* 填过的 Key 记在本文件的记录里，首次在新电脑上必须自己填；
* 记录只在**这台机器**上，不随 ``migrate_device.py`` 的迁移包走。

读取顺序（``config.py`` 与 GUI 都按这个来）
------------------------------------------
    环境变量 ``DSHWX_API_KEY``  >  本库中匹配 API 地址的记录  >  ``config.json`` 的 llm.api_key
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = "WXBOT_API_KEYS"
# 放在用户目录、不放项目目录：这样拷项目文件夹或打迁移包都不会把 Key 带走，
# 新机器第一次必然要自己填。可用环境变量 WXBOT_API_KEYS 指到别处（测试用）。
DEFAULT_PATH = Path.home() / ".orange" / "api_keys.json"

# GUI 里输入的是完整 Key，展示时只露头尾
MASK_HEAD = 5
MASK_TAIL = 4


def path() -> Path:
    return Path(os.environ.get(ENV_PATH) or DEFAULT_PATH)


def mask(key: str) -> str:
    key = key or ""
    if len(key) > MASK_HEAD + MASK_TAIL + 3:
        return key[:MASK_HEAD] + "…" + key[-MASK_TAIL:]
    return "****" if key else ""


def _new_id(key: str) -> str:
    """按 Key 内容生成稳定 id：同一个 Key 不会重复入库。"""
    return "k" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]


def load() -> dict:
    try:
        with path().open(encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"version": 1, "default": "", "items": []}
    if not isinstance(data, dict):
        return {"version": 1, "default": "", "items": []}
    items = [item for item in (data.get("items") or []) if isinstance(item, dict)
             and item.get("key")]
    return {"version": 1, "default": str(data.get("default") or ""), "items": items}


def _save(data: dict) -> bool:
    target = path()
    tmp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, target)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def items() -> list[dict]:
    """全部记录，默认那条排最前。返回的 dict 已去掉明文 Key。"""
    data = load()
    out = []
    for item in data["items"]:
        out.append({
            "id": item.get("id") or _new_id(item["key"]),
            "label": item.get("label") or "",
            "base_url": item.get("base_url") or "",
            "model": item.get("model") or "",
            "masked": mask(item["key"]),
            "default": (item.get("id") or _new_id(item["key"])) == data["default"],
            "last_used": int(item.get("last_used") or 0),
        })
    out.sort(key=lambda row: (not row["default"], -row["last_used"]))
    return out


def secret(id_: str) -> str:
    """取某条记录的完整 Key（只给运行时用，不要塞进界面文本）。"""
    for item in load()["items"]:
        if (item.get("id") or _new_id(item["key"])) == id_:
            return str(item["key"])
    return ""


def _endpoint(url):
    if not isinstance(url, str):
        return None
    url = url.strip()
    if "?" in url or "#" in url or any(c.isspace() or ord(c) < 32 for c in url):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    default_ports = {"http": 80, "https": 443}
    if (parsed.scheme not in default_ports or not parsed.hostname
            or parsed.username is not None or port == 0):
        return None
    path = parsed.path.rstrip("/").removesuffix("/chat/completions").rstrip("/")
    return parsed.scheme, parsed.hostname, port if port is not None else default_ports[parsed.scheme], path


def key_for_url(base_url: str) -> str:
    """仅选用相同 API 地址的 Key，优先默认记录，其次最近使用的记录。"""
    endpoint = _endpoint(base_url)
    if endpoint is None:
        return ""
    data = load()
    matching = [item for item in data["items"] if _endpoint(item.get("base_url")) == endpoint]
    if not matching:
        return ""
    for item in matching:
        if (item.get("id") or _new_id(item["key"])) == data["default"]:
            return str(item["key"])
    latest = max(matching, key=lambda row: (int(row.get("last_used") or 0),
                                            int(row.get("created") or 0)))
    return str(latest["key"])


def add(label: str, base_url: str, model: str, key: str,
        *, make_default: bool = True) -> dict | None:
    """记一条 Key。同一个 Key 重复填只更新标签/地址，不会新增第二条。"""
    key = (key or "").strip()
    if not key:
        return None
    data = load()
    id_ = _new_id(key)
    now = int(time.time())
    for item in data["items"]:
        if (item.get("id") or _new_id(item["key"])) == id_:
            item.update(label=label or item.get("label") or "",
                        base_url=base_url or item.get("base_url") or "",
                        model=model or item.get("model") or "",
                        last_used=now)
            break
    else:
        data["items"].append({
            "id": id_, "label": label or "", "base_url": base_url or "",
            "model": model or "", "key": key, "created": now, "last_used": now,
        })
    if make_default or not data["default"]:
        data["default"] = id_
    _save(data)
    return {"id": id_, "masked": mask(key)}


def select(id_: str) -> bool:
    data = load()
    for item in data["items"]:
        if (item.get("id") or _new_id(item["key"])) == id_:
            data["default"] = id_
            item["last_used"] = int(time.time())
            return _save(data)
    return False


def remove(id_: str) -> bool:
    data = load()
    kept = [item for item in data["items"]
            if (item.get("id") or _new_id(item["key"])) != id_]
    if len(kept) == len(data["items"]):
        return False
    data["items"] = kept
    if data["default"] == id_:
        data["default"] = (kept[0].get("id") or _new_id(kept[0]["key"])) if kept else ""
    return _save(data)


def for_gui() -> dict:
    """给设置面板用的一整包：记录列表 + 默认那条的完整信息（含明文，供填充输入框）。"""
    data = load()
    default_id = data["default"]
    detail = None
    for item in data["items"]:
        if (item.get("id") or _new_id(item["key"])) == default_id:
            detail = {"id": default_id, "label": item.get("label") or "",
                      "base_url": item.get("base_url") or "",
                      "model": item.get("model") or "", "key": str(item["key"])}
            break
    return {"path": str(path()), "items": items(), "default": detail,
            "default_key_exists": bool(data["items"])}


if __name__ == "__main__":
    print(json.dumps(for_gui(), ensure_ascii=False, indent=2))
