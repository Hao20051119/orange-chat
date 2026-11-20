# -*- coding: utf-8 -*-
"""黑名单语义：默认全回，只有拉黑的人/群不回。"""
import json
import os
import sys

import support
from support import check, section

import config as C
import gate
import wx_service

# 配置写到临时文件，别动真配置
TMP_CFG = os.path.join(support.TMP, "bl_config.json")
C.CONFIG_PATH = TMP_CFG
C._state.update({"mtime": 0, "config": None})
base = support.base_cfg(agent={"auto_reply": True, "reply_whitelist": [],
                               "reply_blacklist": [],
                               "group_reply": True, "group_mode": "at_only"})
check("准备配置", C.save(base))

section("默认：除了系统号，谁都会回")
cfg = C.load(force=True)
check("陌生人私聊 → 回", gate.identity_ok(cfg, "wxid_anyone")[0])
check("群聊被@ → 回", gate.identity_ok(cfg, "123@chatroom", at_me=True)[0])
check("群聊没@ → 不回", not gate.identity_ok(cfg, "123@chatroom")[0])
check("文件传输助手 → 不回", not gate.identity_ok(cfg, "filehelper")[0])

section("拉黑后不再回（人和群都适用）")
svc = wx_service.Service("wxid_test")
svc.db = None
svc.token = ""
r = svc.blacklist_action("wxid_bad", "add")
check("加入私聊黑名单", r.get("ok") and "wxid_bad" in r["blacklist"], str(r))
r2 = svc.blacklist_action("123@chatroom", "add")
check("加入群黑名单", r2.get("ok") and "123@chatroom" in r2["blacklist"], str(r2))
check("重复添加不产生重复", len([x for x in r2["blacklist"]
                              if x == "wxid_bad"]) == 1, str(r2["blacklist"]))

cfg = C.load(force=True)
check("被拉黑的人 → 不回", not gate.identity_ok(cfg, "wxid_bad")[0])
check("被拉黑的群 → 不回（即使被@）",
      not gate.identity_ok(cfg, "123@chatroom", at_me=True)[0])
check("其他人不受影响", gate.identity_ok(cfg, "wxid_ok")[0])
check("其他群不受影响",
      gate.identity_ok(cfg, "456@chatroom", at_me=True)[0])
check("统计能看到两个", svc.blacklist()["count"] == 2, str(svc.blacklist()))

section("移出黑名单")
r3 = svc.blacklist_action("wxid_bad", "remove")
check("移出后名单剩 1 个", r3.get("ok") and r3["count"] == 1, str(r3))
cfg = C.load(force=True)
check("移出后又能回", gate.identity_ok(cfg, "wxid_bad")[0])

chapter = svc.blacklist_action("", "add")
check("空会话被拒", not chapter.get("ok"), str(chapter))
check("非法动作被拒", not svc.blacklist_action("wxid_x", "ban")["ok"])

sys.exit(support.finish())
