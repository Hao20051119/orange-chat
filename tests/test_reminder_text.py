# -*- coding: utf-8 -*-
"""到点提醒的措辞：残渣清理、模型改写、模板兜底。

来历：2026-11-21 凌晨那条「凌晨四点五十八分，再 一下 我该去追剧」——
时间对了、内容是把原话剥了一半的残渣，模板一贴就成了复读机。
"""
import json
import sys
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import support
from support import check, section

import reminder
import reminder_parse

ACCOUNT = "wxid_test"
CHAT = "wxid_contact"

REPLY = "四点五十八了，追剧去，别又说我没提醒你"


class FakeBrain:
    """本地提醒只需要改写这一件事，别为了测试拖进整个人格层。"""

    def __init__(self, reply=REPLY, ok=True, error="模型超时"):
        self.reply = reply
        self.ok = ok
        self.error = error
        self.calls = []

    def compose_reminder(self, chat, content, original="", when_text="",
                         late=False, timeout=10):
        self.calls.append({"chat": chat, "content": content, "original": original,
                           "when_text": when_text, "late": late,
                           "timeout": timeout})
        if not self.ok:
            return {"ok": False, "error": self.error}
        return {"ok": True, "reply": self.reply}


def make_cfg(**rem):
    base = {"enabled": True, "llm_text": True, "llm_timeout": 10}
    base.update(rem)
    return support.base_cfg(proactive={"enabled": True, "reminders": base})


def _fake_judge(cfg, text, base_ts, tz=None, tz_zone=None):
    """测试替身：用本地解析模拟模型输出（生产路径现在一律走模型）。"""
    r = reminder_parse.detect(text, base_ts, tz=tz, tz_zone=tz_zone)
    if not r:
        return {"is_reminder": False, "confidence": 0.9, "content": "",
                "rule": "single", "next_ts": None, "need_time": False,
                "ask": "", "tz": "", "next_local": ""}
    return {"is_reminder": True, "confidence": 0.9, "content": r["content"],
            "rule": r["rule"], "next_ts": r["next_ts"],
            "need_time": bool(r.get("need_clarify")), "ask": "",
            "tz": "", "next_local": ""}


def capture(text, base_ts, name):
    """入站 → 委托识别 → 返回 (queue, reminder 行)。"""
    cfg = make_cfg()
    q = support.make_queue(name)
    q.enqueue_inbound(ACCOUNT, CHAT, 1, base_ts, "文本", text, sender=CHAT)
    msg = q.newest_pending(CHAT, account=ACCOUNT)[0]
    with patch.object(reminder, "judge_with_llm", side_effect=_fake_judge):
        ids = reminder.maybe_capture(cfg, q, CHAT, [msg], ACCOUNT)
    check("识别成提醒", len(ids) == 1, str(ids))
    return cfg, q, q.get_reminder(ids[0])


def fire(q, cfg, now):
    reminder.fire_due(q, cfg, now=now)
    return q.next_outbound(account=ACCOUNT)


section("内容抽取：委托词的口头残渣不该进提醒内容")
now = int(time.time())
base_ts = now - 180
cfg, q, rem = capture("三分钟后再提醒我一下，我该去追剧了", base_ts, "rem_text.db")
due = int(rem["next_ts"])
check("内容是「去追剧」", rem["content"] == "去追剧", repr(rem["content"]))
check("到点仍是原消息 +3 分钟", abs(due - (base_ts + 180)) <= 3, str(due - base_ts))
for text, want in (("明天早上提醒我去跑步", "跑步"),
                   ("今晚十一点提醒我关电脑", "关电脑"),
                   ("每天早上七点提醒我吃药", "吃药"),
                   ("记得提醒我八点开会", "开会"),
                   ("20 秒后提醒我喝水", "喝水")):
    got = reminder_parse.detect(text, now)
    check("「%s」→ %s" % (text, want), got and got["content"] == want,
          str(got and got["content"]))

section("模型改写：发送前换掉模板那句")
task = fire(q, cfg, due)
check("到点进了出站队列", task is not None and task["kind"] == "reminder")
template = reminder.build_text(rem, due)
check("默认先是模板兜底", task["text"] == template, task["text"])
brain = FakeBrain()
task = reminder.polish_task(q, cfg, brain, task, now=due)
stored = q.get_outbound(task["id"])
check("模型那句落库", stored["text"] == REPLY, stored["text"])
check("分段一起更新",
      json.loads(stored["segments"]) == reminder.segments_for(REPLY, cfg),
      str(stored["segments"]))
check("提示里带上对方的原话", "三分钟后再提醒我一下" in brain.calls[0]["original"],
      brain.calls[0]["original"])
check("提示里说清到点时间",
      brain.calls[0]["when_text"] == reminder.human_clock(due),
      brain.calls[0]["when_text"])
check("模型知道要提醒什么", brain.calls[0]["content"] == "去追剧")
check("超时按配置传下去", brain.calls[0]["timeout"] == 10)

again = FakeBrain(reply="不该再写一遍")
reminder.polish_task(q, cfg, again, q.get_outbound(task["id"]), now=due)
check("已改写的任务不重复润色", again.calls == [], str(again.calls))

section("兜底：模型不回话，模板照发")
cfg2, q2, rem2 = capture("三分钟后再提醒我一下，我该去跑步",
                         now - 180, "rem_text2.db")
cfg2 = make_cfg()
due2 = int(rem2["next_ts"])
task2 = fire(q2, cfg2, due2)
dead = FakeBrain(ok=False)
task2 = reminder.polish_task(q2, cfg2, dead, task2, now=due2)
check("模型失败时保留模板文案",
      q2.get_outbound(task2["id"])["text"] == task2["text"] and "跑步" in task2["text"],
      q2.get_outbound(task2["id"])["text"])
check("失败也真的问过模型", len(dead.calls) == 1)

skip = FakeBrain(ok=False, error="模型想跳过")
reminder.polish_task(q2, cfg2, skip, q2.get_outbound(task2["id"]), now=due2)
check("模型想跳过也不改文案",
      skip.calls and q2.get_outbound(task2["id"])["text"] == task2["text"])

off = FakeBrain()
kept = reminder.polish_task(q2, make_cfg(llm_text=False), off,
                           q2.get_outbound(task2["id"]), now=due2)
check("旧 llm_text 开关已废弃：正文照样由模型写",
      bool(off.calls) and q2.get_outbound(task2["id"])["text"] == REPLY,
      q2.get_outbound(task2["id"])["text"])

section("迟到一两分钟的提醒照样润色，只是带一句抱怨")
cfg3 = make_cfg()
q3 = support.make_queue("rem_text3.db")
base3 = now - 300
q3.add_reminder(ACCOUNT, CHAT, "喝水", "single", base3, anchor_ts=base3)
task3 = fire(q3, cfg3, now)
check("迟到模板带「晚了点」", task3 and task3["text"].startswith("晚了点，"),
      task3 and task3["text"])
late_brain = FakeBrain(reply="水都凉了，还不去喝")
task3 = reminder.polish_task(q3, cfg3, late_brain, task3, now=now)
check("迟到的也由模型改写",
      q3.get_outbound(task3["id"])["text"] == "水都凉了，还不去喝")
check("提示里说明迟到了", bool(late_brain.calls) and late_brain.calls[0]["late"] is True)

section("聊天回复里别再嘴硬：告诉模型「我已经记下了」")
rid4 = q3.add_reminder(ACCOUNT, CHAT, "吃药", "single", base3 + 60,
                       anchor_ts=base3 + 60)
hint = reminder.accepted_hint(q3, [rid4])
check("提示里带上要做的事", "吃药" in hint, hint)
check("提示里带上到点钟点",
      reminder.human_clock(base3 + 60) in hint, hint)
check("没建提醒就不出提示", reminder.accepted_hint(q3, []) == "")
check("提醒被取消后也不乱说",
      reminder.accepted_hint(q3, [999999]) == "")

section("跨时区：他那边 20:50 就是北京时间 04:50")
now2 = int(time.time())
user_now = datetime.fromtimestamp(now2 - 8 * 3600)      # 英国那边现在
said = ("你记住，中英时差8个小时，我现在是%s，请在%s的时候提醒我该追剧了"
        % (user_now.strftime("%H:%M"),
           (user_now + timedelta(minutes=6)).strftime("%H:%M")))
cfg4 = make_cfg()
q4 = support.make_queue("rem_text_tz.db")
q4.enqueue_inbound(ACCOUNT, CHAT, 1, now2, "文本", said, sender=CHAT)
row4 = q4.newest_pending(CHAT, account=ACCOUNT)[0]
with patch.object(reminder, "judge_with_llm", side_effect=_fake_judge):
    ids4 = reminder.maybe_capture(cfg4, q4, CHAT, [row4], ACCOUNT)
check("跨时区也识别成提醒", len(ids4) == 1, str(ids4))
rem4 = q4.get_reminder(ids4[0])
check("按他那边的时间换算成北京时间",
      abs(int(rem4["next_ts"]) - (now2 + 360)) <= 60,
      "%+d 秒" % (int(rem4["next_ts"]) - now2))
check("内容是「追剧」", rem4["content"] == "追剧", repr(rem4["content"]))
check("时差记在这个联系人头上",
      q4.get_chat_tz(ACCOUNT, CHAT) == -8 * 3600,
      str(q4.get_chat_tz(ACCOUNT, CHAT)))

reminder.fire_due(q4, cfg4, now=int(rem4["next_ts"]))
task4 = q4.next_outbound(account=ACCOUNT)
user_clock = reminder.human_clock(int(rem4["next_ts"]) - 8 * 3600)
host_clock = reminder.human_clock(int(rem4["next_ts"]))
check("兜底模板也报他那边的时间",
      user_clock in task4["text"] and host_clock not in task4["text"],
      "%s / %s" % (user_clock, task4["text"]))

q4.enqueue_inbound(ACCOUNT, CHAT, 2, now2 + 60, "文本",
                   "明天下午三点提醒我开会", sender=CHAT)
row5 = [m for m in q4.newest_pending(CHAT, account=ACCOUNT) if m["id"] != row4["id"]][0]
with patch.object(reminder, "judge_with_llm", side_effect=_fake_judge):
    ids5 = reminder.maybe_capture(cfg4, q4, CHAT, [row5], ACCOUNT)
rem5 = q4.get_reminder(ids5[0])
tomorrow3 = (user_now + timedelta(days=1)).replace(hour=15, minute=0,
                                                   second=0, microsecond=0)
check("没再提时差，也按他那边算",
      int(rem5["next_ts"]) == int(tomorrow3.timestamp()) + 8 * 3600,
      str(datetime.fromtimestamp(int(rem5["next_ts"]))))

section("只碰提醒，别越界")
q2.create_outbound(ACCOUNT, CHAT, "普通回复")
reply_task = q2.next_outbound(account=ACCOUNT)
probe = FakeBrain()
out = reminder.polish_task(q2, cfg2, probe, reply_task, now=due2)
check("普通回复不润色", probe.calls == [] and out["text"] == "普通回复")

q2.begin_outbound_attempt(task2["id"], 0, None, int(time.time()))
check("已经开始提交的任务不许改写",
      not q2.rewrite_outbound_text(task2["id"], "新文案", ["新文案"]))

section("一句话两个钟点：取委托词前面最近的那个")
BASE2 = int(datetime(2026, 11, 22, 20, 0).timestamp())
r = reminder_parse.detect("我明天下午三点半开会，三点记得叫我", BASE2, BASE2)
check("排到三点而不是三点半",
      r and datetime.fromtimestamp(r["next_ts"]) == datetime(2026, 11, 23, 15, 0),
      r and datetime.fromtimestamp(r["next_ts"]))
check("内容留住会议时间",
      r and "下午三点半" in r["content"], r and r["content"])

section("「叫/喊我」这类叫醒服务")
for text, want in (
    ("明天早上七点叫我起来", datetime(2026, 11, 23, 7, 0)),
    ("三点喊我", datetime(2026, 11, 23, 3, 0)),
    ("三点半叫我", datetime(2026, 11, 23, 3, 30)),
):
    r = reminder_parse.detect(text, BASE2, BASE2)
    check("「%s」→ %s" % (text, want.strftime("%m-%d %H:%M")),
          r and datetime.fromtimestamp(r["next_ts"]) == want,
          r and datetime.fromtimestamp(r["next_ts"]))

r = reminder_parse.detect("每天早上七点叫我起床", BASE2, BASE2)
check("每天叫醒是周期提醒",
      r and r["rule"] == "daily" and "起床" in r["content"],
      r and (r["rule"], r["content"]))

r = reminder_parse.detect("下午三点开会，明天九点叫我", BASE2, BASE2)
check("跨天的九点不继承「下午」",
      r and datetime.fromtimestamp(r["next_ts"]).hour == 9,
      r and datetime.fromtimestamp(r["next_ts"]))

section("委托词在句子后半：只取它后面的事，不要把背景写进内容")
r = reminder_parse.detect("我以后每周三晚上都会空下来，你记得提醒我去公园跑步", BASE2, BASE2)
check("识别成每周三", r and r["rule"] == "weekly:3", r and r["rule"])
check("内容只要「跑步」，不要背景说明",
      r and "空下来" not in r["content"] and "跑步" in r["content"],
      r and r["content"])
check("钟点用「晚上」的默认八点",
      r and datetime.fromtimestamp(r["next_ts"]).hour == 20,
      r and datetime.fromtimestamp(r["next_ts"]))
r = reminder_parse.detect("你记得提醒我明天交材料", BASE2, BASE2)
check("「你记得提醒我…」内容不含「你」",
      r and r["content"] == "交材料", r and r["content"])

sys.exit(support.finish())
