# -*- coding: utf-8 -*-
"""暂停语义：暂停期不丢消息、解禁后补答、积压只答最新 N 条。"""
import sys
import time

import support
from support import check, section

import gate

q = support.make_queue("pause.db")
ACC = "wxid_test"
CHAT = "wxid_bro"

section("暂停期间消息保持待处理")
now = int(time.time())
for i in range(6):
    q.enqueue_inbound(ACC, CHAT, 500 + i, now - 30 + i, "文本", "第%d条" % i)

q.pause_chat(CHAT, minutes=30)
check("is_paused 为真", q.is_paused(CHAT))
groups = q.pending_by_chat()
check("暂停不影响入队（仍能看到 6 条）", len(groups.get(CHAT, [])) == 6,
      str(len(groups.get(CHAT, []))))
check("状态仍是 pending（没有被 skip 掉）",
      all(m["status"] == "pending" for m in groups[CHAT]))

# 模拟 worker：暂停时直接跳过，不动状态
processed = 0
for chat, msgs in groups.items():
    if q.is_paused(chat):
        continue
    processed += 1
check("暂停时一条都不处理", processed == 0)

section("解禁后补答最新 5 条")
q.resume_chat(CHAT)
check("恢复后不再暂停", not q.is_paused(CHAT))
msgs = q.newest_pending(CHAT, limit=100)
kept, expired = gate.backfill(msgs, keep=5, max_age=21600, now=time.time())
q.mark_inbounds(expired, "expired")
check("只补 5 条", len(kept) == 5, str(len(kept)))
check("最旧那条过期", expired == [msgs[0]["id"]], str(expired))

with q._conn() as c:
    st = c.execute("SELECT status FROM inbound WHERE id=?", (expired[0],)).fetchone()
check("库里标 expired", st["status"] == "expired", st["status"])

section("超龄消息（>6 小时）不补")
q2 = support.make_queue("pause2.db")
old_ts = int(time.time()) - 7 * 3600
q2.enqueue_inbound(ACC, "wxid_x", 1, old_ts, "文本", "昨天的话")
q2.enqueue_inbound(ACC, "wxid_x", 2, int(time.time()), "文本", "刚才的话")
msgs = q2.newest_pending("wxid_x", limit=50)
kept, expired = gate.backfill(msgs, keep=5, max_age=21600)
check("超龄剔除", [m["content"] for m in kept] == ["刚才的话"], str(kept))
check("超龄进过期", expired == [msgs[0]["id"]], str(expired))

section("暂停不阻断出站门禁（发送前会 cancel）")
cfg = support.base_cfg()
check("暂停时 gate = cancel",
      gate.evaluate(cfg, CHAT, paused=True)["action"] == "cancel")

sys.exit(support.finish())
