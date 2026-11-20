# -*- coding: utf-8 -*-
"""采集回调：自己发的（人工接管）/ 机器人发的 / 普通入站 / 系统消息。"""
import sys
import time

import support
from support import check, section

import reply_worker as RW
from brain import Brain

ACC = "wxid_test"
CHAT = "wxid_bro"
ME = "me"

_N = [0]


def make_worker(cfg=None):
    _N[0] += 1
    w = RW.Worker.__new__(RW.Worker)
    w.cfg = cfg or support.base_cfg()
    w.q = support.make_queue("c%d.db" % _N[0])
    w.brain = Brain(w.cfg)
    w.brain.mem = support.make_memory("cm%d.db" % _N[0])
    w.me = ME
    w.acc = ACC
    w.db = None
    w._watermark = {}
    return w


def msg(content, sender=CHAT, mtype="文本", ts=None, local_id=1, seq=10):
    return {"username": CHAT, "type": mtype, "sender_username": sender,
            "content": content, "create_time": ts or int(time.time()),
            "local_id": local_id, "sort_seq": seq, "shard": "msg0.db"}


section("对方消息 → 入队")
w = make_worker()
w._on_msg(msg("在吗", sender=CHAT, local_id=101, seq=11))
groups = w.q.pending_by_chat()
check("入队成功", len(groups.get(CHAT, [])) == 1, str(groups))
row = groups[CHAT][0]
check("保存了 sort_seq", row["sort_seq"] == 11, str(row["sort_seq"]))
check("保存了 shard", row["shard"] == "msg0.db", str(row["shard"]))
check("状态 pending", row["status"] == "pending", row["status"])
check("不写记忆（等批次处理时写）", w.brain.mem.count(CHAT) == 0,
      str(w.brain.mem.count(CHAT)))

section("同一会话不重复入队")
w._on_msg(msg("在吗", sender=CHAT, local_id=101, seq=11))
check("重复消息被去重", len(w.q.pending_by_chat().get(CHAT, [])) == 1)

section("机器人自己发的 → 不触发接管")
w2 = make_worker()
w2.q.record_robot_sent(CHAT, "机器人说的")
w2._on_msg(msg("机器人说的", sender=ME, local_id=201, seq=21))
check("不暂停", not w2.q.is_paused(CHAT))
check("不入队", not w2.q.pending_by_chat())

section("本人手动发的 → 记记忆，但默认不暂停")
w3 = make_worker()
w3._on_msg(msg("我自己回了一句", sender=ME, local_id=301, seq=31))
check("默认不暂停", not w3.q.is_paused(CHAT))
check("写入 assistant 记忆", w3.brain.mem.count(CHAT) == 1,
      str(w3.brain.mem.count(CHAT)))
rows = w3.brain.mem.recent(CHAT)
check("记忆内容是那句手动回复",
      rows and rows[-1]["content"] == "我自己回了一句" and rows[-1]["role"] == "assistant",
      str(rows))

section("开启接管时才暂停并取消草稿")
cfg_tk = support.base_cfg(takeover={"enabled": True, "pause_minutes": 30})
w4 = make_worker(cfg_tk)
tid = w4.q.create_outbound(ACC, CHAT, "机器人草稿")
w4._on_msg(msg("我来了", sender=ME, local_id=401, seq=41))
check("接管开启 → 暂停", w4.q.is_paused(CHAT))
check("草稿被取消", w4.q.get_outbound(tid)["status"] == "cancelled",
      w4.q.get_outbound(tid)["status"])

section("手动发的非文字消息不会把 XML 写进记忆")
w9 = make_worker()
w9._on_msg(msg('<msg><emoji md5="abc" /></msg>', sender=ME, mtype="动画表情",
               local_id=901, seq=91))
rows = w9.brain.mem.recent(CHAT)
check("写入了一条记忆", len(rows) == 1, str(rows))
check("是描述不是 XML", rows and "<msg>" not in rows[0]["content"], str(rows))
check("角色是 assistant", rows and rows[0]["role"] == "assistant")

section("系统消息跳过")
w5 = make_worker()
w5._on_msg(msg("你已添加了对方", sender=CHAT, mtype="系统消息", local_id=501, seq=51))
check("系统消息不入队", not w5.q.pending_by_chat())

section("非文本类型带 type_code")
w6 = make_worker()
w6._on_msg(msg('<msg><voicemsg/></msg>', sender=CHAT, mtype=34, local_id=601, seq=61))
groups = w6.q.pending_by_chat()
row = groups[CHAT][0]
check("type_code 存下来", row["type_code"] == 34, str(row["type_code"]))
import msgtype
check("能被识别为语音",
      msgtype.kind(row["msg_type"], row["type_code"]) == "voice",
      msgtype.kind(row["msg_type"], row["type_code"]))

section("群消息：剥掉发送者前缀")
w7 = make_worker()


def group_msg(content, sender=CHAT, local_id=701, seq=71):
    return {"username": "123@chatroom", "type": "文本", "sender_username": sender,
            "content": content, "create_time": int(time.time()),
            "local_id": local_id, "sort_seq": seq, "shard": "msg0.db"}


w7._on_msg(group_msg("wxid_example_bro:\n@🍊\u2005出来吧"))
row = w7.q.pending_by_chat()["123@chatroom"][0]
check("入队时剥掉 wxid 前缀", row["content"] == "@🍊\u2005出来吧", repr(row["content"]))

section("群消息：机器人自己的话不触发接管")
w8 = make_worker()
w8.q.record_robot_sent("123@chatroom", "机器人说的")
w8._on_msg(group_msg("wxid_me:\n机器人说的", sender=ME, local_id=801, seq=81))
check("带前缀也能匹配机器人记录", not w8.q.is_paused("123@chatroom"))
w8._on_msg(group_msg("wxid_me:\n我手动说的", sender=ME, local_id=802, seq=82))
check("默认不暂停（接管已关闭）", not w8.q.is_paused("123@chatroom"))

sys.exit(support.finish())
