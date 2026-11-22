# -*- coding: utf-8 -*-
"""两套粗筛词库的覆盖度：该进模型判定的要进，不该进的不进。

定时提醒看 ``reminder.looks_worth_judging``（委托词 / 时间信号）；
代传话看 ``relay.looks_like_relay``（委托动词 + 代词或点名）。
"""
import os
import sys

import support
from support import check, section

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
import relay      # noqa: E402
import reminder   # noqa: E402

RELS = [{"name": "联系人A", "nickname": "组长", "chat": "wxid_a"},
        {"name": "老妈", "nickname": "妈", "chat": "wxid_b"}]

REMIND = [
    ("明天早上七点提醒我吃药", True),
    ("20 秒后提醒我喝水", True),
    ("下周三下午三点提醒我开会", True),
    ("每天晚上八点记得叫我洗澡", True),
    ("别忘了明天带伞", True),
    ("帮我记着，周五交材料", True),
    ("三号提醒我交房租", True),
    ("每周三和周五晚上叫我", True),
    ("到时候叫我一声", True),
    ("提个醒：下个月体检", True),
    ("记得明早八点叫我起来", True),
    ("过两天提醒我", True),
    ("定个闹钟，早上六点半", True),
    ("半小时后叫我", True),
    ("周末提醒我去超市", True),
    ("礼拜天记得叫我", True),
    ("每小时提醒我站起来", True),
    ("三天后提醒我回访", True),
    ("明天开始每天提醒我背单词", True),
    ("我以后每周三晚上都会空下来，你记得提醒我去公园跑步", True),
    ("今天好累啊", False),
    ("你吃饭了吗", False),
    ("哈哈哈哈", False),
    ("我在忙", False),
    ("这个电影好看", False),
    ("晚安", False),
    ("嗯嗯知道了", False),
]

RELAY = [
    ("提醒他明天带伞", True),
    ("让他明天带伞", True),
    ("叮嘱他吃药", True),
    ("嘱咐他早点睡", True),
    ("叮咛他注意安全", True),
    ("督促他写作业", True),
    ("监督他别偷懒", True),
    ("盯着他吃药", True),
    ("问问她晚饭吃什么", True),
    ("问一下他几点到", True),
    ("问一声好不好", True),
    ("跟他说一声我晚点到", True),
    ("跟他说一下我晚点到", True),
    ("告诉他我到了", True),
    ("转告他明天开会", True),
    ("转达一下我的意思", True),
    ("带句话给他", True),
    ("传个话给他", True),
    ("约她明天吃饭", True),
    ("请她帮忙", True),
    ("找他借本书", True),
    ("叫他来一趟", True),
    ("催他交作业", True),
    ("喊他起床", True),
    ("通知他开会", True),
    ("我提醒你了", False),
    ("我记得你说过", False),
    ("让我想想", False),
    ("别让他去", False),
    ("我叫外卖了", False),
    ("我不想提醒他", False),
    ("懒得叫他", False),
    ("请问洗手间在哪", False),
    ("找我有事吗", False),
    ("约吗", False),
    ("嗯嗯知道了", False),
]

section("定时提醒粗筛")
for text, want in REMIND:
    check("「%s」%s" % (text, "进判定" if want else "不触发"),
          reminder.looks_worth_judging(text) == want)

section("代传话粗筛")
for text, want in RELAY:
    check("「%s」%s" % (text, "进判定" if want else "不触发"),
          relay.looks_like_relay(text, RELS) == want)

section("否定只看动词前面")
check("内容里的「别」不会挡掉委托",
      relay.looks_like_relay("告诉他别迟到", RELS))
check("动词前的否定照样挡",
      not relay.looks_like_relay("别告诉他", RELS))
check("不想提醒他 → 挡",
      not relay.looks_like_relay("我不想提醒他", RELS))

section("一句话托好几个人：照样进判定")
check("跟两个人说一声",
      relay.looks_like_relay("帮我跟组长的老妈说一声，明天下午六点半到操场打球", RELS))
check("问两个人几点到",
      relay.looks_like_relay("帮我问问组长和老妈，他们几点到？", RELS))
check("说名字的两个人",
      relay.looks_like_relay("帮我跟小王和小李说一声，明天六点半集合",
                             RELS + [{"name": "小李", "chat": "wxid_c"}]))

sys.exit(support.finish())
