# -*- coding: utf-8 -*-
"""联网：URL 白名单校验、每轮预算（查询数/页面数/截止时间）。"""
import sys
import time

import support
from support import check, section

import web_tools as W

section("URL 安全校验")
allowed = [
    "https://api-docs.deepseek.com/guides/search/",
    "http://example.com/a?b=1",
]
blocked = [
    "file:///C:/Windows/win.ini",
    "http://127.0.0.1:8765/status",
    "http://localhost/",
    "http://192.168.1.1/",
    "http://10.0.0.5/",
    "http://169.254.169.254/latest/meta-data",
    "http://100.100.100.200/",
    "https://user:pass@example.com/",
    "ftp://example.com/x",
    "http://metadata.google.internal/",
]
for u in allowed:
    check("允许 %s" % u[:40], W.url_is_safe(u)[0], W.url_is_safe(u)[1])
for u in blocked:
    check("拒绝 %s" % u[:40], not W.url_is_safe(u)[0], u)

section("检索词隐私清洗")
q = W._clean_query("我电话13800138000 微信 wxid_abc123 身份证 110101199001011234 怎么办")
check("手机号被剔除", "13800138000" not in q, q)
check("wxid 被剔除", "wxid_abc123" not in q, q)
check("身份证被剔除", "110101199001011234" not in q, q)

section("触发规则")
check("问候不联网", not W.should_search("在吗")[0])
check("情绪不联网", not W.should_search("今天好累")[0])
check("问最新版本要联网", W.should_search("最新版本什么时候发布")[0])
check("带链接要联网", W.should_search("看看这个 https://example.com/x")[0])

section("可配置触发词（新梗/新词）")
check("硬编码词表不覆盖穿搭", not W.should_search("多巴胺穿搭好看吗")[0])
check("extra_hints 能补上", W.should_search("多巴胺穿搭好看吗", ["穿搭"])[0])
check("命中理由带 kw* 标记",
      W.should_search("多巴胺穿搭好看吗", ["穿搭"])[1].startswith("kw*"),
      W.should_search("多巴胺穿搭好看吗", ["穿搭"])[1])
check("extra_hints 为空不触发", not W.should_search("多巴胺穿搭好看吗", [])[0])
check("extra_hints 非字符串不炸", not W.should_search("多巴胺穿搭好看吗", [None, 0])[0])
check("闲聊仍不因 extra_hints 触发", not W.should_search("在吗", ["穿搭"])[0])

section("查询词提炼：口语问句 → 能搜的词")
HINT = "昨天聊到明天早八，金华这边冷死了"
check("口语问天气能从上下文补地名",
      W._focus_query("我手机天气好像坏了，帮我看看嘛", HINT) == "金华 天气",
      W._focus_query("我手机天气好像坏了，帮我看看嘛", HINT))
check("自己说了地名就用",
      W._focus_query("金华明天会下雨吗", "") == "金华 天气")
check("没地名至少归到天气",
      W._focus_query("明天天气怎么样", "") == "天气")
check("非天气问题原样保留",
      W._focus_query("多巴胺穿搭好看吗", "") == "多巴胺穿搭好看吗")
check("闲聊不硬塞天气", W._focus_query("在吗", "") == "在吗")
check("穿衣问题也算天气", W._focus_query("今天穿什么", HINT) == "金华 天气",
      W._focus_query("今天穿什么", HINT))

section("注入块：日期语义与排序")
res = {"ok": True, "results": [
    {"title": "旧闻甲", "url": "u1", "summary": "今天上午阴有小雨，下午转好",
     "date": "2026-11-18"},
    {"title": "新预报乙", "url": "u2", "summary": "今天晴到多云", "date": "2026-11-20"},
    {"title": "没有日期的丙", "url": "u3", "summary": "金华一周天气趋势"},
]}
ctx = W.build_context(res, max_chars=1200, today="2026-11-20")
check("声明今天是几号", "今天是 2026-11-20" in ctx, ctx[:60])
check("提醒资料里的「今天」是它自己那天",
      "指的是它自己那天" in ctx, ctx[:120])
check("新资料排在旧资料前面", ctx.index("新预报乙") < ctx.index("旧闻甲"))
check("资料带发布时间", "发布于 2026-11-20" in ctx)
check("没有日期的标注未标注日期", "未标注日期" in ctx)
check("空结果不注入", W.build_context({"ok": False}) == "")
check("只有元信息不注入", W.build_context({"ok": True, "results": []}) == "")

section("每轮预算")
calls = {"search": 0, "fetch": 0}
orig_search, orig_fetch = W.search, W.fetch


def fake_search(query, count=5, need_content=False, ori_question="", timeout=None):
    calls["search"] += 1
    return {"ok": True, "query": query, "results": [
        {"title": "T1", "url": "https://example.com/1", "summary": "摘要一",
         "date": "2026-01-01"}]}


def fake_fetch(url, timeout=None):
    calls["fetch"] += 1
    return {"ok": True, "url": url, "title": "页面", "content": "正文内容"}


W.search, W.fetch = fake_search, fake_fetch
try:
    cfg = {"enabled": True, "max_queries_per_turn": 1, "max_pages_per_turn": 1,
           "timeout": 8, "deadline_ms": 8000}
    r = W.Research(cfg)
    ctx = r.maybe("最新版本什么时候发布")
    check("第一次检索有结果", "T1" in ctx, ctx[:60])
    check("查询计数 = 1", r.queries == 1, str(r.queries))
    ctx2 = r.maybe("再查一次最新版本")
    check("第二轮内第二次查询被拒", ctx2 == "", repr(ctx2[:40]))
    check("底层只调了一次", calls["search"] == 1, str(calls))

    r2 = W.Research(cfg)
    r2.maybe("看看这个 https://example.com/page")
    check("抓页面计数 = 1", r2.pages == 1, str(r2.pages))
    r2.maybe("再看 https://example.com/page2")
    check("页面数上限生效", r2.pages == 1, str(r2.pages))

    r3 = W.Research({"enabled": True, "max_queries_per_turn": 2,
                     "max_pages_per_turn": 2, "deadline_ms": 1})
    time.sleep(0.01)
    check("截止时间到 → 不检索", r3.maybe("最新版本") == "")

    r4 = W.Research(dict(cfg, enabled=False))
    check("联网关闭 → 空", r4.maybe("最新版本") == "")

    # 失败降级：不编造资料
    def bad_search(*a, **k):
        return {"ok": False, "error": "HTTP 500"}

    W.search = bad_search
    r5 = W.Research(cfg)
    check("检索失败不注入伪造资料", r5.maybe("最新版本") == "")
    W.search = fake_search
finally:
    W.search, W.fetch = orig_search, orig_fetch

sys.exit(support.finish())
