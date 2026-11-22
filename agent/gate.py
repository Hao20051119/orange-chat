# -*- coding: utf-8 -*-
"""发送门禁：把「这条到底该不该发」集中到一处，生成前和发送前共用。

返回 ``{"action": "ok"|"hold"|"cancel", "reason": str}``

* ``cancel``：明确不该发（关自动回复、黑名单、不在白名单、群聊未开、草稿过期、人工接管中）
* ``hold``：不自动发但保留给人看（该联系人只要草稿、非回复时段、联系人被暂停）
* ``ok``：可以发

为什么单独一个模块：以前 ``brain.should_reply`` 与 ``reply_worker.sender`` 各写一套，
结果发送时完全绕过了暂停/时段/白名单检查（实测：暂停后 1 秒仍然发出）。
"""

from __future__ import annotations

DEFAULT_MAX_DRAFT_AGE = 300

# 这些会话是系统/服务号，自动回复没有意义（回了对方也不会看，还浪费额度）
DEFAULT_SYSTEM_CHATS = (
    "filehelper", "weixin", "medianote", "fmessage", "floatbottle",
    "newsapp", "notifymessage", "qqmail", "tmessage", "qmessage",
    "weixinliveshare",
)
DEFAULT_SYSTEM_PREFIXES = ("gh_",)


def _agent(cfg: dict) -> dict:
    return (cfg or {}).get("agent", {}) or {}


def is_system_chat(chat: str, agent: dict | None = None) -> bool:
    """文件传输助手、微信团队、公众号等，不自动回。"""
    agent = agent or {}
    if not agent.get("skip_system_accounts", True):
        return False
    chats = set(agent.get("system_accounts") or DEFAULT_SYSTEM_CHATS)
    if chat in chats:
        return True
    prefixes = agent.get("system_account_prefixes") or DEFAULT_SYSTEM_PREFIXES
    return any(chat.startswith(p) for p in prefixes if p)


def is_private_chat(chat: str, agent: dict | None = None) -> bool:
    """是否真人私聊：群聊与系统/服务号都不算。

    与 ``is_system_chat`` 的区别：**不看** ``skip_system_accounts`` 开关。
    主动聊天、提醒候选这类「主动找上门」的功能，不能把文件传输助手、
    公众号当成联系人排期；而微信号是自定义 alias 的联系人（例：wxid_example_alias，
    不是 wxid_ 开头）仍然是私聊——旧代码用 ``startswith("wxid_")`` 判断，
    把这类真人误杀了。
    """
    chat = (chat or "").strip()
    if not chat or chat.endswith("@chatroom"):
        return False
    agent = agent or {}
    chats = set(agent.get("system_accounts") or DEFAULT_SYSTEM_CHATS)
    prefixes = tuple(agent.get("system_account_prefixes") or DEFAULT_SYSTEM_PREFIXES)
    if chat in chats:
        return False
    return not any(p and chat.startswith(p) for p in prefixes)


def contact_cfg(cfg: dict, chat: str) -> dict:
    return (((cfg or {}).get("contacts") or {}).get(chat) or {})


def in_hours(hours, now=None) -> bool:
    """联系人回复时段（支持跨零点，如 [22, 6]）。"""
    import time as _t

    if not isinstance(hours, (list, tuple)) or len(hours) != 2:
        return True
    try:
        lo, hi = int(hours[0]), int(hours[1])
    except (TypeError, ValueError):
        return True
    h = _t.localtime(now).tm_hour
    if lo == hi:
        return True
    if lo < hi:
        return lo <= h < hi
    return h >= lo or h < hi


def identity_ok(cfg: dict, chat: str, from_me: bool = False,
                at_me: bool = False) -> tuple[bool, str]:
    """身份层面的硬规则：自己发的 / 开关 / 黑白名单 / 群聊。

    群聊与私聊分开判断：``reply_whitelist`` 只管私聊，群用 ``group_whitelist``；
    群聊默认 ``at_only``——没被 @ 就不插话。
    """
    if from_me:
        return False, "自己发的"
    agent = _agent(cfg)
    if not agent.get("auto_reply", False):
        return False, "auto_reply 关闭"
    if is_system_chat(chat, agent):
        return False, "系统/公众号账号，不自动回"

    # 黑名单优先于一切：人和群都适用（拉黑即不回，其余默认都回）
    bl = agent.get("reply_blacklist") or []
    if chat in bl:
        return False, "在黑名单"

    is_group = (chat or "").endswith("@chatroom")
    if is_group:
        if not agent.get("group_reply", False):
            return False, "群聊未开启"
        gwl = agent.get("group_whitelist") or []
        if gwl and chat not in gwl:
            return False, "不在群白名单"
        mode = str(agent.get("group_mode") or "at_only")
        if mode == "at_only" and not at_me:
            return False, "群里没@我"
        return True, ""

    wl = agent.get("reply_whitelist") or []
    if wl and chat not in wl:
        return False, "不在白名单"
    return True, ""


def draft_timestamp(task: dict) -> float | None:
    """普通回复重新获批后，从批准时刻计算草稿时效。"""
    if (task.get("kind") or "reply") == "reply" and task.get("approved_ts"):
        return task["approved_ts"]
    return task.get("created_ts")


def evaluate(cfg: dict, chat: str, *, from_me: bool = False,
             paused: bool = False, created_ts: float | None = None,
             now: float | None = None, at_me: bool = False,
             kind: str = "reply", approved: bool = False) -> dict:
    """完整门禁。``paused`` 由调用方从 Queue 查出后传入（避免循环依赖）。

    ``kind`` 决定走哪套规则：

    * ``reply``（默认）—— 普通回复，沿用原有逻辑，旧调用不传即行为不变
    * ``reminder`` —— 定时提醒：受提醒开关管，**不受** ``auto_reply``
      与联系人 ``active_hours`` 管（半夜的提醒就该半夜响）
    * ``proactive`` —— 主动闲聊：同时受 ``auto_reply`` 与 ``proactive.enabled``
      管，并要求白名单 ∧ 该联系人开关
    """
    import time as _t

    now = _t.time() if now is None else float(now)

    if kind == "relay_clarify":
        if not is_private_chat(chat, _agent(cfg)):
            return {"action": "cancel", "reason": "代传话只在真人私聊确认"}
        if not (_proactive_cfg(cfg).get("relay") or {}).get("enabled", True):
            return {"action": "cancel", "reason": "代传话未开启"}
    if kind in ("reminder", "reminder_clarify", "relay_clarify"):
        return evaluate_reminder(cfg, chat, paused=paused, approved=approved)
    if kind == "proactive":
        return evaluate_proactive(cfg, chat, paused=paused, now=now, approved=approved)

    ok, why = identity_ok(cfg, chat, from_me=from_me, at_me=at_me)
    if not ok:
        return {"action": "cancel", "reason": why}

    if paused:
        return {"action": "cancel", "reason": "人工接管/暂停中"}

    c = contact_cfg(cfg, chat)
    if c.get("paused"):
        return {"action": "hold", "reason": "该联系人已暂停（草稿保留）"}
    if not in_hours(c.get("active_hours"), now):
        return {"action": "hold", "reason": "不在该联系人的回复时段"}
    if c.get("draft_only") and not approved:
        return {"action": "hold", "reason": "该联系人只出草稿"}

    if created_ts is not None:
        max_age = float((cfg or {}).get("send", {}).get("max_draft_age",
                                                        DEFAULT_MAX_DRAFT_AGE))
        if max_age > 0 and now - float(created_ts) > max_age:
            return {"action": "cancel", "reason": "草稿已过期（%.0f 秒）" % (
                now - float(created_ts))}
    return {"action": "ok", "reason": ""}


def _proactive_cfg(cfg: dict) -> dict:
    return (cfg or {}).get("proactive") or {}


def _reminders_cfg(cfg: dict) -> dict:
    return _proactive_cfg(cfg).get("reminders") or {}


def reminder_entry_ok(cfg: dict, chat: str) -> tuple[bool, str]:
    """提醒/代传话的入口门禁：只认提醒开关、系统号、黑名单。

    刻意**不看** ``auto_reply``——关自动回复只是不聊天，用户显式委托的提醒
    还得记下来。入口和 ``evaluate_reminder``（发送侧）用同一套标准，两端一致，
    不会出现「收下了却发不出去」。
    """
    agent = _agent(cfg)
    if not _reminders_cfg(cfg).get("enabled", False):
        return False, "提醒功能未开启"
    if is_system_chat(chat, agent):
        return False, "系统/公众号账号，不收提醒委托"
    if chat in (agent.get("reply_blacklist") or []):
        return False, "在黑名单"
    return True, ""


def evaluate_reminder(cfg: dict, chat: str, *, paused: bool = False,
                      approved: bool = False) -> dict:
    """定时提醒的门禁：只受提醒开关、黑名单、人工接管约束。"""
    agent = _agent(cfg)
    if not _reminders_cfg(cfg).get("enabled", False):
        return {"action": "cancel", "reason": "提醒功能未开启"}
    if is_system_chat(chat, agent):
        return {"action": "cancel", "reason": "系统/公众号账号，不发提醒"}
    if chat in (agent.get("reply_blacklist") or []):
        return {"action": "cancel", "reason": "在黑名单"}
    if paused:
        return {"action": "cancel", "reason": "人工接管/暂停中"}
    if contact_cfg(cfg, chat).get("draft_only") and not approved:
        return {"action": "hold", "reason": "该联系人只出草稿"}
    return {"action": "ok", "reason": ""}


def proactive_enabled_for(cfg: dict, chat: str) -> tuple[bool, str]:
    """主动聊天的三道闸：总闸 ∧ 白名单 ∧ 该联系人开关。"""
    if not _agent(cfg).get("auto_reply", False):
        return False, "auto_reply 关闭"
    if not _proactive_cfg(cfg).get("enabled", False):
        return False, "主动聊天未开启"
    if chat not in (_proactive_cfg(cfg).get("whitelist") or []):
        return False, "不在主动聊天白名单"
    if not contact_cfg(cfg, chat).get("proactive"):
        return False, "该联系人的主动聊天开关未开"
    return True, ""


def evaluate_proactive(cfg: dict, chat: str, *, paused: bool = False,
                       now: float | None = None, approved: bool = False) -> dict:
    """主动闲聊的门禁：三层开关 + 时段 + 黑名单/接管（配额与冷却在 initiative 里）。"""
    import time as _t

    now = _t.time() if now is None else float(now)
    ok, why = proactive_enabled_for(cfg, chat)
    if not ok:
        return {"action": "cancel", "reason": why}
    agent = _agent(cfg)
    if is_system_chat(chat, agent):
        return {"action": "cancel", "reason": "系统/公众号账号不主动"}
    if chat in (agent.get("reply_blacklist") or []):
        return {"action": "cancel", "reason": "在黑名单"}
    if (chat or "").endswith("@chatroom"):
        return {"action": "cancel", "reason": "主动聊天只走私聊"}
    if paused:
        return {"action": "cancel", "reason": "人工接管/暂停中"}
    c = contact_cfg(cfg, chat)
    if c.get("paused"):
        return {"action": "cancel", "reason": "该联系人已暂停"}
    if not in_hours(c.get("active_hours"), now):
        return {"action": "cancel", "reason": "不在该联系人的时段"}
    # 主动时段：联系人单独设过 proactive_hours 就用它，否则用全局 active_hours
    own = c.get("proactive_hours")
    if isinstance(own, (list, tuple)) and len(own) == 2:
        if not in_hours(own, now):
            return {"action": "cancel", "reason": "不在该联系人的主动时段"}
    elif not in_hours(_proactive_cfg(cfg).get("active_hours", [9, 23]), now):
        return {"action": "cancel", "reason": "不在主动聊天时段"}
    if c.get("draft_only") and not approved:
        return {"action": "hold", "reason": "该联系人只出草稿"}
    return {"action": "ok", "reason": ""}


def backfill(msgs: list[dict], keep: int = 5, max_age: int = 21600,
             now: float | None = None) -> tuple[list[dict], list[int]]:
    """暂停很久后解禁：只答最新 ``keep`` 条，其余返回 id 列表由调用方标 expired。"""
    import time as _t

    now = _t.time() if now is None else float(now)
    keep = max(1, int(keep or 1))
    expired: list[int] = []
    rows = list(msgs)
    if len(rows) > keep:
        expired.extend(int(m["id"]) for m in rows[:-keep])
        rows = rows[-keep:]
    stale = [m for m in rows if now - int(m.get("msg_ts") or 0) > int(max_age)]
    if stale:
        expired.extend(int(m["id"]) for m in stale)
        rows = [m for m in rows if m not in stale]
    return rows, expired
