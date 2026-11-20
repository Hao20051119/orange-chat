# -*- coding: utf-8 -*-
"""入站身份：`local_id` 会撞号，去重只能按 (account, chat, local_id, msg_ts)。

事故背景（2026-11-21 00:40）：微信重建消息表后 `local_id` 从 1 重新编号，新消息
`local_id=154` 和 11-19 的旧消息撞号；旧代码用 `(shard, local_id)` 预检去重，
把新消息静默丢掉，而采集水位照样前进 → 消息永久丢失、日志一片空白。
"""
import sys

import support
from support import check, section

from wxqueue import Queue  # noqa: E402  （support 已经准备好临时库和 pylibs）

ACC, CHAT = "wxid_test", "wxid_contact"
SHARD = "message__message_0.db"
OLD_TS, NEW_TS = 1795022138, 1795192848        # 11-19 01:15:38 / 11-21 00:40:48

section("同 local_id、不同 msg_ts 必须都能入队（撞号回归）")
q = support.make_queue("identity.db")
check("旧消息（11-19）先入队",
      q.enqueue_inbound(ACC, CHAT, 154, OLD_TS, "文本", "人有善恶…", shard=SHARD))
check("撞号的新消息（11-21）也要入队——修复前这里会被静默丢掉",
      q.enqueue_inbound(ACC, CHAT, 154, NEW_TS, "文本", "测试消息A", shard=SHARD))
check("同 (local_id, msg_ts) 才是真重复",
      not q.enqueue_inbound(ACC, CHAT, 154, NEW_TS, "文本", "测试消息A", shard=SHARD))
with q._conn() as c:
    rows = c.execute("SELECT local_id, msg_ts, status FROM inbound WHERE chat=? "
                     "ORDER BY msg_ts", (CHAT,)).fetchall()
check("两条都在库里且都是 pending",
      [(r["local_id"], r["msg_ts"]) for r in rows] == [("154", OLD_TS), ("154", NEW_TS)]
      and all(r["status"] == "pending" for r in rows),
      str([(r["local_id"], r["msg_ts"], r["status"]) for r in rows]))

section("去重契约 = 表约束 (account, chat, local_id, msg_ts)，shard 不参与")
q2 = support.make_queue("identity2.db")
check("shard A 入队", q2.enqueue_inbound(ACC, CHAT, 7, NEW_TS, "文本", "A", shard="m0.db"))
check("同 local_id+msg_ts 的 shard B 视为同一条（与表约束一致，不会重复入队）",
      not q2.enqueue_inbound(ACC, CHAT, 7, NEW_TS, "文本", "B", shard="m1.db"))
check("完全相同才幂等",
      not q2.enqueue_inbound(ACC, CHAT, 7, NEW_TS, "文本", "A", shard="m0.db"))

section("采集水位可以显式回退（用于重放被误判丢弃的消息）")
q3 = support.make_queue("watermark.db")
q3.enqueue_inbound(ACC, CHAT, 154, NEW_TS, "文本", "测试消息A", shard=SHARD)
q3.set_collect_watermark(ACC, CHAT, NEW_TS)
check("水位先记到最新一条", q3.get_collect_watermark(ACC, CHAT) == NEW_TS)
old = q3.rewind_collect_watermark(ACC, CHAT, NEW_TS - 1)
check("回退返回旧水位", old == NEW_TS, str(old))
check("水位确实回退了", q3.get_collect_watermark(ACC, CHAT) == NEW_TS - 1)
check("回退后重扫已入过的消息是幂等的（不会重复入队）",
      not q3.enqueue_inbound(ACC, CHAT, 154, NEW_TS, "文本", "测试消息A", shard=SHARD))

sys.exit(support.finish())
