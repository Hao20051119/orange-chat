# -*- coding: utf-8 -*-
"""统一配置加载：校验 + 半写入保护 + 环境变量覆盖。

规则（对应改进清单 P2「配置与维护」）：
* 文件写了一半 / JSON 无效 / 关键字段类型不对 → **保留上一份有效配置**，绝不带着坏配置跑
* `api_key` 的来源优先级：环境变量 `DSHWX_API_KEY` → 本机 Key 记录库中匹配 API 地址的记录
  → `config.json`（兼容旧文件）
* 每次读取带 mtime 缓存，改完立即生效
"""

from __future__ import annotations

import json
import os
import threading
import time
from functools import wraps

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
CONFIG_PATH = os.environ.get("WXBOT_CONFIG") or os.path.join(ROOT, "config.json")

ENV_KEY = "DSHWX_API_KEY"
ENV_TOKEN = "DSHWX_TOKEN"

_config_lock = threading.RLock()
_state = {
    "mtime": 0,
    "config": None,      # 最近一次有效配置
    "degraded": False,   # 当前磁盘上的配置是否不可用
    "error": "",
    "checked_at": 0,
}


def synchronized(func):
    """让缓存读写与服务的整次读改写共享同一把锁。"""
    @wraps(func)
    def locked(*args, **kwargs):
        with _config_lock:
            return func(*args, **kwargs)

    return locked


def _defaults() -> dict:
    return {
        "account": "",
        "port": 8765,
        "token": "",
        "llm": {
            "base_url": "https://api.deepseek.com",
            "api_key": "",
            "model": "deepseek-chat",
            "temperature": 1.0,
            "max_tokens": 800,
            "max_tokens_cap": 1200,
            "retries": 2,
            "daily_budget": 2000,
        },
        "agent": {
            "name": "",
            "persona": "",
            "style": {},
            "auto_reply": False,
            "reply_whitelist": [],
            "reply_blacklist": [],
            "group_reply": False,
            "group_mode": "at_only",
            "group_whitelist": [],
            "at_names": [],                # 留空 = 用本机微信昵称兜底
            "group_at_reply": True,
            "group_followup_minutes": 5,
            "group_analysis": True,
            "group_member_memory": True,
            "comprehend_prepass": True,   # 回之前先花一次小调用读懂（同音错字/漏字）
            "skip_system_accounts": True,
            "system_accounts": [],
            "system_account_prefixes": ["gh_"],
            "llm_decide": True,
            "skip_token": "[[SKIP]]",
            "min_delay": 1,
            "max_delay": 3,
        },
        "bot": {"max_parallel_chats": 2},
        "send": {"bubble": True, "bubble_max_chars": 60, "max_bubbles": 2,
                 "min_bubble_gap": 0.6, "max_bubble_gap": 1.5,
                 "max_draft_age": 300, "verify_seconds": 8, "retry_delay": 45},
        "memory": {"enabled": True, "max_turns": 20, "summary_threshold": 30,
                   "record_skipped": True, "ephemeral_ttl": 1800,
                   "adapt_every": 20, "history_msg_chars": 600,
                   "max_context_chars": 12000},
        "media": {"voice_asr": {"enabled": False, "model_dir": ""}},
        "merge": {"quiet_seconds": 3.5, "max_wait_seconds": 12.0,
                  "quiet_complete_seconds": 2.0, "quiet_incomplete_seconds": 5.0,
                  "quiet_media_seconds": 1.5, "absorb_after_seconds": 30,
                  "draft_merge_seconds": 300,
                  "invalidate_on_new": True, "max_regen": 2,
                  "max_age_seconds": 21600, "backfill_max": 5},
        "takeover": {"pause_minutes": 30, "enabled": False, "auto_resume": True},
        "web": {"enabled": False, "max_queries_per_turn": 2, "max_pages_per_turn": 3,
                "timeout": 8, "deadline_ms": 8000, "daily_budget": 200,
                "extra_hints": []},
        "kb": {
            "enabled": False,
            "db_path": "memory/knowledge.db",
            "owner_wxid": "",             # 主人 wxid：他发的文件默认 public
            "context_chars": 1000,        # 每轮注入上限（字）
            "top_k": 20,
            "inbox": "kb/inbox",
            "ingest_roots": [],
            "max_file_mb": 32,
            "daily_ingest_limit": 20,
            "parse": "local",             # local / remote
            "remote_ocr": {
                "enabled": True,
                "auto_in_private": True,  # 私聊收到图片型文档自动 OCR
                "auto_in_group": False,   # 群聊只标记，手动 --ocr
                "purpose": "text_recognition",
                "max_pages_per_file": 50,
                "daily_pages": 100,
            },
            "auto_ingest_files": True,
            "chunk_size": 500,
            "chunk_overlap": 70,
            "default_expires_days": 30,
            "harvest": {"enabled": False, "cron_hours": 24, "tieba": [],
                        "bili_keywords": [], "max_docs_per_run": 20},
        },
        "contacts": {},
        "stickers": {"enabled": False, "dir": "stickers"},
        "logging": {"keep_days": 14, "max_mb": 5, "chat_content": True},
        # 主动聊天与定时提醒（默认全关，见设计稿 §9）
        "proactive": {
            "enabled": False,
            "whitelist": [],
            "idle_min_minutes": 120,
            "idle_max_minutes": 360,
            "quiet_minutes_after_user": 20,
            "min_gap_after_self": 90,
            "active_hours": [9, 23],
            "per_chat_daily": 3,
            "min_per_chat_daily": 0,
            "global_daily": 0,
            "global_guard": 30,
            "trial_hold_days": 3,
            "min_warmup_days": 7,
            "hooks": {"enabled": True, "ttl_days": 5},
            "reminders": {
                "enabled": False,
                "grace_seconds": 7200,
                "short_grace_seconds": 60,
                "max_catchup_per_chat": 1,
                "ask_at_fire": True,
            },
        },
    }


def _merge_defaults(cfg: dict) -> dict:
    d = _defaults()
    for k, v in (cfg or {}).items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k] = {**d[k], **v}
        else:
            d[k] = v
    return d


def _validate(cfg: dict) -> list[str]:
    """返回问题列表；空列表表示可用。"""
    problems = []
    if not isinstance(cfg, dict):
        return ["根节点不是对象"]

    acc = cfg.get("account")
    if acc is not None and not isinstance(acc, str):
        problems.append("account 必须是字符串")

    llm = cfg.get("llm")
    if llm is not None:
        if not isinstance(llm, dict):
            problems.append("llm 必须是对象")
        else:
            for k in ("temperature", "max_tokens", "max_tokens_cap",
                      "retries", "daily_budget"):
                if k in llm and not isinstance(llm[k], (int, float)):
                    problems.append("llm.%s 必须是数字" % k)
            if isinstance(llm.get("max_tokens"), (int, float)) and llm["max_tokens"] <= 0:
                problems.append("llm.max_tokens 必须大于 0")

    agent = cfg.get("agent")
    if agent is not None:
        if not isinstance(agent, dict):
            problems.append("agent 必须是对象")
        else:
            for k in ("reply_whitelist", "reply_blacklist"):
                if k in agent and not isinstance(agent[k], list):
                    problems.append("agent.%s 必须是数组" % k)
            if "auto_reply" in agent and not isinstance(agent["auto_reply"], bool):
                problems.append("agent.auto_reply 必须是布尔")
            for k in ("min_delay", "max_delay"):
                if k in agent and not isinstance(agent[k], (int, float)):
                    problems.append("agent.%s 必须是数字" % k)

    for key in ("bot", "send", "memory", "merge", "takeover", "web", "logging",
                "media", "stickers", "kb"):
        v = cfg.get(key)
        if v is None:
            continue
        if not isinstance(v, dict):
            problems.append("%s 必须是对象" % key)
    for k, lo in (("max_parallel_chats", 1), ("bubble_max_chars", 8),
                  ("max_bubbles", 1), ("max_draft_age", 0)):
        v = (cfg.get("send") or {}).get(k)
        if v is not None:
            if not isinstance(v, (int, float)):
                problems.append("send.%s 必须是数字" % k)
            elif v < lo:
                problems.append("send.%s 不能小于 %s" % (k, lo))
    mp = (cfg.get("bot") or {}).get("max_parallel_chats")
    if mp is not None and (not isinstance(mp, (int, float)) or mp < 1):
        problems.append("bot.max_parallel_chats 必须是不小于 1 的数字")

    contacts = cfg.get("contacts")
    if contacts is not None:
        if not isinstance(contacts, dict):
            problems.append("contacts 必须是对象")
        else:
            for chat, c in contacts.items():
                if not isinstance(c, dict):
                    problems.append("contacts.%s 必须是对象" % chat)
                    continue
                # 主动聊天的人级参数（没写就跟随全局）
                for k in ("proactive_daily", "proactive_idle_min", "proactive_idle_max",
                          "proactive_quiet_minutes", "proactive_gap_after_self"):
                    v = c.get(k)
                    if v is not None and (isinstance(v, bool)
                                          or not isinstance(v, (int, float)) or v < 0):
                        problems.append("contacts.%s.%s 必须是非负数字" % (chat, k))
                ph = c.get("proactive_hours")
                if ph is not None:
                    if (not isinstance(ph, list) or len(ph) != 2
                            or not all(isinstance(x, (int, float))
                                       and not isinstance(x, bool)
                                       and 0 <= x <= 24 for x in ph)):
                        problems.append(
                            "contacts.%s.proactive_hours 必须是 [起始小时, 结束小时]（0~24）"
                            % chat)

    web = cfg.get("web")
    if web is not None and not isinstance(web, dict):
        problems.append("web 必须是对象")

    proactive = cfg.get("proactive")
    if proactive is not None:
        if not isinstance(proactive, dict):
            problems.append("proactive 必须是对象")
        else:
            for k in ("enabled",):
                if k in proactive and not isinstance(proactive[k], bool):
                    problems.append("proactive.%s 必须是布尔" % k)
            if "whitelist" in proactive and not isinstance(proactive["whitelist"], list):
                problems.append("proactive.whitelist 必须是数组")
            for k in ("idle_min_minutes", "idle_max_minutes",
                      "quiet_minutes_after_user", "min_gap_after_self",
                      "per_chat_daily", "min_per_chat_daily", "global_daily",
                      "trial_hold_days", "min_warmup_days"):
                v = proactive.get(k)
                if v is not None and not isinstance(v, (int, float)):
                    problems.append("proactive.%s 必须是数字" % k)
            ah = proactive.get("active_hours")
            if ah is not None:
                if (not isinstance(ah, list) or len(ah) != 2
                        or not all(isinstance(x, (int, float)) for x in ah)
                        or not all(0 <= x <= 24 for x in ah)):
                    problems.append("proactive.active_hours 必须是 [起始小时, 结束小时]（0~24）")
            for k in ("hooks", "reminders"):
                if k in proactive and not isinstance(proactive[k], dict):
                    problems.append("proactive.%s 必须是对象" % k)
            rem = proactive.get("reminders")
            if isinstance(rem, dict):
                if "enabled" in rem and not isinstance(rem["enabled"], bool):
                    problems.append("proactive.reminders.enabled 必须是布尔")
                for k in ("grace_seconds", "short_grace_seconds"):
                    v = rem.get(k)
                    if v is not None and (not isinstance(v, (int, float)) or v < 0):
                        problems.append("proactive.reminders.%s 必须是非负数字" % k)

    kb = cfg.get("kb")
    if isinstance(kb, dict):
        for k, lo, hi in (("context_chars", 300, 2000), ("top_k", 1, 200),
                          ("chunk_size", 200, 1000), ("chunk_overlap", 0, 300),
                          ("max_file_mb", 1, 200)):
            v = kb.get(k)
            if v is None:
                continue
            if not isinstance(v, (int, float)):
                problems.append("kb.%s 必须是数字" % k)
            elif v < lo or v > hi:
                problems.append("kb.%s 必须在 %s~%s 之间" % (k, lo, hi))
        if kb.get("chunk_overlap") is not None and kb.get("chunk_size") is not None:
            try:
                if float(kb["chunk_overlap"]) >= float(kb["chunk_size"]):
                    problems.append("kb.chunk_overlap 必须小于 kb.chunk_size")
            except (TypeError, ValueError):
                pass
        ocr = kb.get("remote_ocr")
        if ocr is not None and not isinstance(ocr, dict):
            problems.append("kb.remote_ocr 必须是对象")

    return problems


@synchronized
def load(force: bool = False) -> dict:
    """读配置；任何异常都回退到上一份有效配置。"""
    try:
        mt = os.path.getmtime(CONFIG_PATH)
    except OSError:
        mt = 0

    if not force and mt == _state["mtime"] and _state["config"] is not None:
        return _state["config"]

    _state["checked_at"] = int(time.time())
    _state["mtime"] = mt

    raw = None
    err = ""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except FileNotFoundError:
        err = "config.json 不存在"
    except json.JSONDecodeError as e:
        err = "JSON 解析失败: %s" % e
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)

    if raw is not None:
        problems = _validate(raw)
        if problems:
            err = "配置校验失败: " + "; ".join(problems)

    if err:
        _state["degraded"] = True
        _state["error"] = err
        if _state["config"] is not None:
            return _state["config"]          # 保留上一份有效配置
        cfg = _defaults()                    # 从未成功过：用默认值，但别崩
        _state["config"] = cfg
        return cfg

    cfg = _merge_defaults(raw)
    _apply_env(cfg)

    _state["config"] = cfg
    _state["degraded"] = False
    _state["error"] = ""
    return cfg


def _backup_current(keep: int = 5):
    """改配置前留一份备份，误操作可回退。"""
    if not os.path.exists(CONFIG_PATH):
        return
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = "%s.bak-%s" % (CONFIG_PATH, stamp)
        with open(CONFIG_PATH, "rb") as src, open(dst, "wb") as out:
            out.write(src.read())
        folder = os.path.dirname(CONFIG_PATH)
        baks = sorted(
            [n for n in os.listdir(folder) if n.startswith("config.json.bak-")],
            reverse=True)
        for name in baks[keep:]:
            try:
                os.unlink(os.path.join(folder, name))
            except OSError:
                pass
    except OSError:
        pass


def _local_key(base_url: str) -> str:
    """从本机记录库查找匹配 API 地址的 Key。"""
    try:
        import apikeys
        return apikeys.key_for_url(base_url)
    except Exception:
        return ""


def _apply_env(cfg: dict) -> dict:
    """密钥来源优先级：环境变量 → 本机 Key 记录库 → config.json（旧文件兼容）。"""
    llm = cfg.get("llm") or {}
    key = os.environ.get(ENV_KEY, "").strip() or _local_key(llm.get("base_url"))
    if key:
        llm["api_key"] = key
        cfg["llm"] = llm
    env_token = os.environ.get(ENV_TOKEN, "").strip()
    if env_token:
        cfg["token"] = env_token
    return cfg


@synchronized
def save(cfg: dict) -> bool:
    """原子写：先备份、再写临时文件替换，避免半写入。"""
    tmp = CONFIG_PATH + ".tmp"
    try:
        stored = dict(cfg)
        stored["llm"] = dict(stored.get("llm") or {})
        # UI 更新的是生效配置。密钥永不写盘：运行时的 Key 来自环境变量或本机记录库，
        # 回写会把它变成明文落进 config.json；磁盘上原有的值（旧文件残留）原样保留。
        try:
            with open(CONFIG_PATH, encoding="utf-8-sig") as f:
                previous = json.load(f)
            if not isinstance(previous, dict):
                previous = {}
        except (OSError, ValueError):
            previous = {}
        prior_llm = previous.get("llm")
        stored["llm"]["api_key"] = (prior_llm.get("api_key", "")
                                    if isinstance(prior_llm, dict) else "")
        if os.environ.get(ENV_TOKEN):
            stored["token"] = previous.get("token", "")
        _backup_current()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stored, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        # 写成功的配置本身就是"最近一份有效配置"，直接进缓存：
        # 之后磁盘上的文件若被写坏，load() 能回退到这一份而不是更早的
        _state["config"] = _apply_env(_merge_defaults(stored))
        try:
            _state["mtime"] = os.path.getmtime(CONFIG_PATH)
        except OSError:
            _state["mtime"] = 0
        _state["degraded"] = False
        _state["error"] = ""
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


@synchronized
def health() -> dict:
    """给 /stats 用的配置健康信息（不暴露密钥）。"""
    cfg = load()
    llm = cfg.get("llm") or {}
    key = llm.get("api_key") or ""
    token = cfg.get("token") or ""
    return {
        "degraded": _state["degraded"],
        "error": _state["error"],
        "path": CONFIG_PATH,
        "mtime": _state["mtime"],
        "api_key_source": (("env:" + ENV_KEY) if os.environ.get(ENV_KEY, "").strip()
                           else ("local:api_keys.local.json" if _local_key(llm.get("base_url")) else "file")),
        "api_key_masked": (key[:5] + "..." + key[-4:]) if len(key) > 10 else ("(空)" if not key else "***"),
        "token_source": ("env:" + ENV_TOKEN) if os.environ.get(ENV_TOKEN) else "file",
        "token_set": bool(token),
        "account": cfg.get("account"),
        "web_enabled": (cfg.get("web") or {}).get("enabled"),
        "auto_reply": (cfg.get("agent") or {}).get("auto_reply"),
        "memory_enabled": (cfg.get("memory") or {}).get("enabled"),
    }


if __name__ == "__main__":
    os.system("chcp 65001 >nul 2>&1")
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(json.dumps(health(), ensure_ascii=False, indent=2))
