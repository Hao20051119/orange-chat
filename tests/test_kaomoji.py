# -*- coding: utf-8 -*-
"""颜文字轮换：不许再"每条都是 (￣▽￣)"。

背景：人设卡里 8 组、共 60 个颜文字，实际输出 20 条带颜文字的回复里 13 条
是 (￣▽￣)——因为 kaomoji_usage 里点了名，模型照抄。这里的用例把"点名示例
已删除 + 生成后确定性轮换"钉住，防止回退。
"""
import json
import os
import random
import sys

import support
from support import check, section

import kaomoji

CARD = os.path.join(support.ROOT, "persona_card.json")
if not os.path.exists(CARD):
    # 开源版里 persona_card.json 不入库（个人人设），退回示例卡校验格式；
    # 同时告诉 agent.persona_card 读哪份，否则 load_groups() 拿到空分组。
    CARD = os.path.join(support.ROOT, "persona_card.example.json")
    os.environ.setdefault("WXBOT_PERSONA_CARD", CARD)

section("人设卡里的颜文字库")
card = json.load(open(CARD, encoding="utf-8-sig"))
st = card.get("style") or {}
groups = st.get("kaomoji_moods") or {}
check("8 种心情", len(groups) == 8, str(list(groups)))
thin = {k: len(v) for k, v in groups.items() if len(v) < 8}
check("每组至少 8 个", not thin, str(thin))
usage = st.get("kaomoji_usage") or ""
check("用法说明不再点名具体颜文字", "（" not in usage and "(" not in usage, usage)
check("用法说明强调别重复", "不要连着" in usage or "别重复" in usage, usage)

section("扫描 / 识别")
text = "又这样 (￣▽￣) 真的 (º﹃º ) 嗯 (￣▽￣)ゞ"
got = kaomoji.scan(text)
check("按出现顺序取出清单内颜文字",
      got == ["(￣▽￣)", "(￣▽￣)ゞ"], str(got))
check("不在清单里的括号不当颜文字", "(º﹃º )" not in got, str(got))
check("空文本不炸", kaomoji.scan("") == [])

section("轮换：只换最近用过的")
out, info = kaomoji.rotate("好耶 (￣▽￣)", recent=[])
check("最近没用过 → 原样保留", out == "好耶 (￣▽￣)" and not info["changed"], out)

out, info = kaomoji.rotate("好耶 (￣▽￣)", recent=["(￣▽￣)"],
                           rng=random.Random(1))
mood = kaomoji.mood_index().get("(￣▽￣)")
new_tok = kaomoji.scan(out)
check("最近用过 → 换掉", info["changed"] and out != "好耶 (￣▽￣)", out)
check("换的还是同一心情组",
      bool(new_tok) and new_tok[0] in (kaomoji.load_groups().get(mood) or []),
      "%s -> %s" % (mood, new_tok))
check("别的内容没被动", out.startswith("好耶 ") and out.endswith(new_tok[0]), out)

out, info = kaomoji.rotate("嗯 (๑>ᴗ<๑) 好", recent=["(￣▽￣)"])
check("不在清单里的颜文字不碰", out == "嗯 (๑>ᴗ<๑) 好", out)

section("轮换：连续 12 次不重样")
recent = ["(￣▽￣)"]
seen = []
for i in range(12):
    out, _ = kaomoji.rotate("行吧 (￣▽￣)", recent=recent,
                            rng=random.Random(100 + i))
    tok = kaomoji.scan(out)
    seen.append(tok[0] if tok else "")
    recent.extend(tok)
check("12 次出现 >= 10 个不同颜文字", len(set(seen)) >= 10, str(seen))
check("没有一次重复上一个", all(seen[i] != seen[i - 1] for i in range(1, len(seen))),
      str(seen))
check("同一个最多出现两次", max(seen.count(t) for t in set(seen)) <= 2, str(seen))

section("轮换：一条里两个都换")
out, info = kaomoji.rotate("好啊 (≧▽≦) 再来 (≧▽≦)", recent=["(≧▽≦)"],
                           rng=random.Random(3))
toks = kaomoji.scan(out)
check("两个都替换", len(info["replaced"]) == 2, str(info))
check("两个不重复", len(set(toks)) == 2, str(toks))
check("都还是开心组", all(t in kaomoji.load_groups()["开心"] for t in toks), str(toks))

section("最近用过清单（从记忆里取）")


class _FakeMem:
    def __init__(self, rows):
        self.rows = rows

    def recent(self, chat, limit=20):
        return self.rows[-limit:]


rows = [
    {"role": "user", "content": "在吗 (￣▽￣)"},
    {"role": "assistant", "content": "在 (￣ω￣)"},
    {"role": "assistant", "content": "嗯 (￣▽￣)"},
    {"role": "user", "content": "好"},
]
rec = kaomoji.recent_from_mem(_FakeMem(rows), "wxid_x", limit=8)
check("只取自己（assistant）发过的", rec == ["(￣ω￣)", "(￣▽￣)"], str(rec))

hint = kaomoji.recent_hint(["(￣▽￣)", "(￣▽￣)", "(￣ω￣)"])
check("提示里去重且带引导", hint.count("(￣▽￣)") == 1 and "颜文字" in hint, hint)
check("没有记录时不给提示", kaomoji.recent_hint([]) == "")

section("带不带颜文字的节奏提示")


def _mem_of(texts, role="assistant"):
    return _FakeMem([{"role": role, "content": t} for t in texts])


m_dry = _mem_of(["在的", "行吧", "那你先去忙"])
fh = kaomoji.frequency_hint(m_dry, "wxid_x")
check("连着几条都没带 → 提醒带一个", "都没带" in fh, fh)

m_full = _mem_of(["好呀 (≧▽≦)", "知道啦 (¬‿¬)", "随你 (￣▽￣)"])
fh = kaomoji.frequency_hint(m_full, "wxid_x")
check("连着两条都带了 → 提醒别带", "先别带" in fh, fh)

m_mixed = _mem_of(["好呀 (≧▽≦)", "知道啦", "随你 (￣▽￣)"])
check("节奏正常 → 不多嘴", kaomoji.frequency_hint(m_mixed, "wxid_x") == "")

uh = kaomoji.usage_hint(m_full, "wxid_x")
check("usage_hint 同时给重复与节奏提醒",
      "颜文字别重复" in uh and "先别带" in uh, uh)

section("接进 Brain：生成后自动轮换")
import brain as brain_mod
from support import base_cfg, make_memory

cfg = base_cfg(agent={"emojis": True})
b = brain_mod.Brain(cfg)
b.mem = make_memory("kao.db")
b.mem.add("wxid_x", "assistant", "早 (￣▽￣)")
b.mem.add("wxid_x", "assistant", "哦 (￣▽￣)")

_orig = brain_mod.llm_chat
brain_mod.llm_chat = lambda msgs, cfg_: {"ok": True, "text": "哼，那你说 (￣▽￣)"}
try:
    r = b.compose("wxid_x", "你说呢", sender="wxid_x")
    reply = r.get("reply") or ""
    toks = kaomoji.scan(reply)
    check("生成结果里的颜文字被换掉", toks and toks[0] != "(￣▽￣)", reply)
    check("句子主体没被改", reply.startswith("哼，那你说"), reply)
finally:
    brain_mod.llm_chat = _orig

brain_mod.llm_chat = lambda msgs, cfg_: {"ok": True, "text": "就不带颜文字"}
try:
    r = b.compose("wxid_x", "你说呢", sender="wxid_x")
    check("本来没有颜文字 → 不硬塞", r.get("reply") == "就不带颜文字", str(r))
finally:
    brain_mod.llm_chat = _orig

msgs = b.build_messages("wxid_x", "在吗")
check("提示词里带上了最近用过的颜文字",
      "颜文字别重复" in msgs[0]["content"], msgs[0]["content"][-200:])

sys.exit(support.finish())
