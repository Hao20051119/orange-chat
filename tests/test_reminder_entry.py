# -*- coding: utf-8 -*-
"""提醒委托入口与自动回复解耦。

旧行为：``identity_ok`` 排在提醒扫描之前，只开「定时提醒」、关掉自动回复的
人，一条委托也收不到——入口被 ``auto_reply`` 绑死了，而发送侧
``evaluate_reminder`` 明明不看这个开关。
"""
import json
import sys
import threading
import time
from datetime import datetime
from unittest.mock import patch

import support
from support import check, section

import gate
import reminder
import reminder_parse
import reply_worker as worker

ACCOUNT = "wxid_test"
CHAT = "wxid_contact"
SELF = "test_self"
OWNER = "wxid_owner"


def _fake_judge(cfg, text, base_ts, tz=None, tz_zone=None):
    """测试替身：生产路径现在一律先过模型再建单。"""
    r = reminder_parse.detect(text, base_ts, tz=tz, tz_zone=tz_zone)
    if not r:
        return {"is_reminder": False, "confidence": 0.9, "content": "",
                "rule": "single", "next_ts": None, "need_time": False,
                "ask": "", "tz": "", "next_local": ""}
    return {"is_reminder": True, "confidence": 0.9, "content": r["content"],
            "rule": r["rule"], "next_ts": r["next_ts"],
            "need_time": bool(r.get("need_clarify")), "ask": "",
            "tz": "", "next_local": ""}


class FakeDB:
    def __init__(self):
        self.rows = []

    def get_messages(self, chat, limit=30):
        return []

    def get_new_messages(self, chat, since_seq, limit):
        return []


class FakeBrain:
    def __init__(self, queue):
        self.mem = support.make_memory("memory_entry_%d.db" % id(self))
        self.cfg = {}
        self.calls = []

    def reload(self, cfg):
        self.cfg = cfg

    def should_reply(self, *args, **kwargs):
        return True, ""

    def compose(self, chat, text, **kwargs):
        self.calls.append(text)
        return {"ok": True, "reply": "好"}

    def sensitive_label(self, text):
        return None

    def maybe_summarize(self, chat):
        pass

    def maybe_adapt(self, chat):
        pass


def make_cfg(**over):
    cfg = support.base_cfg()
    cfg["agent"]["auto_reply"] = False
    cfg["proactive"] = {
        "enabled": False,
        "reminders": {"enabled": True, "grace_seconds": 7200},
        "relay": {"enabled": True, "delay_seconds": 0, "daily_limit": 8},
    }
    for k, v in (over or {}).items():
        cfg[k] = v
    return cfg


def setup(name, config):
    w = worker.Worker.__new__(worker.Worker)
    w.cfg = config
    w.acc = ACCOUNT
    w.me = SELF
    w.q = support.make_queue(name + ".db")
    w.brain = FakeBrain(w.q)
    w.db = FakeDB()
    w._watermark = {}
    w._collector_started_at = int(time.time())
    w._HOOK_LAST = {}
    w._inflight = set()
    w._inflight_lock = threading.Lock()
    return w


section("入口门禁：只看提醒开关")
cfg_off = make_cfg()
check("关自动回复不影响提醒入口", gate.reminder_entry_ok(cfg_off, CHAT)[0])
cfg_rem_off = make_cfg()
cfg_rem_off["proactive"]["reminders"]["enabled"] = False
check("提醒开关关着就不收", not gate.reminder_entry_ok(cfg_rem_off, CHAT)[0])
cfg_black = make_cfg()
cfg_black["agent"]["reply_blacklist"] = [CHAT]
check("黑名单照样拦", not gate.reminder_entry_ok(cfg_black, CHAT)[0])
check("系统号不收委托", not gate.reminder_entry_ok(cfg_off, "filehelper")[0])

section("关自动回复时仍收下提醒委托")
w = setup("entry_off", make_cfg())
w.q.enqueue_inbound(ACCOUNT, CHAT, 1, int(time.time()), "文本",
                    "20 分钟后提醒我喝水", sender=CHAT)
with (patch.object(worker, "load_cfg", return_value=w.cfg),
      patch.object(reminder, "judge_with_llm", side_effect=_fake_judge)):
    w.process(CHAT)
rems = w.q.list_reminders(CHAT, account=ACCOUNT)
check("提醒记下来了", len(rems) == 1, str([r["content"] for r in rems]))
check("提醒开关关着时没回聊天消息", not w.q.list_outbound("draft", account=ACCOUNT))
check("消息按跳过结案", not w.q.newest_pending(CHAT, account=ACCOUNT))

section("提醒开关关着：一条也不收")
w2 = setup("entry_rem_off", make_cfg())
w2.cfg["proactive"]["reminders"]["enabled"] = False
w2.q.enqueue_inbound(ACCOUNT, CHAT, 1, int(time.time()), "文本",
                     "20 分钟后提醒我喝水", sender=CHAT)
with patch.object(worker, "load_cfg", return_value=w2.cfg):
    w2.process(CHAT)
check("没有建提醒", not w2.q.list_reminders(CHAT, account=ACCOUNT))

section("黑名单里的人：不建提醒")
w3 = setup("entry_black", make_cfg())
w3.cfg["agent"]["reply_blacklist"] = [CHAT]
w3.q.enqueue_inbound(ACCOUNT, CHAT, 1, int(time.time()), "文本",
                     "20 分钟后提醒我喝水", sender=CHAT)
with patch.object(worker, "load_cfg", return_value=w3.cfg):
    w3.process(CHAT)
check("黑名单不收委托", not w3.q.list_reminders(CHAT, account=ACCOUNT))

section("规则吃不准的委托：关自动回复也要交给模型判")
w4 = setup("entry_cand", make_cfg())
w4.q.add_candidate(ACCOUNT, CHAT, 7001, "明天下午两点左右叫醒我", int(time.time()))
verdict = {"is_reminder": False, "confidence": 0.9, "content": "叫醒我",
           "rule": "single", "next_ts": None, "need_time": False, "ask": ""}
with patch.object(reminder, "judge_with_llm", return_value=verdict) as judge:
    reminder.process_candidates(w4.q, w4.cfg)
check("候选进了模型判定", judge.called)
check("候选结案，不再卡在 pending",
      not w4.q.pending_candidates(10, account=ACCOUNT))

section("到点正文只能用模型写的：模型没写出来就不发")
cfg_rem = make_cfg()
cfg_rem["agent"]["auto_reply"] = True
cfg_rem["proactive"]["enabled"] = True
cfg_rem["proactive"]["reminders"] = {"enabled": True, "llm_text": True,
                                     "grace_seconds": 7200}
w5 = setup("no_model", cfg_rem)
now = int(time.time())
w5.q.add_reminder(ACCOUNT, CHAT, "喝水", "single", now - 60, anchor_ts=now - 60)
reminder.fire_due(w5.q, cfg_rem)
task5 = w5.q.next_outbound(account=ACCOUNT)
sent_box = []
w5.brain.compose_reminder = lambda *a, **k: {"ok": False, "error": "模型挂了"}
with patch.object(worker, "post",
                  side_effect=lambda p, d: sent_box.append(d) or
                  {"ok": True, "submitted": True, "verified": True}):
    res = w5.send_task(task5, sleep_delay=False, reload_cfg=False)
check("模型没写出正文就不发", res.get("status") == "waiting_model", str(res))
check("一条都没提交给微信", sent_box == [], str(sent_box))
check("任务退回待发等下一轮",
      w5.q.get_outbound(task5["id"])["status"] == "draft")

w6 = setup("with_model", cfg_rem)
w6.q.add_reminder(ACCOUNT, CHAT, "喝水", "single", now - 60, anchor_ts=now - 60)
reminder.fire_due(w6.q, cfg_rem)
task6 = w6.q.next_outbound(account=ACCOUNT)
w6.brain.compose_reminder = lambda *a, **k: {"ok": True, "reply": "该喝水了，快去"}
with patch.object(worker, "post",
                  return_value={"ok": True, "submitted": True, "verified": True}):
    res6 = w6.send_task(task6, sleep_delay=False, reload_cfg=False)
check("模型写好才发出去", res6.get("status") == "sent", str(res6))
check("发的是模型那句", w6.q.get_outbound(task6["id"])["text"] == "该喝水了，快去")

section("时区线索：存时区名，偏移按夏令时现算")
q_tz = support.make_queue("tz.db")
off = reminder.apply_tz_hint(q_tz, ACCOUNT, CHAT, "Europe/London")
check("伦敦（冬令时）= -8 小时", off == -8 * 3600, str(off))
check("落库并在读取时现算", q_tz.get_chat_tz(ACCOUNT, CHAT) == off)
check("UTC+8 写法也认",
      reminder.apply_tz_hint(q_tz, ACCOUNT, CHAT, "UTC+8") == 8 * 3600)
check("认不出就不动已有记录",
      reminder.apply_tz_hint(q_tz, ACCOUNT, CHAT, "火星") is None
      and q_tz.get_chat_tz(ACCOUNT, CHAT) == 8 * 3600)

section("人工改稿后发送的必须是新正文")
w7 = setup("manual_edit", cfg_rem)
oid = w7.q.create_outbound(ACCOUNT, CHAT, "旧的模型话",
                           segments=["旧的模型话", "第二段"])
w7.q.finish_outbound(oid, "hold", "待审")
check("改稿成功", w7.q.update_outbound_text(oid, "人工改过的话"))
row = w7.q.get_outbound(oid)
check("正文是人工那句", row["text"] == "人工改过的话", str(row["text"]))
check("旧分段清空（发送不会读旧话）", not row["segments"], str(row["segments"]))
check("标成人工来源", row["text_source"] == "manual", str(row["text_source"]))
w7.q.approve_outbound(oid, ACCOUNT)
task7 = w7.q.next_outbound(account=ACCOUNT)
sent7 = []


def fake_post(path, data):
    sent7.append(data["text"])
    return {"ok": True, "submitted": True, "verified": True}


with patch.object(worker, "post", side_effect=fake_post):
    w7.send_task(task7, sleep_delay=False, reload_cfg=False)
check("发出去的是人工那句", sent7 == ["人工改过的话"], str(sent7))

section("候选建单失败整体回滚，恢复后只建一条")
w8 = setup("cand_tx", make_cfg())
w8.q.add_candidate(ACCOUNT, CHAT, 8001, "明天下午两点左右叫醒我", int(time.time()))
verdict8 = {"is_reminder": True, "confidence": 0.9, "content": "叫醒我",
            "rule": "single", "next_ts": int(time.time()) + 3600,
            "need_time": False, "ask": "", "tz": "", "next_local": ""}
with patch.object(reminder, "judge_with_llm", return_value=verdict8), \
     patch.object(w8.q, "record_action",
                  side_effect=__import__("sqlite3").OperationalError("模拟登记失败")):
    reminder.process_candidates(w8.q, w8.cfg)
check("登记失败时提醒不留下", not w8.q.list_reminders(CHAT, account=ACCOUNT))
check("候选还挂着，可以重试", bool(w8.q.pending_candidates(5, account=ACCOUNT)))
with patch.object(reminder, "judge_with_llm", return_value=verdict8):
    reminder.process_candidates(w8.q, w8.cfg)
check("恢复后只建一条", len(w8.q.list_reminders(CHAT, account=ACCOUNT)) == 1,
      str(len(w8.q.list_reminders(CHAT, account=ACCOUNT))))

section("地名线索交给模型学：我在英国")
w9 = setup("tz_learn", make_cfg())
now9 = int(time.time())
w9.q.enqueue_inbound(ACCOUNT, CHAT, 1, now9, "文本",
                     "我在英国，明早八点提醒我开会", sender=CHAT)
with patch.object(reminder, "learn_tz_with_llm", return_value="Europe/London"), \
     patch.object(reminder, "judge_with_llm", side_effect=_fake_judge), \
     patch.object(worker, "load_cfg", return_value=w9.cfg):
    w9.process(CHAT)
rems9 = w9.q.list_reminders(CHAT, account=ACCOUNT)
check("时区学到了", w9.q.get_chat_zone(ACCOUNT, CHAT) == "Europe/London",
      str(w9.q.get_chat_zone(ACCOUNT, CHAT)))


def local_hour(ts, zone):
    import reminder_parse
    from datetime import datetime as _dt
    off = reminder_parse.tz_from_zone(zone, int(ts)) or 0
    return _dt.fromtimestamp(int(ts) + off)


check("按伦敦当地 08:00 排期",
      rems9 and local_hour(rems9[0]["next_ts"], "Europe/London").hour == 8,
      str(rems9 and local_hour(rems9[0]["next_ts"], "Europe/London")))

section("普通聊天里提到的地点也会被记住")
w10 = setup("tz_plain", make_cfg())
w10.q.enqueue_inbound(ACCOUNT, CHAT, 1, int(time.time()), "文本",
                      "我最近搬到英国了", sender=CHAT)
with patch.object(reminder, "learn_tz_with_llm", return_value="Europe/London"), \
     patch.object(worker, "load_cfg", return_value=w10.cfg):
    w10.process(CHAT)
check("没有提醒委托也记下了时区",
      w10.q.get_chat_zone(ACCOUNT, CHAT) == "Europe/London",
      str(w10.q.get_chat_zone(ACCOUNT, CHAT)))

section("已发过段落的草稿：改稿只替换剩余段，不重复发前段")
w10 = setup("partial_edit", cfg_rem)
oid2 = w10.q.create_outbound(ACCOUNT, CHAT, "第一段第二段",
                             segments=["第一段", "第二段"])
w10.q.next_outbound(account=ACCOUNT)
w10.q.begin_outbound_attempt(oid2, 0, 0)
w10.q.bump_segments(oid2, 1)
w10.q.finish_outbound(oid2, "hold", "输入框被占用", bump_attempt=False,
                      expected="sending")
check("改稿成功", w10.q.update_outbound_text(oid2, "改过的第二段"))
row2 = w10.q.get_outbound(oid2)
check("保留已发第一段、接上新正文",
      json.loads(row2["segments"] or "[]") == ["第一段", "改过的第二段"],
      str(row2["segments"]))
check("发送进度仍停在第一段", row2["segments_sent"] == 1, str(row2["segments_sent"]))
w10.q.approve_outbound(oid2, ACCOUNT)
task10 = w10.q.next_outbound(account=ACCOUNT)
sent10 = []
with patch.object(worker, "post",
                  side_effect=lambda p, d: sent10.append(d["text"]) or
                  {"ok": True, "submitted": True, "verified": True}):
    w10.send_task(task10, sleep_delay=False, reload_cfg=False)
check("只补发第二段", sent10 == ["改过的第二段"], str(sent10))
check("最终标成已发送", w10.q.get_outbound(oid2)["status"] == "sent")

section("坏周期规则不堵调度")
check("合法规则原样", reminder_parse.valid_rule("weekly:1,3") == "weekly:1,3")
check("中文星期判非法", reminder_parse.valid_rule("weekly:周一") is None)
check("advance 对坏规则返回 None",
      reminder_parse.advance("weekly:周一", 100, 100) is None)
q_bad = support.make_queue("bad_rule.db")
cfg_bad = make_cfg()
now_bad = int(time.time())
q_bad.add_reminder(ACCOUNT, CHAT, "坏的", "weekly:周一", now_bad - 60,
                   anchor_ts=now_bad - 60)
q_bad.add_reminder(ACCOUNT, CHAT, "好的", "single", now_bad - 60,
                   anchor_ts=now_bad - 60)
reminder.fire_due(q_bad, cfg_bad, now=now_bad)
out_bad = q_bad.next_outbound(account=ACCOUNT)
check("坏记录被停用，正常提醒照常出站",
      out_bad and "好的" in (out_bad.get("text") or ""), str(out_bad))
check("坏记录不再 active",
      [r["status"] for r in q_bad.list_reminders(CHAT, account=ACCOUNT)
       if r["content"] == "坏的"] == ["paused"])

section("旧手工周提醒迁移：星期对齐到当地时间")
q_mig = support.make_queue("mig.db")
q_mig.set_chat_tz(ACCOUNT, CHAT, -8 * 3600)          # 伦敦，比北京慢 8 小时
anchor_local_mon = datetime(2027, 1, 4, 1, 0)        # 伦敦周一 01:00
anchor_host = int(anchor_local_mon.timestamp()) + 8 * 3600   # 北京周日 23:00
rid_mig = q_mig.add_reminder(ACCOUNT, CHAT, "旧的", "weekly:7", anchor_host,
                             anchor_ts=anchor_host)
q_mig._migrate_weekly_tz()
check("星期改成当地周一", q_mig.get_reminder(rid_mig)["rule"] == "weekly:1",
      q_mig.get_reminder(rid_mig)["rule"])

section("界面提交整条正文：已发第一段不重发")
w11 = setup("full_text_edit", cfg_rem)
oid3 = w11.q.create_outbound(ACCOUNT, CHAT, "第一段第二段",
                             segments=["第一段", "第二段"])
w11.q.next_outbound(account=ACCOUNT)
w11.q.begin_outbound_attempt(oid3, 0, 0)
w11.q.bump_segments(oid3, 1)
w11.q.finish_outbound(oid3, "hold", "待审", bump_attempt=False,
                      expected="sending")
check("整条改稿成功", w11.q.update_outbound_text(oid3, "第一段改过的第二段"))
row3 = w11.q.get_outbound(oid3)
check("已发第一段不重复进待发段",
      json.loads(row3["segments"] or "[]") == ["第一段", "改过的第二段"],
      str(row3["segments"]))

section("每周多日计划不会被迁移改坏")
q_mig2 = support.make_queue("mig2.db")
q_mig2.set_chat_tz(ACCOUNT, CHAT, -8 * 3600)
anchor2 = int(datetime(2027, 1, 4, 1, 0).timestamp()) + 8 * 3600
rid2 = q_mig2.add_reminder(ACCOUNT, CHAT, "多日", "weekly:1,3", anchor2,
                           anchor_ts=anchor2)
q_mig2._migrate_weekly_tz()
check("多日规则原样保留",
      q_mig2.get_reminder(rid2)["rule"] == "weekly:1,3",
      q_mig2.get_reminder(rid2)["rule"])

section("旧库升级：已有正文的提醒直接能发，不再等模型")
from wxqueue import Queue

q_old = support.make_queue("legacy.db")
oid_old = q_old.create_outbound(ACCOUNT, CHAT, "旧库早就写好的话",
                                kind="reminder")
with q_old._conn() as conn:
    conn.execute("ALTER TABLE outbound DROP COLUMN text_source")
    conn.execute("PRAGMA user_version=0")     # 模拟真旧库：迁移还没跑过
q_old = Queue(q_old.db_path)             # 重新打开 → 触发一次迁移
row_old = q_old.get_outbound(oid_old)
check("旧任务标成 legacy", row_old["text_source"] == "legacy",
      str(row_old["text_source"]))

w12 = setup("legacy_send", cfg_rem)
now12 = int(time.time())
w12.q.add_reminder(ACCOUNT, CHAT, "旧提醒", "single", now12 - 60,
                   anchor_ts=now12 - 60)
reminder.fire_due(w12.q, cfg_rem)
task12 = w12.q.next_outbound(account=ACCOUNT)
with w12.q._conn() as conn:
    conn.execute("UPDATE outbound SET text_source='legacy' WHERE id=?",
                 (task12["id"],))
task12 = w12.q.get_outbound(task12["id"])
with patch.object(worker, "post",
                  return_value={"ok": True, "submitted": True, "verified": True}):
    res12 = w12.send_task(task12, sleep_delay=False, reload_cfg=False)
check("旧正文直接发出去，不再等模型", res12.get("status") == "sent", str(res12))

section("提醒首发待审：改稿后批准，发的是新正文")
w13 = setup("reminder_approve_edit", cfg_rem)
now13 = int(time.time())
w13.q.add_reminder(ACCOUNT, CHAT, "喝水", "single", now13 - 60,
                   anchor_ts=now13 - 60)
reminder.fire_due(w13.q, cfg_rem)
task13 = w13.q.next_outbound(account=ACCOUNT)
w13.brain.compose_reminder = lambda *a, **k: {"ok": False, "error": "模型挂了"}
res13 = w13.send_task(task13, sleep_delay=False, reload_cfg=False)
check("模型没写出来先退回待发", res13.get("status") == "waiting_model", str(res13))
w13.q.finish_outbound(task13["id"], "hold", "人工待审", bump_attempt=False,
                      expected="draft")
check("批准时改正文", w13.q.approve_outbound(task13["id"], ACCOUNT,
                                          text="该喝水了，快去"))
task13b = w13.q.next_outbound(account=ACCOUNT)
sent13 = []
with patch.object(worker, "post",
                  side_effect=lambda p, d: sent13.append(d["text"]) or
                  {"ok": True, "submitted": True, "verified": True}):
    w13.send_task(task13b, sleep_delay=False, reload_cfg=False)
check("发出去的是人工写的那句", sent13 == ["该喝水了，快去"], str(sent13))

section("追问之后补建：回答里带周期就按周期建")
CH2 = "wxid_chem"


def _clarify(q, content="化学前沿", ask="几点提醒你？", base_ts=None):
    return q.add_clarify(ACCOUNT, CH2, content,
                         int(base_ts or time.time()), ask)


q30 = support.make_queue("clarify_cycle.db")
c30 = _clarify(q30)
rid30 = reminder.maybe_answer_clarify(make_cfg(), q30, CH2, "每周三下午一点五十",
                                      ACCOUNT)
rem30 = q30.get_reminder(rid30) if rid30 else None
check("回答带「每周三」就建周期单", rem30 and rem30["rule"] == "weekly:3",
      str(rem30 and rem30["rule"]))
check("时间落在周三 13:50",
      rem30 and datetime.fromtimestamp(rem30["next_ts"]).isoweekday() == 3
      and datetime.fromtimestamp(rem30["next_ts"]).strftime("%H:%M") == "13:50",
      str(rem30 and datetime.fromtimestamp(rem30["next_ts"])))
check("提醒内容还是原委托那件事", rem30 and rem30["content"] == "化学前沿",
      str(rem30 and rem30["content"]))
check("追问记录已结案", q30.get_clarify(CH2, "pending", account=ACCOUNT) is None)

q31 = support.make_queue("clarify_once.db")
_clarify(q31)
rid31 = reminder.maybe_answer_clarify(make_cfg(), q31, CH2, "一点五十", ACCOUNT)
rem31 = q31.get_reminder(rid31)
check("没说周期就还是一次性", rem31 and rem31["rule"] == "single",
      str(rem31 and rem31["rule"]))
check("一次性的时间也晚于现在", rem31 and rem31["next_ts"] > time.time(),
      str(rem31 and rem31["next_ts"]))

q32 = support.make_queue("clarify_daily.db")
_clarify(q32)
rid32 = reminder.maybe_answer_clarify(make_cfg(), q32, CH2, "每天七点", ACCOUNT)
check("「每天」→ daily", q32.get_reminder(rid32)["rule"] == "daily",
      str(q32.get_reminder(rid32)["rule"]))

q33 = support.make_queue("clarify_interval.db")
_clarify(q33)
rid33 = reminder.maybe_answer_clarify(make_cfg(), q33, CH2, "每2天的下午三点半",
                                      ACCOUNT)
check("「每2天」→ interval:2",
      q33.get_reminder(rid33)["rule"] == "interval:2",
      str(q33.get_reminder(rid33)["rule"]))

q34 = support.make_queue("clarify_late.db")
_clarify(q34, base_ts=time.time() - 3 * 86400)
rid34 = reminder.maybe_answer_clarify(make_cfg(), q34, CH2, "每周三下午一点五十",
                                      ACCOUNT)
rem34 = q34.get_reminder(rid34)
check("隔了三天才回，也不会建到过去",
      rem34 and rem34["next_ts"] > time.time(), str(rem34 and rem34["next_ts"]))

q35 = support.make_queue("clarify_none.db")
_clarify(q35)
check("不像回答就不建", reminder.maybe_answer_clarify(
    make_cfg(), q35, CH2, "好的谢谢", ACCOUNT) is None)
check("追问还留着", q35.get_clarify(CH2, "pending", account=ACCOUNT) is not None)

section("代问回传：已生成的正文通过正常出站发送")
returned = setup("relay_back_delivery", make_cfg())
message = "对方说他吃了汉堡。"
now = int(time.time())
returned.q.add_reminder(ACCOUNT, CHAT, message, "single", now,
                        anchor_ts=now, confirm_mode="relay_back")
reminder.fire_due(returned.q, returned.cfg)
task = returned.q.next_outbound(account=ACCOUNT)
with patch.object(worker, "post", return_value={"ok": True, "verified": True}) as post:
    sent = returned.send_task(task, sleep_delay=False, reload_cfg=False)
check("派发回传正文，按实际送达结案", sent["status"] == "sent"
      and post.call_args.args[1]["text"] == message, str(sent))

section("升级前的待发回传仍能发送")
returned.q.add_reminder(ACCOUNT, CHAT, message, "single", now,
                        anchor_ts=now, confirm_mode="relay_back")
reminder.fire_due(returned.q, returned.cfg)
task = returned.q.next_outbound(account=ACCOUNT)
with returned.q.tx() as conn:
    conn.execute("UPDATE outbound SET text_source='template' WHERE id=?", (task["id"],))
with patch.object(worker, "post", return_value={"ok": True, "verified": True}) as post:
    sent = returned.send_task(task, sleep_delay=False, reload_cfg=False)
check("旧回传不会堵在等模型", sent["status"] == "sent"
      and post.call_args.args[1]["text"] == message, str(sent))

sys.exit(support.finish())
