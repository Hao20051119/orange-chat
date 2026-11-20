# -*- coding: utf-8 -*-
"""主动聊天：什么时候开口、说什么、什么时候闭嘴。

分工
----
* ``gate.evaluate_proactive`` —— 硬门禁（开关/白名单/黑名单/时段/接管）
* ``proactive``（本文件）—— 节奏、素材、配额、试发期
* ``brain.compose_proactive`` —— 真的生成那句话
* ``initiative`` —— 每秒 tick 调 ``tick()``

三条铁律（见设计稿 §6）
-----------------------
1. **有由头才开口**：优先用钩子（对方自己说过、还没了结的事）；没钩子才走纯解闷。
2. **别插嘴**：对方刚说完、我方刚说完、还有待回复的入站 → 一律不开口。
3. **宁可不发**：模型说 [[SKIP]]、生成期间对话有变化、当日配额用完 → 直接放弃。
"""

from __future__ import annotations

import json
import random
import time

import gate
from logutil import get_logger, redact
from split import split_reply

log = get_logger()

# 收尾信号：对方这么说就是「今天到此为止」，别再去骚扰
_ENDINGS = ("晚安", "先睡了", "我睡了", "先忙", "明天再说", "晚点聊", "回头聊",
            "先这样", "下了", "去睡了", "睡了睡了")


# ------------------------------------------------------------------ 小工具
def today(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d",
                         time.localtime(time.time() if now is None else now))


def candidates(cfg: dict) -> list[str]:
    """可主动的联系人：白名单 ∧ 该联系人开关（两层都要）。"""
    pro = cfg.get("proactive") or {}
    contacts = cfg.get("contacts") or {}
    return [c for c in (pro.get("whitelist") or [])
            if (contacts.get(c) or {}).get("proactive")]


# 联系人级覆盖：全局键 → contacts[chat] 里的键名（统一 proactive_* 前缀，
# 免得和「回复时段 active_hours」「只出草稿 draft_only」那些字段混在一起）
CONTACT_OVERRIDES = (
    ("per_chat_daily", "proactive_daily"),
    ("idle_min_minutes", "proactive_idle_min"),
    ("idle_max_minutes", "proactive_idle_max"),
    ("quiet_minutes_after_user", "proactive_quiet_minutes"),
    ("min_gap_after_self", "proactive_gap_after_self"),
)

PARAM_KEYS = tuple(name for name, _ in CONTACT_OVERRIDES) + ("active_hours",)


def params(cfg: dict, chat: str) -> dict:
    """主动聊天参数：全局打底 + 该联系人的覆盖（没填的项跟随全局）。

    联系人级参数写在 ``contacts[chat]``（与 ``proactive`` 开关同一个节点），
    这样 GUI 的「修改参数」只改一处，运行时每轮都重新读配置。
    """
    pro = dict((cfg or {}).get("proactive") or {})
    c = (((cfg or {}).get("contacts") or {}).get((chat or "").strip()) or {})
    if not isinstance(c, dict):
        return pro
    for name, key in CONTACT_OVERRIDES:
        value = c.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            pro[name] = value
    hours = c.get("proactive_hours")
    if isinstance(hours, (list, tuple)) and len(hours) == 2:
        pro["active_hours"] = [hours[0], hours[1]]
    return pro


def overrides(cfg: dict, chat: str) -> dict:
    """该联系人显式设过的参数（空 dict = 全部跟随全局），给 GUI 回填用。"""
    c = (((cfg or {}).get("contacts") or {}).get((chat or "").strip()) or {})
    if not isinstance(c, dict):
        return {}
    out = {}
    for name, key in CONTACT_OVERRIDES:
        value = c.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[name] = int(value)
    hours = c.get("proactive_hours")
    if isinstance(hours, (list, tuple)) and len(hours) == 2:
        out["active_hours"] = [int(hours[0]), int(hours[1])]
    return out


# 人级参数的取值范围（GUI 和后端共用这一份）
PARAM_LIMITS = {
    "per_chat_daily": (0, 20),             # 0 = 不限
    "idle_min_minutes": (1, 1440),
    "idle_max_minutes": (1, 10080),
    "quiet_minutes_after_user": (0, 1440),
    "min_gap_after_self": (0, 1440),
}

PARAM_LABELS = {
    "per_chat_daily": "每天上限",
    "idle_min_minutes": "最小间隔",
    "idle_max_minutes": "最大间隔",
    "quiet_minutes_after_user": "对方说完后静默",
    "min_gap_after_self": "自己说完后间隔",
    "active_hours": "主动时段",
}


def normalize_patch(patch) -> tuple[dict, str]:
    """把 GUI 交上来的参数收敛成 {参数名: 数字|None}；返回 (值, 错误说明)。

    ``None`` / 空串 / 空数组表示「这一项恢复跟随全局」；没出现的键保持不动。
    """
    if not isinstance(patch, dict):
        return {}, "参数必须是对象"
    out: dict = {}
    for name, (lo, hi) in PARAM_LIMITS.items():
        if name not in patch:
            continue
        value = patch.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            out[name] = None
            continue
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return {}, "%s 必须是数字" % PARAM_LABELS.get(name, name)
        if not lo <= number <= hi:
            return {}, "%s 必须在 %d~%d 之间" % (PARAM_LABELS.get(name, name), lo, hi)
        out[name] = number
    if "active_hours" in patch:
        value = patch.get("active_hours")
        if value is None or value == "" or value == []:
            out["active_hours"] = None
        elif not isinstance(value, (list, tuple)) or len(value) != 2:
            return {}, "主动时段要填「起始小时」和「结束小时」两个数"
        else:
            try:
                hours = [int(float(x)) for x in value]
            except (TypeError, ValueError):
                return {}, "主动时段必须是整点数字"
            if not all(0 <= x <= 24 for x in hours):
                return {}, "主动时段必须落在 0~24 点之间"
            out["active_hours"] = hours
    return out, ""


def apply_patch(node: dict, values: dict) -> dict:
    """把 normalize_patch 的结果写进 contacts[chat] 节点（返回新节点）。

    全局键名 → ``proactive_*`` 字段名的映射只在这里做一次，避免调用方写错。
    ``None`` 表示删掉该项、恢复跟随全局。
    """
    node = dict(node or {})
    inverse = {name: key for name, key in CONTACT_OVERRIDES}
    for name, value in (values or {}).items():
        if name == "active_hours":
            if value is None:
                node.pop("proactive_hours", None)
            else:
                node["proactive_hours"] = [int(value[0]), int(value[1])]
            continue
        key = inverse.get(name)
        if not key:
            continue
        if value is None:
            node.pop(key, None)
        else:
            node[key] = int(value)
    return node


def plan_next(now: float, pro: dict) -> int:
    """下一次允许开口的时刻：随机间隔 + 落进时段窗口。

    注意单位：配置里的 ``idle_*_minutes`` 是**分钟**，先比大小再统一乘 60。
    （曾经写成 ``max(lo_seconds, minutes) * 60``，默认配置下会把下次开口
    推到 5 天后。）
    """
    lo_min = max(1.0, float(pro.get("idle_min_minutes", 120)))
    hi_min = max(lo_min, float(pro.get("idle_max_minutes", 360)))
    ts = now + random.uniform(lo_min, hi_min) * 60.0
    return _align_hours(ts, pro.get("active_hours") or [9, 23])


def _align_hours(ts: float, hours) -> int:
    """把时间推进入时段窗口（支持跨午夜）。"""
    try:
        lo, hi = int(hours[0]), int(hours[1])
    except (TypeError, ValueError, IndexError):
        return int(ts)
    if lo == hi:
        return int(ts)
    lt = time.localtime(ts)
    h = lt.tm_hour
    inside = (lo <= h < hi) if lo < hi else (h >= lo or h < hi)
    if inside:
        return int(ts)
    # 推到下一个窗口开始
    day = 0 if h < lo else 1
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lo, 0, 0, 0, 0, -1))
    return int(base + day * 86400 + random.randint(0, 1800))


def _has_pending_reply(q, account: str, chat: str) -> bool:
    try:
        for row in q.list_outbound("draft", 50, account=account):
            if row.get("chat") == chat and row.get("kind") == "reply":
                return True
    except Exception:
        pass
    return False


# ------------------------------------------------------------------ 收尾
def note_ending(q, account: str, chat: str, text: str) -> bool:
    """对方说「晚安/先忙」→ 静默几小时，别再主动。"""
    if not any(w in (text or "") for w in _ENDINGS):
        return False
    try:
        q.update_proactive_state(account, chat,
                                 quiet_until=int(time.time()) + 4 * 3600)
        log.info("收尾信号，主动静默 4 小时 [%s]" % chat)
        return True
    except Exception as e:
        log.warning("记录收尾失败：%s" % e)
        return False


# ------------------------------------------------------------------ 主循环
def tick(q, cfg: dict, brain, now: float | None = None) -> int:
    """检查所有候选联系人，能开口就开口。返回本次开口条数。"""
    pro = cfg.get("proactive") or {}
    if not pro.get("enabled"):
        return 0
    if not (cfg.get("agent") or {}).get("auto_reply"):
        return 0
    acct = cfg.get("account") or ""
    if not acct:
        return 0
    now = float(time.time() if now is None else now)
    day = today(now)

    # 异常熔断：正常用不到，防调度器 bug 把额度刷爆
    guard = int(pro.get("global_guard", 30))
    if guard > 0:
        used = sum(int(s.get("day_n") or 0)
                   for s in q.list_proactive_states(acct)
                   if s.get("day") == day)
        if used >= guard:
            log.warning("今日主动消息已达熔断线 %d 条，暂停" % guard)
            return 0

    n = 0
    for chat in candidates(cfg):
        try:
            if maybe_open(q, cfg, brain, acct, chat, now):
                n += 1
        except Exception as e:
            log.error("主动开口气异常 [%s]：%s: %s" % (chat, type(e).__name__, e))
    return n


def maybe_open(q, cfg, brain, acct: str, chat: str, now: float) -> bool:
    # 该联系人的参数（没单独设过的项跟随全局）：上限、间隔、静默、时段
    pro = params(cfg, chat)
    st = q.ensure_proactive_state(acct, chat,
                                  int(pro.get("trial_hold_days", 3)))
    if st.get("muted"):
        return False

    # 硬门禁：开关、白名单、联系人开关、黑名单、接管、时段、联系人暂停
    verdict = gate.evaluate_proactive(cfg, chat,
                                      paused=q.is_paused(chat), now=now)
    if verdict["action"] != "ok":
        return False

    # 节奏
    if now < float(st.get("next_at") or 0):
        return False
    if now < float(st.get("quiet_until") or 0):
        return False
    if now < float(st.get("cooldown_until") or 0):
        return False

    # 上次开口后 24 小时没被回 → 记一次「无回应」；连续 3 次冷却 3 天
    # （对方一旦回话，process 会把 await_reply_until 和 streak 清零）
    await_until = float(st.get("await_reply_until") or 0)
    if await_until and now > await_until:
        streak = int(st.get("no_reply_streak") or 0) + 1
        fields = {"await_reply_until": 0, "no_reply_streak": streak}
        if streak >= 3:
            fields["cooldown_until"] = int(now + 3 * 86400)
            log.info("连续 %d 次主动无回应，冷却 3 天 [%s]" % (streak, chat))
        q.update_proactive_state(acct, chat, **fields)
        if streak >= 3:
            return False

    day = today(now)
    per_day = int(pro.get("per_chat_daily", 3))
    if per_day > 0 and st.get("day") == day and int(st.get("day_n") or 0) >= per_day:
        return False

    # 别插嘴：对方刚说完 / 我方刚说完 / 还有没回的入站 / 有排队中的回复
    last_in = q.last_inbound_ts(chat, account=acct)
    if last_in and now - last_in < float(pro.get("quiet_minutes_after_user", 20)) * 60:
        return False
    if now - float(st.get("last_self_ts") or 0) < \
            float(pro.get("min_gap_after_self", 90)) * 60:
        return False
    if q.newest_pending(chat, limit=1, account=acct):
        return False
    if _has_pending_reply(q, acct, chat):
        return False

    # 素材：钩子优先，没钩子走纯解闷
    hook = q.pick_hook(chat, now, account=acct)
    kind = "hook" if hook else "idle"
    seed = (hook.get("text") or "") if hook else ""

    version = int(st.get("activity_version") or 0)
    comp = brain.compose_proactive(chat, seed=seed, kind=kind)
    if not comp.get("ok"):
        reschedule(q, acct, chat, now, pro, "生成失败：%s" % comp.get("error"))
        return False
    if comp.get("skip"):
        reschedule(q, acct, chat, now, pro, "模型选择沉默")
        log.info("主动开口：模型选择沉默 [%s]（%s）" % (chat, kind))
        return False

    reply = (comp.get("reply") or "").strip()
    if not reply:
        reschedule(q, acct, chat, now, pro, "空回复")
        return False

    # 生成期间对话有变化（对方发消息 / 本人手动发言）→ 丢弃草稿
    st2 = q.get_proactive_state(acct, chat) or {}
    if int(st2.get("activity_version") or 0) != version:
        reschedule(q, acct, chat, now, pro, "生成期间有新动静")
        log.info("主动开口：期间有变化，丢弃草稿 [%s]" % chat)
        return False

    # 占配额（检查与预留同一事务），同时推进 next_at
    next_at = plan_next(now, pro)
    if not q.claim_proactive_slot(acct, chat, day, per_day, next_at):
        log.info("主动开口：当日配额已满 [%s]" % chat)
        return False

    # 默认直接发；只有敏感承诺或截断才转人工（不做试发期）
    status, note = "draft", None
    label = None
    try:
        label = brain.sensitive_label(reply)
    except Exception as e:
        log.warning("主动消息敏感检测失败：%s" % e)
    if label:
        status, note = "hold", "sensitive:%s" % label
    elif comp.get("truncated"):
        status, note = "hold", "truncated"

    segs = split_reply(reply,
                       max_chars=int((cfg.get("send") or {}).get("bubble_max_chars", 40)),
                       max_bubbles=int((cfg.get("send") or {}).get("max_bubbles", 3)))
    ob_id = q.create_outbound(
        acct, chat, reply, status=status, kind="proactive", gate=note,
        ref_id=int(hook["id"]) if hook else None,
        activity_version=version, segments=segs or [reply],
        expires_ts=int(now) + 300)
    if hook:
        q.mark_hook(hook["id"], "used")
    q.update_proactive_state(acct, chat, last_proactive_id=ob_id,
                             last_self_ts=int(now),
                             await_reply_until=int(now + 86400))
    log.info("主动开口 #%d [%s]（%s%s，%s）：%s" % (
        ob_id, chat, kind,
        ":" + (hook.get("kind") or "") if hook else "",
        ("转人工：" + note) if note else "直接发送", redact(reply[:50], cfg)))
    return True


def send_block_reason(q, cfg, task: dict) -> str | None:
    """主动消息的有效期和生成时的会话版本必须仍然有效。"""
    expiry = task.get("expires_ts")
    if not expiry or time.time() >= float(expiry):
        return "主动消息已过期"
    state = q.get_proactive_state(task["account"], task["chat"])
    if not state or task.get("activity_version") != state.get("activity_version"):
        return "会话已有新消息，主动草稿失效"
    return None


def reschedule(q, acct: str, chat: str, now: float, pro: dict, why: str):
    try:
        q.update_proactive_state(acct, chat, next_at=plan_next(now, pro))
        log.info("主动开口重排 [%s]：%s" % (chat, why))
    except Exception as e:
        log.warning("重排失败 [%s]：%s" % (chat, e))


# ------------------------------------------------------------------ 钩子
_HOOK_PROMPT = (
    "读下面的聊天片段，找出「还没聊完、以后值得自然问一句」的事。"
    "只输出 JSON 数组，没有就输出 []。\n"
    "每条：{\"kind\":\"event|question|topic\",\"text\":\"一句话描述\","
    "\"earliest\":\"YYYY-MM-DD 或空\"}\n"
    "规则：\n"
    "1. 最多 2 条，宁少勿多；\n"
    "2. event=他提到未来要做的事（面试/考试/体检/交作业）；\n"
    "3. question=问了你还没答，或他说了一半没说完；\n"
    "4. topic=聊得投入但没聊完的话题；\n"
    "5. 日常寒暄、已经了结的事、抱怨不算；\n"
    "6. earliest 填那件事发生之后才能问的日期，不确定就留空。\n"
    "\n聊天：\n%s"
)


def hooks_allowed(q, cfg: dict, chat: str) -> bool:
    hk = (cfg.get("proactive") or {}).get("hooks") or {}
    if not hk.get("enabled", True):
        return False
    allowed, _ = gate.identity_ok(cfg, chat)
    if not allowed or chat.endswith("@chatroom"):
        return False
    return gate.evaluate_proactive(cfg, chat, paused=q.is_paused(chat))["action"] == "ok"


def extract_hooks(brain, q, acct: str, chat: str, texts: list[str],
                  cfg: dict) -> int:
    """从最近的对话里抽钩子。跑在后台线程，失败不影响回复。"""
    if not hooks_allowed(q, cfg, chat):
        return 0
    hk = (cfg.get("proactive") or {}).get("hooks") or {}
    texts = [t for t in (texts or []) if t and t.strip()]
    if not texts:
        return 0
    from llm import chat as llm_chat

    ttl = int(hk.get("ttl_days", 5)) * 86400
    now = time.time()
    body = "\n".join(texts[-8:])[:1200]
    try:
        r = llm_chat([{"role": "user", "content": _HOOK_PROMPT % body}],
                     cfg, timeout=40)
    except Exception as e:
        log.warning("钩子抽取调用失败：%s" % e)
        return 0
    if not r.get("ok"):
        return 0
    raw = (r.get("text") or "").strip()
    if "[" not in raw:
        return 0
    raw = raw[raw.find("["):raw.rfind("]") + 1]
    try:
        items = json.loads(raw)
    except Exception:
        log.warning("钩子抽取输出不是 JSON：%s" % raw[:80])
        return 0
    if not isinstance(items, list):
        return 0
    n = 0
    for it in items[:2]:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        if len(text) < 4:
            continue
        kind = str(it.get("kind") or "topic")
        if kind not in ("event", "question", "topic"):
            kind = "topic"
        earliest = 0
        ds = str(it.get("earliest") or "").strip()
        if ds:
            try:
                earliest = int(time.mktime(
                    time.strptime(ds + " 09:00", "%Y-%m-%d %H:%M")))
            except Exception:
                earliest = 0
        key = text[:24]
        if q.add_hook(acct, chat, key, kind, text,
                      expires_ts=int(now) + ttl, earliest_ts=earliest):
            n += 1
            log.info("记下钩子 [%s]：%s（%s）" % (chat, text[:40], kind))
    return n
