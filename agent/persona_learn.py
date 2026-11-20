# -*- coding: utf-8 -*-
"""从网络语料学习人设，并**固化**到 persona_card.json。

流程：
1. 用 Search API 的 site: 过滤，从贴吧 / B站 / 抖音抓公开语料
2. B站热门接口补充当下讨论氛围
3. 交给模型提炼「活泼可爱 + 带点腹黑」的说话特征与示例
4. 写入人设卡（带 learned_at 与 sources，便于追溯与回退）

用法：
    python persona_learn.py            # 完整跑一次并写入
    python persona_learn.py --dry      # 只打印，不写文件
"""

from __future__ import annotations

import json
import os
import sys
import time

os.system("chcp 65001 >nul 2>&1")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(ROOT, "pylibs"))

import config as C
import web_tools as wt
from llm import chat as llm_chat

CARD = os.path.join(ROOT, "persona_card.json")
SAMPLES = os.path.join(ROOT, "style_samples.json")
DRY = "--dry" in sys.argv

QUERIES = [
    ("贴吧-女生聊天", "site:tieba.baidu.com 女生 聊天 调侃 姐妹"),
    ("贴吧-可爱梗", "site:tieba.baidu.com 可爱 表情包 撒娇 梗"),
    ("B站-美少女", "site:bilibili.com 美少女 可爱 傲娇 高甜"),
    ("抖音-少女", "site:douyin.com 可爱 少女 搞笑"),
    ("网络流行语", "2026 网络流行语 女生 常用 梗 汇总"),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def fetch_bili_hot(limit: int = 8) -> list[str]:
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://api.bilibili.com/x/web-interface/popular?ps=%d&pn=1" % limit,
            headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            j = json.loads(r.read().decode("utf-8"))
        out = []
        for it in ((j.get("data") or {}).get("list") or [])[:limit]:
            out.append(it.get("title", ""))
        return out
    except Exception:
        return []


def collect() -> tuple[list[dict], list[str]]:
    corpus = []
    for name, q in QUERIES:
        r = wt.search(q, count=4)
        if not r.get("ok"):
            print("  [跳过] %s: %s" % (name, r.get("error")))
            continue
        got = []
        for x in (r.get("results") or [])[:4]:
            body = (x.get("summary") or x.get("snippet") or "").strip()
            if body:
                got.append(body[:600])
        corpus.append({"source": name, "query": q, "texts": got})
        print("  [OK] %-14s 取到 %d 段" % (name, len(got)))
        time.sleep(0.3)
    hot = fetch_bili_hot(8)
    if hot:
        print("  [OK] B站热门     取到 %d 条标题" % len(hot))
    return corpus, hot


def distill(corpus: list[dict], hot: list[str]) -> dict:
    blocks = []
    for c in corpus:
        blocks.append("【%s】\n%s" % (c["source"], "\n".join(c["texts"])))
    if hot:
        blocks.append("【B站热门标题】\n" + "\n".join(hot))
    material = "\n\n".join(blocks)[:12000]

    prompt = (
        "下面是来自贴吧、B站、抖音等中文社区的公开语料。\n\n"
        "请从中提炼「一个活泼可爱、爱撒娇、偶尔腹黑会怼人的年轻女生」在网上聊天的说话方式。"
        "要贴近真实网络用语，不要写成 AI 或者客服。\n\n"
        "语料：\n%s\n\n"
        "严格按下面格式输出，不要多余解释：\n"
        "【性格】用 6-10 个词概括（逗号分隔）\n"
        "【语气词】常用语气词，逗号分隔\n"
        "【句式】3-5 条说话习惯，每行一条\n"
        "【常用词】10-15 个高频词或梗，逗号分隔\n"
        "【口头禅】2-4 句她常挂嘴边的话\n"
        "【避免】3-5 条不要出现的腔调\n"
        "【示例】按下面场景各写一句她会怎么回，格式：场景|回复\n"
        "  在吗|...\n"
        "  别人夸她|...\n"
        "  别人说她胖|...\n"
        "  约她出去|...\n"
        "  她说累了|...\n"
        "  别人装可怜|...\n"
        "  被问是不是机器人|...\n"
        "  说晚安|...\n"
        % material
    )
    r = llm_chat([
        {"role": "system", "content": "你是中文网络语言风格分析师，只输出要求的分段内容。"},
        {"role": "user", "content": prompt},
    ], C.load())
    return r


def parse(text: str) -> dict:
    out = {"性格": "", "语气词": "", "句式": [], "常用词": "", "口头禅": [],
           "避免": [], "示例": []}
    cur = None
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        hit = None
        for k in ("性格", "语气词", "句式", "常用词", "口头禅", "避免", "示例"):
            if s.startswith("【%s】" % k):
                hit = k
                s = s[len("【%s】" % k):].strip()
                break
        if hit:
            cur = hit
            if cur in ("句式", "口头禅", "避免", "示例"):
                if s:
                    (out[cur] if cur == "示例" else out[cur]).append(s)
            elif s:
                out[cur] = s
            continue
        if not s:
            continue
        if cur == "示例":
            if "|" in s:
                out["示例"].append(s)
        elif cur in ("句式", "口头禅", "避免"):
            out[cur].append(s.lstrip("-·• 　"))
        elif cur in ("性格", "语气词", "常用词"):
            out[cur] = (out[cur] + "，" + s).strip("，")
    return out


def build_card(old: dict, d: dict) -> dict:
    card = json.loads(json.dumps(old, ensure_ascii=False)) if old else {}
    card["version"] = int(card.get("version", 1)) + 1

    traits = [x.strip() for x in (d.get("性格") or "").replace("、", ",").split(",") if x.strip()]
    card["personality"] = {
        "traits": traits[:10] or ["活泼", "可爱", "撒娇", "腹黑", "嘴硬心软"],
        "interests": card.get("personality", {}).get("interests") or [],
    }

    style = card.get("style", {}) or {}
    style["default_length"] = "一般一到两句，偶尔撒娇会连发两条，复杂话题才多说"
    style["tone"] = "活泼俏皮，熟了会撒娇，也会冷不丁怼一句（腹黑但不下头）"
    style["emoji"] = "常用 emoji 和颜文字，但别每句都堆"
    style["questions"] = "会反问、会逗人，但对方在收尾时见好就收"
    style["avoid"] = d.get("避免") or style.get("avoid") or []
    if d.get("语气词"):
        style["particles"] = [x.strip() for x in d["语气词"].replace("、", ",").split(",") if x.strip()]
    if d.get("常用词"):
        style["vocab"] = [x.strip() for x in d["常用词"].replace("、", ",").split(",") if x.strip()]
    if d.get("句式"):
        style["patterns"] = d["句式"]
    if d.get("口头禅"):
        style["catchphrases"] = d["口头禅"]
    card["style"] = style

    card["learned"] = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sources": [c["source"] for c in (card.get("_last_corpus_sources") or [])] or
                   [q[0] for q in QUERIES],
        "note": "由 persona_learn.py 从公开网络语料提炼后固化；不会被聊天内容自动改写",
    }
    card.pop("_last_corpus_sources", None)
    return card


def main():
    print("=" * 60)
    print("开始采集网络语料")
    cfg = C.load(force=True)
    orig = (cfg.get("web") or {}).get("enabled")
    cfg.setdefault("web", {})["enabled"] = True
    C.save(cfg)
    C.load(force=True)
    try:
        corpus, hot = collect()
    finally:
        cfg = C.load(force=True)
        cfg.setdefault("web", {})["enabled"] = orig
        C.save(cfg)

    if not corpus:
        print("没采到语料，退出")
        return

    print("")
    print("=" * 60)
    print("交给模型提炼人设…")
    r = distill(corpus, hot)
    if not r.get("ok"):
        print("提炼失败:", r.get("error"))
        return
    raw = r.get("text") or ""
    print(raw[:1500])
    print("")

    d = parse(raw)
    old = {}
    if os.path.exists(CARD):
        with open(CARD, "r", encoding="utf-8-sig") as f:
            old = json.load(f)
    card = build_card(old, d)

    if DRY:
        print("（dry-run，不写文件）")
        print(json.dumps(card, ensure_ascii=False, indent=2)[:2000])
        return

    # 备份旧人设卡
    if os.path.exists(CARD):
        bak = CARD + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        with open(bak, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False, indent=2)
        print("旧人设卡已备份:", os.path.basename(bak))

    with open(CARD, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    print("人设卡已更新:", CARD)

    # 示例写入风格样例库
    if d.get("示例"):
        sys.path.insert(0, BASE)
        import style_samples as ss
        n = 0
        for line in d["示例"]:
            if "|" not in line:
                continue
            scene, reply = line.split("|", 1)
            scene, reply = scene.strip(), reply.strip()
            if not reply:
                continue
            ss.add(scene=scene, context="", draft="", preferred=reply,
                   reason="网络人设学习", scope="global", approved=True)
            n += 1
        print("已写入风格样例 %d 条" % n)


if __name__ == "__main__":
    main()
