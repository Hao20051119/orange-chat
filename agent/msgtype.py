# -*- coding: utf-8 -*-
"""消息类型归一与人类可读描述。

以前 ``reply_worker`` 只认「文本」和「图片/动画表情」，其余（语音、视频、文件、
链接卡片、位置、名片、通话记录）全部退化成「对方发来一张图片」，模型于是会
一本正经地评论一张根本不存在的图。这里把 type 归一成 kind，再给一句如实的
描述；拿不到内容就明说拿不到，绝不冒充图片。

``msg_type`` 可能是 wechatauto 返回的中文名，也可能是裸数字（例如 42 名片、
57 引用、49 卡片），还可能是 4.x 的复合码 ``高位<<32|低位``。
"""

from __future__ import annotations

import re

# wechatauto MSG_TYPE_NAMES 之外的补充（它们会以裸数字形式传进来）
EXTRA_NAMES = {
    42: "名片",
    57: "引用消息",
    419430449: "文件/链接/卡片",
}

_TEXT_NAMES = {"文本", "text"}
_IMAGE_NAMES = {"图片", "image"}
_EMOJI_NAMES = {"动画表情", "表情", "emoji"}
_VOICE_NAMES = {"语音", "voice"}
_VIDEO_NAMES = {"视频", "video"}
_LOCATION_NAMES = {"位置", "location"}
_FILE_NAMES = {"文件/链接/卡片", "文件", "链接", "卡片", "file", "link"}
_VOIP_NAMES = {"音视频通话", "通话", "voip"}
_SYSTEM_NAMES = {"系统消息", "system"}
_CARD_NAMES = {"名片", "card"}
_REDPACKET_NAMES = {"红包", "转账", "redpacket"}
_QUOTE_NAMES = {"引用消息", "quote"}

_APPMSG_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_APPMSG_URL = re.compile(r"<url>(.*?)</url>", re.S)
_APPMSG_DES = re.compile(r"<des>(.*?)</des>", re.S)
_APPMSG_TYPE = re.compile(r"<appmsg[^>]*>.*?<type>(\d+)</type>", re.S)
_APPMSG_FILEEXT = re.compile(r"<fileext>([^<]*)</fileext>")
_APPMSG_APPID = re.compile(r'appid="([^"]*)"')
_REFER_TITLE = re.compile(r"<refermsg>.*?<title>(.*?)</title>", re.S)
_REFER_CONTENT = re.compile(r"<refermsg>.*?<content>(.*?)</content>", re.S)
_REFER_BLOCK = re.compile(r"<refermsg>(.*?)</refermsg>", re.S)
_REFER_WHO = re.compile(r"<displayname>(.*?)</displayname>", re.S)
_REFER_KIND = re.compile(r"<type>(\d+)</type>", re.S)
_REFER_TEXT = re.compile(r"<content>(.*?)</content>", re.S)
_REFER_NICK = re.compile(r'nickname="([^"]*)"')
_REFER_INNER_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_LOCATION_LABEL = re.compile(r'label="([^"]*)"')
_LOCATION_POI = re.compile(r'poiname="([^"]*)"')
_CARD_NICK = re.compile(r'nickname="([^"]*)"')
_CARD_ALIAS = re.compile(r'alias="([^"]*)"')
_VOIP_STATUS = re.compile(r"<msg>.*?<voipmsg[^>]*>(.*?)</voipmsg>", re.S)


def _numeric(msg_type) -> int | None:
    if isinstance(msg_type, bool):
        return None
    if isinstance(msg_type, int):
        return msg_type
    s = str(msg_type or "").strip()
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return None
    return None


def normalize_code(msg_type) -> int | None:
    """把 4.x 复合码压回真实类型码；解析不了返回 None。"""
    n = _numeric(msg_type)
    if n is None:
        return None
    # 完整码先查表（红包这类本身就是超大数值码）
    if n in _CODE_NAMES or n in EXTRA_NAMES:
        return n
    if n > 0xFFFF:
        base = n & 0xFFFFFFFF
        if base in EXTRA_NAMES or base in _CODE_NAMES:
            return base
        low = base & 0xFF
        if low:
            return low
    return n


_CODE_NAMES = {
    1: "文本", 3: "图片", 34: "语音", 43: "视频", 47: "动画表情", 48: "位置",
    49: "文件/链接/卡片", 50: "音视频通话", 10000: "系统消息", 11000: "动画表情",
    42: "名片", 57: "引用消息", 8594229559345: "红包",
}


def kind(msg_type, type_code=None) -> str:
    """归一成 text/image/emoji/voice/video/location/file/voip/system/card/redpacket/quote/unknown。"""
    name = str(msg_type or "").strip()
    if name in _TEXT_NAMES:
        return "text"
    if name in _IMAGE_NAMES:
        return "image"
    if name in _EMOJI_NAMES:
        return "emoji"
    if name in _VOICE_NAMES:
        return "voice"
    if name in _VIDEO_NAMES:
        return "video"
    if name in _LOCATION_NAMES:
        return "location"
    if name in _FILE_NAMES:
        return "file"
    if name in _VOIP_NAMES:
        return "voip"
    if name in _SYSTEM_NAMES:
        return "system"
    if name in _CARD_NAMES:
        return "card"
    if name in _REDPACKET_NAMES:
        return "redpacket"
    if name in _QUOTE_NAMES:
        return "quote"

    code = normalize_code(type_code if type_code not in (None, "") else msg_type)
    if code is None:
        return "unknown"
    mapped = _CODE_NAMES.get(code)
    if not mapped:
        return "unknown"
    # 避免无限递归：用名字直接映射一次
    if mapped in _TEXT_NAMES:
        return "text"
    if mapped in _IMAGE_NAMES:
        return "image"
    if mapped in _EMOJI_NAMES:
        return "emoji"
    if mapped in _VOICE_NAMES:
        return "voice"
    if mapped in _VIDEO_NAMES:
        return "video"
    if mapped in _LOCATION_NAMES:
        return "location"
    if mapped in _FILE_NAMES:
        return "file"
    if mapped in _VOIP_NAMES:
        return "voip"
    if mapped in _SYSTEM_NAMES:
        return "system"
    if mapped in _CARD_NAMES:
        return "card"
    if mapped in _REDPACKET_NAMES:
        return "redpacket"
    if mapped in _QUOTE_NAMES:
        return "quote"
    return "unknown"


def needs_vision(k: str) -> bool:
    return k in ("image", "emoji")


def appmsg_info(content: str) -> dict:
    """解析文件/链接/卡片 XML，取标题、链接、小程序标识、引用。"""
    c = content or ""
    info = {"title": "", "url": "", "des": "", "type": None, "appid": "", "quote": ""}
    m = _APPMSG_TITLE.search(c)
    if m:
        info["title"] = _unescape(m.group(1)).strip()[:200]
    m = _APPMSG_URL.search(c)
    if m:
        info["url"] = _unescape(m.group(1)).strip()[:1000]
    m = _APPMSG_DES.search(c)
    if m:
        info["des"] = _unescape(m.group(1)).strip()[:200]
    m = _APPMSG_TYPE.search(c)
    if m:
        try:
            info["type"] = int(m.group(1))
        except ValueError:
            info["type"] = None
    m = _APPMSG_APPID.search(c)
    if m:
        info["appid"] = m.group(1)
    q = _REFER_CONTENT.search(c) or _REFER_TITLE.search(c)
    if q:
        info["quote"] = _unescape(q.group(1)).strip()
        info["quote"] = re.sub(r"<[^>]+>", " ", info["quote"]).strip()[:120]
    return info


def _unescape(s: str) -> str:
    return (s or "").replace("&lt;", "<").replace("&gt;", ">") \
        .replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


# 被引用消息类型 -> 拿不到文字时的说法
_REFER_LABEL = {
    3: "一张图片", 34: "一条语音", 43: "一段视频", 47: "一个表情",
    48: "一个位置", 50: "一次通话", 10000: "一条系统消息",
}


def refer_info(content: str) -> dict:
    """拆引用回复里的 ``<refermsg>``：被引用的是谁、什么类型、什么内容。

    WeChat 的引用消息长这样（``<type>57</type>``）：::

        <title>本次回复的正文</title>
        <refermsg><type>1</type><displayname>🍊</displayname>
                  <content>被引用的原文</content></refermsg>

    被引用内容可能是纯文本，也可能是**转义过的** XML（名片、文件、图片等），
    所以要 unescape + 去标签；实在拿不到就按类型说一句"被引用的是一张图片"。
    """
    out = {"who": "", "type": None, "text": ""}
    m = _REFER_BLOCK.search(content or "")
    if not m:
        return out
    block = m.group(1)
    w = _REFER_WHO.search(block)
    if w:
        out["who"] = _unescape(w.group(1)).strip()[:40]
    t = _REFER_KIND.search(block)
    if t:
        try:
            out["type"] = int(t.group(1))
        except ValueError:
            out["type"] = None
    x = _REFER_TEXT.search(block)
    if not x:
        return out
    raw = _unescape(x.group(1) or "").strip()
    typ = out["type"] or 1
    if typ == 42:                      # 名片：文字在 nickname 属性里
        nick = _REFER_NICK.search(raw)
        out["text"] = "名片" + ("（%s）" % nick.group(1) if nick else "")
        return out
    if typ in (49, 57):                # 卡片/文件：标题在里面一层
        it = _REFER_INNER_TITLE.search(raw)
        out["text"] = "卡片：%s" % (it.group(1).strip() if it else "文件/链接")
        return out
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    out["text"] = text[:120] if text else _REFER_LABEL.get(typ, "")
    return out


def describe(msg_type, content: str = "", type_code=None, desc: str | None = None) -> dict:
    """返回 ``{"kind","label","text"}``；text 是注入对话的如实描述。"""
    k = kind(msg_type, type_code)
    c = (content or "").strip()
    info = appmsg_info(c) if k in ("file", "quote", "unknown") else {}
    # 引用回复在微信里是 appmsg type=57，外层类型仍然是「文件/链接/卡片」；
    # 以前没往下看一层，57 落进了"视频号"分支，于是引用被说成"发来一条视频号内容"
    if k in ("file", "quote", "unknown") and c.startswith("<"):
        if info.get("type") == 57 or "<refermsg>" in c:
            k = "quote"

    if k == "text":
        return {"kind": k, "label": "文本", "text": c}
    if k == "image":
        text = ("[对方发来一张图片：%s]" % desc) if desc else "[对方发来一张图片]"
        return {"kind": k, "label": "图片", "text": text}
    if k == "emoji":
        text = ("[对方发来一个表情：%s]" % desc) if desc else "[对方发来一个表情]"
        return {"kind": k, "label": "表情", "text": text}
    if k == "voice":
        text = ("[对方发来一条语音：%s]" % desc) if desc else "[对方发来一条语音（没转成文字）]"
        return {"kind": k, "label": "语音", "text": text}
    if k == "video":
        return {"kind": k, "label": "视频", "text": "[对方发来一段视频]"}
    if k == "location":
        m = _LOCATION_LABEL.search(c) or _LOCATION_POI.search(c)
        where = _unescape(m.group(1)).strip() if m else ""
        text = ("[对方发来一个位置：%s]" % where) if where else "[对方发来一个位置]"
        return {"kind": k, "label": "位置", "text": text}
    if k == "card":
        m = _CARD_NICK.search(c) or _CARD_ALIAS.search(c)
        who = _unescape(m.group(1)).strip() if m else ""
        text = ("[对方发来一张名片：%s]" % who) if who else "[对方发来一张名片]"
        return {"kind": k, "label": "名片", "text": text}
    if k == "voip":
        return {"kind": k, "label": "通话", "text": "[对方发起过音视频通话]"}
    if k == "redpacket":
        return {"kind": k, "label": "红包/转账", "text": "[对方发来一个红包或转账]"}
    if k == "system":
        return {"kind": k, "label": "系统消息", "text": ""}
    if k == "quote":
        body = (info.get("title") or "").strip()
        ref = refer_info(c)
        quoted = (ref.get("text") or "").strip()
        who = (ref.get("who") or "").strip()
        if quoted and who:
            quoted = "%s：%s" % (who, quoted)
        if quoted and body:
            text = "[对方引用了「%s」并回复：%s]" % (quoted, body)
        elif quoted:
            text = "[对方引用了一条消息：%s]" % quoted
        elif body:
            text = "[对方回复了某条消息，说：%s]" % body
        else:
            text = "[对方引用了一条消息回复]"
        return {"kind": k, "label": "引用", "text": text}
    if k == "file":
        atype = info.get("type")
        title = info.get("title") or ""
        url = info.get("url") or ""
        appid = info.get("appid") or ""
        m = _APPMSG_FILEEXT.search(c)
        fileext = (m.group(1).strip() if m else "")
        # 真实附件（appmsg type=6）才带元信息，供知识库自动入库时定位磁盘文件
        attach = {"card_type": atype, "title": title, "ext": fileext} \
            if (atype == 6 or fileext) else {"card_type": atype}
        if atype == 62 or "拍了拍" in title:
            text = "[对方拍了拍你]"
        elif atype in (2000, 2001):
            text = "[对方发来一个红包或转账]"
        elif atype in (33, 36) or appid.startswith("wx"):
            text = ("[对方发来一个小程序卡片：%s]" % title) if title else "[对方发来一个小程序卡片]"
        elif atype in (51, 63):
            text = ("[对方发来一条视频号内容：%s]" % title) if title else "[对方发来一条视频号内容]"
        elif atype == 87:
            text = ("[群公告：%s]" % title) if title else "[群公告]"
        elif atype == 6 or fileext:
            text = ("[对方发来一个文件：%s]" % title) if title else "[对方发来一个文件]"
        elif atype == 19:
            text = "[对方转发了一段聊天记录]"
        elif atype == 5 or url:
            text = ("[对方发来一个链接：%s%s]" % (
                title, ("（%s）" % url[:120]) if url else "")) if title else "[对方发来一个链接]"
        else:
            text = ("[对方发来一张卡片：%s]" % title) if title else "[对方发来一张卡片]"
        return {"kind": k, "label": "文件/链接", "text": text, **attach}

    # 未知类型：如实说明，绝不冒充图片
    label = EXTRA_NAMES.get(int(normalize_code(type_code) or 0), "") or ""
    return {"kind": "unknown", "label": "未知",
            "text": "[对方发来一条消息%s]" % (("（类型%s）" % (label or msg_type))
                                          if (label or msg_type) else "")}


if __name__ == "__main__":
    import os
    import sys
    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cases = [
        ("文本", "晚上吃啥"),
        ("42", '<msg bigheadimgurl="x"><msg username="wxid_a" nickname="张三" /></msg>'),
        (49, '<msg><appmsg appid="" sdkver="0"><title>DeepSeek 发布新版本</title>'
             '<des>点此查看</des><url>https://example.com/a</url><type>5</type></appmsg></msg>'),
        ("文件/链接/卡片", '<msg><appmsg><title>会议纪要.pdf</title><type>6</type></appmsg></msg>'),
        (34, "<msg><voicemsg length='3000'/></msg>"),
        (11000, ""),
        (10000, "你已添加了张三"),
        (8572732625, '<msg><appmsg><title>小程序：某某</title><type>33</type></appmsg></msg>'),
    ]
    for t, c in cases:
        print("%-14r -> %s" % (t, describe(t, c)))
