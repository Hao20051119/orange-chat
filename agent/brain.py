# -*- coding: utf-8 -*-
"""回复大脑：把「记忆 + 人设 + 表情 + 联网」串成一次回复。

职责边界（很重要）
------------------
* **不写记忆**。以前 ``compose()`` 一进门就 ``mem.add(user)``，导致重生成时同一句
  话进记忆 2~3 次。现在记忆由 ``reply_worker`` 在批次边界写一次（带 ``src_key``）。
* **不发送**。发送、门禁、分段、核验都在 worker 侧；这里只产出文本。
* 对外入口：
    ``should_reply(chat, text)``   —— 身份规则 + 随机不回（兼容旧测试）
    ``compose(chat, text, extra)`` —— 生成一条回复
    ``handle(...)``                —— 旧的一体式入口，保留给 tune.py / 测试
"""

from __future__ import annotations

import copy
import os
import random
import re
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, os.path.join(ROOT, "memory"))
sys.path.insert(0, os.path.join(ROOT, "stickers"))

import gate as gate_mod
from ephemeral import EphemeralStore
from llm import chat as llm_chat, load_config
from persona import build_system_prompt, reply_probability
from persona import COMPREHEND_BRIEF
from store import MemoryStore

try:
    from logutil import get_logger, redact
    log = get_logger()
except Exception:                                    # pragma: no cover
    class _L:
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
    log = _L()

    def redact(text, cfg=None):
        return str(text)

try:
    from media import resolve_for_message, to_data_uri
    from media import md5_from_content
except Exception:                                    # pragma: no cover
    resolve_for_message = lambda t, c: None
    to_data_uri = lambda p: None
    md5_from_content = lambda c: None

# 收尾语：对方准备结束，别追问、别开新话题
END_HINTS = ("晚安", "拜拜", "回头聊", "明天说", "先这样", "撤了",
             "睡了", "下次聊", "改天聊", "先忙")


def atomic_facts(facts, limit: int = 8, max_chars: int = 300) -> str:
    """把"复合长句"事实拆成原子事实、去掉包含关系的重复项，再拼成一行。

    摘要产出的经常是「A；B；C」这种长句，直接把多条拼起来会重复啰嗦。
    """
    atoms = []
    for f in facts or []:
        for part in re.split(r"[；;。]", f or ""):
            part = part.strip(" ，,、")
            if 2 <= len(part) <= 80:
                atoms.append(part)
    uniq = []
    for a in atoms:
        if any(a == u or a in u or u in a for u in uniq):
            continue
        uniq.append(a)
        if len(uniq) >= max(1, int(limit)):
            break
    return "；".join(uniq)[:max_chars]

HISTORY_TIME_GAP = 1800.0          # 历史消息间隔超过 30 分钟就标时间
VISION_TIMEOUT = 20

# 群聊对话关系判断：先搞清"谁对谁说 / 在回应哪句"，再回
# 读新消息的规矩和主提示词共用一段（COMPREHEND_BRIEF），否则群聊这一步会先按字面读错，
# 后面的回复就跟着错。
GROUP_ANALYZE_PROMPT = (
    "这是一个微信群最近的对话（每条前面是说话人，我是「我」）。"
    "请判断下面这条新消息的对话关系，只输出四行，每行不超过 30 字，不要解释。\n"
    "读新消息时按这个来：" + COMPREHEND_BRIEF + "\n"
    "【谁对谁说】发言人 对 某人/全群\n"
    "【在回应】他接的是之前哪一句、哪个话题\n"
    "【要不要接】该不该我接，简短理由\n"
    "【注意】有没有容易误会的地方；明显打错的字也写在这里（例：「对」多半是「怼」）\n\n"
    "最近的对话：\n%s\n\n"
    "新消息（%s 发的）：%s"
)

# 读懂：微信口语常有同音错字、漏字。回复前先用一次小调用把这一轮读成一句白话，
# 结果交给回复模型（只读不评，也不许它说"你打错了"）。
# 实测（2026-11-20，真实群聊上下文，同一句「没那么爱对人了」各 4 次）：
# 只靠提示词规则，4 次里只有 1 次读到「怼」这层意思；加这一步 4 次都读到了
# 「跟人对着干 / 爱怼人」。代价是一次 ~0.6s、~200 token 的小调用。
COMPREHEND_PROMPT = (
    "你在帮一个聊天机器人听懂微信里的一句口语。只做「听懂」，不回复，不评价对方。\n"
    "微信打字用输入法，偶尔会选错同音字或漏字；同一个人连发的几句要当一句话看。\n"
    "按三行输出，每行不超过 45 字，不要解释：\n"
    "【听懂】用你自己的话重说一遍他到底在说什么，不要照抄原句；"
    "遇到明显打错的字，按上下文读对\n"
    "【另一读】如果还有另一种读法也说得通，写「A…／B…」；只有一种就写「单一」\n"
    "【语气】他这句是在夸、在逗、在抱怨、在问，还是别的\n\n"
    "上下文（可能有）：\n%s\n\n"
    "这句话：%s"
)

# 长期观察：让语气/长度慢慢跟着对方走（每隔 N 条消息更新一次）
ADAPT_PROMPT = (    "下面是我（我）和同一个人（对方）最近的聊天记录。"
    "请总结对方的说话习惯和相处方式，供我以后回他时参考。"
    "不要评价我，也不要写任何关于人设或身份的内容。\n\n"
    "已有观察（可能为空）：\n%s\n\n"
    "最近聊天：\n%s\n\n"
    "只输出四行，每行不超过 28 字，格式严格如下：\n"
    "【说话方式】对方句子长短、用词、爱不爱开玩笑\n"
    "【情绪特点】最近情绪倾向、在意什么\n"
    "【别踩的点】提到会不高兴、要回避的话题\n"
    "【我该怎么回】我该用什么长度、语气、节奏"
)


class Brain:
    def __init__(self, config: dict | None = None):
        self.cfg = config or load_config()
        self.agent = self.cfg.get("agent", {}) or {}
        self.mem = self._make_store(self.cfg)

    # ------------------------------------------------------------------ 记忆后端
    @staticmethod
    def _mem_cfg(cfg: dict) -> dict:
        return (cfg or {}).get("memory", {}) or {}

    def _make_store(self, cfg: dict):
        mc = self._mem_cfg(cfg)
        if mc.get("enabled", True):
            return MemoryStore()
        return EphemeralStore(ttl=int(mc.get("ephemeral_ttl", 1800)),
                              limit=int(mc.get("max_turns", 20)) * 2)

    def reload(self, cfg: dict):
        """热更新配置；记忆开关变化时切换后端（不用重启）。"""
        self.cfg = cfg or {}
        self.agent = self.cfg.get("agent", {}) or {}
        want = bool(self._mem_cfg(self.cfg).get("enabled", True))
        if want != bool(getattr(self.mem, "persistent", True)):
            self.mem = self._make_store(self.cfg)

    # ------------------------------------------------------------------ 过滤
    def contact_cfg(self, chat: str) -> dict:
        return gate_mod.contact_cfg(self.cfg, chat)

    def should_reply(self, chat: str, text: str, from_me: bool = False,
                     at_me: bool = False) -> tuple[bool, str]:
        """身份规则 + 随机不回；完整发送门禁在 ``gate.evaluate``。

        开了 ``llm_decide`` 之后不再掷随机数——该不该回交给模型判断
        （被 @ 时模型被强制要求回复）。
        """
        verdict = gate_mod.evaluate(self.cfg, chat, from_me=from_me, at_me=at_me)
        if verdict["action"] != "ok":
            return False, verdict["reason"]
        if at_me:
            return True, ""
        if not (text or "").strip():
            return False, "空消息"
        if self.agent.get("llm_decide", True):
            return True, ""

        t = (text or "").strip()
        is_question = t.endswith(("？", "?", "吗", "呢", "么")) or "怎么" in t \
            or "为什么" in t or "帮我" in t
        if not is_question and random.random() > reply_probability(self.cfg):
            return False, "随机不回"
        return True, ""

    # ------------------------------------------------------------------ 回复决策
    @staticmethod
    def skip_token(cfg: dict | None = None) -> str:
        agent = (cfg or {}).get("agent", {}) or {}
        return (agent.get("skip_token") or "[[SKIP]]").strip() or "[[SKIP]]"

    def is_skip_text(self, text: str) -> bool:
        """模型用 ``[[SKIP]]`` 表示"这条我不回"。"""
        if not self.agent.get("llm_decide", True):
            return False
        token = self.skip_token(self.cfg)
        return (text or "").strip().replace(token, "").strip() == ""

    # ------------------------------------------------------------------ 识图
    def describe_media(self, msg_type: str, content: str,
                       type_code=None) -> str | None:
        """图片/表情 → 一句描述；同一张图只识别一次（按 md5 缓存）。"""
        from msgtype import needs_vision, kind

        if not needs_vision(kind(msg_type, type_code)):
            return None
        md5 = md5_from_content(content or "")
        if md5:
            cached = self.mem.media_desc(md5)
            if cached:
                return cached

        path = resolve_for_message(msg_type, content or "")
        if not path:
            return None
        uri = to_data_uri(path)
        if not uri:
            return None
        r = llm_chat([
            {"role": "system", "content": "你在帮人看微信聊天里的图片或表情。"},
            {"role": "user", "content": [
                {"type": "text", "text": "用不超过 25 个字描述这张图/表情在传达什么情绪，"
                                         "不要分点、不要分析，直接一句话。"},
                {"type": "image_url", "image_url": {"url": uri}},
            ]},
        ], self.cfg, timeout=VISION_TIMEOUT, max_tokens=120)
        if not r.get("ok"):
            log.warning("识图失败：%s" % r.get("error"))
            return None
        desc = (r.get("text") or "").strip().replace("\n", " ")[:80]
        if md5 and desc:
            try:
                self.mem.save_media_desc(md5, desc)
            except Exception as e:
                log.warning("写识图缓存失败：%s" % e)
        return desc or None

    # ------------------------------------------------------------------ 上下文
    def _render_history(self, chat: str, limit: int) -> list[dict]:
        """历史消息；间隔超过 30 分钟或跨天时给内容加时间前缀。

        群聊里还会给"谁说的"加上名字，否则模型分不清几句话是谁讲的。
        """
        rows = self.mem.recent(chat, limit=limit)
        out: list[dict] = []
        prev_ts = 0
        prev_day = None
        is_group = (chat or "").endswith("@chatroom")
        for m in rows:
            content = m["content"]
            ts = int(m.get("ts") or 0)
            day = time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else None
            prefix = ""
            if ts and (prev_ts == 0 or ts - prev_ts > HISTORY_TIME_GAP or day != prev_day):
                prefix = "[%s] " % time.strftime("%m-%d %H:%M", time.localtime(ts))
            who = ""
            if is_group and m["role"] != "assistant" and m.get("sender"):
                who = "%s：" % m["sender"]
            out.append({"role": "assistant" if m["role"] == "assistant" else "user",
                        "content": prefix + who + content})
            prev_ts, prev_day = ts, day
        return out

    def member_context(self, chat: str, sender_wxid: str) -> str:
        """群里回某人时，补上"私下认识的这个人"：关系 + 事实 + 印象 + 观察。

        记忆是按会话存的（私聊 key=wxid，群聊 key=群号），所以群里默认认不出人。
        这里按发送者 wxid 去读他自己的那条记忆线，把关系和个人事实带进群聊。
        只带稳定信息，不带私聊原文/摘要，避免在群里爆料隐私。
        """
        if not (chat or "").endswith("@chatroom") or not sender_wxid:
            return ""
        agent = self.agent or {}
        if not agent.get("group_member_memory", True):
            return ""

        rel = nick = tone = ""
        try:
            from persona_card import contact_relation, load_card
            r = contact_relation(sender_wxid, load_card()) or {}
            rel = (r.get("role") or "").strip()
            nick = (r.get("nickname") or "").strip()
            tone = (r.get("tone") or "").strip()
        except Exception as e:
            log.warning("读取群成员关系失败：%s" % e)
        c = self.contact_cfg(sender_wxid)
        if c.get("relation"):
            rel = c["relation"]
        if c.get("nickname"):
            nick = c["nickname"]

        lines = []
        if rel:
            lines.append("关系：%s" % rel)
        if nick:
            lines.append("你私下叫他：%s" % nick)
        if tone:
            lines.append("私下语气：%s" % tone)
        try:
            facts = self.mem.facts(sender_wxid, limit=20)
            line = atomic_facts(facts, limit=6, max_chars=240)
            if line:
                lines.append("他本人的事：" + line)
            p = self.mem.profile(sender_wxid)
            if p.get("portrait") and p["portrait"] != "暂无":
                lines.append("你对他的印象：" + p["portrait"][:120])
            ad = self.mem.get_adapt(sender_wxid)
            if ad.get("notes"):
                first = ad["notes"].splitlines()[0][:80]
                if first:
                    lines.append("私下相处观察：" + first)
        except Exception as e:
            log.warning("读取成员记忆失败：%s" % e)

        if not lines:
            return ""
        return ("【你私下认识的这个人（只在群里自然体现，不要说出来源，也不要爆料隐私）】\n"
                + "\n".join("- " + x for x in lines))

    def _group_tip(self, chat: str, sender: str, sender_display: str = "",
                   at_me: bool = False) -> str:
        if not (chat or "").endswith("@chatroom"):
            return ""
        tip = "【当前是群聊】群里不止一个人，说话收敛些，别太私密、别用太亲昵的称呼。"
        tip += ("\n下面的记忆是整段群聊的，可能混着别人说的事，"
                "别把别人的经历安到某个人头上。")
        if sender_display:
            tip += "\n刚才说话的是群里的「%s」。" % sender_display
        elif sender:
            try:
                from persona_card import contact_relation, load_card
                rel = contact_relation(sender, load_card()) or {}
                if rel.get("role"):
                    tip += "\n刚才说话的是你的%s。" % rel["role"]
            except Exception as e:
                log.warning("读取群聊关系失败：%s" % e)
        if at_me:
            tip += ("\n有人刚在群里 @ 你，直接接他的话，别对着整群喊；"
                    "可以顺着他的话损一句或逗一句。"
                    "\n对方点名了，必须回复，不许输出跳过标记。")
            if sender_display:
                tip += ("\n现在要回的是「%s」，只回他一个人；"
                        "不要在回复里写 @名字，系统会自动加。" % sender_display)
        return tip

    def _style_hints(self, chat: str, text: str) -> str:
        parts = []
        try:
            from style_samples import build_prompt as _build_style
            sp = _build_style(text[:80], chat, 3)
            if sp:
                parts.append(sp)
        except Exception as e:
            log.warning("风格样例注入失败：%s" % e)
        return "\n\n".join(p for p in parts if p)

    def _web_context(self, text: str, chat: str = "") -> str:
        web_cfg = self.cfg.get("web") or {}
        if not web_cfg.get("enabled", False) or not text:
            return ""
        try:
            from web_tools import Research, should_search
            fired, why = should_search(text, web_cfg.get("extra_hints") or ())
            if not fired:
                return ""
            # 口语问天气常不带地名，用最近几轮对话补实体（「金华」）
            hint = ""
            if chat:
                try:
                    hint = " ".join((m.get("content") or "")
                                    for m in self.mem.recent(chat, limit=6))
                except Exception:
                    hint = ""
            t0 = time.time()
            r = Research(web_cfg)
            ctx = r.maybe(text, context_hint=hint) or ""
            log.info("联网 %s（%s）%.1fs trace=%s" % (
                ("注入 %d 字" % len(ctx)) if ctx else "无结果",
                why, time.time() - t0, ";".join(r.trace)[:100] or "-"))
            return ctx
        except Exception as e:
            log.warning("联网检索失败（已降级为不联网）：%s: %s" % (type(e).__name__, e))
            return ""

    def _kb_context(self, chat: str, text: str) -> str:
        """按当前这句话检索知识库。命中才注入，出错静默降级。

        权限在 kb.search 的 SQL 里强制：群聊只召回 public 与本群资料，
        私聊资料不会流进群聊。
        """
        kb_cfg = self.cfg.get("kb") or {}
        if not kb_cfg.get("enabled", False):
            return ""
        q = (text or "").strip()
        if len(q) < 2:
            return ""
        try:
            from kb import context_block, should_lookup
            if not should_lookup(q):
                return ""
            block = context_block(
                q, chat_id=chat or "",
                budget_chars=int(kb_cfg.get("context_chars", 1000)),
                account_=self.cfg.get("account") or None)
        except Exception as e:
            log.warning("知识检索失败（已降级为不检索）：%s: %s" % (type(e).__name__, e))
            return ""
        if block:
            n = len([ln for ln in block.splitlines() if ln.startswith("[")])
            log.info("kb hit n=%d [%s]" % (n, chat))
        return block

    def _history_brief(self, chat: str, limit: int = 6) -> str:
        """最近几条对话（带说话人），给「读懂」和自查用。"""
        try:
            rows = self.mem.recent(chat, limit=limit)
        except Exception:
            return ""
        lines = []
        for r in rows:
            who = "我" if r["role"] == "assistant" else (r.get("sender") or "对方")
            lines.append("%s：%s" % (who, (r["content"] or "").replace("\n", " ")[:100]))
        return "\n".join(lines)

    def comprehend(self, chat: str, text: str) -> str:
        """把这一轮消息读成一句白话，返回【听懂/另一读/语气】三行。

        失败、没开、太短都返回空串——调用方拿空串就当没这一步，按原话回。
        """
        if not (self.agent or {}).get("comprehend_prepass", True):
            return ""
        q = (text or "").strip()
        if len(q) < 3:                     # 「嗯」「哈哈」「好的」不值得读一遍
            return ""
        try:
            r = llm_chat([
                {"role": "system",
                 "content": "你只负责听懂微信口语，不做回复，不评价对方。"},
                {"role": "user",
                 "content": COMPREHEND_PROMPT % (self._history_brief(chat) or "（无）",
                                                 q[:600])},
            ], self.cfg, max_tokens=180)
        except Exception as e:
            log.warning("读懂失败（按原话直接回）：%s: %s" % (type(e).__name__, e))
            return ""
        if not r.get("ok"):
            log.warning("读懂失败（按原话直接回）：%s" % r.get("error"))
            return ""
        out = (r.get("text") or "").strip()
        if "【听懂】" not in out:           # 模型没按格式来，宁可不注入
            return ""
        return out[:600]

    def _trim_current(self, history: list[dict], text: str) -> list[dict]:
        """去掉历史末尾与"本轮这句话"重复的部分。

        正常流程里入站记忆是生成完之后才写的，不会重复；但重试、补写等路径
        可能让记忆里已经有这句话——那样模型会看到两遍，这里兜底裁掉。
        """
        cur = (text or "").strip()
        if not cur or not history:
            return history
        time_prefix = re.compile(r"^\[\d{2}-\d{2} \d{2}:\d{2}\]\s*")
        tail: list[str] = []
        i = len(history) - 1
        while i >= 0 and history[i].get("role") == "user":
            tail.insert(0, time_prefix.sub("", history[i].get("content") or "").strip())
            i -= 1
        for k in range(len(tail), 0, -1):
            if "\n".join(tail[-k:]).strip() == cur:
                return history[:len(history) - k]
        return history

    def build_messages(self, chat: str, text: str, extra: str = "",
                       sender: str = "", at_me: bool = False,
                       sender_display: str = "",
                       style_text: str | None = None,
                       reading: str = "") -> list[dict]:
        """system + 记忆画像/事实 + 历史 + 当前这句话。

        ``style_text`` 只影响风格样例检索：主动开口时没有「对方这句话」，
        用钩子/线索当检索文本，风格才贴切。
        ``reading`` 是 ``comprehend()`` 的读法结果，先让模型认下这句话什么意思。
        """
        extra_ctx = ""
        if extra:
            extra_ctx = "【当前情况】\n" + extra

        if reading:
            extra_ctx = (extra_ctx + "\n\n"
                         "【你刚把这一句读了一遍（这不是对方以前说过的话，"
                         "只是你对这一句的理解）】\n"
                         + reading
                         + "\n据此接话：别复述原话，也别提错字，更不要把它当成对方之前讲过的内容；"
                           "有两种读法时选更像他本意的那个，实在拿不准就顺口确认一句。").strip()

        tip = self._group_tip(chat, sender, sender_display=sender_display,
                              at_me=at_me)
        if tip:
            extra_ctx = (extra_ctx + "\n" + tip).strip()

        member = self.member_context(chat, sender)
        if member:
            extra_ctx = (extra_ctx + "\n\n" + member).strip()

        if any(h in (text or "") for h in END_HINTS):
            extra_ctx = (extra_ctx +
                         "\n【对方在收尾】简短回一句就好，不要追问，也不要开新话题。").strip()

        style = self._style_hints(chat, style_text if style_text is not None else text)
        if style:
            extra_ctx = (extra_ctx + "\n\n" + style).strip()

        # 颜文字防单调：别重复 + 该带/不该带的节奏（生成后还有确定性轮换兜底）
        try:
            from kaomoji import usage_hint
            khint = usage_hint(self.mem, chat)
            if khint:
                extra_ctx = (extra_ctx + "\n" + khint).strip()
        except Exception as e:
            log.warning("生成颜文字提示失败：%s" % e)

        cfg_eff = self.cfg
        c = self.contact_cfg(chat)
        if c:
            cfg_eff = copy.deepcopy(self.cfg)
            ag = cfg_eff.setdefault("agent", {})
            st = ag.setdefault("style", {})
            for k in ("tone", "length", "emojis", "no_question"):
                if k in c and c[k] is not None:
                    st[k] = c[k]
            if c.get("persona"):
                ag["persona"] = c["persona"]
            if c.get("relation"):
                extra_ctx = ("【你和对方的关系】%s\n\n%s" % (c["relation"], extra_ctx)).strip()
            if c.get("nickname"):
                extra_ctx = ("【对方希望你叫他】%s\n\n%s" % (c["nickname"], extra_ctx)).strip()

        web_ctx = self._web_context(text, chat)
        if web_ctx:
            extra_ctx = (extra_ctx + "\n\n" + web_ctx).strip()

        kb_ctx = self._kb_context(chat, text)
        if kb_ctx:
            extra_ctx = (extra_ctx + "\n\n" + kb_ctx).strip()

        system = build_system_prompt(extra_ctx, cfg_eff, chat=chat,
                                     inbound=bool((text or "").strip()))

        ctx_extra = ""
        try:
            ad = self.mem.get_adapt(chat)
            if ad.get("notes"):
                ctx_extra += ("【长期观察（只调整语气节奏，不改人设和关系）】\n"
                              + ad["notes"] + "\n\n")
        except Exception as e:
            log.warning("读取观察笔记失败：%s" % e)
        profile = self.mem.profile(chat)
        if profile.get("portrait") and profile["portrait"] != "暂无":
            ctx_extra += "【你记得的关于这个人的印象】\n%s\n\n" % profile["portrait"]
        if profile.get("summary"):
            ctx_extra += "【你们之前的聊天摘要】\n%s\n\n" % profile["summary"]
        facts = self.mem.facts(chat, limit=12)
        fact_line = atomic_facts(facts, limit=10, max_chars=400)
        if fact_line:
            ctx_extra += "【你记得的关于对方的事实】\n" + fact_line + "\n"

        mem_block = ""
        if ctx_extra.strip():
            mem_block = ("【参考记忆（次要；与人设和关系冲突时，以人设和关系为准）】\n"
                         + ctx_extra.strip())
        system_full = (system + "\n\n" + mem_block).strip()

        mc = self._mem_cfg(self.cfg)
        per_msg = max(100, int(mc.get("history_msg_chars", 600)))
        history = self._render_history(chat, int(mc.get("max_turns", 20)))
        history = self._trim_current(history, text)
        history = [dict(m, content=(m["content"] or "")[:per_msg]) for m in history]

        # 上下文预算：人设/关系永远保留，超预算时从最老的历史开始丢
        budget = max(2000, int(mc.get("max_context_chars", 12000)))
        current = (text or "").strip()[:4000]
        used = len(system_full) + len(current)
        kept = []
        for m in reversed(history):
            if used + len(m["content"]) > budget:
                break
            kept.append(m)
            used += len(m["content"])
        kept.reverse()
        if len(kept) < len(history):
            log.info("上下文超预算，丢弃最老 %d 条历史 [%s]"
                     % (len(history) - len(kept), chat))

        msgs = [{"role": "system", "content": system_full}]
        msgs.extend(kept)
        msgs.append({"role": "user", "content": current})
        return msgs

    # ------------------------------------------------------------------ 生成
    def compose(self, chat: str, text: str, extra: str = "",
                sender: str = "", at_me: bool = False,
                sender_display: str = "", force_reply: bool = False) -> dict:
        """构建上下文并调模型，返回 {ok, reply, error, usage}。不写记忆。"""
        if force_reply:
            extra = (extra + "\n【必须回复】对方点名找你了，这次必须回一句，"
                            "不许输出跳过标记。").strip()
        # 先读懂这一轮（同音错字/漏字），再据读法接话。失败就当没这一步。
        reading = self.comprehend(chat, text)
        if reading:
            brief = " / ".join(x for x in reading.splitlines() if x)[:140]
            log.info("读懂：%s" % redact(brief, self.cfg))
        msgs = self.build_messages(chat, text, extra=extra, sender=sender,
                                   at_me=at_me, sender_display=sender_display,
                                   reading=reading)
        r = llm_chat(msgs, self.cfg)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error")}
        reply = (r.get("text") or "").strip()
        if not reply:
            return {"ok": False, "error": "模型返回空"}
        if self.is_skip_text(reply):
            return {"ok": True, "skip": True, "reply": "",
                    "usage": r.get("usage"), "forced": force_reply}
        # 口音兜底：模型偶尔漏出"呗/啥/咋"，这里直接换掉
        from persona import clean_accent
        cleaned = clean_accent(reply)
        if cleaned != reply:
            log.info("替换口音词：%s" % redact(reply[:40] + " → " + cleaned[:40], self.cfg))
            reply = cleaned
        # 颜文字轮换：模型老挂 (￣▽￣)，换成同心情里最近没用过的
        try:
            from kaomoji import recent_from_mem, rotate
            rotated, kinfo = rotate(reply, recent=recent_from_mem(self.mem, chat, limit=8))
            if kinfo.get("changed"):
                log.info("换颜文字：%s" % ", ".join(
                    "%s -> %s" % (a, b) for a, b in kinfo["replaced"]))
                reply = rotated
        except Exception as e:
            log.warning("颜文字轮换失败：%s" % e)
        return {"ok": True, "reply": reply, "usage": r.get("usage"),
                "truncated": bool(r.get("truncated"))}

    # -------------------------------------------------------------- 主动开口
    def compose_proactive(self, chat: str, seed: str = "",
                          kind: str = "hook") -> dict:
        """主动说一句（没有入站消息）。返回 {ok, reply, skip, error}。

        ``kind``：``hook`` 用钩子当由头；``idle`` 是没由头时的纯状态表达。
        模型可以输出 skip_token 表示「此刻不该开口」——这是正常结果，不是失败。
        """
        token = self.skip_token(self.cfg)
        if kind == "idle":
            task = (
                "【内部任务】现在没人跟你说话，你有点闲，想随手发一句。\n"
                "规矩：\n"
                "1. 只写一句，8~24 字，短句，像手机随手打的；\n"
                "2. 不要提问——问句会让对方有「必须回答」的压力；\n"
                "3. 只写感受或泛泛的状态（困、烦、不想动、天气、课累、饭点）；\n"
                "4. 绝对不能编造具体经历（去了哪、见了谁、吃了什么、买了什么）；\n"
                "5. 觉得此刻不该说话，就只输出 %s。" % token
            )
        else:
            task = (
                "【内部任务】现在没有人给你发消息。你要主动找他说句话。\n"
                "由头：%s\n"
                "规矩：\n"
                "1. 像随手打的一句，别用「在吗」，别问「最近怎么样」这种空话；\n"
                "2. 可以引用上面那个由头，但不要像在翻档案；\n"
                "3. 最多一个问句，最好零个；1~2 句；\n"
                "4. 不要编造你自己的经历、行程、身体状态；\n"
                "5. 觉得此刻开口很别扭，就只输出 %s。" % (seed, token)
            )

        msgs = self.build_messages(
            chat, text="",
            extra="【说明】下面这条是内部任务指令，不是你对方说的话；"
                  "你要做的是主动开口，而不是回复谁。",
            style_text=seed or "")
        msgs[-1] = {"role": "user", "content": task}
        r = llm_chat(msgs, self.cfg)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error")}
        reply = (r.get("text") or "").strip()
        if not reply:
            return {"ok": False, "error": "模型返回空"}
        if self.is_skip_text(reply):
            return {"ok": True, "skip": True, "reply": ""}
        from persona import clean_accent
        reply = clean_accent(reply)
        try:
            from kaomoji import recent_from_mem, rotate
            rotated, kinfo = rotate(reply, recent=recent_from_mem(self.mem, chat,
                                                                 limit=8))
            if kinfo.get("changed"):
                reply = rotated
        except Exception as e:
            log.warning("主动开口颜文字轮换失败：%s" % e)
        return {"ok": True, "reply": reply, "truncated": bool(r.get("truncated"))}

    # ------------------------------------------------------------------ 拟人
    def human_delay(self, chat: str | None = None) -> float:
        base_cfg = self.agent
        c = self.contact_cfg(chat) if chat else {}
        lo = float(c.get("min_delay", base_cfg.get("min_delay", 3)))
        hi = float(c.get("max_delay", base_cfg.get("max_delay", 12)))
        if hi < lo:
            lo, hi = hi, lo
        base = random.uniform(lo, hi)
        if random.random() < 0.15:
            base += random.uniform(2, 6)
        return round(base, 1)

    # ------------------------------------------------------------------ 敏感
    SENSITIVE_PATTERNS = (
        (r"借你|借给|借钱|转你|转给|打钱|给你转|红包|代付|买单", "金钱"),
        (r"明天.{0,6}见|几点.{0,8}见|约在|给你约|我去接你|等你到", "约定见面"),
        (r"我保证|我承诺|包在我身上|一定帮你|肯定没问题|交给我", "承诺"),
    )

    def sensitive_label(self, reply: str) -> str | None:
        t = reply or ""
        for pat, label in self.SENSITIVE_PATTERNS:
            if re.search(pat, t):
                return label
        return None

    # ------------------------------------------------------------------ 摘要
    def maybe_summarize(self, chat: str) -> dict:
        mc = self._mem_cfg(self.cfg)
        if not mc.get("enabled", True) or not getattr(self.mem, "persistent", True):
            return {"ok": False, "reason": "长期记忆已关闭"}
        threshold = int(mc.get("summary_threshold", 30))
        try:
            if not self.mem.should_summarize(chat, threshold):
                return {"ok": False, "reason": "未到摘要阈值"}
            return self.mem.summarize(chat, lambda m: llm_chat(m, self.cfg),
                                      keep_recent=int(mc.get("max_turns", 20)))
        except Exception as e:
            log.error("摘要失败 [%s]：%s: %s" % (chat, type(e).__name__, e))
            return {"ok": False, "reason": "%s: %s" % (type(e).__name__, e)}

    # ------------------------------------------------------------------ 群聊关系
    def analyze_group(self, chat: str, sender_display: str, text: str) -> str:
        """群里回之前先判断对话关系；失败就返回空串，不影响正常回复。"""
        agent = self.agent or {}
        if not agent.get("group_analysis", True):
            return ""
        try:
            rows = self.mem.recent(chat, limit=14)
            if len(rows) < 3:
                return ""
            lines = []
            for r in rows:
                who = "我" if r["role"] == "assistant" else (r.get("sender") or "有人")
                lines.append("%s：%s" % (who, (r["content"] or "").replace("\n", " ")[:100]))
            r = llm_chat([
                {"role": "system",
                 "content": "你在帮一个聊天机器人判断微信群里的对话关系。"
                            "只输出要求的四行。"},
                {"role": "user",
                 "content": GROUP_ANALYZE_PROMPT % (
                     "\n".join(lines), sender_display or "有人", (text or "")[:300])},
            ], self.cfg, max_tokens=220)
            if not r.get("ok"):
                return ""
            return self._parse_adapt(r.get("text") or "")
        except Exception as e:
            log.warning("群聊关系判断失败：%s: %s" % (type(e).__name__, e))
            return ""

    # ------------------------------------------------------------------ 长期观察
    @staticmethod
    def _parse_adapt(text: str) -> str:
        lines = []
        for line in (text or "").splitlines():
            s = line.strip()
            if s.startswith("【"):
                lines.append(s[:80])
        return "\n".join(lines[:4])

    def maybe_adapt(self, chat: str, force: bool = False) -> dict:
        """根据长期聊天记录更新"对方习惯"笔记（被 sender / 定时任务调用）。"""
        mc = self._mem_cfg(self.cfg)
        if not mc.get("enabled", True) or not getattr(self.mem, "persistent", True):
            return {"ok": False, "reason": "长期记忆已关闭"}
        every = max(5, int(mc.get("adapt_every", 30)))
        try:
            ad = self.mem.get_adapt(chat)
            n = self.mem.count(chat)
            if not force and n - int(ad.get("seen") or 0) < every:
                return {"ok": False, "reason": "未到观察间隔"}
            rows = self.mem.recent(chat, limit=40)
            if len(rows) < 8:
                return {"ok": False, "reason": "聊天太少，先不总结"}
            convo = "\n".join(
                "%s：%s" % ("我" if r["role"] == "assistant" else "对方",
                           (r["content"] or "").replace("\n", " ")[:120])
                for r in rows)
            r = llm_chat([
                {"role": "system",
                 "content": "你在帮一个聊天机器人总结对方的说话习惯。"
                            "只输出要求的四行，不要解释。"},
                {"role": "user",
                 "content": ADAPT_PROMPT % (ad.get("notes") or "（空）", convo)},
            ], self.cfg, max_tokens=300)
            if not r.get("ok"):
                return {"ok": False, "reason": r.get("error")}
            notes = self._parse_adapt(r.get("text") or "")
            if not notes:
                return {"ok": False, "reason": "观察格式无效"}
            self.mem.save_adapt(chat, notes, n)
            log.info("更新观察笔记 [%s]：%d 条消息后" % (chat, n))
            return {"ok": True, "notes": notes}
        except Exception as e:
            log.error("长期观察失败 [%s]：%s: %s" % (chat, type(e).__name__, e))
            return {"ok": False, "reason": "%s: %s" % (type(e).__name__, e)}

    # ------------------------------------------------------------------ 旧入口
    def handle(self, chat: str, text: str, from_me: bool = False,
               msg_type: str = "文本", raw_content: str = "",
               send=None, dry_run: bool = True, sender: str = "") -> dict:
        """兼容旧调用；不写记忆。新代码请用 worker 的批次流程。"""
        ok, why = self.should_reply(chat, text or ("[媒体]" if msg_type != "文本" else ""),
                                    from_me)
        if not ok:
            return {"ok": False, "skipped": why}

        if msg_type != "文本":
            from msgtype import describe
            desc = self.describe_media(msg_type, raw_content or text)
            text = describe(msg_type, raw_content or text, desc=desc)["text"]

        comp = self.compose(chat, text, sender=sender)
        if not comp.get("ok"):
            return {"ok": False, "error": comp.get("error")}

        reply = comp["reply"]
        result = {"ok": True, "reply": reply, "delay": self.human_delay(chat),
                  "dry_run": dry_run, "status": "draft",
                  "truncated": comp.get("truncated", False)}

        label = self.sensitive_label(reply)
        if label:
            result["status"] = "draft_sensitive"
            result["sensitive"] = label
            return result

        if dry_run or send is None:
            return result

        if self.contact_cfg(chat).get("draft_only"):
            result["status"] = "draft_only"
            return result

        time.sleep(float(result["delay"]))
        self.reload(load_config())
        verdict = gate_mod.evaluate(self.cfg, chat,
                                    created_ts=result.get("created_ts"))
        if verdict["action"] != "ok":
            return {"ok": False, "skipped": verdict["reason"],
                    "reply": reply, "status": "cancelled"}

        sent = send(chat, reply)
        result["sent"] = sent
        if sent.get("ok"):
            result["status"] = "sent"
        else:
            result["status"] = "failed"
            result["ok"] = False
        return result


if __name__ == "__main__":
    import json

    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    brain = Brain()
    brain.agent["auto_reply"] = True

    chat = sys.argv[1] if len(sys.argv) > 1 else "__draft__"
    text = sys.argv[2] if len(sys.argv) > 2 else "在吗"
    print("用户:", text)
    print(json.dumps(brain.handle(chat, text, dry_run=True), ensure_ascii=False, indent=2))
