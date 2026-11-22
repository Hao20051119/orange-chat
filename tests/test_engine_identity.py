"""用临时目录和模拟 HWND 检查账号及会话身份。"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
from support import check, section

import wx_engine as engine


section("登录实例按完整目录和唯一主窗口绑定")
root = Path(support.TMP)
account = "wxid_small_abcd"
selected = root / account
similar = root / "wxid_small_abcd_other"


def process(pid, *directories):
    files = [SimpleNamespace(path=str(p / "db_storage" / "message" / "message.db"))
             for p in directories]
    return SimpleNamespace(info={"pid": pid, "name": "Weixin.exe"}, open_files=lambda: files)


windows = lambda pid: [(pid + 10, "微信", "Qt")]
with patch.object(engine, "_wechat_windows", windows):
    with patch.object(engine.psutil, "process_iter", return_value=[process(1, similar)]):
        check("名字含小号前缀并不匹配", engine.active_account_binding(account, str(selected)) is None)
    with patch.object(engine.psutil, "process_iter", return_value=[process(1, selected, similar)]):
        check("进程同时打开两个账号库时拒绝", engine.active_account_binding(account, str(selected)) is None)
    with patch.object(engine.psutil, "process_iter", return_value=[process(1, selected), process(2, selected)]):
        try:
            engine.active_account_binding(account, str(selected))
            ambiguous = False
        except engine.AccountBindingAmbiguous:
            ambiguous = True
        check("同账号出现两个候选主窗口时明确拒绝（抛歧义异常）", ambiguous)
    with patch.object(engine.psutil, "process_iter", return_value=[process(1, selected), process(2, similar)]):
        check("唯一小号实例得到匹配", engine.active_account_binding(account, str(selected)) == (1, 11))

section("聊天窗口必须属于选定微信进程和联系人")
manager = engine.WxWindowManager(account)
manager.pid = 1
manager.open_chat = Mock(return_value=123)
manager.account_is_bound = Mock(return_value=True)
control = SimpleNamespace(ClassName=engine.CHAT_WINDOW_CLASSES[0], NativeWindowHandle=123,
                          AutomationId="ChatSingleWindowwxid_wrong")
uia = Mock()
uia._find_main.return_value = control
with (patch.object(engine, "WeChatUIA", return_value=uia),
      patch.object(engine.win32process, "GetWindowThreadProcessId", return_value=(7, 1)),
      patch.object(engine, "user32") as user32):
    result = manager.send("同名", "测试", username="wxid_right", preflight=lambda _: None)
    check("同名错会话不写入输入框", result["submitted"] is False and not uia._chat_input.called)
    check("同名错会话不投递回车", not user32.PostMessageW.called)

section("发送前恢复微信主窗口，分别报告列表不可读与目标不存在")


def try_opening(root_for_mode, tables, *, minimized=False, visible=True, close_during_lookup=False,
                show_result=False, tray_ok=None):
    main_hwnd = 11
    main = SimpleNamespace(ClassName=engine.MAIN_CLASSES[0], NativeWindowHandle=main_hwnd)
    state = SimpleNamespace(iconic=minimized, visible=visible, mode=None, now=0.0)
    manager = engine.WxWindowManager(account)
    manager.pid, manager.main_hwnd = 1, main_hwnd
    manager.account_is_bound = Mock(return_value=True)
    manager._find_chat_window = Mock(return_value=None)
    manager._open_from_session_list = Mock(return_value=123)
    manager._restore_foreground = Mock()
    fresh_uia = Mock()
    fresh_uia._find_main.side_effect = lambda: root_for_mode(state.mode, main)

    def tree_ok():
        return root_for_mode(state.mode, main) is main

    manager._tree_ready = tree_ok
    manager._session_list = lambda deadline=None: ("table" if tree_ok() else None)
    manager._main_ready = lambda: manager._window_shown() and tree_ok()
    manager._park_main_window = Mock()
    # 滚动/搜索兜底另有专门用例（test_send_resilience.py），这里只测「读不到行」的报错
    manager._recover_session_row = Mock(return_value=None)

    def show(hwnd, mode):
        state.iconic = False
        state.visible = True
        state.mode = mode

    if tray_ok is not None:
        def from_tray():
            if not tray_ok:
                return False
            state.iconic = False
            state.visible = True
            state.mode = engine.SW_RESTORE
            return True
        manager._show_from_tray = from_tray

    def find_table(*args, **kwargs):
        table = tables(*args, **kwargs)
        if close_during_lookup:
            state.visible = False
        return table

    def sleep(seconds):
        state.now += seconds

    fake_time = SimpleNamespace(monotonic=lambda: state.now, sleep=sleep)
    with (patch.object(engine, "WeChatUIA", return_value=fresh_uia) as constructor,
          patch.object(engine, "_is_fullscreen_foreground", return_value=False),
          patch.object(engine, "_find_by", side_effect=find_table) as find,
          patch.object(engine, "time", fake_time),
          patch.object(engine, "user32") as user32,
          patch.object(engine.win32gui, "IsIconic", side_effect=lambda hwnd: state.iconic),
          patch.object(engine.win32gui, "IsWindowVisible", side_effect=lambda hwnd: state.visible),
          patch.object(engine.win32gui, "GetForegroundWindow", return_value=21)):
        user32.ShowWindow.side_effect = show
        if show_result:
            result = manager.open_chat("测试会话", username="wxid_right")
        else:
            result = manager.send("测试会话", "测试", username="wxid_right", preflight=lambda _: None)

    return SimpleNamespace(result=result, modes=[call.args[1] for call in user32.ShowWindow.call_args_list],
                           uia=fresh_uia, constructors=constructor.call_args_list,
                           find_calls=find.call_count,
                           opened=manager._open_from_session_list.call_args,
                           posted=user32.PostMessageW.called, moved=user32.SetWindowPos.called,
                           foreground=manager._restore_foreground.call_args,
                           restored_window=manager.restored_window)


row = SimpleNamespace(Name="测试会话\n消息预览")
other = SimpleNamespace(Name="其他会话")
matched = SimpleNamespace(GetChildren=lambda: [row])
missing = SimpleNamespace(GetChildren=lambda: [other])
duplicate = SimpleNamespace(GetChildren=lambda: [row, row])
later = iter((None, missing, matched))
collapsed = SimpleNamespace(ClassName="QtWindow", NativeWindowHandle=11)
result = try_opening(lambda mode, main: main if mode == engine.SW_RESTORE else collapsed,
                     lambda *args, **kwargs: next(later), minimized=True, tray_ok=True,
                     show_result=True)
check("最小化也让微信自己重新显示，不用外部 ShowWindow",
      result.result == 123 and not result.modes and result.restored_window
      and result.find_calls == 3 and result.opened.args == ("测试会话", "wxid_right", row))
check("动过窗口就把它挪到屏幕外，并把前台还给用户",
      result.moved and result.foreground.args == (21,))
check("热激活复用绑定窗口的 UIA 实例",
      len(result.constructors) == 2 and all(x.kwargs["hwnd"] == 11 for x in result.constructors)
      and result.uia.ensure_materialized.called)

result = try_opening(lambda mode, main: main, lambda *args, **kwargs: matched, show_result=True)
check("正常窗口找到会话就直接打开，不改变主窗口状态",
      result.result == 123 and not result.modes and not result.restored_window)

result = try_opening(lambda mode, main: main, lambda *args, **kwargs: None)
check("已显示的主窗若始终没有列表，只报告可重试错误",
      result.result["reason"] == "session_list_unavailable" and result.result["retryable"]
      and result.result["submitted"] is False and not result.modes
      and result.opened is None and not result.posted)

result = try_opening(lambda mode, main: main, lambda *args, **kwargs: missing)
check("能读取列表但找不到目标，提示在微信打开会话且可稍后重试",
      result.result["reason"] == "no_row" and "打开该会话" in result.result["error"]
      and result.result["retryable"] and not result.modes and result.opened is None)

result = try_opening(lambda mode, main: main, lambda *args, **kwargs: duplicate)
check("重名会话不恢复、不打开、不投递",
      result.result["reason"] == "ambiguous_target" and not result.result["retryable"]
      and not result.modes and result.opened is None and not result.posted)


section("关到托盘的微信由微信自己叫回主窗")
result = try_opening(lambda mode, main: collapsed if mode != engine.SW_RESTORE else main,
                     lambda *args, **kwargs: matched, visible=False, tray_ok=True,
                     show_result=True)
check("托盘状态点微信自己的托盘图标把主窗叫回来，不调用外部 ShowWindow",
      result.result == 123 and not result.modes and result.restored_window)
check("叫回来的主窗用完挪到屏幕外",
      result.moved and result.foreground.args == (21,))

result = try_opening(lambda mode, main: collapsed, lambda *args, **kwargs: None,
                     visible=False, tray_ok=False)
check("托盘图标点不出来时仍报可重试的 main_in_tray，且确认未提交",
      result.result["reason"] == "main_in_tray" and result.result["retryable"]
      and result.result["submitted"] is False and "托盘" in result.result["error"])
check("托盘里叫不出窗口时不显示、不移动、不读列表",
      not result.modes and not result.moved and not result.find_calls
      and result.opened is None and not result.posted)

result = try_opening(lambda mode, main: main, lambda *args, **kwargs: matched,
                     tray_ok=True, close_during_lookup=True)
check("用户在读取会话时关闭主窗，发送改为等待且不重新打开主窗",
      result.result["reason"] == "main_in_tray" and result.result["retryable"]
      and result.result["submitted"] is False and result.find_calls == 1
      and not result.modes and not result.restored_window
      and result.opened is None and not result.posted)

state = SimpleNamespace(visible=True)


class ClosingRow:
    @property
    def BoundingRectangle(self):
        state.visible = False
        return SimpleNamespace(left=10, right=90, top=10, bottom=40)


manager = engine.WxWindowManager(account)
manager.pid, manager.main_hwnd = 1, 11
manager.account_is_bound = Mock(return_value=True)
manager._windows_of = Mock(return_value=[])
manager._wait_chat_window = Mock(return_value=None)
manager._session_row = Mock(return_value=None)
with (patch.object(engine.win32gui, "IsWindowVisible", side_effect=lambda hwnd: state.visible),
      patch.object(engine.win32gui, "ScreenToClient", return_value=(50, 25)),
      patch.object(engine, "_force_foreground", return_value=False) as foreground,
      patch.object(engine.time, "sleep"), patch.object(engine, "user32") as user32):
    opened = manager._open_from_session_list("测试会话", "wxid_right", ClosingRow())
    check("用户在取得行坐标时关闭主窗，直接和备用双击路径都停止",
          opened is None and (manager.last_open_error or (None,))[0] == "main_in_tray"
          and not foreground.called and not user32.ShowWindow.called
          and not user32.PostMessageW.called and not user32.mouse_event.called)

manager = engine.WxWindowManager(account)
manager.pid, manager.main_hwnd = 1, 11
manager.account_is_bound = Mock(return_value=True)
manager._windows_of = Mock(return_value=[(123, "测试会话", engine.CHAT_WINDOW_CLASSES[0])])
chat_window = SimpleNamespace(ClassName=engine.CHAT_WINDOW_CLASSES[0], NativeWindowHandle=123,
                              AutomationId="ChatSingleWindowwxid_right")
chat_uia = Mock()
chat_uia._find_main.return_value = chat_window
with (patch.object(engine, "WeChatUIA", return_value=chat_uia) as constructor,
      patch.object(engine.win32process, "GetWindowThreadProcessId", return_value=(7, 1)),
      patch.object(engine.win32gui, "IsWindowVisible", side_effect=lambda hwnd: hwnd == 123),
      patch.object(engine, "user32") as user32):
    opened = manager.open_chat("测试会话", username="wxid_right")
    check("主窗隐藏时仍可复用已按进程和会话 ID 核验的独立聊天窗",
          opened == 123 and constructor.call_args.kwargs["hwnd"] == 123
          and user32.SetWindowPos.call_args.args[0] == 123 and not user32.ShowWindow.called)
    user32.reset_mock()
    manager._show_from_tray = Mock(return_value=False)   # 别在测试里真去点托盘图标
    chat_window.AutomationId = "ChatSingleWindowwxid_wrong"
    opened = manager.open_chat("测试会话", username="wxid_right")
    check("同名独立窗身份明确不符时先尝试重新打开，叫不出主窗就报可重试",
          opened is None and manager.last_open_error[0] in ("main_in_tray", "no_window")
          and not user32.SetWindowPos.called and not user32.ShowWindow.called)
    check("身份不符的窗口不会被当成发送目标",
          manager.chat_windows.get("测试会话") is None)


section("共享队列按账号领任务和提交")
queue = support.make_queue()
chat = "wxid_contact"
old = queue.create_outbound("wxid_other", chat, "另一个账号的任务")
selected_job = queue.create_outbound(account, chat, "小号任务")
check("小号领取只拿到自己的任务", queue.next_outbound(account=account)["id"] == selected_job)
check("另一个账号的任务仍在等待", queue.get_outbound(old)["status"] == "draft")
check("未提交的气泡有必需凭据", queue.begin_outbound_attempt(selected_job, 0, 11))
check("另一个账号不能领取提交权限",
      not queue.dispatch_outbound_attempt(selected_job, "wxid_other", 12))
check("小号可以领取提交权限", queue.dispatch_outbound_attempt(selected_job, account, 12))
check("同一段不能重复领取", not queue.dispatch_outbound_attempt(selected_job, account, 12))
check("提交时更新精确的回读水位", queue.get_outbound(selected_job)["attempt_after_seq"] == 12)

now = int(time.time())
queue.enqueue_inbound("wxid_other", chat, 1, now, "text", "另一个账号", sort_seq=99)
queue.enqueue_inbound(account, chat, 1, now, "text", "小号消息", sort_seq=12)
check("小号入站按账号隔离", queue.max_seq(chat, account=account) == 12
      and len(queue.newest_pending(chat, account=account)) == 1)

sys.exit(support.finish())
