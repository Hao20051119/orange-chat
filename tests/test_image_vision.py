# -*- coding: utf-8 -*-
"""聊天里的图片：真给模型看，不再只压成一句情绪描述。

规则（对应 2026-11-22 的调整）：
* 图片 —— 主模型这一轮真看图；同时让模型写一句**内容**描述写进记忆
* 表情包 —— 仍然只要一句情绪描述，不塞图（省钱，也不容易猜错）
* 群聊背景里别人发的图 —— 也一起给模型看，标成「群图N」
* 代传话收集 B 的回复走 relay 自己那套，不在这个文件里
"""
import json
import os
import time
from unittest.mock import patch

import support
from support import check, section

os.environ["WXBOT_MEDIA_CACHE"] = os.path.join(support.TMP, "media_cache")

import brain as brain_mod          # noqa: E402
import media                       # noqa: E402
import msgtype                     # noqa: E402
import reply_worker as worker      # noqa: E402

ACCOUNT = "wxid_test"
PRIVATE = "wxid_friend"
GROUP = "12345@chatroom"
TMP = support.TMP
NOW = int(time.time())

MD5_IMG = "0123456789abcdef0123456789abcdef"
MD5_EMO = "fedcba9876543210fedcba9876543210"


def make_png(name="pic.png", size=(320, 240)):
    from PIL import Image
    p = os.path.join(TMP, name)
    Image.new("RGB", size, (180, 60, 40)).save(p, "PNG")
    return p


PNG = make_png()


class StubBrain:
    def __init__(self, mem=None):
        self.mem = mem
        self.desc_calls = []

    def describe_media(self, msg_type, content, type_code=None, style="emotion",
                       image_uri=None):
        self.desc_calls.append(style)
        if style == "content":
            return "课程表截图：周四 14:00-15:25 化学前沿专题"
        return "满课表带来的疲惫感"


class StubDir:
    NAMES = {"wxid_a": "小王", "wxid_b": "小李"}

    def display(self, chat, sender):
        return self.NAMES.get(sender, sender or "")


def make_cfg(**over):
    cfg = support.base_cfg()
    cfg["agent"]["group_context"] = {"window": 10, "minutes": 30, "chars": 700}
    cfg["media"] = {"vision": {"show_images": True, "max_images_per_turn": 4,
                               "max_bytes": 6 * 1024 * 1024,
                               "content_desc": True, "content_desc_chars": 150}}
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k] = {**cfg[k], **v}
        else:
            cfg[k] = v
    return cfg


def make_worker(q, cfg=None, mem=None):
    w = worker.Worker.__new__(worker.Worker)
    w.cfg = cfg or make_cfg()
    w.acc = ACCOUNT
    w.q = q
    w.brain = StubBrain(mem)
    w.directory = StubDir()
    return w


def img_msg(mid=1, md5=MD5_IMG, sender="", ts=NOW):
    return {"id": mid, "chat": GROUP if sender else PRIVATE, "sender": sender,
            "msg_type": "图片", "type_code": 3, "msg_ts": ts,
            "content": '<msg><img md5="%s"/></msg>' % md5}


def emo_msg(mid=2, sender="", ts=NOW):
    return {"id": mid, "chat": PRIVATE, "sender": sender,
            "msg_type": "动画表情", "type_code": 47, "msg_ts": ts,
            "content": '<msg><emoji md5="%s"/></msg>' % MD5_EMO}


def txt_msg(mid=3, content="在吗", sender="", ts=NOW):
    return {"id": mid, "chat": PRIVATE, "sender": sender, "msg_type": "文本",
            "type_code": 1, "msg_ts": ts, "content": content}


# ---------------------------------------------------------------- 图片进上下文
section("图片：真图跟着这一轮一起给模型")
q = support.make_queue("vision1.db")
w = make_worker(q)
with patch.object(media, "resolve_for_message", return_value=PNG):
    text, pairs, images = w._absorb([txt_msg(1, "看看这个"), img_msg(2)])
check("返回三样（文本/条目/图片）", len(pairs) == 2 and len(images) == 1, str(len(images)))
check("图片是真 data URI", images[0]["uri"].startswith("data:image/jpeg;base64,"),
      images[0]["uri"][:40])
check("标签是「图1」", images[0]["cap"].startswith("图1"), images[0]["cap"])
check("文本里点名了图1，模型知道哪张对哪句",
      "图1" in text and "图片" in text, text)
check("图片走的是内容描述（不是情绪）", w.brain.desc_calls == ["content"],
      str(w.brain.desc_calls))
check("内容描述写进了这一轮的文本（也就是记忆）",
      "化学前沿" in text, text)

section("表情包：不塞图，仍只要一句情绪描述")
w2 = make_worker(support.make_queue("vision2.db"))
with patch.object(media, "resolve_for_message", return_value=PNG):
    text2, _, images2 = w2._absorb([emo_msg(5)])
check("表情包不进图片列表", images2 == [], str(images2))
check("表情包走情绪描述", w2.brain.desc_calls == ["emotion"], str(w2.brain.desc_calls))
check("文本里带表情描述", "疲惫感" in text2, text2)

section("一张图读不出来：退回文字，不崩")
w3 = make_worker(support.make_queue("vision3.db"))
with patch.object(media, "resolve_for_message", side_effect=RuntimeError("没钥匙")):
    text3, _, images3 = w3._absorb([img_msg(7)])
check("没有图片 part", images3 == [], str(images3))
check("保留图片占位，不猜测看不到的内容", "图片" in text3 and not w3.brain.desc_calls, text3)

section("一轮图片上限：超出的只留文字占位")
cfg_cap = make_cfg()
cfg_cap["media"]["vision"]["max_images_per_turn"] = 2
w4 = make_worker(support.make_queue("vision4.db"), cfg_cap)
msgs4 = [img_msg(i, md5="%032x" % i) for i in range(1, 5)]
with patch.object(media, "resolve_for_message", return_value=PNG):
    text4, pairs4, images4 = w4._absorb(msgs4)
check("只塞 2 张", len(images4) == 2, str(len(images4)))
check("4 条消息都还在文本里", len(pairs4) == 4, str(len(pairs4)))
check("超限的图仍写内容描述（不是情绪描述）",
      w4.brain.desc_calls.count("content") >= 2 and "emotion" not in w4.brain.desc_calls,
      str(w4.brain.desc_calls))
check("两张图编号不重复", [im["cap"][:2] for im in images4] == ["图1", "图2"],
      str([im["cap"] for im in images4]))

section("图片总量超预算：不塞图也不报错")
w5 = make_worker(support.make_queue("vision5.db"))
big = "data:image/jpeg;base64," + "A" * (7 * 1024 * 1024)
with patch.object(media, "resolve_for_message", return_value=PNG), \
        patch.object(media, "to_vision_uri", return_value=(big, 7 * 1024 * 1024)):
    text5, _, images5 = w5._absorb([img_msg(9)])
check("超预算就不塞", images5 == [], str(images5))
check("文字占位还在", "图片" in text5, text5)

section("预算刚好用光：后面的图不再塞（0 不等于不限）")
cfg_exact = make_cfg()
cfg_exact["media"]["vision"]["max_bytes"] = 1000
w5b = make_worker(support.make_queue("vision5b.db"), cfg_exact)
uri_one = "data:image/jpeg;base64," + "A" * 900
with patch.object(media, "resolve_for_message", return_value=PNG), \
        patch.object(media, "to_vision_uri", return_value=(uri_one, 999)):
    text5b, _, images5b = w5b._absorb([img_msg(21, md5="%032x" % 1),
                                      img_msg(22, md5="%032x" % 2)])
check("只进第一张", len(images5b) == 1, str(len(images5b)))
check("第二条消息也在文本里", text5b.count("图片") >= 2, text5b)

# ---------------------------------------------------------------- 群背景
section("群聊背景：别人发的图也给模型看")
q6 = support.make_queue("vision6.db")
w6 = make_worker(q6)
q6.enqueue_inbound(ACCOUNT, GROUP, "g1", NOW - 60, "文本", "晚上吃啥", sender="wxid_a",
                   sort_seq=1)
q6.enqueue_inbound(ACCOUNT, GROUP, "g2", NOW - 50, "图片",
                   '<msg><img md5="%s"/></msg>' % MD5_IMG, sender="wxid_a", sort_seq=2)
with patch.object(media, "resolve_for_message", return_value=PNG):
    ctx, imgs = w6._group_context(GROUP, exclude_ids=())
check("背景里有那句话", "晚上吃啥" in ctx and "小王" in ctx, ctx)
check("图变成群图1", "群图1" in ctx, ctx)
check("群图也进了图片列表", len(imgs) == 1 and imgs[0]["cap"].startswith("群图1"),
      str(imgs))
check("群图不额外花一次识图", w6.brain.desc_calls == [], str(w6.brain.desc_calls))

section("群背景：本轮正在处理的消息不重复出现")
with patch.object(media, "resolve_for_message", return_value=PNG):
    ctx2, imgs2 = w6._group_context(GROUP, exclude_ids=(1, 2))
check("排除掉就不出现", ctx2 == "" and imgs2 == [], str((ctx2, imgs2)))

section("群背景：窗口长度和字数都有上限")
q7b = support.make_queue("vision7b.db")
cfg_gc = make_cfg()
cfg_gc["agent"]["group_context"] = {"window": 10, "minutes": 30, "chars": 30}
w7b = make_worker(q7b, cfg_gc)
for i in range(6):
    q7b.enqueue_inbound(ACCOUNT, GROUP, "gc%d" % i, NOW - 100 + i, "文本",
                        "第%d句，一句挺长的话用来撑字数" % i, sender="wxid_a",
                        sort_seq=i)
ctx7b, _ = w7b._group_context(GROUP)
check("背景被字数上限截住", len(ctx7b) <= 80, str(len(ctx7b)))
check("至少带上最后那句", "第5句" in ctx7b, ctx7b[-40:])

section("群背景：私聊不生效、开关能关")
check("私聊返回空", w6._group_context(PRIVATE) == ("", []), "")
cfg_off = make_cfg()
cfg_off["agent"]["group_context"] = {"window": 0}
w7 = make_worker(support.make_queue("vision7.db"), cfg_off)
check("window=0 就关掉", w7._group_context(GROUP) == ("", []), "")

# ---------------------------------------------------------------- 组装与记忆
section("build_messages：图片挂在最后一条 user 上")
mem = support.make_memory("vision_mem.db")
cfg_b = make_cfg()
b = brain_mod.Brain(cfg_b)
b.mem = mem
caps = [{"uri": "data:image/jpeg;base64,AAAA", "cap": "图1：课表"},
        {"uri": "data:image/jpeg;base64,BBBB", "cap": "群图1（小王 刚发的）"}]
msgs = b.build_messages(PRIVATE, "帮我看看课表", images=caps)
last = msgs[-1]
check("最后一条 user 是多模态", isinstance(last["content"], list), str(type(last["content"])))
kinds = [x.get("type") for x in last["content"]]
check("两张图都在", kinds.count("image_url") == 2, str(kinds))
check("图前有标签", "【图1：课表】" in last["content"][1]["text"],
      str(last["content"][1]))
plain = b.build_messages(PRIVATE, "帮我看看课表")
check("没图时仍是纯字符串", isinstance(plain[-1]["content"], str), "")
empty = b.build_messages(PRIVATE, "帮我看看课表",
                         images=[{"uri": "", "cap": "图1"}, {"uri": "", "cap": "图2"}])
check("图全没读出来也退回纯字符串", isinstance(empty[-1]["content"], str),
      str(type(empty[-1]["content"])))

section("老配置只写了部分 vision 字段：默认值兜住")
cfg_partial = make_cfg()
cfg_partial["media"] = {"vision": {"max_images_per_turn": 2}}
w9 = make_worker(support.make_queue("vision9.db"), cfg_partial)
vc = w9._vision_cfg()
check("缺的键用默认值补上",
      vc["show_images"] is True and vc["max_bytes"] == 6 * 1024 * 1024
      and vc["max_bytes"] > 0 and vc["max_images_per_turn"] == 2, str(vc))
cfg_broken = make_cfg()
cfg_broken["media"] = {"vision": [1, 2, 3]}
w10 = make_worker(support.make_queue("vision10.db"), cfg_broken)
check("段写坏了也不崩，退回默认值", w10._vision_cfg()["max_images_per_turn"] == 4,
      str(w10._vision_cfg()))

section("配置校验：新参数写错能报出来")
import config as config_mod                       # noqa: E402

bad = support.base_cfg()
bad["media"] = {"vision": {"max_images_per_turn": "four"}, "ffmpeg": 123}
bad["agent"]["group_context"] = [1, 2]
probs = config_mod._validate(bad)
check("抓到 vision 类型错",
      any("max_images_per_turn" in p for p in probs), str(probs))
check("抓到 group_context 类型错",
      any("group_context" in p for p in probs), str(probs))
check("抓到 ffmpeg 类型错", any("ffmpeg" in p for p in probs), str(probs))
check("默认配置本身没毛病", config_mod._validate(config_mod._defaults()) == [],
      str(config_mod._validate(config_mod._defaults())))

section("提醒扫描用的图片清单：只解路径，不调模型")
w11 = make_worker(support.make_queue("vision11.db"))
with patch.object(media, "resolve_for_message", return_value=PNG):
    refs = w11._batch_image_refs([txt_msg(31, "看看"), img_msg(32), emo_msg(33)])
check("只收图片，不把表情当图片", len(refs) == 1, str(refs))
check("带路径和 md5", refs and refs[0]["p"] == PNG and refs[0]["m"] == MD5_IMG,
      str(refs))
with patch.object(media, "resolve_for_message", return_value=PNG):
    refs_cap = w11._batch_image_refs([img_msg(40 + i, md5="%032x" % i) for i in range(5)],
                                     limit=3)
check("张数有上限", len(refs_cap) == 3, str(len(refs_cap)))
check("没花一次识图", w11.brain.desc_calls == [], str(w11.brain.desc_calls))

section("私聊整合：带图提醒单独发出核对，即使自动回复关闭")
import reminder as reminder_mod                      # noqa: E402

PCHAT = "wxid_pm"


class PvtBrain(StubBrain):
    def __init__(self):
        super().__init__(support.make_memory("vision_pm_mem.db"))
        self.composes = []

    def reload(self, cfg):
        self.cfg = cfg

    def should_reply(self, *a, **k):
        return True, ""

    def compose(self, chat, text, **kw):
        self.composes.append({"text": text, **kw})
        return {"ok": True, "reply": "行，记着了"}

    def sensitive_label(self, text):
        return None

    def contact_cfg(self, chat):
        return {}

    def maybe_summarize(self, chat):
        pass

    def maybe_adapt(self, chat):
        pass

    def analyze_group(self, *a, **k):
        return ""


def pvt_worker(name="vision_pm.db"):
    cfg = make_cfg()
    cfg["proactive"] = {"enabled": True,
                        "reminders": {"enabled": True, "llm_text": True,
                                      "confirm_with_images": True,
                                      "clarify_max_asks": 3},
                        "relay": {"enabled": False}}
    q = support.make_queue(name)
    w = worker.Worker.__new__(worker.Worker)
    w.cfg = cfg
    w.acc = ACCOUNT
    w.q = q
    w.brain = PvtBrain()
    w.directory = StubDir()
    w.me = "wxid_self"
    w.nick = "🍊"
    w._watermark = {}
    w._HOOK_LAST = {}
    w._inflight = set()
    w._inflight_lock = __import__("threading").Lock()
    return w


VERDICT = {"is_reminder": True, "confidence": 0.95,
           "content": "化学前沿课前准备", "rule": "weekly:3",
           "next_ts": int(time.time()) + 3 * 86400, "need_time": False,
           "ask": "是不是要在每周三 13:50 提醒你化学前沿课前准备？",
           "tz": "", "next_local": ""}

wp = pvt_worker()
wp.cfg["agent"]["auto_reply"] = False
wp.db = type("EmptyDB", (), {"get_messages": lambda self, *args, **kwargs: []})()
wp.q.enqueue_inbound(ACCOUNT, PCHAT, "pm1", NOW, "文本",
                     "以后每次化学前沿课前十分钟都记得提醒我一下", sender=PCHAT,
                     sort_seq=1)
wp.q.enqueue_inbound(ACCOUNT, PCHAT, "pm2", NOW + 1, "图片",
                     '<msg><img md5="%s"/></msg>' % MD5_IMG, sender=PCHAT,
                     sort_seq=2)
with patch.object(media, "resolve_for_message", return_value=PNG), \
        patch.object(worker, "load_cfg", return_value=wp.cfg), \
        patch.object(reminder_mod, "judge_with_llm", return_value=dict(VERDICT)) as judge:
    wp.process(PCHAT)
check("只发专用核对，不另出一条普通回复", not wp.brain.composes)
check("图交给提醒判定", bool(judge.call_args.kwargs.get("images")))
with patch.object(worker, "post", return_value={"ok": True, "verified": True,
                                                "verified_seq": 3}) as send:
    result = wp.send_task(wp.q.next_outbound(account=ACCOUNT), sleep_delay=False, reload_cfg=False)
check("自动回复关闭仍调用发送接口发送核对",
      result["status"] == "sent" and send.call_args.args[1]["source"] == "reminder_clarify")
rec_pm = (wp.q.get_clarify(PCHAT, "asked", account=ACCOUNT)
          or wp.q.get_clarify(PCHAT, "pending", account=ACCOUNT))
check("留了带图的待确认",
      bool(rec_pm) and bool(rec_pm.get("images")), str(rec_pm and rec_pm["images"]))
check("待确认里有提案时间",
      bool(rec_pm) and int(rec_pm.get("proposed_ts") or 0) == VERDICT["next_ts"],
      str(rec_pm and rec_pm.get("proposed_ts")))

section("记忆：图片的内容描述会落进对话记忆")
q8 = support.make_queue("vision8.db")
mem8 = support.make_memory("vision_mem8.db")
w8 = make_worker(q8, make_cfg(), mem=mem8)
with patch.object(media, "resolve_for_message", return_value=PNG):
    text8, pairs8, images8 = w8._absorb([img_msg(11)])
w8._record_user(pairs8)
rows = mem8.recent(PRIVATE, limit=5) if hasattr(mem8, "recent") else []
blob = json.dumps(rows, ensure_ascii=False)
check("记忆里存了图片的内容描述", "化学前沿" in blob, blob[:200])

section("compose：真的把图发给了模型")
captured = {}


class CapLlm:
    def __init__(self):
        self.calls = []

    def __call__(self, msgs, cfg=None, **kw):
        self.calls.append(msgs)
        # 只对「生成回复」那次返回内容；读懂那一层给空
        if any(isinstance(m.get("content"), list) for m in msgs):
            return {"ok": True, "text": "看着像周四的课"}
        return {"ok": True, "text": ""}


cap = CapLlm()
with patch.object(brain_mod, "llm_chat", cap):
    comp = b.compose(PRIVATE, "帮我看看课表", images=caps)
multimodal = [c for c in cap.calls
              if any(isinstance(m.get("content"), list) for m in c)]
check("compose 成功", comp.get("ok") is True, str(comp))
check("确实有一次请求带了图", len(multimodal) == 1, str(len(multimodal)))
check("带图的那次有 2 张", sum(1 for m in multimodal[0][-1]["content"]
                              if m.get("type") == "image_url") == 2, "")

section("文本接口拒绝图片时，本轮以文字继续")
def reject_image(msgs, cfg=None, **kw):
    if isinstance(msgs[-1]["content"], list):
        return {"ok": False, "status": 400, "error": "unsupported image_url"}
    return {"ok": True, "text": "我可以先回答文字里的问题"}


with patch.object(b, "comprehend", return_value=""), \
        patch.object(brain_mod, "llm_chat", side_effect=reject_image) as text_api:
    comp = b.compose(PRIVATE, "帮我看看课表", images=caps)
    check("带图拒绝后只重试一次，改为文本", comp.get("ok") and text_api.call_count == 2)

# ---------------------------------------------------------------- 群聊走一遍
section("群聊整合：@我时，群里刚发的图一起进本轮")


class GroupBrain(StubBrain):
    def __init__(self):
        super().__init__(support.make_memory("vision_group_mem.db"))
        self.composes = []

    def should_reply(self, *a, **k):
        return True, ""

    def reload(self, cfg):
        self.cfg = cfg

    def compose(self, chat, text, **kw):
        self.composes.append({"text": text, **kw})
        return {"ok": True, "reply": "行"}

    def sensitive_label(self, text):
        return None

    def contact_cfg(self, chat):
        return {}

    def maybe_summarize(self, chat):
        pass

    def maybe_adapt(self, chat):
        pass

    def analyze_group(self, *a, **k):
        return ""


def group_worker(name="vision_group.db"):
    cfg = make_cfg()
    cfg["agent"].update({"group_reply": True, "group_mode": "at_only",
                         "at_names": ["🍊"]})
    q = support.make_queue(name)
    w = worker.Worker.__new__(worker.Worker)
    w.cfg = cfg
    w.acc = ACCOUNT
    w.q = q
    w.brain = GroupBrain()
    w.directory = StubDir()
    w.me = "wxid_self"
    w.nick = "🍊"
    w._watermark = {}
    w._HOOK_LAST = {}
    w._inflight = set()
    w._inflight_lock = __import__("threading").Lock()
    return w


GW = "999@chatroom"
wg = group_worker()
now = int(time.time())
# 群里刚才：小王发了一张图（没 @ 我）
wg.q.enqueue_inbound(ACCOUNT, GW, "gw1", now - 90, "图片",
                     '<msg><img md5="%s"/></msg>' % MD5_IMG,
                     sender="wxid_a", sort_seq=1)
wg.q.mark_inbounds([r["id"] for r in wg.q.recent_inbound(GW, limit=5, account=ACCOUNT)],
                   "skipped")
# 现在：小李 @我 说了一句
wg.q.enqueue_inbound(ACCOUNT, GW, "gw2", now, "文本", "@🍊 上面那张图是啥",
                     sender="wxid_b", sort_seq=2)
with patch.object(media, "resolve_for_message", return_value=PNG), \
        patch.object(worker, "load_cfg", return_value=wg.cfg):
    wg.process(GW)
check("这一轮真的进模型了", len(wg.brain.composes) == 1, str(len(wg.brain.composes)))
kw = wg.brain.composes[0] if wg.brain.composes else {}
imgs_in = kw.get("images") or []
check("群背景里的图进了本轮", len(imgs_in) == 1, str(len(imgs_in)))
check("标签是群图1",
      bool(imgs_in) and imgs_in[0]["cap"].startswith("群图1"), str(imgs_in))
check("群背景文字也带上了",
      "群里刚才说了什么" in (kw.get("extra") or ""), str(kw.get("extra"))[:80])

section("同一轮正文、补发和群背景共用图片额度")
wg.cfg["media"]["vision"].update(content_desc=False, max_images_per_turn=3)
with patch.object(media, "resolve_for_message", return_value=PNG):
    _, _, initial = wg._absorb([img_msg(10), img_msg(11)])
    _, _, additions = wg._absorb([img_msg(12)], prior_images=initial)
    _, background = wg._group_context(GW, prior_images=initial + additions)
check("补发顺序延续图编号", [im["cap"] for im in initial + additions] == ["图1", "图2", "图3"])
check("群背景不突破剩余张数", background == [])
check("关闭内容描述仍可看图且不再请求描述", wg.brain.desc_calls == [])

wg.cfg["media"]["vision"]["max_images_per_turn"] = 4
wg.cfg["media"]["vision"]["max_bytes"] = wg._vision_used(initial)
with patch.object(media, "resolve_for_message", return_value=PNG):
    _, _, additions = wg._absorb([img_msg(14)], prior_images=initial)
    _, background = wg._group_context(GW, prior_images=initial)
check("群背景和补发同样遵守剩余字节数", additions == [] and background == [])

raise SystemExit(support.finish())
