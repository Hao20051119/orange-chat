# -*- coding: utf-8 -*-
"""队列 v2：排序、去重、退避重试、出站状态机、崩溃恢复、机器人 intent。"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import support
from support import check, section

q = support.make_queue()
ACC = "wxid_test"

section("入队与排序（同秒多条）")
ts = int(time.time())
for i in range(3):
    q.enqueue_inbound(ACC, "chatA", 100 + i, ts, "文本", "同秒%d" % i,
                      sort_seq=30 + i)
groups = q.pending_by_chat()
check("聚合到同一会话", list(groups.keys()) == ["chatA"], str(groups.keys()))
check("按 sort_seq 保序", [m["content"] for m in groups["chatA"]] ==
      ["同秒0", "同秒1", "同秒2"], str([m["content"] for m in groups["chatA"]]))

check("同 shard+local_id 去重",
      not q.enqueue_inbound(ACC, "chatA", 100, ts, "文本", "重复", shard="msg0.db"))

section("重试退避")
mid = groups["chatA"][0]["id"]
n1 = q.mark_retry(mid, delay=1)
check("第 1 次 → retry", n1 == 1 and q.pending_inbound_count() >= 3, str(n1))
time.sleep(1.2)
pending_now = q.pending_by_chat()
check("退避到期后回到待处理", any(m["id"] == mid for m in pending_now.get("chatA", [])))
for _ in range(3):
    q.mark_retry(mid, delay=0)
with q._conn() as c:
    st = c.execute("SELECT status, retry_count FROM inbound WHERE id=?", (mid,)).fetchone()
check("超过上限 → failed", st["status"] == "failed" and st["retry_count"] >= 4,
      "%s/%s" % (st["status"], st["retry_count"]))

section("出站状态机")
tid = q.create_outbound(ACC, "chatA", "你好呀", segments=["你好呀"])
task = q.next_outbound()
check("取到任务并置 sending", task and task["id"] == tid and task["status"] == "draft")
with q._conn() as c:
    row = c.execute("SELECT status FROM outbound WHERE id=?", (tid,)).fetchone()
check("库里已变 sending", row["status"] == "sending", row["status"])

n = q.recover_inflight(age=-1)
check("崩溃恢复把 sending 转 uncertain", n == 1, str(n))
check("状态 = uncertain", q.get_outbound(tid)["status"] == "uncertain")

q.finish_outbound(tid, "sent", verified=1)
check("置为 sent", q.get_outbound(tid)["status"] == "sent")

section("not_before 与重排")
tid2 = q.create_outbound(ACC, "chatB", "全屏时的话")
task2 = q.next_outbound()
check("取到第二条", task2 and task2["id"] == tid2)
q.requeue_outbound(tid2, delay=60, note="fullscreen")
check("重排后 60 秒内不取", q.next_outbound() is None)
row2 = q.get_outbound(tid2)
check("回到 draft 且 attempts=0", row2["status"] == "draft" and row2["attempts"] == 0,
      "%s/%s" % (row2["status"], row2["attempts"]))

section("分段进度")
tid3 = q.create_outbound(ACC, "chatC", "第一句。第二句。第三句。",
                         segments=["第一句。", "第二句。", "第三句。"])
q.bump_segments(tid3, 2)
check("segments_sent 记录进度", q.get_outbound(tid3)["segments_sent"] == 2,
      str(q.get_outbound(tid3)["segments_sent"]))

section("重发只接受确定未提交的开窗失败")
from wxqueue import Queue

approval = support.make_queue("approval.db")
legacy = approval.create_outbound(ACC, "chatA", "旧失败记录")
legacy_error = "会话列表里没有 %r" % "代号 'A'"
approval.finish_outbound(legacy, "failed", legacy_error, retryable=0)
with approval._conn() as conn:
    conn.execute("ALTER TABLE outbound DROP COLUMN failure_code")
approval = Queue(approval.db_path)
check("升级旧库保留记录，补充失败代码列",
      approval.get_outbound(legacy)["failure_code"] is None)
check("旧 no_row 不会自动重发，也不能跨账号批准",
      approval.next_outbound(account=ACC) is None
      and not approval.approve_outbound(legacy, "wxid_other"))

def concurrent_approval(text):
    return approval.approve_outbound(legacy, ACC, text=text)

with ThreadPoolExecutor(max_workers=2) as pool:
    edits = ["版本一", "版本二"]
    outcomes = list(pool.map(concurrent_approval, edits))
row = approval.get_outbound(legacy)
check("并发批准只有一次成功，发送内容来自成功的请求",
      outcomes.count(True) == 1 and row["text"] == edits[outcomes.index(True)]
      and row["approved_ts"] is not None)

partial = approval.create_outbound(ACC, "chatA", "第一段\n第二段",
                                   segments=["第一段", "第二段"])
approval.bump_segments(partial, 1)
approval.finish_outbound(partial, "failed", "新机当前读不到会话列表",
                         retryable=0, failure_code="session_list_unavailable")
check("已发段落的草稿可以改，但只替换剩余部分",
      approval.update_outbound_text(partial, "改写", ACC))
row = approval.get_outbound(partial)
check("保留已发第一段、新正文接在后面",
      json.loads(row["segments"] or "[]") == ["第一段", "改写"],
      str(row["segments"]))
check("批准时不能再带正文覆盖已发段",
      not approval.approve_outbound(partial, ACC, text="再改一次"))
check("允许原文只续发未提交的分段", approval.approve_outbound(partial, ACC))
row = approval.get_outbound(partial)
check("分段进度不退回，尝试次数重新计算",
      row["segments_sent"] == 1
      and json.loads(row["segments"] or "[]") == ["第一段", "改写"]
      and row["attempts"] == 0)

unsafe = approval.create_outbound(ACC, "chatB", "未经确认")
approval.finish_outbound(unsafe, "failed", legacy_error + "：无法核验", retryable=0)
check("不放行仅前缀相同的未知失败", not approval.approve_outbound(unsafe, ACC))
approval.finish_outbound(unsafe, "failed", legacy_error, verified=1, retryable=0)
check("不重发已核实的记录", not approval.approve_outbound(unsafe, ACC))
for status in ("sending", "uncertain", "unverified", "sent"):
    candidate = approval.create_outbound(ACC, "chatB", "状态不允许")
    approval.finish_outbound(candidate, status, legacy_error, retryable=0)
    check("状态 %s 不允许直接重发" % status, not approval.approve_outbound(candidate, ACC))

pending = approval.create_outbound(ACC, "chatC", "等待核验")
approval.set_outbound_status(pending, "sending")
approval.begin_outbound_attempt(pending, 0, 20)
approval.dispatch_outbound_attempt(pending, ACC, 20)
approval.finish_outbound(pending, "failed", legacy_error, retryable=0)
check("仍有提交标记的失败不可重发", not approval.approve_outbound(pending, ACC))

stale = approval.create_outbound(ACC, "chatD", "过期主动消息", kind="proactive",
                                 expires_ts=int(time.time()) - 1)
approval.finish_outbound(stale, "failed", legacy_error, retryable=0)
check("过期主动消息继续保持截止时间", not approval.approve_outbound(stale, ACC))

section("机器人发言 intent（发送前登记）")
ts0 = int(time.time())
iid = q.record_robot_intent("chatA", "机器人说的", outbound_id=1, ts=ts0)
check("intent 阶段就算机器人",
      q.was_robot_sent("chatA", "机器人说的", ts0 + 5))
q.confirm_robot_sent(iid, ts=ts0 + 2)
check("确认后仍匹配", q.was_robot_sent("chatA", "机器人说的", ts0 + 10))
q.drop_robot_intent(iid)
check("已确认的不会被 drop 删掉",
      q.was_robot_sent("chatA", "机器人说的", ts0 + 10))
iid2 = q.record_robot_intent("chatA", "没发出去的", outbound_id=2, ts=ts0)
q.drop_robot_intent(iid2)
check("发送失败可撤回 intent", not q.was_robot_sent("chatA", "没发出去的", ts0))

# 群里的 @ 会被微信存成 U+2005，原文比对会漏 → 现在按去空白比较
stamp = int(time.time())
q.record_robot_sent("chatA", "@🥭 请教是这么请教的", ts=stamp)
check("群里带 mention 的也能认出来",
      q.was_robot_sent("chatA", "@🥭\u2005请教是这么请教的", stamp + 3))
check("内容不同仍不算",
      not q.was_robot_sent("chatA", "完全不一样的话", stamp + 3))

section("会话锁")
check("首次可获取", q.acquire_chat("chatLock"))
check("持有期间别人拿不到", not q.acquire_chat("chatLock"))
check("清锁后立刻可再获取", q.clear_locks() >= 1 and q.acquire_chat("chatLock"))
q.release_chat("chatLock")

section("最近一条已发回复")
q.finish_outbound(tid, "sent", verified=1)
last = q.last_sent_for("chatA")
check("能取到最近一条", last and last["text"] == "你好呀", str(last))
check("没发过的会话为空", q.last_sent_for("没有这个会话") is None)

section("暂停与统计")
q.pause_chat("chatA", minutes=5)
check("暂停生效", q.is_paused("chatA"))
check("暂停列表", [x["chat"] for x in q.paused_chats()] == ["chatA"])
q.resume_chat("chatA")
check("恢复", not q.is_paused("chatA"))
st = q.stats()
for key in ("inbound_pending", "outbound_hold", "outbound_uncertain",
            "outbound_sent", "outbound_failed", "outbound_expired"):
    check("stats 有 %s" % key, key in st, str(list(st)))

section("过期清理")
q.enqueue_inbound(ACC, "chatOld", 1, int(time.time()) - 100000, "文本", "很久以前")
n = q.expire_inbound(max_age=3600)
check("超龄消息标过期", n >= 1, str(n))

sys.exit(support.finish())
