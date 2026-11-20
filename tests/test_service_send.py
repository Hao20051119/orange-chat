# -*- coding: utf-8 -*-
"""服务发送：机器人自己发的不能再写一遍记忆/机器人登记。"""
import os
import sqlite3
import sys
import time

import support
from support import check, section

import wx_service


class FakeEngine:
    def __init__(self):
        self.sent = []

    def account_is_bound(self):
        return True

    def send(self, chat, text, *, username, preflight):
        blocked = preflight(False) or preflight(True)
        if blocked:
            return blocked
        self.sent.append((chat, text))
        return {"ok": True, "chat": chat, "submitted": True, "t0": time.time()}


class FakeDB:
    def __init__(self):
        self.messages = []

    def get_sessions(self, limit=100):
        return [{"username": "wxid_a", "unread": 0, "summary": ""}]

    def get_nickname(self, uname):
        return "测试联系人"

    def nickname_map(self, refresh=False):
        return {"wxid_a": "测试联系人"}

    def group_id_to_name(self, chatroom):
        return None

    def search_contact(self, kw):
        return []

    def get_messages(self, chat, limit=20):
        return self.messages

    def get_self_info(self):
        return {"username": "me", "nick_name": "🍊"}


def mem_count(text):
    conn = sqlite3.connect(os.environ["WXBOT_MEMORY_DB"])
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content=?", (text,)).fetchone()[0]
    finally:
        conn.close()


def robot_rows(text):
    conn = sqlite3.connect(os.environ["WXBOT_QUEUE_DB"])
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM robot_sent WHERE text=?", (text,)).fetchone()[0]
    finally:
        conn.close()


section("手动发送 → 写记忆 + 机器人登记")
svc = wx_service.Service("wxid_test")
svc.db = FakeDB()
svc.me = "me"
svc.engine = FakeEngine()
svc.token = ""
svc._verify = lambda *a, **k: True          # 跳过回读轮询
wx_service.C.load = lambda **_: support.base_cfg()

r = svc.send("wxid_a", "手动说一句")
check("返回成功", r.get("ok"), str(r))
check("记忆写了一条", mem_count("手动说一句") == 1, str(mem_count("手动说一句")))
check("机器人登记了一条", robot_rows("手动说一句") == 1, str(robot_rows("手动说一句")))

section("机器人自己发的 → 不重复写")
from wxqueue import Queue

q = Queue()
task_id = q.create_outbound(svc.account, "wxid_a", "机器人说一句")
q.next_outbound(account=svc.account)
q.begin_outbound_attempt(task_id, 0, 0)
other = svc.send("wxid_a", "机器人说一句", source="bot",
                 account="wxid_other", outbound_id=task_id)
check("异账号任务到达服务即拦截", other["submitted"] is False)
r2 = svc.send("wxid_a", "机器人说一句", source="bot",
              account=svc.account, outbound_id=task_id)
check("返回成功", r2.get("ok"), str(r2))
check("记忆里没有这条", mem_count("机器人说一句") == 0,
      str(mem_count("机器人说一句")))
check("也不重复登记", robot_rows("机器人说一句") == 0,
      str(robot_rows("机器人说一句")))
check("确实调了发送", len(svc.engine.sent) == 2, str(svc.engine.sent))
again = svc.send("wxid_a", "机器人说一句", source="bot",
                 account=svc.account, outbound_id=task_id)
check("同一个分段仅能领取一次", again["submitted"] is None and len(svc.engine.sent) == 2)

section("服务发送复核人工批准时间")
old_ts = int(time.time()) - 600
approved_id = q.create_outbound(svc.account, "wxid_a", "人工批准的旧回复")
q.finish_outbound(approved_id, "failed", "会话列表里没有 '测试联系人'", retryable=0)
with q._conn() as conn:
    conn.execute("UPDATE outbound SET created_ts=? WHERE id=?", (old_ts, approved_id))
approval = svc.draft_action(approved_id, "approve")
check("可从待处理批准明确未提交的旧回复", approval.get("ok"), approval)
q.next_outbound(account=svc.account)
q.begin_outbound_attempt(approved_id, 0, 0)
approved_result = svc.send("wxid_a", "人工批准的旧回复", source="bot",
                           account=svc.account, outbound_id=approved_id)
check("刚批准后通过服务端两次发送前复核", approved_result.get("verified"), approved_result)

unapproved_id = q.create_outbound(svc.account, "wxid_a", "尚未批准的旧回复")
with q._conn() as conn:
    conn.execute("UPDATE outbound SET created_ts=? WHERE id=?", (old_ts, unapproved_id))
q.next_outbound(account=svc.account)
q.begin_outbound_attempt(unapproved_id, 0, 0)
unapproved_result = svc.send("wxid_a", "尚未批准的旧回复", source="bot",
                             account=svc.account, outbound_id=unapproved_id)
check("未批准的旧回复仍然过期",
      unapproved_result.get("submitted") is False
      and unapproved_result.get("outbound_status") == "expired", unapproved_result)

section("手动连发两句不会互相吞")
svc.send("wxid_a", "第一句")
svc.send("wxid_a", "第二句")
check("两句都在", mem_count("第一句") == 1 and mem_count("第二句") == 1)

section("提交后未回读")
svc._verify = lambda *a, **k: False
r3 = svc.send("wxid_a", "暂未确认")
check("回读缺失明确返回待确认", r3["submitted"] and not r3["verified"])
check("回读缺失不写助手记忆", mem_count("暂未确认") == 0)

section("HTTP：Host 头只认回环（防 DNS rebinding）")


def host_ok(value):
    h = object.__new__(wx_service.Handler)
    h.headers = {"Host": value}
    return wx_service.Handler._host_ok(h)


check("允许 127.0.0.1:8765", host_ok("127.0.0.1:8765"))
check("允许 localhost", host_ok("localhost:8765"))
check("允许 [::1]:8765", host_ok("[::1]:8765"))
check("拦住外部域名（rebinding）", not host_ok("evil.example.com"))
check("拦住伪装成回环的外部域名", not host_ok("attacker.test:8765"))
check("无 Host 头（HTTP/1.0 本机工具）放行", host_ok(""))

sys.exit(support.finish())
