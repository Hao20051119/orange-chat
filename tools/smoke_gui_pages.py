# -*- coding: utf-8 -*-
"""无头冒烟：用假 runtime 依次构建全部页面，抓 Tk 回调异常。

用法：python tools\\smoke_gui_pages.py
它不启动任何后台服务，也不会碰 GUI 的真实会话库。
"""
import sys
import time
import traceback
import tkinter as tk
from pathlib import Path
from threading import Event
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "GUI"))
import app as gui  # noqa: E402

INBOX = str(ROOT / "GUI" / ".runtime" / "demo" / "inbox")
PAGE_DATA = {
    "/status": {"ok": True, "account": "wxid_demo", "nick": "小号", "account_bound": True,
                "bound_pid": 1, "db_error": None, "last_error": None},
    "/stats": {"ok": True, "queue": {"inbound_pending": 1, "inbound_retry": 2,
                                     "outbound_draft": 3, "outbound_hold": 4,
                                     "outbound_sent": 5, "outbound_failed": 6,
                                     "outbound_uncertain": 7, "outbound_unverified": 8,
                                     "paused": ["wxid_a"], "paused_detail": [
                                         {"chat": "wxid_a", "until_ts": time.time() + 600,
                                          "reason": "manual"}]},
                "engine": {"pid": 1}},
    "/config": {"agent": {"auto_reply": True, "reply_whitelist": ["wxid_a"],
                          "group_reply": False},
                "llm": {"model": "deepseek-chat", "base_url": "https://api.deepseek.com"},
                "memory": {"enabled": True}, "web": {"enabled": False},
                "send": {"bubble": True}, "logging": {"chat_content": False},
                "kb": {"enabled": True}},
    "/inbox": {"messages": [{"ts": time.time(), "chat": "wxid_a", "display": "甲",
                             "content": "你好", "status": "done"}]},
    "/chats": {"chats": [{"username": "wxid_a", "display": "甲", "unread": 2}]},
    "/blacklist": {"blacklist": ["wxid_a"]},
    "/drafts": {"drafts": [{"id": 1, "chat": "wxid_a", "display": "甲", "status": "hold",
                            "text": "草稿正文", "gate": "sensitive",
                            "created_ts": time.time(), "retryable": True, "error": ""}]},
    "/reminders": {"enabled": False, "items": [
        {"id": 1, "chat": "wxid_a", "display": "甲", "content": "喝水",
         "rule": "daily", "next_ts": time.time() + 3600, "status": "active"}],
        "occurrences": []},
    "/proactive/contacts": {"enabled": False, "items": [
        {"username": "wxid_a", "display": "甲", "state": "正常", "next_at": time.time(),
         "day_n": 1, "per_chat_daily": 3, "no_reply_streak": 0,
         "override": {"per_chat_daily": 3}, "params": {"per_chat_daily": 3}}],
        "defaults": {"per_chat_daily": 3, "idle_min_minutes": 120, "idle_max_minutes": 360,
                     "quiet_minutes_after_user": 20, "min_gap_after_self": 90,
                     "active_hours": [9, 23]}},
    "/kb": {"enabled": True, "stats": {"documents": 3}, "items": [
        {"id": 2, "title": "资料", "scope": "public", "kind": "file"}],
        "jobs": [], "inbox": INBOX},
    "/persona": {"ok": True, "fixed_persona": "你是小明的妹妹，大一在读。",
                 "source": "card", "path": "persona_card.json", "version": 10,
                 "relations": [{"name": "老妈", "relation": "你和哥哥的妈妈",
                                "nickname": "老妈", "note": "", "chat": ""}]},
    "/log": {"lines": ["INFO 一行日志\n"]},
    "/usage": {"llm": {"calls": 3, "tokens": 1200}, "web": {}},
}


class FakeRuntime:
    exit_reason = ""
    running = True

    def __init__(self):
        self.keys_ready = Event()

    def configured_model(self):
        self.keys_ready.wait(5)
        return {"base_url": "https://api.deepseek.com", "model": "deepseek-chat",
                "key_available": True}

    def api_keys(self):
        self.keys_ready.wait(5)
        row = {"id": "fixture-key", "masked": "****", "default": True}
        return {"items": [row], "default": dict(row, key="fixture-key")}

    def discover_accounts(self):
        return [{"account": account, "nick": nick, "wxid": account}
                for account, nick in (("wxid_demo", "小号"), ("wxid_other", "另一个账号"))]

    def account_nicks(self, accounts):
        return {row["account"]: row.get("nick") for row in accounts if row.get("nick")}

    def known_nicks(self):
        return {}

    def remember_nick(self, account, nick):
        pass

    def request(self, path, payload=None, timeout=12):
        base = path.split("?", 1)[0]
        if payload is not None and base == "/drafts":
            return {"ok": True, "status": "hold"}
        if payload is not None:
            return {"ok": True}
        return PAGE_DATA.get(base, {"ok": True})

    def stop(self):
        pass


root = tk.Tk()
root.withdraw()
errors = []
root.report_callback_exception = lambda exc, val, tb: errors.append(
    "".join(traceback.format_exception(exc, val, tb)))
app = gui.OrangeApp(root, runtime=FakeRuntime())


def pump(seconds=0.4):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.01)


def setup_resize_keeps_state():
    deadline = time.monotonic() + 3
    tree, index = app.tables["accounts"]
    while "wxid_demo" not in index and time.monotonic() < deadline:
        pump(0.05)
    if "wxid_demo" not in index:
        app.runtime.keys_ready.set()
        errors.append("连接页缩放测试没有取得模拟账号")
        return
    tree.selection_set("wxid_demo")
    pump(0.1)
    app.small_confirmed.set(True)
    target_scale = 1.0 if app.scale >= 1.2 else 1.4
    with patch.object(gui, "ui_scale", return_value=target_scale):
        app._apply_scale()
        app.runtime.keys_ready.set()
        deadline = time.monotonic() + 3
        while ((app.scale != target_scale or app.pending & {"model-config", "api-keys"})
               and time.monotonic() < deadline):
            pump(0.05)
    tree, _ = app.tables["accounts"]
    if (app.scale != target_scale or tree.selection() != ("wxid_demo",)
            or not app.small_confirmed.get()):
        errors.append("连接页缩放后丢失账号或小号确认")
    if not app.configured.get("key_available") or not app.saved_keys.get("items"):
        errors.append("连接页缩放后丢失尚在读取的 API 设置或 Key")
    tree.selection_set("wxid_other")
    pump(0.1)
    if app.small_confirmed.get():
        errors.append("连接页改选另一个账号后仍保留原账号的确认")


setup_resize_keeps_state()


def setup_card_sizes():
    """连接页两张卡片在三种 API 来源下的尺寸：必须完全一致。

    高度变了会把底部按钮顶下去；宽度变了说明右侧卡片在抢左侧账号卡的宽度。
    全屏（scale=1.4）也测一遍：写死像素的弹层/说明在那时候最容易露馅。
    窗口是 withdraw 的，geometry 不会真的改尺寸，所以直接按 app.scale 模拟。
    """
    sizes = {}
    infos = {}
    for zoom, scale in (("默认", 1.0), ("全屏", 1.4)):
        app.scale = scale
        gui.style.SCALE = scale
        app.show_setup()
        pump(0.6)
        wrap = app.body.winfo_children()[0]
        grid = [w for w in wrap.winfo_children() if len(w.grid_slaves()) == 2][0]
        for name, choice in (("未选", ""), ("沿用项目已有 API", "沿用项目已有 API"),
                             ("自定义 OpenAI 兼容 API", "自定义 OpenAI 兼容 API")):
            app.model_choice.set(choice)
            app._show_model_mode()
            pump(0.25)
            cards = sorted((w.winfo_width(), w.winfo_height()) for w in grid.grid_slaves())
            sizes["%s/%s" % (zoom, name)] = (grid.winfo_width(), grid.winfo_height(),
                                             tuple(cards))
            infos["%s/%s" % (zoom, name)] = app.model_info_label.winfo_height()
    return sizes, infos


def popup_rows():
    """API 来源的下拉弹层：两个选项都要完整露出来（曾经被裁成 4px 高）。"""
    # 按类型取而不是按下标：「API 来源」是 setup_controls 里的第一个下拉
    select = next(w for w in app.setup_controls if isinstance(w, gui.PillSelect))
    select._open()
    pump(0.3)
    top = select._popup
    if top is None:
        return None
    rows = []

    def walk(widget):
        for child in widget.winfo_children():
            if isinstance(child, gui.PillButton):
                yield child
            yield from walk(child)

    for button in walk(top):
        rows.append((button._text, button.winfo_height(),
                     button.winfo_rooty() - top.winfo_rooty() + button.winfo_height()))
    popup_h = top.winfo_height()
    # 位置：必须贴着选择器，不能落在窗口/屏幕左上角（曾经因为先 map 再 geometry）
    geometry = (top.winfo_x(), top.winfo_y(),
                select.winfo_rootx(), select.winfo_rooty(), select.winfo_height())
    select._close()
    pump(0.15)
    return rows, popup_h, geometry


sizes, infos = setup_card_sizes()
print("连接页卡片尺寸：", sizes)
for zoom in ("默认", "全屏"):
    group = {k: v for k, v in sizes.items() if k.startswith(zoom + "/")}
    if len(set(group.values())) != 1:
        errors.append("%s：选择 API 来源会改变卡片尺寸：%s" % (zoom, group))
print("模型说明高度：", infos)
for zoom in ("默认", "全屏"):
    group = {k: v for k, v in infos.items() if k.startswith(zoom + "/")}
    if len(set(group.values())) != 1:
        errors.append("%s：模型说明行数在切换来源时会变：%s" % (zoom, group))

popup = popup_rows()
print("下拉弹层：", popup)
if popup is None:
    errors.append("API 来源下拉没有弹出")
else:
    rows, popup_h, geometry = popup
    if len(rows) != 2:
        errors.append("下拉里只渲染出 %d 个选项：%s" % (len(rows), rows))
    for text, height, bottom in rows:
        if height < app._s(30):
            errors.append("下拉选项「%s」被压成 %spx" % (text, height))
        if bottom > popup_h:
            errors.append("下拉选项「%s」超出弹层高度（%s > %s）" % (text, bottom, popup_h))
    popup_x, popup_y, select_x, select_y, select_h = geometry
    if popup_x <= 4 and popup_y <= 4:
        errors.append("下拉弹层跑到左上角了：popup=(%s, %s)" % (popup_x, popup_y))
    if abs(popup_x - select_x) > 8:
        errors.append("下拉弹层没有和选择器左对齐：popup_x=%s select_x=%s"
                      % (popup_x, select_x))
    # 上方放不下时允许翻到上面，所以只要求纵向贴边
    if not (abs(popup_y - (select_y + select_h + 5)) <= 8
            or abs(popup_y + popup_h - (select_y - 5)) <= 8):
        errors.append("下拉弹层没贴住选择器：popup_y=%s select_y=%s 高=%s"
                      % (popup_y, select_y, popup_h))

root.geometry("1180x760+0+0")
pump(0.5)
app.show_setup()
app.session = {"account": "wxid_demo", "nick": "小号", "account_bound": True,
               "bound_pid": 1, "model": "deepseek-chat"}
app.show_workbench()
pump(0.4)

for page in gui.PAGES:
    app.show_page(page)
    pump(0.4)
    print("页面构建：%-12s ok" % page)

print("概览指标：", {k: v.get() for k, v in app.metrics.items()})
app.show_page("drafts")
pump(0.2)
print("草稿正文框只读？", app.draft_text.cget("state") == "disabled")
print("草稿正文：", app.draft_text.get("1.0", "end-1c"))
app.show_page("blocks")
pump(0.2)
print("黑名单表：", app.tables["blocklist"][0].get_children())
print("暂停表：", app.tables["held"][0].get_children())

root.destroy()
print("\nTk 回调异常：", len(errors))
for item in errors:
    print(item)
sys.exit(1 if errors else 0)
