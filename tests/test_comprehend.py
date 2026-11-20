# -*- coding: utf-8 -*-
"""口语理解：微信里的同音错字、漏字，要先读懂再回。

线上事故（真实日志）：群里对方发「已经给我调好了，没那么爱对人了」，
「对」是「怼」；Orange 按字面理解，回了「调好了还追着人聊，这算哪门子不爱对人」——
既没接住原意，还把一次输入失误变成了抬杠。

这里覆盖三件事：
* 主提示词在「本轮有对方消息」时带【口语理解】段（主动开口不带）
* 群聊关系判断复用同一段理解规则，并允许在【注意】里标出错字
* 生成期间对方补发消息 → 旧的判断作废，按合并后的整轮重做
"""
import sys
import threading
import time

import support
from support import check, section

import brain as brain_mod
import persona
import reply_worker as RW

CFG = support.base_cfg(agent={"group_reply": True, "group_mode": "at_only",
                              "group_analysis": True, "group_at_reply": True},
                       merge={"quiet_seconds": 3.5, "quiet_complete_seconds": 2.0,
                              "quiet_incomplete_seconds": 5.0,
                              "quiet_media_seconds": 1.5,
                              "absorb_after_seconds": 30,
                              "draft_merge_seconds": 300,
                              "max_wait_seconds": 12.0})
ACC = "wxid_test"
GROUP = "10000000001@chatroom"
SENDER = "wxid_阿橘"

section("主提示词带口语理解规则")
with_block = persona.build_system_prompt("【当前情况】测试", CFG, inbound=True)
without = persona.build_system_prompt("【当前情况】测试", CFG, inbound=False)
check("有【口语理解】段", "【口语理解】" in with_block)
check("写明同音误选要按最顺的意思接", "同音误选" in with_block)
check("举例「对」其实是「怼」", "「对」其实是「怼」" in with_block)
check("写明别点评错字", "不要点评他的错字" in with_block)
check("写明先读对再抬杠", "先把话读对" in with_block)
check("谐音梗按原意理解", "蚌埠住了" in with_block)
check("拿不准先接住确定的部分", "先接住你确定的那部分" in with_block)
check("更正优先", "以更正为准" in with_block)
check("理解段排在回复决策之前",
      with_block.find("【口语理解】") < with_block.find("【回复决策】"))
check("主动开口那段不出现", "【口语理解】" not in without)
check("老调用方默认仍带（不会悄悄丢规则）",
      "【口语理解】" in persona.build_system_prompt("", CFG))

section("只有本轮有对方消息时才注入")
b = brain_mod.Brain(CFG)
b.mem = support.make_memory("comprehend.db")
check("回复时带",
      "【口语理解】" in b.build_messages("wxid_x", "在吗")[0]["content"])
check("没有入站文本不带",
      "【口语理解】" not in b.build_messages("wxid_x", "")[0]["content"])

calls = []
_orig_llm = brain_mod.llm_chat


def _fake_llm(msgs, config=None, timeout=60, max_tokens=None):
    calls.append(msgs)
    return {"ok": True, "text": "困死了，先躺一会儿"}


brain_mod.llm_chat = _fake_llm
try:
    r = b.compose_proactive("wxid_x", seed="刚下课", kind="hook")
finally:
    brain_mod.llm_chat = _orig_llm
check("主动开口能生成", r.get("ok") and r.get("reply"), str(r))
check("主动开口不发理解规则",
      calls and "【口语理解】" not in calls[0][0]["content"])

section("群聊关系判断复用同一段理解规则")
gp = brain_mod.GROUP_ANALYZE_PROMPT
check("请求里带同一段", persona.COMPREHEND_BRIEF in gp, gp[:120])
for k in ("【谁对谁说】", "【在回应】", "【要不要接】", "【注意】"):
    check("仍然输出四行里的 %s" % k, k in gp)
check("注意里可以标错字", "多半是「怼」" in gp)
filled = gp % ("🥭：@🍊 晚上好", "🥭", "已经给我调好了，没那么爱对人了")
check("只剩三个占位符（格式化不会炸）", "已经给我调好了" in filled)
check("新消息原样进提示词", "没那么爱对人了" in filled)

seen = {}
bb = brain_mod.Brain(support.base_cfg(agent={"group_reply": True,
                                            "group_analysis": True}))
bb.mem = support.make_memory("comprehend_group.db")
for i, (who, txt) in enumerate([("张伟", "今晚谁去打球"), ("李娜", "我不去"),
                                ("张伟", "@🍊 你说呢")]):
    bb.mem.add(GROUP, "user", txt, src_key="in:%d" % i, sender=who)


def _fake_analyze(msgs, config=None, timeout=60, max_tokens=None):
    seen["text"] = msgs[-1]["content"]
    return {"ok": True, "text": "【谁对谁说】张伟 对 🍊\n【在回应】问打球\n"
                                "【要不要接】要接\n【注意】「对」多半是「怼」"}


brain_mod.llm_chat = _fake_analyze
try:
    note = bb.analyze_group(GROUP, "张伟", "没那么爱对人了")
finally:
    brain_mod.llm_chat = _orig_llm
check("请求里真的带上了", persona.COMPREHEND_BRIEF in seen.get("text", ""),
      seen.get("text", "")[:160])
check("四行原样解析回来", note.count("【") == 4, note)


section("先读懂：读法会进提示词")
b2 = brain_mod.Brain(CFG)
b2.mem = support.make_memory("comprehend_prepass.db")
b2.mem.add("wxid_x", "user", "你最近怎么这么凶", src_key="in:old")
seen2 = []


def fake_two(msgs, config=None, timeout=60, max_tokens=None):
    seen2.append((msgs[0]["content"], msgs[-1]["content"]))
    if "听懂微信口语" in msgs[0]["content"]:
        return {"ok": True,
                "text": "【听懂】你被调好了，没那么爱怼人了\n【另一读】单一\n"
                        "【语气】在逗，带点邀功"}
    return {"ok": True, "text": "哦…那我先看看效果"}


brain_mod.llm_chat = fake_two
try:
    r = b2.compose("wxid_x", "已经给我调好了，没那么爱对人了")
finally:
    brain_mod.llm_chat = _orig_llm
check("先调了一次读懂，再调回复（两次）", len(seen2) == 2, str(len(seen2)))
check("读懂请求带上了最近的话", "你最近怎么这么凶" in seen2[0][1], seen2[0][1][:120])
check("读懂请求带上本轮这句", "没那么爱对人了" in seen2[0][1])
check("回复提示词里带读法",
      "【你刚把这一句读了一遍" in seen2[-1][0] and "没那么爱怼人了" in seen2[-1][0])
check("明确要求别复述别提错字",
      "别复述原话" in seen2[-1][0] and "别提错字" in seen2[-1][0])
check("说明这不是对方说过的话", "不是对方以前说过的话" in seen2[-1][0])
check("回复照常生成", r.get("ok") and r.get("reply") == "哦…那我先看看效果", str(r))

seen3 = []
brain_mod.llm_chat = lambda msgs, c=None, **kw: (seen3.append(msgs[0]["content"]),
                                                {"ok": True, "text": "嗯"})[1]
try:
    b2.compose("wxid_x", "嗯")
    n_short = len(seen3)
    b_off = brain_mod.Brain(support.base_cfg(agent={"comprehend_prepass": False}))
    b_off.mem = b2.mem
    b_off.compose("wxid_x", "已经给我调好了，没那么爱对人了")
    n_off = len(seen3)
finally:
    brain_mod.llm_chat = _orig_llm
check("太短不值得读（只一次调用）", n_short == 1, str(n_short))
check("关掉开关就不读（只一次调用）", n_off == n_short + 1, str(n_off))
check("关掉后提示词里没有读法", "【你刚把这一句读了一遍" not in seen3[-1])

seen4 = []


def fake_fail(msgs, config=None, timeout=60, max_tokens=None):
    seen4.append(msgs[0]["content"])
    if "听懂微信口语" in msgs[0]["content"]:
        return {"ok": False, "error": "HTTP 500"}
    return {"ok": True, "text": "行吧"}


brain_mod.llm_chat = fake_fail
try:
    r = b2.compose("wxid_x", "已经给我调好了，没那么爱对人了")
finally:
    brain_mod.llm_chat = _orig_llm
check("读懂失败照样回", r.get("ok") and r.get("reply") == "行吧", str(r))
check("读懂失败时不注入空读法", "【你刚把这一句读了一遍" not in seen4[-1])


# ------------------------------------------------------- 生成期间补发更正
class FakeDB:
    def get_group_members(self, chatroom):
        return [{"username": SENDER, "remark": "🥭", "nick_name": "阿橘"}]

    def group_id_to_name(self, chatroom):
        return "🥭王朝"


class FakeDir:
    def display(self, chat, wxid):
        return "🥭" if wxid == SENDER else ""


class GroupBrain:
    """第一次 compose 时模拟「对方补发更正」，看旧判断会不会被继续用。"""

    def __init__(self, q):
        self.q = q
        self.an = []
        self.comp = []
        self.mem = type("M", (), {"persistent": False})()

    def reload(self, cfg):
        pass

    def should_reply(self, chat, text, from_me=False, at_me=False):
        return True, "ok"

    def analyze_group(self, chat, sender_display, text):
        self.an.append(text)
        n = len(self.an)
        return "【谁对谁说】%d\n【在回应】第%d次\n【要不要接】要接\n【注意】第%d次" % (n, n, n)

    def compose(self, chat, text, extra="", **kw):
        self.comp.append((text, extra))
        if len(self.comp) == 1:
            self.q.enqueue_inbound(ACC, GROUP, "l2", int(time.time()), "文本",
                                   "@🍊 怼*", sender=SENDER)
        return {"ok": True, "reply": "那我先看看效果"}

    def sensitive_label(self, reply):
        return None

    def contact_cfg(self, chat):
        return {}

    def human_delay(self, chat=None):
        return 0

    def maybe_summarize(self, chat):
        return {"ok": False}

    def maybe_adapt(self, chat):
        return {"ok": False}


def make_worker(q, brain):
    w = RW.Worker.__new__(RW.Worker)
    w.cfg = dict(CFG)
    w.q = q
    w.brain = brain
    w.me = "me"
    w.nick = "🍊"
    w.acc = ACC
    w.db = FakeDB()
    w.directory = FakeDir()
    w._watermark = {}
    w._HOOK_LAST = {GROUP: time.time()}
    w._inflight = set()
    w._inflight_lock = threading.Lock()
    return w


section("生成期间对方补发更正 → 判断重做")
RW.load_cfg = lambda: dict(CFG)
q = support.make_queue("comprehend_merge.db")
gb = GroupBrain(q)
w = make_worker(q, gb)
q.enqueue_inbound(ACC, GROUP, "l1", int(time.time()) - 6, "文本",
                  "@🍊 没那么爱对人了", sender=SENDER)
w.process(GROUP)
check("生成了草稿", len(q.list_outbound("draft")) == 1,
      str([(x["id"], x["text"]) for x in q.list_outbound("draft")]))
check("关系判断做了两次（补发后重做）", len(gb.an) == 2, str(gb.an))
check("第一次只看原话", gb.an and "怼*" not in gb.an[0], str(gb.an[:1]))
check("第二次带上补发的那句", len(gb.an) > 1 and "怼*" in gb.an[1], str(gb.an))
check("两轮生成，没有多余的", len(gb.comp) == 2, str([c[0] for c in gb.comp]))
check("最终这一轮用的是新判断",
      "第2次" in gb.comp[-1][1] and "第1次" not in gb.comp[-1][1],
      gb.comp[-1][1][:200])
check("合并后的文本进给模型",
      "没那么爱对人了" in gb.comp[-1][0] and "怼*" in gb.comp[-1][0],
      gb.comp[-1][0])

sys.exit(support.finish())
