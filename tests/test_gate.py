# -*- coding: utf-8 -*-
"""门禁：暂停 / 开关 / 白名单 / 时段 / 只出草稿 / 草稿过期。"""
import sys
import time

import support
from support import check, section

import gate

section("gate.identity_ok")
cfg = support.base_cfg()
check("自己发的 → 拒绝", not gate.identity_ok(cfg, "wxid_a", from_me=True)[0])
cfg1 = support.base_cfg(agent={"auto_reply": False})
check("关自动回复 → 拒绝", not gate.identity_ok(cfg1, "wxid_a")[0])
cfg2 = support.base_cfg(agent={"reply_whitelist": ["wxid_ok"]})
check("不在白名单 → 拒绝", not gate.identity_ok(cfg2, "wxid_other")[0])
check("在白名单 → 通过", gate.identity_ok(cfg2, "wxid_ok")[0])
cfg3 = support.base_cfg(agent={"reply_blacklist": ["wxid_bad"]})
check("黑名单 → 拒绝", not gate.identity_ok(cfg3, "wxid_bad")[0])
check("白名单为空 → 所有私聊都回",
      gate.identity_ok(support.base_cfg(agent={"reply_whitelist": []}),
                       "wxid_anyone")[0])
check("文件传输助手不回",
      not gate.identity_ok(support.base_cfg(), "filehelper")[0])
check("公众号不回", not gate.identity_ok(support.base_cfg(), "gh_abc123")[0])
check("微信团队不回", not gate.identity_ok(support.base_cfg(), "weixin")[0])
check("可以关掉系统账号过滤",
      gate.identity_ok(support.base_cfg(agent={"skip_system_accounts": False}),
                       "filehelper")[0])
check("自定义前缀生效",
      not gate.identity_ok(support.base_cfg(agent={
          "system_account_prefixes": ["gh_", "bot_"]}), "bot_x")[0])
cfg4 = support.base_cfg(agent={"group_reply": False})
check("群聊未开 → 拒绝", not gate.identity_ok(cfg4, "123@chatroom")[0])
check("群聊开但没@ → 拒绝",
      not gate.identity_ok(support.base_cfg(agent={"group_reply": True}),
                           "123@chatroom")[0],
      str(gate.identity_ok(support.base_cfg(agent={"group_reply": True}),
                           "123@chatroom")))
check("群聊开且被@ → 通过",
      gate.identity_ok(support.base_cfg(agent={"group_reply": True}),
                       "123@chatroom", at_me=True)[0])

section("gate.evaluate")
cfg = support.base_cfg()
check("正常 → ok", gate.evaluate(cfg, "wxid_a")["action"] == "ok")
check("暂停 → cancel", gate.evaluate(cfg, "wxid_a", paused=True)["action"] == "cancel")
cfg = support.base_cfg(contacts={"wxid_c": {"paused": True}})
check("联系人暂停 → hold", gate.evaluate(cfg, "wxid_c")["action"] == "hold")
cfg = support.base_cfg(contacts={"wxid_d": {"draft_only": True}})
check("只出草稿 → hold", gate.evaluate(cfg, "wxid_d")["action"] == "hold")

now = time.mktime((2026, 1, 1, 23, 30, 0, 0, 0, -1))
cfg = support.base_cfg(contacts={"wxid_e": {"active_hours": [9, 18]}})
check("时段外（23:30）→ hold",
      gate.evaluate(cfg, "wxid_e", now=now)["action"] == "hold")
cfg = support.base_cfg(contacts={"wxid_e": {"active_hours": [22, 6]}})
check("跨零点时段（23:30 在 22-6 内）→ ok",
      gate.evaluate(cfg, "wxid_e", now=now)["action"] == "ok")
check("in_hours 白天在 22-6 外", not gate.in_hours([22, 6],
                                                  time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, -1))))

cfg = support.base_cfg(send={"max_draft_age": 300})
check("草稿 10 分钟 → 过期 cancel",
      gate.evaluate(cfg, "wxid_a", created_ts=time.time() - 600)["action"] == "cancel")
check("草稿 10 秒 → ok",
      gate.evaluate(cfg, "wxid_a", created_ts=time.time() - 10)["action"] == "ok")
cfg = support.base_cfg(send={"max_draft_age": 0})
check("max_draft_age=0 表示不限期",
      gate.evaluate(cfg, "wxid_a", created_ts=time.time() - 99999)["action"] == "ok")

section("gate.backfill")
now = time.time()
msgs = [{"id": i, "msg_ts": int(now) - 60 + i} for i in range(1, 9)]
kept, expired = gate.backfill(msgs, keep=5, max_age=3600, now=now)
check("只保留最新 5 条", len(kept) == 5 and kept[0]["id"] == 4, str([m["id"] for m in kept]))
check("更旧的 3 条被标过期", expired == [1, 2, 3], str(expired))

old = [{"id": 10, "msg_ts": int(now) - 7200}, {"id": 11, "msg_ts": int(now)}]
kept, expired = gate.backfill(old, keep=5, max_age=3600, now=now)
check("超龄消息剔除", [m["id"] for m in kept] == [11] and expired == [10])

sys.exit(support.finish())
