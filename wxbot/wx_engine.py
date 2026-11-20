# -*- coding: utf-8 -*-
"""微信小号后台代理服务 —— 核心引擎。

设计要点
--------
* 只在「小号」这个微信实例上操作，通过进程打开的数据目录反查 PID，
  绝不触碰大号进程。
* 收消息：直接读解密后的 SQLite（WeChatDB），零干扰、窗口最小化也能收。
* 发消息：
    - 开窗（仅首次）：把双击消息直接 PostMessage 给微信主窗口 → 独立窗口出现
      （不动鼠标、不抢前台；只有这条不灵时才退回「拉到前台用鼠标双击」）
    - 挪屏：把独立窗口 SetWindowPos 到屏幕外，视觉上完全消失
    - 发送：ValuePattern.SetValue 写内容 + PostMessage 回车（不抢焦点、不动鼠标）
    - 主窗口关闭到托盘（既不最小化也不可见）时，先热激活控件树，再让微信
      自己把主窗口显示出来（从不直接 ShowWindow，那样只会得到一块空白外壳），
      用完把主窗口挪到屏幕外，不影响用户。
* 全屏保护：检测到全屏应用时，开新窗动作排队，避免把游戏踢出全屏。
"""

from __future__ import annotations

import ctypes
import os
import re
import threading
import time
from collections.abc import Callable

import psutil
import win32gui
import win32process

from wechatauto.uia_driver import CHAT_WINDOW_CLASSES, MAIN_CLASSES, WeChatUIA, _find_by

SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
WM_KEYDOWN, WM_KEYUP, VK_RETURN = 0x0100, 0x0101, 0x0D
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_LBUTTONDBLCLK, MK_LBUTTON = 0x0203, 0x0001
GA_ROOT = 2
SW_SHOWNOACTIVATE, SW_RESTORE = 4, 9
SESSION_LIST_WAIT = 1.5
# 从托盘叫出主窗口后，等控件树物化的上限
TRAY_TREE_WAIT = 6.0

OFFSCREEN_X = -4500
user32 = ctypes.windll.user32
MAIN_TITLE = re.compile(r"(?:微信|Weixin)(?:\s*[（(]\d+[）)])?")
# 托盘图标/聊天页 tab 的名字：中文客户端是「微信」，英文客户端是 WeChat/Weixin
TRAY_HINTS = ("微信", "wechat", "weixin")


def _looks_like_wechat(text: str) -> bool:
    low = (text or "").lower()
    return any(hint in low for hint in TRAY_HINTS)


def _work_area_origin(hwnd: int) -> tuple[int, int]:
    """显示器工作区左上角（任务栏在左/上时，窗口放置坐标要按它换算）。"""
    try:
        import win32api
        info = win32api.GetMonitorInfo(win32gui.MonitorFromWindow(hwnd, 2))["Work"]
        return int(info[0]), int(info[1])
    except Exception:
        return 0, 0


def _wechat_windows(pid: int) -> list[tuple[int, str, str]]:
    windows = []

    def add_window(hwnd, _):
        try:
            _, owner_pid = win32process.GetWindowThreadProcessId(hwnd)
            if owner_pid == pid:
                cls = win32gui.GetClassName(hwnd)
                if cls.startswith("Qt") or cls in (*MAIN_CLASSES, *CHAT_WINDOW_CLASSES):
                    windows.append((hwnd, win32gui.GetWindowText(hwnd), cls))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(add_window, None)
    except win32gui.error:
        return []
    return windows


def active_account_binding(account: str, account_dir: str) -> tuple[int, int] | None:
    """只接受已打开该账号库、且有唯一主窗口的微信进程。"""
    if not account or not account_dir:
        return None
    directory = os.path.normcase(os.path.realpath(account_dir))
    if os.path.normcase(account) != os.path.basename(directory):
        return None
    db_directory = os.path.join(directory, "db_storage")
    parent = os.path.dirname(directory)
    matches = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() not in ("weixin.exe", "weixin"):
                continue
            pid = proc.info["pid"]
            mains = [h for h, title, cls in _wechat_windows(pid)
                     if cls in MAIN_CLASSES or (cls.startswith("Qt") and MAIN_TITLE.fullmatch(title))]
            if len(mains) != 1:
                continue
            open_databases = set()
            for entry in proc.open_files():
                path = os.path.normcase(os.path.realpath(entry.path))
                if not path.startswith(parent + os.sep):
                    continue
                parts = os.path.relpath(path, parent).split(os.sep)
                if len(parts) > 2 and parts[1] == "db_storage":
                    open_databases.add(os.path.join(parent, *parts[:2]))
            if open_databases == {db_directory}:
                matches.append((pid, mains[0]))
        except (OSError, psutil.Error, ValueError):
            continue
    return matches[0] if len(matches) == 1 else None


def _is_fullscreen_foreground() -> bool:
    """前台窗口是否铺满屏幕（粗略判定游戏/全屏视频）。"""
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return False
        cls = win32gui.GetClassName(hwnd)
        if cls in ("Progman", "WorkerW"):
            return False
        r = win32gui.GetWindowRect(hwnd)
        mon = win32gui.MonitorFromWindow(hwnd, 2)
        mi = win32api_monitor_info(mon)
        if not mi:
            return False
        ml, mt, mr, mb = mi
        return (r[0] <= ml + 2 and r[1] <= mt + 2 and r[2] >= mr - 2 and r[3] >= mb - 2)
    except Exception:
        return False


def win32api_monitor_info(mon):
    try:
        import win32api
        return win32api.GetMonitorInfo(mon)["Monitor"]
    except Exception:
        return None


def _force_foreground(hwnd: int) -> bool:
    """尽力把窗口弄到前台，并如实返回是否成功。

    微信多实例/用户正在操作时，``SetForegroundWindow`` 会被系统拒绝；这里先
    试常规手段，再用 ``AttachThreadInput`` 挂到当前前台线程上重试一次，最后
    用 ``GetForegroundWindow`` 校验——调用方必须拿到 True 才可以真去点鼠标。
    """
    try:
        if not win32gui.IsWindowVisible(hwnd):
            return False
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        try:
            user32.AllowSetForegroundWindow(-1)   # ASFW_ANY
        except Exception:
            pass
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return False
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.25)
        if win32gui.GetForegroundWindow() == hwnd:
            return True

        k32 = ctypes.windll.kernel32
        fg = win32gui.GetForegroundWindow()
        if not fg:
            return False
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        my_tid = k32.GetCurrentThreadId()
        if fg_tid and fg_tid != my_tid:
            if user32.AttachThreadInput(my_tid, fg_tid, True):
                try:
                    if win32gui.IsWindowVisible(hwnd):
                        user32.SetForegroundWindow(hwnd)
                        time.sleep(0.2)
                finally:
                    user32.AttachThreadInput(my_tid, fg_tid, False)
        return win32gui.GetForegroundWindow() == hwnd
    except Exception:
        return False


class WxWindowManager:
    """负责在小号实例上：找窗口、开独立聊天窗、后台发送。"""

    def __init__(self, account: str, log=print, account_dir: str = ""):
        self.account = account
        self.account_dir = account_dir
        self.log = log
        self.pid = None
        self.main_hwnd = None
        self.uia = None
        self.chat_windows: dict[str, int] = {}
        # RLock：open_chat 持锁期间会调用 connect()，后者也要拿同一把锁
        self._lock = threading.RLock()
        # 发送串行锁：写入→回车→核验必须独占，避免并发覆盖输入框
        self._send_lock = threading.Lock()
        self.me_username: str | None = None
        # 最近一次开窗失败的原因代码与说明，供 send() 区分「可重试」和「真没有」
        self.last_open_error: tuple[str, str] | None = None
        # 本次开窗是否动过主窗口状态（最小化/托盘）：动过就等用完放回任务栏
        self.restored_window = False
        # 动主窗口之前记下它的位置和最小化状态，收尾时放回原处
        self.saved_rect: tuple[int, int] | None = None
        self.saved_iconic = False

    # ---------------------------------------------------------------- 定位实例
    def _windows_of(self, pid: int):
        return _wechat_windows(pid)

    def account_is_bound(self) -> bool:
        with self._lock:
            if not self.pid or not self.main_hwnd or self.uia is None:
                return False
            return active_account_binding(self.account, self.account_dir) == (self.pid, self.main_hwnd)

    def connect(self) -> bool:
        with self._lock:
            self.pid = self.main_hwnd = self.uia = None
            self.chat_windows.clear()
            if not self.account_dir:
                from wechatauto.db import list_accounts
                paths = [a["path"] for a in list_accounts() if a.get("account") == self.account]
                if len(paths) == 1:
                    self.account_dir = paths[0]
            binding = active_account_binding(self.account, self.account_dir)
            if binding is None:
                self.log("无法核验已登录小号 %s 的微信窗口" % self.account)
                return False
            pid, hwnd = binding
            uia = WeChatUIA(timeout=8.0, hwnd=hwnd)
            self.pid, self.main_hwnd, self.uia = pid, hwnd, uia
            root = uia._find_main()
            if root is None or root.ClassName not in MAIN_CLASSES:
                # 微信 4.x 冷启动 / 最小化 / 收进托盘时 UIA 树不物化（根类名还是 Qt 空壳），
                # 直接判定失败会让"窗口缩在任务栏/收在托盘"这种正常状态也起不来。
                try:
                    if not uia.ensure_materialized(timeout=6.0):
                        self.log("热激活微信 UIA 没成功，尝试让微信自己打开主窗口")
                    root = uia._find_main()
                except Exception as exc:  # noqa: BLE001 激活失败就走原来的报错
                    self.log("热激活微信 UIA 失败：%s: %s" % (type(exc).__name__, exc))
                if root is None or root.ClassName not in MAIN_CLASSES:
                    if self._ensure_main_window():      # 最小化就还原，收在托盘就点托盘图标
                        root = uia._find_main()
            if (root is None or root.ClassName not in MAIN_CLASSES
                    or int(root.NativeWindowHandle) != hwnd):
                self.log("无法读取所选微信的主界面。请在微信打开小号主窗口后重试")
                self.pid = self.main_hwnd = self.uia = None
                return False
            self.log("已绑定小号 PID=%s hwnd=%s" % binding)
            if self.restored_window:
                # 只是启动服务，别把微信窗口留在屏幕上
                self._park_main_window()
                self.restored_window = False
            return True

    # ---------------------------------------------------------------- 开独立窗
    def _find_chat_window(self, chat: str) -> int | None:
        hits = [h for h, title, cls in self._windows_of(self.pid or 0)
                if title == chat and h != self.main_hwnd]
        if len(hits) > 1:
            self.last_open_error = ("ambiguous_target", "有多个同名聊天窗口，无法确认发送目标")
        return hits[0] if len(hits) == 1 else None

    def _move_offscreen(self, hwnd: int):
        user32.SetWindowPos(hwnd, 0, OFFSCREEN_X, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    # ---------------------------------------------------------------- 托盘恢复
    def _uia(self):
        """绑定窗口可用的 UIA 句柄（只读，不触发任何窗口动作）。"""
        if self.uia is None:
            self.uia = WeChatUIA(timeout=8.0, hwnd=self.main_hwnd)
        return self.uia

    def _tree_ready(self) -> bool:
        """mmui 控件树在不在：微信收进托盘后根会退回 Qt 空壳，一个控件都读不到。"""
        try:
            win = self._uia()._find_main()
            return (win is not None and win.ClassName in MAIN_CLASSES
                    and int(win.NativeWindowHandle) == self.main_hwnd)
        except Exception:
            return False

    def _safe_visible(self) -> bool:
        try:
            return bool(win32gui.IsWindowVisible(self.main_hwnd))
        except Exception:
            return False

    def _safe_iconic(self) -> bool:
        try:
            return bool(win32gui.IsIconic(self.main_hwnd))
        except Exception:
            return False

    def _window_shown(self) -> bool:
        """主窗既可见又没最小化（收进托盘时两者都不满足）。"""
        try:
            return self._safe_visible() and not win32gui.IsIconic(self.main_hwnd)
        except Exception:
            return False

    def _activate_tree(self, timeout: float = TRAY_TREE_WAIT) -> bool:
        """只热写 accessibility gate 让控件树物化：不显示、不移动、不置前窗口。"""
        if self._tree_ready():
            return True
        try:
            self._uia().ensure_materialized(timeout=timeout, force=True)
        except Exception as exc:
            self.log("热激活微信 UIA 失败：%s: %s" % (type(exc).__name__, exc))
        return self._tree_ready()

    def _session_list(self, deadline=None):
        """左侧会话列表控件；微信退到托盘或只剩 Qt 空壳时读不到它。"""
        win = self._uia()._find_main()
        if win is None:
            return None
        extra = {"deadline": deadline} if deadline is not None else {}
        return _find_by(win, lambda c: (c.AutomationId or "").endswith("session_list"),
                        max_depth=30, **extra)

    def _main_ready(self) -> bool:
        """主窗真的能用：可见、没最小化、控件树在、会话列表也读得到。"""
        if not self._window_shown() or not self._tree_ready():
            return False
        return self._session_list(deadline=time.monotonic() + 0.8) is not None

    def _chat_tab(self):
        """左侧竖栏里「聊天」那一页的按钮（微信 4.x 里它的名字是「微信」）。"""
        win = self._uia()._find_main()
        if win is None:
            return None
        for name in ("微信", "聊天", "Chats", "WeChat"):
            hit = _find_by(win, lambda c, n=name: ((c.ClassName or "") == "mmui::XTabBarItem"
                                                   and (c.Name or "").strip() == n),
                           max_depth=30)
            if hit is not None:
                return hit
        return None

    def _click_chat_tab(self) -> bool:
        """用户在通讯录/收藏/发现页时，切回聊天页才有会话列表。"""
        tab = self._chat_tab()
        if tab is None:
            return False
        try:
            tab.Click()
            time.sleep(0.4)
            return True
        except Exception as exc:
            self.log("切回聊天页失败：%s: %s" % (type(exc).__name__, exc))
            return False

    def _show_main(self) -> bool:
        """让微信自己把主窗口显示出来（托盘/最小化都靠点它自己的托盘图标）。

        外部 ``ShowWindow`` 只会得到白板，所以这里不碰窗口，只点托盘图标。
        """
        if self._window_shown():
            return True
        return self._show_from_tray()

    # ---- 托盘图标：Win11 收在「显示隐藏的图标」溢出面板里，Win10 结构不同也兼容 ----
    def _tray_icon_buttons(self):
        """任务栏托盘区（常显部分）里的图标按钮。

        Win11 的托盘按钮类名以 ``SystemTray.`` 开头；Win10 常显区是老式
        ``ToolbarWindow32``，类名甚至是空的——所以这里放宽成「不是任务栏应用按钮」。
        """
        import uiautomation as auto
        out = []
        try:
            bar = auto.PaneControl(searchDepth=1, ClassName="Shell_TrayWnd")
            if not bar.Exists(0.3, 0.1):
                return out
            stack = [bar]
            while stack:
                node = stack.pop()
                try:
                    cls = node.ClassName or ""
                    if (node.ControlTypeName == "ButtonControl"
                            and (cls.startswith("SystemTray.") or not cls.startswith("Taskbar."))):
                        out.append(node)
                except Exception:
                    pass
                try:
                    stack.extend(node.GetChildren())
                except Exception:
                    pass
        except Exception as exc:
            self.log("枚举任务栏托盘失败：%s: %s" % (type(exc).__name__, exc))
        return out

    def _chevron(self):
        """「显示隐藏的图标」那个箭头按钮（Win11 有 AutomationId，Win10 只能按名字找）。"""
        import uiautomation as auto
        try:
            bar = auto.PaneControl(searchDepth=1, ClassName="Shell_TrayWnd")
            if not bar.Exists(0.3, 0.1):
                return None
            for name in ("显示隐藏的图标", "Show hidden icons"):
                hit = bar.ButtonControl(AutomationId="SystemTrayIcon", Name=name)
                if hit.Exists(0.15, 0.1):
                    return hit
            stack = [bar]
            while stack:
                node = stack.pop()
                try:
                    name = (node.Name or "").lower()
                    if (node.ControlTypeName == "ButtonControl"
                            and ("隐藏的图标" in name or "hidden icon" in name
                                 or "chevron" in name)):
                        return node
                except Exception:
                    pass
                try:
                    stack.extend(node.GetChildren())
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _overflow_visible(self):
        """当前真正显示着的溢出面板窗口（Win11 会留一个隐藏的同名窗口）。"""
        import uiautomation as auto
        try:
            for w in auto.GetRootControl().GetChildren():
                try:
                    cls = w.ClassName or ""
                    if ("Overflow" in cls or "XamlIsland" in cls) and win32gui.IsWindowVisible(
                            int(w.NativeWindowHandle)):
                        return w
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _overflow_flyout(self):
        """「显示隐藏的图标」溢出面板（已开就直接用，没开就点开）。"""
        flyout = self._overflow_visible()
        if flyout is not None:
            return flyout
        chevron = self._chevron()
        if chevron is not None:
            try:
                chevron.Click()
                time.sleep(0.8)
            except Exception as exc:
                self.log("展开托盘溢出面板失败：%s: %s" % (type(exc).__name__, exc))
        return self._overflow_visible()

    def _close_overflow(self):
        """把溢出面板收起来：再点一次 chevron。

        注意不能用 Esc——微信主窗口的 Esc 就是「隐藏到托盘」，会把刚拉起来
        的窗口又藏回去。
        """
        if self._overflow_visible() is None:
            return
        chevron = self._chevron()
        if chevron is None:
            return
        try:
            chevron.Click()
            time.sleep(0.4)
        except Exception:
            pass

    def _wechat_tray_buttons(self):
        """名字里带微信的托盘图标按钮：先看常显区，再看溢出面板。"""
        hits = [b for b in self._tray_icon_buttons()
                if _looks_like_wechat(getattr(b, "Name", ""))]
        if hits:
            return hits
        flyout = self._overflow_flyout()
        if flyout is None:
            return []
        out, names = [], []
        try:
            stack = [flyout]
            while stack:
                node = stack.pop()
                try:
                    name = node.Name or ""
                    kind = node.ControlTypeName
                except Exception:
                    continue
                if name and kind in ("ButtonControl", "ListItemControl", "MenuItemControl"):
                    names.append(name.replace("\n", " ")[:40])
                    if _looks_like_wechat(name):
                        out.append(node)
                try:
                    stack.extend(node.GetChildren())
                except Exception:
                    pass
        except Exception as exc:
            self.log("读取托盘溢出面板失败：%s: %s" % (type(exc).__name__, exc))
            return []
        if not out:
            self.log("溢出面板里没找到微信图标，面板里有：%s" % "、".join(names[:12]))
        return out

    def _wait_shown(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._window_shown():
                return True
            time.sleep(0.15)
        return self._window_shown()

    def _show_from_tray(self) -> bool:
        """点微信托盘图标，让微信自己把主窗口打开。

        多个微信账号同时在跑时，托盘里会有多个「微信」图标，逐个点、每次都
        用本实例的 hwnd 是否真的可见来校验，避免点了别的账号。只有一个图标时
        绝不点第二次——托盘图标是开关，点两下等于又藏回去。
        """
        buttons = []
        for _ in range(2):
            buttons = self._wechat_tray_buttons()
            if buttons:
                break
            time.sleep(0.6)                 # 溢出面板偶尔要等一下才枚举得到
        if not buttons:
            self.log("托盘里没找到微信图标，无法自动叫出主窗口")
            return False
        for attempt in range(min(len(buttons), 4)):
            button = buttons[min(attempt, len(buttons) - 1)]
            try:
                button.Click()
            except Exception as exc:
                self.log("点击微信托盘图标失败：%s: %s" % (type(exc).__name__, exc))
                self._close_overflow()
                continue
            if self._wait_shown(3.0):
                self._close_overflow()
                return True
            if len(buttons) == 1:
                break
        self._close_overflow()
        return self._window_shown()

    def _park_main_window(self):
        """用完把主窗放回原处并藏起来（原来收在托盘就藏回去，原来最小化就最小化）。

        不能只挪到屏幕外：那样用户点托盘/任务栏图标只会把窗口"激活"在屏幕外，
        看着像打不开。用一次 ``SetWindowPlacement`` 同时做到"位置回到原处 +
        不可见"，也不会像 ``SetWindowPos`` 那样把最小化的窗口拽回桌面。
        """
        if not self.main_hwnd:
            return
        try:
            flags, _cmd, pt_min, pt_max, rect = win32gui.GetWindowPlacement(self.main_hwnd)
            if self.saved_rect:
                # saved_rect 是屏幕坐标，SetWindowPlacement 要的是工作区坐标
                ox, oy = _work_area_origin(self.main_hwnd)
                width = max(1, rect[2] - rect[0])
                height = max(1, rect[3] - rect[1])
                left, top = self.saved_rect[0] - ox, self.saved_rect[1] - oy
                rect = (left, top, left + width, top + height)
            show = 2 if self.saved_iconic else 0        # 2=最小化到任务栏, 0=隐藏(托盘)
            win32gui.SetWindowPlacement(self.main_hwnd, (flags, show, pt_min, pt_max, rect))
            time.sleep(0.15)
        except Exception:
            pass

    def _remember_position(self):
        """记下主窗"还原后"该在的位置。

        最小化时 ``GetWindowRect`` 是 -32000；我们自己工作时也会把它挪到屏幕外。
        这两种都不是用户想在的位置，退回微信自己记住的窗口位置（radium/window.config）。
        """
        pos = None
        try:
            if win32gui.IsIconic(self.main_hwnd):
                # GetWindowPlacement 的 rcNormalPosition 是"工作区坐标"，转成屏幕坐标
                ox, oy = _work_area_origin(self.main_hwnd)
                rc = win32gui.GetWindowPlacement(self.main_hwnd)[4]
                pos = (int(rc[0]) + ox, int(rc[1]) + oy)
            else:
                r = win32gui.GetWindowRect(self.main_hwnd)
                pos = (int(r[0]), int(r[1]))
        except Exception:
            pos = None
        if (pos is None or not (-1000 < pos[0] < 10000) or not (-1000 < pos[1] < 10000)):
            pos = self._default_position()
        self.saved_rect = pos

    @staticmethod
    def _default_position():
        """微信自己记住的窗口位置（拿不到就用一个稳妥的默认值）。"""
        try:
            import json
            path = os.path.join(os.environ.get("APPDATA", ""), "Tencent", "xwechat",
                                "radium", "window.config")
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            left, top = int(data.get("left")), int(data.get("top"))
            if -1000 < left < 10000 and -1000 < top < 10000:
                return (left, top)
        except Exception:
            pass
        return (200, 150)

    def _ensure_main_window(self) -> bool:
        """保证主窗口真的可用：可见、没最小化、在聊天页、会话列表读得到。

        不可用时点微信自己的托盘图标让它开窗（用户看不见——成功后立刻挪到屏幕
        外）；如果只是停在通讯录/收藏/发现页，就点回「微信」页。全程不用外部
        ``ShowWindow``（那只会造白板）。
        """
        if not self.main_hwnd:
            return False
        if self._main_ready():
            return True
        was_hidden = not self._safe_visible()
        was_iconic = self._safe_iconic()
        if self.saved_rect is None:
            self._remember_position()
            self.saved_iconic = bool(was_iconic and not was_hidden)
        if not self._tree_ready():
            self._activate_tree()
        if not self._window_shown():
            if not self._show_main():
                return False
            self.restored_window = True
            self.log("微信主窗口原本不可用（%s），已让微信自己重新显示"
                     % ("收在托盘" if was_hidden else "最小化" if was_iconic else "未显示"))
        deadline = time.monotonic() + 3.0
        switched = False
        while time.monotonic() < deadline:
            if self._session_list(deadline=time.monotonic() + 0.8) is not None:
                break
            if not switched:
                if self.log:
                    self.log("读不到会话列表，切回聊天页")
                switched = True
                if self._click_chat_tab():
                    continue
            self._activate_tree(timeout=1.0)
            time.sleep(0.25)
        ready = self._main_ready()
        if ready and self.restored_window:
            self._move_offscreen(self.main_hwnd)
        return ready

    def _main_visible(self) -> bool:
        if self._safe_visible():
            return True
        self.last_open_error = ("main_in_tray",
                                "微信小号主窗口已收进托盘且自动叫出失败，请手动点托盘图标打开小号窗口")
        return False

    def _session_row(self, chat: str):
        """等待左侧会话列表可读，区分列表不可读和名单里真没有目标。"""
        unavailable = ("session_list_unavailable", "无法读取微信会话列表，请打开小号主窗口并切到聊天页")
        if not self._ensure_main_window():
            if not self._main_visible():
                return None
            self.last_open_error = unavailable
            return None
        hwnd = self.main_hwnd
        try:
            uia = WeChatUIA(timeout=8.0, hwnd=hwnd)
        except Exception:
            self.last_open_error = unavailable
            return None

        rows = None
        deadline = time.monotonic() + SESSION_LIST_WAIT
        while time.monotonic() < deadline:
            if not self._main_visible():
                return None
            rows = None
            try:
                win = uia._find_main()
                if (win is not None and win.ClassName in MAIN_CLASSES
                        and int(win.NativeWindowHandle) == hwnd and not win32gui.IsIconic(hwnd)):
                    table = _find_by(win, lambda c: (c.AutomationId or "").endswith("session_list"),
                                     max_depth=30, deadline=deadline)
                    if table is not None:
                        rows = [row for row in table.GetChildren()
                                if (row.Name or "").split("\n", 1)[0].strip() == chat]
            except Exception:
                # 主窗口恢复时，UIA 元素可能在本轮读取中失效。
                pass
            if not self._main_visible():
                return None
            if rows:
                if len(rows) == 1:
                    self.last_open_error = None
                    return rows[0]
                self.last_open_error = ("ambiguous_target", "会话列表有重名，无法确认发送目标")
                return None
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.2, remaining))

        if not self._main_visible():
            return None
        self.last_open_error = (unavailable if rows is None else
                                ("no_row", "会话列表里没有 %r，请先在微信小号主窗口打开该会话" % chat))
        return None

    def _post_double_click(self, hwnd: int, sx: int, sy: int) -> bool:
        """把双击消息直接投给微信窗口：不动鼠标、不抢前台。

        这条是开窗首选：微信对 WM_LBUTTONDBLCLK 的反应和真实双击一样（开独立
        聊天窗），但它只依赖窗口句柄和客户区坐标，不受「鼠标落在别的窗口上 /
        用户正在操作微信 / 前台被别的程序抢走」影响。
        """
        try:
            cx, cy = win32gui.ScreenToClient(hwnd, (sx, sy))
        except Exception:
            return False
        lp = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
        try:
            if not self._main_visible():
                return False
            user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
            time.sleep(0.04)
            user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)
            time.sleep(0.06)
            if not self._main_visible():
                return False
            user32.PostMessageW(hwnd, WM_LBUTTONDBLCLK, MK_LBUTTON, lp)
            time.sleep(0.04)
            user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)
            return True
        except Exception:
            return False

    def _wait_chat_window(self, chat: str, before: set,
                          timeout: float = 4.0) -> int | None:
        """等一个标题正好是 chat 的新窗口出现；必须核对标题才认。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            hwnd = self._find_chat_window(chat)
            if self.last_open_error or (hwnd and hwnd not in before):
                return hwnd
            time.sleep(0.08)
        return None

    def _restore_foreground(self, prev: int):
        """开窗时微信会把自己提到前台，尽量把前台还给用户原来的窗口。"""
        if not prev:
            return
        try:
            if win32gui.GetForegroundWindow() != prev:
                user32.SetForegroundWindow(prev)
        except Exception:
            pass

    def _chat_root(self, uia, hwnd: int, username: str):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid != self.pid:
            return None
        root = uia._find_main()
        if (root is not None and root.ClassName in CHAT_WINDOW_CLASSES
                and int(root.NativeWindowHandle) == hwnd
                and root.AutomationId == "ChatSingleWindow" + username):
            return root
        return None

    def _finish_open(self, chat: str, hwnd: int, username: str | None) -> int | None:
        """核验窗口身份后再移出屏幕。"""
        if username:
            root = self._chat_root(WeChatUIA(timeout=8.0, hwnd=hwnd), hwnd, username)
            if root is None:
                self.last_open_error = ("ambiguous_target", "聊天窗口身份与选定会话不一致")
                return None
        if not self.account_is_bound():
            self.last_open_error = ("account_unavailable", "无法核验选定小号")
            return None
        self._move_offscreen(hwnd)
        self.chat_windows[chat] = hwnd
        return hwnd

    def _mouse_double_click(self, row) -> bool:
        """兜底：把主窗口拉到前台，用真实鼠标双击会话行。

        只有确认主窗口真的拿到了前台、且鼠标下确实是它，才会按下按键；否则
        直接放弃，避免点到别人的窗口上。
        """
        try:
            if not self._main_visible() or win32gui.IsIconic(self.main_hwnd):
                return False
            user32.SetWindowPos(self.main_hwnd, 0, 60, 80, 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(0.2)
            if not self._main_visible():
                return False
            if not _force_foreground(self.main_hwnd):
                self.log("主窗口拿不到前台，跳过鼠标双击")
                return False
            rect = row.BoundingRectangle
            x = (rect.left + rect.right) // 2
            y = (rect.top + rect.bottom) // 2
            if x < -2000 or x > 20000 or y < -2000 or y > 20000:
                self.log("会话行坐标异常（%s,%s），跳过鼠标双击" % (x, y))
                return False
            hit = win32gui.WindowFromPoint((x, y))
            if not hit or win32gui.GetAncestor(hit, GA_ROOT) != self.main_hwnd:
                self.log("鼠标落点不在微信主窗口上，跳过鼠标双击")
                return False
            if not self._main_visible() or win32gui.GetForegroundWindow() != self.main_hwnd:
                return False
            cursor = win32gui.GetCursorPos()
            user32.SetCursorPos(x, y)
            try:
                time.sleep(0.15)
                for _ in range(2):
                    if not self._main_visible() or win32gui.GetForegroundWindow() != self.main_hwnd:
                        return False
                    user32.mouse_event(0x0002, 0, 0, 0, 0)
                    time.sleep(0.03)
                    user32.mouse_event(0x0004, 0, 0, 0, 0)
                    time.sleep(0.05)
            finally:
                user32.SetCursorPos(cursor[0], cursor[1])
            return True
        except Exception as e:
            self.log("鼠标双击异常：%s" % e)
            return False

    def open_chat(self, chat: str, force: bool = False, *,
                  username: str | None = None) -> int | None:
        """确保 chat 有一个已挪到屏幕外的独立窗口，返回 hwnd。"""
        with self._lock:
            self.last_open_error = None
            self.restored_window = False
            self.saved_rect = None
            self.saved_iconic = False
            if not self.account_is_bound() and not self.connect():
                self.last_open_error = ("account_unavailable", "选定小号的主窗口不可用")
                return None

            # 复用已有窗口
            hwnd = self._find_chat_window(chat)
            if hwnd:
                return self._finish_open(chat, hwnd, username)
            if self.last_open_error:
                return None

            if not self._ensure_main_window():
                if not self._main_visible():
                    return None
                self.last_open_error = ("session_list_unavailable",
                                        "无法读取微信会话列表，请打开小号主窗口并切到聊天页")
                return None
            if _is_fullscreen_foreground():
                self.log("前台是全屏应用，暂不开窗：%s" % chat)
                self.last_open_error = ("fullscreen", "前台全屏，暂不开窗")
                return None

            main_hwnd = self.main_hwnd
            prev_fg = win32gui.GetForegroundWindow()
            try:
                if not self.account_is_bound():
                    self.last_open_error = ("account_unavailable", "无法核验选定小号")
                    return None
                row = self._session_row(chat)
                if row is not None and self._main_visible():
                    return self._open_from_session_list(chat, username, row)
                return None
            finally:
                if self.account_is_bound() and win32gui.IsWindowVisible(main_hwnd):
                    if self.restored_window:
                        # 动过主窗口就放回原处再藏起来：用户点托盘/任务栏能正常打开
                        self._park_main_window()
                    self._restore_foreground(prev_fg)

    def _open_from_session_list(self, chat: str, username: str | None, row) -> int | None:
        before = {w[0] for w in self._windows_of(self.pid)}

        # 首选：投递双击消息（不碰鼠标、不抢前台）
        for attempt in range(2):
            if attempt:
                # 上一轮可能只是慢了一点：先确认没开出来再补点一次
                time.sleep(0.5)
                hit = self._find_chat_window(chat)
                if hit:
                    return self._finish_open(chat, hit, username)
                if self.last_open_error:
                    return None
                row = self._session_row(chat)
                if row is None:
                    break
            try:
                rect = row.BoundingRectangle
                sx = (rect.left + rect.right) // 2
                sy = (rect.top + rect.bottom) // 2
            except Exception:
                break
            if (not self.account_is_bound() or not self._main_visible()
                    or not self._post_double_click(self.main_hwnd, sx, sy)):
                break
            new_hwnd = self._wait_chat_window(chat, before, timeout=2.5)
            if new_hwnd:
                return self._finish_open(chat, new_hwnd, username)
            if self.last_open_error:
                return None

        # 兜底：拉前台 + 真实鼠标双击（有严格校验，宁可不点也不误点）
        if not self.last_open_error and self.account_is_bound():
            row = self._session_row(chat)
            if row is not None and self.account_is_bound() and self._mouse_double_click(row):
                new_hwnd = self._wait_chat_window(chat, before, timeout=4.0)
                if new_hwnd:
                    return self._finish_open(chat, new_hwnd, username)
        if self.last_open_error is None:
            self.last_open_error = ("no_window", "没能打开 %r 的独立窗口" % chat)
        return None

    # ---------------------------------------------------------------- 发送
    def send(self, chat: str, text: str, *, username: str,
             preflight: Callable[[bool], dict | None]) -> dict:
        # 同一实例内串行：写入→回车→核验期间独占，防止并发覆盖输入框
        with self._send_lock, self._lock:
            return self._send_locked(chat, text, username, preflight)

    def _fail(self, chat: str, reason: str, error: str, retryable: bool = False,
              submitted: bool = False) -> dict:
        return {"ok": False, "chat": chat, "reason": reason, "error": error,
                "retryable": retryable, "submitted": submitted}

    def _send_locked(self, chat: str, text: str, username: str,
                     preflight: Callable[[bool], dict | None]) -> dict:
        if not chat or not username or not (text or "").strip():
            return self._fail(chat, "bad_args", "会话或内容为空")
        submitted = False
        try:
            hwnd = self.open_chat(chat, username=username)
            if hwnd is None:
                code, msg = self.last_open_error or ("no_window", "无法打开会话窗口")
                return self._fail(chat, code, msg, retryable=code in {
                    "fullscreen", "no_window", "account_unavailable", "session_list_unavailable", "main_in_tray",
                })
            cu = WeChatUIA(timeout=8.0, hwnd=hwnd)
            root = self._chat_root(cu, hwnd, username)
            if root is None:
                return self._fail(chat, "ambiguous_target", "聊天窗口身份无法核验")
            block = preflight(False)
            if block:
                return block
            inp = cu._chat_input(root)
            if inp is None:
                return self._fail(chat, "no_input", "聊天窗口里没有输入框", retryable=True)
            vp = inp.GetPattern(10002)
            if vp.Value:
                return self._fail(chat, "input_busy", "聊天输入框已有草稿", retryable=True)
            vp.SetValue(text)
            expected = text.replace("\r\n", "\n")
            time.sleep(0.15)
            root = self._chat_root(cu, hwnd, username)
            if root is None or not self.account_is_bound():
                return self._fail(chat, "ambiguous_target", "窗口身份发生变化，已停止发送")
            if (vp.Value or "").replace("\r\n", "\n") != expected:
                return self._fail(chat, "write_failed", "输入框内容与待发内容不一致")
            block = preflight(True)
            if block:
                if vp.Value == text and self._chat_root(cu, hwnd, username) is not None:
                    vp.SetValue("")
                return block
            if (self._chat_root(cu, hwnd, username) is None
                    or (vp.Value or "").replace("\r\n", "\n") != expected):
                return self._fail(chat, "write_changed", "发送前窗口或输入内容发生变化")
            t0 = time.time()
            submitted = bool(user32.PostMessageW(hwnd, WM_KEYDOWN, VK_RETURN, 0))
            if not submitted:
                vp.SetValue("")
                return self._fail(chat, "enter_failed", "无法提交发送", retryable=True)
            user32.PostMessageW(hwnd, WM_KEYUP, VK_RETURN, 0)
            return {"ok": True, "chat": chat, "t0": t0, "submitted": True, "verified": False}
        except Exception:
            return self._fail(chat, "send_exception", "微信界面操作失败", submitted=submitted)
