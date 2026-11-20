# -*- coding: utf-8 -*-
"""中文提醒解析：把「明天早上八点提醒我跑步」变成结构化任务。

设计原则
--------
* **纯函数**：不碰数据库、不调模型，好测也跑得快。
* **宁可漏，不可错**：解析不出来的返回 ``None``，交给上层决定问模型还是反问。
* **基准时间是消息本身**：``base_ts`` 传该条入站消息的 ``msg_ts``，不是 ``now``。
  合并等待可能拖十几秒，「20 秒后」必须按原消息时间算。

返回结构::

    {"content": "跑步", "rule": "single", "next_ts": 1795...,
     "anchor_ts": 1795..., "until_ts": None, "span": [(0,8),(10,12)],
     "need_clarify": False, "hard": True}
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta

# 委托信号：没有这些词就不当提醒委托（避免「我记得你」这种误判）
# 长词在前，否则「记得提醒我」会被「记得」先吃掉，残留一个「我」在内容里
COMMISSION = re.compile(
    r"(记得提醒我|提醒一下我|提醒下我|提醒我|记得提醒|别忘了|别忘记|别忘|"
    r"叫我去|叫我起来|叫我|帮我记|帮我提醒|说好了|到时候|定个闹钟|设个闹钟|记得)"
)

# 时段词 → 没写钟点时的默认小时
PERIOD_HOUR = (
    ("凌晨", 5), ("早上", 8), ("早晨", 8), ("上午", 9), ("中午", 12),
    ("下午", 14), ("傍晚", 18), ("晚上", 20), ("夜里", 22), ("半夜", 23),
)

CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
      "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
WEEKDAY = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}

NUM = r"(\d{1,2}|[零一二两三四五六七八九十]{1,3})"

_CLOCK = re.compile(
    r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|今晚|明晚|今早|明早|夜里|半夜)?\s*"
    + NUM + r"\s*[点时]\s*"
    r"(半|\d{1,2}|[零一二两三四五六七八九十]{1,3})?\s*分?"
)

_MD = re.compile(NUM + r"月" + NUM + r"[日号]")

_DAY_WORDS = (("大后天", 3), ("后天", 2), ("明早", 1), ("明晚", 1),
              ("明天", 1), ("明日", 1), ("今晚", 0), ("今天", 0), ("今日", 0))

_REL = re.compile(
    r"(\d{1,4}|[零一二两三四五六七八九十]{1,3}|半)\s*"
    r"(秒钟|秒|分钟|分|个?小时|钟头)\s*(?:以?后|之后|后)")

_MAX_DAYS = 365
_MIN_LEAD = 10          # 至少 10 秒之后，否则视为已过期


# ------------------------------------------------------------------ 小工具
def normalize(text: str) -> str:
    """全角转半角、统一冒号、压缩空白。"""
    if not text:
        return ""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out).strip())


def to_int(tok: str) -> int | None:
    """阿拉伯数字或中文数字 → int。"""
    tok = (tok or "").strip()
    if not tok:
        return None
    if tok.isdigit():
        return int(tok)
    if tok == "十":
        return 10
    if len(tok) == 1:
        return CN.get(tok)
    if len(tok) == 2 and tok[0] == "十":
        rest = CN.get(tok[1])
        return 10 + rest if rest is not None else None
    if len(tok) == 2 and tok[1] == "十":
        head = CN.get(tok[0])
        return head * 10 if head is not None else None
    if len(tok) == 3 and tok[1] == "十":
        a, b = CN.get(tok[0]), CN.get(tok[2])
        if a and b is not None:
            return a * 10 + b
    return None


def _clock_of(text: str):
    """返回 (hour, minute, span)；只有时段词没钟点时用该时段默认值。"""
    m = _CLOCK.search(text)
    if m:
        period = m.group(1)
        h = to_int(m.group(2))
        if h is not None and 0 <= h <= 24:
            mm_tok = m.group(3)
            mm = 0
            if mm_tok == "半":
                mm = 30
            elif mm_tok:
                v = to_int(mm_tok)
                if v is not None and 0 <= v <= 59:
                    mm = v
            if period in ("下午", "傍晚", "晚上", "今晚", "明晚", "夜里", "半夜") and h < 12:
                h += 12
            elif period == "中午" and h < 11:
                h += 12
            elif period == "凌晨" and h == 12:
                h = 0
            return h % 24, mm, m.span()
    for word, hour in PERIOD_HOUR:
        m = re.search(word, text)
        if m:
            return hour, 0, m.span()
    return None


def _day_offset(text: str):
    for word, off in _DAY_WORDS:
        if word in text:
            m = re.search(word, text)
            return off, m.span()
    return None, None


def _rule_of(text: str):
    """周期规则；不匹配返回 None。"""
    m = re.search(r"每\s*(天|日|晚)", text)
    if m:
        return "daily", m.span()
    m = re.search(r"每\s*周\s*([一二三四五六日天]+)", text)
    if m:
        days = sorted({WEEKDAY[ch] for ch in m.group(1) if ch in WEEKDAY})
        if days:
            return "weekly:" + ",".join(str(d) for d in days), m.span()
    m = re.search(r"每\s*(\d{1,3})\s*天", text)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 365:
            return "interval:%d" % n, m.span()
    m = re.search(r"每\s*月\s*(\d{1,2})\s*[日号]", text)
    if m:
        d = int(m.group(1))
        if 1 <= d <= 31:
            return "monthly:%d" % d, m.span()
    return None


def _merge_spans(spans) -> list:
    """合并重叠区间（「今晚十一点」会同时被日期词和时段词命中）。"""
    ss = sorted([s for s in spans if s], key=lambda x: x[0])
    out: list = []
    for s in ss:
        if out and s[0] <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], s[1]))
        else:
            out.append((s[0], s[1]))
    return out


def _content_of(text: str, spans) -> str:
    """去掉时间表达式和委托动词，剩下的就是「提醒什么」。"""
    s = text
    for sp in reversed(_merge_spans(spans)):
        s = s[:sp[0]] + " " + s[sp[1]:]
    s = COMMISSION.sub(" ", s)
    s = re.sub(r"[，,。.!！、\s]+", " ", s).strip()
    s = re.sub(r"^(记得|别忘了|别忘|要|去|一下)\s*", "", s)
    s = re.sub(r"^我(?:想|要|得|该|需要|先)?\s*", "", s)
    s = re.sub(r"(记得|别忘了|别忘|啊|呀|吧|哦|哈|嘛|了)\s*$", "", s).strip()
    s = re.sub(r"\[[^\[\]]{1,8}\]", " ", s)          # 微信表情转文字留的标记
    s = re.sub(r"\s+", " ", s).strip(" ，,。、")
    return s[:60]


# ------------------------------------------------------------------ 推进
def _next_recurring(rule: str, hour: int, minute: int, base: datetime) -> int | None:
    """按规则算出「第一次」到点（严格晚于 base）。"""
    if rule == "daily":
        dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= base:
            dt += timedelta(days=1)
        return int(dt.timestamp())

    if rule.startswith("weekly:"):
        days = {int(x) for x in rule.split(":")[1].split(",") if x}
        for i in range(0, 9):
            d = base + timedelta(days=i)
            if d.isoweekday() in days:
                dt = d.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if dt > base:
                    return int(dt.timestamp())
        return None

    if rule.startswith("interval:"):
        n = max(1, int(rule.split(":")[1]))
        dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= base:
            dt += timedelta(days=n)
        return int(dt.timestamp())

    if rule.startswith("monthly:"):
        day = int(rule.split(":")[1])
        for i in range(0, 14):
            y, m = base.year, base.month + i
            while m > 12:
                m -= 12
                y += 1
            if day <= calendar.monthrange(y, m)[1]:
                dt = datetime(y, m, day, hour, minute)
                if dt > base:
                    return int(dt.timestamp())
        return None
    return None


def advance(rule: str, anchor_ts: int | None, last_scheduled: int) -> int | None:
    """算出下一个到点（严格晚于 ``last_scheduled``）。

    用 ``anchor_ts`` 而不是「上次实际发送时间」推进——延迟发送不该改变钟点。
    """
    if not rule or rule == "single" or anchor_ts is None:
        return None
    anchor = datetime.fromtimestamp(int(anchor_ts))
    last = int(last_scheduled)
    if int(anchor_ts) > last:
        return int(anchor_ts)
    last_day = datetime.fromtimestamp(last)
    elapsed_days = (last_day.date() - anchor.date()).days

    if rule == "daily" or rule.startswith("interval:"):
        step = 1 if rule == "daily" else max(1, int(rule.split(":")[1]))
        cur = anchor + timedelta(days=(elapsed_days // step) * step)
        if int(cur.timestamp()) <= last:
            cur += timedelta(days=step)
        return int(cur.timestamp())

    if rule.startswith("weekly:"):
        days = {int(x) for x in rule.split(":")[1].split(",") if x}
        cur = anchor + timedelta(days=elapsed_days)
        for _ in range(8):
            if cur.isoweekday() in days and int(cur.timestamp()) > last:
                return int(cur.timestamp())
            cur += timedelta(days=1)
        return None

    if rule.startswith("monthly:"):
        day = int(rule.split(":")[1])
        y, m = last_day.year, last_day.month
        for _ in range(2):
            dim = calendar.monthrange(y, m)[1]
            cur = anchor.replace(year=y, month=m, day=min(day, dim))
            if int(cur.timestamp()) > last:
                return int(cur.timestamp())
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return None
    return None


def parse_clock(text: str):
    """只解析钟点，返回 (hour, minute)；认不出返回 ``None``。

    给「对方回答几点」用：「三点」「下午三点」「15:30」。
    """
    c = _clock_of(normalize(text))
    return (c[0], c[1]) if c else None


# ------------------------------------------------------------------ 主入口
def detect(text: str, base_ts: int, now_ts: float | None = None) -> dict | None:
    """解析一条消息。返回任务描述，或 ``None``（不是提醒 / 解析不了）。"""
    import time as _t

    t = normalize(text)
    if not t or len(t) > 200:
        return None
    if not COMMISSION.search(t):
        return None

    now = float(now_ts if now_ts is not None else _t.time())
    base = datetime.fromtimestamp(int(base_ts))

    # ---- 1) 周期提醒（每天 / 每周X / 每N天 / 每月X号）
    rl = _rule_of(t)
    if rl:
        rule, rspan = rl
        clock = _clock_of(t)
        if not clock:
            return {"content": _content_of(t, [rspan]), "rule": rule,
                    "next_ts": None, "anchor_ts": None, "until_ts": None,
                    "span": [rspan], "need_clarify": True, "hard": True}
        hour, minute, cspan = clock
        nxt = _next_recurring(rule, hour, minute, base)
        content = _content_of(t, [rspan, cspan])
        if not content or not nxt:
            return None
        return {"content": content, "rule": rule, "next_ts": nxt,
                "anchor_ts": nxt, "until_ts": None,
                "span": [rspan, cspan], "need_clarify": False, "hard": True}

    # ---- 2) 相对时间（N 秒 / 分钟 / 小时后；支持「两分钟」「半小时」）
    m = _REL.search(t)
    if m:
        tok = m.group(1)
        unit = m.group(2)
        n = 0.5 if tok == "半" else to_int(tok)
        if n is not None:
            if "秒" in unit:
                delta = n
            elif "分" in unit:
                delta = n * 60
            else:
                delta = n * 3600
            nxt = int(base.timestamp() + delta)
            if nxt < now + 1:                   # 消息在队列里躺太久
                nxt = int(now + 1)
            content = _content_of(t, [m.span()])
            if content:
                return {"content": content, "rule": "single", "next_ts": nxt,
                        "anchor_ts": nxt, "until_ts": None, "span": [m.span()],
                        "need_clarify": False, "hard": True,
                        "short_delay": "秒" in unit}

    # ---- 3) 绝对日期 / 时刻
    day_off, dspan = _day_offset(t)
    md = _MD.search(t)
    clock = _clock_of(t)

    if clock is None and day_off is None and md is None:
        return None

    hour = minute = None
    cspan = None
    if clock:
        hour, minute, cspan = clock

    dt = None
    if md:
        mo = to_int(md.group(1))
        da = to_int(md.group(2))
        if mo and da and 1 <= mo <= 12 and 1 <= da <= 31:
            try:
                dt = datetime(base.year, mo, da, hour or 8, minute or 0)
            except ValueError:
                return None
            if dt <= base:
                try:
                    dt = dt.replace(year=base.year + 1)
                except ValueError:
                    return None
    elif day_off is not None:
        day = base + timedelta(days=day_off)
        if hour is None:
            return {"content": _content_of(t, [dspan]), "rule": "single",
                    "next_ts": None, "anchor_ts": None, "until_ts": None,
                    "span": [dspan], "need_clarify": True, "hard": True}
        dt = day.replace(hour=hour, minute=minute or 0, second=0, microsecond=0)
    elif hour is not None:
        dt = base.replace(hour=hour, minute=minute or 0, second=0, microsecond=0)
        if dt <= base:
            dt += timedelta(days=1)

    if dt is None:
        return None

    nxt = int(dt.timestamp())
    if nxt < now + _MIN_LEAD:
        return None
    if nxt > now + _MAX_DAYS * 86400:
        return None

    content = _content_of(t, [dspan, cspan, md.span() if md else None])
    if not content:
        return None
    return {"content": content, "rule": "single", "next_ts": nxt,
            "anchor_ts": nxt, "until_ts": None,
            "span": [dspan, cspan, md.span() if md else None],
            "need_clarify": False, "hard": True}


# ------------------------------------------------------------------ 自查
if __name__ == "__main__":
    import sys
    import time as _t

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    now = _t.time()
    cases = [
        "20 秒后提醒我喝水",
        "明天早上提醒我去跑步",
        "明天晚上八点叫我去跑步",
        "每天早上七点提醒我吃药",
        "每周一三五晚上八点提醒我健身",
        "后天下午三点提醒我交作业",
        "12月5日上午十点提醒我开会",
        "记得提醒我八点开会",
        "今晚十一点提醒我关电脑",
        "我记得你上周说过这事",
        "明天再说吧",
    ]
    for c in cases:
        r = detect(c, int(now), now)
        if r is None:
            print("  跳过 | %s" % c)
            continue
        if r.get("need_clarify"):
            print("  要问 | %-24s 内容=%s 规则=%s" % (c, r["content"], r["rule"]))
            continue
        when = _t.strftime("%m-%d %H:%M:%S", _t.localtime(r["next_ts"]))
        print("  %-4s | %-24s -> %s  内容=%s" %
              ("单次" if r["rule"] == "single" else "循环", c, when, r["content"]))
