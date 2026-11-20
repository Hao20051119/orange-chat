# -*- coding: utf-8 -*-
"""消息类型：语音/文件/名片/未知类型都不能被说成"图片"。"""
import sys

import support
from support import check, section

import msgtype as M

section("kind 归一")
cases = [
    ("文本", None, "text"),
    ("图片", None, "image"),
    ("动画表情", None, "emoji"),
    ("语音", None, "voice"),
    ("视频", None, "video"),
    ("位置", None, "location"),
    ("文件/链接/卡片", None, "file"),
    ("音视频通话", None, "voip"),
    ("系统消息", None, "system"),
    (42, None, "card"),
    ("42", None, "card"),
    (57, None, "quote"),
    (11000, None, "emoji"),
    (8594229559345, None, "redpacket"),
    ("奇怪类型", None, "unknown"),
]
for t, code, expect in cases:
    got = M.kind(t, code)
    check("%r → %s" % (t, expect), got == expect, got)

check("4.x 复合码 17<<32|11000 → emoji",
      M.kind("", (17 << 32) | 11000) == "emoji", M.kind("", (17 << 32) | 11000))

section("describe 文案")
d = M.describe("语音", "<msg/>")
check("语音不说成图片", "语音" in d["text"] and "图片" not in d["text"], d["text"])

d = M.describe("文件/链接/卡片",
               '<msg><appmsg><title>NEWS</title><url>https://x.com/a</url>'
               '<type>5</type></appmsg></msg>')
check("链接带标题与地址", "NEWS" in d["text"] and "https://x.com/a" in d["text"], d["text"])

d = M.describe(42, '<msg><msg nickname="张三" /></msg>')
check("名片带昵称", "名片" in d["text"] and "张三" in d["text"], d["text"])

d = M.describe("文件/链接/卡片",
               '<msg><appmsg><title>会议纪要.pdf</title><type>6</type></appmsg></msg>')
check("文件", "文件" in d["text"] and "会议纪要" in d["text"], d["text"])

d = M.describe("文件/链接/卡片",
               '<msg><appmsg appid="wx123"><title>某小程序</title><type>33</type>'
               '</appmsg></msg>')
check("小程序", "小程序" in d["text"], d["text"])

d = M.describe("位置", '<msg><location label="西湖景区" /></msg>')
check("位置带标签", "位置" in d["text"] and "西湖" in d["text"], d["text"])

# 拍一拍：appmsg 里带 fileext（空）不能被误判成文件
pat = ('<msg><appmsg appid="" sdkver="0"><title>"🥭" 拍了拍我说女神会原谅你的</title>'
       '<type>62</type><appattach><totallen>0</totallen><fileext></fileext></appattach>'
       '</appmsg></msg>')
d = M.describe("文件/链接/卡片", pat)
check("拍一拍 ≠ 文件", "拍了拍" in d["text"], d["text"])
check("拍一拍 ≠ 图片", "图片" not in d["text"], d["text"])

d = M.describe("文件/链接/卡片",
               '<msg><appmsg><title>报告.docx</title><type>6</type>'
               '<appattach><fileext>docx</fileext></appattach></appmsg></msg>')
check("真文件仍识别", "文件" in d["text"] and "报告" in d["text"], d["text"])

d = M.describe("文件/链接/卡片",
               '<msg><appmsg><title>转账给你</title><type>2000</type></appmsg></msg>')
check("转账/红包", "红包" in d["text"] or "转账" in d["text"], d["text"])

d = M.describe(99999, "<msg/>")
check("未知类型不冒充图片", "图片" not in d["text"] and "消息" in d["text"], d["text"])

d = M.describe("图片", "<msg/>", desc="一只猫")
check("有描述时带上", "一只猫" in d["text"], d["text"])

d = M.describe("系统消息", "你已添加了张三")
check("系统消息返回空文本", d["text"] == "", d["text"])

d = M.describe("引用消息",
               '<msg><appmsg><title>回复</title><refermsg><content>引用的话</content>'
               '</refermsg></appmsg></msg>')
check("引用值取到", "引用的话" in d["text"], d["text"])

section("引用回复（被误判成视频号的事故）")
QUOTE_TEXT = """<msg><appmsg appid="" sdkver="0"><title>你决定？</title><type>57</type>
<refermsg><type>1</type><displayname>🍊</displayname>
<content>行吧行吧，聊什么 (￣▽￣)</content><createtime>1795023874</createtime>
</refermsg></appmsg><fromusername>wxid_x</fromusername></msg>"""
d = M.describe("文件/链接/卡片", QUOTE_TEXT, 49)
check("引用被识别为 quote", d["kind"] == "quote", d["kind"])
check("引用不被说成视频号", "视频号" not in d["text"], d["text"])
check("带上了被引用的原文", "行吧行吧" in d["text"], d["text"])
check("带上了这次回复的正文", "你决定？" in d["text"], d["text"])
check("带上了被引用的人", "🍊" in d["text"], d["text"])

QUOTE_CARD = """<msg><appmsg><title>这是你妈</title><type>57</type><refermsg>
<type>42</type><displayname>🥭</displayname>
<content>&lt;msg username="wxid_a" nickname="王" sex="2"&gt;&lt;/msg&gt;</content>
</refermsg></appmsg></msg>"""
d = M.describe("文件/链接/卡片", QUOTE_CARD, 49)
check("引用名片 → 名片（王）", "名片" in d["text"] and "王" in d["text"], d["text"])
check("引用名片也不冒充视频号", "视频号" not in d["text"], d["text"])

VIDEO = ('<msg><appmsg><title>某个视频</title><type>51</type>'
         '<url>https://channels.weixin.qq.com/x</url></appmsg></msg>')
d = M.describe("文件/链接/卡片", VIDEO, 49)
check("真·视频号（type 51）仍识别", "视频号" in d["text"], d["text"])

NO_REFER = ('<msg><appmsg><title>就一句话</title><type>57</type></appmsg></msg>')
d = M.describe("文件/链接/卡片", NO_REFER, 49)
check("57 没有 refermsg → 只报正文", "就一句话" in d["text"] and "视频号" not in d["text"],
      d["text"])

d = M.describe("奇怪类型", QUOTE_TEXT, 99999)
check("外层类型不认识时也能认出引用", d["kind"] == "quote" and "你决定？" in d["text"],
      str(d))

d = M.describe("", QUOTE_TEXT, None)
check("类型为空（库里常见）也认得出引用",
      d["kind"] == "quote" and "你决定？" in d["text"], str(d))

ref = M.refer_info(QUOTE_TEXT)
check("refer_info 拆出谁/类型/内容",
      ref["who"] == "🍊" and ref["type"] == 1 and "聊什么" in ref["text"], str(ref))

check("needs_vision 只对图片/表情", M.needs_vision("image") and M.needs_vision("emoji")
      and not M.needs_vision("voice"))

section("语音转写降级")
import voice

check("默认关闭 → 不转写", voice.transcribe("wxid_a", 1) is None)
ok, why = voice.available()
check("缺依赖/模型时给出原因", ok or bool(why), str((ok, why)))

sys.exit(support.finish())
