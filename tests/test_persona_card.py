# -*- coding: utf-8 -*-
"""固定人设与关系表：GUI 两页的数据层、提示词注入、旧版关系迁移。

「老妈」这类双方共同认识的人原先只靠记忆摘要，聊久了会丢，
现在由关系表无条件注入提示词 —— 这组用例就是防回退的。
"""
import json
import os
import sys

import support
from support import check, section

import persona_card

CARD = {
    "version": 10,
    "fixed_persona": "你是小橙，大一在读：嘴硬心软，爱逗人。",
    "relations": [
        {"name": "阿橘", "relation": "你的哥哥", "nickname": "阿橘",
         "tone": "爱抬杠", "chat": "wxid_bro"},
        {"name": "老妈", "relation": "你和哥哥的妈妈", "nickname": "老妈", "chat": ""},
    ],
    "identity": {"display_name": "🍊", "role": "大一在读"},
    "personality": {"traits": ["活泼"]},
    "relationships": {"default": {"role": "朋友", "tone": "礼貌"}},
}
CARD_PATH = os.path.join(support.TMP, "persona_card.json")


def write_card(card=CARD):
    with open(CARD_PATH, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    os.environ["WXBOT_PERSONA_CARD"] = CARD_PATH


def read_card():
    with open(CARD_PATH, encoding="utf-8") as f:
        return json.load(f)


section("固定人设 + 关系表进提示词")
write_card()
card = persona_card.load_card(force=True)
prompt = persona_card.build_card_prompt(card, chat="wxid_bro")
check("固定人设在最前面", "【你的固定人设" in prompt, prompt[:80])
check("固定人设内容", "你是小橙" in prompt)
check("对话对象的关系来自关系表", "【你和对方的关系】你的哥哥" in prompt)
check("关系表带上家人", "老妈：你和哥哥的妈妈" in prompt)
check("当前对象不重复列进关系表", "- 阿橘：" not in prompt, prompt)

other = persona_card.build_card_prompt(card, chat="wxid_other")
check("陌生会话走默认关系", "【你和对方的关系】朋友" in other, other)
check("陌生会话能看到哥哥", "- 阿橘：你的哥哥" in other, other)
check("陌生会话能看到老妈", "老妈：你和哥哥的妈妈" in other)
os.environ["WXBOT_PERSONA_CARD"] = os.path.join(support.TMP, "missing_card.json")
check("没有卡片时人设段为空", persona_card.build_card_prompt(None) == "")
write_card()

section("旧版 relationships 迁移")
legacy = {
    "version": 9,
    "identity": {"display_name": "🍊"},
    "relationships": {"wxid_bro": {"role": "哥哥", "nickname": "阿橘"},
                      "default": {"role": "朋友"}},
}
write_card(legacy)
legacy_card = persona_card.load_card(force=True)
items = persona_card.effective_relations(legacy_card)
check("迁移出一条关系", len(items) == 1, str(items))
check("迁移保留绑定会话", items[0]["chat"] == "wxid_bro", str(items))
check("迁移保留称呼", items[0]["nickname"] == "阿橘", str(items))
check("旧关系仍能用于对话",
      "【你和对方的关系】哥哥" in persona_card.build_card_prompt(legacy_card, "wxid_bro"))
check("迁移结果不会被写回磁盘", "relations" not in read_card())

section("保存固定人设")
write_card()
bad = persona_card.set_fixed_persona("太短")
check("太短拒绝", not bad.get("ok"), str(bad))
ok = persona_card.set_fixed_persona("你是小橙，\n大一在读，说话短。")
check("保存成功", ok.get("ok"), str(ok))
saved = read_card()
check("文件里的固定人设已更新", "你是小橙" in saved.get("fixed_persona", ""),
      str(saved.get("fixed_persona")))
check("其它字段不丢", saved.get("identity", {}).get("display_name") == "🍊")
check("生效来源是人设卡", persona_card.effective_persona(saved)[1] == "card")
backups = [n for n in os.listdir(support.TMP) if n.startswith("persona_card.json.bak-")]
check("改前留了备份", bool(backups), str(backups))

section("保存关系表")
write_card()
items = [{"name": " 老爸 ", "relation": "你和哥哥的爸爸", "chat": "", "note": ""}]
r = persona_card.set_relations(items)
check("保存成功", r.get("ok"), str(r))
saved = read_card()
check("关系写进文件", saved.get("relations")[0]["name"] == "老爸",
      str(saved.get("relations")))
check("称呼去掉多余空格", saved["relations"][0]["name"] == "老爸")
bad = persona_card.set_relations([{"name": "老妈"}])
check("缺关系被拒绝", not bad.get("ok"), str(bad))
bad = persona_card.set_relations([{"name": "a", "relation": "b", "chat": "not a chat id"}])
check("会话 ID 非法被拒绝", not bad.get("ok"), str(bad))
bad = persona_card.set_relations([
    {"name": "a", "relation": "b", "chat": "wxid_x1"},
    {"name": "c", "relation": "d", "chat": "wxid_x1"}])
check("同一会话不能绑两条", not bad.get("ok"), str(bad))
ok = persona_card.set_relations([
    {"name": "老妈", "relation": "你和哥哥的妈妈", "nickname": "老妈", "chat": ""},
    {"name": "阿橘", "relation": "你的哥哥", "chat": "wxid_example_bro"}])
check("多条关系保存成功", ok.get("ok") and ok.get("count") == 2, str(ok))
check("保存后关系表可读", len(persona_card.effective_relations(read_card())) == 2)

section("提示词不出现两版人设")
import persona as persona_mod

cfg = support.base_cfg()
cfg["agent"]["persona"] = "旧版 config 人设：不要在提示词里重复出现。"
prompt = persona_mod.build_system_prompt("", cfg, chat="wxid_bro")
check("用人设卡的固定人设", "你是小橙" in prompt)
check("config 人设被顶掉", "旧版 config 人设" not in prompt, prompt[:400])
check("最高优先级提到关系", "你身边的人" in prompt)

write_card({"version": 9, "identity": {"display_name": "🍊"},
            "relationships": {"default": {"role": "朋友"}}})
prompt = persona_mod.build_system_prompt("", cfg, chat="wxid_bro")
check("人设卡没写固定人设时回退 config", "旧版 config 人设" in prompt, prompt[:400])

section("摘要要留下共同认识的人")
write_card()
m = support.make_memory("persona_sum.db")
for i in range(25):
    m.add("chatS", "user", "第%d句" % i, src_key="in:%d" % i)
seen = {}


def fake_llm(msgs):
    seen["prompt"] = msgs[-1]["content"]
    return {"ok": True, "text": "【摘要】聊了家里的事\n【画像】暂无\n"
                                "【事实】\n- 老妈的称呼是老妈，是对方和我的妈妈"}


r = m.summarize("chatS", fake_llm, keep_recent=10, batch=20)
check("摘要成功", r.get("ok"), str(r))
check("摘要要求记共同认识的人", "共同认识的人" in seen.get("prompt", ""),
      seen.get("prompt", "")[-200:])
check("共同认识的人写进事实", any("老妈" in f for f in m.facts("chatS")),
      str(m.facts("chatS")))

section("人设接口（/persona 的薄封装）")
write_card()
try:
    import gui_facilities
    data = gui_facilities.persona_request()
    check("GET 返回固定人设", "你是小橙" in (data.get("fixed_persona") or ""), str(data)[:200])
    check("GET 返回关系表", len(data.get("relations") or []) == 2, str(data.get("relations")))
    res = gui_facilities.persona_request(
        {"action": "save", "fixed_persona": "你是小橙，大二在读，话更少了。"})
    check("POST 保存固定人设", res.get("ok"), str(res))
    res = gui_facilities.persona_request(
        {"action": "relations", "items": [{"name": "老妈", "relation": "你和哥哥的妈妈"}]})
    check("POST 保存关系表", res.get("ok"), str(res))
    check("保存真的落盘", len(read_card().get("relations") or []) == 1)
    check("未知操作被拒绝",
          not gui_facilities.persona_request({"action": "nope"}).get("ok"))
except ImportError as exc:                     # 控制台依赖缺失时跳过
    print("  （跳过：%s）" % exc)

sys.exit(support.finish())
