# -*- coding: utf-8 -*-
"""拆气泡：中文标点、emoji、长句、短回复、上限。"""
import sys

import support
from support import check, section

from split import split_reply

section("split_reply")
bubbles = split_reply("在的在的，怎么啦～（探头）\n\n哥你换新手机啦？哇好耶，快给我看看啥样！")
check("空行拆成 2 条", len(bubbles) == 2, str(bubbles))
check("第一条内容正确", bubbles[0] == "在的在的，怎么啦～（探头）", repr(bubbles[0]))

check("单字也保留", split_reply("行") == ["行"])

# 换行就是"想分两条发"的信号，不能再被合并回一条
check("短句换行不合并",
      split_reply("在呀\n怎么啦") == ["在呀", "怎么啦"],
      str(split_reply("在呀\n怎么啦")))
check("三条短句保持三条",
      split_reply("哼\n才不要\n你猜") == ["哼", "才不要", "你猜"],
      str(split_reply("哼\n才不要\n你猜")))
check("只有标点的碎片被丢掉",
      split_reply("好\n…\n") == ["好"], str(split_reply("好\n…\n")))

long_text = ("今天上课好累啊，老师拖堂拖到十二点半，我饭都没吃上，饿死了真的，"
             "你那边怎么样呀，今天忙不忙？")
b = split_reply(long_text, max_chars=30)
check("超长按标点拆分", len(b) >= 2, str(b))
check("每段不超太多", all(len(x) <= 40 for x in b), str([len(x) for x in b]))

emoji = "哈哈哈哈哈😂😂笑死我了\n绝了真的\n你太逗了"
b = split_reply(emoji)
check("多行拆条且保留 emoji", len(b) >= 2 and any("😂" in x for x in b), str(b))

b = split_reply("一\n二\n三\n四\n五\n六", max_bubbles=3)
check("上限 3 条", len(b) <= 3, str(b))

check("空输入返回空", split_reply("   \n\n ") == [])

b = split_reply("嗯", )
check("短回复不硬拆", b == ["嗯"], str(b))

section("split + 人设样例")
sample = "哥，我想吃火锅了，你请我嘛，不请我就不理你了哼"
b = split_reply(sample, max_chars=25, max_bubbles=3)
check("撒娇长句可拆", 1 <= len(b) <= 3, str(b))
check("拼回原文（允许丢换行）", "".join(b).replace(" ", "") == sample.replace(" ", ""),
      str(b))

sys.exit(support.finish())
