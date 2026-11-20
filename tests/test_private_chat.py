# -*- coding: utf-8 -*-
"""私聊判定：微信号是自定义 alias（不是 wxid_ 开头）的联系人不能被误杀。

旧代码用 ``username.startswith("wxid_")`` 当私聊判断，结果：
主动聊天列表比会话列表少人，且这类联系人连「允许主动聊天」都点不开。
"""
import json
import os
import sys

import support
from support import check, section

import gate
import wx_service


class FakeDB:
    """只提供 Service 用到的会话/昵称接口。"""

    NAMES = {"wxid_a": "阿甲", "wxid_example_alias": "朋友A"}

    def __init__(self, sessions):
        self.sessions = sessions

    def get_sessions(self, limit=100):
        return self.sessions[:limit]

    def get_nickname(self, uname):
        return self.NAMES.get(uname, uname)

    def nickname_map(self, refresh=False):
        return dict(self.NAMES)

    def group_id_to_name(self, chatroom):
        return {"10000000001@chatroom": "同学群"}.get(chatroom)

    def search_contact(self, kw):
        return []

    def get_messages(self, chat, limit=20):
        return []

    def get_self_info(self):
        return {"username": "me", "nick_name": "🍊"}


SESSIONS = [
    {"username": "wxid_a", "unread": 1, "summary": "在吗"},
    {"username": "wxid_example_alias", "unread": 0, "summary": "你好"},
    {"username": "10000000001@chatroom", "unread": 0, "summary": "群里聊"},
    {"username": "filehelper", "unread": 0, "summary": "笔记"},
    {"username": "gh_abc123", "unread": 0, "summary": "推送"},
]

section("私聊判定：群 / 系统号 / 自定义微信号")
check("wxid_ 私聊", gate.is_private_chat("wxid_a"))
check("微信号 alias 也算私聊", gate.is_private_chat("wxid_example_alias"))
check("群聊不算", not gate.is_private_chat("10000000001@chatroom"))
check("文件传输助手不算", not gate.is_private_chat("filehelper"))
check("公众号不算", not gate.is_private_chat("gh_abc123"))
check("空值不算", not gate.is_private_chat(""))
check("配置追加的系统号也排除",
      not gate.is_private_chat("bot_x", {"system_accounts": ["bot_x"]}))
check("跳系统号开关关着也不放行系统号",
      gate.is_private_chat("wxid_example_alias", {"skip_system_accounts": False}))

# 配置整包替换成内存版，别碰生产 config.json
STORE = {"cfg": support.base_cfg(
    contacts={},
    proactive={"enabled": True, "whitelist": [], "active_hours": [0, 24],
              "per_chat_daily": 3, "reminders": {"enabled": False}},
)}


def fake_load(force=False):
    return json.loads(json.dumps(STORE["cfg"], ensure_ascii=False))


def fake_save(cfg):
    STORE["cfg"] = json.loads(json.dumps(cfg, ensure_ascii=False))
    return True


wx_service.C.load = fake_load
wx_service.C.save = fake_save

svc = wx_service.Service("wxid_test")
svc.db = FakeDB(SESSIONS)
svc.me = "me"

section("主动聊天列表：不再漏掉微信号 alias 的联系人")
users = [x["username"] for x in svc.proactive_contacts()]
check("包含 wxid_ 私聊", "wxid_a" in users, str(users))
check("包含微信号 alias 联系人", "wxid_example_alias" in users, str(users))
check("排除群聊", "10000000001@chatroom" not in users, str(users))
check("排除文件传输助手", "filehelper" not in users, str(users))
check("排除公众号", not any(u.startswith("gh_") for u in users), str(users))
check("只剩两个真人私聊", len(users) == 2, str(users))

section("开关：alias 联系人能开，群与系统号仍然拒绝")
r = svc.set_proactive_contact("wxid_example_alias", True)
check("alias 联系人开启成功", r.get("ok"), str(r))
check("写进白名单", "wxid_example_alias" in STORE["cfg"]["proactive"]["whitelist"],
      str(STORE["cfg"]["proactive"]))
check("写进联系人开关", STORE["cfg"]["contacts"]["wxid_example_alias"]["proactive"] is True,
      str(STORE["cfg"]["contacts"]))
r2 = svc.set_proactive_contact("10000000001@chatroom", True)
check("群聊拒绝", not r2.get("ok") and "私聊" in (r2.get("error") or ""), str(r2))
r3 = svc.set_proactive_contact("filehelper", True)
check("系统号拒绝", not r3.get("ok"), str(r3))
r4 = svc.set_proactive_contact("wxid_a", True)
check("wxid_ 私聊仍可开启", r4.get("ok"), str(r4))

section("列表回显：开启状态跟着配置走")
by_user = {x["username"]: x for x in svc.proactive_contacts()}
check("alias 显示已允许", by_user["wxid_example_alias"]["enabled"]
      and by_user["wxid_example_alias"]["active"], str(by_user.get("wxid_example_alias")))

section("会话列表：仍保持全量，但标出 private")
rows = svc.chats()
check("会话数不变（群与系统号也在）", len(rows) == len(SESSIONS), str(len(rows)))
by_chat = {r["username"]: r for r in rows}
check("alias 标为私聊", by_chat["wxid_example_alias"]["private"] is True)
check("wxid_ 标为私聊", by_chat["wxid_a"]["private"] is True)
check("群标为非私聊", by_chat["10000000001@chatroom"]["private"] is False)
check("文件传输助手标为非私聊", by_chat["filehelper"]["private"] is False)
check("公众号标为非私聊", by_chat["gh_abc123"]["private"] is False)

section("控制台：会话 ID 校验接受微信号 alias")
sys.path.insert(0, os.path.join(support.ROOT, "GUI"))
import app  # noqa: E402  只读模块级函数，不建窗口

check("alias 通过校验", app.chat_id("wxid_example_alias") == "wxid_example_alias")
check("wxid_ 通过校验", app.chat_id("wxid_a") == "wxid_a")
check("群号通过校验", app.chat_id("10000000001@chatroom") == "10000000001@chatroom")
try:
    app.chat_id("坏值")
    bad = False
except ValueError:
    bad = True
check("乱填仍然报错", bad)
check("控制台兜底判定：alias 是私聊", app.is_private_chat("wxid_example_alias"))
check("控制台兜底判定：群不是私聊", not app.is_private_chat("10000000001@chatroom"))
check("控制台兜底判定：系统号不是私聊", not app.is_private_chat("filehelper"))

sys.exit(support.finish())
