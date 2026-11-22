# -*- coding: utf-8 -*-
"""发送鲁棒性：身份读不出来先自愈、未提交失败只退避重试、永不落 failed。

背景（Orange 另一台机器的现场）：微信 4.x 的 UIA 控件树被 Weixin.dll 里一个
gate 字节门控，字节掉回 0 时窗口对人正常、对 UIA 是空壳。旧实现直接把这种情况
判成「聊天窗口身份与选定会话不一致」（ambiguous_target，不可重试），一次误判
就把草稿作废。这里逐条钉住新行为。
"""
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
from support import check, section

import wx_engine as engine
import reply_worker as RW
import gate as gate_mod
from brain import Brain
from wxqueue import OUTBOUND_MAX_ATTEMPTS

ACC = "wxid_test"
CHAT = "wxid_bro"
SELF = "me"
HWND = 123
PID = 1
WANT = "ChatSingleWindowwxid_right"


def fake_control(autoid="", cls=None, nwh=HWND):
    return SimpleNamespace(ClassName=cls or engine.CHAT_WINDOW_CLASSES[0],
                           NativeWindowHandle=nwh, AutomationId=autoid)


class FakeTree:
    """可以按需「掉回 Qt 空壳」再被热激活救回来的假控件树。"""

    def __init__(self, autoid=WANT, class_ok=True):
        self.autoid = autoid
        self.class_ok = class_ok
        self.wakes = 0

    def root(self):
        if not self.class_ok:
            return None
        return fake_control(self.autoid)

    def ensure_materialized(self, timeout=2.0, force=False, **_):
        self.wakes += 1
        self.class_ok = True          # 热写 gate 之后树就物化了
        if not self.autoid:
            self.autoid = WANT        # mmui 根在、身份字段稍后才填上
        return True


def make_manager(tree, *, bound=True):
    manager = engine.WxWindowManager(ACC, log=lambda _msg: None)
    manager.pid, manager.main_hwnd = PID, 11
    manager.account_is_bound = Mock(return_value=bound)
    manager.open_chat = Mock(return_value=HWND)
    uia = Mock()
    uia._find_main.side_effect = tree.root
    uia.ensure_materialized.side_effect = tree.ensure_materialized
    return manager, uia


def make_input(pattern):
    inp = Mock()
    inp.GetPattern.return_value = pattern
    return inp


class FakeVP:
    def __init__(self, initial=""):
        self.value = initial
        self.sets = []

    @property
    def Value(self):
        return self.value

    def SetValue(self, text):
        self.sets.append(text)
        self.value = text


def send_once(manager, uia, *, pattern, text="测试内容", clears=True,
              writes=1, ws_sets=None, preflight=None):
    """跑一次 engine.send，返回 (结果, 写入记录, 回车次数)。"""
    ws = ws_sets if ws_sets is not None else []
    entered = []

    def post(hwnd, msg, wparam, lparam):
        if msg == engine.WM_KEYDOWN:
            entered.append((hwnd, msg))
            if clears:
                pattern.value = ""
        return True

    uia._chat_input.return_value = make_input(pattern)
    with (patch.object(engine, "WeChatUIA", return_value=uia),
          patch.object(engine.win32process, "GetWindowThreadProcessId",
                       return_value=(7, PID)),
          patch.object(engine.win32gui, "GetClassName", return_value="Qt51514QWindowIcon"),
          patch.object(engine.win32gui, "IsWindow", return_value=True),
          patch.object(engine, "user32") as user32):
        user32.PostMessageW.side_effect = post
        result = manager.send(CHAT, text, username="wxid_right",
                              preflight=preflight or (lambda _s: None))
    return result, entered, user32


section("gate 掉回 0：先热激活再复核，不再判「身份不符」")
tree = FakeTree(class_ok=False)
manager, uia = make_manager(tree)
pattern = FakeVP()
result, entered, user32 = send_once(manager, uia, pattern=pattern)
check("热激活后复核通过并真正提交", result.get("ok") and result.get("submitted"), str(result))
check("热激活只做必要次数", tree.wakes >= 1, str(tree.wakes))
check("写入的是待发正文", pattern.sets[:1] == ["测试内容"], str(pattern.sets))
check("提交回车只按一次", len(entered) == 1, str(entered))

section("autoid 有了但还没填：多等一下就自愈，不误判")
tree = FakeTree(autoid="")
manager, uia = make_manager(tree)
pattern = FakeVP()
result, entered, _ = send_once(manager, uia, pattern=pattern)
check("内容一样就继续发", result.get("submitted"), str(result))

section("明确是别人的会话：拒发、绝不写输入框、绝不回车")
tree = FakeTree(autoid="ChatSingleWindowwxid_other")
manager, uia = make_manager(tree)
pattern = FakeVP()
result, entered, user32 = send_once(manager, uia, pattern=pattern)
check("返回 identity_mismatch 且不可重试",
      result["reason"] == "identity_mismatch" and not result["retryable"], str(result))
check("没有改输入框、没有投递回车",
      not pattern.sets and not entered and not user32.PostMessageW.called)

section("树一直救不回来：归类为 identity_unreadable（可重试），而不是身份不符")
tree = FakeTree(class_ok=False)
tree.ensure_materialized = lambda timeout=2.0, force=False, **_: False
manager, uia = make_manager(tree)
pattern = FakeVP()
with patch.object(engine, "IDENTITY_WAIT", 0.3), patch.object(engine, "IDENTITY_WAKES", 1):
    result, entered, user32 = send_once(manager, uia, pattern=pattern)
check("reason 是可重试的 identity_unreadable",
      result["reason"] == "identity_unreadable" and result["retryable"]
      and result["submitted"] is False, str(result))
check("没写、没回车", not pattern.sets and not user32.PostMessageW.called)

section("输入框残留上一轮草稿：默认不动它，交给人工确认")
tree = FakeTree()
manager, uia = make_manager(tree)
pattern = FakeVP("上一轮的残稿")
result, entered, _ = send_once(manager, uia, pattern=pattern)
check("默认不删除草稿", pattern.value == "上一轮的残稿", pattern.value)
check("返回 input_busy 且可重试", result["reason"] == "input_busy" and result["retryable"],
      str(result))
check("没有写入、没有回车", not pattern.sets and not entered)

section("写入被吞掉一次：重写后仍然发出")
tree = FakeTree()
manager, uia = make_manager(tree)
pattern = FakeVP()


def flaky_set(text):
    pattern.sets.append(text)
    pattern.value = "" if len(pattern.sets) == 1 else text


pattern.SetValue = flaky_set
result, entered, _ = send_once(manager, uia, pattern=pattern)
check("重写一次成功", result.get("submitted") and len(pattern.sets) == 2, str(pattern.sets))

section("回车被吞（输入框没清空）：补按一次，不重复发整段")
tree = FakeTree()
manager, uia = make_manager(tree)
pattern = FakeVP()
state = {"n": 0}


def post_swallow(hwnd, msg, wparam, lparam):
    if msg == engine.WM_KEYDOWN:
        state["n"] += 1
        if state["n"] >= 2:
            pattern.value = ""
    return True


uia._chat_input.return_value = make_input(pattern)
with (patch.object(engine, "WeChatUIA", return_value=uia),
      patch.object(engine.win32process, "GetWindowThreadProcessId", return_value=(7, PID)),
      patch.object(engine.win32gui, "GetClassName", return_value="Qt51514QWindowIcon"),
      patch.object(engine.win32gui, "IsWindow", return_value=True),
      patch.object(engine, "user32") as user32):
    user32.PostMessageW.side_effect = post_swallow
    result = manager.send(CHAT, "测试内容", username="wxid_right", preflight=lambda _s: None)
check("第二次回车被接受，结果成功", result.get("submitted") and state["n"] == 2, str(state))
check("正文只写了一遍", pattern.sets == ["测试内容"], str(pattern.sets))

section("回车三次都没被接受：结果仍算「可能已提交」，绝不让上层重发")
tree = FakeTree()
manager, uia = make_manager(tree)
pattern = FakeVP()
uia._chat_input.return_value = make_input(pattern)
with (patch.object(engine, "WeChatUIA", return_value=uia),
      patch.object(engine.win32process, "GetWindowThreadProcessId", return_value=(7, PID)),
      patch.object(engine.win32gui, "GetClassName", return_value="Qt51514QWindowIcon"),
      patch.object(engine.win32gui, "IsWindow", return_value=True),
      patch.object(engine, "user32") as user32):
    user32.PostMessageW.return_value = True
    result = manager.send(CHAT, "测试内容", username="wxid_right", preflight=lambda _s: None)
check("submitted=True 且标了 unconfirmed，交给回读对账",
      result.get("submitted") is True and result.get("unconfirmed"), str(result))

section("账号库归属暂时读不到：按已绑定实例继续，不假装掉线")
manager = engine.WxWindowManager(ACC, log=lambda _msg: None)
manager.pid, manager.main_hwnd, manager.uia = PID, 11, object()
manager.account_dir = support.os.path.join(support.TMP, "wxid_test")
proc = Mock()
proc.name.return_value = "weixin.exe"
proc.open_files.return_value = [SimpleNamespace(
    path=support.os.path.join(manager.account_dir, "db_storage", "message", "message.db"))]
with (patch.object(engine, "active_account_binding", return_value=None),
      patch.object(engine.win32gui, "IsWindow", return_value=True),
      patch.object(engine.win32process, "GetWindowThreadProcessId", return_value=(7, PID)),
      patch.object(engine.psutil, "Process", return_value=proc)):
    check("进程仍打开该账号目录 → 视为仍绑定", manager.account_is_bound())
proc.open_files.return_value = [SimpleNamespace(
    path=support.os.path.join(support.TMP, "wxid_other", "db_storage", "x.db"))]
with (patch.object(engine, "active_account_binding", return_value=None),
      patch.object(engine.win32gui, "IsWindow", return_value=True),
      patch.object(engine.win32process, "GetWindowThreadProcessId", return_value=(7, PID)),
      patch.object(engine.psutil, "Process", return_value=proc)):
    check("读到的是别的账号库 → 不认", not manager.account_is_bound())
with patch.object(engine, "active_account_binding", return_value=None), \
     patch.object(engine.win32gui, "IsWindow", return_value=False):
    check("主窗已经没了 → 不认", not manager.account_is_bound())


# ---------------------------------------------------------------- 队列与工作线程
def make_worker(q, cfg=None):
    w = RW.Worker.__new__(RW.Worker)
    w.cfg = cfg or support.base_cfg()
    w.q = q
    w.brain = Brain(w.cfg)
    w.brain.mem = support.make_memory("resilience_%d.db" % id(w))
    w.me = SELF
    w.db = FakeDB()
    w.acc = ACC
    w._watermark = {}
    return w


class FakeDB:
    def get_messages(self, chat, limit=30):
        return []


def unsubmitted(reason="ambiguous_target", message="聊天窗口身份与选定会话不一致"):
    return {"ok": False, "submitted": False, "retryable": False,
            "reason": reason, "error": message}


section("未提交的失败：退避重排而不是落 failed，草稿年龄同时顺延")
q = support.make_queue("resilience1.db")
cfg = support.base_cfg(send={"retry_delay": 5, "retry_max_delay": 60})
w = make_worker(q, cfg)
tid = q.create_outbound(ACC, CHAT, "这条不能被判死刑")
old_created = q.get_outbound(tid)["created_ts"]
task = q.next_outbound(account=ACC)
with patch.object(RW, "post", return_value=unsubmitted()):
    res = w.send_task(task, sleep_delay=False, reload_cfg=False)
row = q.get_outbound(tid)
check("回到 draft 等重试", res["status"] == "draft" and row["status"] == "draft", str(res))
check("记了一次软重试", row["attempts"] == 1, str(row["attempts"]))
check("草稿年龄被顺延，恢复后不会立刻过期",
      row["created_ts"] > old_created, "%s -> %s" % (old_created, row["created_ts"]))
check("退避时间是 5 秒", row["not_before"] >= old_created + 5, str(row["not_before"]))
check("正文还在", row["text"] == "这条不能被判死刑")
check("没有落 failed", row["status"] != "failed")

section("退避期内领不到，过了就能再领")
check("冷却期取不到任务", q.next_outbound(account=ACC) is None)
q.reset_not_before(tid)
task = q.next_outbound(account=ACC)
with patch.object(RW, "post", return_value=unsubmitted()):
    res = w.send_task(task, sleep_delay=False, reload_cfg=False)
row = q.get_outbound(tid)
check("第二次失败退避翻倍", row["attempts"] == 2
      and row["not_before"] >= old_created + 10, "%s %s" % (row["attempts"], row["not_before"]))

section("连续失败到上限：转人工待审（hold），草稿保留，仍不是 failed")
q2 = support.make_queue("resilience2.db")
cfg2 = support.base_cfg(send={"retry_delay": 1})
w2 = make_worker(q2, cfg2)
tid2 = q2.create_outbound(ACC, CHAT, "到上限的话")
with q2._conn() as c:
    c.execute("UPDATE outbound SET attempts=? WHERE id=?", (OUTBOUND_MAX_ATTEMPTS, tid2))
task = q2.next_outbound(account=ACC)
with patch.object(RW, "post", return_value=unsubmitted()):
    res = w2.send_task(task, sleep_delay=False, reload_cfg=False)
row = q2.get_outbound(tid2)
check("转 hold 而不是 failed", res["status"] == "hold" and row["status"] == "hold", str(res))
check("原因写清是待人工", "人工" in (row["error"] or ""), row["error"])
check("正文没丢", row["text"] == "到上限的话")
check("不再被自动领取", q2.next_outbound(account=ACC) is None)

section("参数类错误直接转人工，不做无意义重试")
q3 = support.make_queue("resilience3.db")
w3 = make_worker(q3)
tid3 = q3.create_outbound(ACC, CHAT, "参数错误")
task = q3.next_outbound(account=ACC)
with patch.object(RW, "post", return_value=unsubmitted("bad_args", "会话或内容为空")):
    res = w3.send_task(task, sleep_delay=False, reload_cfg=False)
check("直接 hold", res["status"] == "hold" and q3.get_outbound(tid3)["status"] == "hold",
      str(res))

section("身份明确是别人：不空转重试，直接转人工核对")
q3b = support.make_queue("resilience3b.db")
w3b = make_worker(q3b)
tid3b = q3b.create_outbound(ACC, CHAT, "窗口是别人的会话")
task = q3b.next_outbound(account=ACC)
with patch.object(RW, "post", return_value=unsubmitted(
        "identity_mismatch", "聊天窗口身份与选定会话不一致")):
    res = w3b.send_task(task, sleep_delay=False, reload_cfg=False)
row = q3b.get_outbound(tid3b)
check("直接 hold 且正文保留",
      res["status"] == "hold" and row["status"] == "hold" and row["text"] == "窗口是别人的会话",
      str(res))

section("输入框里有人工草稿：不空转重试，直接转人工")
q3c = support.make_queue("resilience3c.db")
w3c = make_worker(q3c)
tid3c = q3c.create_outbound(ACC, CHAT, "有草稿时的话")
task = q3c.next_outbound(account=ACC)
with patch.object(RW, "post", return_value=unsubmitted(
        "input_busy", "聊天输入框里已有草稿，未自动清空")):
    res = w3c.send_task(task, sleep_delay=False, reload_cfg=False)
check("直接 hold，不删草稿也不空转",
      res["status"] == "hold" and q3c.get_outbound(tid3c)["status"] == "hold", str(res))

section("人工批准：回到 draft、清空重试计数，草稿时效从批准时刻算")
q4 = support.make_queue("resilience4.db")
tid4 = q4.create_outbound(ACC, CHAT, "被故障拖了很久的草稿", kind="reply")
with q4._conn() as c:
    c.execute("UPDATE outbound SET status='hold', created_ts=created_ts-3600 WHERE id=?",
              (tid4,))
check("批准成功", q4.approve_outbound(tid4, ACC))
row = q4.get_outbound(tid4)
check("状态回到 draft", row["status"] == "draft")
check("重试计数清零", row["attempts"] == 0)
check("草稿时效从批准时刻算（不会一批准就过期）",
      int(gate_mod.draft_timestamp(row) or 0) == int(row["approved_ts"] or 0),
      "%s %s" % (row.get("approved_ts"), row.get("created_ts")))


# ---------------------------------------------------------------- 会话行兜底
section("会话排到列表可视区外：先滚动列表找，找完滚回原处")
manager = engine.WxWindowManager(ACC, log=lambda _m: None)
manager.pid, manager.main_hwnd = PID, 11
manager._main_visible = Mock(return_value=True)
manager._bring_main_on_screen = Mock(return_value=(9000, 9000))
manager._put_main_back = Mock()
manager._scroll_list_once = Mock(return_value=True)
reads = iter([None, None, "row-deep"])
manager._read_session_row = Mock(side_effect=lambda _c: next(reads, "row-deep"))
row = manager._scroll_session_to("测试会话")
check("滚动两屏后拿到行", row == "row-deep")
check("滚下去几屏就滚回来几屏", manager._scroll_list_once.call_count == 3 + 3,
      str(manager._scroll_list_once.call_count))
check("主窗放回原来的屏幕外位置",
      manager._put_main_back.call_args.args == ((9000, 9000),))

section("滚动不可用（主窗被盖住）就改用搜索框打开会话")
manager2 = engine.WxWindowManager(ACC, log=lambda _m: None)
manager2.pid, manager2.main_hwnd = PID, 11
manager2._main_visible = Mock(return_value=True)
manager2._bring_main_on_screen = Mock(return_value=None)
manager2._put_main_back = Mock()
manager2._scroll_list_once = Mock(return_value=False)
manager2._read_session_row = Mock(return_value="row-top")
search_uia = Mock()
search_uia.open_chat.return_value = True
manager2._uia = Mock(return_value=search_uia)
row = manager2._recover_session_row("测试会话")
check("搜索打开后列表顶部拿到行", row == "row-top")
check("搜索用的就是会话显示名",
      search_uia.open_chat.call_args.args == ("测试会话",))

section("两条兜底都失败才报 no_row（仍可重试）")
manager3 = engine.WxWindowManager(ACC, log=lambda _m: None)
manager3.pid, manager3.main_hwnd = PID, 11
manager3._ensure_main_window = Mock(return_value=True)
manager3._main_visible = Mock(return_value=True)
manager3._recover_session_row = Mock(return_value=None)
main_win = SimpleNamespace(ClassName=engine.MAIN_CLASSES[0], NativeWindowHandle=11)
with (patch.object(engine, "WeChatUIA", return_value=Mock(_find_main=lambda: main_win)),
      patch.object(engine, "_find_by", return_value=SimpleNamespace(GetChildren=lambda: [])),
      patch.object(engine, "SESSION_LIST_WAIT", 0.15),
      patch.object(engine.win32gui, "IsIconic", return_value=False)):
    row = manager3._session_row("测试会话")
check("返回 None 且原因是 no_row", row is None and manager3.last_open_error[0] == "no_row")
check("提示里写明已经自动滚动并搜索",
      "自动滚动" in manager3.last_open_error[1], manager3.last_open_error[1])

section("列表里读得到但没有这一行时，会走兜底再返回")
manager4 = engine.WxWindowManager(ACC, log=lambda _m: None)
manager4.pid, manager4.main_hwnd = PID, 11
manager4._ensure_main_window = Mock(return_value=True)
manager4._main_visible = Mock(return_value=True)
manager4._recover_session_row = Mock(return_value="row-rec")
with (patch.object(engine, "WeChatUIA", return_value=Mock(_find_main=lambda: main_win)),
      patch.object(engine, "_find_by", return_value=SimpleNamespace(GetChildren=lambda: [])),
      patch.object(engine, "SESSION_LIST_WAIT", 0.15),
      patch.object(engine.win32gui, "IsIconic", return_value=False)):
    row = manager4._session_row("测试会话")
check("兜底拿到行且清空错误", row == "row-rec" and manager4.last_open_error is None)

section("滚轮：落点是列表中心、delta 向下、主窗被盖住就不滚")
manager5 = engine.WxWindowManager(ACC, log=lambda _m: None)
manager5.main_hwnd = 11
manager5._session_list = Mock(return_value=SimpleNamespace(
    BoundingRectangle=SimpleNamespace(left=100, top=100, right=300, bottom=500)))
with (patch.object(engine, "user32") as user32,
      patch.object(engine.win32gui, "WindowFromPoint", return_value=11),
      patch.object(engine.win32gui, "GetAncestor", return_value=11),
      patch.object(engine.time, "sleep")):
    ok = manager5._scroll_list_once(engine.SESSION_SCROLL_STEP)
check("光标放到列表中心", ok and user32.SetCursorPos.call_args.args == (200, 300),
      str(user32.SetCursorPos.call_args))
check("滚轮方向向下", user32.mouse_event.call_args.args[3] == engine.SESSION_SCROLL_STEP)
with (patch.object(engine, "user32") as user32,
      patch.object(engine.win32gui, "WindowFromPoint", return_value=999),
      patch.object(engine.win32gui, "GetAncestor", return_value=999),
      patch.object(engine.time, "sleep")):
    ok = manager5._scroll_list_once(engine.SESSION_SCROLL_STEP)
check("鼠标下是别人的窗口就不滚", not ok and not user32.mouse_event.called)


# ---------------------------------------------------------------- 安全边界
section("回车已投递后异常：必须报「可能已提交」，不能再重发")
tree = FakeTree()
manager6, uia6 = make_manager(tree)
pattern6 = FakeVP()
entered6 = []


def post_keyup_boom(hwnd, msg, wparam, lparam):
    if msg == engine.WM_KEYDOWN:
        entered6.append(hwnd)
        pattern6.value = ""
        return True
    raise RuntimeError("窗口已关闭")


uia6._chat_input.return_value = make_input(pattern6)
with (patch.object(engine, "WeChatUIA", return_value=uia6),
      patch.object(engine.win32process, "GetWindowThreadProcessId", return_value=(7, PID)),
      patch.object(engine.win32gui, "GetClassName", return_value="Qt51514QWindowIcon"),
      patch.object(engine.win32gui, "IsWindow", return_value=True),
      patch.object(engine, "user32") as user32):
    user32.PostMessageW.side_effect = post_keyup_boom
    result = manager6.send(CHAT, "测试内容", username="wxid_right",
                           preflight=lambda _s: None)
check("返回值标成已提交，上层只会回读对账",
      result.get("submitted") is True, str(result))
check("不是「未提交」的成功重试路径",
      not (result.get("ok") is False and result.get("submitted") is False))

section("输入框里有手写草稿：默认不清空、不回显原文")
tree = FakeTree()
manager7, uia7 = make_manager(tree)
pattern7 = FakeVP("手写的草稿，别删")
logs7 = []
manager7.log = logs7.append
result7, entered7, _ = send_once(manager7, uia7, pattern=pattern7)
check("草稿原样保留", pattern7.value == "手写的草稿，别删", pattern7.value)
check("返回可重试的 input_busy",
      result7["reason"] == "input_busy" and result7["retryable"], str(result7))
check("日志只有字数，没有原文",
      logs7 and all("手写" not in line for line in logs7), str(logs7))
check("没有按回车", not entered7)

section("配置显式允许时才清空残留草稿，日志仍不回显")
tree = FakeTree()
manager8, uia8 = make_manager(tree)
manager8.clear_input_draft = True
pattern8 = FakeVP("上一轮残稿")
logs8 = []
manager8.log = logs8.append
result8, entered8, _ = send_once(manager8, uia8, pattern=pattern8)
check("清空后正常发出", result8.get("submitted"), str(result8))
check("先清空再写正文", pattern8.sets[0] == "" and pattern8.sets[-1] == "测试内容",
      str(pattern8.sets))
check("日志不出现草稿原文", all("上一轮残稿" not in line for line in logs8), str(logs8))

section("重试不能把已取消/已发出的任务改回待发")
qs = support.make_queue("resilience_cancel.db")
tid_c = qs.create_outbound(ACC, CHAT, "取消后不该重发")
qs.next_outbound(account=ACC)
qs.finish_outbound(tid_c, "cancelled", "人工取消", bump_attempt=False)
qs.requeue_outbound(tid_c, delay=1, note="重试撞车")
check("取消状态保持不变", qs.get_outbound(tid_c)["status"] == "cancelled",
      qs.get_outbound(tid_c)["status"])
tid_s = qs.create_outbound(ACC, CHAT, "已发出的也不该回退")
qs.next_outbound(account=ACC)
qs.finish_outbound(tid_s, "sent", verified=1)
qs.requeue_outbound(tid_s, delay=1, note="重试撞车")
check("已发送状态保持不变", qs.get_outbound(tid_s)["status"] == "sent",
      qs.get_outbound(tid_s)["status"])

section("两个实例都匹配账号：明确拒绝，不走旧绑定兜底")
manager9 = engine.WxWindowManager(ACC, log=lambda _m: None)
manager9.pid, manager9.main_hwnd, manager9.uia = PID, 11, object()
manager9.account_dir = support.os.path.join(support.TMP, "wxid_test")
with (patch.object(engine, "_scan_active_account_binding", return_value=(True, (-1, -1))),
      patch.object(engine.psutil, "process_iter", return_value=[])):
    try:
        engine.active_account_binding(ACC, manager9.account_dir)
        raised = False
    except engine.AccountBindingAmbiguous:
        raised = True
    check("active_account_binding 抛歧义异常", raised)
    manager9._fallback_bound = Mock(return_value=True)
    check("account_is_bound 直接拒绝", not manager9.account_is_bound())
    check("兜底函数没有被调用", not manager9._fallback_bound.called)

section("兜底检查：读到别的账号库就拒绝，哪怕本账号缓存文件也开着")
manager10 = engine.WxWindowManager(ACC, log=lambda _m: None)
manager10.pid, manager10.main_hwnd, manager10.uia = PID, 11, object()
manager10.account_dir = support.os.path.join(support.TMP, "wxid_test")
proc10 = Mock()
proc10.name.return_value = "weixin.exe"
proc10.open_files.return_value = [
    SimpleNamespace(path=support.os.path.join(manager10.account_dir, "cache", "x.dat")),
    SimpleNamespace(path=support.os.path.join(support.TMP, "wxid_other", "db_storage",
                                              "message", "message.db")),
]
with (patch.object(engine, "active_account_binding", return_value=None),
      patch.object(engine.win32gui, "IsWindow", return_value=True),
      patch.object(engine.win32process, "GetWindowThreadProcessId", return_value=(7, PID)),
      patch.object(engine.psutil, "Process", return_value=proc10)):
    check("明确读到别的账号库 → 不认", not manager10.account_is_bound())
proc10.open_files.return_value = [
    SimpleNamespace(path=support.os.path.join(manager10.account_dir, "cache", "x.dat")),
]
with (patch.object(engine, "active_account_binding", return_value=None),
      patch.object(engine.win32gui, "IsWindow", return_value=True),
      patch.object(engine.win32process, "GetWindowThreadProcessId", return_value=(7, PID)),
      patch.object(engine.psutil, "Process", return_value=proc10)):
    check("一个 db 归属都读不到时才用弱证据兜底", manager10.account_is_bound())

section("句柄表读不到（权限/进程保护）：无法确认账号，必须拒绝")
proc10.open_files.side_effect = engine.psutil.AccessDenied()
with (patch.object(engine, "active_account_binding", return_value=None),
      patch.object(engine.win32gui, "IsWindow", return_value=True),
      patch.object(engine.win32process, "GetWindowThreadProcessId", return_value=(7, PID)),
      patch.object(engine.psutil, "Process", return_value=proc10),
      patch.object(engine.time, "sleep")):
    check("AccessDenied → 不沿用旧绑定", not manager10.account_is_bound())
proc10.open_files.side_effect = None
proc10.open_files.return_value = [
    SimpleNamespace(path=support.os.path.join(support.os.getcwd(), "cached.dat"))
]
manager10.account_dir = ""
with (patch.object(engine, "active_account_binding", return_value=None),
      patch.object(engine.win32gui, "IsWindow", return_value=True),
      patch.object(engine.win32process, "GetWindowThreadProcessId", return_value=(7, PID)),
      patch.object(engine.psutil, "Process", return_value=proc10)):
    check("没有账号目录 → 无法确认，拒绝", not manager10.account_is_bound())
manager10.account_dir = support.os.path.join(support.TMP, "wxid_test")

section("补按回车前必须重新核验会话：窗口换了就不补按")
tree = FakeTree()
manager11, uia11 = make_manager(tree)
pattern11 = FakeVP()
root_ok = fake_control(WANT)
manager11._chat_root = Mock(side_effect=[root_ok, None])
keydowns11 = []


def post_no_hit(hwnd, msg, wparam, lparam):
    if msg == engine.WM_KEYDOWN:
        keydowns11.append(hwnd)
        return False              # 第一次就没投递成功
    return True


uia11._chat_input.return_value = make_input(pattern11)
with (patch.object(engine, "WeChatUIA", return_value=uia11),
      patch.object(engine.win32process, "GetWindowThreadProcessId", return_value=(7, PID)),
      patch.object(engine.win32gui, "GetClassName", return_value="Qt51514QWindowIcon"),
      patch.object(engine.win32gui, "IsWindow", return_value=True),
      patch.object(engine, "user32") as user32):
    user32.PostMessageW.side_effect = post_no_hit
    result11 = manager11.send(CHAT, "测试内容", username="wxid_right",
                              preflight=lambda _s: None)
check("只按了一次回车，没有补按到别的会话",
      len(keydowns11) == 1, str(keydowns11))
check("返回未提交，交给上层安全重试",
      result11["submitted"] is False, str(result11))

section("发送途中输入的手写内容：不清空")
tree = FakeTree()
manager12, uia12 = make_manager(tree)
pattern12 = FakeVP()


def preflight12(submitting):
    if submitting:
        pattern12.value = "用户手写的话"
    return None


result12, entered12, _ = send_once(manager12, uia12, pattern=pattern12,
                                   preflight=preflight12)
check("用户手写内容原样保留", pattern12.value == "用户手写的话", pattern12.value)
check("返回 write_changed 且未提交",
      result12["reason"] == "write_changed" and result12["submitted"] is False,
      str(result12))
check("没有按回车", not entered12)

section("任务已被取消：转待审核不覆盖取消状态")
qc = support.make_queue("resilience_cancel2.db")
wc = make_worker(qc)
tid_c2 = qc.create_outbound(ACC, CHAT, "取消后又想转待审")
task_c2 = qc.next_outbound(account=ACC)
qc.finish_outbound(tid_c2, "cancelled", "人工取消", bump_attempt=False)
res_c2 = wc._retry_unsubmitted(task_c2, "被取消后重试", 0, "identity_mismatch")
check("返回 blocked 而不是 hold", res_c2["status"] == "blocked", str(res_c2))
check("状态仍是 cancelled", qc.get_outbound(tid_c2)["status"] == "cancelled",
      qc.get_outbound(tid_c2)["status"])

sys.exit(support.finish())
