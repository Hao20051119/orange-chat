# -*- coding: utf-8 -*-
"""表格列宽：先要够，富余按设计比例分，短列不虚胖。

旧实现把富余**全塞给最长的那一列**：大屏上「草稿内容」能宽到 626px（内容
只用 195px），而「会话」「状态」贴着内容边，看着又空又挤。
"""
import os
import sys

import support
from support import check, section

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "GUI"))
import app as gui   # noqa: E402

CHAR = 13          # 近似一个中文字符宽
PAD = 30
FLOOR = 48


def measure(text):
    return len(str(text)) * CHAR


SPEC = [("time", "生成时间", 105, False), ("chat", "会话", 155, False),
        ("status", "状态", 112, False), ("text", "草稿内容", 285, True)]


def fit(content, avail):
    return gui.fit_columns(SPEC, content, avail, head_measure=measure,
                           pad=PAD, floor=FLOOR)


section("富余按比例分，不再一列独占")
mins, widths = fit([77, 52, 52, 247], 867)
check("总宽填满可用宽", abs(sum(widths) - 867) <= 2, str(widths))
check("每列都不小于表头宽", all(widths[i] >= mins[i] for i in range(4)), str(widths))
check("每列都放得下自己的内容",
      all(widths[i] >= c + PAD for i, c in enumerate([77, 52, 52, 247])),
      str(widths))
check("短列没被撑胖（≤ 内容 + 24）", widths[1] <= 52 + PAD + 24, str(widths[1]))
check("内容列拿到大部分富余", widths[3] > 350, str(widths[3]))
check("时间列也留了呼吸空间", widths[0] >= 77 + PAD, str(widths[0]))

section("大屏：内容列吃剩余，但不至于吞掉整张表")
mins, widths = fit([77, 52, 52, 247], 1400)
check("总宽等于可用宽", abs(sum(widths) - 1400) <= 2, str(widths))
check("内容列不超过可用宽的八成", widths[3] <= 1400 * 0.8, str(widths[3]))
check("时间列也跟着长了一点", widths[0] > 107, str(widths[0]))

section("窄屏：压缩长列、保底出滚动条")
mins, widths = fit([77, 52, 52, 247], 420)
check("压到表头宽为止，不压穿", all(widths[i] >= mins[i] for i in range(4)),
      str(widths))
check("放下时总宽等于可用宽", abs(sum(widths) - 420) <= 2, str(widths))

# 表头本身就比可用宽长：只能出横向滚动条
LONG_SPEC = [("a", "超级长的表头名称一", 120, False),
             ("b", "超级长的表头名称二", 120, False),
             ("c", "超级长的表头名称三", 120, False)]
mins, widths = gui.fit_columns(LONG_SPEC, [0, 0, 0], 200,
                               head_measure=measure, pad=PAD, floor=FLOOR)
check("min 之和超过可用宽时保持 min（靠横向滚动）",
      all(widths[i] >= mins[i] for i in range(3)) and sum(widths) > 200,
      str(widths))

section("空表：按表头宽分配，不留死白")
mins, widths = fit([0, 0, 0, 0], 867)
check("总宽填满", abs(sum(widths) - 867) <= 2, str(widths))
check("每列至少表头宽", all(widths[i] >= mins[i] for i in range(4)), str(widths))

section("真实页面：每种表的列宽都不越界")
PAGES = {
    "inbox": ([("time", "时间", 105, False), ("chat", "会话", 150, False),
               ("body", "消息", 290, True), ("status", "状态", 100, False)],
              [77, 26, 169, 39]),
    "drafts": (SPEC, [77, 52, 52, 247]),
    "reminders": ([("display", "收件人", 115, False),
                   ("content", "提醒内容", 225, True), ("rule", "重复", 85, False),
                   ("next", "下次发送", 135, False), ("status", "状态", 92, False)],
                  [52, 208, 26, 115, 39]),
    "relations": ([("name", "称呼", 100, False), ("relation", "关系", 180, True),
                   ("nickname", "你叫她/他", 100, False),
                   ("chat", "绑定会话", 200, False), ("note", "备注", 130, True)],
                  [39, 78, 26, 39, 39]),
}
for name, (spec, content) in PAGES.items():
    for avail in (300, 520, 867, 1180):
        mins, widths = gui.fit_columns(spec, content, avail,
                                       head_measure=measure, pad=PAD, floor=FLOOR)
        ok = all(widths[i] >= mins[i] for i in range(len(spec)))
        check("%s @%d 每列不低于表头宽" % (name, avail), ok, str(widths))
        # 只有「整张表放得下」的时候才要求每列都容得下内容；窄屏允许截断
        need = sum(c + PAD for c in content)
        if avail >= need:
            check("%s @%d 宽屏每列都放得下内容" % (name, avail),
                  all(widths[i] >= content[i] + PAD for i in range(len(spec))),
                  str([(i, widths[i], content[i] + PAD) for i in range(len(spec))]))
        else:
            check("%s @%d 窄屏至少不越界" % (name, avail),
                  abs(sum(widths) - avail) <= 2 or sum(widths) >= avail,
                  str(sum(widths)))

sys.exit(support.finish())
