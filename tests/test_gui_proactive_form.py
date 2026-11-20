# -*- coding: utf-8 -*-
"""主动聊天参数弹窗的下拉选项与提交值收敛（纯函数，不开窗口）。"""
import os
import sys

import support
from support import check, section

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "GUI"))
import app as gui  # noqa: E402

DEFAULTS = {"per_chat_daily": 3, "idle_min_minutes": 120, "idle_max_minutes": 360,
            "quiet_minutes_after_user": 20, "min_gap_after_self": 90,
            "active_hours": [9, 23]}

section("下拉选项")
options = gui.proactive_options("per_chat_daily", 3, None)
check("第一项是跟随全局并带全局值", options[0] == ("跟随全局（3 次）", None), options[0])
check("有「不限」档", ("不限", 0) in options, options)
check("有 5 次档", ("5 次", 5) in options, options)
check("旧的自定义值会补进选项",
      ("7 次（自定义）", 7) in gui.proactive_options("per_chat_daily", 3, 7))
options = gui.proactive_options("idle_min_minutes", 120, None)
check("分钟数念成小时", ("2 小时", 120) in options
      and options[0] == ("跟随全局（2 小时）", None), options[:3])
check("非整小时念分钟", ("90 分钟", 90) in gui.proactive_options("min_gap_after_self", 0, 90))

section("提交值收敛")
follow = {"per_chat_daily": "跟随全局（3 次）", "idle_min_minutes": "跟随全局（2 小时）",
          "idle_max_minutes": "跟随全局（6 小时）",
          "quiet_minutes_after_user": "跟随全局（20 分钟）",
          "min_gap_after_self": "跟随全局（90 分钟）",
          "active_hours": ["跟随全局", "跟随全局"]}
patch, error = gui.proactive_patch(follow, {}, DEFAULTS)
check("全跟随全局 → 全 None", not error and patch == {
    "per_chat_daily": None, "idle_min_minutes": None, "idle_max_minutes": None,
    "quiet_minutes_after_user": None, "min_gap_after_self": None,
    "active_hours": None}, (patch, error))

picked = dict(follow, per_chat_daily="2 次", idle_min_minutes="30 分钟",
              idle_max_minutes="4 小时", quiet_minutes_after_user="不静默",
              min_gap_after_self="1 小时", active_hours=["10 点", "12 点"])
patch, error = gui.proactive_patch(picked, {}, DEFAULTS)
check("选档位 → 数值", not error and patch == {
    "per_chat_daily": 2, "idle_min_minutes": 30, "idle_max_minutes": 240,
    "quiet_minutes_after_user": 0, "min_gap_after_self": 60,
    "active_hours": [10, 12]}, (patch, error))

section("错误提示")
_, error = gui.proactive_patch(dict(follow, active_hours=["跟随全局", "10 点"]), {}, DEFAULTS)
check("时段只选一半被拦", "都要" in error or "要么" in error, error)
_, error = gui.proactive_patch(dict(follow, idle_min_minutes="4 小时",
                                    idle_max_minutes="1 小时"), {}, DEFAULTS)
check("最小间隔大于最大被拦", "最小间隔不能大于最大间隔" in error, error)
_, error = gui.proactive_patch(dict(follow, per_chat_daily="瞎写的"), {}, DEFAULTS)
check("非选项文本被拦", "无效" in error, error)
patch, error = gui.proactive_patch(dict(follow, active_hours=["22 点", "6 点"]), {}, DEFAULTS)
check("跨午夜时段允许", not error and patch["active_hours"] == [22, 6], (patch, error))

section("已有覆盖值回显")
patch, error = gui.proactive_patch(
    dict(follow, per_chat_daily="7 次（自定义）"), {"per_chat_daily": 7}, DEFAULTS)
check("自定义值能原样回选", not error and patch["per_chat_daily"] == 7, (patch, error))

raise SystemExit(support.finish())
