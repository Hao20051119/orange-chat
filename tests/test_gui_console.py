# -*- coding: utf-8 -*-
"""控制台补充能力：草稿改文后批准、暂停详情、黑名单列表。

只碰 support 隔离出来的临时队列库。
"""
import os
import sqlite3
import sys
from unittest.mock import Mock, patch

import support
from support import check, section

import wx_service
from wxqueue import Queue


def rows(sql, args=()):
    conn = sqlite3.connect(os.environ["WXBOT_QUEUE_DB"])
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def cell(sql, args=()):
    return rows(sql, args)[0][0]


class FakeEngine:
    pid = 1
    main_hwnd = 1
    chat_windows = {}


section("草稿正文只能改还没发出去的记录")
q = Queue()
hold_id = q.create_outbound("wxid_test", "wxid_a", "原始草稿", status="hold",
                            segments=["原始", "草稿"])
check("改写草稿正文", q.update_outbound_text(hold_id, "改过的草稿", "wxid_test"))
check("正文已落库", cell("SELECT text FROM outbound WHERE id=?", (hold_id,)) == "改过的草稿")
check("旧分段失效", cell("SELECT segments FROM outbound WHERE id=?", (hold_id,)) is None)
check("空正文拒绝", not q.update_outbound_text(hold_id, "   ", "wxid_test"))
check("别的账号改不动", not q.update_outbound_text(hold_id, "越权", "wxid_other"))
check("空正文没有覆盖原文", cell("SELECT text FROM outbound WHERE id=?", (hold_id,)) == "改过的草稿")
sent_id = q.create_outbound("wxid_test", "wxid_b", "已经发出的", status="sent")
check("已发送不能改", not q.update_outbound_text(sent_id, "改不动", "wxid_test"))

section("控制台接口：保存修改与批准发送")
svc = wx_service.Service("wxid_test")
svc.engine = FakeEngine()
wx_service.C.load = lambda **_: support.base_cfg()

r = svc.draft_action(hold_id, "update", text="第二版")
check("只保存不发送", r.get("ok") and cell(
    "SELECT text FROM outbound WHERE id=?", (hold_id,)) == "第二版", str(r))
check("保存后状态没变", cell("SELECT status FROM outbound WHERE id=?", (hold_id,)) == "hold")
check("缺少正文明确报错", not svc.draft_action(hold_id, "update")["ok"])
check("超长正文被拒", not svc.draft_action(hold_id, "update", text="长" * 4001)["ok"])

r = svc.draft_action(hold_id, "approve", text="批准时改好的正文")
check("批准成功", r.get("ok"), str(r))
check("批准后进入待发", cell("SELECT status FROM outbound WHERE id=?", (hold_id,)) == "draft")
check("发出去的是改过的正文", cell(
    "SELECT text FROM outbound WHERE id=?", (hold_id,)) == "批准时改好的正文")
check("已发送的不能再改", not svc.draft_action(sent_id, "update", text="改不动")["ok"])

section("控制台给出准确的人工批准资格")
legacy_id = q.create_outbound("wxid_test", "wxid_a", "旧会话行未打开",
                              segments=["旧会话行", "未打开"])
q.finish_outbound(legacy_id, "failed", "会话列表里没有 '测试对象'", retryable=0)
unknown_id = q.create_outbound("wxid_test", "wxid_a", "无法核实")
q.finish_outbound(unknown_id, "failed", "未知的发送错误", retryable=0)
drafts = {entry["id"]: entry for entry in svc.drafts()}
check("不自动重试的历史开窗失败可以人工批准",
      drafts[legacy_id]["can_approve"] and not drafts[legacy_id]["retryable"])
check("不暴露未知发送结果的批准入口", not drafts[unknown_id]["can_approve"])
with patch.object(wx_service.log, "warning") as warning:
    result = svc.draft_action(unknown_id, "approve", text="错误修改")
check("拒绝会记日志且不覆盖正文", not result["ok"] and warning.called
      and q.get_outbound(unknown_id)["text"] == "无法核实")
result = svc.draft_action(legacy_id, "approve", text="重新编辑的正文")
record = q.get_outbound(legacy_id)
check("旧失败允许批准，使用重新编辑的正文",
      result["ok"] and record["status"] == "draft"
      and record["text"] == "重新编辑的正文" and record["segments"] is None)

sys.path.insert(0, os.path.join(support.ROOT, "GUI"))
import app as orange_gui

gui = Mock()
gui._selected.return_value = drafts[legacy_id]
gui.draft_text.get.return_value = drafts[legacy_id]["text"]
with patch.object(orange_gui.messagebox, "askyesno", return_value=True):
    orange_gui.OrangeApp._draft_action(gui, "approve")
check("桌面面板按后端资格批准历史开窗失败",
      gui._change.call_args.args[1] == {"id": legacy_id, "action": "approve"})

gui.reset_mock()
gui._selected.return_value = drafts[unknown_id]
gui.draft_text.get.return_value = drafts[unknown_id]["text"]
with patch.object(orange_gui.messagebox, "askyesno") as dialog:
    orange_gui.OrangeApp._draft_action(gui, "approve")
check("桌面面板拦住未知结果", not gui._change.called and not dialog.called)

section("暂停详情返回给控制台")
q.pause_chat("wxid_c", minutes=10, reason="manual")
q.pause_chat("10000000001@chatroom", minutes=5, reason="takeover")
stats = svc.stats()["queue"]
check("paused_detail 带恢复时间", all(
    isinstance(row.get("until_ts"), int) for row in stats["paused_detail"]))
check("paused 仍是名字列表（兼容旧前端）",
      sorted(stats["paused"]) == sorted(["wxid_c", "10000000001@chatroom"]), str(stats["paused"]))
check("暂停会话可以恢复", (q.resume_chat("wxid_c") or True)
      and "wxid_c" not in [row["chat"] for row in q.paused_chats()])

section("知识库导入只允许白名单目录")
import gui_facilities as gf

extra = os.path.join(support.TMP, "资料")
os.makedirs(extra, exist_ok=True)
doc_path = os.path.join(extra, "需求.txt")
with open(doc_path, "w", encoding="utf-8") as f:
    f.write("项目二要求：扫码与统计")
cfg_roots = {"kb": {"ingest_roots": [extra]}}
check("白名单外的文件被拒",
      not gf._ingest_allowed(os.path.join(support.TMP, "evil.txt"), cfg_roots))
check("kb.ingest_roots 里的文件允许", gf._ingest_allowed(doc_path, cfg_roots))
check("路径穿越被 realpath 归一化后仍被拒",
      not gf._ingest_allowed(os.path.join(extra, "..", "..", "Windows", "win.ini"),
                             cfg_roots))

raise SystemExit(support.finish())
