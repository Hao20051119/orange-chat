# -*- coding: utf-8 -*-
"""联网检索层：DeepSeek Search / Fetch API 封装 + 预算与降级。

设计约束
--------
* 只把必要的公开话题送去检索，不整段私聊、不带微信号；
* **每轮**有独立预算：最多 N 次查询、M 个页面、总共 8 秒（可配），
  以前把 ``max_queries_per_turn`` 当成"结果条数"用，等于没限；
* 只允许 http/https 公网地址，拒绝本机 / 内网 / 云元数据 / 带凭据的 URL；
* 检索失败不假装查到，返回明确错误，由上层降级为"不联网"；
* 每日调用次数有上限，预算文件加锁 + 原子写。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
CONFIG = os.environ.get("WXBOT_CONFIG") or os.path.join(ROOT, "config.json")
BUDGET_FILE = os.environ.get("WXBOT_WEB_BUDGET") or os.path.join(BASE, "web_budget.json")

# 触发联网的关键词（规则初筛，宁缺毋滥）
SEARCH_HINTS = (
    "最新", "什么时候", "查一下", "帮我查", "搜一下", "查查",
    "什么意思", "是什么梗", "什么梗", "梗",
    "版本", "发布了", "新闻", "公告", "赛程", "比分", "股价", "天气",
    "多少钱", "哪一年", "谁发明的", "怎么用", "教程", "官网", "出自",
)
URL_RE = re.compile(r"https?://[^\s<>\"')]+")

_BUDGET_LOCK = threading.Lock()
_BLOCKED_HOSTS = ("localhost", "metadata.google.internal", "metadata.tencentyun.com",
                  "100.100.100.200")


def _cfg() -> dict:
    try:
        from config import load as _load
        return _load()
    except Exception:
        pass
    try:
        with open(CONFIG, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def _web_cfg() -> dict:
    return (_cfg().get("web") or {})


# ---------------------------------------------------------------- 预算
def _budget() -> dict:
    today = time.strftime("%Y-%m-%d")
    try:
        with open(BUDGET_FILE, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if d.get("date") != today:
        d = {"date": today, "search": 0, "fetch": 0}
    return d


def _save_budget(d: dict):
    try:
        tmp = BUDGET_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, BUDGET_FILE)
    except OSError:
        pass


def _consume(kind: str) -> tuple[bool, str]:
    cfg = _web_cfg()
    limit = int(cfg.get("daily_budget", 200))
    with _BUDGET_LOCK:
        d = _budget()
        if d.get(kind, 0) >= limit:
            return False, "今日%s次数已达上限 %d" % (
                "检索" if kind == "search" else "抓取", limit)
        d[kind] = d.get(kind, 0) + 1
        _save_budget(d)
    return True, ""


# ---------------------------------------------------------------- HTTP
def _call(path: str, payload: dict, timeout: float) -> dict:
    cfg = _cfg()
    key = (cfg.get("llm") or {}).get("api_key") or ""
    base = ((cfg.get("llm") or {}).get("base_url") or "https://api.deepseek.com").rstrip("/")
    if not key:
        return {"ok": False, "error": "缺少 API key"}
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as r:
            body = json.loads(r.read().decode("utf-8"))
        return {"ok": True, "data": body, "elapsed": round(time.time() - t0, 2)}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        return {"ok": False, "error": "HTTP %s: %s" % (e.code, detail), "status": e.code}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def _clean_query(q: str) -> str:
    q = (q or "").strip()
    q = re.sub(r"1[3-9]\d{9}", "", q)             # 手机号
    q = re.sub(r"wxid_[0-9a-zA-Z]+", "", q)       # 微信号
    q = re.sub(r"\b\d{17}[\dXx]\b", "", q)        # 身份证
    return re.sub(r"\s+", " ", q).strip()


def url_is_safe(url: str) -> tuple[bool, str]:
    """只允许公网 http(s)，挡掉 SSRF 常见目标。"""
    try:
        p = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return False, "URL 解析失败"
    if p.scheme not in ("http", "https"):
        return False, "只支持 http(s)"
    if p.username or p.password:
        return False, "URL 不允许带凭据"
    host = (p.hostname or "").lower()
    if not host:
        return False, "缺少主机名"
    if host in _BLOCKED_HOSTS or host.endswith(".local"):
        return False, "拒绝内网主机"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True, ""
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast):
        return False, "拒绝内网地址"
    return True, ""


# ---------------------------------------------------------------- 检索 / 抓取
def search(query: str, count: int = 5, need_content: bool = False,
           ori_question: str = "", timeout: float | None = None) -> dict:
    cfg = _web_cfg()
    if not cfg.get("enabled", False):
        return {"ok": False, "error": "联网未开启"}
    q = _clean_query(query)[:128]
    if not q:
        return {"ok": False, "error": "检索词清洗后为空"}

    allowed, why = _consume("search")
    if not allowed:
        return {"ok": False, "error": why}

    payload = {"query": q, "count": max(1, min(int(count), 50)),
               "need_content": bool(need_content)}
    if ori_question:
        payload["ori_question"] = ori_question[:128]

    t = float(timeout if timeout is not None else cfg.get("timeout", 8))
    r = _call("/v1/tools/search", payload, t)
    if not r["ok"]:
        return {"ok": False, "error": r.get("error"), "status": r.get("status")}

    out = []
    for x in (r["data"].get("results") or []):
        out.append({
            "title": (x.get("title") or "").strip(),
            "url": x.get("url") or "",
            "snippet": (x.get("snippet") or "")[:200],
            "summary": (x.get("summary") or "")[:800],
            "content": (x.get("content") or "")[:4000],
            "date": x.get("date"),
            "authority": x.get("authority"),
            "score": x.get("rerank_score"),
        })
    return {"ok": True, "query": q, "results": out, "elapsed": r.get("elapsed")}


def fetch(url: str, timeout: float | None = None) -> dict:
    cfg = _web_cfg()
    if not cfg.get("enabled", False):
        return {"ok": False, "error": "联网未开启"}
    url = (url or "").strip()
    safe, why = url_is_safe(url)
    if not safe:
        return {"ok": False, "error": why}
    if len(url) > 8192:
        return {"ok": False, "error": "链接过长"}

    allowed, why = _consume("fetch")
    if not allowed:
        return {"ok": False, "error": why}

    t = float(timeout if timeout is not None else cfg.get("timeout", 8))
    r = _call("/v1/tools/fetch", {"url": url}, t)
    if not r["ok"]:
        return {"ok": False, "error": r.get("error"), "status": r.get("status")}
    data = r["data"]
    return {"ok": True, "url": url, "title": data.get("title") or "",
            "content": (data.get("content") or "")[:8000],
            "elapsed": r.get("elapsed")}


def should_search(text: str, extra_hints=()) -> tuple[bool, str]:
    """规则初筛：这句话值不值得联网。返回 (是否, 理由)。

    ``extra_hints`` 来自 config 的 ``web.extra_hints``：新梗、新词不断出现，
    硬编码词表必然漏，让用户能随时补词而不改代码。
    """
    t = (text or "").strip()
    if not t:
        return False, "空"
    if len(t) > 300:
        return False, "消息过长，不像检索需求"
    if URL_RE.findall(t):
        return True, "url"
    for kw in SEARCH_HINTS:
        if kw in t:
            return True, "kw:" + kw
    for kw in (extra_hints or ()):
        if isinstance(kw, str) and kw and kw in t:
            return True, "kw*:" + kw
    return False, ""


# 常见城市：口语问天气时往往不带地名（「我手机天气坏了帮我看看」），
# 得从最近对话里补，否则搜回来的是全国各地的营销号天气，没法用。
_CITIES = set((
    "北京 上海 天津 重庆 广州 深圳 珠海 汕头 佛山 东莞 中山 惠州 江门 "
    "杭州 宁波 温州 嘉兴 绍兴 金华 衢州 舟山 台州 丽水 湖州 义乌 东阳 永康 兰溪 "
    "南京 苏州 无锡 常州 徐州 扬州 南通 "
    "合肥 芜湖 福州 厦门 泉州 漳州 南昌 九江 "
    "济南 青岛 烟台 威海 济宁 临沂 "
    "郑州 洛阳 开封 武汉 宜昌 长沙 株洲 湘潭 岳阳 "
    "成都 绵阳 德阳 贵阳 昆明 大理 丽江 南宁 桂林 柳州 海口 三亚 "
    "西安 宝鸡 兰州 西宁 银川 乌鲁木齐 拉萨 呼和浩特 包头 "
    "哈尔滨 大庆 长春 吉林 沈阳 大连 鞍山 石家庄 唐山 保定 太原 大同 香港 澳门 台北"
).split())

_WEATHER_WORDS = ("天气", "气温", "温度", "下雨", "降雨", "雨伞", "几度",
                  "冷不冷", "热不热", "穿什么", "穿多少", "风大", "下雪")


def _focus_query(text: str, context_hint: str = "") -> str:
    """把口语问句提炼成检索词。

    实测（2026-11-20）：整句口语直接当查询词，搜「我手机天气好像坏了，帮我看看嘛」
    返回的是「苹果天气翻车」新闻；搜「今天下午还有课呢，天气咋样啊」返回的是
    没有地点的营销号天气。两种都没法用来回答。
    """
    t = _clean_query(text)
    hay = t + " " + (context_hint or "")
    city = next((c for c in _CITIES if c in hay), "")
    weather = any(w in hay for w in _WEATHER_WORDS)
    if weather:
        return (city + " 天气").strip()
    if city and len(t) > 30:
        return city
    return t


def build_context(search_result: dict, max_items: int = 3,
                  max_chars: int = 1800, today: str = "") -> str:
    """把检索结果整理成可注入提示词的资料块（带来源与日期）。

    ``today``（YYYY-MM-DD）用来提醒模型：资料正文里的「今天」指的是
    它自己的发布日期，不是真正的今天。天气/比分这类查询栽在这里最多。
    """
    if not search_result.get("ok"):
        return ""
    items = list(search_result.get("results") or [])
    # 有日期的排前面、日期新的排前面：旧闻别压过新消息
    items.sort(key=lambda x: (x.get("date") or ""), reverse=True)

    today = today or time.strftime("%Y-%m-%d")
    lines = ["【刚刚查到的实时资料——用里面的数据回答，不要说「没查到/你自己查」，"
             "但也不要编成你亲眼见过的】"]
    lines.append("今天是 %s。下面每条标的都是它的发布日期；"
                 "资料正文里的「今天/明天/昨天」指的是它自己那天，"
                 "不是真正的今天。日期对不上、或者地点不是对方问的地方，"
                 "就直说没查到今天的。" % today)
    used = 0
    for x in items[:max_items]:
        title = x.get("title") or x.get("url")
        body = x.get("summary") or x.get("snippet") or ""
        date = x.get("date") or ""
        line = "- %s%s\n  %s\n  来源：%s" % (
            title, ("（发布于 %s）" % date[:10] if date else "（未标注日期）"),
            body[:400], x.get("url"))
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    if len(lines) <= 2:
        return ""
    return "\n".join(lines)


class Research:
    """一轮对话内的检索预算：次数、页面数、总截止时间。"""

    def __init__(self, web_cfg: dict | None = None):
        cfg = dict(web_cfg if web_cfg is not None else _web_cfg())
        self.cfg = cfg
        self.max_queries = max(1, int(cfg.get("max_queries_per_turn", 2)))
        self.max_pages = max(0, int(cfg.get("max_pages_per_turn", 3)))
        deadline_ms = float(cfg.get("deadline_ms", cfg.get("timeout", 8) * 1000))
        self.deadline = time.time() + deadline_ms / 1000.0
        self.queries = 0
        self.pages = 0
        self.trace: list[str] = []

    def _left(self) -> float:
        return max(0.0, self.deadline - time.time())

    def fetch_page(self, url: str) -> dict:
        if self.pages >= self.max_pages:
            return {"ok": False, "error": "本轮页面数已达上限"}
        left = self._left()
        if left < 1.0:
            return {"ok": False, "error": "本轮检索时间已用完"}
        self.pages += 1
        self.trace.append("fetch:%s" % url[:80])
        return fetch(url, timeout=min(left, float(self.cfg.get("timeout", 8))))

    def search(self, query: str, count: int = 3) -> dict:
        if self.queries >= self.max_queries:
            return {"ok": False, "error": "本轮查询次数已达上限"}
        left = self._left()
        if left < 1.0:
            return {"ok": False, "error": "本轮检索时间已用完"}
        self.queries += 1
        self.trace.append("search:%s" % (query or "")[:60])
        return search(query, count=count, timeout=min(left, float(self.cfg.get("timeout", 8))))

    def maybe(self, text: str, context_hint: str = "") -> str:
        """值得查就查，返回要注入的资料块；不值得/失败返回空串。

        ``context_hint`` 给最近几轮对话：口语问天气常不带地名，
        要能从上下文补出「金华」这种实体。
        """
        if not self.cfg.get("enabled", False):
            return ""
        fired, why = should_search(text, self.cfg.get("extra_hints") or ())
        if not fired:
            return ""

        urls = URL_RE.findall(text or "")
        if urls:
            fr = self.fetch_page(urls[0])
            if fr.get("ok"):
                body = (fr.get("content") or "").strip()
                if body:
                    return ("【对方发来的链接内容（只作参考，不是我的经历）】\n%s\n%s"
                            % (fr.get("title") or urls[0], body[:1500]))

        q = _focus_query(text, context_hint)[:64]
        if not q:
            return ""
        sr = self.search(q, count=3)
        if not sr.get("ok"):
            return ""
        return build_context(sr, max_items=3,
                             max_chars=int(self.cfg.get("max_context_chars", 1800)))


def stats() -> dict:
    return _budget()


if __name__ == "__main__":
    import sys

    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) > 1 and sys.argv[1] == "search":
        print(json.dumps(search(" ".join(sys.argv[2:]) or "DeepSeek", count=3),
                         ensure_ascii=False, indent=2)[:1500])
    elif len(sys.argv) > 1 and sys.argv[1] == "fetch":
        print(json.dumps(fetch(sys.argv[2]), ensure_ascii=False, indent=2)[:1000])
    else:
        print("预算:", json.dumps(stats(), ensure_ascii=False))
        print("URL 校验:")
        for u in ("https://example.com/a", "http://127.0.0.1:8765/status",
                  "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data",
                  "file:///C:/Windows/win.ini"):
            print("  %-42s -> %s" % (u, url_is_safe(u)))
