# -*- coding: utf-8 -*-
"""目标解析：同名联系人必须拒绝，不能盲取搜索结果第一条。"""
import sys

import support
from support import check, section


class FakeDB:
    def __init__(self, sessions, nicks, contacts, groups=None):
        self.sessions = sessions
        self.nicks = nicks
        self.contacts = contacts
        self.groups = groups or {}

    def get_sessions(self, limit=100):
        return [{"username": u, "unread": 0, "summary": ""} for u in self.sessions]

    def get_nickname(self, uname):
        return self.nicks.get(uname, uname)

    def nickname_map(self, refresh=False):
        return {**self.nicks, **self.groups}

    def group_id_to_name(self, chatroom):
        return self.groups.get(chatroom)

    def search_contact(self, kw):
        return self.contacts.get(kw, [])


import wx_service

svc = wx_service.Service("wxid_test")

section("直连 username")
svc.db = FakeDB(["wxid_a"], {"wxid_a": "张三"}, {})
r = svc.resolve_target("wxid_a")
check("username 直接用", r["ok"] and r["username"] == "wxid_a", str(r))
check("显示名取备注/昵称", r["display"] == "张三", r.get("display"))
check("未知 ID 不会被当成会话", not svc.resolve_target("wxid_other", strict=True)["ok"])

section("群聊 username")
svc.db = FakeDB(["123@chatroom"], {"123@chatroom": "同学群"}, {})
r = svc.resolve_target("123@chatroom")
check("群聊直连", r["ok"] and r["display"] == "同学群", str(r))

svc.db = FakeDB(["10000000001@chatroom"], {"10000000001@chatroom": "10000000001@chatroom"},
                {}, groups={"10000000001@chatroom": "🥭王朝"})
r = svc.resolve_target("10000000001@chatroom")
check("群聊用群名而不是 wxid", r["ok"] and r["display"] == "🥭王朝", str(r))

section("备注精确匹配")
svc.db = FakeDB(["wxid_a"], {"wxid_a": "张三", "wxid_b": "王五"},
                {"老王": [{"username": "wxid_b", "remark": "老王", "nick_name": "王五"}]})
r = svc.resolve_target("老王")
check("备注唯一 → 命中", r["ok"] and r["username"] == "wxid_b", str(r))

section("同名联系人 → 拒绝")
svc.db = FakeDB(
    ["wxid_a", "wxid_b"],
    {"wxid_a": "张三", "wxid_b": "张三"},
    {"张三": [{"username": "wxid_a", "remark": "张三", "nick_name": "张三"},
              {"username": "wxid_b", "remark": "张三", "nick_name": "张三"}]})
r = svc.resolve_target("张三")
check("两个张三 → 拒绝", not r["ok"], str(r))
check("错误里说明重名", "重名" in r.get("error", "") or "2 个" in r.get("error", ""),
      r.get("error"))

section("备注唯一但显示名撞车 → 拒绝")
svc.db = FakeDB(
    ["wxid_a", "wxid_c"],
    {"wxid_a": "张三", "wxid_c": "张三"},
    {"老王": [{"username": "wxid_c", "remark": "老王", "nick_name": "张三"}]})
r = svc.resolve_target("老王")
check("显示名冲突 → 拒绝", not r["ok"], str(r))
svc.db = FakeDB(["wxid_a"], {"wxid_a": "张三", "wxid_old": "张三"}, {})
check("最近会话以外的重名也会冲突",
      not svc.resolve_target("wxid_a", strict=True)["ok"])

section("找不到")
svc.db = FakeDB(["wxid_a"], {"wxid_a": "张三"}, {})
r = svc.resolve_target("查无此人")
check("找不到 → 失败", not r["ok"] and "找不到" in r.get("error", ""), str(r))
check("空参数 → 失败", not svc.resolve_target("")["ok"])

section("数据库不可用")
svc.db = None
check("db 缺失 → 明确报错", not svc.resolve_target("wxid_a")["ok"])

sys.exit(support.finish())
