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

import re
import time
from datetime import datetime, timedelta

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
_HINTS = ("提醒", "记得", "别忘", "叫我", "帮我记", "闹钟", "说好了", "到时候")

# 时间/事件信号：哪怕没有委托词，也可能是「我明天要做某事，你记着点」
_TIME_RE = re.compile(
    r"(\d{1,2}|[一二两三四五六七八九十]{1,3})\s*[点时]"
    r"|\d{1,2}\s*分"
    r"|每天|每周|每月|明天|后天|大后天|下周|下个月|今晚|今早"
    r"|待会|等会|一会儿|马上|回头|到时候"
)

# 取消意图：「别再提醒我跑步」「不用提醒了」
_CANCEL_HINTS = ("别再提醒", "不用再提醒", "不要提醒", "取消提醒", "停止提醒",
                 "别提醒", "不用提醒", "别再叫我", "别再喊我", "别催我")
_CANCEL_RE = re.compile(
    r"(别再|不用再|不要|停止|取消|不用)?\s*(提醒我|提醒我一下|提醒|叫我|喊我|催我)")
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


def build_text(rem: dict, scheduled_ts: int, late: bool = False) -> str:
    """按人设生成一条提醒。同一个提醒固定用同一个模板，别忽冷忽热。"""
    tpl = _TEMPLATES[int(rem.get("id") or 0) % len(_TEMPLATES)]
    text = tpl.format(t=human_clock(scheduled_ts), c=rem.get("content") or "该做的事")
    return ("晚了点，" + text) if late else text


def segments_for(text: str, cfg: dict) -> list[str]:
    send_cfg = cfg.get("send") or {}
    if not send_cfg.get("bubble", True):
        return [text]
    return split_reply(text,
                       max_chars=int(send_cfg.get("bubble_max_chars", 40)),
                       max_bubbles=int(send_cfg.get("max_bubbles", 3))) or [text]


# ------------------------------------------------------------------ 识别
def clean_content(text: str) -> str:
    """清掉微信表情标记和多余空白。"""
    s = _EMOJI_TAG.sub(" ", text or "")
    return re.sub(r"\s+", " ", s).strip(" ，,。、")[:60]


def wants_cancel(text: str) -> bool:
    return any(h in (text or "") for h in _CANCEL_HINTS)


def _cancel_target(text: str) -> str:
    """从「别再提醒我跑步了」里抠出「跑步」。"""
    s = _CANCEL_RE.sub(" ", text or "")
    s = re.sub(r"^(你|我)\s*", "", s.strip())
    s = re.sub(r"(都|了吧|了|啊|呀|吧|哈|嘛|额|，|,|。|！|!|\s)+", " ", s).strip()
    s = re.sub(r"^(你|我)\s*", "", s)
    return clean_content(s)[:20]


def maybe_cancel(q, chat: str, text: str, account: str | None = None) -> tuple[bool, int]:
    """识别「别再提醒我 X」并取消对应提醒。返回 (是否命中取消意图, 取消条数)。

    没有目标（「别再提醒我了」）就取消该会话全部活跃提醒；
    有目标但一条都没匹配上时只记日志，**不会**顺手把别的提醒删掉。
    """
    if not wants_cancel(text):
        return False, 0
    target = _cancel_target(text)
    rows = q.cancellable_reminders(chat, account=account)
    if not rows:
        log.info("收到取消意图但没有活跃提醒 [%s]" % chat)
        return True, 0
    if target:
        hit = [r for r in rows if target and target in (r["content"] or "")]
        if not hit:
            log.info("收到取消意图但没匹配到提醒 [%s]：目标=%s" % (chat, redact(target)))
            return True, 0
    else:
        hit = rows
    n = 0
    for r in hit:
        if q.update_reminder(r["id"], expected_revision=r["revision"], status="cancelled"):
            n += 1
            log.info("已取消提醒 #%d [%s]" % (r["id"], chat))
    return True, n


def looks_commission(text: str) -> bool:
    return any(h in (text or "") for h in _HINTS)


# ------------------------------------------------------------------ 缺钟点反问
def clarify_hint(q, chat: str, account: str | None = None) -> str:
    """给回复生成用的提示：你还有件事没问清钟点。取用一次即标记 asked。"""
    rec = q.get_clarify(chat, "pending", account=account)
    if not rec:
        return ""
    q.set_clarify(rec["id"], "asked")
    ask = rec.get("ask") or "几点"
    return ("【顺带确认】你还没问清「%s」具体几点，可以在这次回复里自然地问一句"
            "（比如「%s？」），别问得太正式，也不用解释为什么问。"
            % (rec.get("content") or "这件事", ask))


def maybe_answer_clarify(cfg, q, chat: str, text: str, account: str) -> int | None:
    """看这条消息是不是在回答「几点提醒」。命中就补建提醒并返回 id。"""
    rec = q.get_clarify(chat, "asked", account=account) or q.get_clarify(chat, "pending", account=account)
    if not rec:
        return None
    body = (text or "").strip()
    if not body or len(body) > 20:
        return None
    clock = reminder_parse.parse_clock(body)
    if not clock:
        return None
    hour, minute = clock
    base = datetime.fromtimestamp(int(rec["base_ts"]))
    dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if dt <= datetime.now():
        dt += timedelta(days=1)
    ts = int(dt.timestamp())
    try:
        rid = q.add_reminder(account, chat, rec["content"], "single", ts,
                             anchor_ts=ts)
    except Exception as e:
        log.error("按澄清补建提醒失败 [%s]：%s" % (chat, e))
        return None
    q.set_clarify(rec["id"], "done")
    log.info("按澄清补建提醒 #%d [%s]：%s @ %s" % (
        rid, chat, rec["content"],
        time.strftime("%m-%d %H:%M", time.localtime(ts))))
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


def judge_with_llm(cfg: dict, text: str, base_ts: int) -> dict | None:
    """让模型判断「这句话是不是提醒委托」，并给出内容、规则、时间。

    返回 ``{"is_reminder", "confidence", "content", "rule", "next_ts",
    "need_time", "ask"}``；模型不可用或输出不可解析时返回 ``None``。

    为什么要有这一层：本地规则只能认「提醒我 明早八点…」这种显式委托，
    认不出「明天下午两点多我要去打球」（没委托词）也补不出「吃早饭=07:30」
    这种常识。语义判断本来就是模型的活。
    """
    try:
        from llm import chat as llm_chat
    except Exception:
        return None
    import json

    now = datetime.fromtimestamp(int(base_ts))
    weekday = "一二三四五六日"[now.isoweekday() - 1]
    prompt = (
        "判断这句话是不是在「托我记住某件事，到时候提醒他」。只输出 JSON，不要解释。\n"
        "当前时间：%s（周%s，北京时间）\n"
        "消息：%s\n"
        "输出：\n"
        '{"is_reminder":true,"confidence":0.0,"content":"提醒什么","rule":"single",'
        '"next":"YYYY-MM-DD HH:MM","need_time":false,"ask":""}\n'
        "判定标准：\n"
        "1. 明说「提醒我 / 记得 / 别忘」→ confidence 0.9 以上\n"
        "2. 只说「我明天下午要去打球」这种自己未来要做的事 → 可能想被提醒，confidence 0.6\n"
        "3. 寒暄、抱怨、过去的事、反问句 → is_reminder=false\n"
        "4. 没说钟点但有明确事件（吃早饭/上班/午睡）→ 按常识补：早饭 07:30、"
        "午饭 12:00、晚饭 18:30、睡前 23:00、上班 08:30、下班 18:00；need_time=false\n"
        "5. 真定不了时间 → need_time=true，next 填日期部分（如 2026-11-21 00:00），"
        "ask 写一句要问他本人的话\n"
        "6. rule 只能取：single | daily | weekly:1,3 | interval:2 | monthly:15\n"
        "7. next 必须晚于当前时间；单次超过 30 天算不合理\n"
        "8. content 只写「要做的事」，去掉时间词和「提醒我/记得/每天」这类字眼：\n"
        "   「明天下午两点多去打球」→「打球」；「每天吃早饭」→「吃早饭」\n"
    ) % (now.strftime("%Y-%m-%d %H:%M"), weekday, text[:200])
    try:
        r = llm_chat([{"role": "user", "content": prompt}], cfg, timeout=30)
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

    out = {
        "is_reminder": bool(obj.get("is_reminder")),
        "confidence": float(obj.get("confidence") or 0),
        "content": str(obj.get("content") or "").strip()[:60],
        "rule": str(obj.get("rule") or "single").strip() or "single",
        "next_ts": None,
        "need_time": bool(obj.get("need_time")),
        "ask": str(obj.get("ask") or "").strip()[:60],
    }
    next_s = str(obj.get("next") or "").strip()
    if next_s:
        try:
            ts = int(datetime.strptime(next_s, "%Y-%m-%d %H:%M").timestamp())
            now_ts = time.time()
            # need_time 时模型只给日期（当天 00:00 可能已过），放宽下界
            lo = now_ts - 86400 if out["need_time"] else now_ts + 5
            if lo < ts < now_ts + 366 * 86400:
                out["next_ts"] = ts
        except Exception:
            pass
    return out


def maybe_capture(cfg: dict, q, chat: str, msgs: list[dict],
                  account: str) -> list[int]:
    """扫描一轮入站消息，把明确的提醒委托写进提醒库。

    幂等：同一条入站消息（``inbound.id``）只会处理一次。
    返回新建的 reminder id 列表。
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
        # 先看是不是「别再提醒我 X」——取消意图不该再走建库流程
        hit, n = maybe_cancel(q, chat, text, account=account)
        if hit:
            q.record_action(account, chat, key, None, "cancel:%d" % n)
            continue
        # 是不是在回答上一个「几点提醒」的追问
        cid = maybe_answer_clarify(cfg, q, chat, text, account)
        if cid:
            q.record_action(account, chat, key, cid, "clarify_created")
            created.append(cid)
            continue
        # 含委托词，或者含时间信号（「明天」「两点」「每天」）都放行：
        # 后者交给模型判断"他是不是想让我提醒"——「明天下午我要去打球」没有委托词
        if not looks_worth_judging(text):
            continue
        base_ts = int(m.get("msg_ts") or time.time())
        try:
            r = reminder_parse.detect(text, base_ts)
        except Exception as e:
            log.warning("提醒解析异常 [%s]：%s" % (chat, e))
            r = None

        if r and not r.get("need_clarify"):
            # 本地规则吃准了：零成本、秒级，直接入库
            try:
                rid = q.add_reminder(account, chat, r["content"], r["rule"],
                                     r["next_ts"], anchor_ts=r.get("anchor_ts"),
                                     until_ts=r.get("until_ts"),
                                     short_delay=r.get("short_delay", False))
            except Exception as e:
                log.error("写入提醒失败 [%s]：%s" % (chat, e))
                continue
            q.record_action(account, chat, key, rid, "created")
            created.append(rid)
            log.info("记下提醒 #%d [%s]：%s（%s @ %s）" % (
                rid, chat, redact(r["content"], cfg), r["rule"],
                time.strftime("%m-%d %H:%M:%S", time.localtime(r["next_ts"]))))
            continue

        # 本地没吃准（没委托词 / 缺钟点 / 解析失败）→ 进候选队列，交给模型判定。
        # 只收含时间信号的消息，寒暄不会进来，所以不会滥烧额度。
        if looks_worth_judging(text):
            if q.add_candidate(account, chat, m.get("id"), text, base_ts):
                log.info("提醒待判定 [%s]：%s" % (chat, redact(text[:40], cfg)))
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
            with q.tx() as conn:
                row = conn.execute(
                    "SELECT * FROM reminders WHERE id=? AND revision=? AND next_ts=? "
                    "AND status='active'",
                    (rem["id"], rem["revision"], scheduled)).fetchone()
                if not row:
                    continue
                occ_id = q.add_occurrence(conn, rem["id"], scheduled, rem["revision"])
                nxt = reminder_parse.advance(rem.get("rule"), rem.get("anchor_ts"),
                                             scheduled)
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
                text = build_text(rem, scheduled, late=late)
                segs = segments_for(text, cfg)
                ob_id = q.create_outbound(
                    row["account"], row["chat"], text, status="draft",
                    kind="reminder", ref_id=occ_id,
                    expires_ts=scheduled + g, segments=segs, conn=conn)
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
        allowed, reason = gate.identity_ok(cfg, c["chat"])
        if not allowed or q.is_paused(c["chat"]):
            q.set_candidate(c["id"], "skipped", reason or "会话暂停")
            continue
        if int(c["msg_ts"] or 0) < cutoff:
            q.set_candidate(c["id"], "skipped", "超过 24 小时，忽略")
            continue
        try:
            r = judge_with_llm(cfg, c["text"], c["msg_ts"])
        except Exception as e:
            log.warning("提醒判定异常：%s: %s" % (type(e).__name__, e))
            q.set_candidate(c["id"], "error", str(e)[:100])
            continue
        if r is None:
            q.set_candidate(c["id"], "error", "模型不可用")
            continue
        if not r["is_reminder"]:
            q.set_candidate(c["id"], "skipped", "不是提醒委托")
            continue
        if r["need_time"] or not r["next_ts"]:
            ask = r["ask"] or "几点提醒你？"
            day_ts = int(r["next_ts"] or c["msg_ts"])
            cid = q.add_clarify(c["account"], c["chat"],
                                r["content"] or c["text"][:20], day_ts, ask)
            q.set_candidate(c["id"], "done", "待澄清 #%d：%s" % (cid, ask))
            log.info("提醒缺时间，待澄清 #%d [%s]：%s（该问：%s）" % (
                cid, c["chat"], redact(c["text"][:30], cfg), redact(ask, cfg)))
            continue
        if r["confidence"] < 0.5:
            q.set_candidate(c["id"], "skipped",
                            "置信度 %.2f 偏低" % r["confidence"])
            continue
        try:
            rid = q.add_reminder(c["account"], c["chat"],
                                 r["content"] or c["text"][:20], r["rule"],
                                 r["next_ts"], anchor_ts=r["next_ts"])
        except Exception as e:
            q.set_candidate(c["id"], "error", str(e)[:100])
            continue
        q.record_action(c["account"], c["chat"], str(c["msg_id"]), rid,
                        "llm_created")
        q.set_candidate(c["id"], "done", "建了 #%d" % rid)
        log.info("模型判定建提醒 #%d [%s]：%s（%s @ %s，置信 %.2f）" % (
            rid, c["chat"], redact(r["content"], cfg), r["rule"],
            time.strftime("%m-%d %H:%M", time.localtime(r["next_ts"])),
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
