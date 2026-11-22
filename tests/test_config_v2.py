# -*- coding: utf-8 -*-
"""配置：新键校验、原子写、备份、环境变量覆盖、坏文件回退。"""
import json
import os
import sys

import support
from support import check, section

import config as C

section("默认值与校验")
d = C._defaults()
for key in ("bot", "send", "media", "logging"):
    check("默认含 %s" % key, key in d, str(sorted(d)))
check("气泡默认开", (d.get("send") or {}).get("bubble") is True)
check("表情包默认关（未接通）", (d.get("stickers") or {}).get("enabled") is False)
check("记忆默认开", (d.get("memory") or {}).get("enabled") is True)
check("默认使用支持图片输入的官方模型", d["llm"]["model"] == "deepseek-flash")
check("官方地址自动替换停用模型", C.model_id("https://api.deepseek.com", "deepseek-chat") == "deepseek-flash")
check("兼容接口模型名原样保留", C.model_id("https://example.test/v1", "deepseek-chat") == "deepseek-chat")

check("合法配置无问题", C._validate(d) == [], str(C._validate(d)))
bad = json.loads(json.dumps(d))
bad["agent"]["reply_whitelist"] = "wxid_a"
check("白名单类型错误被发现", any("reply_whitelist" in x for x in C._validate(bad)),
      str(C._validate(bad)))
bad2 = json.loads(json.dumps(d))
bad2["send"]["max_bubbles"] = 0
check("max_bubbles 下限检查", any("max_bubbles" in x for x in C._validate(bad2)),
      str(C._validate(bad2)))
bad3 = json.loads(json.dumps(d))
bad3["bot"]["max_parallel_chats"] = 0
check("并发下限检查", any("max_parallel_chats" in x for x in C._validate(bad3)))
bad4 = json.loads(json.dumps(d))
bad4["contacts"] = {"wxid_a": "应该是个对象"}
check("contacts 结构检查", any("contacts" in x for x in C._validate(bad4)))

section("保存 / 备份 / 回退")
tmp = os.path.join(support.TMP, "cfg")
os.makedirs(tmp, exist_ok=True)
C.CONFIG_PATH = os.path.join(tmp, "config.json")
C._state.update({"mtime": 0, "config": None})
cfg = dict(d)
cfg["agent"] = dict(d["agent"], auto_reply=True)
check("首次保存成功", C.save(cfg))
check("加载回来 auto_reply=True", C.load(force=True)["agent"]["auto_reply"] is True)

cfg["agent"]["auto_reply"] = False
C.save(cfg)
baks = [n for n in os.listdir(tmp) if n.startswith("config.json.bak-")]
check("改动前留了备份", len(baks) >= 1, str(baks))

# 写坏文件 → 回退到上一份有效配置
with open(C.CONFIG_PATH, "w", encoding="utf-8") as f:
    f.write('{"agent": {"auto_reply": ')
again = C.load(force=True)
check("坏 JSON 回退到上次有效配置", again["agent"]["auto_reply"] is False,
      str(again.get("agent", {}).get("auto_reply")))
check("标记为 degraded", C.health()["degraded"] is True)

# 恢复
C.save(cfg)
C.load(force=True)
check("degraded 复位", C.health()["degraded"] is False)

section("环境变量覆盖")
os.environ["DSHWX_API_KEY"] = "sk-from-env-123456"
os.environ["DSHWX_TOKEN"] = "tok-from-env"
got = C.load(force=True)
check("API key 用环境变量", got["llm"]["api_key"] == "sk-from-env-123456",
      got["llm"]["api_key"][:8])
check("token 用环境变量", got["token"] == "tok-from-env")
h = C.health()
check("health 标注来源", h["api_key_source"].startswith("env:"), h["api_key_source"])
check("health 不泄露明文", "sk-from-env-123456" not in json.dumps(h, ensure_ascii=False))
os.environ.pop("DSHWX_API_KEY", None)
os.environ.pop("DSHWX_TOKEN", None)

section("备份数量上限")
for i in range(9):
    cfg["agent"]["name"] = "名字%d" % i
    C.save(cfg)
baks = [n for n in os.listdir(tmp) if n.startswith("config.json.bak-")]
check("只保留最近 5 份备份", len(baks) <= 5, str(len(baks)))

sys.exit(support.finish())
