# -*- coding: utf-8 -*-
"""代传话：联系人A请 Orange 给另一位联系人转达提醒。

规则：只有关系网里（人设卡 relations 且绑定了会话）的人才发得出去——
发起人和收件人都得在网里，发不出去就如实说，不许假装发了。
"""
import json
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import support
from support import check, section

import relay
import reminder

ACCOUNT = "wxid_test"
FRIEND = "wxid_friend"
OWNER = "wxid_owner"
STRANGER = "wxid_stranger"

RELS = [
    {"name": "alex", "nickname": "alex", "relation": "你的哥哥", "chat": OWNER},
    {"name": "联系人A", "nickname": "组长", "relation": "活动组长", "chat": FRIEND},
    {"name": "老妈", "nickname": "老妈", "relation": "妈妈", "chat": ""},
]


class FakeBrain:
    def __init__(self, reply="睡了睡了，这就去"):
        self.reply = reply
        self.calls = []

    def compose_reminder(self, chat, content, original="", when_text="",
                         late=False, timeout=10, **kw):
        self.calls.append({"chat": chat, "content": content,
                           "when_text": when_text, "relay_from": kw.get("relay_from")})
        return {"ok": True, "reply": self.reply}


def make_cfg(**over):
    cfg = support.base_cfg()
    cfg["agent"]["owner_wxid"] = OWNER
    cfg["proactive"] = {
        "enabled": True,
        "reminders": {"enabled": True, "llm_text": True, "llm_timeout": 10},
        "relay": {"enabled": True, "delay_seconds": 0, "daily_limit": 8,
                  "polish": False},
    }
    for k, v in (over or {}).items():
        cfg[k] = v
    return cfg


def relay_message(id_, content, sort_seq):
    return {"id": id_, "content": content, "msg_type": "文本",
            "msg_ts": int(time.time()), "sort_seq": sort_seq}


def decision(kind, target=""):
    """判读答复的模型输出（测试里替掉真调用）。"""
    return {"decision": kind, "target": target, "confidence": 0.95, "reason": "test"}


def acknowledge_relay_prompt(q, sort_seq):
    """Simulate confirmed WeChat readback for one actual queued question."""
    task = q.next_outbound(account=ACCOUNT)
    check("核对问句排给发起人", bool(task) and task["kind"] == "relay_clarify"
          and task["chat"] == FRIEND, str(task))
    if task is None or task["kind"] != "relay_clarify":
        return None
    check("核对问句不含微信号", "wxid_" not in task["text"], task["text"])
    check("回读后确认送达", q.bump_segments(task["id"], 1, complete=True,
                                              sent_after_seq=sort_seq))
    return q.get_outbound(task["id"])


section("收件人映射：名字 → 会话")
cfg = make_cfg()
check("owner → 主人", relay.target_of(cfg, "owner", RELS)[0] == OWNER)
check("点名 → 关系表里的会话",
      relay.target_of(cfg, "alex", RELS)[0] == OWNER)
cfg_no_owner = make_cfg()
cfg_no_owner["agent"]["owner_wxid"] = ""
check("没配主人：说没他微信",
      relay.target_of(cfg_no_owner, "owner", RELS) == ("", "没有他的微信"))
cfg_out = make_cfg()
cfg_out["agent"]["owner_wxid"] = STRANGER
check("主人不在关系表也发得到：关系表只管称呼，不是发送权限",
      relay.target_of(cfg_out, "owner", RELS) == (STRANGER, ""))
check("关系没绑定会话：发不了",
      relay.target_of(cfg, "老妈", RELS) == ("", "关系里没绑定会话"))
check("关系网里没这个人",
      relay.target_of(cfg, "路人", RELS) == ("", "关系网里没这个人"))
check("网内发起人通过", relay.requester_ok(cfg, FRIEND, RELS))
check("网外发起人不通过", not relay.requester_ok(cfg, STRANGER, RELS))

section("匹配完整姓名、昵称与关系表显示标签")
label_relations = [
    {"name": "林澄澄", "nickname": "青柚", "chat": FRIEND},
    {"name": "柯新", "nickname": "North 42", "chat": OWNER},
]
for alias in ("柯新", "North 42", "柯新（North 42）", "North 42（柯新）",
              "「柯新 (Ｎｏｒｔｈ　４２)」。", "“柯 新（North 42）”!",
              "North　42？"):
    check("完整姓名、昵称或同一条关系的标签可以定位：" + alias,
          relay.target_of(cfg, alias, label_relations) == (OWNER, ""))
for alias in ("柯", "North", "柯新（青柚）", "柯新（陌生人）", "青柚（North 42）"):
    check("不根据截短名字或拼接标签猜收件人：" + alias,
          relay.target_of(cfg, alias, label_relations)[0] == "")
collision_relations = label_relations + [
    {"name": "Ｎｏｒｔｈ４２", "nickname": "北极", "chat": STRANGER},
]
collision_chat, collision_reason = relay.target_of(cfg, "North 42", collision_relations)
check("宽窄、空白归一后命中两个会话会拒绝", not collision_chat and bool(collision_reason))
check("完整标签仍能明确指向同一条关系",
      relay.target_of(cfg, "柯新（North 42）", collision_relations) == (OWNER, ""))
repeated_label = label_relations + [
    {"name": "Ｎｏｒｔｈ４２", "nickname": "柯新", "chat": STRANGER},
]
check("两个会话的正反组合相同也无法确定",
      not relay.target_of(cfg, "柯新（North 42）", repeated_label)[0])

section("模型返回显示标签时，代问候能安排并排入出站")
label_queue = support.make_queue("relay_formatted_label.db")
greeting = "帮我向North 42问个晚上好"
with patch("llm.chat", return_value={"ok": True, "text": json.dumps({
        "is_relay": True, "kind": "say", "target": "柯新（North 42）",
        "targets": ["柯新（North 42）"], "content": "晚上好", "confidence": 0.95,
    }, ensure_ascii=False)}) as model:
    greeted = relay.maybe_relay(cfg, label_queue, FRIEND, [{"id": 1, "content": greeting}],
                                ACCOUNT, relations=label_relations)
check("模型确实收到含姓名和昵称的关系表",
      model.call_count == 1 and all(name in model.call_args.args[0][0]["content"]
                                    for name in ("柯新", "North 42")))
check("只安排给标签对应的收件人",
      len(greeted) == 1 and greeted[0]["ok"] and greeted[0]["chat"] == OWNER, str(greeted))
reminder.fire_due(label_queue, cfg, now=time.time() + 1)
label_task = label_queue.next_outbound(account=ACCOUNT)
check("问候排到目标会话等待发送",
      bool(label_task) and label_task["chat"] == OWNER and "晚上好" in label_task["text"])

section("收件人无法确定时说明尚未发送")
refused_queue = support.make_queue("relay_unmatched_label.db")
with patch("llm.chat", return_value={"ok": True, "text": json.dumps({
        "is_relay": True, "kind": "say", "target": "柯新（青柚）",
        "content": "晚上好", "confidence": 0.95,
    }, ensure_ascii=False)}):
    rejected = relay.maybe_relay(cfg, refused_queue, FRIEND,
                                 [{"id": 2, "content": "帮我问问陌生人晚上好"}],
                                 ACCOUNT, relations=label_relations)
check("拼错标签在排期前被拒绝",
      len(rejected) == 1 and not rejected[0]["ok"] and
      not refused_queue.list_reminders(account=ACCOUNT), str(rejected))
refusal_hint = relay.hint_line(rejected)
check("回复提示区分尚未发送和送达失败",
      "尚未发送" in refusal_hint and "送达失败" in refusal_hint, refusal_hint)
refusal_reply = relay.refusal_reply(rejected) or ""
check("实际回执只说明没有发送，不猜收件人是否能收到",
      "没有发送" in refusal_reply and "收不到" not in refusal_reply and
      "送达失败" not in refusal_reply, refusal_reply)

q_mismatch = support.make_queue("relay_model_mismatch.db")
with patch.object(relay, "judge", return_value={
        "is_relay": True, "kind": "say", "target": "简称没对上",
        "content": "晚上好", "confidence": 0.95}):
    recovered = relay.maybe_relay(cfg, q_mismatch, FRIEND, [{"id": 3, "content": greeting}],
                                 ACCOUNT, relations=label_relations)
check("模型回填的名字没对上时，可按原消息的登记昵称核对",
      recovered and recovered[0].get("pending") and "柯新" in recovered[0]["ask"]
      and not q_mismatch.list_reminders(account=ACCOUNT), recovered)

section("真的发出去：目标聊天里存一条 relay 提醒")
q = support.make_queue("relay.db")
cfg = make_cfg()
rid = relay.deliver(cfg, q, FRIEND, OWNER, "该睡觉了", from_name="联系人A",
                    account=ACCOUNT, now=time.time(), relations=RELS)
check("建了提醒", rid is not None, str(rid))
rem = q.get_reminder(rid)
check("收件人是「他」不是当前聊天", rem["chat"] == OWNER, rem["chat"])
check("标记成代传话", rem["confirm_mode"] == "relay", rem["confirm_mode"])
check("记下谁托的", rem["relay_from"] == "联系人A", rem["relay_from"])

reminder.fire_due(q, cfg, now=time.time() + 1)
task = q.next_outbound(account=ACCOUNT)
check("到点排进出站", task is not None and task["chat"] == OWNER)
check("兜底文案交代是谁托的", task["text"] == "联系人A让我提醒你：该睡觉了",
      task["text"])
brain = FakeBrain()
task = reminder.polish_task(q, cfg, brain, task)
check("交给模型改写时带上委托人", brain.calls and brain.calls[0]["relay_from"] == "联系人A",
      str(brain.calls[:1]))
check("改写结果落库", q.get_outbound(task["id"])["text"] == "睡了睡了，这就去")

section("边界")
check("不给同一个会话代传话",
      relay.deliver(cfg, q, FRIEND, FRIEND, "睡觉", from_name="x",
                    account=ACCOUNT, relations=RELS) is None)
check("群聊不发", relay.deliver(cfg, q, FRIEND, "123@chatroom", "睡觉",
                                from_name="x", account=ACCOUNT, relations=RELS) is None)
check("关系网外的人不发",
      relay.deliver(cfg, q, FRIEND, STRANGER, "睡觉", from_name="x",
                    account=ACCOUNT, relations=RELS) is None)
check("网外发起人也不行",
      relay.deliver(cfg, q, STRANGER, OWNER, "睡觉", from_name="x",
                    account=ACCOUNT, relations=RELS) is None)
cfg_black = make_cfg()
cfg_black["agent"]["reply_blacklist"] = [OWNER]
check("黑名单不发", relay.deliver(cfg_black, q, FRIEND, OWNER, "睡觉",
                                  from_name="x", account=ACCOUNT, relations=RELS) is None)
cfg_off = make_cfg()
cfg_off["proactive"]["relay"]["enabled"] = False
check("开关关了不发", relay.deliver(cfg_off, q, FRIEND, OWNER, "睡觉",
                                    from_name="x", account=ACCOUNT, relations=RELS) is None)
cfg_lim = make_cfg()
cfg_lim["proactive"]["relay"]["daily_limit"] = 1
check("当天次数封顶", relay.deliver(cfg_lim, q, FRIEND, OWNER, "再睡",
                                    from_name="x", account=ACCOUNT, relations=RELS) is None)

section("整条链路：网内发起 → 模型判定 → 建单 → 回复知道已经安排好了")
q2 = support.make_queue("relay2.db")
cfg2 = make_cfg()
now = int(time.time())
q2.enqueue_inbound(ACCOUNT, FRIEND, 1, now, "文本",
                   "你主动提醒一下他该睡觉了", sender=FRIEND)
msg = q2.newest_pending(FRIEND, account=ACCOUNT)[0]
VERDICT = {"is_relay": True, "target": "owner", "content": "该睡觉了",
           "confidence": 0.9, "tz": "", "reason": ""}
with patch.object(relay, "judge", return_value=VERDICT):
    items = relay.maybe_relay(cfg2, q2, FRIEND, [msg], ACCOUNT, relations=RELS)
check("扫出了代传话", len(items) == 1 and items[0]["ok"], str(items))
check("记了动作，避免再被当成自己的提醒",
      q2.has_action(ACCOUNT, FRIEND, str(msg["id"])))
line = relay.hint_line(items)
check("回复提示里说清发给谁、说什么",
      "该睡觉了" in line and "alex" in line and "已经安排" in line, line)

section("网外发起人：一条都不发，回复如实回绝")
q3 = support.make_queue("relay3.db")
q3.enqueue_inbound(ACCOUNT, STRANGER, 1, now, "文本",
                   "你主动提醒一下他该睡觉了", sender=STRANGER)
msg3 = q3.newest_pending(STRANGER, account=ACCOUNT)[0]
items3 = relay.maybe_relay(cfg2, q3, STRANGER, [msg3], ACCOUNT, relations=RELS)
check("没建任何提醒", q3.add_reminder and not items3[0]["ok"], str(items3))
check("给了回绝原因", items3 and items3[0]["reason"] == "对方不在你的关系网里",
      str(items3))
bad = None
with patch.object(relay, "judge", return_value={
        "is_relay": True, "target": "老妈", "candidates": [], "need_target": False,
        "content": "吃药", "confidence": 0.9, "tz": "", "reason": ""}):
    bad = relay.maybe_relay(cfg2, q2, FRIEND, [{"id": 99, "content": "帮我提醒老妈吃药"}],
                            ACCOUNT, relations=RELS)
check("关系没绑会话 → 如实说发不了",
      bad and not bad[0]["ok"] and "没绑定会话" in bad[0]["reason"], str(bad))
check("回绝提示不许假装发了",
      "不许说已经发了" in relay.hint_line(items3)
      and "已经安排" not in relay.hint_line(items3), relay.hint_line(items3))

section("同一条入站只兑现一次")
q4 = support.make_queue("relay4.db")
cfg4 = make_cfg()
q4.enqueue_inbound(ACCOUNT, FRIEND, 1, now, "文本",
                   "你主动提醒一下他该睡觉了", sender=FRIEND)
m4 = q4.newest_pending(FRIEND, account=ACCOUNT)[0]
with patch.object(relay, "judge", return_value=VERDICT):
    first = relay.maybe_relay(cfg4, q4, FRIEND, [m4], ACCOUNT, relations=RELS)
    again = relay.maybe_relay(cfg4, q4, FRIEND, [m4], ACCOUNT, relations=RELS)
check("第一次建了单", len(first) == 1 and first[0]["ok"], str(first))
check("重试再来一轮不重复建单", again == [], str(again))
check("库里只有一条代传话提醒",
      len([r for r in q4.list_reminders(OWNER, account=ACCOUNT)
           if r["confirm_mode"] == "relay"]) == 1)

section("登记写失败：提醒不能留下（建单 + 登记同一事务）")
q5 = support.make_queue("relay5.db")
cfg5 = make_cfg()


def boom(*args, **kwargs):
    raise sqlite3.OperationalError("模拟登记写入失败")


with patch.object(q5, "record_action", side_effect=boom):
    lost = relay.deliver(cfg5, q5, FRIEND, OWNER, "该睡觉了", from_name="x",
                         account=ACCOUNT, relations=RELS, src_key="9001")
check("登记失败时整单回滚", lost is None
      and not q5.list_reminders(OWNER, account=ACCOUNT), str(lost))
kept = relay.deliver(cfg5, q5, FRIEND, OWNER, "该睡觉了", from_name="x",
                     account=ACCOUNT, relations=RELS, src_key="9001")
check("恢复后重试只建一条", kept is not None
      and len(q5.list_reminders(OWNER, account=ACCOUNT)) == 1)
again2 = relay.deliver(cfg5, q5, FRIEND, OWNER, "该睡觉了", from_name="x",
                       account=ACCOUNT, relations=RELS, src_key="9001")
check("同一入站再交付一次被事务内查重挡下",
      again2 is None and len(q5.list_reminders(OWNER, account=ACCOUNT)) == 1)

section("模型说不准发给谁：先问一句，确认后再发")
q6 = support.make_queue("relay6.db")
cfg6 = make_cfg()
AMBIG = {"is_relay": True, "target": "", "candidates": ["alex"],
         "need_target": True, "content": "明天带材料", "confidence": 0.8,
         "tz": "", "reason": "不知道是哪一个"}
with patch.object(relay, "judge", return_value=AMBIG):
    items6 = relay.maybe_relay(cfg6, q6, FRIEND,
                              [relay_message(9006, "你帮我提醒一下他明天记得带材料", 100)],
                              ACCOUNT, relations=RELS)
check("没硬发，先追问", items6 and items6[0].get("pending"), str(items6))
check("追问里点了名字", "alex" in (items6[0].get("ask") or ""), str(items6[0].get("ask")))
check("还没建提醒", not q6.list_reminders(OWNER, account=ACCOUNT))
relay.maybe_relay(cfg6, q6, FRIEND, [relay_message(9007, "对", 101)], ACCOUNT, RELS)
check("核对未经回读时不能用「对」建单", not q6.list_reminders(account=ACCOUNT))
first_prompt = acknowledge_relay_prompt(q6, 102)
check("单人核对在微信正文里使用登记称呼",
      bool(first_prompt) and "alex" in first_prompt["text"] and "吗" in first_prompt["text"])
relay.maybe_relay(cfg6, q6, FRIEND, [relay_message(9008, "对", 102)], ACCOUNT, RELS)
check("微信序号未晚于问句的旧肯定不能建单", not q6.list_reminders(account=ACCOUNT))
answer6 = relay_message(9009, "对。", 103)
with patch.object(relay, "judge"), \
        patch.object(relay, "judge_clarify", return_value=decision("confirm")) as dc:
    ans = relay.maybe_relay(cfg6, q6, FRIEND, [answer6], ACCOUNT, RELS)
    relay.maybe_relay(cfg6, q6, FRIEND, [answer6], ACCOUNT, RELS)
check("答复判读只调一次模型，重放不再调", dc.call_count == 1, str(dc.call_count))
check("确认后建了单", ans and ans[0]["ok"], str(ans))
check("发给了 alex", ans and ans[0]["chat"] == OWNER, str(ans))
check("追问已结案", q6.get_relay_clarify(ACCOUNT, FRIEND) is None)
check("同一确认只建一条代传话", len(q6.list_reminders(account=ACCOUNT)) == 1)
with patch.object(relay, "judge", return_value=AMBIG):
    relay.maybe_relay(cfg6, q6, FRIEND,
                      [relay_message(9010, "帮我提醒一下他带伞", 104)], ACCOUNT, RELS)
check("结案后可以再次提出新委托", q6.get_relay_clarify(ACCOUNT, FRIEND) is not None)

section("每一轮只问一个人：被否掉就换下一个")
RELS2 = RELS + [{"name": "小李", "nickname": "小李", "relation": "朋友",
                 "chat": "wxid_li"}]
q8 = support.make_queue("relay8.db")
cfg8 = make_cfg()
AMBIG2 = {"is_relay": True, "target": "", "candidates": ["alex", "小李"],
          "need_target": True, "content": "明天带材料", "confidence": 0.8,
          "tz": "", "reason": ""}
with patch.object(relay, "judge", return_value=AMBIG2):
    relay.maybe_relay(cfg8, q8, FRIEND, [relay_message(9201, "帮我提醒一下他带材料", 200)],
                      ACCOUNT, relations=RELS2)
first_q = acknowledge_relay_prompt(q8, 201)
check("先只问概率最大的那个", bool(first_q) and "alex" in first_q["text"]
      and "小李" not in first_q["text"], str(first_q))
with patch.object(relay, "judge_clarify", return_value=decision("reject")):
    relay.maybe_relay(cfg8, q8, FRIEND, [relay_message(9202, "不是他", 202)], ACCOUNT, RELS2)
check("否掉之后不建单", not q8.list_reminders(account=ACCOUNT))
second_q = acknowledge_relay_prompt(q8, 203)
check("换下一个候选来问", bool(second_q) and "小李" in second_q["text"]
      and "alex" not in second_q["text"], str(second_q))
with patch.object(relay, "judge_clarify", return_value=decision("pick", "小李")):
    ans2 = relay.maybe_relay(cfg8, q8, FRIEND,
                             [relay_message(9204, "不是，发给小李", 204)], ACCOUNT, RELS2)
check("点名另一个人也能落单", ans2 and ans2[0]["ok"] and ans2[0]["chat"] == "wxid_li",
      str(ans2))

section("全否定且没有下一个候选：不建单，也不把否定当答案")
q8b = support.make_queue("relay8b.db")
with patch.object(relay, "judge", return_value=dict(AMBIG2, candidates=["alex"])):
    relay.maybe_relay(cfg8, q8b, FRIEND, [relay_message(9206, "帮我提醒一下他带材料", 210)],
                      ACCOUNT, relations=RELS2)
acknowledge_relay_prompt(q8b, 211)
with patch.object(relay, "judge_clarify", return_value=decision("reject")):
    ans3 = relay.maybe_relay(cfg8, q8b, FRIEND, [relay_message(9207, "两个都不是", 212)],
                             ACCOUNT, RELS2)
check("全否定 → 不建单", ans3 is None or all(x["ok"] is False for x in ans3), str(ans3))
check("全否定后没有安排任何提醒", not q8b.list_reminders(account=ACCOUNT))

section("可以改为其他已登记的联系人")
q_correct = support.make_queue("relay_correct_contact.db")
with patch.object(relay, "judge", return_value=AMBIG):
    relay.maybe_relay(cfg8, q_correct, FRIEND, [relay_message(9211, "帮我提醒他带材料", 220)],
                      ACCOUNT, RELS2)
acknowledge_relay_prompt(q_correct, 221)
with patch.object(relay, "judge_clarify", return_value=decision("pick", "小李")):
    corrected = relay.maybe_relay(cfg8, q_correct, FRIEND,
                                  [relay_message(9212, "不是alex，改发给小李", 222)],
                                  ACCOUNT, RELS2)
check("原本不在候选名单的联系人也可明确纠正",
      corrected and corrected[0]["ok"] and corrected[0]["chat"] == "wxid_li", str(corrected))
check("不会向被否定的人建单", not q_correct.list_reminders(OWNER, account=ACCOUNT))

section("引号和后置否定都不会误确认收件人")
for index, denial in enumerate(("别发给「alex」", "alex，别发给他")):
    q_deny = support.make_queue("relay_clarify_deny_%d.db" % index)
    with patch.object(relay, "judge", return_value=AMBIG):
        relay.maybe_relay(cfg8, q_deny, FRIEND,
                          [relay_message(9260, "帮我提醒他带材料", 330)], ACCOUNT, RELS2)
    acknowledge_relay_prompt(q_deny, 331)
    with patch.object(relay, "judge_clarify", return_value=decision("reject")):
        relay.maybe_relay(cfg8, q_deny, FRIEND, [relay_message(9261, denial, 332)],
                          ACCOUNT, RELS2)
    check("明确否定时不排代传：" + denial, not q_deny.list_reminders(account=ACCOUNT))
    retry = acknowledge_relay_prompt(q_deny, 333)
    check("下一句不再提议已否定的人：" + denial, bool(retry) and "alex" not in retry["text"])

section("待确认时的新委托独立判定")
q_new = support.make_queue("relay_clarify_new_request.db")
with patch.object(relay, "judge", return_value=AMBIG):
    relay.maybe_relay(cfg8, q_new, FRIEND,
                      [relay_message(9270, "帮我提醒他带材料", 340)], ACCOUNT, RELS2)
acknowledge_relay_prompt(q_new, 341)
new_request = "提醒小李明天带伞"
new_verdict = dict(AMBIG, target="小李", candidates=[], need_target=False, content="明天带伞")
with patch.object(relay, "judge", return_value=new_verdict) as new_judge, \
        patch.object(relay, "judge_clarify", return_value=decision("not_answer")):
    relay.maybe_relay(cfg8, q_new, FRIEND, [relay_message(9271, new_request, 342)], ACCOUNT, RELS2)
check("新委托送去模型独立判定",
      new_judge.call_count == 1 and new_judge.call_args.args[1] == new_request)
new_relays = q_new.list_reminders(account=ACCOUNT)
check("只保存新委托的原文和收件人",
      len(new_relays) == 1 and new_relays[0]["chat"] == "wxid_li" and new_relays[0]["content"] == "明天带伞")
check("旧待确认委托仍在等待自己的回答", q_new.get_relay_clarify(ACCOUNT, FRIEND) is not None)

section("三次都否掉仍未确认：结案、不建单、不再追问")
q_limit = support.make_queue("relay_clarify_limit.db")
with patch.object(relay, "judge", return_value=dict(AMBIG, candidates=["alex"])):
    relay.maybe_relay(cfg8, q_limit, FRIEND, [relay_message(9220, "帮我提醒他带材料", 240)],
                      ACCOUNT, RELS2)
for i in range(3):
    acknowledge_relay_prompt(q_limit, 241 + i * 2)
    if i == 0:
        q_limit = type(q_limit)(q_limit.db_path)
    answer = relay_message(9221 + i, "不是", 242 + i * 2)
    with patch.object(relay, "judge_clarify", return_value=decision("reject")):
        relay.maybe_relay(cfg8, q_limit, FRIEND, [answer], ACCOUNT, RELS2)
        relay.maybe_relay(cfg8, q_limit, FRIEND, [answer], ACCOUNT, RELS2)
check("重启和重复投递不会重置提问次数，三轮后结案",
      q_limit.get_relay_clarify(ACCOUNT, FRIEND) is None)
relay.maybe_relay(cfg8, q_limit, FRIEND, [relay_message(9224, "对", 248)], ACCOUNT, RELS2)
check("结案后短肯定不触发发送，也不再排第四条问句",
      not q_limit.list_reminders(account=ACCOUNT)
      and q_limit.next_outbound(kinds=("relay_clarify",), account=ACCOUNT) is None)

section("核对后关系表改绑：先重新核对，不按旧会话发")
q_rebound = support.make_queue("relay_clarify_rebound.db")
with patch.object(relay, "judge", return_value=AMBIG):
    relay.maybe_relay(cfg8, q_rebound, FRIEND, [relay_message(9230, "帮我提醒他带材料", 260)],
                      ACCOUNT, RELS2)
acknowledge_relay_prompt(q_rebound, 261)
rebound_relations = [{**r, "chat": STRANGER} if r["chat"] == OWNER else r for r in RELS2]
with patch.object(relay, "judge_clarify", return_value=decision("confirm")):
    relay.maybe_relay(cfg8, q_rebound, FRIEND, [relay_message(9231, "对", 262)],
                      ACCOUNT, rebound_relations)
check("不按旧会话建单，也不会发给新绑定的会话",
      not q_rebound.list_reminders(OWNER, account=ACCOUNT)
      and not q_rebound.list_reminders(STRANGER, account=ACCOUNT))
check("重新挂起核对，等新一次确认",
      q_rebound.get_relay_clarify(ACCOUNT, FRIEND) is not None)

section("发起人可以取消待确认委托并撤掉待发问句")
q_cancel = support.make_queue("relay_clarify_cancel.db")
with patch.object(relay, "judge", return_value=AMBIG):
    relay.maybe_relay(cfg8, q_cancel, FRIEND, [relay_message(9240, "帮我提醒他带材料", 270)],
                      ACCOUNT, RELS2)
with patch.object(relay, "judge_clarify", return_value=decision("cancel")):
    relay.maybe_relay(cfg8, q_cancel, FRIEND, [relay_message(9241, "不用发了", 271)],
                      ACCOUNT, RELS2)
check("取消后没有等待核对的委托和问句",
      q_cancel.get_relay_clarify(ACCOUNT, FRIEND) is None
      and q_cancel.next_outbound(account=ACCOUNT) is None)
relay.maybe_relay(cfg8, q_cancel, FRIEND, [relay_message(9242, "对", 272)], ACCOUNT, RELS2)
check("取消后的短肯定不能发出原委托", not q_cancel.list_reminders(account=ACCOUNT))

section("核对走发送线程，无需开启普通自动回复")
import reply_worker

q_sender = support.make_queue("relay_clarify_sender.db")
cfg_sender = make_cfg()
cfg_sender["agent"]["auto_reply"] = False
worker = reply_worker.Worker.__new__(reply_worker.Worker)
worker.acc = ACCOUNT
worker.cfg = cfg_sender
worker.q = q_sender
worker.db = SimpleNamespace(get_messages=lambda *args, **kwargs: [])
worker.brain = SimpleNamespace(mem=SimpleNamespace(add=lambda *args, **kwargs: None),
                               reload=lambda cfg: None)
worker._inflight_lock = threading.Lock()
worker._inflight = set()
q_sender.enqueue_inbound(ACCOUNT, FRIEND, 9250, int(time.time()), "文本",
                         "帮我提醒他带材料", sender=FRIEND, sort_seq=310)
with (patch.object(relay, "_relations", return_value=RELS2),
      patch.object(relay, "judge", return_value=dict(AMBIG, target="alex")),
      patch.object(reply_worker, "load_cfg", return_value=cfg_sender)):
    worker.process(FRIEND)
check("模型猜中一个候选但表示不确定时仍先核对",
      not q_sender.list_reminders(account=ACCOUNT))
sender_task = q_sender.next_outbound(account=ACCOUNT)
with (patch.object(relay, "_relations", return_value=RELS2),
      patch.object(reply_worker, "post",
                   return_value={"ok": True, "verified": True, "verified_seq": 311}) as post):
    sender_result = (worker.send_task(sender_task, sleep_delay=False, reload_cfg=False)
                     if sender_task else {})
check("仅开启代传话和定时提醒也能送出核对", sender_result.get("status") == "sent",
      str(sender_result))
check("实际提交的是发起人会话和核对专用正文",
      post.called and post.call_args.args[1]["chat"] == FRIEND
      and post.call_args.args[1]["source"] == "relay_clarify"
      and "alex" in post.call_args.args[1]["text"] and "wxid_" not in post.call_args.args[1]["text"])

section("同一会话内两类核对并存时，短肯定只确认最新问句")


def send_relay_question(q, sort_seq):
    with patch.object(relay, "judge", return_value=AMBIG):
        relay.maybe_relay(cfg_sender, q, FRIEND,
                          [relay_message(9280, "帮我提醒他带材料", sort_seq - 1)],
                          ACCOUNT, RELS2)
    acknowledge_relay_prompt(q, sort_seq)


def send_reminder_question(q, sort_seq):
    now = int(time.time())
    q.add_clarify(ACCOUNT, FRIEND, "自己取快递", now - 1, "", rule="single",
                  proposed_ts=now + 3600)
    reminder.enqueue_clarify_prompt(q, ACCOUNT, FRIEND)
    task = q.next_outbound(kinds=("reminder_clarify",), account=ACCOUNT)
    check("个人提醒核对已送达", bool(task)
          and q.bump_segments(task["id"], 1, complete=True, sent_after_seq=sort_seq))


for older, newer, expected in (
    (send_relay_question, send_reminder_question, (FRIEND, "自己取快递")),
    (send_reminder_question, send_relay_question, (OWNER, "明天带材料")),
):
    q_overlap = support.make_queue("relay_reminder_overlap_%s.db" % expected[0])
    older(q_overlap, 401)
    newer(q_overlap, 403)
    worker.q = q_overlap
    for seq in (404, 405):
        q_overlap.enqueue_inbound(ACCOUNT, FRIEND, seq, int(time.time()), "文本", "对",
                                  sender=FRIEND, sort_seq=seq)
        with (patch.object(relay, "_relations", return_value=RELS2),
              patch.object(relay, "judge_clarify", return_value=decision("confirm")),
              patch.object(reply_worker, "load_cfg", return_value=cfg_sender)):
            worker.process(FRIEND)
        actual = [(r["chat"], r["content"]) for r in q_overlap.list_reminders(account=ACCOUNT)]
        check("第 %d 条短肯定后只存在最新核对的安排：%s" % (seq - 403, expected[1]),
              actual == [expected], str(actual))

section("较新的普通问句也不能把短肯定变成对旧代传的授权")
q_ordinary = support.make_queue("relay_clarify_ordinary_question.db")
with patch.object(relay, "judge", return_value=AMBIG):
    relay.maybe_relay(cfg8, q_ordinary, FRIEND, [relay_message(9270, "帮我提醒他带材料", 500)],
                      ACCOUNT, RELS2)
acknowledge_relay_prompt(q_ordinary, 501)
ordinary = q_ordinary.create_outbound(ACCOUNT, FRIEND, "你现在已经到家了吗？")
q_ordinary.next_outbound(kinds=("reply",), account=ACCOUNT)
q_ordinary.bump_segments(ordinary, 1, complete=True, sent_after_seq=502)
with patch.object(relay, "judge_clarify", return_value=decision("confirm")):
    relay.maybe_relay(cfg8, q_ordinary, FRIEND, [relay_message(9271, "对", 503)], ACCOUNT, RELS2)
check("回复更新的普通问句不会给第三方安排发送",
      not q_ordinary.list_reminders(account=ACCOUNT))
with patch.object(relay, "judge_clarify", return_value=decision("pick", "alex")):
    confirmed = relay.maybe_relay(cfg8, q_ordinary, FRIEND,
                                  [relay_message(9272, "发给alex", 504)], ACCOUNT, RELS2)
check("明确说发给登记联系人仍可完成核对",
      confirmed and confirmed[0]["ok"] and confirmed[0]["chat"] == OWNER, confirmed)

section("拒绝的代传不会被当成自己的提醒")
q9 = support.make_queue("relay9.db")
cfg9 = make_cfg()
REFUSE = {"is_relay": True, "target": "路人", "candidates": [],
          "need_target": False, "content": "明天复诊", "confidence": 0.9,
          "tz": "", "reason": ""}
with patch.object(relay, "judge", return_value=REFUSE):
    items9 = relay.maybe_relay(cfg9, q9, FRIEND,
                              [{"id": 9301, "content": "帮我提醒他明天下午两点去复诊"}],
                              ACCOUNT, relations=RELS)
check("回复如实说发不了", items9 and not items9[0]["ok"], str(items9))
check("这条消息被占住，不会再进普通提醒",
      q9.has_action(ACCOUNT, FRIEND, "9301"))
check("没有给发起人建提醒",
      not q9.list_reminders(FRIEND, account=ACCOUNT))

section("关掉代传话：不调模型、不写待澄清")
q10 = support.make_queue("relay10.db")
cfg10 = make_cfg()
cfg10["proactive"]["relay"]["enabled"] = False
with patch.object(relay, "judge") as mock_judge:
    items10 = relay.maybe_relay(cfg10, q10, FRIEND,
                                [{"id": 9401, "content": "帮我提醒他带材料"}],
                                ACCOUNT, relations=RELS)
check("模型没被调用", not mock_judge.called)
check("没写待澄清", q10.get_relay_clarify(ACCOUNT, FRIEND) is None)
check("什么都没返回", items10 == [])

section("后置否定也算否定：小王和小李都不是")
q11 = support.make_queue("relay11.db")
cfg11 = make_cfg()
with patch.object(relay, "judge", return_value=AMBIG2):
    relay.maybe_relay(cfg11, q11, FRIEND,
                      [{"id": 9501, "content": "帮我提醒一下他带材料"}],
                      ACCOUNT, relations=RELS2)
acknowledge_relay_prompt(q11, 280)
ans5 = relay.maybe_relay(cfg11, q11, FRIEND,
                         [relay_message(9502, "小王和小李都不是", 281)],
                         ACCOUNT, relations=RELS2)
check("全否定 → 不建单", ans5 is None or all(not x["ok"] for x in ans5), str(ans5))
check("追问还挂着，下次还能回答",
      q11.get_relay_clarify(ACCOUNT, FRIEND) is not None)

section("模型不可用时代传不转成自提醒")
q12 = support.make_queue("relay12.db")
cfg12 = make_cfg()
with patch.object(relay, "judge", return_value=None):
    items12 = relay.maybe_relay(cfg12, q12, FRIEND,
                               [{"id": 9601, "content": "帮我提醒他明天下午两点去复诊"}],
                               ACCOUNT, relations=RELS)
check("模型挂了就占住这条消息", q12.has_action(ACCOUNT, FRIEND, "9601"))
check("没有给发起人建提醒", not q12.list_reminders(FRIEND, account=ACCOUNT))

section("候选里真有这两个人：全否定仍不建单")
RELS3 = RELS + [
    {"name": "小李", "nickname": "小李", "relation": "朋友", "chat": "wxid_li"},
    {"name": "小王", "nickname": "老王", "relation": "同事", "chat": "wxid_wang"},
]
q13 = support.make_queue("relay13.db")
cfg13 = make_cfg()
AMBIG3 = {"is_relay": True, "target": "", "candidates": ["小王", "小李"],
          "need_target": True, "content": "带材料", "confidence": 0.8,
          "tz": "", "reason": ""}
with patch.object(relay, "judge", return_value=AMBIG3):
    relay.maybe_relay(cfg13, q13, FRIEND,
                      [{"id": 9701, "content": "帮我提醒一下他带材料"}],
                      ACCOUNT, relations=RELS3)
acknowledge_relay_prompt(q13, 290)
ans13 = relay.maybe_relay(cfg13, q13, FRIEND,
                          [relay_message(9702, "小王和小李都不是", 291)],
                          ACCOUNT, relations=RELS3)
check("两个都否定 → 一个都不发", ans13 is None or all(not x["ok"] for x in ans13),
      str(ans13))
check("追问保留，可以再说一次", q13.get_relay_clarify(ACCOUNT, FRIEND) is not None)

section("点名未绑定的人：不建单也不转成自提醒")
q14 = support.make_queue("relay14.db")
cfg14 = make_cfg()
UNKNOWN = {"is_relay": True, "target": "小张", "candidates": [],
           "need_target": True, "content": "复诊", "confidence": 0.9,
           "tz": "", "reason": "不在关系网"}
with patch.object(relay, "judge", return_value=UNKNOWN):
    items14 = relay.maybe_relay(cfg14, q14, FRIEND,
                               [{"id": 9801, "content": "帮我提醒小张明天去复诊"}],
                               ACCOUNT, relations=RELS)
check("如实说发不了", items14 and not items14[0]["ok"], str(items14))
check("消息被占住", q14.has_action(ACCOUNT, FRIEND, "9801"))
check("没有给发起人建提醒", not q14.list_reminders(FRIEND, account=ACCOUNT))

section("预筛别把『叫外卖』当成代传")
check("我叫外卖了不触发模型", not relay.looks_like_relay("我叫外卖了", RELS))
check("提醒小张仍要判定", relay.looks_like_relay("帮我提醒小张明天复诊", RELS))
check("催他算代传", relay.looks_like_relay("帮我催一下他交作业", RELS))

section("闲聊里提到候选名字不算回答")
q15 = support.make_queue("relay15.db")
cfg15 = make_cfg()
with patch.object(relay, "judge", return_value=AMBIG3):
    relay.maybe_relay(cfg15, q15, FRIEND,
                      [{"id": 9901, "content": "帮我提醒一下他带材料"}],
                      ACCOUNT, relations=RELS3)
acknowledge_relay_prompt(q15, 300)
longtalk = "小李昨天跟我说他下周要去上海出差，你知道吗"
with patch.object(relay, "judge_clarify", return_value=decision("not_answer")):
    ans15 = relay.maybe_relay(cfg15, q15, FRIEND, [relay_message(9902, longtalk, 301)],
                              ACCOUNT, relations=RELS3)
check("长句不当作确认", not ans15, str(ans15))
check("追问还挂着", q15.get_relay_clarify(ACCOUNT, FRIEND) is not None)

section("代问一句：问句不再被当成反向话挡掉")
check("「问问组长」能进判定",
      relay.looks_like_relay("帮我问问组长，晚饭吃的什么", RELS))
check("带问号的委托也能进",
      relay.looks_like_relay("你问问她明天来不来？", RELS))
check("反问仍被挡", not relay.looks_like_relay("我不是提醒他了吗", RELS))
check("叫外卖不触发", not relay.looks_like_relay("我叫外卖了", RELS))

section("提醒族的口语说法都要能进判定")
for text, want in (
    ("提醒他明天带伞", True),
    ("让他明天带伞", True),
    ("叮嘱他吃药", True),
    ("嘱咐他早点睡", True),
    ("督促他写作业", True),
    ("别忘了提醒他吃药", True),
    ("别忘了给他带钥匙", True),
    ("提个醒：明天交材料", True),
    ("跟他说一下我晚点到", True),
    ("问问组长晚饭吃什么", True),
    ("我提醒你了", False),
    ("我记得你说过", False),
    ("让我想想", False),
    ("别让他去", False),
    ("我叫外卖了", False),
    ("你别忘了吃药", False),
):
    check("「%s」%s" % (text, "能进判定" if want else "不触发"),
          bool(relay.looks_like_relay(text, RELS)) == want)

section("代说：正文直出，不套「让我提醒你」")
q16 = support.make_queue("relay16.db")
cfg16 = make_cfg()
SAY = {"is_relay": True, "kind": "say", "target": "alex", "candidates": [],
       "need_target": False, "content": "晚饭吃的什么", "confidence": 0.9,
       "tz": "", "reason": ""}
with patch.object(relay, "judge", return_value=SAY):
    items16 = relay.maybe_relay(cfg16, q16, FRIEND,
                               [{"id": 10001, "content": "帮我问问组长，晚饭吃的什么"}],
                               ACCOUNT, relations=RELS)
check("建了单", items16 and items16[0]["ok"], str(items16))
rem16 = q16.get_reminder(items16[0]["rid"]) if items16 and items16[0]["ok"] else {}
check("标成代说", rem16.get("relay_verbatim") == 1, str(rem16.get("relay_verbatim")))
check("正文就是那句话",
      reminder.build_text(rem16, time.time()) == "晚饭吃的什么",
      reminder.build_text(rem16, time.time()) if rem16 else "—")

section("模型判不是委托：带使唤口气才提醒别答应")
q17 = support.make_queue("relay17.db")
cfg17 = make_cfg()
NO = {"is_relay": False, "kind": "remind", "target": "", "candidates": [],
      "need_target": False, "content": "", "confidence": 0.9, "tz": "",
      "reason": "反问"}
with patch.object(relay, "judge", return_value=NO):
    items17 = relay.maybe_relay(cfg17, q17, FRIEND,
                                [{"id": 10101, "content": "你告诉她了吗"}],
                                ACCOUNT, relations=RELS)
check("反问不注入「别答应」",
      not items17 or not items17[0].get("unhandled"), str(items17))
with patch.object(relay, "judge", return_value=NO):
    items18 = relay.maybe_relay(cfg17, q17, FRIEND,
                                [{"id": 10102, "content": "帮我问问她晚饭吃啥"}],
                                ACCOUNT, relations=RELS)
check("使唤口气但没听懂 → 提示别空口答应",
      items18 and items18[0].get("unhandled"), str(items18))
check("提示词里明说别答应", "别空口答应" in relay.hint_line(items18),
      relay.hint_line(items18))

section("周期代传话：每天叫他去上课")
q18 = support.make_queue("relay18.db")
cfg18 = make_cfg()
_nxt = (datetime.now() + timedelta(days=1)).replace(
    hour=7, minute=30, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
DAILY = {"is_relay": True, "kind": "remind", "target": "alex", "candidates": [],
         "need_target": False, "content": "去上课", "rule": "daily",
         "next_local": _nxt, "confidence": 0.9, "tz": "", "reason": ""}
with patch.object(relay, "judge", return_value=DAILY):
    items18 = relay.maybe_relay(cfg18, q18, FRIEND,
                               [{"id": 11001, "content": "你记得每天叫他去上课"}],
                               ACCOUNT, relations=RELS)
check("建了单", items18 and items18[0]["ok"], str(items18))
rem18 = q18.get_reminder(items18[0]["rid"]) if items18 and items18[0]["ok"] else {}
check("标成每天重复", rem18.get("rule") == "daily", str(rem18.get("rule")))
check("首次是明天早上七点半",
      rem18 and datetime.fromtimestamp(rem18["next_ts"]).strftime("%H:%M") == "07:30",
      rem18 and datetime.fromtimestamp(rem18["next_ts"]))

section("一句话取消代传话：发起人和被传话人都行")


def _relay_daily(q, content):
    first = int(time.time()) + 3600
    return q.add_reminder(ACCOUNT, "wxid_li", content, "daily", first,
                          anchor_ts=first, confirm_mode="relay",
                          relay_from="alex", relay_from_chat=FRIEND)


q19 = support.make_queue("relay19.db")
rid19 = _relay_daily(q19, "去上课")
hit19, n19 = reminder.maybe_cancel(q19, FRIEND, "别每天叫他去上课了",
                                   account=ACCOUNT)
check("发起人能取消", hit19 and n19 == 1
      and q19.get_reminder(rid19)["status"] == "cancelled",
      str((hit19, n19)))

q20 = support.make_queue("relay20.db")
rid20 = _relay_daily(q20, "去上课")
hit20, n20 = reminder.maybe_cancel(q20, "wxid_li", "不用再叫我去上课了",
                                   account=ACCOUNT)
check("被传话人能取消", hit20 and n20 == 1
      and q20.get_reminder(rid20)["status"] == "cancelled",
      str((hit20, n20)))

q21 = support.make_queue("relay21.db")
rid21 = _relay_daily(q21, "去上课")
hit21, n21 = reminder.maybe_cancel(q21, FRIEND, "明天我去学校", account=ACCOUNT)
check("无关消息不动它", not hit21
      and q21.get_reminder(rid21)["status"] == "active", str((hit21, n21)))

q22 = support.make_queue("relay22.db")
rid22 = _relay_daily(q22, "去上课")
hit22, n22 = reminder.maybe_cancel(q22, FRIEND, "别让他提醒我了", account=ACCOUNT)
check("没说具体事也能取消他托的那些", hit22 and n22 == 1
      and q22.get_reminder(rid22)["status"] == "cancelled", str((hit22, n22)))

section("代传话回传：问了要回")


def confirm_forwarded_questions(q, cfg):
    reminder.fire_due(q, cfg, now=time.time() + 1)
    while task := q.next_outbound(account=ACCOUNT):
        q.bump_segments(task["id"], len(json.loads(task["segments"])),
                        complete=True, sent_after_seq=100)


q23 = support.make_queue("relay23.db")
cfg23 = make_cfg()
ASK = {"is_relay": True, "kind": "say", "target": "alex", "candidates": [],
       "need_target": False, "content": "今天吃的什么", "rule": "single",
       "next_local": "", "confidence": 0.9, "tz": "", "reason": ""}
with patch.object(relay, "judge", return_value=ASK):
    relay.maybe_relay(cfg23, q23, FRIEND,
                      [{"id": 12001, "content": "帮我问问组长今天吃的什么"}],
                      ACCOUNT, relations=RELS)
check("提问尚未发出时不收对方图片", q23.relay_watch_for(ACCOUNT, OWNER) is None)
confirm_forwarded_questions(q23, cfg23)
watch = q23.relay_watch_for(ACCOUNT, OWNER)
check("登记了等待回传", watch and watch["status"] == "waiting", str(watch))
check("较旧的序号不能进入回传", not q23.add_relay_reply(
    watch["id"], "较旧的独立会话记录", message_id=1, sort_seq=99))
check("较新的消息进入回传", q23.add_relay_reply(
    watch["id"], "吃了汉堡和西兰花", message_id=2, sort_seq=101))
check("同一条入站重试不重复计数", not q23.add_relay_reply(
    watch["id"], "吃了汉堡和西兰花", message_id=2, sort_seq=101))
q23.add_relay_media(watch["id"], md5="0" * 32, message_id=3, sort_seq=102)
with q23._conn() as c:
    c.execute("UPDATE relay_watch SET started_ts=? WHERE id=?",
              (int(time.time()) - 400, watch["id"]))
with patch.object(relay, "compose_back", return_value="组长说吃了汉堡和西兰花。"):
    n23 = relay.back_tick(q23, cfg23)
check("汇总成一条回传", n23 == 1, str(n23))
back23 = [r for r in q23.list_reminders(account=ACCOUNT)
          if r["confirm_mode"] == "relay_back"]
check("回传给发起人", back23 and back23[0]["chat"] == FRIEND, str(back23))
check("文案是模型组织的那句",
      back23 and back23[0]["content"] == "组长说吃了汉堡和西兰花。",
      str(back23 and back23[0]["content"]))
check("封口后再次调度不会重复建单", relay.back_tick(q23, cfg23) == 0)

section("混存旧账号的队列：其他账号收的答复不能回传")
q_cross = support.make_queue("relay_cross.db")
old_watch = q_cross.add_relay_watch("wxid_other", FRIEND, OWNER, "几点到",
                                   asked_ts=int(time.time()) - 500)
q_cross.add_relay_reply(old_watch, "六点到", ts=int(time.time()) - 400)
check("账号过滤覆盖已经在收集的代问", not q_cross.due_relay_watch(account=ACCOUNT))
check("台账无当前账号的回传", relay.back_tick(q_cross, cfg23) == 0
      and not q_cross.list_reminders(account=ACCOUNT))

section("同一个收件人不能同时开两个代问")
q_busy = support.make_queue("relay_busy.db")
other = "wxid_other_requester"
busy_rels = RELS + [{"name": "小李", "chat": other}]
question1 = relay.deliver(cfg23, q_busy, FRIEND, OWNER, "什么时候到",
                          relations=busy_rels, watch_back=True, src_key="401")
question2 = relay.deliver(cfg23, q_busy, other, OWNER, "晚饭吃什么",
                          relations=busy_rels, watch_back=True, src_key="402")
check("已有待发送的代问就拒绝另一个", bool(question1) and question2 is None)
check("占住被拒绝的原消息，避免落入个人提醒", q_busy.has_action(ACCOUNT, other, "402"))
confirm_forwarded_questions(q_busy, cfg23)
with patch.object(relay, "judge", return_value=ASK):
    result_busy = relay.maybe_relay(cfg23, q_busy, other,
                                    [{"id": 403, "content": "帮我问alex晚饭吃什么"}],
                                    ACCOUNT, relations=busy_rels)
watch_busy = q_busy.relay_watch_for(ACCOUNT, OWNER)
check("第一个问题送达后也保持单一收件人回传",
      bool(result_busy) and not result_busy[0]["ok"] and watch_busy["from_chat"] == FRIEND
      and len(q_busy.list_reminders(OWNER, account=ACCOUNT)) == 1)
check("正常入口的拒绝也占住原消息", q_busy.has_action(ACCOUNT, other, "403"))
q_busy.set_relay_watch(watch_busy["id"], "answered")
check("前一个问题结案后可以重新发问", bool(relay.deliver(
    cfg23, q_busy, other, OWNER, "晚饭吃什么", relations=busy_rels, watch_back=True,
    src_key="404")))

section("60 分钟没回：如实告诉他")
q24 = support.make_queue("relay24.db")
with patch.object(relay, "judge", return_value=ASK):
    relay.maybe_relay(cfg23, q24, FRIEND,
                      [{"id": 13001, "content": "帮我问问组长今天吃的什么"}],
                      ACCOUNT, relations=RELS)
confirm_forwarded_questions(q24, cfg23)
with q24._conn() as c:
    c.execute("UPDATE relay_watch SET asked_ts=? WHERE id=1",
              (int(time.time()) - 3700,))
relay.back_tick(q24, cfg23)
back24 = [r for r in q24.list_reminders(account=ACCOUNT)
          if r["confirm_mode"] == "relay_back"]
check("没回时也回一条", len(back24) == 1, str(back24))
check("内容如实说没回", back24 and "没回" in back24[0]["content"],
      str(back24 and back24[0]["content"]))

section("一句话托了好几个人：都要发到")
LI = "wxid_li"


def _watch_row(q, wid):
    with q._conn() as c:
        row = c.execute("SELECT * FROM relay_watch WHERE id=?", (int(wid),)).fetchone()
    return dict(row) if row else None

RELS3 = RELS + [{"name": "小李", "nickname": "小李", "relation": "同学", "chat": LI}]


class _FakeLlm:
    """替身：只回一段 JSON，用来验证 judge 的解析与 prompt。"""

    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    def __call__(self, msgs, cfg=None, **kw):
        self.prompts.append(msgs[0]["content"])
        return {"ok": True, "text": self.payload}


_llm = __import__("llm")
_orig_chat = _llm.chat
try:
    fake = _FakeLlm('{"is_relay":true,"kind":"say","target":"alex",'
                    '"targets":["alex","小李"],"content":"明天下午六点半到操场打球",'
                    '"confidence":0.9,"reason":""}')
    _llm.chat = fake
    info = relay.judge(make_cfg(), "帮我跟alex和小李说一声，明天下午六点半到操场打球",
                       RELS3, requester="联系人A")
finally:
    _llm.chat = _orig_chat
check("模型给了 targets 就原样收下",
      info and info["targets"] == ["alex", "小李"], str(info))
check("target 兜底成第一个",
      info and info["target"] == "alex", str(info and info["target"]))
check("prompt 里有「同时托好几个人」这条",
      fake.prompts and "targets" in fake.prompts[0], "")

try:
    fake2 = _FakeLlm('{"is_relay":true,"kind":"say","target":"小李",'
                     '"content":"问问几点到","confidence":0.9}')
    _llm.chat = fake2
    info2 = relay.judge(make_cfg(), "帮我问问小李几点到", RELS3, requester="联系人A")
finally:
    _llm.chat = _orig_chat
check("只给 target 的老输出也能用",
      info2 and info2["targets"] == ["小李"], str(info2))

q25 = support.make_queue("relay25.db")
cfg25 = make_cfg()
MULTI = {"is_relay": True, "kind": "say", "target": "alex",
         "targets": ["alex", "小李"], "candidates": [], "need_target": False,
         "content": "明天下午六点半到操场打球", "rule": "single",
         "next_local": "", "confidence": 0.9, "tz": "", "reason": ""}
with patch.object(relay, "judge", return_value=MULTI):
    items25 = relay.maybe_relay(
        cfg25, q25, FRIEND,
        [{"id": 14001, "content": "帮我跟alex和小李说一声，明天下午六点半到操场打球"}],
        ACCOUNT, relations=RELS3)
oks = [it for it in items25 if it.get("ok")]
check("两个人都建了单", len(oks) == 2, str(items25))
check("分别发到两个人的会话",
      q25.list_reminders(OWNER, account=ACCOUNT)
      and q25.list_reminders(LI, account=ACCOUNT),
      str([(r["chat"], r["confirm_mode"]) for r in q25.list_reminders(account=ACCOUNT)]))
check("正文是同一句",
      q25.list_reminders(LI, account=ACCOUNT)[0]["content"] == "明天下午六点半到操场打球",
      str(q25.list_reminders(LI, account=ACCOUNT)[0]["content"]))
confirm_forwarded_questions(q25, cfg25)
w25a = q25.relay_watch_for(ACCOUNT, OWNER)
w25b = q25.relay_watch_for(ACCOUNT, LI)
check("两个人都登记了回传", bool(w25a and w25b), str((w25a, w25b)))
check("归到同一组", w25a and w25b and w25a["batch"] == w25b["batch"]
      and bool(w25a["batch"]), str((w25a and w25a["batch"], w25b and w25b["batch"])))
check("记下了被问的人叫什么",
      w25b and w25b["target_name"] == "小李", str(w25b and w25b["target_name"]))
check("回复提示里两个人都提到",
      "alex" in relay.hint_line(items25) and "小李" in relay.hint_line(items25),
      relay.hint_line(items25))

q26 = support.make_queue("relay26.db")
with patch.object(relay, "judge", return_value=dict(MULTI, targets=["alex", "老妈"])):
    items26 = relay.maybe_relay(
        cfg25, q26, FRIEND, [{"id": 14002, "content": "帮我问问alex和老妈"}],
        ACCOUNT, relations=RELS3)
check("能发的照发", [it for it in items26 if it.get("ok")]
      and q26.list_reminders(OWNER, account=ACCOUNT), str(items26))
check("发不了的那个如实说",
      any(not it.get("ok") and "没绑定会话" in (it.get("reason") or "")
          for it in items26), str(items26))
check("没给「老妈」硬造提醒",
      not [r for r in q26.list_reminders(account=ACCOUNT) if not r["chat"]],
      str(q26.list_reminders(account=ACCOUNT)))

section("多人回传：等人齐了合成一句")
for w in (w25a, w25b):
    q25.add_relay_reply(w["id"], "六点到" if w["target_chat"] == OWNER else "六点半到",
                        sort_seq=101)
with q25._conn() as c:
    c.execute("UPDATE relay_watch SET started_ts=? WHERE batch=?",
              (int(time.time()) - 400, w25a["batch"]))
with patch.object(relay, "compose_back",
                  return_value="alex 说六点到，小李说六点半到。"):
    n25 = relay.back_tick(q25, cfg25)
back25 = [r for r in q25.list_reminders(FRIEND, account=ACCOUNT)
          if r["confirm_mode"] == "relay_back"]
check("两个人的回话一起回，只回一条", n25 == 1 and len(back25) == 1,
      str((n25, len(back25))))
check("回传的是合并后的那句",
      back25 and back25[0]["content"] == "alex 说六点到，小李说六点半到。",
      str(back25 and back25[0]["content"]))
check("两条回传一起结案",
      [_watch_row(q25, w["id"])["status"] for w in (w25a, w25b)]
      == ["answered", "answered"],
      str([_watch_row(q25, w["id"])["status"] for w in (w25a, w25b)]))

q27 = support.make_queue("relay27.db")
with patch.object(relay, "judge", return_value=dict(MULTI, targets=["alex", "小李"])):
    relay.maybe_relay(cfg25, q27, FRIEND,
                      [{"id": 15001, "content": "帮我问问alex和小李几点到"}],
                      ACCOUNT, relations=RELS3)
confirm_forwarded_questions(q27, cfg25)
wa = q27.relay_watch_for(ACCOUNT, OWNER)
wb = q27.relay_watch_for(ACCOUNT, LI)
for w in (wa, wb):
    q27.add_relay_reply(w["id"], "六点到", sort_seq=101)
with q27._conn() as c:
    # 只有 alex 到了汇总时间，小李还在等：这一轮先别回，别拆成两条
    c.execute("UPDATE relay_watch SET started_ts=? WHERE id=?",
              (int(time.time()) - 400, wa["id"]))
    c.execute("UPDATE relay_watch SET asked_ts=?, status='waiting' WHERE id=?",
              (int(time.time()), wb["id"]))
with patch.object(relay, "compose_back", return_value="alex 说六点到。"):
    n27a = relay.back_tick(q27, cfg25)
check("人没齐先不发", n27a == 0, str(n27a))
with q27._conn() as c:
    c.execute("UPDATE relay_watch SET asked_ts=? WHERE id=?",
              (int(time.time()) - 3700, wb["id"]))
with patch.object(relay, "compose_back", return_value="alex 说六点到，小李没回。"):
    n27b = relay.back_tick(q27, cfg25)
back27 = [r for r in q27.list_reminders(FRIEND, account=ACCOUNT)
          if r["confirm_mode"] == "relay_back"]
check("另一个超时后一起回一条", n27b == 1 and len(back27) == 1,
      str((n27b, len(back27))))
check("有人没回也在同一句里说清",
      back27 and back27[0]["content"] == "alex 说六点到，小李没回。",
      str(back27 and back27[0]["content"]))

section("模型把「A和B」当成一个名字：拆开再发")
q28 = support.make_queue("relay28.db")
ONESTR = dict(MULTI, target="alex和 小李", targets=[], content="明天下午六点半到操场打球")
with patch.object(relay, "judge", return_value=ONESTR):
    items28 = relay.maybe_relay(
        cfg25, q28, FRIEND, [{"id": 16001, "content": "帮我跟alex和小李说一声"}],
        ACCOUNT, relations=RELS3)
check("拆开后两个人都发到了", len([it for it in items28 if it.get("ok")]) == 2,
      str(items28))
check("两个人各一条提醒",
      q28.list_reminders(OWNER, account=ACCOUNT)
      and q28.list_reminders(LI, account=ACCOUNT), str(items28))

q29 = support.make_queue("relay29.db")
with patch.object(relay, "judge",
                  return_value=dict(MULTI, target="路人甲和 小李", targets=[])):
    items29 = relay.maybe_relay(
        cfg25, q29, FRIEND, [{"id": 17001, "content": "帮我跟路人甲和小李说一声"}],
        ACCOUNT, relations=RELS3)
check("拆不全就整句当一个人，不硬拆",
      len(items29) == 1 and not items29[0].get("ok"), str(items29))

section("「他们」要改成对收件人说的「你们」")
check("两个人：他们 → 你们", relay._fix_addressee("他们几点到？", 2) == "你们几点到？")
check("两个人：她们 → 你们", relay._fix_addressee("她们几点到？", 2) == "你们几点到？")
check("一个人：你们 → 你", relay._fix_addressee("你们几点到？", 1) == "你几点到？")
check("一个人：本来就对", relay._fix_addressee("你几点到？", 1) == "你几点到？")
check("不相干的句子不动", relay._fix_addressee("明天下午六点半到操场打球", 2)
      == "明天下午六点半到操场打球")

q30 = support.make_queue("relay30.db")
with patch.object(relay, "judge", return_value=dict(MULTI, content="他们几点到？")):
    relay.maybe_relay(cfg25, q30, FRIEND,
                      [{"id": 18001, "content": "帮我问问alex和小李，他们几点到？"}],
                      ACCOUNT, relations=RELS3)
check("发给两个人都成了「你们」",
      q30.list_reminders(OWNER, account=ACCOUNT)[0]["content"] == "你们几点到？"
      and q30.list_reminders(LI, account=ACCOUNT)[0]["content"] == "你们几点到？",
      str([r["content"] for r in q30.list_reminders(account=ACCOUNT
                                                   ) if r["confirm_mode"] == "relay"]))

section("关系称呼也能当收件人：你哥哥 → 关系表里的 relation")
check("「你哥哥」按 relation 字段解析到主人",
      relay.target_of(cfg, "你哥哥", RELS) == (OWNER, ""))
check("「哥哥呀」归一到同一个人", relay.target_of(cfg, "哥哥呀", RELS) == (OWNER, ""))
check("「你的哥哥」也对", relay.target_of(cfg, "你的哥哥", RELS) == (OWNER, ""))
check("不认识的称呼不瞎猜", relay.target_of(cfg, "你二舅", RELS)[0] == "")

EMPTY_TARGET = {"is_relay": True, "target": "", "candidates": [],
                "need_target": True, "content": "问声好", "confidence": 0.8,
                "tz": "", "reason": ""}
q_rel = support.make_queue("relay_relation_word.db")
with patch.object(relay, "judge", return_value=EMPTY_TARGET):
    first_rel = relay.maybe_relay(cfg, q_rel, FRIEND,
                                  [relay_message(9400, "你替我向他说一声早", 600)],
                                  ACCOUNT, relations=RELS)
check("模型说不出是谁时就追问", first_rel and first_rel[0].get("pending"), str(first_rel))
rel_prompt = acknowledge_relay_prompt(q_rel, 601)
check("追问里给了可选的名字，不是干巴巴一句「你要发给谁」",
      bool(rel_prompt) and "alex" in rel_prompt["text"], str(rel_prompt))
with patch.object(relay, "judge_clarify", return_value=decision("pick", "你哥哥")):
    rel_answer = relay.maybe_relay(cfg, q_rel, FRIEND,
                                   [relay_message(9401, "你哥哥", 602)], ACCOUNT, RELS)
check("用关系称呼答复也能建单", rel_answer and rel_answer[0]["ok"]
      and rel_answer[0]["chat"] == OWNER, str(rel_answer))

section("「没毛病」「可以」这类肯定也算数——只由模型判，不再靠词表")
q_yes = support.make_queue("relay_yes_words.db")
with patch.object(relay, "judge", return_value=EMPTY_TARGET):
    relay.maybe_relay(cfg, q_yes, FRIEND,
                      [relay_message(9410, "你替我向他说一声早", 610)], ACCOUNT, RELS)
acknowledge_relay_prompt(q_yes, 611)
for word in ("没毛病", "可以", "嗯嗯", "好的", "ok"):
    q_one = support.make_queue("relay_yes_%d.db" % abs(hash(word)))
    with patch.object(relay, "judge", return_value=EMPTY_TARGET):
        relay.maybe_relay(cfg, q_one, FRIEND,
                          [relay_message(9411, "你替我向他说一声早", 620)], ACCOUNT, RELS)
    acknowledge_relay_prompt(q_one, 621)
    with patch.object(relay, "judge_clarify", return_value=decision("confirm")):
        got = relay.maybe_relay(cfg, q_one, FRIEND,
                                [relay_message(9412, word, 622)], ACCOUNT, RELS)
    check("肯定说法「%s」能落单" % word,
          bool(got) and got[0].get("ok") and got[0]["chat"] == OWNER, str(got))

section("被问的人让橙子带话回去：反托，不再问「你要发给谁」")
q_back = support.make_queue("relay_back.db")
q_back.add_relay_watch(ACCOUNT, OWNER, FRIEND, "早", from_name="alex",
                       target_name="联系人A", asked_ts=int(time.time()) - 60)
BACK_MODEL = {"intent": "relay_back", "is_relay": True, "kind": "say",
              "target": "alex", "content": "联系人A让我跟你说：她向你问声不好",
              "confidence": 0.9}
with patch.object(relay, "judge", return_value=BACK_MODEL):
    back = relay.maybe_relay(cfg, q_back, FRIEND,
                             [relay_message(9500, "你代我向他问声不好", 700)],
                             ACCOUNT, relations=RELS)
check("反托不建追问", q_back.get_relay_clarify(ACCOUNT, FRIEND) is None)
check("反托直接带给了发起人", back and back[0].get("back") and back[0]["ok"]
      and back[0]["chat"] == OWNER, str(back))
back_rem = q_back.list_reminders(OWNER, account=ACCOUNT)
check("发起人那边有一条代说", len(back_rem) == 1
      and back_rem[0]["confirm_mode"] == "relay"
      and "问声不好" in (back_rem[0]["content"] or ""), str(back_rem))
check("反托不另开回传（避免两人来回传）",
      q_back.relay_watch_for(ACCOUNT, OWNER) is None)
check("同一次委托只反托一次",
      bool(q_back.relay_watch_for(ACCOUNT, FRIEND).get("back_ts")))

with patch.object(relay, "judge", return_value=BACK_MODEL):
    back_again = relay.maybe_relay(cfg, q_back, FRIEND,
                                   [relay_message(9501, "你替我向他道个歉", 701)],
                                   ACCOUNT, relations=RELS)
check("第二次反托被挡下并如实回执",
      back_again and back_again[0].get("receipt") and not back_again[0].get("ok"),
      str(back_again))
check("没有发出第二条", len(q_back.list_reminders(OWNER, account=ACCOUNT)) == 1)

section("反托只认模型判定：无关消息不会被当成带话")
q_noise = support.make_queue("relay_back_noise.db")
q_noise.add_relay_watch(ACCOUNT, OWNER, FRIEND, "早", from_name="alex",
                        asked_ts=int(time.time()) - 60)
with patch.object(relay, "judge", return_value={"is_relay": False, "intent": "none",
                                                "content": ""}):
    noise = relay.maybe_relay(cfg, q_noise, FRIEND,
                              [relay_message(9510, "帮我检查一下这道数学题", 710)],
                              ACCOUNT, relations=RELS)
check("闲聊不会被兜底成反托", not noise, str(noise))
check("发起人没收到无关消息",
      not q_noise.list_reminders(OWNER, account=ACCOUNT))

section("反托不看旧消息：早于提问送达的不算")
q_early = support.make_queue("relay_back_early.db")
q_early.add_relay_watch(ACCOUNT, OWNER, FRIEND, "早", from_name="alex")
old_ts = int(time.time()) - 300
with patch.object(relay, "judge", return_value=BACK_MODEL):
    early = relay.maybe_relay(cfg, q_early, FRIEND,
                              [{"id": 9511, "content": "你代我向他问声不好",
                                "msg_type": "文本", "msg_ts": old_ts, "sort_seq": 1}],
                              ACCOUNT, relations=RELS)
check("提问之前的旧消息不触发反托", not early, str(early))
check("发起人没收到这条旧消息",
      not q_early.list_reminders(OWNER, account=ACCOUNT))

section("模型不可用：宁可不带，也不乱带")
q_back2 = support.make_queue("relay_back_fallback.db")
q_back2.add_relay_watch(ACCOUNT, OWNER, FRIEND, "早", from_name="alex",
                        asked_ts=int(time.time()) - 60)
with patch.object(relay, "judge", return_value=None):
    fb = relay.maybe_relay(cfg, q_back2, FRIEND,
                           [relay_message(9512, "你代我向他问声不好", 711)],
                           ACCOUNT, relations=RELS)
check("模型不可用时不凭字面建反托", not fb, str(fb))
check("发起人什么都没收到",
      not q_back2.list_reminders(OWNER, account=ACCOUNT))

section("反托不抢点名了别人的委托")
q_back3 = support.make_queue("relay_back_named.db")
q_back3.add_relay_watch(ACCOUNT, OWNER, FRIEND, "早", from_name="alex",
                        asked_ts=int(time.time()) - 60)
NAMED = {"is_relay": True, "kind": "say", "target": "小李", "targets": ["小李"],
         "content": "生日快乐", "confidence": 0.9}
with patch.object(relay, "judge", return_value=NAMED):
    named = relay.maybe_relay(cfg25, q_back3, FRIEND,
                              [relay_message(9530, "你帮我跟小李说一声生日快乐", 730)],
                              ACCOUNT, relations=RELS3)
check("点名了别人就按别人发", named and named[0]["ok"] and named[0]["chat"] == LI,
      str(named))
check("没有误发给发起人", not q_back3.list_reminders(OWNER, account=ACCOUNT))

section("「他」要按上下文指人：有在途代问时指发起人")
check("没有上下文：他 = 主人",
      relay.target_of(cfg, "他", RELS)[0] == OWNER)
check("有上下文：他 = 当初托事的人",
      relay.target_of(cfg, "他", RELS, context={"counterpart": "wxid_li"}) == ("wxid_li", ""))

q_ctx = support.make_queue("relay_context.db")
q_ctx.add_relay_watch(ACCOUNT, LI, FRIEND, "几点到", from_name="小李")
with patch.object(relay, "judge", return_value=EMPTY_TARGET):
    relay.maybe_relay(cfg25, q_ctx, FRIEND,
                      [relay_message(9520, "提醒他明天带材料", 720)],
                      ACCOUNT, relations=RELS3)
ctx_prompt = acknowledge_relay_prompt(q_ctx, 721)
check("还在追问收件人", bool(ctx_prompt))
with patch.object(relay, "judge_clarify", return_value=decision("pick", "他")):
    ctx_answer = relay.maybe_relay(cfg25, q_ctx, FRIEND,
                                   [relay_message(9521, "他", 722)], ACCOUNT, RELS3)
check("答「他」时发给发起人而不是主人", ctx_answer and ctx_answer[0]["ok"]
      and ctx_answer[0]["chat"] == LI, str(ctx_answer))

section("追问和回执先让模型说人话，但事实一条都不能改")
cfg_p = make_cfg()
cfg_p["proactive"]["relay"]["polish"] = True

q_p = support.make_queue("relay_polish.db")
with patch.object(relay, "judge", return_value=EMPTY_TARGET):
    relay.maybe_relay(cfg_p, q_p, FRIEND,
                      [relay_message(9600, "你替我向他说一声早", 800)], ACCOUNT, RELS)
plain_task = q_p.next_outbound(account=ACCOUNT)
check("追问先按模板排出去",
      bool(plain_task) and "你要发送的人是" in plain_task["text"], str(plain_task))
nice = {"ok": True, "text": "这话到底带给谁呀？回 alex 就行，或者说「对」～"}
with patch("llm.chat", return_value=nice):
    polished = relay.polish_clarify_task(q_p, cfg_p, plain_task)
check("改写后换成橙子口吻", "～" in polished["text"] and "alex" in polished["text"],
      polished["text"])
rec_p = q_p.get_relay_clarify_by_id(plain_task["ref_id"])
check("发出去的那句同步成改写版", rec_p["ask"] == polished["text"], str(rec_p.get("ask")))
check("判重用的模板没被动", rec_p["ask_core"] == plain_task["text"],
      str(rec_p.get("ask_core")))
with patch.object(relay, "_relations", return_value=RELS):
    block_reason = relay.clarify_send_block_reason(q_p, polished)
check("发送前校验仍认这条问句", block_reason is None, str(block_reason))

q_p2 = support.make_queue("relay_polish_bad.db")
with patch.object(relay, "judge", return_value=EMPTY_TARGET):
    relay.maybe_relay(cfg_p, q_p2, FRIEND,
                      [relay_message(9601, "你替我向他说一声早", 810)], ACCOUNT, RELS)
plain2 = q_p2.next_outbound(account=ACCOUNT)
with patch("llm.chat", return_value={"ok": True, "text": "这话到底带给谁呀？"}):
    kept = relay.polish_clarify_task(q_p2, cfg_p, plain2)
check("改写丢了候选名字就退回原句", kept["text"] == plain2["text"], kept["text"])
check("退回时没有把问句记录改花",
      q_p2.get_relay_clarify_by_id(plain2["ref_id"])["ask"] == plain2["text"])

receipt = "这次没有发送。可以重新发起委托。"
with patch("llm.chat", return_value={"ok": True, "text": "行吧，这次没发出去，再喊我一遍"}):
    check("回执可以说得像聊天",
          relay.polish_receipt(cfg_p, receipt) == "行吧，这次没发出去，再喊我一遍")
with patch("llm.chat", return_value={"ok": True, "text": "好呀，下次一定帮你带到"}):
    check("回执丢了「没发」就退回原句",
          relay.polish_receipt(cfg_p, receipt) == receipt)
with patch("llm.chat", return_value={"ok": False, "error": "超时"}):
    check("模型不可用就原样发", relay.polish_receipt(cfg_p, receipt) == receipt)
with patch("llm.chat", return_value={"ok": True,
                                     "text": "已经给 alex 带到了，别的不发"}):
    check("回执里点到的人名必须留着",
          "alex" in relay.polish_receipt(cfg_p, "已安排给alex转达，其余没有发送。"))
check("关掉开关就完全不调模型",
      relay.polish_text(make_cfg(), "这次没有发送。") == "这次没有发送。")

section("答复里点名了一个关系表外的人：说清楚，别装作没听见")
q_miss = support.make_queue("relay_clarify_missing.db")
with patch.object(relay, "judge", return_value=AMBIG):
    relay.maybe_relay(cfg8, q_miss, FRIEND, [relay_message(9700, "帮我提醒他带材料", 900)],
                      ACCOUNT, RELS2)
acknowledge_relay_prompt(q_miss, 901)
with patch.object(relay, "judge_clarify", return_value=decision("pick", "小王")):
    missed = relay.maybe_relay(cfg8, q_miss, FRIEND,
                               [relay_message(9701, "不是alex，是小王", 902)], ACCOUNT, RELS2)
check("没有硬发给任何人", not q_miss.list_reminders(account=ACCOUNT))
check("把发不了的原因说清楚",
      bool(missed) and "小王" in (missed[0].get("ask") or "")
      and "关系表" in (missed[0].get("ask") or ""), str(missed))
check("追问没结案，还能接着说发给谁",
      q_miss.get_relay_clarify(ACCOUNT, FRIEND) is not None)

section("委托里已经点了名：不再追问，直接发")
q_named = support.make_queue("relay_named_target.db")
NAMED_JUDGE = {"is_relay": True, "kind": "say", "target": "alex",
               "targets": ["alex"], "candidates": [], "need_target": False,
               "content": "问声好", "confidence": 0.95, "tz": "", "reason": ""}
with patch.object(relay, "judge", return_value=NAMED_JUDGE):
    named_out = relay.maybe_relay(cfg, q_named, FRIEND,
                                  [relay_message(9710, "帮我跟alex问声好", 910)],
                                  ACCOUNT, RELS)
check("直接建单，没有追问",
      bool(named_out) and named_out[0].get("ok")
      and q_named.get_relay_clarify(ACCOUNT, FRIEND) is None, str(named_out))
check("发给了 alex", bool(named_out) and named_out[0]["chat"] == OWNER)

section("判不准 / 模型挂了：不许嘴上答应「我去问问」")
q_unsure = support.make_queue("relay_unsure.db")
UNSURE = {"is_relay": True, "target": "", "candidates": [], "need_target": False,
          "content": "问声好", "confidence": 0.3, "tz": "", "reason": ""}
with patch.object(relay, "judge", return_value=UNSURE):
    unsure = relay.maybe_relay(cfg, q_unsure, FRIEND,
                               [relay_message(9720, "帮我跟组长问声好", 920)],
                               ACCOUNT, RELS)
check("低置信度不建单", not q_unsure.list_reminders(account=ACCOUNT))
check("提醒回复侧别空口答应", "别空口答应" in relay.hint_line(unsure),
      relay.hint_line(unsure))

q_down = support.make_queue("relay_model_down.db")
with patch.object(relay, "judge", return_value=None):
    down = relay.maybe_relay(cfg, q_down, FRIEND,
                             [relay_message(9730, "帮我跟组长问声好", 930)],
                             ACCOUNT, RELS)
check("模型挂了也不建单", not q_down.list_reminders(account=ACCOUNT))
check("同样提醒别空口答应", "别空口答应" in relay.hint_line(down),
      relay.hint_line(down))

section("答复判读：提示词能拼出来、JSON 能解析、乱写不崩")
pending_jc = {"content": "明天带材料", "confirm_chat": OWNER,
              "candidates": '["alex"]', "options": '["alex", "小李"]',
              "ask": "你要发送的人是alex吗？"}
with patch("llm.chat", return_value={"ok": True, "text":
                                    '{"decision":"pick","target":"小李","confidence":0.9}'}) as fake:
    got = relay.judge_clarify(make_cfg(), "不是alex，是小李", pending_jc, RELS2,
                              requester="联系人A")
prompt = fake.call_args.args[0][0]["content"] if fake.called else ""
check("提示词里带了原委托、问过的名字和候选",
      "明天带材料" in prompt and "alex" in prompt and "小李" in prompt
      and "你要发送的人是alex吗" in prompt, prompt[:200])
check("判读结果解析成 pick", got and got["decision"] == "pick"
      and got["target"] == "小李", str(got))
with patch("llm.chat", return_value={"ok": True, "text": '{"decision":"乱写"}'}):
    fallback = relay.judge_clarify(make_cfg(), "嗯？", pending_jc, RELS2)
check("非法 decision 归为 unclear", fallback and fallback["decision"] == "unclear",
      str(fallback))
with patch("llm.chat", return_value={"ok": True, "text": "我看不懂"}):
    check("不是 JSON 返回 None",
          relay.judge_clarify(make_cfg(), "嗯", pending_jc, RELS2) is None)
with patch("llm.chat", return_value={"ok": False, "error": "超时"}):
    check("模型失败返回 None",
          relay.judge_clarify(make_cfg(), "嗯", pending_jc, RELS2) is None)

section("模型判读只作参考：这四类越权由代码否决")
q_guard = support.make_queue("relay_guard_pick.db")
with patch.object(relay, "judge", return_value=AMBIG):
    relay.maybe_relay(cfg, q_guard, FRIEND, [relay_message(9800, "帮我提醒他带材料", 800)],
                      ACCOUNT, RELS)
acknowledge_relay_prompt(q_guard, 801)
with patch.object(relay, "judge_clarify", return_value=decision("pick", "小李")):
    g_pick = relay.maybe_relay(cfg, q_guard, FRIEND, [relay_message(9801, "对", 802)],
                               ACCOUNT, RELS)
check("没点名却 pick → 不建单", not q_guard.list_reminders(account=ACCOUNT), str(g_pick))

q_guard2 = support.make_queue("relay_guard_deny.db")
with patch.object(relay, "judge", return_value=AMBIG):
    relay.maybe_relay(cfg, q_guard2, FRIEND, [relay_message(9810, "帮我提醒他带材料", 810)],
                      ACCOUNT, RELS)
acknowledge_relay_prompt(q_guard2, 811)
with patch.object(relay, "judge_clarify", return_value=decision("confirm")):
    g_deny = relay.maybe_relay(cfg, q_guard2, FRIEND,
                               [relay_message(9811, "不要发给这人", 812)], ACCOUNT, RELS)
check("明说不要却 confirm → 不建单", not q_guard2.list_reminders(account=ACCOUNT),
      str(g_deny))

q_guard3 = support.make_queue("relay_guard_lowconf.db")
with patch.object(relay, "judge", return_value=AMBIG):
    relay.maybe_relay(cfg, q_guard3, FRIEND, [relay_message(9820, "帮我提醒他带材料", 820)],
                      ACCOUNT, RELS)
acknowledge_relay_prompt(q_guard3, 821)
with patch.object(relay, "judge_clarify",
                  return_value={"decision": "confirm", "target": "", "confidence": 0.1}):
    g_low = relay.maybe_relay(cfg, q_guard3, FRIEND, [relay_message(9821, "对", 822)],
                              ACCOUNT, RELS)
check("置信度太低不落单", not q_guard3.list_reminders(account=ACCOUNT), str(g_low))

q_guard4 = support.make_queue("relay_guard_cancel.db")
with patch.object(relay, "judge", return_value=AMBIG):
    relay.maybe_relay(cfg, q_guard4, FRIEND, [relay_message(9830, "帮我提醒他带材料", 830)],
                      ACCOUNT, RELS)
acknowledge_relay_prompt(q_guard4, 831)
with patch.object(relay, "judge_clarify", return_value=decision("cancel")):
    relay.maybe_relay(cfg, q_guard4, FRIEND, [relay_message(9831, "再说吧", 832)],
                      ACCOUNT, RELS)
check("没说要取消就不结案", q_guard4.get_relay_clarify(ACCOUNT, FRIEND) is not None)

section("显式称呼不吃上下文：主人就是配置的主人")
check("「主人」永远指配置的主人",
      relay.target_of(cfg, "主人", RELS, context={"counterpart": LI}) == (OWNER, ""))
check("「owner」同理",
      relay.target_of(cfg, "owner", RELS, context={"counterpart": LI}) == (OWNER, ""))
check("只有「他/她」才看上下文",
      relay.target_of(cfg, "他", RELS, context={"counterpart": LI}) == (LI, ""))

section("最近联系人只认确实送达过的委托")
q_partner = support.make_queue("relay_partner.db")
q_partner.add_relay_watch(ACCOUNT, OWNER, FRIEND, "还没发出去", from_name="alex",
                          pending_send=True)
check("没送达的不算上一位委托人", q_partner.recent_relay_partner(ACCOUNT, FRIEND) == "")
q_partner2 = support.make_queue("relay_partner2.db")
q_partner2.add_relay_watch(ACCOUNT, OWNER, FRIEND, "已送达", from_name="alex")
check("送达过的才算", q_partner2.recent_relay_partner(ACCOUNT, FRIEND) == OWNER)

section("候选文案不显示微信号")
legacy_rels = RELS + [{"name": "wxid_abcdefg", "nickname": "", "relation": "老关系",
                       "chat": OWNER}]
opts = relay._clarify_options(cfg, FRIEND, legacy_rels, context={"counterpart": OWNER})
check("备用分支也过滤微信号", all("wxid_" not in o for o in opts), str(opts))
legacy_only = [{"name": "wxid_abcdefg", "nickname": "", "relation": "老关系",
                "chat": OWNER}]
check("没有真名就退回「主人」", relay._owner_label(cfg, legacy_only) == "主人")

section("改写不许把问句换成别人、也不许把回执说成已收到")
ASK_ALEX = "你要发送的人是alex吗？回「对」确认，或者告诉我该发给谁。"
q_foreign = support.make_queue("relay_polish_foreign.db")
q_foreign.add_relay_clarify(ACCOUNT, FRIEND, "明天带材料", ["alex"], ASK_ALEX, "k",
                            confirm_chat=OWNER, options=["alex"], ask_core=ASK_ALEX)
q_foreign.enqueue_relay_clarify_prompt(ACCOUNT, FRIEND, ASK_ALEX)
task_f = q_foreign.next_outbound(account=ACCOUNT)
bad_ask = "是发给小李吗？回「对」确认就行（alex 那边我一会儿再说）"
with patch("llm.chat", return_value={"ok": True, "text": bad_ask}), \
        patch.object(relay, "_relations", return_value=RELS2):
    kept_ask = relay.polish_clarify_task(q_foreign, cfg_p, task_f)
check("改写提到别的联系人 → 回退模板", kept_ask["text"] == task_f["text"],
      kept_ask["text"])

with patch("llm.chat", return_value={"ok": True, "text": "好呀，已经收到你的话了"}):
    check("回执不许说成「已经收到」",
          relay.polish_receipt(cfg_p, "好，这条委托取消了，没有发送。")
          == "好，这条委托取消了，没有发送。")

section("发送门禁先于润色：代传关着时不为它调模型")
q_gate = support.make_queue("relay_polish_gate.db")
cfg_gate = make_cfg()
cfg_gate["proactive"]["relay"]["enabled"] = False
cfg_gate["proactive"]["relay"]["polish"] = True
worker_gate = reply_worker.Worker.__new__(reply_worker.Worker)
worker_gate.acc = ACCOUNT
worker_gate.cfg = cfg_gate
worker_gate.q = q_gate
worker_gate.db = SimpleNamespace(get_messages=lambda *args, **kwargs: [])
worker_gate.brain = SimpleNamespace(mem=SimpleNamespace(add=lambda *args, **kwargs: None),
                                    reload=lambda cfg: None)
worker_gate._inflight_lock = threading.Lock()
worker_gate._inflight = set()
q_gate.add_relay_clarify(ACCOUNT, FRIEND, "内容", ["alex"], ASK_ALEX, "kg",
                         confirm_chat=OWNER, options=["alex"], ask_core=ASK_ALEX)
q_gate.enqueue_relay_clarify_prompt(ACCOUNT, FRIEND, ASK_ALEX)
gate_task = q_gate.next_outbound(account=ACCOUNT)
with patch("llm.chat") as gate_model:
    gate_result = worker_gate.send_task(gate_task, sleep_delay=False, reload_cfg=False)
check("代传关着 → 发送被挡", gate_result.get("status") == "cancelled", str(gate_result))
check("而且没有为它调模型", not gate_model.called, str(gate_model.call_count))

sys.exit(support.finish())
