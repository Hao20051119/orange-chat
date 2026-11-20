# -*- coding: utf-8 -*-
"""群聊：@ 识别、发送者前缀、门禁矩阵、@ 回复前缀。"""
import os
import sys
import time

import support
from support import check, section

import gate
import groupchat

GROUP = "10000000001@chatroom"
RAW = "wxid_example_bro:\n@🍊\u2005出来吧 我的orange！"

section("群消息清洗")
check("剥掉 wxid 前缀",
      groupchat.strip_sender_prefix(RAW) == "@🍊\u2005出来吧 我的orange！",
      repr(groupchat.strip_sender_prefix(RAW)))
check("私聊正文不受影响",
      groupchat.strip_sender_prefix("在吗") == "在吗")
check("多行正文只剥第一行",
      groupchat.strip_sender_prefix("wxid_a:\n第一行\n第二行") == "第一行\n第二行")

section("@ 识别（U+2005 也要认）")
names = ["🍊", "orange"]
for text, expect in [
    ("@🍊\u2005出来吧", True),
    ("@🍊 出来吧", True),
    ("@🍊出来吧", False),
    ("@orange 出来", True),
    ("出来吧 @🍊", True),
    ("@🍊🍊 不是", False),
    ("@小橙子 出来", False),
    ("没叫你", False),
]:
    check("%r -> %s" % (text, expect), groupchat.mentions_me(text, names) == expect)

check("空名字列表不误判", not groupchat.mentions_me("@🍊 hi", []))
check("微信分隔符之前的空格属于昵称",
      not groupchat.mentions_me("@小橘 同学\u2005在吗", ["小橘"]))
check("含空格的完整昵称也能识别",
      groupchat.mentions_me("@小橘 同学\u2005在吗", ["小橘 同学"]))
check("同名前缀的别人在微信里被 @，不改正文也不插话",
      not groupchat.mentions_me("@小橘子\u2005在吗", ["小橘"])
      and groupchat.strip_mention("@小橘子\u2005在吗", ["小橘"]).startswith("@小橘子"))
check("at_names 合并微信昵称",
      "🍊" in groupchat.at_names({"agent": {"at_names": ["orange"]}}, "🍊"))

section("去 @ 后正文")
check("去 @ 留正文",
      groupchat.strip_mention("@🍊\u2005出来吧 我的orange！", names)
      == "出来吧 我的orange！",
      repr(groupchat.strip_mention("@🍊\u2005出来吧 我的orange！", names)))

section("群聊回复加 @")
segs = groupchat.prepend_at(["在呢", "你叫我干嘛"], "🥭")
check("第一条加 @名字", segs[0] == "@🥭 在呢", str(segs))
check("第二条不加", segs[1] == "你叫我干嘛", str(segs))
check("空列表安全", groupchat.prepend_at([], "🥭") == [])
check("没名字不加", groupchat.prepend_at(["在呢"], "") == ["在呢"])
check("模型自己写的 @ 不会变成双份",
      groupchat.prepend_at(["@🥭 笑死"], "🥭") == ["@🥭 笑死"],
      str(groupchat.prepend_at(["@🥭 笑死"], "🥭")))
check("模型写的别的 @ 也统一成对方名字",
      groupchat.prepend_at(["@某人 哼"], "🥭") == ["@🥭 哼"],
      str(groupchat.prepend_at(["@某人 哼"], "🥭")))

section("门禁矩阵")
cfg_off = support.base_cfg()
check("群关着 → cancel",
      gate.evaluate(cfg_off, GROUP)["action"] == "cancel")
check("原因写明群聊未开启",
      "群聊" in gate.evaluate(cfg_off, GROUP)["reason"])

cfg = support.base_cfg(agent={"group_reply": True, "group_mode": "at_only"})
check("群里没@ → cancel", gate.evaluate(cfg, GROUP)["action"] == "cancel")
check("原因写明没@", "没@我" in gate.evaluate(cfg, GROUP)["reason"],
      gate.evaluate(cfg, GROUP)["reason"])
check("群里被@ → ok", gate.evaluate(cfg, GROUP, at_me=True)["action"] == "ok")

cfg_all = support.base_cfg(agent={"group_reply": True, "group_mode": "all"})
check("all 模式不@也回", gate.evaluate(cfg_all, GROUP)["action"] == "ok")

cfg_wl = support.base_cfg(agent={"group_reply": True, "group_mode": "at_only",
                                 "group_whitelist": ["other@chatroom"]})
check("不在群白名单 → cancel", not gate.evaluate(cfg_wl, GROUP, at_me=True)["action"] == "ok")
check("在群白名单 → ok",
      gate.evaluate(support.base_cfg(agent={
          "group_reply": True, "group_mode": "at_only",
          "group_whitelist": [GROUP]}), GROUP, at_me=True)["action"] == "ok")

section("联系人白名单不挡群")
cfg_pm = support.base_cfg(agent={"group_reply": True, "group_mode": "at_only",
                                 "reply_whitelist": ["wxid_someone_else"]})
check("私聊白名单只对私聊生效",
      gate.evaluate(cfg_pm, GROUP, at_me=True)["action"] == "ok")
check("私聊仍被白名单挡",
      gate.evaluate(cfg_pm, "wxid_other")["action"] == "cancel")

section("被 @ 时不掷随机数")
import brain as brain_mod
cfg_r = support.base_cfg(agent={"group_reply": True, "group_mode": "at_only",
                                "style": {"reply_probability": 0.0}})
b = brain_mod.Brain(cfg_r)
ok_pm, _why = b.should_reply(GROUP, "@🍊\u2005出来", at_me=False)
check("私聊概率 0 → 不回", not ok_pm)
ok_at, why_at = b.should_reply(GROUP, "@🍊\u2005出来", at_me=True)
check("被@ 必回", ok_at, why_at)
ok_empty, _ = b.should_reply(GROUP, "", at_me=True)
check("被@ 但正文为空也回", ok_empty)

section("被 @ 后的接话窗口")
now = time.time()
check("刚被@，同一个人接着说 → 算在聊",
      groupchat.followup_active(int(now - 60), "wxid_a", "wxid_a", 5, now))
check("超过窗口 → 不算",
      not groupchat.followup_active(int(now - 600), "wxid_a", "wxid_a", 5, now))
check("换人说话 → 不算",
      not groupchat.followup_active(int(now - 60), "wxid_a", "wxid_b", 5, now))
check("从没被@ → 不算",
      not groupchat.followup_active(0, "", "wxid_a", 5, now))
check("窗口为 0 → 关闭",
      not groupchat.followup_active(int(now - 1), "wxid_a", "wxid_a", 0, now))

section("一批里只回一个人")
now = time.time()
mixed = [
    {"id": 1, "sender": "wxid_a", "content": "随便聊聊"},
    {"id": 2, "sender": "wxid_b", "content": "@🍊\u2005出来"},
    {"id": 3, "sender": "wxid_c", "content": "我也说一句"},
]
target, explicit = groupchat.pick_target(mixed, ["🍊"], 5, 0, "", now)
check("回最近 @ 我的人", (target, explicit) == ("wxid_b", True), str((target, explicit)))

only_ctx = [{"id": 1, "sender": "wxid_a", "content": "随便聊聊"},
            {"id": 2, "sender": "wxid_c", "content": "我也说一句"}]
target, explicit = groupchat.pick_target(only_ctx, ["🍊"], 5, 0, "", now)
check("没@也没窗口 → 不回", (target, explicit) == ("", False), str((target, explicit)))

target, explicit = groupchat.pick_target(only_ctx, ["🍊"], 5, int(now - 30), "wxid_c", now)
check("窗口内同一个人 → 回他", (target, explicit) == ("wxid_c", False),
      str((target, explicit)))

target, explicit = groupchat.pick_target(only_ctx, ["🍊"], 5, int(now - 30), "wxid_a", now)
check("窗口内是别人 → 不回", (target, explicit) == ("", False), str((target, explicit)))

section("回复决策（模型自己判断，@ 必回）")
from brain import Brain as _Brain
import persona as _persona

cfg_d = support.base_cfg(agent={"group_reply": True, "group_mode": "at_only",
                                "llm_decide": True, "skip_token": "[[SKIP]]"})
b_d = _Brain(cfg_d)
check("识别跳过标记", b_d.is_skip_text("[[SKIP]]"))
check("带空白也识别", b_d.is_skip_text("  [[SKIP]]  "))
check("正常文本不跳过", not b_d.is_skip_text("在呀"))
check("关掉决策就不认标记",
      not _Brain(support.base_cfg(agent={"llm_decide": False})).is_skip_text("[[SKIP]]"))

prompt_d = _persona.build_system_prompt("", cfg_d)
check("提示词有回复决策段", "【回复决策】" in prompt_d)
check("提示词写明被点名必须回", "必须回" in prompt_d)
check("提示词包含跳过标记", "[[SKIP]]" in prompt_d)

ok_ask, _ = b_d.should_reply(GROUP, "随便说一句", at_me=True)
check("被@不再掷随机数", ok_ask)
cfg_p = support.base_cfg(agent={"llm_decide": True,
                                "style": {"reply_probability": 0.0}})
ok_prob, _ = _Brain(cfg_p).should_reply("wxid_x", "在吗")
check("llm_decide 开着时概率不生效", ok_prob)

section("颜文字")
import json
# 开源版里 persona_card.json 不入库（个人人设）：退回示例卡，并告诉 agent 用哪一份
card_path = os.path.join(support.ROOT, "persona_card.json")
if not os.path.exists(card_path):
    card_path = os.path.join(support.ROOT, "persona_card.example.json")
    os.environ.setdefault("WXBOT_PERSONA_CARD", card_path)
card = json.load(open(card_path, encoding="utf-8-sig"))
kao = (card.get("style") or {}).get("kaomoji") or []
check("人设卡有颜文字列表", len(kao) >= 4, str(kao))
prompt_k = _persona.build_system_prompt("", support.base_cfg(), chat="wxid_x")
check("提示词包含颜文字示例", any(k in prompt_k for k in kao), str(kao[:2]))
check("提示词要求不是每条都加", "不要每条都加" in prompt_k or "偶尔" in prompt_k)

section("群聊状态落库")
from wxqueue import Queue as _Q
q = support.make_queue("group_state.db")
check("初始为 0", q.last_at(GROUP) == (0, ""), str(q.last_at(GROUP)))
q.mark_at(GROUP, "wxid_a")
ts, by = q.last_at(GROUP)
check("@ 时间已记录", ts > 0 and by == "wxid_a", str((ts, by)))
q.mark_at(GROUP, "wxid_b")
check("后来者覆盖", q.last_at(GROUP)[1] == "wxid_b")

section("出站任务记住 at_me")
tid_at = q.create_outbound("wxid_test", GROUP, "群里回一句",
                           segments=["群里回一句"], at_me=True)
tid_pm = q.create_outbound("wxid_test", "wxid_x", "私聊回一句",
                           segments=["私聊回一句"])
check("群任务 at_me=1", q.get_outbound(tid_at)["at_me"] == 1,
      str(q.get_outbound(tid_at)["at_me"]))
check("私聊 at_me=0", q.get_outbound(tid_pm)["at_me"] == 0)

section("纯表情/图片交给模型判断，不硬跳过")
import reply_worker as RW

emoji_msg = {"id": 1, "msg_type": "动画表情", "type_code": 47, "content": "<msg/>"}
text_msg = {"id": 2, "msg_type": "文本", "type_code": 1, "content": "在吗"}
hint = RW.media_only_hint([emoji_msg], recent_reply=False)
check("没刚回过时提示可以接一句", "看得懂就接一句" in hint, hint)
hint2 = RW.media_only_hint([emoji_msg], recent_reply=True)
check("刚回过时提示通常不用回", "别再回" in hint2 or "通常" in hint2, hint2)
check("有文字时不加提示", RW.media_only_hint([text_msg, emoji_msg], False) == "")

quote_msg = {"id": 3, "msg_type": "文件/链接/卡片", "type_code": 49,
             "content": '<msg><appmsg><title>你决定？</title><type>57</type>'
                        '<refermsg><type>1</type><displayname>🍊</displayname>'
                        '<content>行吧行吧</content></refermsg></appmsg></msg>'}
check("引用回复不算纯表情", RW.media_only_hint([quote_msg], False) == "",
      RW.media_only_hint([quote_msg], False))
link_msg = {"id": 4, "msg_type": "文件/链接/卡片", "type_code": 49,
            "content": '<msg><appmsg><title>一篇文章</title><type>5</type>'
                       '<url>https://x.com/a</url></appmsg></msg>'}
check("链接卡片不算纯表情", RW.media_only_hint([link_msg], False) == "")

section("回复决策里的表情规则")
prompt_d2 = _persona.build_system_prompt("", support.base_cfg(), chat="wxid_x")
check("写明表情不能默认不回", "不能默认不回" in prompt_d2, prompt_d2[-400:])
check("写明刚回过的表情通常不用回", "只补了个表情" in prompt_d2)

section("群聊关系判断")
import brain as _bm
cfg_an = support.base_cfg(agent={"group_reply": True, "group_analysis": True})
b_an = _bm.Brain(cfg_an)
b_an.mem = support.make_memory("analyze.db")
for i, (who, txt) in enumerate([("张伟", "今晚谁去打球"), ("李娜", "我不去"),
                                 ("张伟", "@🍊 你说呢"), ("橙子", "在呢")]):
    b_an.mem.add(GROUP, "user", txt, src_key="in:%d" % i, sender=who)
calls_an = []


def fake_analyze(msgs, config=None, timeout=60, max_tokens=None):
    calls_an.append(1)
    return {"ok": True, "text": "【谁对谁说】张伟 对 🍊\n【在回应】问谁去打球那句\n"
                                "【要不要接】要接，被点名了\n【注意】李娜不想去，别劝她"}


_orig_an = _bm.llm_chat
_bm.llm_chat = fake_analyze
try:
    note = b_an.analyze_group(GROUP, "张伟", "@🍊 你说呢")
finally:
    _bm.llm_chat = _orig_an
check("拿到关系判断", "谁对谁说" in note and "张伟" in note, note)
check("走了一次模型", len(calls_an) == 1)

_bm.llm_chat = lambda *a, **k: {"ok": False, "error": "x"}
try:
    fail_note = b_an.analyze_group(GROUP, "张伟", "随便")
finally:
    _bm.llm_chat = _orig_an
check("模型失败不影响", fail_note == "")

b_off = _bm.Brain(support.base_cfg(agent={"group_analysis": False}))
b_off.mem = b_an.mem
check("关掉开关就不判断", b_off.analyze_group(GROUP, "张伟", "随便") == "")

section("历史里带说话人")
sp = b_an.build_messages(GROUP, "在吗")[0]["content"]
hist = [m["content"] for m in b_an.build_messages(GROUP, "在吗")
        if m["role"] == "user"]
check("群历史标了名字", any(h.startswith("张伟：") or "张伟：" in h for h in hist),
      str(hist[-2:]))
check("私聊历史不加名字",
      not any("：" in (m["content"] or "")[:6]
              for m in b_an.build_messages("wxid_x", "在吗")[1:]
              if m["role"] == "user"))

section("群聊提示词")
msgs = b.build_messages(GROUP, "@🍊\u2005出来吧", at_me=True,
                        sender="wxid_example_bro", sender_display="🥭")
system = msgs[0]["content"]
check("提示是群聊", "群聊" in system)
check("提示在@我", "@" in system and ("@ 你" in system or "在群里 @ 你" in system),
      system[-200:])
check("带说话人名字", "🥭" in system, system[-200:])
check("要求不要自己写 @",
      "不要" in system and "@名字" in system, system[-200:])
check("写明要回谁", "现在要回的是" in system and "🥭" in system)
check("私聊不注入群提示",
      "群里" not in b.build_messages("wxid_x", "在吗")[0]["content"])

section("群里认得这个人（私人记忆带进来）")
MEMBER = "wxid_member_1"
cfg_mm = support.base_cfg(agent={"group_reply": True, "group_member_memory": True},
                          contacts={MEMBER: {"relation": "哥哥", "nickname": "老王"}})
b_mm = _bm.Brain(cfg_mm)
b_mm.mem = support.make_memory("member.db")
b_mm.mem.add(MEMBER, "user", "这是我私聊说的话：我下周三生日", src_key="in:p1")
b_mm.mem.add_fact(MEMBER, "下周三生日", topic="生日")
b_mm.mem.save_adapt(MEMBER, "【说话方式】短句，爱开玩笑", 5)

sp = b_mm.build_messages(GROUP, "在吗", sender=MEMBER,
                         sender_display="朋友B")[0]["content"]
check("群里有关系", "关系：哥哥" in sp, sp[-600:])
check("群里有私下称呼", "老王" in sp)
check("群里有私人事实", "下周三生日" in sp)
check("群里有私下观察", "短句，爱开玩笑" in sp)
check("私聊原文不带进群", "我下周三生日" not in sp)
check("提示不要爆料隐私", "不要说出来源" in sp or "爆料隐私" in sp, sp[-300:])

b_off = _bm.Brain(support.base_cfg(agent={"group_member_memory": False}))
b_off.mem = b_mm.mem
check("开关关闭就不带", b_off.member_context(GROUP, MEMBER) == "")
check("私聊不注入成员块", b_mm.member_context("wxid_x", MEMBER) == "")
check("没有发送者不注入", b_mm.member_context(GROUP, "") == "")

sys.exit(support.finish())
