# -*- coding: utf-8 -*-
"""HTTP 路由：GET/POST /persona 真的能读写固定人设与关系表。

不起后台、不碰微信：只把 wx_service 的 Handler 当成普通对象，喂一个假请求。
"""
import io
import json
import os
import sys
from email.message import Message

import support
from support import check, section

CARD = os.path.join(support.TMP, "api_persona_card.json")
with open(CARD, "w", encoding="utf-8") as f:
    json.dump({"version": 10,
               "fixed_persona": "你是小橙，大一在读，嘴硬心软。",
               "relations": [{"name": "老妈", "relation": "你和哥哥的妈妈",
                              "nickname": "老妈", "chat": ""}]},
              f, ensure_ascii=False, indent=2)
os.environ["WXBOT_PERSONA_CARD"] = CARD

section("装配一个假 Handler")
import wx_service                                   # noqa: E402

wx_service.SERVICE = wx_service.Service("wxid_test")
wx_service.SERVICE.token = ""


class FakeHandler(wx_service.Handler):
    def __init__(self, path, data=None):
        self.path = path
        self.headers = Message()
        self.headers["Host"] = "127.0.0.1:8765"
        self.answer = None
        self.code = None
        body = b""
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.headers["Content-Type"] = "application/json; charset=utf-8"
            self.headers["Content-Length"] = str(len(body))
        self.rfile = io.BytesIO(body)

    def _json(self, obj, code=200):
        self.answer, self.code = obj, code


def call(method, path, data=None):
    handler = FakeHandler(path, data)
    if method == "GET":
        handler.do_GET()
    else:
        handler.do_POST()
    return handler


def read_card():
    with open(CARD, encoding="utf-8") as f:
        return json.load(f)


section("GET /persona")
h = call("GET", "/persona")
data = h.answer or {}
check("返回 200", h.code == 200, str(h.code))
check("带固定人设", "你是小橙" in (data.get("fixed_persona") or ""), str(data)[:160])
check("带关系表", [x["name"] for x in data.get("relations") or []] == ["老妈"],
      str(data.get("relations")))
check("标出来源", data.get("source") == "card", str(data.get("source")))

section("POST /persona 保存固定人设")
h = call("POST", "/persona", {"action": "save",
                             "fixed_persona": "你是小橙，大二在读，话更少。"})
check("保存成功", (h.answer or {}).get("ok"), str(h.answer))
check("真的落盘", "大二在读" in read_card().get("fixed_persona", ""))

section("POST /persona 保存关系表")
h = call("POST", "/persona", {"action": "relations", "items": [
    {"name": "老妈", "relation": "你和哥哥的妈妈", "nickname": "老妈", "chat": ""},
    {"name": "阿橘", "relation": "你的哥哥", "chat": "wxid_example_bro"}]})
check("保存成功", (h.answer or {}).get("ok"), str(h.answer))
check("关系条数对得上", len(read_card().get("relations") or []) == 2)

section("异常请求")
h = call("POST", "/persona", {"action": "nope"})
check("未知操作返回 ok=false", (h.answer or {}).get("ok") is False, str(h.answer))
h = call("POST", "/persona", {"action": "relations", "items": [{"name": "x"}]})
check("缺关系被拒绝", (h.answer or {}).get("ok") is False, str(h.answer))

sys.exit(support.finish())
