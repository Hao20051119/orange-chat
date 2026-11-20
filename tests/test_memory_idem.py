# -*- coding: utf-8 -*-
"""记忆：src_key 幂等、摘要保留最近 N 条、记忆开关、媒体描述缓存。"""
import sys
import time

import support
from support import check, section

section("src_key 幂等")
m = support.make_memory("idem.db")
m.add("chatA", "user", "同一句", src_key="in:1")
m.add("chatA", "user", "同一句", src_key="in:1")
check("同 src_key 只写一次", m.count("chatA") == 1, str(m.count("chatA")))
m.add("chatA", "user", "同一句", src_key="in:2")
check("不同 src_key 正常追加", m.count("chatA") == 2)
m.add("chatA", "assistant", "没有键")
m.add("chatA", "assistant", "没有键")
check("无键则允许重复（兼容旧调用）", m.count("chatA") == 4)

section("事实：并列不互相删除")
mf = support.make_memory("facts.db")
mf.add_fact("chatF", "他喜欢猫")
r = mf.add_fact("chatF", "他喜欢狗")
kept = mf.facts("chatF")
# 早先相似度 >=0.55 就把「他喜欢猫」判成同一件事并置为失效，
# 结果并列爱好互相删除，记忆越聊越少。
check("并列爱好都保留", "他喜欢猫" in kept and "他喜欢狗" in kept, str(kept))
check("没有把上一条标成冲突", not r.get("superseded"), str(r))
mf.add_fact("chatF", "职业是学生", topic="职业")
r2 = mf.add_fact("chatF", "职业是老师", topic="职业")
kept2 = mf.facts("chatF")
check("同一 topic 覆盖旧值",
      "职业是老师" in kept2 and "职业是学生" not in kept2, str(kept2))
check("覆盖被记录为 superseded", bool(r2.get("superseded")), str(r2))

section("摘要（32 条保留 20）")
m2 = support.make_memory("sum.db")
for i in range(32):
    m2.add("chatS", "user", "第%d句" % i, src_key="in:%d" % i)


def good_llm(msgs):
    return {"ok": True, "text": "【摘要】对方聊了三十多句废话\n"
                                "【画像】话多的人\n"
                                "【事实】\n- 喜欢猫\n- 住在杭州"}


r = m2.summarize("chatS", good_llm, keep_recent=20, batch=40)
check("摘要成功", r.get("ok"), str(r))
check("剩余正好 20 条", m2.count("chatS") == 20, str(m2.count("chatS")))
p = m2.profile("chatS")
check("摘要已写入", "三十多句" in (p.get("summary") or ""), p.get("summary"))
check("事实已写入", "喜欢猫" in (m2.facts("chatS") or []), str(m2.facts("chatS")))


def bad_llm(msgs):
    return {"ok": True, "text": "抱歉我不会总结"}


before = m2.count("chatS")
r4 = m2.summarize("chatS", bad_llm, keep_recent=5, batch=10)
check("格式无效 → 失败", not r4.get("ok"), str(r4))
check("失败不动原文", m2.count("chatS") == before, "%d/%d" % (before, m2.count("chatS")))

def empty_llm(msgs):
    return {"ok": False, "error": "模型返回空内容"}


r5 = m2.summarize("chatS", empty_llm, keep_recent=5, batch=10)
check("模型失败 → 失败", not r5.get("ok"))

section("摘要补偿扫描")
m3 = support.make_memory("sweep.db")
for i in range(12):
    m3.add("chatT", "user", "x%d" % i, src_key="in:%d" % i)
rows = m3.chats_needing_summary(threshold=10)
check("扫到超阈值会话", [x["chat"] for x in rows] == ["chatT"], str(rows))

section("媒体描述缓存")
check("未缓存时为 None", m3.media_desc("abc") is None)
m3.save_media_desc("abc", "一只在笑的猫")
check("写入后可读", m3.media_desc("abc") == "一只在笑的猫")
m3.save_media_desc("abc", "另一句")
check("重复写是更新", m3.media_desc("abc") == "另一句")

section("临时记忆（memory.enabled=false）")
from ephemeral import EphemeralStore

e = EphemeralStore(ttl=60)
e.add("c1", "user", "hi")
e.add("c1", "assistant", "hello")
check("临时记忆可读", len(e.recent("c1")) == 2)
check("临时记忆无画像", e.profile("c1")["summary"] == "")
check("临时记忆不摘要", not e.should_summarize("c1", 0))
check("persistent 标记", e.persistent is False and m3.persistent is True)

section("Brain 不写记忆 / 记忆开关")
import brain as brain_mod
from brain import Brain

writes = []
cfg = support.base_cfg(memory={"enabled": True})
b = Brain(cfg)
b.mem = support.make_memory("brain.db")


def fake_llm(msgs, config=None, timeout=60, max_tokens=None):
    writes.append(msgs[-1]["content"])
    return {"ok": True, "text": "好呀"}


b_mod_llm = brain_mod.llm_chat
brain_mod.llm_chat = fake_llm
try:
    comp = b.compose("chatB", "在吗")
finally:
    brain_mod.llm_chat = b_mod_llm
check("compose 成功", comp.get("ok"), str(comp))
check("compose 不写记忆", b.mem.count("chatB") == 0, str(b.mem.count("chatB")))

cfg_off = support.base_cfg(memory={"enabled": False})
b2 = Brain(cfg_off)
check("开关关闭 → 临时记忆", getattr(b2.mem, "persistent", True) is False,
      type(b2.mem).__name__)
b2.reload(support.base_cfg(memory={"enabled": True}))
check("热切换回长期记忆", getattr(b2.mem, "persistent", False) is True,
      type(b2.mem).__name__)

section("上下文不重复当前这句")
b_ctx = Brain(support.base_cfg())
b_ctx.mem = support.make_memory("ctx.db")
CH = "wxid_ctx"
b_ctx.mem.add(CH, "user", "在吗", src_key="in:1")
msgs = b_ctx.build_messages(CH, "在吗")
user_msgs = [m for m in msgs if m["role"] == "user"]
check("当前这句话只出现一次",
      len(user_msgs) == 1 and user_msgs[-1]["content"] == "在吗",
      str([m["content"] for m in user_msgs]))
check("最后一条是本轮消息", msgs[-1]["content"] == "在吗")

# 批量的情况：三条记忆里最后两条正好等于本轮合并文本
b_ctx.mem.add(CH, "user", "晚上吃什么", src_key="in:2")
b_ctx.mem.add(CH, "user", "给我推荐几家", src_key="in:3")
msgs = b_ctx.build_messages(CH, "晚上吃什么\n给我推荐几家")
user_msgs = [m for m in msgs if m["role"] == "user"]
batch = [m for m in user_msgs if m["content"] == "晚上吃什么\n给我推荐几家"]
check("合并批次只出现一次", len(batch) == 1,
      str([m["content"] for m in user_msgs]))
check("更早的历史保留", any("在吗" in m["content"] for m in user_msgs),
      str([m["content"] for m in user_msgs]))

# 历史里带时间前缀也要认出来
b_ctx2 = Brain(support.base_cfg())
b_ctx2.mem = support.make_memory("ctx2.db")
b_ctx2.mem.add(CH, "user", "在吗", src_key="in:9")
trimmed = b_ctx2._trim_current(
    [{"role": "user", "content": "[11-17 19:13] 在吗"}], "在吗")
check("带时间前缀也能裁掉", trimmed == [], str(trimmed))
check("不相关的历史不裁",
      b_ctx2._trim_current([{"role": "user", "content": "上一句"}], "在吗") != [])

section("长期观察笔记")
m_ad = support.make_memory("adapt.db")
check("初始为空", m_ad.get_adapt("cA")["notes"] == "")
m_ad.save_adapt("cA", "【说话方式】短句\n【我该怎么回】也短", 10)
ad = m_ad.get_adapt("cA")
check("能读回", "短句" in ad["notes"] and ad["seen"] == 10, str(ad))
for i in range(12):
    m_ad.add("cA", "user", "第%d句" % i)
rows = m_ad.chats_needing_adapt(every=2)
check("攒够新消息会进待观察列表", any(r["chat"] == "cA" for r in rows), str(rows))

b_ad = Brain(support.base_cfg(memory={"adapt_every": 5}))
b_ad.mem = support.make_memory("adapt2.db")
for i in range(10):
    b_ad.mem.add("cB", "user", "第%d句" % i)
check("没有笔记时不注入",
      "长期观察" not in b_ad.build_messages("cB", "在吗")[0]["content"])
b_ad.mem.save_adapt("cB", "【说话方式】对方话很短", 10)
sp = b_ad.build_messages("cB", "在吗")[0]["content"]
check("有笔记时注入提示词", "长期观察" in sp and "话很短" in sp, sp[-120:])

calls_ad = []


def fake_adapt_llm(msgs, config=None, timeout=60, max_tokens=None):
    calls_ad.append(1)
    return {"ok": True, "text": "【说话方式】短句\n【情绪特点】平稳\n"
                                "【别踩的点】无\n【我该怎么回】也短一点"}


_orig_llm2 = brain_mod.llm_chat
brain_mod.llm_chat = fake_adapt_llm
try:
    r_ad = b_ad.maybe_adapt("cB", force=True)
finally:
    brain_mod.llm_chat = _orig_llm2
check("观察成功", r_ad.get("ok"), str(r_ad))
check("笔记已保存", "也短一点" in b_ad.mem.get_adapt("cB")["notes"],
      b_ad.mem.get_adapt("cB")["notes"])
check("seen 更新到当前条数", b_ad.mem.get_adapt("cB")["seen"] == 10,
      str(b_ad.mem.get_adapt("cB")["seen"]))
check("再调一次不会重复消耗",
      not b_ad.maybe_adapt("cB").get("ok"))

check("临时记忆没有观察笔记",
      EphemeralStore().get_adapt("c1")["notes"] == "")

section("人设与关系优先，记忆次要")
b_h = Brain(support.base_cfg())
b_h.mem = support.make_memory("hier.db")
b_h.mem.add("cH", "user", "我喜欢猫", src_key="in:1")
b_h.mem.add_fact("cH", "对方养了两只猫", topic="宠物")
sp_h = b_h.build_messages("cH", "在吗")[0]["content"]
check("提示词声明最高优先级", "【最高优先级】" in sp_h, sp_h[:80])
check("写明冲突以人设和关系为准", "以人设和关系为准" in sp_h)
check("记忆区标为次要参考", "【参考记忆" in sp_h, sp_h[-200:])
check("事实被放进记忆区", "两只猫" in sp_h)

section("长上下文：截断与预算")
b_l = Brain(support.base_cfg(memory={"max_turns": 20, "history_msg_chars": 600,
                                     "max_context_chars": 6000}))
b_l.mem = support.make_memory("budget.db")
b_l.mem.add("cL", "user", "长" * 1000, src_key="in:long")
msgs = b_l.build_messages("cL", "hi")
hist = [m for m in msgs if m["role"] == "user" and m["content"] != "hi"]
check("单条历史被截断到 600 字以内",
      all(len(m["content"]) <= 600 for m in hist),
      str([len(m["content"]) for m in hist]))

for i in range(20):
    b_l.mem.add("cL", "user", "第%d条" % i + "长" * 500, src_key="in:%d" % i)
msgs = b_l.build_messages("cL", "hi")
total = len(msgs[0]["content"]) + sum(len(m["content"]) for m in msgs[1:])
check("总字数受预算限制", total <= 6000 + 600, str(total))
check("老历史被丢弃", len(msgs) < 22, str(len(msgs)))
check("最新一句一定保留", msgs[-1]["content"] == "hi")

sys.exit(support.finish())
