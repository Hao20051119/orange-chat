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
    r"(记得提醒我|提醒我一下|提醒我一遍|提醒我下|提醒一下我|提醒下我|提醒我|"
    r"记得提醒|别忘了|别忘记|别忘|叫我去|叫我|喊我|叫醒我|喊醒我|"
    r"帮我记|帮我提醒|说好了|到时候|定个闹钟|设个闹钟|记得)"
)
# 纯「叫我/喊我」这类没有具体事项的委托：只说了几点叫醒，没有「提醒什么」
_WAKE = re.compile(r"(叫我|喊我|叫醒|喊醒)")

# 时段词 → 没写钟点时的默认小时
PERIOD_HOUR = (
    ("凌晨", 5), ("早上", 8), ("早晨", 8), ("上午", 9), ("中午", 12),
    ("下午", 14), ("傍晚", 18), ("晚上", 20), ("夜里", 22), ("半夜", 23),
)
# 时段词全集（_CLOCK 的 period 组里有今早/明早这类，PERIOD_HOUR 里没有）
_PERIOD_WORDS = tuple(w for w, _ in PERIOD_HOUR) + ("今晚", "明晚", "今早", "明早")

CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
      "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
WEEKDAY = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}

NUM = r"(\d{1,2}|[零一二两三四五六七八九十]{1,3})"

_CLOCK = re.compile(
    r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|今晚|明晚|今早|明早|夜里|半夜)?\s*"
    + NUM + r"\s*[点时]\s*"
    r"(半|\d{1,2}|[零一二两三四五六七八九十]{1,3})?\s*分?"
)

# 「20:50」「10：05」这种写法：整点在 [点时] 里认不出，专门补一条（「我现在是
# 20:44，请20:50提醒我」这类跨时区委托全靠它）。
_CLOCK_HM = re.compile(
    r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|今晚|明晚|今早|明早|夜里|半夜)?\s*"
    r"(\d{1,2})\s*[:：]\s*(\d{2})"
)

# ---------------------------------------------------------------- 时区
# 对方当地时间 - 主机本地时间（秒）。英国 20:44 / 北京 04:44 → -8h。
_TZ_GAP = re.compile(
    r"(?:中英|中美|中日|中韩|中澳|中法|中德|和国内|跟国内)?\s*"
    r"时差\s*(?:是|有)?\s*(\d{1,2})\s*(?:个)?\s*(?:小时|h|H)")
# 「我这边比国内晚 8 个小时」：没有「时差」两个字，方向写在词里
_TZ_GAP2 = re.compile(
    r"(?:我)?\s*这边?\s*(?:现在)?\s*比\s*(?:国内|北京|中国|你们那边?|你那边)\s*"
    r"(早|晚|快|慢)\s*(\d{1,2})\s*(?:个)?\s*(?:小时|h|H)")
_TZ_AHEAD = re.compile(r"(快|早|提前|领先)")
_TZ_BEHIND = re.compile(r"(慢|晚|落后|推迟)")

_NOW_CN = (
    re.compile(r"我现在\s*(?:这边)?\s*(?:是|为|当地时间)?\s*(\d{1,2})\s*[:：]\s*(\d{2})"),
    re.compile(r"我这边\s*(?:现在)?\s*(?:是)?\s*(\d{1,2})\s*[:：]\s*(\d{2})"),
    re.compile(r"这边\s*(?:现在)?\s*(?:是)?\s*(\d{1,2})\s*[:：]\s*(\d{2})"),
)

# 解析前先摘掉「时差/我现在几点/请/的时候」这些叙述，别让它们变成提醒内容
_TZ_NOISE = (
    _TZ_GAP, _TZ_GAP2,
    _NOW_CN[0], _NOW_CN[1], _NOW_CN[2],
    re.compile(r"(?:的时间?)?的时候"),
    re.compile(r"(?:你)?(?:记住|记着|记一下)[，,、 ]*"),
    re.compile(r"请\s*"),
)

_MD = re.compile(NUM + r"月" + NUM + r"[日号]")

_DAY_WORDS = (("大后天", 3), ("后天", 2), ("明早", 1), ("明晚", 1),
              ("明天", 1), ("明日", 1), ("今晚", 0), ("今天", 0), ("今日", 0))

# 一次性的星期词：「周一 / 这周三 / 下周一 / 下下周日」。只在没有「每」时生效
# （周期写法由 _rule_of 先吃掉），否则「下周一早八点」会退化成「下一个八点」。
_WEEKDAY_WORD = re.compile(
    r"(下下|下|这个?|本|上)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])")

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


def _apply_period(period: str | None, h: int) -> int:
    """「下午三点」→ 15 点；「凌晨十二点」→ 0 点。"""
    if period in ("下午", "傍晚", "晚上", "今晚", "明晚", "夜里", "半夜") and h < 12:
        h += 12
    elif period == "中午" and h < 11:
        h += 12
    elif period == "凌晨" and h == 12:
        h = 0
    return h % 24


def _clock_of(text: str, before: int | None = None):
    """返回 (hour, minute, span)；只有时段词没钟点时用该时段默认值。

    ``before`` 给定时优先选它**前面最近**的钟点：一句话里常有两个时间
    （「我明天三点半开会，三点叫我」），委托词前面的那个才是要提醒的点。
    """
    found = []
    for m in _CLOCK_HM.finditer(text):
        period, h, mm = m.group(1), to_int(m.group(2)), to_int(m.group(3))
        if h is not None and 0 <= h <= 24 and mm is not None and 0 <= mm <= 59:
            found.append((_apply_period(period, h), mm, m.span()))
    for m in _CLOCK.finditer(text):
        period = m.group(1)
        h = to_int(m.group(2))
        if h is None or not 0 <= h <= 24:
            continue
        mm_tok = m.group(3)
        mm = 0
        if mm_tok == "半":
            mm = 30
        elif mm_tok:
            v = to_int(mm_tok)
            if v is not None and 0 <= v <= 59:
                mm = v
        found.append((_apply_period(period, h), mm, m.span()))
    if found:
        found.sort(key=lambda item: item[2][0])
        if before is not None:
            earlier = [item for item in found if item[2][1] <= before]
            if earlier:
                return earlier[-1]
            later = [item for item in found if item[2][0] >= before]
            if later:
                return later[0]
        return found[0]
    for word, hour in PERIOD_HOUR:
        m = re.search(word, text)
        if m:
            return hour, 0, m.span()
    return None


def _period_near(text: str, pos: int) -> str | None:
    """``pos`` 之前最近的时段词；中间隔着日期词的不要（那是另一天的时段）。

    「我明天下午三点半开会，三点叫我」里第二个「三点」没写「下午」，
    要能继承前面那个「下午」，不然会排到凌晨三点。
    """
    best = None
    for word in _PERIOD_WORDS:
        idx = text.rfind(word, 0, pos)
        if idx < 0:
            continue
        gap = text[idx + len(word):pos]
        if re.search(r"(明天|后天|今天|后天|下周|上周|这周|周[一二三四五六日天])", gap):
            continue
        if best is None or idx > best[0]:
            best = (idx, word)
    return best[1] if best else None


def _day_offset(text: str):
    for word, off in _DAY_WORDS:
        if word in text:
            m = re.search(word, text)
            return off, m.span()
    return None, None


def _weekday_target(text: str, base: datetime):
    """一次性星期词 → ``(目标日期, span, 模式)``。

    模式三态，决定这句话该不该按这个星期排：

    * ``"time"`` —— 是触发时间（「下周一早上八点提醒我交材料」）
    * ``"content"`` —— 是正文的定语（「晚上八点提醒我准备下周一的面试」），
      这个词属于要说的事，不能拿来算日期，也不能从内容里剥掉
    * ``"past"`` —— 「上周一」这种过期时间词，整句不该按现在这套规则建单

    「下周一」= 下一个自然周的周一；「这周一 / 本周一 / 周一」= 未来最近的周一。
    ``base`` 已经是收件人当地时间。
    """
    m = _WEEKDAY_WORD.search(text)
    if not m:
        return None, None, ""
    wd = WEEKDAY.get(m.group(2))
    if wd is None:
        return None, None, ""
    span = m.span()
    if text[m.end():m.end() + 1] == "的":       # 「下周一的面试」是定语
        return None, span, "content"
    prefix = m.group(1) or ""
    if prefix == "上":
        return None, span, "past"
    if prefix in ("下", "下下"):
        weeks = 2 if prefix == "下下" else 1
        this_monday = base.date() - timedelta(days=base.isoweekday() - 1)
        return this_monday + timedelta(days=7 * weeks + wd - 1), span, "time"
    delta = (wd - base.isoweekday()) % 7
    return base.date() + timedelta(days=delta), span, "time"


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


# 内容清理用的三把刀（顺序：开头的委托残渣 → 「我/我该」→ 句尾语气词）
_LEAD_LEGACY = re.compile(r"^(记得|别忘了|别忘|要|去|一下|一遍)\s*")
_LEAD_JUNK = re.compile(
    r"^(?:再|又|就|那|然后|顺便|干脆|还是|接着|帮我|给我|一下|一遍|在|于|该|请)\s*")
_LEAD_ME = re.compile(r"^我(?:想|要|得|该|需要|先|来|去)?\s*")
_TAIL_PARTICLE = re.compile(r"(记得|别忘了|别忘|啊|呀|吧|哦|哈|嘛|了|呢|呐)\s*$")


def _tidy(s: str) -> str:
    """把一段文字清成「提醒什么」：去委托词、去口头残渣、去表情标记。"""
    s = COMMISSION.sub(" ", s)
    s = re.sub(r"[，,。.!！、\s]+", " ", s).strip()
    s = _LEAD_LEGACY.sub("", s)
    # 委托词被吃掉后常留下「再/我就/帮我/一下」这类口头残渣（「提醒我一下」只删
    # 掉了「提醒我」，剩「再一下」），剥到不动为止——它们进了内容就会变成
    # 到点模板里的复读（「凌晨四点五十八分，再 一下 我该去追剧」）。
    for _ in range(4):
        before = s
        s = _LEAD_JUNK.sub("", s)
        s = _LEAD_ME.sub("", s)
        s = _TAIL_PARTICLE.sub("", s).strip()
        if s == before:
            break
    s = re.sub(r"\[[^\[\]]{1,8}\]", " ", s)          # 微信表情转文字留的标记
    return re.sub(r"\s+", " ", s).strip(" ，,。、")[:60]


def _content_of(text: str, spans) -> str:
    """去掉时间表达式和委托动词，剩下的就是「提醒什么」。

    委托词落在句子后半时，**前面的部分是背景，不该进内容**——
    「我以后每周三晚上都会空下来，你记得提醒我去公园跑步」要的是「去公园跑步」，
    不是「以后 都会空下来 你 去公园跑步」。
    """
    m = COMMISSION.search(text)
    if m and m.start() > 0:
        tail = text[m.end():]
        tail = _CLOCK.sub(" ", tail)                 # 委托词后面也可能带钟点
        for word, _off in _DAY_WORDS:
            tail = tail.replace(word, " ")
        tail = _tidy(tail)
        if tail:
            return tail
    s = text
    for sp in reversed(_merge_spans(spans)):
        s = s[:sp[0]] + " " + s[sp[1]:]
    return _tidy(s)


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


_RULE_RX = None


def valid_rule(rule: str) -> str | None:
    """把模型/外部给来的周期规则规范化；非法返回 ``None``。

    模型偶尔会回「weekly:周一」这种写法。直接入库的话，调度每轮都会在这
    几条上抛异常、永远推进不了，把后面的正常提醒堵死。
    """
    r = (rule or "").strip()
    if not r:
        return None
    if r in ("single", "daily"):
        return r
    m = re.fullmatch(r"interval:(\d{1,3})", r)
    if m and 1 <= int(m.group(1)) <= 365:
        return r
    m = re.fullmatch(r"weekly:([1-7](?:,[1-7]){0,6})", r)
    if m:
        days = sorted({int(x) for x in m.group(1).split(",")})
        return "weekly:" + ",".join(str(d) for d in days)
    m = re.fullmatch(r"monthly:(\d{1,2})", r)
    if m and 1 <= int(m.group(1)) <= 31:
        return r
    return None


def advance(rule: str, anchor_ts: int | None, last_scheduled: int,
            tz: int = 0, tz_zone: str | None = None) -> int | None:
    """算出下一个到点（严格晚于 ``last_scheduled``）。

    用 ``anchor_ts`` 而不是「上次实际发送时间」推进——延迟发送不该改变钟点。

    ``tz``：收件人当地时间相对主机的偏移（秒）。星期和日期一律按**收件人
    本地**推进：不然「每周一凌晨一点」在快两小时的联系人那里，第一次是周一
    （本地周一 01:00 = 主机周日 23:00），第二次按主机日期的星期换算就滑到周二。
    """
    if not rule or rule == "single" or anchor_ts is None:
        return None
    if valid_rule(rule) is None:
        # 坏规则（「weekly:周一」这种）直接判结束，别再让调度每轮都卡在它上面
        return None
    tz = int(tz or 0)

    def host(dt: datetime) -> int:
        return to_host_ts(int(dt.timestamp()), tz, tz_zone)

    def _off_at(ts: int) -> int:
        """某个主机时刻对应的当地偏移。还原 anchor/上次到点时必须按当时算，
        不然夏令时一换，把主机时间戳还原成挂钟就会偏一小时。"""
        if tz_zone:
            z = tz_from_zone(tz_zone, int(ts))
            if z is not None:
                return int(z)
        return tz

    anchor = datetime.fromtimestamp(
        int(anchor_ts) + _off_at(int(anchor_ts)))       # 收件人当地
    last = int(last_scheduled)
    if int(anchor_ts) > last:
        return int(anchor_ts)
    last_local = datetime.fromtimestamp(last + _off_at(last))
    elapsed_days = (last_local.date() - anchor.date()).days

    if rule == "daily" or rule.startswith("interval:"):
        step = 1 if rule == "daily" else max(1, int(rule.split(":")[1]))
        cur = anchor + timedelta(days=(elapsed_days // step) * step)
        if host(cur) <= last:
            cur += timedelta(days=step)
        return host(cur)

    if rule.startswith("weekly:"):
        days = {int(x) for x in rule.split(":")[1].split(",") if x}
        cur = anchor + timedelta(days=elapsed_days)
        for _ in range(8):
            if cur.isoweekday() in days and host(cur) > last:
                return host(cur)
            cur += timedelta(days=1)
        return None

    if rule.startswith("monthly:"):
        day = int(rule.split(":")[1])
        y, m = last_local.year, last_local.month
        for _ in range(2):
            dim = calendar.monthrange(y, m)[1]
            cur = anchor.replace(year=y, month=m, day=min(day, dim))
            if host(cur) > last:
                return host(cur)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return None
    return None


def parse_clock(text: str):
    """只解析钟点，返回 (hour, minute)；认不出返回 ``None``。

    给「对方回答几点」用：「三点」「下午三点」「15:30」。
    """
    c = _clock_of(normalize(text))
    return (c[0], c[1]) if c else None


def second_delay(text: str) -> int | None:
    """从明确写出的「N 秒后」取得精确倒计时。"""
    text = normalize(text)
    matches = list(_REL.finditer(text))
    if len(matches) != 1 or "秒" not in matches[0].group(2):
        return None
    delay = matches[0]
    if (_clock_of(text) or _day_offset(text)[0] is not None or _WEEKDAY_WORD.search(text)
            or _MD.search(text) or _rule_of(text)
            or re.search(r"不是|不要|不用|别", text[:delay.start()])):
        return None
    return to_int(delay.group(1))


def clarification_schedule(text: str, source: str, rule: str, now: datetime,
                           original: datetime,
                           proposed: datetime | None = None) -> tuple[str, int] | None:
    """把用户补充的日期、周期、钟点合并进提醒计划。"""
    reply = normalize(text)
    reply = re.sub(r"^(?:不是|不对)[^,，。]*[,，。]\s*(?:是|应该是|改成|改到)?",
                   "", reply)
    explicit_rule = _rule_of(reply)
    clock = _clock_of(reply)
    weekday, weekday_span, weekday_mode = _weekday_target(reply, now)
    day_offset, day_span = _day_offset(reply)
    month_day = _MD.search(reply)
    if weekday_mode == "past":
        return None
    spans = _merge_spans((explicit_rule[1] if explicit_rule else None,
                         clock[2] if clock else None, weekday_span, day_span,
                         month_day.span() if month_day else None))
    rest = reply
    for left, right in reversed(spans):
        rest = rest[:left] + " " + rest[right:]
    rest = re.sub(r"(?:不对|不是|应该是|改成|改到|没错|那就|就行|就好|可以|左右|"
                  r"那天|开始|提前|改|是|的|就|在|吧|啊|呢|哦|好|对|起|[，,。！!？?\s])",
                  "", rest)
    if rest:
        return None
    if not clock:
        if not proposed or not (explicit_rule or weekday is not None or
                                day_offset is not None or month_day):
            return None
        clock = (proposed.hour, proposed.minute)
    hour, minute = clock[:2]
    weekday_word = _WEEKDAY_WORD.fullmatch(reply[slice(*weekday_span)]) if weekday_span else None
    if explicit_rule:
        rule = explicit_rule[0]
        date = None
    elif (weekday is not None and rule.startswith("weekly:") and
          weekday_word and not weekday_word.group(1)):
        rule = "weekly:%d" % weekday.isoweekday()
        date = None
    elif weekday is not None or day_offset is not None or month_day:
        rule = "single"
        date = (now + timedelta(days=day_offset)).date() if day_offset is not None else weekday
    else:
        date = None
        if rule == "single":
            if proposed:
                date = proposed.date()
            else:
                source = normalize(source)
                original_offset, _ = _day_offset(source)
                original_weekday, _, _ = _weekday_target(source, original)
                if original_offset is not None:
                    date = (original + timedelta(days=original_offset)).date()
                elif original_weekday is not None:
                    date = original_weekday
                else:
                    month_day = _MD.search(source)

    if rule != "single":
        target = _next_recurring(rule, hour, minute, now)
        return (rule, target) if target is not None else None
    if month_day and date is None:
        month, day = to_int(month_day.group(1)), to_int(month_day.group(2))
        if month is None or day is None:
            return None
        try:
            dt = datetime(now.year, month, day, hour, minute)
            if dt <= now:
                dt = dt.replace(year=now.year + 1)
        except ValueError:
            return None
    elif date is not None:
        dt = datetime(date.year, date.month, date.day, hour, minute)
        if dt <= now and weekday is not None and weekday_mode == "time":
            dt += timedelta(days=7)
    else:
        target = _next_recurring("daily", hour, minute, now)
        return (rule, target) if target is not None else None
    return (rule, int(dt.timestamp())) if dt > now else None


def to_host_ts(local_ts: int, tz: int = 0, tz_zone: str | None = None) -> int:
    """当地挂钟时刻（秒）→ 主机时间戳。

    有 ``tz_zone`` 时迭代求「那一刻的真实偏移」：夏令时切换当天，第一次猜
    出来的主机时刻可能落在切换点的另一侧，一次查表会差一小时，要收敛两次。
    """
    local_ts = int(local_ts)
    off = int(tz or 0)
    ts = local_ts - off
    if tz_zone:
        for _ in range(3):
            z = tz_from_zone(tz_zone, ts)
            if z is None or z == off:
                break
            off = z
            ts = local_ts - off
    return int(ts)


# ------------------------------------------------------------------ 时区
def tz_from_zone(name: str, at_ts: float | int | None = None) -> int | None:
    """IANA 时区名 → 「对方当地 - 主机」的秒数；名字无效返回 ``None``。

    ``at_ts`` 指定要算哪一刻的偏移（默认现在）。**跨夏令时的排期必须传目标
    时刻**：伦敦冬令时差 8 小时、夏令时差 7 小时，用「现在」的偏移去排三个月
    后的提醒会差一小时。需要 Windows 上的 ``tzdata`` 包，没装就退回落库的秒数。
    """
    name = (name or "").strip()
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        base = datetime.now() if at_ts is None else datetime.fromtimestamp(float(at_ts))
        other_off = base.astimezone(ZoneInfo(name)).utcoffset()
        local_off = base.astimezone().utcoffset() or timedelta(0)
    except Exception:
        return None
    if other_off is None:
        return None
    return int((other_off - local_off).total_seconds())


def tz_offset(text: str, base_ts: int) -> int | None:
    """对方当地时间 - 主机本地时间（秒）；认不出返回 ``None``。

    两条线索：自述的「我现在是 20:44」（跟收到消息的主机钟点对齐，最硬），
    以及「中英时差 8 个小时」（方向词能反推）。都不够就返回 None，
    交给上一条消息存下来的偏移。
    """
    t = normalize(text or "")
    if not t:
        return None
    gap = None
    gap_sign = None
    m = _TZ_GAP.search(t)
    if m:
        h = to_int(m.group(1))
        if h is not None and 0 <= h <= 14:
            gap = h
    else:
        m = _TZ_GAP2.search(t)
        if m:
            h = to_int(m.group(2))
            if h is not None and 0 <= h <= 14:
                gap = h
                gap_sign = -1 if m.group(1) in ("晚", "慢") else 1
    local = None
    for rx in _NOW_CN:
        mm = rx.search(t)
        if mm:
            hh, mi = to_int(mm.group(1)), to_int(mm.group(2))
            if hh is not None and 0 <= hh <= 24 and mi is not None and 0 <= mi < 60:
                local = (hh % 24, mi)
                break
    if local is not None:
        base = datetime.fromtimestamp(int(base_ts))
        diff = (local[0] * 60 + local[1]) - (base.hour * 60 + base.minute)
        diff %= 1440
        if diff > 720:                 # 取「最近的那一圈」：20:44 对 04:44 是 -8h 不是 +16h
            diff -= 1440
        return int(round(diff / 15.0) * 15) * 60
    if gap is None or m is None:
        return None
    if gap_sign is not None:           # 「比国内晚 8 小时」方向就在词里
        return gap_sign * gap * 3600
    around = t[max(0, m.start() - 10): m.end() + 10]
    if _TZ_AHEAD.search(around) and not _TZ_BEHIND.search(around):
        return gap * 3600
    if _TZ_BEHIND.search(around):
        return -gap * 3600
    return None


def _strip_tz_noise(text: str) -> str:
    """把「时差/我现在几点/请/的时候」摘掉，剩下的才是要提醒的事。"""
    s = text
    for rx in _TZ_NOISE:
        s = rx.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# ------------------------------------------------------------------ 主入口
def detect(text: str, base_ts: int, now_ts: float | None = None,
           tz: int | None = None, tz_zone: str | None = None) -> dict | None:
    """解析一条消息。返回任务描述，或 ``None``（不是提醒 / 解析不了）。

    ``tz``：对方当地时间相对主机的偏移（秒）。传 None 时从这句话里自己认
    （「我现在是20:44」「时差8个小时」），认不出就当同一时区。
    ``tz_zone``：对方的 IANA 时区名。有它时，排期用**目标那一刻**的偏移换算，
    而不是「现在」的偏移——跨夏令时的提醒才不会差一小时。
    """
    import time as _t

    t = normalize(text)
    if not t or len(t) > 200:
        return None
    if not COMMISSION.search(t):
        return None

    if tz is None:
        tz = tz_offset(t, base_ts)
    tz = int(tz or 0)
    # 不管有没有时区，「我现在是20:44」「的时候」「请」都不是要提醒的事
    t = _strip_tz_noise(t)
    if not t:
        return None

    now = float(now_ts if now_ts is not None else _t.time())
    # 解析一律在「对方当地」的时间轴上进行，最后再减回主机时间
    base = datetime.fromtimestamp(int(base_ts) + tz)

    def _to_host(local_ts: int) -> int:
        """当地挂钟时刻（秒）→ 主机时间戳。夏令时按目标那一刻的偏移算。"""
        return to_host_ts(int(local_ts), tz, tz_zone)

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
        if nxt is not None:
            nxt = _to_host(nxt)
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
            # 相对时间按**真实经过的秒数**算，不做时区换算：夏令时切换当天
            # 「90 分钟后」就是主机时间 +5400 秒，换成挂钟会少算一小时。
            nxt = int(base_ts + delta)
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
    wd_day, wspan, wd_mode = _weekday_target(t, base)
    if wd_mode == "past":
        # 「上周一早上八点提醒我补交」：时间词已经过期，按剩下的「八点」排
        # 只会排到明天八点，全错。交给候选队列让模型判断。
        return None
    # 一句话里可能有两个钟点（「我明天三点半开会，三点叫我」）：取委托词
    # 前面最近的那个，它才是要提醒的点；没有委托词就退回第一个。
    commission = COMMISSION.search(t)
    clock = _clock_of(t, before=(commission.end() if commission else None))

    if clock is None and day_off is None and md is None and wd_day is None:
        return None

    hour = minute = None
    cspan = None
    if clock:
        hour, minute, cspan = clock
        # 钟点自己没写时段（「…三点叫我」）就继承前面最近的时段词，
        # 免得「下午三点半开会，三点叫我」被排到凌晨三点。
        span_text = t[cspan[0]:cspan[1]]
        if not any(span_text.startswith(w) for w in _PERIOD_WORDS):
            period = _period_near(t, cspan[0])
            if period:
                hour = _apply_period(period, hour)

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
    elif wd_day is not None:
        # 「下周一早上八点」：先定星期几，再补钟点。钟点已过就顺延一周
        # （「周一八点」在周一十点说的话，指下周一）。
        if hour is None:
            return {"content": _content_of(t, [wspan]), "rule": "single",
                    "next_ts": None, "anchor_ts": None, "until_ts": None,
                    "span": [wspan], "need_clarify": True, "hard": True}
        dt = datetime(wd_day.year, wd_day.month, wd_day.day, hour, minute or 0)
        if dt <= base:
            dt += timedelta(days=7)
    elif hour is not None:
        dt = base.replace(hour=hour, minute=minute or 0, second=0, microsecond=0)
        if dt <= base:
            dt += timedelta(days=1)

    if dt is None:
        return None

    nxt = _to_host(int(dt.timestamp()))
    if nxt < now + _MIN_LEAD:
        return None
    if nxt > now + _MAX_DAYS * 86400:
        return None

    # 只有真当触发时间用的星期词才从正文里剥掉；「下周一的面试」是内容
    wspan_time = wspan if wd_day is not None else None
    content = _content_of(t, [dspan, cspan, md.span() if md else None, wspan_time])
    if not content:
        # 只说「三点叫我」——没有「提醒什么」，那是叫醒服务，给个默认内容
        if _WAKE.search(t):
            content = "到点了"
        else:
            return None
    return {"content": content, "rule": "single", "next_ts": nxt,
            "anchor_ts": nxt, "until_ts": None,
            "span": [dspan, cspan, md.span() if md else None, wspan_time],
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
