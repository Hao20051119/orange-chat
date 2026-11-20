# -*- coding: utf-8 -*-
"""采集、连发、发送核验与主动行为的离线回归。"""
import json
import sys
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import support
from support import check, section

import llm
import kb
import proactive
import reminder
import reminder_parse
import reply_worker as worker

ACCOUNT = "wxid_test"
CHAT = "wxid_contact"
SELF = "test_self"


class FakeDB:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def get_messages(self, chat, limit=30):
        return list(reversed(self.rows))[:limit]

    def get_new_messages(self, chat, since_seq, limit):
        return [row for row in self.rows if row["sort_seq"] > since_seq][:limit]


class FakeBrain:
    def __init__(self, queue):
        self.queue = queue
        self.mem = support.make_memory("memory_%d.db" % id(self))
        self.cfg = {}
        self.calls = []
        self.followup = None

    def reload(self, cfg):
        self.cfg = cfg

    def should_reply(self, *args, **kwargs):
        return True, ""

    def compose(self, chat, text, **kwargs):
        self.calls.append(text)
        if self.followup:
            self.followup(len(self.calls))
        return {"ok": True, "reply": "收到了"}

    def sensitive_label(self, text):
        return None

    def contact_cfg(self, chat):
        return self.cfg.get("contacts", {}).get(chat, {})

    def maybe_summarize(self, chat):
        pass

    def maybe_adapt(self, chat):
        pass


def setup(name, cfg=None):
    instance = worker.Worker.__new__(worker.Worker)
    instance.cfg = cfg or support.base_cfg()
    instance.acc = ACCOUNT
    instance.me = SELF
    instance.q = support.make_queue(name + ".db")
    instance.brain = FakeBrain(instance.q)
    instance.db = FakeDB()
    instance._watermark = {}
    instance._collector_started_at = int(time.time())
    instance._HOOK_LAST = {}
    instance._inflight = set()
    instance._inflight_lock = threading.Lock()
    return instance


def receive(q, seq, content):
    q.enqueue_inbound(ACCOUNT, CHAT, seq, int(time.time()), "文本", content, sort_seq=seq)


section("生成期间多次补发，超出本轮上限的消息仍需处理")
w = setup("regen")
receive(w.q, 1, "最先说的话")
w.brain.followup = lambda call: receive(w.q, call + 1, "补充-%d" % call) if call <= 3 else None
with patch.object(worker, "load_cfg", return_value=w.cfg):
    w.process(CHAT)
    pending = w.q.newest_pending(CHAT, account=ACCOUNT)
    draft = w.q.list_outbound("draft", account=ACCOUNT)[0]
    check("只结案进入模型的三条", json.loads(draft["covered"]) == [1, 2, 3])
    check("最后一条补充仍待处理", [row["content"] for row in pending] == ["补充-3"])
    w.process(CHAT)
    check("下一轮模型收到了最后一条补充", "补充-3" in w.brain.calls[-1])
    check("下一轮完成后无残留待处理", not w.q.newest_pending(CHAT, account=ACCOUNT))

w = setup("burst")
for seq in range(1, 7):
    receive(w.q, seq, "第%d句" % seq)
with patch.object(worker, "load_cfg", return_value=w.cfg):
    w.process(CHAT)
check("六条连发都进入同一轮模型", all("第%d句" % seq in w.brain.calls[0] for seq in range(1, 7)))
check("带问号的短句和数字选项不省略", all(not worker.is_trivial_text(t) for t in ("在？", "可以?", "1")))

section("采集入队报错，无论后续消息多少都不能跳过水位")
w = setup("collector")
w.q.set_collect_watermark(ACCOUNT, CHAT, 10)
w.db.rows = [
    {"local_id": seq, "sort_seq": seq, "create_time": int(time.time()),
     "content": "消息%d" % seq, "type": "文本", "sender_username": CHAT}
    for seq in (11, 12)
]
with patch.object(w.q, "enqueue_inbound", side_effect=OSError("模拟入队失败")):
    try:
        w._collect_chat(CHAT)
    except OSError:
        pass
check("写失败时水位没有推进", w.q.get_collect_watermark(ACCOUNT, CHAT) == 10)
check("重试前没有抢先入队后面的消息", not w.q.newest_pending(CHAT, account=ACCOUNT))
w._collect_chat(CHAT)
check("恢复后按序补齐两条消息", [row["sort_seq"] for row in w.q.newest_pending(CHAT, account=ACCOUNT)] == [11, 12])
check("恢复后保存水位", w.q.get_collect_watermark(ACCOUNT, CHAT) == 12)

section("发送回读失败只对账当前段，不写未回读段进记忆")
w = setup("delivery")
now = int(time.time())
w.db.rows = [{"sender_username": SELF, "content": "第二段", "create_time": now, "sort_seq": 21}]
task_id = w.q.create_outbound(ACCOUNT, CHAT, "第一段第二段", segments=["第一段", "第二段"])
sent = []


def post(path, data):
    sent.append(data["text"])
    check("出站请求指明账户与任务", data["account"] == ACCOUNT and data["outbound_id"] == task_id)
    w.q.dispatch_outbound_attempt(task_id, ACCOUNT, data["after_seq"])
    return {"ok": True, "submitted": True, "verified": data["text"] == "第一段"}


with patch.object(worker, "post", side_effect=post):
    result = w.send_task(w.q.next_outbound(account=ACCOUNT), sleep_delay=False, reload_cfg=False)
current = w.q.get_outbound(task_id)
check("第二段未确认进入对账", result["status"] == "uncertain" and current["segments_sent"] == 1)
check("记忆里只有确认送达的第一段", w.brain.mem.count(CHAT) == 1)
check("对账先忽略提交水位之前同文旧消息", current["attempt_after_seq"] == 21)
w._reconcile_task(current, now + 2, 8)
check("旧消息不能补成发送成功", w.q.get_outbound(task_id)["status"] == "uncertain")
w.db.rows.append({"sender_username": SELF, "content": "第二段", "create_time": now + 2, "sort_seq": 22})
w._reconcile_task(current, now + 3, 8)
current = w.q.get_outbound(task_id)
check("回读到新消息才结案", current["status"] == "sent" and current["segments_sent"] == 2)
check("两段各发送一次且记忆齐全", sent == ["第一段", "第二段"] and w.brain.mem.count(CHAT) == 2)

race = w.q.create_outbound(ACCOUNT, CHAT, "待核实")
w.q.next_outbound(account=ACCOUNT)
w.q.begin_outbound_attempt(race, 0, 22, now)
w.q.dispatch_outbound_attempt(race, ACCOUNT, 22)
w.q.mark_uncertain(race, "暂未回读")


def operator_cancel(*args):
    w.q.set_outbound_status(race, "cancelled", "人工关闭")
    return True


with patch.object(w, "_verify_sent", side_effect=operator_cancel):
    w._reconcile_task(w.q.get_outbound(race), now + 4, 8)
check("对账不覆盖同步发生的人工操作", w.q.get_outbound(race)["status"] == "cancelled")

section("取消到点提醒及会话变化使待发送的主动消息失效")
cfg = support.base_cfg(proactive={"enabled": True, "whitelist": [CHAT], "active_hours": [0, 0],
                                  "reminders": {"enabled": True}},
                       contacts={CHAT: {"proactive": True}})
w = setup("scheduling", cfg)
rid = w.q.add_reminder(ACCOUNT, CHAT, "喝水", "single", now - 120, anchor_ts=now - 120)
check("常规提醒迟到两分钟仍可以发送", len(reminder.fire_due(w.q, cfg, now=now)) == 1)
queued_reminder = w.q.next_outbound(account=ACCOUNT)
hit, count = reminder.maybe_cancel(w.q, CHAT, "别再提醒我喝水了", account=ACCOUNT)
check("一次性提醒入队后仍可以取消", hit and count == 1 and w.q.get_reminder(rid)["status"] == "cancelled")
w.q.add_reminder(ACCOUNT, CHAT, "站起来", "single", now - 600, anchor_ts=now - 600, short_delay=True)
check("秒级倒计时迟到十分钟保持错过", reminder.fire_due(w.q, cfg, now=now) == [])
deadline = now + 300
w.q.ensure_proactive_state(ACCOUNT, CHAT)
v = w.q.get_proactive_state(ACCOUNT, CHAT)["activity_version"]
old = w.q.create_outbound(ACCOUNT, CHAT, "过期了", kind="proactive", activity_version=v, expires_ts=now - 1)
stale = w.q.create_outbound(ACCOUNT, CHAT, "过时的旧话题", kind="proactive", activity_version=v, expires_ts=deadline)
receive(w.q, 1, "新话题")
with patch.object(worker, "post") as no_send:
    reminder_result = w.send_task(queued_reminder, sleep_delay=False, reload_cfg=False)
    results = [w.send_task(w.q.next_outbound(account=ACCOUNT), sleep_delay=False, reload_cfg=False)
               for _ in range(2)]
    check("已取消提醒和过期、已失效草稿均未请求发送", not no_send.called)
check("已领取的已取消提醒仍被拦截", reminder_result["status"] in ("blocked", "cancelled") and
      w.q.get_outbound(queued_reminder["id"])["status"] == "cancelled")
check("主动任务均按各自原因终止", [r["status"] for r in results] == ["expired", "cancelled"])
check("过期和新消息状态准确", (w.q.get_outbound(old)["status"], w.q.get_outbound(stale)["status"]) == ("expired", "cancelled"))

anchor = datetime(2022, 11, 20, 18, 30)
last = anchor + timedelta(days=365 * 4)
next_run = reminder_parse.advance("daily", int(anchor.timestamp()), int(last.timestamp()))
check("每日提醒超过四年仍保持原定时间", next_run == int((last + timedelta(days=1)).timestamp()))

section("关闭主动聊天或拉黑后，话题提取不会调用模型")
for blocked in (
    {**cfg, "proactive": {**cfg["proactive"], "enabled": False}},
    {**cfg, "agent": {**cfg["agent"], "reply_blacklist": [CHAT]}},
):
    with patch.object(llm, "chat") as model:
        proactive.extract_hooks(w.brain, w.q, ACCOUNT, CHAT, ["私聊原文"], blocked)
        check("未通过门禁时模型不会收到正文", not model.called)

section("运行时知识库开关热更新")
w = setup("kb_toggle")
options = [{**w.cfg, "kb": {"enabled": enabled}} for enabled in (False, True, False)]
delays = []
fake_stop = SimpleNamespace(is_set=lambda: len(delays) == 3, wait=delays.append)
with (patch.object(worker, "_stop", fake_stop),
      patch.object(worker, "load_cfg", side_effect=options),
      patch.object(kb, "scan_inbox", return_value={}) as scan,
      patch.object(kb, "process_jobs", return_value={}) as process):
    w.run_kb_inbox()
    check("只在知识库开启期间处理文件", scan.call_count == process.call_count == 1)

sys.exit(support.finish())
