# -*- coding: utf-8 -*-
"""代传话：A 让你去提醒 B（「你主动提醒一下他该睡觉了」）。

为什么单拎出来
--------------
这句话看着像聊天，其实是跨会话的委托：橙子嘴上答应「行吧，我给他发一句」，
转头什么也没发（实测 2026-11-21 那次）。这里把它落成「目标会话里的一条提醒」，
调度、门禁、发送、回写全复用提醒链路，只有两处不同：收件人不是当前聊天、
措辞要交代是谁托的。

边界
----
* 只发私聊；黑名单、系统账号、自己和自己之间一律不发
* 按天封顶（``proactive.relay.daily_limit``），防止被当成遥控器刷屏
* 否定/反问（「我不是提醒他了吗」）不算委托
* 「他/她」指主人（``agent.owner_wxid``）；说名字就按人设卡里的关系找
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime

import gate
import media
import reminder_parse
from logutil import get_logger, redact
from msgtype import kind as message_kind

log = get_logger()

# 委托动词。三族：「提醒/催」（让他到点做事）、「问/说/告诉」（替他张嘴）、
# 「让/叮嘱/嘱咐/督促/别忘」（口语里最常用的那几种说法）。
_VERB = re.compile(
    r"(叫醒|提个醒|提醒一下|提醒|叮嘱|嘱咐|叮咛|督促|监督|盯着|"
    r"问一下|问一声|问问|问|说一声|说句|说一下|"
    r"告诉|转告|转达|带句话|带个话|传个话|托个话|"
    r"别忘|让|催|叫|喊|通知|约|请|找)")
# 强动词：单独出现就值得让模型看一眼（「提醒小张」「问问她」）。
# 「叫/喊/催/让/别忘/说一下」太宽（「我叫外卖了」「让我想想」「别忘带钥匙」），
# 必须同时有代词或关系网名字才放行。
_STRONG_VERB = re.compile(
    r"(提醒|通知|叫醒|叮嘱|嘱咐|叮咛|督促|提个醒|问问|问一下|问一声|"
    r"告诉|转告|转达|带句话|带个话|传个话|说一声)")
# 否定/反问才排除委托。问号、「吗」「呢」本身不是否定——「你问问她吃的什么？」
# 是最典型的委托，旧逻辑把它一起杀了，橙子于是嘴上答应却什么都不发。
# 「别忘/不要忘」是提醒的口语说法，不能当成否定（「别忘了给他带钥匙」）。
_NEG = re.compile(
    r"(别(?!忘)|不要(?!忘)|不用|不必|不是|没有|谁|不想|不愿意|懒得)")
# 「提醒/叫醒/通知」后面紧跟「我」是让自己记着，不是代传话；其余动词不管。
_ALERT_VERB = re.compile(r"(提醒|叫醒|通知)")
# 像在使唤人的口气：模型判定「不是委托」时，只有带这种口气才提醒回复侧
# 别空口答应（「你告诉她了吗」这类问句不该被当成委托）。
_IMPERATIVE_HINT = re.compile(r"(帮我|帮忙|麻烦|请你|你帮|给我|替我|替你|替她|替他)")
_PRONOUN = re.compile(r"(他|她|[tT][aA])")

_STRIP_HEAD = re.compile(r"^(?:一下|一声|下|吧|啊)\s*")
_STRIP_TAIL = re.compile(r"[，,。.!！~、\s]+$")
# 名字后面回指的那个「他/她」：「提醒小王，他明天要开会」不该把「他」写进内容
_LEAD_REF = re.compile(r"^(?:他|她)们?\s*[，,、]?\s*")
_ALIAS_ENDS = "。.,!?;:、~…"
_ALIAS_WRAPPERS = dict(zip("(\"'“‘「『【[", ")\"'”’」』】]"))
_MODEL_NAME_LIMIT = 96  # 姓名和昵称各最多 40 字，还要容纳括号及引用符号
# 追问最多三轮：每轮只问「概率最大的那个人」，被否掉就换下一个，
# 三轮还定不下来就结案——别再拿同一个问题把人当复读机。
_CLARIFY_MAX_ASKS = 3
# 一句答复最多这么长；再长就不像在回答「是不是他」了
_ANSWER_MAX_CHARS = 300
_CLARIFY_DECISIONS = ("confirm", "reject", "pick", "cancel", "unclear", "not_answer")
# 在途回传时，这句像不像「替我把话带回去」——只决定要不要问模型，不做判定。
# 「帮我检查数学题」不在这里面，所以不会被当成带话。
_BACK_PREFILTER = re.compile(
    r"(代我|替我|带我|捎|转告|转达|传话|问声|问个|说一声|说句|"
    r"告诉他|告诉她|跟他说|跟她说|和他说|和她说|对他说|对她说|回他|回她)")
# 「他点名的人发不出去」时怎么跟人解释（target_of 的拒绝原因 → 人话）
_MISS_TEXT = {
    "关系网里没这个人": "不在你的关系表里",
    "关系里没绑定会话": "还没绑定微信会话",
    "关系网里有两个人叫这个名字": "在关系表里有两个人叫这个名字，分不清是哪个",
    "没说清发给谁": "没说清是谁",
}
_ASK_OPTION_LIMIT = 3
# 称谓前的所有格/口语前缀：「你哥哥」「我家的组长」→「哥哥」「组长」。
# 只在剥掉之后还剩两个字以上才用，免得把单字名切没了。
_ALIAS_PREFIX = re.compile(r"^(?:你|您|妳|我|俺|我们|咱们)(?:家|们)?的?")
# 句尾语气词：「对呀」「alex吧」→「对」「alex」
_ALIAS_TAIL = re.compile(r"(?:呢|吧|啊|呀|哦|嘛|啦|了)$")
# 「他/她」在没有上下文时指向主人（配置的 owner_wxid）；
# 「主人 / owner」是明确称呼，永远指向配置里的主人，不参与上下文消解。
_OWNER_WORDS = ("owner", "主人")
_PRONOUN_HINTS = ("他", "她")
_OWNER_HINTS = _OWNER_WORDS + _PRONOUN_HINTS
_CLARIFY_YES = {"对", "是", "嗯", "没错", "对的", "是的", "就是", "就是他", "就是她",
                "对就是他", "对就是她", "确认", "发吧", "没问题", "对就发给他", "对就发给她"}
_CLARIFY_CANCEL = {"算了", "不用发了", "不用发", "别发了", "不发了", "取消", "取消这条", "先别发"}
_CLARIFY_NO = {"不", "不是", "不对", "不是他", "不是她", "不是这个人", "别发给他", "别发给她"}
_CLARIFY_RECEIPTS = {
    "relay_clarify_cancelled": "好，这条委托取消了，没有发送。",
    "relay_clarify_exhausted": "问了三遍还是没确定要发给谁，这次没有发送。可以直接说名字，或者重新发起委托。",
    "relay_clarify_prompt_failed": "核对问句没有确认送达，这次委托没有发送。可以重新发起委托。",
}


def _relations() -> list[dict]:
    """人设卡里的关系表（名字 / 昵称 → 会话）。读不到就当没有。"""
    try:
        from persona_card import effective_relations, load_card
        return effective_relations(load_card()) or []
    except Exception as e:
        log.warning("读取人设关系失败：%s" % e)
        return []


def display_name(chat: str, relations: list[dict] | None = None) -> str:
    """会话 → 人设卡里的名字（联系人A / alex / 老妈…），没有就空串。"""
    for rel in (relations if relations is not None else _relations()):
        if rel.get("chat") and rel["chat"] == chat:
            return rel.get("name") or rel.get("nickname") or ""
    return ""


def owner_chat(cfg: dict) -> str:
    """「他/她」指谁：主人（接收提醒的那个人）。"""
    agent = (cfg or {}).get("agent") or {}
    owner = str(agent.get("owner_wxid") or "").strip()
    if owner:
        return owner
    return str(((cfg or {}).get("kb") or {}).get("owner_wxid") or "").strip()


def _relation_lines(relations: list[dict] | None) -> list[str]:
    """关系网 → 给模型看的清单：名字（昵称）｜关系｜能不能发。"""
    lines = []
    for rel in relations or []:
        name = (rel.get("name") or rel.get("nickname") or "").strip()
        if not name:
            continue
        nick = (rel.get("nickname") or "").strip()
        label = name if not nick or nick == name else "%s（%s）" % (name, nick)
        relation = (rel.get("relation") or "").strip() or "关系网里的人"
        can = "可以发消息" if (rel.get("chat") or "").strip() else "没绑会话，发不了"
        lines.append("- %s｜%s｜%s" % (label, relation, can))
    return lines


def looks_like_relay(text: str, relations: list[dict] | None = None) -> bool:
    """省钱的预筛：这句话有没有可能在「托橙子提醒别人」。

    只是「要不要花一次模型调用」的判断，真正的收件人和内容仍全由模型定。

    命中条件（任一）：
    * 提到了「他/她」；
    * 提到了关系网里的某个人；
    * 委托动词后面跟着的不是「我」——点名了一个关系网外的人
      （「提醒小张」），这种也要交给模型去判断，不能直接当成自己的提醒。
    """
    t = (text or "").strip()
    if not t or len(t) > 120:
        return False
    verb = _VERB.search(t)
    if not verb:
        return False
    # 否定只看**动词之前**：「我不想提醒他」是否定委托；
    # 「监督他别偷懒」「告诉他别迟到」里的「别」是内容的一部分，不能整句挡掉。
    if _NEG.search(t[:verb.start()]):
        return False
    if _PRONOUN.search(t):
        return True
    names = [r.get(k) for r in (relations or [])
             for k in ("name", "nickname") if r.get(k)]
    if any(n in t for n in names):
        return True
    # 「我提醒你了」「他提醒我了」是陈述，不是托我去办事
    head = t[:verb.start()].rstrip()
    if head.endswith("我") and not head.endswith(("帮我", "给我", "替我", "让我", "要我")):
        return False
    if not _STRONG_VERB.search(t):
        return False            # 「叫/喊/催/让/别忘」：没有代词也没点名，不值一次调用
    # 只有「提醒/叫醒/通知」才要排掉「提醒我…」这种自己提醒自己；
    # 「转达一下我的意思」「告诉我的想法」里的「我」是要转达的内容。
    if _ALERT_VERB.search(t):
        after = _STRIP_HEAD.sub("", t[verb.end():].strip(" ，,。.!！~、"))
        if after.startswith("我"):
            return False
    return True


def judge(cfg: dict, text: str, relations: list[dict] | None = None,
          requester: str = "", context: dict | None = None) -> dict | None:
    """让模型判定「是不是让橙子去提醒别人」，并给出收件人和要转达的事。

    收件人不再用正则猜：模型拿到整张关系网（名字、关系、能不能发），由它
    决定目标；代码只做「名字 → 会话」的映射，对不上就不建单。

    ``context`` 是这轮对话的来龙去脉（「你刚才替谁问过我什么」）。有了它，
    被问的人回一句「你代我向他问声不好」才不会被当成另一件新委托：
    模型能认出这是在让橙子把话带回给发起人（``intent=relay_back``）。

    返回 ``{"intent", "is_relay", "target", "content", "confidence", "tz",
    "reason"}``；模型不可用或输出不可解析时返回 ``None``（调用方按「不建单」处理）。
    """
    try:
        from llm import chat as llm_chat
    except Exception:
        return None
    import json as _json

    roster = "\n".join(_relation_lines(relations)) or "（关系网是空的）"
    bg = ""
    if context:
        near = (context.get("relay_from") or "").strip()
        near_name = (context.get("relay_from_name") or "").strip() or near
        if near:
            bg = ("背景（很重要）：你之前替「%s」向现在这个说话人问过一句「%s」。"
                  "他这句话很可能是在回那件事，或者在让你把话带回去。\n"
                  "指代：他说「他/她」时指的是「%s」，不是主人。\n"
                  % (near_name, (context.get("question") or "一件事")[:60], near_name))
    prompt = (
        "你是橙子。判断下面这句话是不是「让橙子去对另一个人说话」——"
        "可能是转达一句提醒，也可能是替他问一句、说一声。\n"
        "正在跟你说话的人：%s\n"
        "你的关系网（只有清单里的人才有微信，才能发出去）：\n%s\n\n"
        "%s消息：%s\n\n"
        "只输出 JSON，不要解释：\n"
        '{"is_relay":true,"kind":"remind","target":"清单里的名字或 owner",'
        '"targets":["清单里的名字"],'
        '"candidates":[],"need_target":false,"content":"要转达的话",'
        '"confidence":0.9,"tz":"","reason":"","intent":"new_relay"}\n'
        "规则：\n"
        "1. 必须是让橙子去对别人说点什么。只说自己的事、抱怨、反问、过去的事 → "
        "is_relay=false。\n"
        "2. target、targets、candidates 用清单每行第一个｜前的称呼，可照抄"
        "「姓名（昵称）」，也可以填登记的姓名或昵称。不要缩写或猜别名。"
        "说话人用「他/她」指自己主人时填 owner。\n"
        "3. 确定是在托你、但不确定是谁 → is_relay=true，target 留空，"
        "need_target=true，candidates 列出圈内可能的 1~3 个名字（宁缺毋滥）。\n"
        "4. 说话人**明确点名了一个不在清单里的人**（「提醒小张」）→ is_relay 仍为 "
        "true，target 填这个名字，need_target=true，reason 写「不在关系网」。"
        "只有确实不是托你办事（闲聊、自己的事、反问）才 is_relay=false。\n"
        "5. content 只写要转达的话本身，不要「提醒/催/转告/告诉/问问」这些动词。\n"
        "6. kind 只能是 remind 或 say：\n"
        "   remind = 让他到点做某事（「提醒他八点吃药」「催他交作业」）\n"
        "   say    = 替他说/问一句（「问问她晚饭吃什么」「告诉他我晚点到」）\n"
        "   问句和问候都归 say。替他问好、说晚安时，content 写「你好」「晚安」等问候。\n"
        "7. 委托里带重复（「每天」「每周三」「每两天」）→ rule 填 daily / "
        "weekly:3 / interval:2 / monthly:15，next 填**第一次**该说的时间"
        "（「明天我走了，你记得每天叫他去上课」→ rule=daily，next=明天早上）；\n"
        "   不带重复 → rule=single，next 留空（马上发）。\n"
        "8. 对方提到自己在哪个国家或城市（例如「我在英国」）→ tz 填 IANA "
        "时区名（Europe/London、Asia/Tokyo）；没提到就填空字符串。\n"
        "9. confidence 取 0~1；不确定就低于 0.5。\n"
        "10. 一句话同时托了好几个人（「帮我跟组长和小李说一声」"
        "「帮我问问 B 和 C 几点到」）→ targets 填全部名字（[\"组长\",\"小李\"]），"
        "target 填第一个；只托一个人 → targets 也填这一个。"
        "content 里别带收件人的名字，称呼对方用「你」，同时问好几个人就用「你们」，"
        "不要用「他们」。\n"
        "11. 如果这句话是让对方把一句回话带回去（「你代我向他说声…」"
        "「你帮我回他一句…」），intent 填 relay_back，target 填当初托你传话的那个人，"
        "content 写成一句完整、能直接发出去的话：用他这个人的名字当主语，"
        "对收件人用「你」，例如「联系人A让我提醒你明天带伞」。\n"
        "12. 不是让你把话带回去的委托 intent 填 new_relay；不是委托就填 none。\n"
    ) % (requester or "对方", roster, bg, (text or "")[:200])
    try:
        r = llm_chat([{"role": "user", "content": prompt}], cfg, timeout=30)
    except Exception as e:
        log.warning("代传话判定模型调用失败：%s" % e)
        return None
    if not r.get("ok"):
        return None
    raw = (r.get("text") or "").strip()
    if "{" in raw:
        raw = raw[raw.find("{"):raw.rfind("}") + 1]
    if not raw:
        return None
    try:
        obj = _json.loads(raw)
    except Exception:
        log.warning("代传话判定输出不是 JSON：%s" % redact(" ".join(raw.split())[:80], cfg))
        return None
    if not isinstance(obj, dict):
        return None
    try:
        confidence = float(obj.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if not 0 <= confidence <= 1:
        confidence = 0.0
    target = obj.get("target")
    target = target.strip() if isinstance(target, str) else ""
    targets = _model_names(obj.get("targets"), 5)
    if target and target not in targets:
        targets.insert(0, target)
    if not targets and target:
        targets = [target]
    intent = str(obj.get("intent") or "").strip().lower()
    if intent not in ("relay_back", "new_relay", "none"):
        intent = "new_relay" if obj.get("is_relay") is True else "none"
    is_relay = obj.get("is_relay") is True or intent in ("relay_back", "new_relay")
    kind = str(obj.get("kind") or "").strip().lower()
    if kind not in ("say", "remind"):
        kind = "say" if intent == "relay_back" else "remind"
    return {
        "intent": intent,
        "is_relay": is_relay,
        "kind": kind,
        "rule": reminder_parse.valid_rule(obj.get("rule")) or "single",
        "next_local": str(obj.get("next") or "").strip()[:20],
        "target": target or (targets[0] if targets else ""),
        "targets": targets,
        "candidates": _model_names(obj.get("candidates"), 4),
        "need_target": bool(obj.get("need_target")),
        "content": str(obj.get("content") or "").strip()[:60],
        "confidence": confidence,
        "tz": str(obj.get("tz") or "").strip()[:40],
        "reason": str(obj.get("reason") or "").strip()[:60],
    }


def _model_names(value, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [name.strip() for name in value
            if isinstance(name, str) and name.strip()][:limit]


def _alias_key(value: str) -> str:
    """称呼归一：全半角、空白、包裹符号、所有格前缀、句尾语气词。

    「你哥哥」「哥哥」「哥哥呀」归一到同一个键，这样关系表里写「你的哥哥」、
    对方嘴里说「你哥哥」也能对上；但只做前后缀归一，**不截短名字**
    （「柯」永远不等于「柯新」），免得把两个不同的人并成一个。
    """
    value = "".join(unicodedata.normalize("NFKC", value or "").split()).strip(_ALIAS_ENDS)
    while len(value) > 1 and _ALIAS_WRAPPERS.get(value[0]) == value[-1]:
        value = value[1:-1].strip(_ALIAS_ENDS)
    stripped = _ALIAS_PREFIX.sub("", value)
    if len(stripped) >= 2 and stripped != value:
        value = stripped
    tailed = _ALIAS_TAIL.sub("", value)
    if tailed and tailed != value:
        value = tailed
    return value.casefold()


# 固定答复也要走同一套归一：多说一个「呀」「了」，取消和否认可不能再失灵。
_CLARIFY_YES_KEYS = frozenset(_alias_key(v) for v in _CLARIFY_YES)
_CLARIFY_NO_KEYS = frozenset(_alias_key(v) for v in _CLARIFY_NO)
_CLARIFY_CANCEL_KEYS = frozenset(_alias_key(v) for v in _CLARIFY_CANCEL)


def _relation_chats(rels: list[dict], hint: str) -> list[str]:
    """整个标签必须对应同一条关系，两个不同联系人的姓名不能拼在一起。

    姓名、昵称之外，``relation`` 字段（「你的哥哥」）也参与匹配：
    对方嘴里说的是「你哥哥」，关系表里写的是「你的哥哥」，归一后是同一个键。
    """
    chats = []
    for rel in rels:
        name = _alias_key(rel.get("name") or "")
        nick = _alias_key(rel.get("nickname") or "")
        aliases = {name, nick, _alias_key(rel.get("relation") or "")} - {""}
        if name and nick and name != nick:
            aliases.update((f"{name}({nick})", f"{nick}({name})"))
        if hint in aliases:
            chats.append((rel.get("chat") or "").strip())
    return chats


def _addressable(cfg: dict, chat: str, relations: list[dict] | None = None) -> bool:
    """这个人收得到代传话吗：关系网里绑了会话，或者他本人就是主人。

    主人即使没写进关系表也一定能收到——他就是配置里的 ``owner_wxid``，
    关系表只负责「该叫他什么」，不该变成一道发送权限。
    """
    chat = (chat or "").strip()
    if not chat:
        return False
    if chat in network_chats(relations):
        return True
    owner = owner_chat(cfg)
    return bool(owner) and chat == owner


def target_of(cfg: dict, hint: str,
              relations: list[dict] | None = None,
              *, context: dict | None = None) -> tuple[str, str]:
    """模型（或对方本人）给的收件人 → 会话 wxid。只做映射，不再猜。

    返回 ``(会话, 拒绝原因)``；成功时原因为空串。关系网里有两个同名但会话
    不同的联系人时算歧义，不乱挑一个。

    ``context`` 用来消解「他/她」：在 B 的会话里说「他」，指的是当初托橙子
    传话的那个人（``relay_from``），不是主人；没有这层上下文才退回主人。
    """
    rels = relations if relations is not None else _relations()
    if len(hint or "") > _MODEL_NAME_LIMIT:
        return "", "关系网里没这个人"
    hint = _alias_key(hint)
    if not hint:
        return "", "没说清发给谁"
    if hint in _OWNER_WORDS:
        # 「主人 / owner」是明确称呼：只认配置里的主人，不走上下文，
        # 否则上一位委托人会顶替主人（实测会选错人）。
        target = owner_chat(cfg)
        if not target:
            return "", "没有他的微信"
        return target, ""
    if hint in _PRONOUN_HINTS:
        # 只有「他 / 她」才按上下文消解
        near = str((context or {}).get("counterpart")
                   or (context or {}).get("relay_from") or "").strip()
        if near:
            return near, ""
        target = owner_chat(cfg)
        if not target:
            return "", "没有他的微信"
        return target, ""
    hits = _relation_chats(rels, hint)
    if not hits:
        return "", "关系网里没这个人"
    uniq = set(hits)
    if len(uniq) > 1 or hits.count("") > 1:
        return "", "关系网里有两个人叫这个名字"
    if not hits[0]:
        return "", "关系里没绑定会话"
    return hits[0], ""


def _ask_target(candidates: list[str], options: list[str] | None = None) -> str:
    """追问收件人。候选为空时也不能干巴巴地问一句「你要发给谁」。"""
    names = [c for c in (candidates or []) if c][:_ASK_OPTION_LIMIT]
    if len(names) == 1:
        return "你要发送的人是%s吗？回「对」确认，或者告诉我该发给谁。" % names[0]
    if names:
        return "你要发给%s？请回关系表里登记的姓名或昵称。" % "还是".join(names)
    opts = [o for o in (options or []) if o][:_ASK_OPTION_LIMIT]
    if len(opts) == 1:
        return "你要发给%s吗？回「对」确认，或者直接说名字。" % opts[0]
    if opts:
        return "你要发给谁？回名字或序号都行：%s。" % "、".join(
            "%d.%s" % (i + 1, o) for i, o in enumerate(opts))
    return "你要发给谁？回关系表里登记的姓名、昵称或称呼都行。"


def _ask_again(options: list[str] | None = None) -> str:
    """同一句话别问第二遍：换成带序号的问法，或者干脆请他取消。"""
    opts = [o for o in (options or []) if o][:_ASK_OPTION_LIMIT]
    if opts:
        return "没听明白。回序号就行：%s。" % "、".join(
            "%d.%s" % (i + 1, o) for i, o in enumerate(opts))
    return "没听明白要发给谁。说个名字或称呼，或者回「算了」取消。"


def _safe_label(chat: str, rels: list[dict]) -> str:
    """能拿去给用户看的称呼：真名/昵称可以，微信号和群号不行。

    关系表可能是从旧版按会话 ID 自动迁过来的，``name`` 里会残留 ``wxid_``；
    同一条规则在 ``_clarify_contacts`` 里有，这里统一收口，别让备用分支把
    微信号当名字问出来。
    """
    name = display_name(chat, rels) or ""
    key = _alias_key(name)
    if not name or re.search(r"wxid_|@chatroom|gh_", key):
        return ""
    return name


def _owner_label(cfg: dict, rels: list[dict]) -> str:
    """主人该怎么被称呼：优先关系表里的名字，没有就退回「主人」。"""
    owner = owner_chat(cfg)
    if not owner:
        return ""
    return _safe_label(owner, rels) or "主人"


def _deliverable(cfg: dict, chat: str) -> bool:
    """这个会话现在能不能发：真人私聊、不在黑名单。"""
    chat = (chat or "").strip()
    if not chat:
        return False
    agent = (cfg or {}).get("agent") or {}
    if chat in (agent.get("reply_blacklist") or []):
        return False
    return gate.is_private_chat(chat, agent)


def _clarify_options(cfg: dict, source: str, rels: list[dict],
                     text: str = "", context: dict | None = None) -> list[str]:
    """追问时能摆出来的选项：本次对话的对面、主人，其余按关系表顺序，最多三个。

    ``text`` 是对方刚才那句答复：已经明确否掉的人不再摆出来
    （「别发给 alex」之后不能再建议 alex）。``context`` 里的 ``counterpart``
    是这次代传话的对面（我是被打扰的一方时就是发起人），排在最前面。
    """
    contacts = _clarify_contacts(cfg, source, rels)
    answer = _alias_key(text or "")
    out: list[str] = []
    owner = owner_chat(cfg)
    near = str((context or {}).get("counterpart") or "").strip()
    if near and near != source and _deliverable(cfg, near):
        label = _safe_label(near, rels)
        labels = contacts.get(near, [])
        if label and not (answer and labels and _explicitly_rejected(answer, labels)):
            out.append(label)
    if owner and owner != source and owner != near and _deliverable(cfg, owner):
        label = _owner_label(cfg, rels)
        labels = contacts.get(owner, [])
        if label and not (answer and labels and _explicitly_rejected(answer, labels)):
            out.append(label)
    for recipient, labels in contacts.items():
        if recipient in (owner, near) or not labels:
            continue
        if answer and _explicitly_rejected(answer, labels):
            continue
        out.extend(labels[:1])
    return [x for x in dict.fromkeys(out) if x][:_ASK_OPTION_LIMIT]


def _clarify_contacts(cfg: dict, source: str, rels: list[dict]) -> dict[str, list[str]]:
    """只使用已登记、可转达的姓名或昵称作为核对选项。"""
    contacts: dict[str, list[str]] = {}
    ids = {_alias_key(c) for c in network_chats(rels)}
    agent = (cfg or {}).get("agent") or {}
    blocked = set(agent.get("reply_blacklist") or [])
    for rel in rels:
        recipient = (rel.get("chat") or "").strip()
        if not recipient or recipient == source or recipient in blocked or not gate.is_private_chat(recipient, agent):
            continue
        names = []
        for field in ("name", "nickname"):
            name = (rel.get(field) or "").strip()
            key = _alias_key(name)
            if (key and not any(key == id_ or (len(id_) >= 6 and id_ in key) for id_ in ids)
                    and key not in ("owner", "他", "她", "主人")
                    and not re.search(r"wxid_|@chatroom|gh_", key) and name not in names):
                names.append(name)
        if len(names) == 2:
            names.append(f"{names[0]}（{names[1]}）")
        if names:
            labels = contacts.setdefault(recipient, [])
            labels.extend(name for name in names if name not in labels)
    return contacts


def _clarify_choices(cfg: dict, source: str, rels: list[dict], hints,
                     context: dict | None = None) -> tuple[list[str], str]:
    contacts = _clarify_contacts(cfg, source, rels)
    suggested = {}
    for hint in hints:
        if not hint or len(hint) > _MODEL_NAME_LIMIT:
            continue
        key = _alias_key(hint)
        hits = _relation_chats(rels, key)
        if key in _OWNER_HINTS:
            # 「他/她」按上下文解析：B 的会话里指的是当初托事的那个人
            near = target_of(cfg, key, rels, context=context)[0]
            hits = [near or owner_chat(cfg)]
        for recipient in hits:
            # 一个联系人的简称若和其他人冲突，优先找能区分他的姓名或完整标签。
            matching = [label for label in contacts.get(recipient, [])
                        if target_of(cfg, label, rels)[0] == recipient]
            if matching:
                suggested.setdefault(recipient, matching[0])
    labels = list(suggested.values())[:_ASK_OPTION_LIMIT]
    confirm_chat = next(iter(suggested)) if len(suggested) == 1 else ""
    return labels, confirm_chat


def _is_negated(text: str, idx: int, name: str) -> bool:
    """这个名字在这句话里是不是被否定了。

    三种写法都要认：
    * 前置：「不是小王」
    * 紧跟：「小王不是」「小王也不在」
    * 集合后置：「小王和小李都不是」（名字后面到句尾带「都/全 + 不是」）
    """
    clauses = re.split(r"[,;、。]", text[:idx])
    head = clauses[-1].rstrip("\"'“‘「『【")
    if not head and len(clauses) > 1:
        head = clauses[-2]
    rest = text[idx + len(name):].lstrip("\"'”’」』】,、;。")
    if re.search(r"(不是|不对|不要|不用|不|别是|别|并非|而不是|除了|除)"
                 r"(?:发送给|发给|给|发)?\s*$", head):
        return True
    if re.match(r"\s*(都|也)?\s*(不|没在|别发)", rest):
        return True
    if re.search(r"(都|全|也)\s*(不是|不对|没在|不在|不行)", rest):
        return True
    return False


def judge_clarify(cfg: dict, text: str, pending: dict,
                  relations: list[dict] | None, requester: str = "",
                  context: dict | None = None) -> dict | None:
    """模型判读「对方这句答复是什么意思」。

    每轮只问一个人，所以只要分清：是不是他（confirm）、换成谁（pick）、
    不是他（reject）、不发了（cancel）、听不出（unclear）、还是根本不在答这件事
    （not_answer，交回主流程）。

    返回 ``{"decision", "target", "confidence", "reason"}``；
    模型不可用时返回 ``None``，由调用方用字面兜底（``_coarse_decision``）。
    """
    try:
        from llm import chat as llm_chat
    except Exception:
        return None
    import json as _json

    rels = relations if relations is not None else _relations()
    proposed_chat = (pending.get("confirm_chat") or "").strip()
    proposed = display_name(proposed_chat, rels) or proposed_chat or ""
    try:
        cands = _model_names(json.loads(pending.get("candidates") or "[]"), 5)
    except (TypeError, ValueError):
        cands = []
    names = "、".join(dict.fromkeys([*cands, *_pending_options(pending)])) or "（没有别的候选）"
    roster = "\n".join(_relation_lines(rels)) or "（关系网是空的）"
    asked = (pending.get("ask") or "").strip()
    content = (pending.get("content") or "").strip()
    bg = ""
    if context and context.get("relay_from"):
        bg = "背景：这是「%s」那边的委托。\n" % (context.get("relay_from_name")
                                              or context.get("relay_from"))
    prompt = (
        "你是橙子，正在替人传话。\n"
        "要转达的内容：「%s」\n"
        "你刚刚问对方的话：「%s」\n"
        "现在对方（%s）回你：%s\n"
        "%s"
        "你的任务：判断这句话对上面那句问话是什么意思。\n"
        "关系网：\n%s\n"
        "还能问的人：%s\n"
        "上一句问的是：%s\n\n"
        "只输出 JSON，不要解释：\n"
        '{"decision":"confirm","target":"","confidence":0.9,"reason":""}\n'
        "decision 只能取：\n"
        "  confirm    = 肯定你问的那个人（对/是/没错/没毛病/可以/行/好的/嗯/就他 都算）\n"
        "  reject     = 否定你问的那个人，又没说换成谁（不是他 / 不是 / 弄错了）\n"
        "  pick       = 点名了关系网里的另一个人（那发给小李 / 不是他，是小李）→ target 填名字\n"
        "  cancel     = 不发了 / 算了 / 取消\n"
        "  unclear    = 像在回答这件事，但看不出到底是谁\n"
        "  not_answer = 这句不是在回答这件事（换话题、闲聊、另起一个新委托）\n"
        "规则：\n"
        "1. 只看意思，不抠字面：错字、语气词、方言、反问都按意思判。\n"
        "2. 先肯定又改人（「对我，不过发给小李」）→ pick。\n"
        "3. 「他/她/你哥哥/组长」这类称呼先按关系网的称呼对号；明显在指你刚问的"
        "那个人 → confirm。\n"
        "4. 拿不准就填 unclear，别硬猜。\n"
        "5. target 用关系表里的姓名或昵称原样；他明确点了别人（哪怕那人不在关系表里）"
        "也照原样填，别改写成关系表里的人——代码会负责告诉他发不了。\n"
    ) % (content[:80], asked[:80], requester or "对方", (text or "")[:200],
         bg, roster, names, proposed or "（还没问到具体的人）")
    try:
        r = llm_chat([{"role": "user", "content": prompt}], cfg, timeout=20)
    except Exception as e:
        log.warning("代传答复判读失败：%s" % e)
        return None
    if not r.get("ok"):
        return None
    raw = (r.get("text") or "").strip()
    if "{" in raw:
        raw = raw[raw.find("{"):raw.rfind("}") + 1]
    try:
        obj = _json.loads(raw)
    except Exception:
        log.warning("代传答复判读不是 JSON：%s" % redact(" ".join(raw.split())[:80], cfg))
        return None
    if not isinstance(obj, dict):
        return None
    decision = str(obj.get("decision") or "").strip().lower()
    if decision not in _CLARIFY_DECISIONS:
        decision = "unclear"
    try:
        confidence = float(obj.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if not 0 <= confidence <= 1:
        confidence = 0.0
    return {"decision": decision,
            "target": str(obj.get("target") or "").strip()[:40],
            "confidence": confidence,
            "reason": str(obj.get("reason") or "").strip()[:60]}


def _coarse_decision(text: str) -> dict | None:
    """模型不可用时的兜底：只认字面完全一致的几个短答复，别猜。

    正常路径一律由 ``judge_clarify`` 判；这里只是「模型挂了也别把整条链路卡死」
    的安全网，认得「对 / 不是 / 算了」就够。
    """
    src = (text or "").strip()
    if not src or re.search(r"[?？]", src):
        return None
    key = re.sub(r"[,、]", "", _alias_key(src))
    if key in _CLARIFY_CANCEL_KEYS:
        return {"decision": "cancel", "target": "", "confidence": 1.0, "reason": "字面"}
    if key in _CLARIFY_YES_KEYS:
        return {"decision": "confirm", "target": "", "confidence": 1.0, "reason": "字面"}
    if key in _CLARIFY_NO_KEYS:
        return {"decision": "reject", "target": "", "confidence": 1.0, "reason": "字面"}
    return None


_DENY_WORDS = re.compile(r"(不是|不对|不要|不用|别|弄错|搞错|错了|换人)")
_AFFIRM_EXCEPT = re.compile(r"(没错|没问题|没毛病|不错)")
_CANCEL_WORDS = re.compile(r"(算了|取消|不发了|不用发|别发|先别发|不弄了)")


def _denies(text: str) -> bool:
    """这句话里有没有明确的否定（「没错 / 没问题」这类肯定不算否定）。"""
    src = text or ""
    return bool(_DENY_WORDS.search(src)) and not _AFFIRM_EXCEPT.search(src)


def _looks_cancel(text: str) -> bool:
    return bool(_CANCEL_WORDS.search(text or ""))


def _grounded(cfg: dict, hit: str, text: str, rels: list[dict],
              context: dict | None = None) -> bool:
    """模型说「他点了 X」时，X 必须真的能从这句话里读出来。

    否则就是模型自己编了一个收件人——宁可再问一遍，也不能发给别人。
    """
    hit = (hit or "").strip()
    key = _alias_key(text or "")
    if not hit or not key:
        return False
    names = {_alias_key(hit)} - {""}
    chat_id, _why = target_of(cfg, hit, rels, context=context)
    if chat_id:
        for rel in rels:
            if (rel.get("chat") or "").strip() != chat_id:
                continue
            for field in ("name", "nickname", "relation"):
                label = _alias_key(rel.get(field) or "")
                if label:
                    names.add(label)
    return any(name and name in key for name in names)


def _explicitly_rejected(text: str, labels: list[str]) -> bool:
    for label in labels:
        name = _alias_key(label)
        if any(_is_negated(text, match.start(), name)
               for match in re.finditer(re.escape(name), text)):
            return True
    return False


def _pending_options(pending: dict) -> list[str]:
    try:
        opts = json.loads(pending.get("options") or "[]")
    except (TypeError, ValueError):
        opts = []
    return [o for o in opts if isinstance(o, str) and o.strip()][:_ASK_OPTION_LIMIT]


def answer_clarify(cfg: dict, q, chat: str, text: str, account: str,
                   relations: list[dict] | None, msg_key: str = "", *,
                   msg_ts: int | None = None, msg_seq: int | None = None,
                   context: dict | None = None) -> dict | None:
    """发起人在回答「你要发给谁」。由模型判读这句答复，代码只管落库和发送。

    * 判读交给 ``judge_clarify``：对、不是、换成谁、算了，全按意思来，不再靠词表；
      模型不可用才退回字面兜底（``_coarse_decision``）。
    * 每轮只问一个人（概率最大的那个）；被否掉就把他从候选里剔掉再问下一个。
    * 最多问三次；三次还定不下来就结案，如实告诉发起人「没确定，这次没发」。
    """
    t = (text or "").strip()
    rels = relations if relations is not None else _relations()
    if not t or len(t) > _ANSWER_MAX_CHARS or not requester_ok(cfg, chat, rels):
        return None
    if msg_key and q.has_action(account, chat, msg_key):
        return None
    pending = q.get_relay_clarify(account, chat)
    if not pending:
        return None
    if pending.get("prompt_status") in ("cancelled", "expired", "unverified"):
        with q.tx() as c:
            q.set_relay_clarify(pending["id"], "expired", conn=c)
            if msg_key:
                q.record_action(account, chat, msg_key, None,
                                "relay_clarify_prompt_failed", conn=c)
        return {"ok": False, "receipt": _CLARIFY_RECEIPTS["relay_clarify_prompt_failed"]}
    if not pending.get("prompt_outbound_id"):
        # 老记录：问句还没排出去，补排一次
        cands, _confirm = _clarify_choices(
            cfg, chat, rels,
            _model_names(json.loads(pending.get("candidates") or "[]"), 3),
            context=context)
        options = _clarify_options(cfg, chat, rels, context=context)
        ordered = list(dict.fromkeys([*cands, *options]))
        first = ordered[0] if ordered else ""
        first_chat = target_of(cfg, first, rels, context=context)[0] if first else ""
        ask = _ask_target([first] if first_chat else [], ordered)
        with q.tx() as c:
            if q.advance_relay_clarify(pending["id"], ordered, ask,
                                       confirm_chat=first_chat, options=ordered,
                                       ask_core=ask, conn=c):
                q.enqueue_relay_clarify_prompt(account, chat, ask, conn=c)
        return {"ok": False, "pending": True, "ask": ask}
    # 「算了 / 不发了」是明确取消，不必等问句回读完成，也不用麻烦模型
    coarse = _coarse_decision(t)
    if coarse and coarse["decision"] == "cancel":
        with q.tx() as c:
            q.set_relay_clarify(pending["id"], "cancelled", conn=c)
            if msg_key:
                q.record_action(account, chat, msg_key, None,
                                "relay_clarify_cancelled", conn=c)
        return {"ok": False, "receipt": _CLARIFY_RECEIPTS["relay_clarify_cancelled"]}
    received = int(msg_ts if msg_ts is not None else time.time())
    if not pending.get("prompt_sent_ts") or not q.relay_reply_is_new(
            pending["prompt_sent_ts"], pending.get("prompt_verified_seq") or 0,
            received, msg_seq):
        return {"ok": False, "pending": True}
    # 这条答复之前有没有更新的问句？有的话，「对 / 不是」可能是在回那句新的。
    # 但明确点名、明确取消不受影响——那是信息，不是含糊的短答复。
    newer = q.has_newer_clarify_prompt(account, chat, pending["prompt_outbound_id"],
                                       msg_ts=received, msg_seq=msg_seq)

    # 判读放在事务外面：这一步要等模型，不该占着写锁
    decision = judge_clarify(cfg, t, pending, rels,
                             requester=display_name(chat, rels) or chat,
                             context=context)
    if decision is None:
        decision = coarse or _coarse_decision(t)   # 模型不可用：只认字面
        if decision is None:
            log.info("代传答复看不懂（模型不可用），等下一句 [%s]" % chat)
            return {"ok": False, "pending": True}
    kind = decision.get("decision") or "unclear"
    # 模型只负责解释，下面这几道闸由代码把关：一次误判不能把话说到别人头上。
    if kind == "confirm" and _denies(t):
        kind = "reject"                # 明说「不要/不是」，绝不当确认
    if kind == "cancel" and not _looks_cancel(t):
        kind = "unclear"               # 没说要取消，不能替人取消
    if kind == "pick" and not _grounded(cfg, decision.get("target") or "", t,
                                        rels, context):
        kind = "unclear"               # 点了名就必须真的出现在这句话里
    if kind in ("confirm", "reject", "pick") and \
            float(decision.get("confidence") or 0) < 0.5:
        kind = "unclear"               # 模型自己都没把握，别落单
    decision["decision"] = kind
    if kind == "not_answer":
        return None                    # 不是在回答这件事，交回主流程
    if newer and kind in ("confirm", "reject", "unclear"):
        return None

    target = ""
    rid = None
    try:
        with q.tx() as c:
            # 模型调用要几秒，期间可能已经结案或换过问句：落库前再核一次
            if msg_key and q.has_action(account, chat, msg_key, conn=c):
                return None
            now_pending = q.get_relay_clarify(account, chat, conn=c)
            if (not now_pending or now_pending["id"] != pending["id"]
                    or now_pending.get("prompt_outbound_id")
                    != pending.get("prompt_outbound_id")):
                return {"ok": False, "pending": True}
            pending = now_pending
            cands = _model_names(json.loads(pending.get("candidates") or "[]"), 5)
            options = _pending_options(pending)
            proposed = (pending.get("confirm_chat") or "").strip()
            kind = decision.get("decision") or "unclear"
            hit_name = ((decision.get("target") or "").strip()
                        if kind == "pick" else "")
            pick_miss = ""

            if kind == "cancel":
                q.set_relay_clarify(pending["id"], "cancelled", conn=c)
                if msg_key:
                    q.record_action(account, chat, msg_key, None,
                                    "relay_clarify_cancelled", conn=c)
                return {"ok": False, "receipt": _CLARIFY_RECEIPTS["relay_clarify_cancelled"]}

            if kind == "confirm" and proposed:
                # 就发给刚才问的那个人；但关系表可能已经改绑了——先按名字重算，
                # 对不上就重新核对，别沿用一个过期的会话。
                asked_label = cands[0] if cands else ""
                now_chat = (target_of(cfg, asked_label, rels, context=context)[0]
                            if asked_label else "")
                if asked_label and now_chat != proposed:
                    kind = "unclear"
                else:
                    target = proposed if _deliverable(cfg, proposed) else ""
            elif kind == "pick" and hit_name:
                target, why = target_of(cfg, hit_name, rels, context=context)
                if not target:
                    pick_miss = why or "关系网里没这个人"
            # reject / unclear / 确认了一个发不了的人 → 都走「换下一个」

            if target and not _deliverable(cfg, target):
                q.set_relay_clarify(pending["id"], "expired", conn=c)
                if msg_key:
                    q.record_action(account, chat, msg_key, None,
                                    "relay_clarify_unreachable", conn=c)
                return {"ok": False, "receipt":
                        "「%s」这个会话现在发不了消息，这次没有发送。"
                        % (display_name(target, rels) or target)}

            if not target:
                # 把这次问到/否掉的人剔掉，剩下的按原顺序挑下一个问
                drop = {proposed} if proposed else set()
                if hit_name:
                    drop_chat = target_of(cfg, hit_name, rels, context=context)[0]
                    if drop_chat:
                        drop.add(drop_chat)
                remaining = []
                for label in dict.fromkeys([*cands, *options]):
                    label_chat = target_of(cfg, label, rels, context=context)[0]
                    if not label_chat or label_chat in drop or label_chat == chat:
                        continue
                    remaining.append(label)
                if pending.get("asks", 0) >= _CLARIFY_MAX_ASKS:
                    q.set_relay_clarify(pending["id"], "expired", conn=c)
                    if msg_key:
                        q.record_action(account, chat, msg_key, None,
                                        "relay_clarify_exhausted", conn=c)
                    return {"ok": False,
                            "receipt": _CLARIFY_RECEIPTS["relay_clarify_exhausted"]}
                next_label = remaining[0] if remaining else ""
                next_chat = (target_of(cfg, next_label, rels, context=context)[0]
                             if next_label else "")
                ask = _ask_target([next_label] if next_chat else [], remaining)
                if pick_miss:
                    # 他点了名，但这个人发不出去：说清楚，别再装作没听见
                    ask = ("「%s」%s，这条发不出去。要发给谁？回名字或称呼都行。"
                           % (hit_name, _MISS_TEXT.get(pick_miss, "现在没法发")))
                    next_chat = ""
                prev_ask = (pending.get("ask_core") or "").strip()
                if ask == prev_ask:
                    ask = _ask_again(remaining)
                if ask == prev_ask:
                    # 换一种问法还是同一句：别再复读了，结案
                    q.set_relay_clarify(pending["id"], "expired", conn=c)
                    if msg_key:
                        q.record_action(account, chat, msg_key, None,
                                        "relay_clarify_exhausted", conn=c)
                    return {"ok": False,
                            "receipt": _CLARIFY_RECEIPTS["relay_clarify_exhausted"]}
                if q.advance_relay_clarify(pending["id"], remaining, ask,
                                           confirm_chat=next_chat, options=remaining,
                                           ask_core=ask, conn=c):
                    q.enqueue_relay_clarify_prompt(account, chat, ask, conn=c)
                    if msg_key:
                        q.record_action(account, chat, msg_key, None,
                                        "relay_clarify_asked", conn=c)
                return {"ok": False, "pending": True, "ask": ask}

            rid = deliver(cfg, q, chat, target, pending.get("content") or "",
                          from_name=display_name(chat, rels) or chat,
                          account=account, relations=rels, src_key=msg_key, conn=c,
                          verbatim=(pending.get("kind") == "say"),
                          watch_back=(pending.get("kind") == "say"))
            q.set_relay_clarify(pending["id"], "done", conn=c)
            if not rid and msg_key:
                q.record_action(account, chat, msg_key, None,
                                "relay_delivery_refused", conn=c)
    except Exception as e:
        log.warning("代传确认落库失败 [%s]：%s: %s" % (chat, type(e).__name__, e))
        return {"ok": False, "reason": "这条没发出去，稍后再说一次"}
    if not rid:
        return {"ok": False, "reason": "这条发不出去"}
    return {"ok": True, "rid": rid, "chat": target,
            "name": display_name(target, rels) or target,
            "content": pending.get("content") or ""}


def network_chats(relations: list[dict] | None = None) -> set[str]:
    """关系网里真正能发消息的那些会话（绑定了会话的关系）。"""
    rels = relations if relations is not None else _relations()
    return {(r.get("chat") or "").strip() for r in rels if r.get("chat")}


def requester_ok(cfg: dict, chat: str, relations: list[dict] | None = None) -> bool:
    """发起人必须在关系网里，且没被拉黑——不然谁都能指挥橙子跑腿。"""
    rels = relations if relations is not None else _relations()
    if not (chat or "").strip():
        return False
    agent = (cfg or {}).get("agent") or {}
    if chat in (agent.get("reply_blacklist") or []):
        return False
    return (chat or "").strip() in network_chats(rels)


def _last_partner(q, account: str, chat: str, watch: dict | None = None) -> str:
    """这个会话最近一次代传话的对面是谁（用来消解「他/她」）。

    有在途回传（我是被问的 B）→ 发起人 A；否则查最近一条代传记录
    （我是发起人 A）→ 被问的 B。查不到就空串，退回主人。
    """
    if watch:
        return (watch.get("from_chat") or "").strip()
    try:
        return (q.recent_relay_partner(account, chat) or "").strip()
    except Exception:
        return ""


def _back_sentence(text: str, who: str) -> str:
    """模型写不出来时的兜底：把「你代我向他问声不好」变成能直接发的一句。"""
    t = re.sub(
        r"^(?:那|嗯|好|行|你|您|请)*\s*(?:代我|替我|帮我|带我|麻烦你|帮忙)?\s*"
        r"(?:向|跟|和|对|给)?\s*(?:他|她)\s*[，,、]?\s*", "", (text or "").strip())
    t = t.strip("，,。.！!~ ")
    return ("%s让我跟你说：%s" % (who, t)) if t else ("%s说有事找你。" % who)


def _relay_cfg(cfg: dict) -> dict:
    return ((cfg or {}).get("proactive") or {}).get("relay") or {}


# ------------------------------------------------------------------ 人设改写
# 追问和回执是「程序发的消息」：必须带上选项、迟早、发没发这些事实，所以由代码
# 生成。但微信里一句话全是系统腔会很割裂，于是发之前再让模型按人设说一遍——
# **只改外壳**：事实缺一条就退回原句（fail-open），宁可生硬也不许说错。
def _polish_on(cfg: dict) -> bool:
    return bool(_relay_cfg(cfg).get("polish", True))


def _persona_excerpt(cfg: dict, limit: int = 400) -> str:
    """改写时参考的语气：优先人设卡，其次 config 里的 agent.persona。"""
    text = ""
    try:
        from persona_card import effective_persona
        text = (effective_persona()[0] or "").strip()
    except Exception:
        text = ""
    if not text:
        text = str(((cfg or {}).get("agent") or {}).get("persona") or "").strip()
    return " ".join(text.split())[:limit]


def polish_text(cfg: dict, text: str, *, must=(), must_any=(), forbid=(),
                limit: int = 80, timeout: int = 8) -> str:
    """把系统文案改成橙子的口吻，只改外壳，事实一条都不能丢。

    * ``must`` —— 这些字串必须原样出现在改写结果里（收件人的名字、「对」）；
    * ``must_any`` —— 至少出现其中一个（「没发 / 没送出去」这类同义说法）；
    * ``forbid`` —— 出现任何一个就作废（例如把「没发」说成「已经收到」）。

    模型不可用、超时、输出超长或漏事实 → 原样返回。这层永远 fail-open。
    """
    src = (text or "").strip()
    if not src or not _polish_on(cfg):
        return src
    try:
        from llm import chat as llm_chat
    except Exception:
        return src
    must = [m for m in (must or []) if m]
    must_any = [m for m in (must_any or []) if m]
    forbid = [f for f in (forbid or []) if f]
    persona = _persona_excerpt(cfg)
    keep = ""
    if must:
        keep += "这些字串必须原样出现：%s。\n" % "、".join(must)
    if must_any:
        keep += "必须说到「没发出去」的意思，可以用其中一种说法：%s。\n" % "、".join(must_any)
    if forbid:
        keep += "绝对不许出现这些说法：%s。\n" % "、".join(forbid)
    prompt = (
        "你在微信里扮演「橙子」。把下面这句系统写好的机器话，改成一句自然的微信"
        "消息，只输出改好的那一句。\n"
        "%s\n"
        "要求：\n"
        "1. 意思一点都不能变，尤其「已经发了 / 没发 / 要发给谁」这类事实。\n"
        "%s"
        "2. 不超过 %d 字，一句话；不要换行、不要引号、不要解释、不要加前缀。\n"
        "3. 可以带一点口癖或颜文字，但别为了可爱把话说糊。\n"
        "原句：%s"
    ) % (("你的人设（只参考语气）：%s\n" % persona) if persona else "",
         keep, int(limit), src)
    try:
        r = llm_chat([{"role": "user", "content": prompt}], cfg,
                     timeout=max(3, int(timeout)), max_tokens=200)
    except Exception as e:
        log.warning("系统文案改写失败：%s" % e)
        return src
    out = (r.get("text") or "").strip() if isinstance(r, dict) and r.get("ok") else ""
    if not out:
        return src
    out = out.strip("\"'“”‘’『』「」 \n")
    if (not out or len(out) > limit or "\n" in out
            or any(m not in out for m in must)
            or (must_any and not any(m in out for m in must_any))
            or any(f in out for f in forbid)):
        log.info("改写后事实不全/改错或超长，沿用原句")
        return src
    return out


# 回执里绝对不能出现的说法：把「没发」改写成像已经送达
_RECEIPT_FORBID = ("已经发", "已发送", "已发出", "已经送达", "已送达",
                   "送到了", "收到了", "收到啦", "已经收到")


def polish_receipt(cfg: dict, text: str, names=()) -> str:
    """回执改成人话：谁说、发没发必须留着，别改成一句好听话。

    收件人名字要留；「没发出去 / 已取消」的意思必须在；任何「已经收到/已发出」
    的说法直接作废——这类回执本来就是在说「没办成」。
    """
    src = (text or "").strip()
    if not src:
        return src
    must = [n for n in (names or []) if n and str(n) in src]
    must_any = ("没发", "没有发", "没送", "发不了", "不发", "没法发", "取消")
    if "取消" in src:
        must.append("取消")
    return polish_text(cfg, src, must=must, must_any=must_any,
                       forbid=_RECEIPT_FORBID, limit=90)


def _foreign_names(text: str, keep_chat: str, chat: str,
                   rels: list[dict]) -> bool:
    """改写后的问句里，有没有冒出「另一个联系人」的名字。

    只比姓名/昵称（不比对关系词，免得「同学」这种日常词误伤）。有就说明模型把
    「问谁」换成了别人，这句改写必须作废——否则会出现「问小北、发小南」。
    """
    key = _alias_key(text or "")
    if not key:
        return False
    for rel in rels:
        recipient = (rel.get("chat") or "").strip()
        if not recipient or recipient in (keep_chat, chat):
            continue
        for field in ("name", "nickname"):
            label = _alias_key(rel.get(field) or "")
            if label and label in key:
                return True
    return False


def polish_clarify_task(q, cfg: dict, task: dict) -> dict:
    """发「你要发给谁」之前换成橙子的口吻；候选和「对」必须原样保留。

    只改这一次出站的正文与 ``relay_clarify.ask``（显示用），``ask_core``
    （模板）不动——判重还按模板走，换过说法也不会把同一句再问一遍。
    """
    if not _polish_on(cfg) or (task.get("kind") or "") != "relay_clarify":
        return task
    try:
        rec = q.get_relay_clarify_by_id(task["ref_id"]) if task.get("ref_id") else None
    except Exception:
        rec = None
    if (not rec or rec.get("status") != "pending"
            or rec.get("prompt_outbound_id") != task.get("id")):
        return task                   # 已经结案/过期，或者不是这条问句
    core = (rec.get("ask_core") or rec.get("ask") or "").strip()
    src = (task.get("text") or "").strip()
    if not core or src != core:
        return task                   # 已经是改写过的版本，别再改一遍
    must = _pending_options(rec)
    if "对" in core:
        must.append("对")
    text = polish_text(cfg, src, must=must, limit=80)
    if not text or text == src:
        return task
    # 改写里冒出别的联系人名字 = 把「问谁」换了人，作废回模板
    proposed_chat = (rec.get("confirm_chat") or "").strip()
    if _foreign_names(text, proposed_chat, task.get("chat") or "", _relations()):
        log.info("问句改写提到了别的联系人，回退模板 #%s" % task.get("id"))
        return task
    try:
        from reminder import segments_for
        segs = segments_for(text, cfg)
    except Exception:
        segs = [text]
    try:
        with q.tx() as conn:
            if not q.rewrite_outbound_text(task["id"], text, segs,
                                           source="model", conn=conn):
                return task
            if not q.set_relay_clarify_ask(rec["id"], text, conn=conn):
                raise RuntimeError("问句记录没更新")
    except Exception as e:
        log.warning("问句改写落库失败 #%s：%s: %s"
                    % (task.get("id"), type(e).__name__, e))
        return task
    task = dict(task)
    task["text"] = text
    task["segments"] = json.dumps(segs, ensure_ascii=False)
    task["text_source"] = "model"
    log.info("收件人追问改由模型说 #%d [%s]：%s"
             % (task["id"], task.get("chat"), redact(text, cfg)))
    return task


def clarify_send_block_reason(q, task: dict) -> str | None:
    rec = q.get_relay_clarify_by_id(task["ref_id"]) if task.get("ref_id") else None
    if not rec or rec["account"] != task["account"] or rec["chat"] != task["chat"]:
        return "找不到对应的代传话核对"
    if time.time() >= rec["expires_ts"]:
        return "代传话核对已过期"
    if (rec["status"] != "pending" or rec["prompt_outbound_id"] != task["id"]
            or rec["ask"] != task["text"]):
        return "代传话核对已更新或结案"
    if task["chat"] not in network_chats():
        return "代传话发起人已不在关系网里"
    return None


def deliver(cfg: dict, q, from_chat: str, target: str, content: str,
            *, from_name: str = "", account: str | None = None,
            now: float | None = None,
            relations: list[dict] | None = None,
            src_key: str | None = None,
            verbatim: bool = False,
            rule: str = "single",
            first_ts: int | None = None,
            watch_back: bool = False,
            watch_batch: str = "",
            conn=None) -> int | None:
    """建一条「发给他」的提醒。返回 reminder id；被门禁挡掉返回 None。

    ``src_key`` 是这条入站消息的 id：建单和「已处理」登记必须落在同一个事务里，
    否则登记写失败时提醒已经存下了，重试就会再建一条（同一句催两遍）。
    """
    rcfg = _relay_cfg(cfg)
    if not rcfg.get("enabled", True):
        return None
    if not ((cfg.get("proactive") or {}).get("reminders") or {}).get("enabled", False):
        return None
    account = account or (cfg or {}).get("account")
    target = (target or "").strip()
    content = (content or "").strip()
    if not account or not target or not content:
        return None
    if target == from_chat:
        return None
    if not requester_ok(cfg, from_chat, relations):
        return None
    if not _addressable(cfg, target, relations):
        return None                     # 网外的人一个都不发（主人除外）
    agent = (cfg or {}).get("agent") or {}
    if not gate.is_private_chat(target, agent):
        return None
    if target in (agent.get("reply_blacklist") or []):
        return None
    try:
        if int(q.count_relays_since(int(time.time()) - 86400, account=account)) >= \
                int(rcfg.get("daily_limit", 8) or 0):
            log.info("代传话超出每日上限，本轮不发")
            return None
    except Exception:
        pass
    delay = max(0.0, float(rcfg.get("delay_seconds", 4) or 0))
    # 周期的代传话（「每天叫他去上课」）用模型给的首次时间；单次的是「马上发」
    rule = rule if rule and rule != "single" and first_ts else "single"
    ts = int(first_ts) if rule != "single" else (
        int(now if now is not None else time.time()) + int(round(delay)))

    def _write(c):
        if src_key and q.has_action(account, from_chat, str(src_key), conn=c):
            return None                 # 并发/重试：已经兑现过了
        if watch_back and c.execute(
                "SELECT 1 FROM relay_watch WHERE account=? AND target_chat=? "
                "AND status IN ('pending_send','waiting','collecting') LIMIT 1",
                (account, target)).fetchone():
            if src_key:
                q.record_action(account, from_chat, str(src_key), None, "relay_target_busy", conn=c)
            return None
        rid = q.add_reminder(account, target, content, rule, ts, anchor_ts=ts,
                             confirm_mode="relay", relay_from=from_name or "有人",
                             relay_verbatim=verbatim,
                             relay_from_chat=from_chat, conn=c)
        if src_key:
            q.record_action(account, from_chat, str(src_key), rid, "relay",
                            conn=c)
        if watch_back:
            q.add_relay_watch(account, from_chat, target, content,
                              from_name=from_name or "有人", reminder_id=rid,
                              batch=watch_batch, target_name=display_name(target, relations),
                              pending_send=True, conn=c)
        return rid

    try:
        if conn is not None:
            rid = _write(conn)          # 调用方的事务：建单和它的其它写入一起提交
        else:
            with q.tx() as c:
                rid = _write(c)
    except Exception as e:
        if conn is not None:
            raise
        log.error("代传话建单失败 [%s]：%s" % (target, e))
        return None
    if rid is None:
        return None
    when = ("%.0f 秒后发" % delay) if rule == "single" else (
        "%s 首次 %s" % (rule, time.strftime("%m-%d %H:%M", time.localtime(ts))))
    log.info("代传话 #%d：%s → %s（%s，%s）" % (
        rid, redact(from_name or "有人", cfg), target,
        redact(content, cfg), when))
    return rid


def _deliver_back(cfg: dict, q, chat: str, text: str, watch: dict, *,
                  account: str, relations: list[dict] | None, requester: str,
                  context: dict | None = None, src_key: str | None = None,
                  info: dict | None = None) -> dict:
    """B 让橙子把话带回给发起人 A：落成一条直接发给 A 的「代说」。

    只有模型明确判成 ``intent=relay_back`` 才会走到这里（调用方负责），
    所以一句「帮我检查数学题」不会被当成带话。

    和正向代传话共用同一条发送链路（门禁、每日上限、去重），区别：
    * 收件人固定是 A（不再追问）；
    * 不建新的回传 watch（否则 A 一回话又被当成反托，两人乒乓）；
    * 一次委托只反托一次（``relay_watch.back_ts``），且建单和打标**同一个事务**，
      崩在中间不会出现「消息已发但没打标」→ 下次再发一遍。
    """
    a_chat = (watch.get("from_chat") or "").strip()
    if not a_chat or a_chat == chat:
        return {"ok": False, "back": True, "reason": "这条带不回去"}
    if int(watch.get("back_ts") or 0):
        return {"ok": False, "back": True,
                "receipt": "这句已经替你带过去了，来回传话先停一下哈。"}
    content = str((info or {}).get("content") or "").strip() \
        or _back_sentence(text, requester)
    try:
        with q.tx() as conn:
            rid = deliver(cfg, q, chat, a_chat, content, from_name=requester,
                          account=account, relations=relations, src_key=src_key,
                          verbatim=True, watch_back=False, conn=conn)
            if not rid:
                return {"ok": False, "back": True, "reason": "这句没法替你带回去"}
            if not q.mark_relay_watch_back(watch["id"], conn=conn):
                raise RuntimeError("这条委托已经反托过，不能再来一次")
    except Exception as e:
        log.warning("代传话反托落库失败 [%s]：%s: %s" % (chat, type(e).__name__, e))
        return {"ok": False, "back": True, "reason": "这条没带回去，稍后再试"}
    log.info("代传话反托 #%s：%s → %s（%s）" % (
        rid, redact(requester, cfg), redact(a_chat, cfg), redact(content, cfg)))
    return {"ok": True, "back": True, "rid": rid, "chat": a_chat,
            "name": _safe_label(a_chat, relations) or a_chat, "content": content}


def _expand_hints(hints: list[str], cfg: dict,
                  relations: list[dict] | None) -> list[str]:
    """模型把「A和B」当成一个名字时，拆开再试一次。

    只有拆出来的每一段都能对上人，才真的拆——名字里带「和」的（比如
    「和平」）不会被误伤。
    """
    out: list[str] = []
    rels = relations if relations is not None else _relations()
    for h in hints or []:
        normalized = _alias_key(h)
        if (len(h) > _MODEL_NAME_LIMIT or "(" in normalized or ")" in normalized
                or _relation_chats(rels, normalized)):
            out.append(h)
            continue
        parts = [p.strip() for p in re.split(r"[和与跟及、,，/]", h or "")
                 if p.strip()]
        if len(parts) > 1 and all(target_of(cfg, p, rels)[0] for p in parts):
            out.extend(parts)
        else:
            out.append(h)
    return out


def _fix_addressee(text: str, n: int) -> str:
    """转达的话要对着收件人说：一个人用「你」，好几个人用「你们」。

    「帮我问问 B 和 C，他们几点到？」原样发给 B 就成了「他们几点到」，
    B 会以为自己只是个传话的。只在明显指代收件人时换，别的一概不动。
    """
    t = text or ""
    if n > 1:
        for a, b in (("他们", "你们"), ("她们", "你们"), ("他俩", "你们俩"),
                     ("她俩", "你们俩")):
            t = t.replace(a, b)
    else:
        for a, b in (("你们俩", "你俩"), ("你们", "你")):
            t = t.replace(a, b)
    return t


def maybe_relay(cfg: dict, q, chat: str, msgs: list[dict],
                account: str | None = None,
                relations: list[dict] | None = None) -> list[dict]:
    """扫一轮入站，把「让橙子去提醒别人」的请求兑现。

    返回逐条结果：``{"ok": True, "rid", "chat", "name", "content"}``（已安排）
    或 ``{"ok": False, "name", "reason"}``（发不出去，让回复如实交代）。
    只有关系网内的人才可能真正发出去。
    """
    if (chat or "").endswith("@chatroom"):
        return []                       # 群里说「提醒他」指代不清，不做
    rels = relations if relations is not None else _relations()
    account = account or cfg.get("account")
    requester = display_name(chat, rels) or chat
    if not _relay_cfg(cfg).get("enabled", True) or not gate.reminder_entry_ok(cfg, chat)[0]:
        return []                       # 开关关着：不调模型、不写待确认
    out: list[dict] = []
    for m in msgs or []:
        if (m.get("msg_type") or m.get("type_code")) and message_kind(
                m.get("msg_type"), m.get("type_code")) != "text":
            continue
        text = (m.get("content") or "").strip()
        if not text:
            continue
        # 同一条入站消息只兑现一次。生成失败会把这批消息打回重试，不查这张表
        # 就会给同一个人重复建单（一条委托催两遍）。
        key = str(m.get("id") or "")
        if key:
            result = q.action_result(account, chat, key)
            if result is not None:
                if result in _CLARIFY_RECEIPTS:
                    out.append({"ok": False, "receipt": _CLARIFY_RECEIPTS[result]})
                elif result.startswith("relay_refused:") or result == "relay_refused":
                    out.append({"ok": False, "reason": result.partition(":")[2]})
                elif result in ("relay_pending", "relay_clarify_asked"):
                    out.append({"ok": False, "pending": True})
                continue

        def _claim(result: str):
            """一旦认定这是「代传话」，就把这条消息占住。

            不占的话它会继续流进「普通提醒」流程，被当成发起人自己的委托
            建一条提醒——既发错人，又用掉了原消息 id，之后补办也会被挡。
            """
            if not key:
                return
            try:
                q.record_action(account, chat, key, None, result)
            except Exception:
                pass
        # 他是在回我今天替他问过的那件事吗？先把在途回传读出来：
        # 「他/她」才有得指代，「你代我带句话」也才知道要带给谁。
        try:
            watch = q.relay_watch_for(account, chat)
        except Exception:
            watch = None
        context: dict = {}
        if watch:
            context = {"relay_from": (watch.get("from_chat") or "").strip(),
                       "relay_from_name": (watch.get("from_name") or "").strip(),
                       "question": (watch.get("question") or "").strip(),
                       "counterpart": (watch.get("from_chat") or "").strip()}
        else:
            partner = _last_partner(q, account, chat)
            if partner:
                context = {"counterpart": partner}
        # 先说是不是在回答上一句「你说的是谁」——「是小王」不含委托动词，
        # 得在预筛之前接住。
        answered = answer_clarify(cfg, q, chat, text, account, rels, key,
                                  msg_ts=m.get("msg_ts"), msg_seq=m.get("sort_seq"),
                                  context=context or None)
        if answered is not None:
            out.append(answered)
            continue
        # 先花零成本的正则筛一下「有没有可能是在托橙子办事」，只有可疑的
        # 那几句才调模型——真正的收件人和内容全由模型定。
        relayish = looks_like_relay(text, rels)
        if not relayish and watch and _BACK_PREFILTER.search(text) and _PRONOUN.search(text):
            # 「你替我向他道个歉」这类没有委托动词：在途回传时也交给模型判一下
            relayish = True
        if not relayish:
            continue
        if not requester_ok(cfg, chat, rels):
            log.info("代传话被拒 [%s]：发起人不在关系网" % chat)
            _claim("relay_denied")
            out.append({"ok": False, "name": "", "reason": "对方不在你的关系网里"})
            continue
        info = judge(cfg, text, rels, requester=requester, context=context or None)
        if not info:
            # 模型不可用时不猜：占住这条消息，别让它掉进普通提醒建给发起人
            log.info("代传话判定失败（模型不可用），不建单 [%s]" % chat)
            _claim("relay_unjudged")
            if _IMPERATIVE_HINT.search(text):
                # 有使唤口气又没判出结果：明确告诉回复侧别空口答应
                out.append({"ok": False, "unhandled": True,
                            "reason": "没听清要不要我替你转达"})
            continue
        # 反托只认模型明确判成 relay_back：一句「帮我检查数学题」不会再被
        # 「帮我」两个字带偏。还要确认这句确实发生在提问送达之后。
        if info.get("intent") == "relay_back":
            if not watch:
                _claim("relay_back_unmatched")
                out.append({"ok": False, "unhandled": True,
                            "reason": "他说要把话带给谁，但我这边没有对应的委托"})
                continue
            if not q.relay_reply_is_new(int(watch.get("asked_ts") or 0),
                                        watch.get("reply_after_seq"),
                                        int(m.get("msg_ts") or 0), m.get("sort_seq")):
                # 这句早于问句送达：旧消息，不能算反托
                _claim("relay_back_early")
                continue
            back = _deliver_back(cfg, q, chat, text, watch, account=account,
                                 relations=rels, requester=requester,
                                 context=context, src_key=key or None, info=info)
            if back.get("ok"):
                watch["back_ts"] = int(time.time())
            elif back.get("receipt"):
                _claim("relay_back_repeat")
            out.append(back)
            continue
        if not info.get("is_relay"):
            # 看着像在使唤人、模型又说不办——让回复侧别顺着答应
            if _IMPERATIVE_HINT.search(text):
                out.append({"ok": False, "unhandled": True,
                            "reason": info.get("reason") or "没听出要我转达什么"})
            continue
        if info.get("confidence", 0) < 0.5 and not info.get("need_target"):
            log.info("代传话把握不足（%.2f），不建单 [%s]：%s" % (
                info.get("confidence", 0), chat, redact(text[:30], cfg)))
            _claim("relay_unsure")
            if _IMPERATIVE_HINT.search(text):
                out.append({"ok": False, "unhandled": True,
                            "reason": "没把握这是不是让我转达"})
            continue
        if not (info.get("content") or "").strip():
            _claim("relay_empty")
            out.append({"ok": False, "unhandled": True})
            continue

        if info.get("tz"):
            try:
                import reminder as _reminder
                _reminder.apply_tz_hint(q, account, chat, info["tz"])
            except Exception as e:
                log.warning("代传话时区线索处理失败 [%s]：%s" % (chat, e))
        # 一句话可能托了好几个人（「帮我跟组长和小李说一声」）：逐个解析，
        # 能发的都发，发不了的单独记下来如实交代，别因为一个人卡住全句。
        hints = [h for h in (info.get("targets") or []) if h] or (
            [info["target"]] if info.get("target") else [])
        hints = _expand_hints(hints, cfg, rels)
        resolved: list[tuple[str, str]] = []        # (会话, 模型给的名字)
        misses: list[tuple[str, str]] = []          # (名字, 为什么发不了)
        for h in hints:
            target, why = target_of(cfg, h, rels, context=context or None)
            if target:
                if target not in [x[0] for x in resolved]:
                    resolved.append((target, h))
            elif (h, why) not in misses:
                misses.append((h, why))
        suggestions = info.get("candidates") or []
        ambiguous = any(why == "关系网里有两个人叫这个名字" for _, why in misses)
        if not resolved and not suggestions and any(why == "关系网里没这个人" for _, why in misses):
            # 输出的名字没对上时，从原句里的登记称呼提供核对选项。仍需发起人确认。
            message = _alias_key(text)
            suggestions = [label for labels in _clarify_contacts(cfg, chat, rels).values()
                           for label in labels if _alias_key(label) in message]
        if info.get("need_target") or ambiguous or (not resolved and suggestions):
            cands, _confirm = _clarify_choices(cfg, chat, rels, [*suggestions, *hints],
                                               context=context or None)
            options = _clarify_options(cfg, chat, rels, text,
                                       context=context or None)
            # 每轮只问一个人：先问概率最大的那个，被否掉再换下一个
            ordered = list(dict.fromkeys([*cands, *options]))
            first = ordered[0] if ordered else ""
            first_chat = (target_of(cfg, first, rels, context=context or None)[0]
                          if first else "")
            ask = _ask_target([first] if first_chat else [], ordered)
            try:
                with q.tx() as conn:
                    if key and q.has_action(account, chat, key, conn=conn):
                        continue
                    q.add_relay_clarify(account, chat, info.get("content") or "", ordered, ask, key,
                                        kind=info.get("kind") or "remind", confirm_chat=first_chat,
                                        options=ordered, ask_core=ask, conn=conn)
                    if not q.enqueue_relay_clarify_prompt(account, chat, ask, conn=conn):
                        raise RuntimeError("未能排入收件人核对")
                    if key:
                        q.record_action(account, chat, key, None, "relay_pending", conn=conn)
            except Exception as e:
                log.warning("代传话追问落库失败 [%s]：%s" % (chat, e))
                _claim("relay_delivery_refused")
                out.append({"ok": False, "reason": "未能安排收件人核对"})
                continue
            log.info("代传话待确认 [%s]：%s（候选：%s）" % (
                chat, redact(text[:30], cfg), redact("、".join(ordered)[:96], cfg)))
            out.append({"ok": False, "pending": True, "ask": ask,
                        "name": first,
                        "content": info.get("content") or "",
                        "reason": misses[0][1] if misses else ""})
            continue
        if not resolved:
            why = misses[0][1] if misses else "没说清发给谁"
            who = "、".join(n for n, _ in misses) or info.get("target") or "那个人"
            log.info("代传话未建单 [%s]：%s（目标：%s；请求：%s）" % (
                chat, why, redact(" ".join(who.split())[:96], cfg), redact(text[:30], cfg)))
            _claim("relay_refused:" + why)
            out.append({"ok": False, "name": who, "reason": why})
            continue
        content = _fix_addressee((info.get("content") or "").strip(),
                                 len(resolved))
        if not content:
            _claim("relay_empty")
            continue
        # 周期代传话（「每天叫他去上课」）：用模型给的首次时间；没给就退回单次，
        # 免得一开口就变成每天轰炸。
        rule = info.get("rule") or "single"
        first_ts = None
        if rule != "single" and info.get("next_local"):
            try:
                local_ts = int(datetime.strptime(
                    info["next_local"], "%Y-%m-%d %H:%M").timestamp())
                tz = q.get_chat_tz(account, chat) or 0
                zone = q.get_chat_zone(account, chat)
                first_ts = reminder_parse.to_host_ts(local_ts, tz, zone)
                if first_ts <= time.time():
                    first_ts = None
            except Exception:
                first_ts = None
        if rule != "single" and not first_ts:
            rule = "single"
        # 一组回传用同一个 batch：两个人各自回了，凑齐再合成一句告诉发起人。
        batch = ("%s|%s" % (chat, key)) if (key and len(resolved) > 1) else ""
        for idx, (target, hint) in enumerate(resolved):
            # 第一个用原消息 id 占位（别的流程靠它认「这条已经办过」），
            # 后面的各用 ``id#序号``，重试时也不会重复建单。
            skey = key if idx == 0 else ("%s#%d" % (key, idx) if key else None)
            rid = deliver(cfg, q, chat, target, content,
                          from_name=requester, account=account,
                          relations=rels, src_key=skey,
                          verbatim=(info.get("kind") == "say"),
                          rule=rule, first_ts=first_ts,
                          watch_back=(info.get("kind") == "say"),
                          watch_batch=batch)
            if not rid:
                _claim("relay_delivery_refused")
                out.append({"ok": False, "name": hint, "reason": "这条发不出去"})
                continue
            out.append({"ok": True, "rid": rid, "chat": target,
                        "name": display_name(target, rels) or target,
                        "content": content})
        for hint, why in misses:
            out.append({"ok": False, "name": hint, "reason": why})
    return out


# ------------------------------------------------------------------ 回传
# 一次回传最多看几张图：封口条件是 5 分钟 / 10 条，10 条里最多也就这几张；
# 再多就是刷图，没必要为它烧 token，剩下的用文字占位如实说「图没看」。
_MAX_VISION_IMAGES = 8
_VISION_BUDGET = 5 * 1024 * 1024      # base64 总量（约 3.7MB 原图）


def _watch_items(watch: dict) -> list:
    """收集到的条目：文字是字符串，图片/表情是 ``{"k":"image","p":路径,"l":标签}``。

    老记录（升级前只有字符串）照样能读，别为了新格式把在途的回传弄丢。
    """
    import json as _json

    try:
        items = _json.loads(watch.get("reply_text") or "[]")
    except Exception:
        items = []
    if not isinstance(items, list):
        return []
    out = []
    for it in items[:10]:
        if isinstance(it, dict):
            if it.get("k") == "image" and (it.get("p") or it.get("m")
                                           or it.get("local_id") is not None):
                out.append(it)
        elif str(it).strip():
            out.append(str(it))
    return out


def _who(watch: dict) -> str:
    return (watch.get("target_name") or watch.get("target_chat") or "他").strip()


def _vision_uri(item: dict, watch: dict) -> tuple[str | None, int]:
    """图片条目 → data URI。先用收集时存下的路径，没了按 md5 再解一次。"""
    path = (item.get("p") or "").strip()
    if not (path and os.path.isfile(path)):
        kind = "emoji" if (item.get("l") or "").strip() == "表情包" else "image"
        path = media.resolve_any(item.get("m") or "", kind=kind,
                                 chat=watch.get("target_chat") or "",
                                 local_id=item.get("local_id"), shard=item.get("shard"),
                                 account=watch.get("account")) or ""
    if not path:
        return "", 0
    try:
        return media.to_vision_uri(path)
    except Exception as e:
        log.warning("回传读图失败：%s" % e)
        return "", 0


def compose_back(cfg: dict, watches) -> str:
    """把 B（可能好几个人）这段时间发来的**全部**消息组织成一句回给 A 的话。

    以前只把文字丢进模型，B 回一张饭图就成了「他发了张图片，没正面回你」。
    现在图和表情一起塞进**同一次**多模态请求：问了什么、他回了哪些话、几张图
    按顺序摆好，由模型自己看、自己总结，再如实转述。模型不可用时返回空串。
    """
    try:
        from llm import chat as llm_chat
    except Exception:
        return ""
    if isinstance(watches, dict):
        watches = [watches]
    # 每个 watch 只解析一次 reply_text
    plans = [(_w, _watch_items(_w)) for _w in (watches or [])]
    if not any(items for _, items in plans):
        return ""
    watches = [w for w, _ in plans]
    multi = len(watches) > 1
    first = watches[0]
    who = first.get("from_name") or "有人"
    asked = (first.get("question") or "").strip() or "一件事"
    targets = "、".join(_who(w) for w in watches)

    if multi:
        head = ("你是橙子。朋友「%s」之前让你分别去问 %s，问的都是：「%s」。\n"
                % (who, targets, asked))
        subj = "他们各自"
    else:
        head = ("你是橙子。朋友「%s」之前让你去问「%s」：「%s」。\n"
                % (who, targets, asked))
        subj = "他"
    head += ("下面是%s在那之后发来的**全部**消息，按时间顺序排列；图片和表情已经"
             "附在对应位置后面，你自己看。\n" % subj)
    if any(int(w.get("back_ts") or 0) for w in watches):
        head += ("注意：他已经当场让我替他把一句回话带给你了（那句已经转达过），"
                 "这里别再原样重复那句话。\n")

    parts: list[dict] = [{"type": "text", "text": head}]
    shown = used = 0
    n = 0
    for w, items in plans:
        if multi:
            parts.append({"type": "text", "text": "【%s】" % _who(w)})
        if not items:
            parts.append({"type": "text", "text": "（这个人还没回）"})
        for it in items:
            n += 1
            if not isinstance(it, dict):
                parts.append({"type": "text",
                              "text": "[%d] %s" % (n, it.strip()[:300])})
                continue
            label = (it.get("l") or "图片").strip()
            uri, size = (_vision_uri(it, w) if shown < _MAX_VISION_IMAGES
                         and used < _VISION_BUDGET else (None, 0))
            if uri and used + size <= _VISION_BUDGET:
                shown += 1
                used += size
                parts.append({"type": "text",
                              "text": "[%d] （%s，见下图）" % (n, label)})
                parts.append({"type": "image_url",
                              "image_url": {"url": uri}})
            else:
                parts.append({"type": "text",
                              "text": "[%d] （%s，这张图没读到）" % (n, label)})

    tail = (
        "\n现在用 1~3 句如实转述给「%s」：%s是怎么回答的（说话的是%s，"
        "别把「他」写成「你」）。\n"
        "规矩：\n"
        "1. 只说你看到的。文字照实转；图片/表情里看到的东西（吃的什么、什么"
        "表情、什么态度）看到了就说，看不到就别补。\n"
        "2. 看不清、认不出、图糊了 → 直说「没看出来」，绝不许猜细节、不许编动作。\n"
        "3. 他拒绝、不想理、说反话、阴阳怪气 → 照实说，别替他美化，也别替他"
        "找台阶。\n"
        "4. 答的和问题无关 → 说「他没正面回你」。\n"
        "5. 像聊天，别用「根据对话记录」「对方表示」这种报告腔。"
        "只输出要发的那句话，不要解释。\n"
    ) % (who, targets, targets)
    parts.append({"type": "text", "text": tail})

    try:
        content = parts if shown else "\n".join(part["text"] for part in parts)
        r = llm_chat([{"role": "user", "content": content}], cfg, timeout=40)
    except Exception as e:
        log.warning("代传话回传组织失败：%s" % e)
        return ""
    if not r.get("ok") or r.get("truncated"):
        return ""
    return _clip((r.get("text") or "").strip(), 160)


def _clip(text: str, limit: int) -> str:
    """按字数截断，尽量停在句号/问号上，别把话切一半。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for mark in ("。", "！", "？", "…", "；", "\n"):
        pos = cut.rfind(mark)
        if pos >= limit // 2:
            return cut[:pos + 1]
    return cut


def _back_text(watches: list[dict]) -> str:
    """模型组织不出来时，如实回传已有文字和图片的读取状态。"""
    parts = []
    for watch in watches:
        who = _who(watch) if len(watches) > 1 else "他"
        items = _watch_items(watch)
        if int(watch.get("msg_count") or 0) <= 0:
            parts.append("%s还没回。" % who)
        else:
            words = [it for it in items if isinstance(it, str)]
            if words:
                parts.append("%s回：%s。" % (who, "，".join(words)))
            if any(isinstance(it, dict) for it in items):
                parts.append("%s发了图，我没看出来。" % who)
            elif not words:
                parts.append("%s回了消息，我没读到内容。" % who)
    return _clip("".join(parts), 160) or "还没收到回复。"


def back_tick(q, cfg) -> int:
    """把该汇总的代传话回给发起人：回了就转述，没回就如实说。"""
    account = (cfg or {}).get("account")
    rem_cfg = ((cfg or {}).get("proactive") or {}).get("reminders") or {}
    if not account or not rem_cfg.get("enabled", False):
        return 0
    n = 0
    due = [w for w in q.due_relay_watch(account=account) if w.get("account") == account]
    due_ids = {int(w["id"]) for w in due}
    # 同一句委托问了好几个人：等人齐了再一起回，别一人一条刷屏
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for w in due:
        b = (w.get("batch") or "").strip()
        gkey = ("batch:" + b) if b else ("one:%d" % w["id"])
        if gkey not in groups:
            groups[gkey] = []
            order.append(gkey)
        groups[gkey].append(w)

    for gkey in order:
        watches = groups[gkey]
        batch = (watches[0].get("batch") or "").strip()
        if batch:
            open_ids = set(q.relay_watch_open(account, batch))
            if open_ids - due_ids:
                continue                # 还有人没到点，下一轮再来
        answered = any(int(w.get("msg_count") or 0) > 0 for w in watches)
        text = compose_back(cfg, watches) if answered else ""
        if not text:
            text = _back_text(watches)
        try:
            with q.tx() as conn:
                # 模型调用期间收到新消息或有别的线程已封口时，丢掉旧快照。
                ids = [w["id"] for w in watches]
                placeholders = ",".join("?" for _ in ids)
                rows = conn.execute(
                    f"SELECT id, status, msg_count, reply_text FROM relay_watch "
                    f"WHERE account=? AND id IN ({placeholders})", (account, *ids))
                current = {row["id"]: (row["status"], row["msg_count"], row["reply_text"])
                           for row in rows}
                if any(current.get(w["id"]) != (w["status"], w["msg_count"], w["reply_text"])
                       for w in watches):
                    continue
                first = int(time.time()) + 1
                q.add_reminder(account, watches[0]["from_chat"], text, "single", first,
                               anchor_ts=first, confirm_mode="relay_back",
                               relay_from=watches[0].get("from_name"), conn=conn)
                for w in watches:
                    ok = int(w.get("msg_count") or 0) > 0
                    q.set_relay_watch(w["id"], "answered" if ok else "timeout", conn=conn)
        except Exception as e:
            log.warning("代传话回传建单失败：%s" % e)
            continue
        n += 1
        log.info("代传话回传 #%s → %s：%s"
                 % ("、".join(str(w["id"]) for w in watches),
                    watches[0]["from_chat"], redact(text)))
    return n


def refusal_reply(items: list[dict]) -> str:
    """把发送前的拒绝直接告诉发起人，避免模型猜测微信送达结果。"""
    refused = [it for it in items if not it.get("ok") and not it.get("pending")
               and not it.get("unhandled") and not it.get("back")]
    if not refused:
        return ""
    if len(items) == 1 and refused[0].get("receipt"):
        return refused[0]["receipt"]
    reasons = {it.get("reason") or "" for it in refused}
    if reasons == {"关系里没绑定会话"}:
        detail = "关系表里的联系人还没绑定微信会话。"
    elif reasons <= {"", "关系网里没这个人", "没说清发给谁",
                     "关系网里有两个人叫这个名字"}:
        detail = "我没能确认关系表里对应的人。可以用登记的姓名或昵称再发一次委托。"
    else:
        detail = "这次委托没能安排发送，请核对关系表后再试。"
    sent = list(dict.fromkeys(str(it["name"]) for it in items if it.get("ok") and it.get("name")))
    prefix = "这次没有发送。"
    if sent:
        prefix = "已安排给%s转达，其余没有发送。" % "、".join(sent)
    return prefix + detail


def hint_line(items: list[dict]) -> str:
    """给聊天回复的一句话：安排好了就说安排好了，不确定就先问一句。"""
    if not items:
        return ""
    sent = [it for it in items if it.get("ok") and not it.get("back")]
    backs = [it for it in items if it.get("ok") and it.get("back")]
    asking = [it for it in items if it.get("pending")]
    refused = [it for it in items if not it.get("ok") and not it.get("pending")
               and not it.get("unhandled") and not it.get("back")]
    back_failed = [it for it in items if it.get("back") and not it.get("ok")]
    parts = []
    if backs:
        what = "；".join("%s：%s" % (it.get("name") or it.get("chat"), it.get("content"))
                        for it in backs)
        parts.append("【你已经代他转告】%s。这是你替他带回去的一句回话，"
                     "说明已经安排发送；没收到送达结果前别说已经发出去了。" % what)
    if back_failed:
        why = "；".join(str(it.get("receipt") or it.get("reason") or "没能带回去")
                       for it in back_failed)
        parts.append("【这句没带回去】%s。只说明这次没有替他转达，别编造成因。" % why)
    if asking:
        asks = "；".join(str(it.get("ask") or "") for it in asking if it.get("ask"))
        if asks:
            parts.append("【先问一句】他让你提醒的人你还不确定，这一轮别答应已经发了，"
                         "也别硬猜一个名字发出去。先自然地问清楚：%s"
                         "（可以带一点被使唤的不情愿）" % asks)
    unhandled = [it for it in items if it.get("unhandled")]
    if unhandled:
        parts.append("【别空口答应】他这句话像是在托你替他说点什么，但你没弄明白要说什么。"
                     "这一轮别答应（别说「我帮你说」「行吧我问问」），"
                     "要么让他说得具体点，要么如实说办不了。")
    if sent:
        what = "；".join("%s：%s" % (it.get("name") or it.get("chat"),
                                    it.get("content")) for it in sent)
        parts.append("【你已经安排发送】对方让你联系的人：%s。"
                     "说明你已经安排，收到送达结果前别说已经发出；"
                     "也别让他自己去问——这件事你已经接了。" % what)
    if refused:
        who = "、".join(sorted({str(it.get("name") or "那个人") for it in refused}))
        parts.append("【尚未发送】对「%s」的委托没有排入发送。原因：%s。"
                     "只说明此次没有发送，不许说已经发了，"
                     "也不能说对方收不到或送达失败。"
                     % (who, "；".join(sorted({str(it.get("reason") or "")
                                               for it in refused if it.get("reason")})) or "未能建单"))
    return "\n\n".join(parts)