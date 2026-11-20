# -*- coding: utf-8 -*-
"""时间注入：系统提示词里必须有明确的「现在」。"""
import sys
import time

import support
from support import check, section

import persona

section("now_line")
line = persona.now_line()
check("含【现在】", line.startswith("【现在】"), line)
check("含年份", str(time.localtime().tm_year) in line, line)
check("含星期", "周" in line, line)
check("含解释说明", "明天" in line and "几点" in line, line)

section("system prompt")
# 人设卡里写了 fixed_persona 时会顶掉 config 的 agent.persona（GUI「固定人设」页
# 改的就是人设卡）。这里把卡片指到空路径，验证没人设卡时仍回退到 config。
import os
os.environ["WXBOT_PERSONA_CARD"] = os.path.join(support.TMP, "no_persona_card.json")
cfg = support.base_cfg()
prompt = persona.build_system_prompt("【当前情况】对方在问时间", cfg)
check("提示词含现在", "【现在】" in prompt, prompt[:80])
check("提示词含硬规则", "【硬规则】" in prompt)
check("提示词不含密钥", "sk-test" not in prompt)
check("仍保留人设", "测试人设" in prompt)

prompt2 = persona.build_system_prompt("", cfg, chat="wxid_x")
check("带 chat 也能生成", "【现在】" in prompt2)

sys.exit(support.finish())
