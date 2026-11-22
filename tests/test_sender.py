# -*- coding: utf-8 -*-
"""发送器：分段进度、失败不重复、全屏重排、暂停拦截、回读核验。"""
import sys
import time

import support
from support import check, section

import reply_worker as RW
from brain import Brain
from wxqueue import OUTBOUND_MAX_ATTEMPTS

ACC = "wxid_test"
CHAT = "wxid_bro"


class FakeDB:
    def __init__(self, rows=None):
        self.rows = rows or []

    def get_messages(self, chat, limit=30):
        return self.rows


_N = [0]


def make_worker(q, cfg=None, rows=None):
    w = RW.Worker.__new__(RW.Worker)          # 跳过 WeChatDB 初始化
    w.cfg = cfg or support.base_cfg()
    w.q = q
    w.brain = Brain(w.cfg)
    _N[0] += 1
    w.brain.mem = support.make_memory("sender%d.db" % _N[0])
    w.me = "me"
    w.db = FakeDB(rows)
    w.acc = ACC
    w._watermark = {}
    return w


section("三段回复，第 2 段失败")
q = support.make_queue("s1.db")
w = make_worker(q)
sent = []


def post_fail_second(path, obj, timeout=90):
    sent.append(obj["text"])
    if len(sent) == 2:
        return {"ok": False, "submitted": False, "retryable": True,
                "error": "聊天窗口里没有输入框"}
    return {"ok": True, "submitted": True, "verified": True}


RW.post = post_fail_second
tid = q.create_outbound(ACC, CHAT, "一。二。三。", segments=["一。", "二。", "三。"])
task = q.next_outbound()
res = w.send_task(task, sleep_delay=False, reload_cfg=False)
check("确认未提交时可重新排队", res["status"] == "draft", str(res))
row = q.get_outbound(tid)
check("只发出 1 段", row["segments_sent"] == 1, str(row["segments_sent"]))
check("状态 draft", row["status"] == "draft", row["status"])
check("记忆只记已发那 1 段", w.brain.mem.count(CHAT) == 1,
      str(w.brain.mem.count(CHAT)))

section("重试只补未发出的段（不重复）")
sent.clear()


def post_ok(path, obj, timeout=90):
    sent.append(obj["text"])
    return {"ok": True, "verified": True}


RW.post = post_ok
q.reset_not_before(tid)
task = q.next_outbound()
res = w.send_task(task, sleep_delay=False, reload_cfg=False)
check("本轮 sent", res["status"] == "sent", str(res))
check("只补第 2、3 段", sent == ["二。", "三。"], str(sent))
check("最终 sent", q.get_outbound(tid)["status"] == "sent")
check("三段记忆齐全", w.brain.mem.count(CHAT) == 3, str(w.brain.mem.count(CHAT)))

section("全屏 → 回 draft 不计数")
q2 = support.make_queue("s2.db")
w2 = make_worker(q2)


def post_fullscreen(path, obj, timeout=90):
    return {"ok": False, "submitted": False, "retryable": True, "reason": "fullscreen",
            "error": "前台全屏，暂不开窗"}


RW.post = post_fullscreen
tid2 = q2.create_outbound(ACC, CHAT, "全屏时的话")
task = q2.next_outbound()
res = w2.send_task(task, sleep_delay=False, reload_cfg=False)
check("返回 draft", res["status"] == "draft", str(res))
row = q2.get_outbound(tid2)
check("attempts 仍为 0", row["attempts"] == 0, str(row["attempts"]))
check("not_before 已设到未来", row["not_before"] > 0)
check("冷却期取不到任务", q2.next_outbound() is None)

section("微信会话列表暂不可读 → 有限重试")
q_retry = support.make_queue("session-retry.db")
w_retry = make_worker(q_retry)
retry_calls = []


def post_unavailable(path, obj, timeout=90):
    retry_calls.append(obj["text"])
    return {"ok": False, "submitted": False, "retryable": True,
            "reason": "session_list_unavailable", "error": "微信会话列表暂不可读"}


RW.post = post_unavailable
retry_id = q_retry.create_outbound(ACC, CHAT, "等待微信界面恢复")
for attempt in range(1, OUTBOUND_MAX_ATTEMPTS + 2):
    q_retry.reset_not_before(retry_id)
    res = w_retry.send_task(q_retry.next_outbound(), sleep_delay=False, reload_cfg=False)
    row = q_retry.get_outbound(retry_id)
    expected = "draft" if attempt <= OUTBOUND_MAX_ATTEMPTS else "hold"
    check("第 %d 次尝试记录真实计数和状态" % attempt,
          row["attempts"] == min(attempt, OUTBOUND_MAX_ATTEMPTS)
          and row["status"] == res["status"] == expected)

check("达到重试上限转人工待审（不落 failed，草稿保留）",
      row["status"] == "hold" and row["failure_code"] == "session_list_unavailable"
      and row["retryable"] == 0
      and row["attempt_started_ts"] is None and q_retry.next_outbound() is None
      and len(retry_calls) == OUTBOUND_MAX_ATTEMPTS + 1)
check("确认未提交的失败可重新批准", q_retry.approve_outbound(retry_id, ACC)
      and q_retry.get_outbound(retry_id)["attempts"] == 0)
RW.post = post_ok
sent.clear()
res = w_retry.send_task(q_retry.next_outbound(), sleep_delay=False, reload_cfg=False)
check("恢复后批准，只发送一次", res["status"] == "sent" and sent == ["等待微信界面恢复"])

section("微信退到托盘 → 保留草稿，打开后恢复发送")
q_tray = support.make_queue("tray-sender.db")
w_tray = make_worker(q_tray)
tray_id = q_tray.create_outbound(ACC, CHAT, "等待打开微信")
tray_message = "微信小号已收起到托盘，请从托盘打开小号主窗口"


def post_tray(path, obj, timeout=90):
    return {"ok": False, "submitted": False, "retryable": True,
            "reason": "main_in_tray", "error": tray_message}


RW.post = post_tray
for _ in range(OUTBOUND_MAX_ATTEMPTS + 1):
    q_tray.reset_not_before(tray_id)
    res = w_tray.send_task(q_tray.next_outbound(), sleep_delay=False, reload_cfg=False)

row = q_tray.get_outbound(tray_id)
check("等待打开不会计为发送失败或消耗重试次数",
      res["status"] == row["status"] == "draft" and row["attempts"] == 0
      and row["error"] == tray_message and row["attempt_started_ts"] is None)
check("等待期间按既有间隔退避且不记录已发送消息",
      q_tray.next_outbound() is None and w_tray.brain.mem.count(CHAT) == 0)
RW.post = post_ok
sent.clear()
q_tray.reset_not_before(tray_id)
res = w_tray.send_task(q_tray.next_outbound(), sleep_delay=False, reload_cfg=False)
check("小号重新打开后只发送一次", res["status"] == "sent" and sent == ["等待打开微信"])

stale_tray_id = q_tray.create_outbound(ACC, CHAT, "等待期间过期")
RW.post = post_tray
w_tray.send_task(q_tray.next_outbound(), sleep_delay=False, reload_cfg=False)
with q_tray._conn() as c:
    c.execute("UPDATE outbound SET created_ts=created_ts-600 WHERE id=?", (stale_tray_id,))
RW.post = post_ok
q_tray.reset_not_before(stale_tray_id)
res = w_tray.send_task(q_tray.next_outbound(), sleep_delay=False, reload_cfg=False)
check("等待期间已过期的回复不会补发", res["status"] == "expired" and sent == ["等待打开微信"])

section("暂停中 → 直接取消，不发送")
q3 = support.make_queue("s3.db")
w3 = make_worker(q3)
q3.pause_chat(CHAT, minutes=5)
tid3 = q3.create_outbound(ACC, CHAT, "暂停期间的话")
task = q3.next_outbound()
res = w3.send_task(task, sleep_delay=False, reload_cfg=False)
check("返回 cancelled", res["status"] == "cancelled", str(res))
check("库里 cancelled", q3.get_outbound(tid3)["status"] == "cancelled")
check("没有写记忆", w3.brain.mem.count(CHAT) == 0)

section("只出草稿的联系人 → hold")
q4 = support.make_queue("s4.db")
cfg4 = support.base_cfg(contacts={CHAT: {"draft_only": True}})
w4 = make_worker(q4, cfg4)
tid4 = q4.create_outbound(ACC, CHAT, "只出草稿")
task = q4.next_outbound()
res = w4.send_task(task, sleep_delay=False, reload_cfg=False)
check("返回 hold", res["status"] == "hold", str(res))
check("库里 hold", q4.get_outbound(tid4)["status"] == "hold")

section("草稿过期 → expired")
q5 = support.make_queue("s5.db")
w5 = make_worker(q5, support.base_cfg(send={"max_draft_age": 1}))
tid5 = q5.create_outbound(ACC, CHAT, "很久以前的草稿")
with q5._conn() as c:
    c.execute("UPDATE outbound SET created_ts=created_ts-600 WHERE id=?", (tid5,))
task = q5.next_outbound()
res = w5.send_task(task, sleep_delay=False, reload_cfg=False)
check("返回 expired", res["status"] == "expired", str(res))
check("库里 expired", q5.get_outbound(tid5)["status"] == "expired")

section("人工批准给普通回复重新计时")
q_approved = support.make_queue("approved-sender.db")
w_approved = make_worker(q_approved, support.base_cfg(send={"max_draft_age": 10}))
approved_id = q_approved.create_outbound(ACC, CHAT, "人工重新批准", status="hold")
stale_id = q_approved.create_outbound(ACC, CHAT, "批准后也已过期", status="hold")
now = int(time.time())
with q_approved._conn() as c:
    c.execute("UPDATE outbound SET created_ts=? WHERE id IN (?,?)",
              (now - 600, approved_id, stale_id))
check("两条旧普通草稿可以进入批准流程",
      q_approved.approve_outbound(approved_id, ACC) and q_approved.approve_outbound(stale_id, ACC))
with q_approved._conn() as c:
    c.execute("UPDATE outbound SET approved_ts=? WHERE id=?", (now - 20, stale_id))
sent.clear()
RW.post = post_ok
fresh = w_approved.send_task(q_approved.next_outbound(), sleep_delay=False, reload_cfg=False)
stale = w_approved.send_task(q_approved.next_outbound(), sleep_delay=False, reload_cfg=False)
check("刚批准的旧草稿可发送", fresh["status"] == "sent" and sent == ["人工重新批准"])
check("超过批准时效的普通回复仍会过期", stale["status"] == "expired")

expired_proactive_id = q_approved.create_outbound(
    ACC, CHAT, "过期主动消息", kind="proactive", expires_ts=now - 1)
with q_approved._conn() as c:
    c.execute("UPDATE outbound SET approved_ts=? WHERE id=?", (now, expired_proactive_id))
res = w_approved.send_task(q_approved.next_outbound(), sleep_delay=False, reload_cfg=False)
check("人工批准不延长主动消息的绝对截止时间",
      res["status"] == "expired" and sent == ["人工重新批准"])

section("回读核验")
w6 = make_worker(support.make_queue("s6.db"))
w6.db = FakeDB([{"sender_username": "me", "content": "你到家了吗", "create_time": 1000, "sort_seq": 25}])
check("库里有这条 → 返回确认序号", w6._verify_sent(CHAT, "你到家了吗", 995) == 25)
check("时间太早 → False", not w6._verify_sent(CHAT, "你到家了吗", 2000))
check("内容不同 → False", not w6._verify_sent(CHAT, "别的", 995))
w6.db = FakeDB([{"sender_username": "对方", "content": "你到家了吗", "create_time": 1000}])
check("别人发的 → False", not w6._verify_sent(CHAT, "你到家了吗", 995))

section("单段回复（不拆气泡）")
q7 = support.make_queue("s7.db")
cfg7 = support.base_cfg(send={"bubble": False})
w7 = make_worker(q7, cfg7)
sent.clear()
RW.post = post_ok
tid7 = q7.create_outbound(ACC, CHAT, "一句完整的话")
task = q7.next_outbound()
res = w7.send_task(task, sleep_delay=False, reload_cfg=False)
check("sent", res["status"] == "sent")
check("只发 1 次", len(sent) == 1, str(sent))

section("群聊出站：at_me 才放行")
cfg_g = support.base_cfg(agent={"group_reply": True, "group_mode": "at_only"})
q8 = support.make_queue("s8.db")
w8 = make_worker(q8, cfg_g)
sent.clear()
RW.post = post_ok
q8.create_outbound(ACC, "123@chatroom", "在呢", segments=["在呢"], at_me=True)
task = q8.next_outbound()
res = w8.send_task(task, sleep_delay=False, reload_cfg=False)
check("被 @ 的群任务能发出去", res["status"] == "sent", str(res))
check("真的调了发送", len(sent) == 1, str(sent))

q9 = support.make_queue("s9.db")
w9 = make_worker(q9, cfg_g)
sent.clear()
q9.create_outbound(ACC, "123@chatroom", "没@也发", segments=["没@也发"])
task = q9.next_outbound()
res = w9.send_task(task, sleep_delay=False, reload_cfg=False)
check("没 @ 的群任务被拦下", res["status"] == "cancelled", str(res))
check("没真发出去", not sent, str(sent))

sys.exit(support.finish())
