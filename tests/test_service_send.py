# -*- coding: utf-8 -*-
"""服务发送：机器人自己发的不能再写一遍记忆/机器人登记。"""
import os
import sqlite3
import sys
import time
from unittest.mock import patch

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
# 「未提交、可重试」的失败才允许人工放行——明确未提交（retryable=1）
q.finish_outbound(approved_id, "failed", "会话列表里没有 '测试联系人'", retryable=1)
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

section("控制台手工每周提醒：星期按联系人当地算")
import reminder_parse
from datetime import datetime

q_tz = Queue()
q_tz.set_chat_tz(svc.account, "wxid_a", 7200)      # 对方比主机快两小时
mon_23 = int(datetime(2026, 11, 23, 23, 0).timestamp())   # 主机周一 23:00
rule = wx_service.local_weekday_rule(q_tz, svc.account, "wxid_a", "weekly:1", mon_23)
check("星期换成联系人当地的周二", rule == "weekly:2", rule)
nxt = reminder_parse.advance(rule, mon_23, mon_23, 7200)
check("下一次仍是主机周一 23:00", nxt == mon_23 + 7 * 86400,
      datetime.fromtimestamp(nxt).strftime("%m-%d %H:%M") if nxt else "None")
check("不重算就会漂到周日（旧行为对照）",
      reminder_parse.advance("weekly:1", mon_23, mon_23, 7200) != mon_23 + 7 * 86400)
check("没时差时不改 rule",
      wx_service.local_weekday_rule(q_tz, svc.account, "wxid_other", "weekly:1", mon_23)
      == "weekly:1")

section("控制台提醒列表优先显示可操作的计划")
for i in range(105):
    rid = q_tz.add_reminder(svc.account, "wxid_a", f"历史{i}", "single", None)
    q_tz.update_reminder(rid, status="done")
active_id = q_tz.add_reminder(svc.account, "wxid_a", "仍有效", "single", mon_23)
paused_id = q_tz.add_reminder(svc.account, "wxid_a", "可恢复", "single", mon_23)
q_tz.update_reminder(paused_id, status="paused")
q_tz.add_reminder("wxid_other", "wxid_a", "另一账号", "single", mon_23)
wx_service.SERVICE = svc
handler = object.__new__(wx_service.Handler)
handler.headers = {"Host": "127.0.0.1"}
handler._json = lambda result, code=200: (result, code)
handler.path = "/reminders"
payload, code = handler.do_GET()
check("真实 GET 默认列表不被历史挤满", code == 200
      and [item["id"] for item in payload["items"][:2]] == [active_id, paused_id]
      and len(payload["items"]) == 100
      and all(item["account"] == svc.account for item in payload["items"]))
handler.path = "/reminders?status=done"
payload, code = handler.do_GET()
check("显式历史过滤仍生效", code == 200 and len(payload["items"]) == 100
      and all(item["status"] == "done" for item in payload["items"]))

section("发送回读同时返回实际消息序号")


class ReadbackEngine(FakeEngine):
    def send(self, chat, text, **kwargs):
        result = super().send(chat, text, **kwargs)
        if result.get("submitted"):
            svc.db.messages.insert(0, {"sender_username": svc.me, "content": text, "sort_seq": 31})
        return result


svc.engine = ReadbackEngine()
svc._verify = wx_service.Service._verify.__get__(svc)
svc.db.messages = [{"sender_username": svc.me, "content": "真实回读", "sort_seq": 12}]
result = svc.send("wxid_a", "真实回读")
check("verified 保持布尔，忽略水位之前的同文消息",
      result.get("verified") is True and result.get("after_seq") == 12
      and result.get("verified_seq") == 31, result)

section("核对问句独立发送并保留人工审核")
import reminder

cfg = support.base_cfg(agent={"auto_reply": False}, proactive={"reminders": {"enabled": True}},
                       contacts={"wxid_a": {"draft_only": True}})
wx_service.C.load = lambda **_: cfg
svc._verify = lambda *_a, **_kw: 45
svc.engine = FakeEngine()
base = int(time.time())
ask = "几点提醒你开会？"
clarify_id = q_tz.add_clarify(svc.account, "wxid_a", "开会", base, ask)
prompt_id = reminder.enqueue_clarify_prompt(q_tz, svc.account, "wxid_a")
q_tz.next_outbound(kinds=("reminder_clarify",), account=svc.account)
q_tz.begin_outbound_attempt(prompt_id, 0, 0)
result = svc.send("wxid_a", ask, source="reminder_clarify",
                  account=svc.account, outbound_id=prompt_id)
check("联系人只出草稿时不能直接发核对", result["submitted"] is False
      and result["outbound_status"] == "hold", result)
q_tz.clear_outbound_attempt(prompt_id)
q_tz.finish_outbound(prompt_id, "hold", bump_attempt=False)
check("不允许把人工编辑的核对时间当成真实排程",
      not svc.draft_action(prompt_id, "approve", "改到八点行吗？")["ok"])
check("自动回复关闭仍可批准原始核对问句", svc.draft_action(prompt_id, "approve")["ok"])
q_tz.next_outbound(kinds=("reminder_clarify",), account=svc.account)
q_tz.begin_outbound_attempt(prompt_id, 0, 0)
result = svc.send("wxid_a", ask, source="reminder_clarify",
                  account=svc.account, outbound_id=prompt_id)
check("批准后通过提醒门禁，并返回核验水位", result.get("verified_seq") == 45, result)

section("代传话核对独立发送与真实回读")
import relay

ask = "你要我转告测试同学吗？"
relay_id = q_tz.add_relay_clarify(svc.account, "wxid_a", "你好", ["测试同学"], ask,
                                  confirm_chat="wxid_other")
prompt_id = q_tz.enqueue_relay_clarify_prompt(svc.account, "wxid_a", ask)
with patch.object(relay, "_relations", return_value=[]):
    check("发起人离开关系网后不再发送核对",
          bool(relay.clarify_send_block_reason(q_tz, q_tz.get_outbound(prompt_id))))
relay_relations = patch.object(relay, "_relations",
                               return_value=[{"name": "发起人", "chat": "wxid_a"}])
relay_relations.start()
check("入队不会将问句算成已送达",
      q_tz.get_relay_clarify(svc.account, "wxid_a")["asks"] == 0)
q_tz.next_outbound(kinds=("relay_clarify",), account=svc.account)
q_tz.begin_outbound_attempt(prompt_id, 0, 0)
result = svc.send("wxid_a", ask, source="relay_clarify",
                  account=svc.account, outbound_id=prompt_id)
check("代传话核对尊重联系人草稿开关",
      result["submitted"] is False and result["outbound_status"] == "hold", result)
q_tz.clear_outbound_attempt(prompt_id)
q_tz.finish_outbound(prompt_id, "hold", bump_attempt=False)
check("代传话核对不能手动改字或标为已发送",
      not svc.draft_action(prompt_id, "approve", "确定吗？")["ok"]
      and not svc.draft_action(prompt_id, "mark_sent")["ok"])
cfg["proactive"]["relay"] = {"enabled": False}
check("代传功能已关闭时不能批准核对",
      not svc.draft_action(prompt_id, "approve")["ok"])
cfg["proactive"]["relay"]["enabled"] = True
check("关闭普通自动回复仍可批准代传话核对",
      svc.draft_action(prompt_id, "approve")["ok"])
q_tz.next_outbound(kinds=("relay_clarify",), account=svc.account)
q_tz.begin_outbound_attempt(prompt_id, 0, 0)
result = svc.send("wxid_a", ask, source="relay_clarify",
                  account=svc.account, outbound_id=prompt_id)
check("服务对代传话核对返回实际回读序号", result.get("verified_seq") == 45, result)
q_tz.bump_segments(prompt_id, 1, complete=True, sent_after_seq=result.get("verified_seq"))
record = q_tz.get_relay_clarify(svc.account, "wxid_a")
check("核验完成后才有送达证明并累计一次",
      record["prompt_sent_ts"] and record["prompt_verified_seq"] == 45
      and record["asks"] == 1 and record["confirm_chat"] == "wxid_other", record)
check("换一轮确认会清除旧问句的核验依据",
      q_tz.advance_relay_clarify(relay_id, [], "你要我转给谁？")
      and q_tz.get_relay_clarify(svc.account, "wxid_a")["prompt_sent_ts"] is None
      and q_tz.get_relay_clarify(svc.account, "wxid_a")["asks"] == 1)
old_prompt_id = q_tz.enqueue_relay_clarify_prompt(svc.account, "wxid_a", "你要我转给谁？")
q_tz.add_relay_clarify(svc.account, "wxid_a", "改为这条", [], "要转达给谁？")
check("新委托撤回上一条还未发送的核对",
      q_tz.get_outbound(old_prompt_id)["status"] == "cancelled"
      and q_tz.get_relay_clarify_by_id(relay_id)["status"] == "cancelled")
relay_relations.stop()

sys.exit(support.finish())
