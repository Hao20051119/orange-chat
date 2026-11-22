# -*- coding: utf-8 -*-
"""定时提醒：识别入库、到点触发、话术生成。

分工
----
* ``reminder_parse`` —— 纯解析（中文时间 → 结构化），不碰库
* ``reminder``（本文件）—— 业务：判断委托、写库、到点触发、生成话术
* ``initiative`` —— 调度线程，每秒调一次 ``fire_due``

关键约束（见设计稿 §5）
------------------------
* 识别必须发生在 ``should_reply`` / ``[[SKIP]]`` **之前**——「明天提醒我跑步」
  是最不需要回复的句子，放在回复分支后面必然丢。
* 时间以该条消息的 ``msg_ts`` 为基准，不用 ``now()``。
* 到点记录、出站任务、推进排期在**同一事务**里完成。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta

import media
import msgtype
import reminder_parse
import gate
from logutil import get_logger, redact
from split import split_reply

log = get_logger()

_CN = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
       "十一", "十二")

# 话术模板：到点不依赖模型（模型挂了提醒也得响）。
# 别写「{t}了」——human_clock 已给出「凌晨一点四十六分」这种完整说法，再加「了」很怪
_TEMPLATES = (
    "{t}，{c}",
    "{t}，{c}别忘了",
    "{t}咯，{c}去",
    "到点了，{c}（¬_¬）",
)

# 关键词预筛：命中才值得交给模型判定（省调用）
# 委托词：出现任意一个就值得让模型看一眼「这是不是让我记着点什么」。
# 只收**有指向性的说法**——「时间」「记」这种单字太泛，会把普通聊天全拉进来。
_HINTS = (
    # 提醒 / 记着
    "提醒", "提个醒", "记得", "记着", "记住", "记一下", "备忘", "帮我记",
    "帮我留意", "帮我盯着", "帮我记着",
    # 叫醒
    "叫我", "喊我", "叫醒", "喊醒", "叫我起来", "喊我起床", "敲我",
    # 别忘了
    "别忘", "千万别忘", "不要忘",
    # 闹钟 / 定时
    "闹钟", "定闹钟", "设闹钟", "订闹钟", "定时",
    # 其他委托说法
    "说好了", "到时候", "到点", "别错过", "提醒我一下", "提醒下",
)

# 时间/事件信号：哪怕没有委托词，也可能是「我明天要做某事，你记着点」。
# 只收**能定位到某个时刻/周期**的说法；「今天」「晚上」这类高频词不收，
# 否则每句闲聊都要花一次模型调用。
_TIME_RE = re.compile(
    r"(\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*[点时]"
    r"|\d{1,2}\s*分(钟|钟)?"
    r"|\d{1,2}\s*[号日]"
    r"|每\s*(天|日|周|月|年|小时|\d+\s*(天|周|小时|分钟))"
    r"|\d+\s*(秒|分钟|小时|天|周|个月)\s*(后|以后|之后)"
    r"|明天|后天|大后天|明早|明晚|今晚|明天早上"
    r"|下下周|下周|下个月|本周|这周|这个月"
    r"|周[一二三四五六日天]|星期[一二三四五六日天]|礼拜[一二三四五六日天]|周末"
    r"|凌晨|早上|早晨|上午|中午|下午|傍晚|夜里|半夜"
    r"|待会|等会|一会儿|马上|回头|到时候|过两天|过几天"
)

# 取消意图：「别再提醒我跑步」「不用提醒了」
_CANCEL_HINTS = ("别再提醒", "不用再提醒", "不要提醒", "取消提醒", "停止提醒",
                 "别提醒", "不用提醒", "别再叫我", "别再喊我", "别催我",
                 "别叫他", "别叫她", "别每天", "不用再叫", "别再催", "别让他提醒",
                 "别再来叫我", "别喊他", "别催他")
# 取消词 / 动作词分开判断：「别每天叫他去上课了」「不用再叫我去上课了」
_CANCEL_NEG = re.compile(r"(别再|不用再|不要|停止|取消|不用|别)")
_CANCEL_ACTION = re.compile(r"(提醒|叫醒|叫我|叫他|叫她|叫|喊我|喊他|喊|催我|催他|催|让)")
# 剥目标时顺手去掉的助词/称呼
_CANCEL_FILLER = re.compile(r"(每天|再|都|要|去|了|吧|啊|呀|哦|呢|嘛|他|她|我|他们)")

# 微信表情转成文字后留下的标记，如「下楼买咖啡 [亲亲]」
_EMOJI_TAG = re.compile(r"\[[^\[\]]{1,8}\]")


# ------------------------------------------------------------------ 话术
def _num_cn(n: int) -> str:
    """0~59 → 中文（八、十、二十七、五十九）。"""
    n = int(n)
    if n < 10:
        return _CN[n]
    if n < 20:
        return "十" + (_CN[n - 10] if n > 10 else "")
    if n % 10 == 0:
        return _CN[n // 10] + "十"
    return _CN[n // 10] + "十" + _CN[n % 10]


def human_clock(ts: float | int) -> str:
    """「20:00」→「晚上八点」；「08:27」→「早上八点二十七分」。"""
    dt = datetime.fromtimestamp(int(ts))
    h, m = dt.hour, dt.minute
    if h < 6:
        period, hh = "凌晨", h or 12
    elif h < 12:
        period, hh = "早上", h
    elif h == 12:
        period, hh = "中午", 12
    elif h < 18:
        period, hh = "下午", h - 12
    else:
        period, hh = "晚上", h - 12
    out = _num_cn(hh % 24) + "点"
    if m == 30:
        out += "半"
    elif m:
        out += _num_cn(m) + "分"
    return period + out


# 一次判定最多看几张图：课表/日程常常一句话配一张图，多给也没用
_IMAGE_JUDGE_MAX = 3
_IMAGE_JUDGE_BUDGET = 5 * 1024 * 1024      # data URI 总量

# 「对/是/嗯/好的」这类确认
_YES_RE = re.compile(
    r"^(对|对啊|对呀|嗯|嗯嗯|恩|恩恩|是|是的|是滴|好|好的|好滴|行|可以|"
    r"没错|对的|就这个|就这样|这样|记吧|记下吧|没问题|ok|okay)[，,。.!！~～]*$", re.I)

_WEEK_CN = "一二三四五六日"


def describe_schedule(rule: str, ts: int | float, tz: int = 0,
                      tz_zone: str | None = None) -> str:
    """规则 + 第一个到点 → 一句给人看的说法（「每周二 13:50」）。"""
    ts = int(ts or 0)
    if not ts:
        return "（时间没定）"
    offset = reminder_parse.tz_from_zone(tz_zone, ts) if tz_zone else None
    dt = datetime.fromtimestamp(ts + (tz if offset is None else offset))
    hm = dt.strftime("%H:%M:%S" if dt.second else "%H:%M")
    rule = (rule or "single").strip()
    if rule == "single":
        return "%d月%d日 %s" % (dt.month, dt.day, hm)
    if rule == "daily":
        return "每天 " + hm
    if rule.startswith("weekly:"):
        try:
            days = [int(x) for x in rule.split(":", 1)[1].split(",") if x.strip()]
        except ValueError:
            days = []
        days = [d for d in days if 1 <= d <= 7]
        if days:
            return "每周%s %s" % ("、".join(_WEEK_CN[d - 1] for d in days), hm)
        return "每周 " + hm
    if rule.startswith("interval:"):
        return "每 %s 天 %s" % (rule.split(":", 1)[1], hm)
    if rule.startswith("monthly:"):
        return "每月 %s 号 %s" % (rule.split(":", 1)[1], hm)
    return hm


def _confirm_ask(rule: str, ts: int | float, content: str, tz: int = 0,
                 tz_zone: str | None = None) -> str:
    """带图判定之后用来核对的一句：「是不是要在每周二 13:50 提醒你…？」"""
    return "是不是要在%s提醒你%s？" % (describe_schedule(rule, ts, tz, tz_zone),
                                      (content or "这件事").strip())


def _judge_images(images, limit: int = _IMAGE_JUDGE_MAX, *,
                  account: str | None = None) -> list[str]:
    """候选/待判定带的图 → data URI 列表；读不出来的直接跳过。"""
    uris: list[str] = []
    used = 0
    for it in (images or [])[:limit]:
        if not isinstance(it, dict):
            continue
        path = (it.get("p") or "").strip()
        if not (path and os.path.isfile(path)):
            path = media.resolve_any(
                it.get("m") or "", kind="image", chat=it.get("chat") or "",
                local_id=it.get("local_id"), shard=it.get("shard"), account=account) or ""
        if not path:
            continue
        try:
            uri, size = media.to_vision_uri(path)
        except Exception as e:
            log.warning("判定用图读取失败：%s" % e)
            continue
        if not uri or used + size > _IMAGE_JUDGE_BUDGET:
            continue
        used += size
        uris.append(uri)
    return uris


def build_text(rem: dict, scheduled_ts: int, late: bool = False,
               tz: int = 0) -> str:
    """按人设生成一条提醒。同一个提醒固定用同一个模板，别忽冷忽热。

    ``tz`` 是他那边与主机的时差：报点报他那边的时间，别让英国的早上被写成
    北京的「下午五点半」。代传话（relay）不报钟点，只交代谁托的。
    """
    if (rem.get("confirm_mode") or "") == "relay_back":
        # 回传：这句话已经是模型组织好的转述，直接发
        return rem.get("content") or "他回你了"
    if (rem.get("confirm_mode") or "") == "relay":
        if rem.get("relay_verbatim"):
            # 代说/代问：正文就是他要转达的那句话，不套「让我提醒你」
            return rem.get("content") or "有事找你说一声"
        text = "%s让我提醒你：%s" % (rem.get("relay_from") or "有人",
                                     rem.get("content") or "该做的事")
        return ("晚了点，" + text) if late else text
    tpl = _TEMPLATES[int(rem.get("id") or 0) % len(_TEMPLATES)]
    text = tpl.format(t=human_clock(int(scheduled_ts) + int(tz or 0)),
                      c=rem.get("content") or "该做的事")
    return ("晚了点，" + text) if late else text


def apply_tz_hint(q, account: str, chat: str, value) -> int | None:
    """把模型给出的时区线索落库，返回「对方当地 - 主机」秒数。

    ``value`` 只认 IANA 时区名（``Europe/London``）或 ``UTC+8`` 这种写法：
    模型负责「判断对方在哪」，算术和夏令时交给 ``tzinfo`` —— 让模型自己
    算「北京 22:00 对应伦敦几点」正是上一轮方向写反的根源。
    认不出就不动已有记录，返回 ``None``。
    """
    text = str(value or "").strip()
    if not text:
        return None
    off = None
    zone = None
    if "/" in text:                       # IANA 名：能跟着夏令时变
        off = reminder_parse.tz_from_zone(text)
        if off is not None:
            zone = text
    if off is None:
        m = re.match(r"^(?:UTC|GMT)?\s*([+-]?\d{1,2})(?:[:：](\d{2}))?$", text, re.I)
        if m:
            h = int(m.group(1))
            mi = int(m.group(2) or 0)
            if -14 <= h <= 14 and 0 <= mi < 60:
                off = h * 3600 + (mi * 60 if h >= 0 else -mi * 60)
    if off is None:
        return None
    try:
        q.set_chat_tz(account, chat, off, zone=zone)
        log.info("记住时区线索 [%s]：%s → %+.1f 小时" % (chat, text, off / 3600.0))
    except Exception as e:
        log.warning("时区线索落库失败 [%s]：%s" % (chat, e))
        return None
    return off


_PLACE_HINT = re.compile(
    # 「我在/住在/搬到/移居」这类句式后面常跟地点；「我在忙/开会/吃饭」是状态，
    # 用负向前瞻挡掉，不为它们多花一次模型调用。
    r"(?:我在|我这边在|我现在在|人在|出差在|待在|住在|搬到|搬去|移居|定居|来到|到了)\s*"
    r"(?!忙|吃|开|上|下|睡|等|看|写|做|休|加|出|想|说|聊|打|准|学|试|排|赶|收|处|玩|逛|回)"
    r"[^\s，,。！!？?]{1,12}")


def learn_tz_with_llm(cfg: dict, text: str, base_ts: int) -> str | None:
    """问模型「说话人现在在哪个时区」，只收 IANA 时区名。

    规则只认「现在几点」「时差几小时」；「我在英国」这种地名线索靠模型认。
    模型只负责认地名，**不负责算偏移**——算术交给 ``tz_from_zone``。
    """
    try:
        from llm import chat as llm_chat
    except Exception:
        return None
    now = datetime.fromtimestamp(int(base_ts))
    prompt = (
        "判断说话人现在所在地的 IANA 时区名。只输出 JSON，不要解释：\n"
        '{"tz":"Europe/London"}\n'
        "规则：\n"
        "1. 说得出国家/城市就用该地常用时区名（英国→Europe/London，"
        "日本→Asia/Tokyo，纽约→America/New_York）。\n"
        "2. 跟位置无关或判断不出（「我在忙」「我在家」）→ tz 填空字符串。\n"
        "3. 不要自己换算小时数。\n"
        "当前（北京）时间：%s\n消息：%s\n"
    ) % (now.strftime("%Y-%m-%d %H:%M"), (text or "")[:200])
    try:
        r = llm_chat([{"role": "user", "content": prompt}], cfg, timeout=8)
    except Exception as e:
        log.warning("时区线索判定失败：%s" % e)
        return None
    if not r.get("ok"):
        return None
    raw = (r.get("text") or "").strip()
    if "{" in raw:
        raw = raw[raw.find("{"):raw.rfind("}") + 1]
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    zone = str(obj.get("tz") or "").strip()
    if not zone or "/" not in zone:
        return None
    if reminder_parse.tz_from_zone(zone) is None:
        return None                     # 模型编的时区名，别落库
    return zone


def segments_for(text: str, cfg: dict) -> list[str]:
    send_cfg = cfg.get("send") or {}
    if not send_cfg.get("bubble", True):
        return [text]
    return split_reply(text,
                       max_chars=int(send_cfg.get("bubble_max_chars", 40)),
                       max_bubbles=int(send_cfg.get("max_bubbles", 3))) or [text]


def polish_task(q, cfg: dict, brain, task: dict,
                now: float | None = None) -> dict:
    """发送前让模型重写到点那句话；不成气候就保持模板文案。

    为什么不在 ``fire_due`` 里写：调度线程 1 秒一跳，模型一卡就把别的提醒
    拖晚；发送线程本来就是串行等发送的，顺手把措辞换掉最划算。
    准点的底线不动——模型超时 / 空回复 / 想跳过时，原模板照发。
    """
    rem_cfg = ((cfg.get("proactive") or {}).get("reminders") or {})
    if (task.get("kind") or "") == "reminder":
        try:
            occ = q.get_occurrence(int(task["ref_id"])) if task.get("ref_id") else None
            _rem = q.get_reminder(occ["reminder_id"]) if occ else None
        except Exception:
            _rem = None
        if _rem and (_rem.get("confirm_mode") or "") == "relay_back":
            if task.get("text_source") == "template" and q.rewrite_outbound_text(
                    task["id"], task["text"], segments_for(task["text"], cfg), source="model"):
                return q.get_outbound(task["id"]) or task
            return task          # 回传的话已经组织好了，别再让模型改写
    # 提醒正文一律由模型写（旧配置里的 llm_text 不再作为开关：允许模板顶上
    # 就等于允许系统生成消息内容，和「内容完全由模型生成」的约定冲突）。
    make = getattr(brain, "compose_reminder", None)
    if make is None or not task.get("ref_id"):
        return task
    if (task.get("kind") or "") != "reminder":
        return task
    try:
        occ = q.get_occurrence(int(task["ref_id"]))
        rem = q.get_reminder(occ["reminder_id"]) if occ else None
    except Exception as e:
        log.warning("读取提醒失败 [%s]：%s" % (task.get("chat"), e))
        return task
    scheduled = int((occ or {}).get("scheduled_ts") or 0)
    if not rem or not scheduled:
        return task
    chat = task.get("chat") or ""
    tz = reminder_parse.tz_from_zone(rem.get("tz_zone"), scheduled)
    if tz is None:
        tz = int(q.get_chat_tz(task.get("account"), chat) or 0)
    plain = build_text(rem, scheduled, late=False, tz=tz).strip()
    lagged = build_text(rem, scheduled, late=True, tz=tz).strip()
    at_hand = (task.get("text") or "").strip()
    if at_hand == lagged:
        late = True                      # 到点后拖了一小会儿，模板是「晚了点，」
    elif at_hand == plain:
        late = False
    else:
        return task                      # 已经改写过（或人工改过），别覆盖人家
    try:
        original = q.reminder_source_text(rem["id"], account=task.get("account"))
    except Exception:
        original = ""
    timeout = float(rem_cfg.get("llm_timeout", 10) or 10)
    # 报点报的是「他那边几点」，否则英国傍晚会被写成凌晨。
    # 代说/代问不报点：问一句「晚饭吃的什么」前面缀个钟点很怪。
    when_text = "" if rem.get("relay_verbatim") else (
        human_clock(scheduled + tz) if tz else human_clock(scheduled))
    try:
        kwargs = {}
        if (rem.get("confirm_mode") or "") == "relay":
            kwargs["relay_from"] = rem.get("relay_from") or "有人"
        res = make(chat, rem.get("content") or "", original=original,
                   when_text=when_text, late=late,
                   timeout=max(3, int(timeout)), **kwargs)
    except Exception as e:
        log.warning("提醒措辞失败 [%s]：%s: %s" % (chat, type(e).__name__, e))
        return task
    text = (res.get("reply") or "").strip() if res.get("ok") else ""
    if not text:
        log.info("提醒沿用模板 [%s]：%s" % (chat, res.get("error") or "模型没给话"))
        return task
    segs = segments_for(text, cfg)
    try:
        if not q.rewrite_outbound_text(task["id"], text, segs, source="model"):
            return task
    except Exception as e:
        log.warning("提醒改写落库失败 #%s：%s" % (task.get("id"), e))
        return task
    task = dict(task)
    task["text"] = text
    task["segments"] = json.dumps(segs, ensure_ascii=False)
    task["text_source"] = "model"
    log.info("提醒改由模型写 #%d [%s]：%s" % (
        task["id"], chat, redact(text, cfg)))
    return task


# ------------------------------------------------------------------ 识别
def accepted_hint(q, ids, account: str | None = None,
                  chat: str | None = None) -> str:
    """这一轮刚记下的提醒 → 塞给聊天回复的一句话。

    模型看不到提醒库，不告诉它就会嘴上说「我又不是定时器」，转头系统照发，
    看起来像两个人格。只描述事实，不让它复述原话。有跨时差的，报他那边的时间。
    """
    parts = []
    for rid in ids or []:
        try:
            rem = q.get_reminder(int(rid), account=account)
        except Exception:
            continue
        if not rem or not rem.get("next_ts"):
            continue
        if chat and rem.get("chat") != chat:
            continue                  # 编号撞车的别进来（待确认 id 与提醒 id 不共用）
        tz = reminder_parse.tz_from_zone(rem.get("tz_zone"), rem["next_ts"])
        if tz is None:
            tz = int(q.get_chat_tz(rem["account"], rem["chat"]) or 0)
        parts.append("%s（%s）" % ((rem.get("content") or "该做的事"),
                                   human_clock(rem["next_ts"] + tz)))
    if not parts:
        return ""
    return ("【你已经记下了】对方刚托你提醒他：%s。到点你会主动开口，"
            "这一轮别提「我不是定时器」「我记不住」这类话，也别把原话念回去，"
            "顺口应一声就行。" % "、".join(parts))


def clean_content(text: str) -> str:
    """清掉微信表情标记和多余空白。"""
    s = _EMOJI_TAG.sub(" ", text or "")
    return re.sub(r"\s+", " ", s).strip(" ，,。、")[:60]


def wants_cancel(text: str) -> bool:
    """这句话是不是在「取消某个提醒」。"""
    t = text or ""
    if any(h in t for h in _CANCEL_HINTS):
        return True
    if re.fullmatch(r"(?:不[用必]了?|算了|取消吧)[。！! ]*", t):
        return True
    # 通用写法：取消词 + 动作词都要有（「别每天叫他去上课了」）
    return bool(_CANCEL_NEG.search(t) and _CANCEL_ACTION.search(t))


def _cancel_target(text: str) -> str:
    """从「别再提醒我跑步了」「别每天叫他去上课了」里抠出「跑步」「去上课」。"""
    s = _CANCEL_NEG.sub(" ", text or "")
    s = _CANCEL_ACTION.sub(" ", s)
    s = _CANCEL_FILLER.sub(" ", s)
    s = re.sub(r"[，,。.!！、？?\s]+", " ", s).strip()
    return clean_content(s)[:20]


def maybe_cancel(q, chat: str, text: str, account: str | None = None) -> tuple[bool, int]:
    """识别「别再提醒我 X」并取消对应提醒。返回 (是否命中取消意图, 取消条数)。

    没有目标（「别再提醒我了」）就取消该会话全部活跃提醒；
    有目标但一条都没匹配上时只记日志，**不会**顺手把别的提醒删掉。
    """
    if not wants_cancel(text):
        return False, 0
    target = _cancel_target(text)
    if re.fullmatch(r"(?:不[用必]了?|算了|取消吧)[。！! ]*", text):
        target = ""
        return True, q.cancel_pending_reminders(account, chat, target)
    rows = q.cancellable_reminders(chat, account=account)
    n = q.cancel_pending_reminders(account, chat, target)
    # 代传话：作为**发起人**也能取消（「别每天叫他去上课了」）
    try:
        seen = {r["id"] for r in rows}
        rows += [r for r in q.relay_reminders_from(chat, account=account)
                 if r["id"] not in seen]
    except Exception as e:
        log.warning("读取代传话失败 [%s]：%s" % (chat, e))
    if not rows:
        log.info("收到取消意图但没有活跃提醒 [%s]" % chat)
        return True, n
    if target:
        hit = [r for r in rows if target and target in (r["content"] or "")]
        if not hit:
            log.info("收到取消意图但没匹配到提醒 [%s]：目标=%s" % (chat, redact(target)))
            return True, n
    else:
        hit = rows
    for r in hit:
        if q.update_reminder(r["id"], expected_revision=r["revision"], status="cancelled"):
            n += 1
            log.info("已取消提醒 #%d [%s]" % (r["id"], chat))
    return True, n


def looks_commission(text: str) -> bool:
    return any(h in (text or "") for h in _HINTS)


# ------------------------------------------------------------------ 缺钟点反问
def enqueue_clarify_prompt(q, account: str, chat: str, conn=None) -> int | None:
    def format_ask(rec):
        if rec["proposed_ts"]:
            return _confirm_ask(rec["rule"], rec["proposed_ts"], rec["content"],
                                q.get_chat_tz(account, chat) or 0, rec.get("tz_zone"))
        ask = (rec["ask"] or "什么时候提醒你%s？" % rec["content"]).strip()
        return ask if ask.endswith(("？", "?")) else ask.rstrip("。") + "？"

    return q.enqueue_clarify_prompt(account, chat, format_ask, conn=conn)


def clarify_send_block_reason(q, task: dict) -> str | None:
    """拒绝发送已撤销、改期、过期或正文被改动的核对问句。"""
    rec = q.get_clarify_by_id(task["ref_id"]) if task.get("ref_id") else None
    if not rec or rec["account"] != task["account"] or rec["chat"] != task["chat"]:
        return "找不到对应的提醒核对"
    if time.time() >= rec["expires_ts"]:
        return "提醒核对已过期"
    if (rec["status"] != "pending" or rec["prompt_outbound_id"] != task["id"]
            or rec["ask"] != task["text"]):
        return "提醒核对已更新或结案"
    return None


def maybe_answer_clarify(cfg, q, chat: str, text: str, account: str, *,
                         msg_ts: int | None = None, msg_seq: int | None = None,
                         request_key: str | None = None) -> int | None:
    """合并已送达提案与用户答复，建单和结案一起提交。"""
    body = (text or "").strip()
    if not body or len(body) > 100:
        return None
    if looks_commission(body) and not re.match(r"^(不对|不是|应该是|改成|改到)", body):
        return None
    rem_cfg = ((cfg.get("proactive") or {}).get("reminders") or {})
    limit = int(rem_cfg.get("clarify_max_asks", 3) or 3)
    now = int(time.time())
    msg_ts = int(msg_ts or now)
    with q.tx() as conn:
        if request_key is not None and q.has_action(account, chat, request_key, conn=conn):
            return None
        rec = q.get_clarify(chat, "asked", account=account, conn=conn) or q.get_clarify(
            chat, account=account, conn=conn)
        if not rec or msg_ts < rec["base_ts"]:
            return None
        sent_ts = rec.get("prompt_sent_ts")
        asked = sent_ts is not None and msg_ts >= sent_ts
        sent_seq = rec.get("prompt_verified_seq")
        if sent_seq and msg_seq:
            asked = int(msg_seq) > sent_seq
        proposed = int(rec.get("proposed_ts") or 0)
        rule = reminder_parse.valid_rule(rec.get("rule")) or "single"
        tz = int(q.get_chat_tz(account, chat) or 0)
        zone = rec.get("tz_zone") or q.get_chat_zone(account, chat)
        ts = None
        if _YES_RE.fullmatch(body):
            if not asked or q.has_newer_clarify_prompt(
                    account, chat, rec.get("prompt_outbound_id"),
                    msg_ts=msg_ts, msg_seq=msg_seq, conn=conn):
                return None
            if proposed > now:
                ts = proposed
            elif proposed:
                q.clear_clarify_proposal(rec["id"], "原时间已过，你想改到什么时候提醒？",
                                         conn=conn)
        else:
            def local(host_ts):
                offset = reminder_parse.tz_from_zone(zone, host_ts) if zone else None
                return datetime.fromtimestamp(host_ts + (tz if offset is None else offset))

            fixed = reminder_parse.clarification_schedule(
                body, rec.get("source_text") or "", rule, local(now), local(rec["base_ts"]),
                local(proposed) if proposed else None)
            if fixed:
                rule, local_ts = fixed
                ts = reminder_parse.to_host_ts(local_ts, tz, zone)
        if ts is None:
            if asked:
                q.set_clarify(rec["id"], "expired" if rec["asks"] >= limit else "pending",
                              conn=conn)
            return None
        rid = q.add_reminder(account, chat, rec["content"], rule, ts, anchor_ts=ts,
                             tz_zone=zone, conn=conn)
        q.set_clarify(rec["id"], "done", conn=conn)
        if request_key is not None:
            q.record_action(account, chat, request_key, rid, "clarify_confirmed", conn=conn)
    log.info("按澄清建提醒 #%d [%s]：%s" % (rid, chat, describe_schedule(rule, ts, tz, zone)))
    return rid


def looks_worth_judging(text: str) -> bool:
    """值不值得花一次模型调用去判断「这是不是提醒委托」。

    命中委托词，或者含时间/事件信号（「明天」「两点」「每天」…）就值得。
    纯寒暄、纯表情不会命中，所以不会滥烧额度。
    """
    text = text or ""
    if looks_commission(text):
        return True
    return bool(_TIME_RE.search(text))


def judge_with_llm(cfg: dict, text: str, base_ts: int,
                   tz: int | None = None,
                   tz_zone: str | None = None,
                   images=None) -> dict | None:
    """让模型判断「这句话是不是提醒委托」，并给出内容、规则、时间。

    返回 ``{"is_reminder", "confidence", "content", "rule", "next_ts",
    "need_time", "ask"}``；模型不可用或输出不可解析时返回 ``None``。

    为什么要有这一层：本地规则只能认「提醒我 明早八点…」这种显式委托，
    认不出「明天下午两点多我要去打球」（没委托词）也补不出「吃早饭=07:30」
    这种常识。语义判断本来就是模型的活。

    ``images``：这一批里同时发来的图（``[{"p":路径,"m":md5}]``）。课表、日程、
    海报里的时间只有图上才有——光看「以后每次化学前沿课前十分钟提醒我」永远
    猜不出那门课是周几几点。

    ``tz``：对方所在地与主机的时差（秒）。有时差必须在提示里讲明白，否则
    模型会把「他那边 20:50」直接填成北京的 20:50，提醒晚 8 小时。
    """
    try:
        from llm import chat as llm_chat
    except Exception:
        return None
    import json

    now = datetime.fromtimestamp(int(base_ts))
    weekday = "一二三四五六日"[now.isoweekday() - 1]
    tz_line = ""
    if tz or tz_zone:
        offset = reminder_parse.tz_from_zone(tz_zone, base_ts)
        local = datetime.fromtimestamp(int(base_ts) + (int(tz or 0) if offset is None else offset))
        zone_note = "（%s）" % tz_zone if tz_zone else ""
        tz_line = (
            "对方所在地%s，现在是他那边的 %s。"
            "**这是从之前的对话里记下来的，别问他时区、也别自己换算**；"
            "下面要求你填的时间一律按他当地写。\n"
            % (zone_note, local.strftime("%Y-%m-%d %H:%M:%S")))
    uris = _judge_images(images, account=cfg.get("account"))
    image_line = ""
    if uris:
        image_line = (
            "他同时发来 %d 张图（课表、日程、截图、海报都有可能）。**先看图**："
            "从图里找出这句话需要的信息——那门课/那件事是哪一天、第几节、"
            "几点到几点、周期是不是每周。课表里的课一般每周一次，rule 用 "
            "weekly:N（周一=1…周日=7）。图和话对不上时以话为准。\n"
            "给了图就必须在 ask 里写一句**核对用**的问句，带上你从图里读到的"
            "具体时间和事，例如「是不是要在每周二 13:50 提醒你化学前沿课前准备？」；"
            "图上真读不出时间就按缺时间处理（need_time=true）。\n" % len(uris))
    prompt = (
        "判断这句话是不是在「托我记住某件事，到时候提醒他」。只输出 JSON，不要解释。\n"
        "当前时间：%s（周%s，主机时间）\n"
        "%s"
        "%s"
        "消息：%s\n"
        "输出：\n"
        '{"is_reminder":true,"confidence":0.0,"content":"提醒什么","rule":"single",'
        '"next":"YYYY-MM-DD HH:MM:SS","need_time":false,"ask":"","tz":""}\n'
        "判定标准：\n"
        "1. 明说「提醒我 / 记得 / 别忘」→ confidence 0.9 以上\n"
        "2. 只说「我明天下午要去打球」这种自己未来要做的事 → 可能想被提醒，confidence 0.6\n"
        "3. 寒暄、抱怨、过去的事、反问句 → is_reminder=false\n"
        "4. 没说钟点但有明确事件（吃早饭/上班/午睡）→ 按常识补：早饭 07:30、"
        "午饭 12:00、晚饭 18:30、睡前 23:00、上班 08:30、下班 18:00；need_time=false\n"
        "   只说了时段没给钟点（「每周三晚上」）→ 凌晨 05:00、早上 08:00、"
        "上午 09:00、中午 12:00、下午 14:00、傍晚 18:00、晚上 20:00、半夜 23:00\n"
        "5. 真定不了时间 → need_time=true，next 填日期部分（如 2026-11-21 00:00），"
        "ask 写一句要问他本人的话\n"
        "6. rule 只能取：single | daily | weekly:1,3 | interval:2 | monthly:15\n"
        "7. next 必须晚于当前时间；单次超过 30 天算不合理\n"
        "8. content 只写「要做的事」，去掉时间词和「提醒我/记得/每天」这类字眼：\n"
        "   「明天下午两点多去打球」→「打球」；「每天吃早饭」→「吃早饭」\n"
        "9. 「我现在是20:44」「时差8个小时」这类话是时区线索，不是要提醒的事\n"
        "10. **next 一律按对方当地时间写，不要换算成北京时间**；对方提到自己在"
        "哪个国家/城市（如「我在英国」）→ tz 填 IANA 时区名（Europe/London），"
        "没提就填空字符串\n"
        "11. 相对时间（「20 秒后」「半小时后」「两天后」）要按当前时间精确推算，"
        "秒数也算进去，别抹成整分\n"
        "12. 一句话可能先讲背景再让你提醒（「我以后每周三晚上都会空下来，"
        "你记得提醒我去公园跑步」）→ content 只写要提醒的那件事（「去公园跑步」），"
        "不要把背景复述进来\n"
        "13. 一句话里有两个时间（「我三点半开会，三点叫我」）→ next 取**要提醒的"
        "那个点**（三点），不是事件时间（三点半）\n"
        "14. 有起点表达（「从下周开始」「从下下周开始」「从 12 月起」「后天开始」）"
        "→ next 取那个起点之后**第一次**到点，不要提前到本周/本周之前\n"
        "15. 「课前/开会前十分钟」这种要**提前量**的：next 就填往前推十分钟的那个"
        "点，content 写事件本身（「化学前沿课前准备」）\n"
        "16. 这件事的时间只有他自己知道（「每次XX课前」「那个会」「老时间」）而图上"
        "又读不出来时，**不要按常识硬编周几/钟点**：need_time=true，ask 直接问"
        "「XX课是周几几点上？」。宁可问一句，也别给一个错时间\n"
    ) % (now.strftime("%Y-%m-%d %H:%M:%S"), weekday, tz_line, image_line, text[:200])
    parts: list[dict] = [{"type": "text", "text": prompt}]
    for uri in uris:
        parts.append({"type": "image_url", "image_url": {"url": uri}})
    payload = parts if uris else prompt
    try:
        r = llm_chat([{"role": "user", "content": payload}], cfg, timeout=30)
        if uris and not r.get("ok") and r.get("status") in (400, 413, 415, 422):
            uris = []
            fallback = prompt.replace(
                image_line, "附图无法读取。如果时间需从图里得知，填 need_time=true，请对方提供时间。\n", 1)
            r = llm_chat([{"role": "user", "content": fallback}], cfg, timeout=30)
    except Exception as e:
        log.warning("提醒判定模型调用失败：%s" % e)
        return None
    if not r.get("ok"):
        return None
    raw = (r.get("text") or "").strip()
    if "{" in raw:
        raw = raw[raw.find("{"):raw.rfind("}") + 1]
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        log.warning("提醒判定输出不是 JSON：%s" % raw[:80])
        return None

    if not isinstance(obj, dict) or not isinstance(obj.get("is_reminder"), bool):
        return None
    try:
        confidence = float(obj.get("confidence") or 0)
    except (ValueError, TypeError):
        return None
    if not 0 <= confidence <= 1:
        return None
    model_zone = str(obj.get("tz") or "").strip()[:40]
    if reminder_parse.tz_from_zone(model_zone, base_ts) is None:
        model_zone = ""
    zone = model_zone or tz_zone
    out = {
        "is_reminder": obj["is_reminder"],
        "confidence": confidence,
        "content": str(obj.get("content") or "").strip()[:60],
        "rule": reminder_parse.valid_rule(str(obj.get("rule") or "")) or "single",
        "next_ts": None,
        "need_time": obj.get("need_time") is True,
        "ask": str(obj.get("ask") or "").strip()[:80],
        "tz": model_zone,
        # 图没读出来时模型其实没看图，别把它当「带图判定」去要核对
        "used_images": bool(uris),
    }
    if images and not uris and out["is_reminder"] and not reminder_parse.parse_clock(text):
        out["need_time"] = True
    short_delay = reminder_parse.second_delay(text) if out["rule"] == "single" else None
    if short_delay and out["is_reminder"] and looks_commission(text):
        out.update(next_ts=int(base_ts) + short_delay, need_time=False, short_delay=True)
        return out
    next_s = str(obj.get("next") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(next_s, fmt)
        except ValueError:
            continue
        rule = out["rule"]
        if rule.startswith("weekly:") and str(dt.isoweekday()) not in rule[7:].split(","):
            aligned = reminder_parse._next_recurring(rule, dt.hour, dt.minute, dt)
            if aligned is not None:
                dt = datetime.fromtimestamp(aligned).replace(second=dt.second)
        # 模型给当地钟点，按计划触发那一刻的时区换算。
        ts = reminder_parse.to_host_ts(int(dt.timestamp()), int(tz or 0), zone)
        now_ts = time.time()
        if now_ts < ts < now_ts + 366 * 86400:
            out["next_ts"] = ts
        break
    return out


def note_tz_hint(cfg: dict, q, chat: str, text: str, base_ts: int,
                 account: str | None = None,
                 rem_cfg: dict | None = None) -> int | None:
    """从一句话里记下对方所在地（「我在英国」这类地名线索）。

    规则只认「我现在是20:44」「时差8小时」；地名交给模型认，只收 IANA
    时区名，偏移由代码按目标时刻算。记住之后，这个联系人后续所有提醒都
    按他当地的时间排——**时区信息可能在这句话里，也可能来自之前记下的**。
    """
    text = (text or "").strip()
    if not text:
        return None
    if reminder_parse.tz_offset(text, base_ts) is not None:
        return None                        # 报时/自述时差，规则已经处理
    if not _PLACE_HINT.search(text):
        return None
    rem_cfg = rem_cfg if rem_cfg is not None else (
        (cfg.get("proactive") or {}).get("reminders") or {})
    if not rem_cfg.get("llm_tz", True):
        return None
    zone = learn_tz_with_llm(cfg, text, base_ts)
    if not zone:
        return None
    account = account or (cfg or {}).get("account") or ""
    return apply_tz_hint(q, account, chat, zone)


def _apply_llm_tz(q, account: str, chat: str, r: dict) -> int | None:
    return apply_tz_hint(q, account, chat, r["tz"]) if r.get("tz") else None


def plan_decision(r: dict, rem_cfg: dict, has_images: bool, source_text: str = "",
                  tz: int = 0, tz_zone: str | None = None) -> dict:
    """把模型判定翻成动作：create / clarify / skip。

    * 缺时间 → clarify（问，不建单）
    * 这批带了图 → clarify（先把「是不是要在每周二 13:50 提醒你化学前沿？」核对一遍）
    * 把握不足 → skip
    * 其余 → create

    「信息不全就问到清楚为止」的循环由 ``maybe_answer_clarify`` 接着做：
    他回「对」按提案建单，回「不对，周三」按新说法重建，回别的就再问一次。
    """
    content = (r.get("content") or "").strip()
    conf = float(r.get("confidence") or 0)
    if conf < 0.5:
        return {"action": "skip", "reason": "置信度 %.2f 偏低" % conf}
    rule = r.get("rule") or "single"
    next_ts = int(r.get("next_ts") or 0)
    need_time = bool(r.get("need_time")) or not next_ts
    confirm_images = bool(has_images) and bool(rem_cfg.get("confirm_with_images", True))
    if need_time:
        # 模型自己都说「不知道几点」：它顺手填的那个时间是猜的（「每周一 07:50」），
        # 绝不能存成提案——否则用户回一句「对」就把错时间坐实了。
        ask = (r.get("ask") or "").strip() or "几点提醒你？"
        explicit = reminder_parse._rule_of(reminder_parse.normalize(source_text))
        return {"action": "clarify", "ask": ask, "rule": explicit[0] if explicit else "",
                "proposed_ts": None, "content": content}
    if confirm_images:
        # 图里读到了时间也要先核对一句，让他有机会纠正
        ask = _confirm_ask(rule, next_ts, content, tz, tz_zone)
        return {"action": "clarify", "ask": ask, "rule": rule,
                "proposed_ts": next_ts, "content": content}
    return {"action": "create", "rule": rule, "ts": next_ts, "content": content}


def maybe_capture(cfg: dict, q, chat: str, msgs: list[dict],
                  account: str, images=None, prompt_outbounds: list[int] | None = None) -> list[int]:
    """扫描一轮入站消息，把提醒委托写进提醒库（或记成「待确认」）。

    粗筛（委托词/时间信号）命中之后，把这句话和**这一批发来的图**一起交给
    模型：课表、日程、海报里的时间只有图上才有。模型判完：

    * 信息全、也没图 → 直接建单
    * 缺时间 / 带了图 → 记「待确认」，回复侧先问一句核对，等他确认或纠正
    * 把握不足 → 不建

    幂等：同一条入站消息（``inbound.id``）只会处理一次。
    返回新建的提醒 ID。若该批排入核对问句，将其出站 ID 写入 prompt_outbounds。
    """
    rem_cfg = ((cfg.get("proactive") or {}).get("reminders") or {})
    if not rem_cfg.get("enabled", False):
        return []
    if (chat or "").endswith("@chatroom"):
        return []                      # v1 只支持私聊委托
    created: list[int] = []
    for m in msgs:
        text = (m.get("content") or "").strip()
        if not text:
            continue
        # msg_type 是中文（「文本」「动画表情」…），必须走 msgtype.kind 归一，
        # 直接比 "text" 会把所有文本消息都漏掉
        if msgtype.kind(m.get("msg_type"), m.get("type_code")) != "text":
            continue
        key = str(m.get("id"))
        if q.has_action(account, chat, key):
            continue
        base_ts = int(m.get("msg_ts") or time.time())
        # 先看是不是「别再提醒我 X」——取消意图不该再走建库流程
        hit, n = maybe_cancel(q, chat, text, account=account)
        if hit:
            q.record_action(account, chat, key, None, "cancel:%d" % n)
            continue
        # 是不是在回答上一个「几点提醒」的追问
        rid = maybe_answer_clarify(cfg, q, chat, text, account, msg_ts=base_ts,
                                   msg_seq=m.get("sort_seq"), request_key=key)
        if rid:
            created.append(rid)
            continue
        # 含委托词，或者含时间信号（「明天」「两点」「每天」）都放行：
        # 后者交给模型判断"他是不是想让我提醒"——「明天下午我要去打球」没有委托词
        if not looks_worth_judging(text):
            continue
        tz_zone = None
        try:
            tz = reminder_parse.tz_offset(text, base_ts)
            if tz is not None:
                # 他说了「我现在是20:44」「时差8小时」：记住，之后的钟点都按他那边算
                q.set_chat_tz(account, chat, tz)
            else:
                # 「我在英国」这类地名线索交给模型认，记住后按他当地算
                applied = note_tz_hint(cfg, q, chat, text, base_ts, account, rem_cfg)
                tz = applied if applied is not None else q.get_chat_tz(account, chat)
            tz_zone = q.get_chat_zone(account, chat)
        except Exception as e:
            log.warning("时差读取失败 [%s]：%s" % (chat, e))
            tz = None

        # 规则只做粗筛（「这句话像不像提醒委托」），**时间、重复规则、提醒内容
        # 一律由模型给**——系统只负责把它记进定时提醒，到点再把信息交给模型
        # 组织发送，不再自己截内容、拼模板。图一起给，课表里的时间只能从图里读。
        try:
            kw = {"images": images} if images else {}
            r = judge_with_llm(cfg, text, base_ts, tz=tz, tz_zone=tz_zone, **kw)
        except Exception as e:
            log.warning("提醒判定异常 [%s]：%s: %s"
                        % (chat, type(e).__name__, e))
            r = None
        if r is None:
            # 模型不可用：先入候选（连图一起存），等它恢复再判定；不拿规则结果顶替
            if q.add_candidate(account, chat, m.get("id"), text, base_ts,
                               images=images):
                log.info("提醒判定失败，入候选等重试 [%s]：%s"
                         % (chat, redact(text[:40], cfg)))
            continue
        off = _apply_llm_tz(q, account, chat, r)
        zone = r.get("tz") or tz_zone
        if not r.get("is_reminder"):
            continue
        plan = plan_decision(r, rem_cfg, bool(r.get("used_images", images)),
                             text, (tz if off is None else off) or 0, zone)
        if plan["action"] == "skip":
            log.info("提醒把握不足（%.2f），不建单 [%s]"
                     % (r.get("confidence", 0), chat))
            continue
        content = plan["content"] or text[:20]
        try:
            with q.tx() as conn:
                if q.has_action(account, chat, key, conn=conn):
                    continue
                if plan["action"] == "clarify":
                    cid = q.add_clarify(account, chat, content, base_ts, plan["ask"],
                                        rule=plan["rule"], proposed_ts=plan["proposed_ts"],
                                        images=images, source_text=text, tz_zone=zone, conn=conn)
                    q.record_action(account, chat, key, None, "clarify_created", conn=conn)
                    log.info("提醒待确认 #%d [%s]：%s" % (cid, chat, redact(plan["ask"], cfg)))
                    continue
                rid = q.add_reminder(account, chat, content, plan["rule"], plan["ts"],
                                     anchor_ts=plan["ts"], conn=conn,
                                     short_delay=bool(r.get("short_delay")), tz_zone=zone)
                q.record_action(account, chat, key, rid, "llm_created", conn=conn)
        except Exception as e:
            log.error("写入提醒失败 [%s]：%s" % (chat, e))
            continue
        created.append(rid)
        log.info("记下提醒 #%d [%s]：%s（%s @ %s，模型判定）" % (
            rid, chat, redact(content, cfg), plan["rule"],
            time.strftime("%m-%d %H:%M:%S", time.localtime(plan["ts"]))))
    prompt_id = enqueue_clarify_prompt(q, account, chat)
    if prompt_id is not None and prompt_outbounds is not None:
        prompt_outbounds.append(prompt_id)
    return created


def send_block_reason(q, cfg, task: dict) -> str | None:
    """关联提醒已编辑、暂停或取消时，原有出站任务即刻失效。"""
    expiry = task.get("expires_ts")
    if not expiry or time.time() >= float(expiry):
        return "提醒已过期，判定错过"
    occurrence = q.get_occurrence(task["ref_id"]) if task.get("ref_id") else None
    if not occurrence or occurrence.get("outbound_id") != task["id"]:
        return "找不到对应的提醒计划"
    if occurrence["status"] not in ("queued", "uncertain"):
        return "该次提醒已结案"
    reminder = q.get_reminder(occurrence["reminder_id"])
    if not reminder or reminder["account"] != task["account"] or reminder["chat"] != task["chat"]:
        return "提醒与发送账号或会话不一致"
    if reminder["status"] not in ("active", "done") or reminder["revision"] != occurrence["revision"]:
        return "提醒已暂停、取消或改期"
    return None


# ------------------------------------------------------------------ 触发
def fire_due(q, cfg, now: float | None = None, limit: int = 5) -> list[int]:
    """到点提醒 → 出站队列。返回本次排期的 occurrence id 列表。"""
    now = float(time.time() if now is None else now)
    rem_cfg = ((cfg.get("proactive") or {}).get("reminders") or {})
    if not rem_cfg.get("enabled", False):
        return []                      # 开关关着就完全不动，连错过都不记
    grace = int(rem_cfg.get("grace_seconds", 7200))
    short_grace = int(rem_cfg.get("short_grace_seconds", 60))
    fired: list[int] = []

    for rem in q.due_reminders(now, limit, account=cfg.get("account")):
        scheduled = int(rem.get("next_ts") or 0)
        if not scheduled:
            continue
        # 秒级倒计时给 60 秒宽限，常规给 2 小时
        g = short_grace if rem.get("short_delay") else grace
        late = (now - scheduled) > 60
        expired = now >= scheduled + g
        try:
            zone = rem.get("tz_zone") or q.get_chat_zone(rem["account"], rem["chat"])
            tz = reminder_parse.tz_from_zone(zone, scheduled)
            if tz is None:
                # 旧记录没有时区名：退回联系人当前时差（迁移已把 rule 对齐）
                tz = int(q.get_chat_tz(rem.get("account"), rem.get("chat")) or 0)
        except Exception:
            zone = None
            tz = 0
        try:
            with q.tx() as conn:
                row = conn.execute(
                    "SELECT * FROM reminders WHERE id=? AND revision=? AND next_ts=? "
                    "AND status='active'",
                    (rem["id"], rem["revision"], scheduled)).fetchone()
                if not row:
                    continue
                if reminder_parse.valid_rule(rem.get("rule")) is None:
                    # 坏规则（模型回「weekly:周一」这种）没法推进，也不能让它
                    # 一直占着调度名额，直接停用，人工改好再恢复。
                    conn.execute(
                        "UPDATE reminders SET status='paused', updated_ts=? WHERE id=?",
                        (int(now), rem["id"]))
                    log.warning("提醒 #%d [%s] 周期规则非法（%s），已停用"
                                % (rem["id"], rem["chat"], rem.get("rule")))
                    continue
                occ_id = q.add_occurrence(conn, rem["id"], scheduled, rem["revision"])
                nxt = reminder_parse.advance(rem.get("rule"), rem.get("anchor_ts"),
                                              scheduled, tz, zone)
                if nxt is not None and rem.get("until_ts") and nxt > rem["until_ts"]:
                    nxt = None
                new_status = "done" if nxt is None else "active"
                conn.execute(
                    "UPDATE reminders SET next_ts=?, status=?, "
                    "updated_ts=? WHERE id=? AND revision=?",
                    (nxt, new_status, int(now), rem["id"], rem["revision"]))
                if expired:
                    conn.execute(
                        "UPDATE reminder_occurrences SET status='missed', reason=? "
                        "WHERE id=?", ("超过宽限，未发送", occ_id))
                    log.warning("错过提醒 #%d [%s]：%s" % (
                        rem["id"], rem["chat"], redact(rem.get("content"), cfg)))
                    continue
                text = build_text(rem, scheduled, late=late, tz=tz)
                segs = segments_for(text, cfg)
                ob_id = q.create_outbound(
                    row["account"], row["chat"], text, status="draft",
                    kind="reminder", ref_id=occ_id,
                    expires_ts=scheduled + g, segments=segs, conn=conn)
                # 模板只是占位，标出来：发送线程只认 model/manual/legacy，
                # 这样「text_source 为 NULL」就只可能来自旧库，迁移才好认。
                source = "model" if rem.get("confirm_mode") == "relay_back" else "template"
                conn.execute("UPDATE outbound SET text_source=? WHERE id=?", (source, ob_id))
                conn.execute(
                    "UPDATE reminder_occurrences SET outbound_id=? WHERE id=?",
                    (ob_id, occ_id))
            fired.append(occ_id)
            log.info("提醒到点 #%d -> 出站 #%d [%s]：%s" % (
                rem["id"], ob_id, rem["chat"], redact(text[:40], cfg)))
        except Exception as e:
            log.error("提醒触发失败 #%s：%s: %s" % (rem.get("id"),
                                                   type(e).__name__, e))
    return fired


# ------------------------------------------------------------------ 模型判定
def process_candidates(q, cfg, limit: int = 2) -> int:
    """异步处理候选队列：让模型判定「这是不是提醒委托」。返回新建条数。

    跑在独立线程里，不阻塞回复；每次只取 limit 条，避免一次调太多次模型。
    """
    rem_cfg = ((cfg.get("proactive") or {}).get("reminders") or {})
    if not rem_cfg.get("enabled", False):
        return 0
    n = 0
    cutoff = time.time() - 86400          # 太旧的候选没有提醒价值，直接丢弃
    for c in q.pending_candidates(limit, account=cfg.get("account")):
        account, chat, key = c["account"], c["chat"], str(c["msg_id"])
        # 候选判定走提醒入口门禁（只看提醒开关/黑名单），不受 auto_reply 管：
        # 关着自动回复时，「明天下午两点左右叫醒我」这种规则吃不准的委托
        # 仍要交给模型判一下，否则候选队列永远不动。
        allowed, reason = gate.reminder_entry_ok(cfg, c["chat"])
        if not allowed or q.is_paused(c["chat"]):
            q.set_candidate(c["id"], "skipped", reason or "会话暂停")
            continue
        if int(c["msg_ts"] or 0) < cutoff:
            q.set_candidate(c["id"], "skipped", "超过 24 小时，忽略")
            continue
        if q.has_action(account, chat, key):
            q.set_candidate(c["id"], "done", "入站已处理")
            continue
        try:
            tz = q.get_chat_tz(account, chat)
            zone = q.get_chat_zone(account, chat)
            try:
                cand_images = json.loads(c.get("images") or "[]")
            except Exception:
                cand_images = []
            kw = {"images": cand_images} if cand_images else {}
            r = judge_with_llm(cfg, c["text"], c["msg_ts"], tz=tz, tz_zone=zone, **kw)
        except Exception as e:
            log.warning("提醒判定异常：%s: %s" % (type(e).__name__, e))
            q.retry_candidate(c["id"], str(e)[:100])
            continue
        if r is None:
            q.retry_candidate(c["id"], "模型不可用")
            continue
        # 模型判断「对方在哪」→ IANA 时区名落库，并按目标时刻重算时间
        off = _apply_llm_tz(q, account, chat, r)
        zone = r.get("tz") or zone
        if not r["is_reminder"]:
            q.set_candidate(c["id"], "skipped", "不是提醒委托")
            continue
        plan = plan_decision(r, rem_cfg, bool(r.get("used_images", cand_images)),
                             c["text"], (tz if off is None else off) or 0, zone)
        if plan["action"] == "skip":
            q.set_candidate(c["id"], "skipped", plan.get("reason") or "不建单")
            continue
        try:
            content = plan["content"] or c["text"][:20]
            with q.tx() as conn:
                if not q.is_candidate_pending(c["id"], conn):
                    continue
                if q.has_action(account, chat, key, conn=conn):
                    q.set_candidate(c["id"], "done", "入站已处理", conn=conn)
                    continue
                if plan["action"] == "clarify":
                    cid = q.add_clarify(account, chat, content, c["msg_ts"], plan["ask"],
                                        rule=plan["rule"], proposed_ts=plan["proposed_ts"],
                                        images=cand_images, source_text=c["text"],
                                        tz_zone=zone, conn=conn)
                    q.record_action(account, chat, key, None, "clarify_created", conn=conn)
                    q.set_candidate(c["id"], "done", "待确认 #%d" % cid, conn=conn)
                    enqueue_clarify_prompt(q, account, chat, conn=conn)
                    log.info("提醒待确认 #%d [%s]：%s" % (cid, chat, redact(plan["ask"], cfg)))
                    continue
                rid = q.add_reminder(account, chat, content, plan["rule"], plan["ts"],
                                     anchor_ts=plan["ts"], conn=conn, tz_zone=zone,
                                     short_delay=bool(r.get("short_delay")))
                q.record_action(account, chat, key, rid, "llm_created", conn=conn)
                q.set_candidate(c["id"], "done", "建了 #%d" % rid, conn=conn)
        except Exception as e:
            # 事务回滚了：提醒没建、候选还挂着。保持 pending 让下一轮重试，
            # 别标成 error 把这条委托永久丢掉。
            log.warning("候选建单失败，保持待处理 [%s]：%s: %s"
                        % (c["chat"], type(e).__name__, e))
            continue
        log.info("模型判定建提醒 #%d [%s]：%s（%s @ %s，置信 %.2f）" % (
            rid, c["chat"], redact(plan["content"], cfg), plan["rule"],
            time.strftime("%m-%d %H:%M", time.localtime(plan["ts"])),
            r["confidence"]))
        n += 1
    return n


# ------------------------------------------------------------------ 对账
_OCC_FROM_OB = {
    "sent": "sent",
    "expired": "missed",
    "cancelled": "cancelled",
    "uncertain": "uncertain",
    "unverified": "unverified",
    "failed": "missed",
}


def sync_occurrences(q, limit: int = 50, account: str | None = None) -> int:
    """把出站任务的最终状态回写到到点记录（提醒发出后才有 sent）。"""
    rows = q.pending_occurrences(limit, account=account)
    n = 0
    for occ in rows:
        ob = q.get_outbound(int(occ["outbound_id"])) if occ.get("outbound_id") else None
        if not ob:
            continue
        target = _OCC_FROM_OB.get(ob.get("status"))
        if not target or occ["status"] == target:
            continue
        if ob["status"] == "failed" and ob["retryable"] and ob["attempts"] < 3:
            continue
        q.set_occurrence_status(occ["id"], target,
                                reason=(ob.get("error") or "")[:120],
                                sent_ts=ob.get("sent_ts"))
        n += 1
    return n
