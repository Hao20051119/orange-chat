"""Orange 审核版桌面控制台。"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.parse import urlencode, urlsplit

from runtime import DEFAULT_MODEL, OrangeRuntime
import ui as style
from ui import (EntryField, GlassFrame, GlassText, GlassWell, Palette, PillButton,
                PillCheck, PillSelect)


BASE_WIDTH = 1180           # 界面基准宽度：窗口比它宽时整体放大


def ui_scale(width):
    """按窗口宽度算界面缩放（1180 宽 = 1.0，最大 1.4）。"""
    ratio = (width or BASE_WIDTH) / BASE_WIDTH
    return round(max(1.0, min(1.4, ratio)) * 20) / 20


BG = "#F3EDE3"
CARD = "#FFFFFF"
INK = "#2A2521"
MUTED = "#6F675A"
LINE = "#EFE6D9"
ORANGE = "#9C421E"
ORANGE_BG = "#FAEDE1"
GREEN = "#2F654D"
FONT = "Microsoft YaHei UI"
PAL = Palette(bg=BG, card=CARD, ink=INK, muted=MUTED, line=LINE, orange=ORANGE,
              orange_bg=ORANGE_BG, green=GREEN, font=FONT)

PAGES = {
    "overview": "概览",
    "persona": "固定人设",
    "relations": "关系",
    "chats": "会话与发送",
    "blocks": "黑名单与接管",
    "drafts": "待处理",
    "reminders": "定时提醒",
    "proactive": "主动聊天",
    "knowledge": "知识库",
    "logs": "日志与用量",
    "settings": "设置",
}
STATUS = {
    "active": "启用", "cancelled": "已取消", "done": "已完成", "draft": "待发送",
    "expired": "已过期", "failed": "失败", "hold": "待审核", "missed": "已错过",
    "paused": "已暂停", "pending": "待处理", "queued": "已排队", "running": "处理中",
    "sending": "发送中", "sent": "已发送", "skipped": "已跳过",
    "superseded": "已由新回复替代", "uncertain": "等待核验", "unverified": "未核实",
    "manual": "手动暂停", "takeover": "自动接管", "idle": "空闲超时",
}
RULES = {"仅一次": "single", "每天": "daily", "每周": "weekly"}
SETTINGS = (
    ("agent.group_reply", "接收群聊中的回复请求"),
    ("memory.enabled", "保存长期聊天记忆"),
    ("web.enabled", "允许联网检索"),
    ("send.bubble", "允许拆分多个消息气泡"),
    ("logging.chat_content", "日志记录聊天正文"),
    ("kb.enabled", "启用知识库"),
)

# ------------------------------------------------------------ 主动聊天参数预设
# 一律下拉选，不让人手填：「3」「3次」「半小时」混着写没法校验，也容易填错。
# 这里只放常用档位，后端（proactive.PARAM_LIMITS）仍做范围兜底。
PROACTIVE_FOLLOW = "跟随全局"
PROACTIVE_PRESETS = {
    "per_chat_daily": (0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20),
    "idle_min_minutes": (5, 10, 15, 20, 30, 45, 60, 90, 120, 150, 180, 240,
                         360, 480, 720, 1440),
    "idle_max_minutes": (15, 30, 60, 90, 120, 180, 240, 360, 480, 720, 1080,
                         1440, 2160, 2880),
    "quiet_minutes_after_user": (0, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240),
    "min_gap_after_self": (0, 15, 30, 45, 60, 90, 120, 180, 240, 300, 360),
}
PROACTIVE_TITLES = {
    "per_chat_daily": "每天主动上限",
    "idle_min_minutes": "两次开口最小间隔",
    "idle_max_minutes": "两次开口最大间隔",
    "quiet_minutes_after_user": "对方说完后先静默",
    "min_gap_after_self": "自己说完后至少隔",
}
PROACTIVE_HOURS = ([(PROACTIVE_FOLLOW, None)]
                   + [("%d 点" % h, h) for h in range(24)]
                   + [("24 点（到午夜）", 24)])


def param_text(key, value):
    """下拉里怎么念这个值。"""
    if value is None:
        return PROACTIVE_FOLLOW
    value = int(value)
    if key == "per_chat_daily":
        return "不限" if value == 0 else "%d 次" % value
    if value == 0:
        return "不静默" if key == "quiet_minutes_after_user" else "不等待"
    if value % 60 == 0:
        return "%d 小时" % (value // 60)
    return "%d 分钟" % value


def proactive_options(key, default=None, current=None):
    """下拉选项：第一项跟随全局（带全局当前值），其余是预设；旧的自定义值也补上。"""
    follow = PROACTIVE_FOLLOW
    if default is not None:
        follow = "%s（%s）" % (PROACTIVE_FOLLOW, param_text(key, default))
    items = [(follow, None)] + [(param_text(key, value), value)
                                for value in PROACTIVE_PRESETS.get(key, ())]
    if current is not None and current not in {value for _, value in items}:
        items.insert(1, ("%s（自定义）" % param_text(key, current), current))
    return items


def _hour_of(label):
    for text, value in PROACTIVE_HOURS:
        if text == label:
            return True, value
    return False, None


def proactive_patch(picked, own=None, defaults=None):
    """把下拉选中的文字收敛成提交给后端的 patch；返回 (patch, 错误说明)。"""
    if not isinstance(picked, dict):
        return {}, "表单数据无效"
    own = own or {}
    defaults = defaults or {}
    patch = {}
    for key in PROACTIVE_PRESETS:
        if key not in picked:
            continue
        options = dict((text, value) for text, value in
                       proactive_options(key, defaults.get(key), own.get(key)))
        if picked[key] not in options:
            return {}, "「%s」选的档位无效，请重新选择" % PROACTIVE_TITLES.get(key, key)
        patch[key] = options[picked[key]]
    if "active_hours" in picked:
        hours = picked.get("active_hours")
        if not isinstance(hours, (list, tuple)) or len(hours) != 2:
            return {}, "主动时段要选开始和结束两个档位"
        ok_lo, lo = _hour_of(hours[0])
        ok_hi, hi = _hour_of(hours[1])
        if not (ok_lo and ok_hi):
            return {}, "主动时段选的档位无效，请重新选择"
        if (lo is None) != (hi is None):
            return {}, "主动时段要么都选「跟随全局」，要么起止都选具体小时"
        patch["active_hours"] = None if lo is None else [int(lo), int(hi)]
        if lo is not None and not (0 <= lo <= 24 and 0 <= hi <= 24):
            return {}, "主动时段必须落在 0~24 点之间"
    # 合并后最小间隔不能大于最大间隔：跟随全局的那半边用全局值算
    merged = {}
    for key in ("idle_min_minutes", "idle_max_minutes"):
        if key in patch:
            merged[key] = patch[key] if patch[key] is not None else defaults.get(key)
        else:
            merged[key] = own.get(key, defaults.get(key))
    if (isinstance(merged.get("idle_min_minutes"), (int, float))
            and isinstance(merged.get("idle_max_minutes"), (int, float))
            and merged["idle_min_minutes"] > merged["idle_max_minutes"]):
        return {}, "最小间隔不能大于最大间隔（现在是 %s > %s）" % (
            param_text("idle_min_minutes", merged["idle_min_minutes"]),
            param_text("idle_max_minutes", merged["idle_max_minutes"]))
    return patch, ""


def clock(value, *, date=False):
    try:
        return time.strftime("%Y-%m-%d %H:%M" if date else "%m-%d %H:%M",
                             time.localtime(float(value))) if value else "—"
    except (ValueError, TypeError, OverflowError, OSError):
        return "—"


def translated(value):
    return STATUS.get(value, value or "—")


def clipped(text, limit=120):
    text = " ".join(str(text or "").split())
    return text[:limit - 1] + "…" if len(text) > limit else text


# 会话 ID：wxid_xxx / 数字群号@chatroom / 微信号当 alias（如 wxid_example_alias）
CHAT_PATTERN = re.compile(
    r"(?:wxid_[A-Za-z0-9_-]+|[0-9A-Za-z]+@chatroom|[A-Za-z][A-Za-z0-9_-]{5,19})")

# 微信内置系统/服务号：不是真人私聊，与 agent/gate.py 的默认清单保持一致
SYSTEM_CHATS = frozenset((
    "filehelper", "weixin", "medianote", "fmessage", "floatbottle", "newsapp",
    "notifymessage", "qqmail", "tmessage", "qmessage", "weixinliveshare",
))


def is_private_chat(username):
    """本地兜底判定：后端返回 private 字段时以它为准。"""
    name = (username or "").strip()
    if not name or name.endswith("@chatroom") or name in SYSTEM_CHATS:
        return False
    return not name.startswith("gh_")


def chat_id(value):
    value = value.strip()
    if not CHAT_PATTERN.fullmatch(value):
        raise ValueError("请从会话列表选择收件人，或填写完整的 wxid_ / 微信号 / 群聊 ID。")
    return value


def fit_columns(spec, content, avail, *, head_measure, pad, floor):
    """算出每列宽度：先要够（表头/内容 + 内边距），再按设计比例分富余。

    ``spec`` 是 ``[(key, title, 设计宽度, 可拉伸)]``，``content`` 是每列内容
    实测的最大像素宽。返回 ``(mins, widths)``。

    纯函数，不碰 Tk：GUI 只是把测量函数传进来，测试可以直接调它。
    """
    base, mins, caps, weights = [], [], [], []
    for index, (key, title, width, stretch) in enumerate(spec):
        head_w = head_measure(title) + pad
        body_w = (content[index] if index < len(content) else 0) + pad
        min_w = int(max(floor, head_w))
        want = int(max(min_w, body_w))
        mins.append(min_w)
        base.append(want)
        # 封顶决定「富余能把它撑多胖」：可拉伸列最多翻倍；本来就宽的非拉伸
        # 列给 60px；时间/状态/次数这类短列只给一点呼吸空间，别虚胖。
        if stretch:
            caps.append(int(max(want * 2, want + int(pad * 4))))
        elif want > int(pad * 6.5):
            caps.append(int(want + int(pad * 2)))
        else:
            caps.append(int(want + int(pad * 0.8)))
        weights.append(max(int(width), int(pad * 2)))

    total = sum(base)
    if total < avail:
        extra = avail - total
        widths = list(base)
        active = set(range(len(spec)))
        while extra > 0 and active:
            wsum = sum(weights[i] for i in active)
            if wsum <= 0:
                break
            used = 0
            for i in list(active):
                share = extra * weights[i] // wsum
                room = caps[i] - widths[i]
                add = max(0, min(share, room))
                widths[i] += add
                used += add
                if widths[i] >= caps[i]:
                    active.discard(i)
            if used <= 0:
                break
            extra -= used
        if extra > 0:
            # 都顶到封顶还有富余（大屏 + 内容短）：给内容最长的那列
            flexible = [i for i, col in enumerate(spec) if col[3]] or list(range(len(spec)))
            main = max(flexible,
                       key=lambda i: (content[i] if i < len(content) else 0, weights[i]))
            widths[main] += extra
        return mins, widths
    if total == avail:
        return mins, base
    # 放不下：从长列里按比例扣，扣到表头宽为止，还不够就靠横向滚动条
    over = total - avail
    shrinkable = [i for i in range(len(spec)) if base[i] > mins[i]]
    while over > 0 and shrinkable:
        room_total = sum(base[i] - mins[i] for i in shrinkable)
        if room_total <= 0:
            break
        take = min(over, room_total)
        cut_total = 0
        for i in shrinkable:
            room = base[i] - mins[i]
            cut = min(room, int(take * room / room_total))
            if cut:
                base[i] -= cut
                cut_total += cut
        if cut_total <= 0:
            # 取整把每一刀都磨成 0 时，至少从最宽的那列切 1px，避免死循环
            biggest = max(shrinkable, key=lambda i: base[i] - mins[i])
            base[biggest] -= 1
            cut_total = 1
        over -= cut_total
        shrinkable = [i for i in shrinkable if base[i] > mins[i]]
    return mins, base


class OrangeApp:
    def __init__(self, root, runtime=None):
        self.root = root
        self.runtime = runtime if runtime is not None else OrangeRuntime()
        self.root.title("Orange")
        screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
        width = max(950, min(1180, screen_width - 52))
        height = max(680, min(800, screen_height - 76))
        x = max((screen_width - width) // 2, 0)
        y = max((screen_height - height - 38) // 2, 0)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(950, 680)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.scale = ui_scale(width)
        style.SCALE = self.scale
        self.root.option_add("*Font", (FONT, self._fs(10)))
        self._set_icon()
        self._theme()
        self.events = queue.SimpleQueue()
        self.pending = set()
        self.view_version = 0
        self.page = ""
        self.session = {}
        self.known_nicks = {}
        # 账号检测到哪一步了：改窗口大小要重建页面，靠它决定重建后要不要重新扫。
        self._accounts_state = "idle"
        self.tables = {}
        # 表格列宽自适应：定义表、内容宽缓存、横向滚动条、防抖任务
        self.table_specs = {}
        self.table_content = {}
        self.table_scroll = {}
        self._fit_jobs = {}
        self._tip = None
        # 量文字宽度用同款字体，避免列宽和数据实际显示差一截
        self._table_head_font = tkfont.Font(root=root, family=FONT,
                                            size=self._fs(9), weight="bold")
        self._table_body_font = tkfont.Font(root=root, family=FONT, size=self._fs(10))
        self.nav = {}
        self._resize_job = None
        self.feedback = tk.StringVar()
        self.connection = tk.StringVar(value="尚未连接")
        # 顶部标题条与底部提示条都做成浮起的圆角玻璃条
        self.topbar = tk.Frame(root, bg=BG)
        self.topbar.pack(side="top", fill="x", padx=self._s(14), pady=(self._s(12), 0))
        self.banner = GlassFrame(self.topbar, PAL, padding=0, radius=20, inset=5, band=8)
        self.banner.pack(fill="x")
        brand = tk.Frame(self.banner.inner, bg=CARD)
        brand.pack(side="left", padx=(self._s(14), self._s(8)), pady=self._s(7))
        if getattr(self, "_mark", None) is not None:
            tk.Label(brand, image=self._mark, bg=CARD, bd=0).pack(
                side="left", padx=(0, self._s(8)))
        self._label(brand, "Orange", size=19, color=ORANGE, bg=CARD, bold=True
                    ).pack(side="left")
        self.connection_label = self._label(self.banner.inner, variable=self.connection,
                                            color=MUTED, bg=CARD)
        self.connection_label.pack(side="right", padx=self._s(20))
        self.footwrap = tk.Frame(root, bg=BG)
        self.footwrap.pack(side="bottom", fill="x", padx=self._s(14),
                           pady=(0, self._s(12)))
        self.footer = GlassFrame(self.footwrap, PAL, padding=0, radius=18, inset=5, band=7)
        self.footer.pack(fill="x")
        self._label(self.footer.inner, variable=self.feedback, color=MUTED, bg=CARD,
                    anchor="w").pack(fill="x", padx=self._s(18), pady=self._s(9))
        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.root.bind("<F5>", lambda _: self.refresh() if self.page else self.reload_accounts())
        self.root.bind("<Configure>", self._on_root_resize)
        self.show_setup()
        self.root.after(80, self._drain)
        self.root.after(5000, self._poll)

    def _on_root_resize(self, event):
        """窗口尺寸变了就重算缩放，字号跟着窗口走（全屏时不会显得特别小）。"""
        if event.widget is not self.root:
            return
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(220, self._apply_scale)

    def _apply_scale(self):
        self._resize_job = None
        scale = ui_scale(self.root.winfo_width())
        if abs(scale - self.scale) < 0.03:
            return
        # 连接操作和初次读取 API 设置完成后再重建，保留当前页的回调。
        if self.pending & {"start", "edit", "stop", "model-config", "api-keys"}:
            self._resize_job = self.root.after(400, self._apply_scale)
            return
        # 字号写在控件创建时，只有整页重建才会跟着窗口变大；但用户已经选好的
        # 账号/API 来源、已经拿到的账号列表不能因为缩放一次就归零。
        state = self._view_state()
        # 旧任务的回调会因为 view_version 变了被判无效，留着只会挡住重建后的新请求
        # （reload_accounts 见到旧的 discover 未完成就直接 return，账号就再也不回来了）
        self.pending.clear()
        self.scale = scale
        style.SCALE = scale
        self.root.option_add("*Font", (FONT, self._fs(10)))
        self._theme()
        if self.page:
            self.show_workbench(self.page)
            self.refresh(silent=True)
        else:
            self.show_setup(state)

    def _view_state(self):
        """重建界面前先拍照：重建后原样贴回去，缩放不该让用户重新选一遍。"""
        state = {"page": self.page, "feedback": self.feedback.get(),
                 "connection": self.connection.get(),
                 "connection_color": str(self.connection_label.cget("fg"))}
        if self.page:
            return state
        accounts, selected = [], []
        table = self.tables.get("accounts")
        if table is not None:
            tree, index = table
            accounts = list(index.values())
            selected = [iid for iid in tree.selection() if iid in index]
        state.update({
            "accounts": accounts,
            "accounts_state": self._accounts_state,
            "selected": selected,
            "model_choice": self.model_choice.get(),
            "model_fields": {key: var.get() for key, var in self.model_fields.items()},
            "key_choice": self.key_choice.get(),
            "configured": dict(self.configured),
            "saved_keys": self.saved_keys,
            "small_confirmed": bool(self.small_confirmed.get()
                                    and selected == [self._setup_account]),
            "model_info": self.model_info.get(),
        })
        return state

    def _set_icon(self):
        """用项目自带的橙色图标做窗口/任务栏图标和标题行标记；缺失时静默跳过。"""
        path = Path(__file__).with_name("orange-icon.png")
        try:
            raw = tk.PhotoImage(file=str(path))
        except (tk.TclError, OSError):
            return
        self._raw_icon = raw
        longest = max(raw.width(), raw.height())
        factor = max(1, round(longest / 256))
        image = raw.subsample(factor, factor) if factor > 1 else raw
        self._icon = image
        mark_factor = max(1, round(longest / 32))
        self._mark = raw.subsample(mark_factor, mark_factor) if mark_factor > 1 else raw
        try:
            self.root.iconphoto(True, image)
        except tk.TclError:
            pass

    def _theme(self):
        """ttk 只管表格、下拉框这些原生控件的细节；按钮/卡片/输入框由 ui 模块绘制。"""
        style = ttk.Style(self.root)
        style.theme_use("clam")
        fs, px = self._fs, self._s
        style.configure("TFrame", background=CARD)
        style.configure("TButton", font=(FONT, fs(10)), padding=(px(12), px(6)),
                        background=CARD, foreground=INK, bordercolor=LINE,
                        lightcolor=CARD, darkcolor=LINE, focuscolor=ORANGE)
        style.map("TButton", background=[("disabled", BG), ("active", ORANGE_BG)])
        # 圆角外壳里的输入框：去掉 clam 的方块边框，只留文字
        style.configure("Well.TEntry", font=(FONT, fs(10)), fieldbackground=CARD,
                        background=CARD, bordercolor=CARD, lightcolor=CARD,
                        darkcolor=CARD, insertcolor=INK, padding=(1, px(3)),
                        relief="flat")
        style.map("Well.TEntry", fieldbackground=[("disabled", CARD)],
                  foreground=[("disabled", MUTED)])
        style.configure("Well.TCombobox", font=(FONT, fs(10)), fieldbackground=CARD,
                        background=CARD, bordercolor=CARD, lightcolor=CARD,
                        darkcolor=CARD, arrowcolor=MUTED, arrowsize=fs(13),
                        padding=(2, px(4)), foreground=INK,
                        selectbackground=CARD, selectforeground=INK,
                        focuscolor=CARD, focusthickness=0)
        style.map("Well.TCombobox", fieldbackground=[("readonly", CARD)],
                  bordercolor=[("focus", CARD)], focuscolor=[("focus", CARD)],
                  selectbackground=[("readonly", CARD)],
                  selectforeground=[("readonly", INK)])
        style.configure("TCombobox", font=(FONT, fs(10)), padding=(px(9), px(5)),
                        fieldbackground=CARD, background=CARD, bordercolor=LINE,
                        lightcolor=LINE, darkcolor=LINE, arrowcolor=MUTED,
                        arrowsize=fs(13), foreground=INK, selectbackground=CARD,
                        selectforeground=INK)
        style.map("TCombobox", fieldbackground=[("readonly", CARD)],
                  bordercolor=[("focus", ORANGE)],
                  selectbackground=[("readonly", CARD)], selectforeground=[("readonly", INK)])
        style.configure("TCheckbutton", background=CARD, foreground=INK,
                        font=(FONT, fs(10)), focuscolor=CARD, indicatorcolor=CARD)
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=INK,
                        rowheight=px(31), borderwidth=0, font=(FONT, fs(10)), relief="flat",
                        bordercolor=CARD, lightcolor=CARD, darkcolor=CARD)
        style.configure("Treeview.Heading", background="#FBF6EC", foreground=MUTED,
                        font=(FONT, fs(9), "bold"), padding=(px(9), px(7)), relief="flat",
                        bordercolor="#FBF6EC", lightcolor="#FBF6EC", darkcolor="#F3EADD")
        style.map("Treeview.Heading", background=[("active", "#F7F0E3")])
        style.map("Treeview", background=[("selected", ORANGE_BG)],
                  foreground=[("selected", INK)])
        style.configure("Vertical.TScrollbar", background="#E6D9C7", troughcolor=CARD,
                        bordercolor=CARD, arrowcolor="#B4A594", arrowsize=px(9),
                        width=px(10), lightcolor="#E6D9C7", darkcolor="#D9CAB6",
                        gripcount=0)
        style.map("Vertical.TScrollbar",
                  background=[("active", "#D9CAB6"), ("pressed", "#CDBCA6")])

    def _fs(self, size):
        """字号按窗口缩放，全屏时不会显得特别小。"""
        return max(8, int(round(size * self.scale)))

    def _s(self, value):
        """像素尺寸按窗口缩放。"""
        return int(round(value * self.scale))

    def _label(self, parent, text="", *, variable=None, color=INK, bg=BG, size=10,
               bold=False, **kwargs):
        return tk.Label(parent, text=text, textvariable=variable, fg=color, bg=bg,
                        font=(FONT, self._fs(size), "bold" if bold else "normal"), **kwargs)

    def _button(self, parent, text, action, *, primary=False, nav=False, width=None,
                align="center"):
        return PillButton(parent, text, action, pal=PAL,
                          variant="primary" if primary else ("nav" if nav else "glass"),
                          size=self._fs(10), height=self._s(36), padx=self._s(18),
                          width=width, align=align)

    def _check(self, parent, text, variable, **kwargs):
        """圆角勾选框（tk.Checkbutton 的直角小方框和圆角界面不搭）。"""
        return PillCheck(parent, PAL, text=text, variable=variable,
                         size=self._fs(10), height=self._s(30), box=20, **kwargs)

    @staticmethod
    def _enable(widget, enabled):
        """ttk 用 state()，经典控件用 state 选项。"""
        if isinstance(widget, tk.Checkbutton):
            widget.configure(state="normal" if enabled else "disabled")
        else:
            widget.state(["!disabled"] if enabled else ["disabled"])

    def _panel(self, parent, padding=14):
        """圆角渐变毛玻璃卡片，返回 (外壳, 内容区)。"""
        shell = GlassFrame(parent, PAL, padding=padding, radius=26, inset=5, band=12)
        return shell, shell.inner

    def _row(self, parent, *, bg=CARD):
        bar = tk.Frame(parent, bg=bg)
        bar.pack(fill="x")
        return bar

    def _text(self, parent, *, height=5, read_only=False):
        return GlassText(parent, PAL, height=height, read_only=read_only,
                         size=self._fs(10))

    @staticmethod
    def _put(box, value):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", value)
        box.configure(state="disabled")

    def _task(self, key, run, callback, *, message=None, fail=None, persistent=False):
        if key in self.pending:
            return False
        self.pending.add(key)
        generation = None if persistent else self.view_version
        if message:
            self.feedback.set(message)

        def work():
            try:
                value = run()
            except Exception as exc:
                self.events.put((key, generation, fail, str(exc), True))
            else:
                self.events.put((key, generation, callback, value, False))

        threading.Thread(target=work, daemon=True).start()
        return True

    def _drain(self):
        while not self.events.empty():
            key, version, callback, value, failed = self.events.get()
            self.pending.discard(key)
            if version is not None and version != self.view_version:
                continue
            if callback:
                callback(value)
            elif failed:
                self.feedback.set(value or "操作失败，请重试。")
        self.root.after(80, self._drain)

    def _request(self, path, payload=None, *, timeout=12):
        result = self.runtime.request(path, payload, timeout=timeout)
        if not isinstance(result, dict):
            raise ValueError("本地服务返回的数据格式有误。")
        if result.get("ok") is False or result.get("error"):
            raise ValueError(str(result.get("error") or result.get("reason") or "操作未完成。"))
        return result

    def _table(self, parent, name, columns, *, height=8):
        """表格外面套一层圆角外壳：不然整块白色区域是直角，和卡片圆角打架。

        列宽不写死：``_fit_table`` 按表头和实际内容量文字宽度，短列收到内容宽、
        长文本列吃剩余；窗口窄到放不下时给横向滚动条（悬停还能看完整内容）。
        """
        shell = GlassWell(parent, PAL, radius=14)
        shell.pack(fill="both", expand=True)
        frame = ttk.Frame(shell)
        frame.pack(fill="both", expand=True, padx=self._s(11), pady=self._s(9))
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=tuple(col[0] for col in columns),
                            show="headings", selectmode="browse", height=height)
        for key, title, width, stretch in columns:
            tree.heading(key, text=title, anchor="w")
            tree.column(key, width=self._s(width), minwidth=self._s(48),
                        stretch=False, anchor="w")
        scroll = ttk.Scrollbar(frame, command=tree.yview)
        hscroll = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=hscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")
        hscroll.grid_remove()
        tree.tag_configure("stripe", background="#FDF8F1")
        self.tables[name] = (tree, {})
        self.table_specs[name] = [(key, title, int(width), bool(stretch))
                                  for key, title, width, stretch in columns]
        self.table_scroll[name] = hscroll
        self.table_content.pop(name, None)
        tree.bind("<Configure>", lambda _e, n=name: self._schedule_fit(n), add="+")
        tree.bind("<Motion>", lambda e, n=name: self._hover_tip(n, e), add="+")
        tree.bind("<Leave>", lambda _e: self._hide_tip(), add="+")
        self.root.after_idle(lambda: self._fit_table(name))
        return tree

    # ------------------------------------------------------------ 列宽自适应
    def _schedule_fit(self, name, delay=40):
        """防抖：连续拖动窗口只算一次，避免每像素重排整张表。"""
        job = self._fit_jobs.pop(name, None)
        if job is not None:
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
        self._fit_jobs[name] = self.root.after(delay, lambda: self._fit_table(name))

    def _fit_table(self, name):
        """按「表头 + 内容 + 设计比例」重排列宽。

        旧逻辑：先按内容要够，剩下的**全塞给最长的那一列**——大屏上那一列
        能空出四百像素，其他列却贴着内容边，看着又挤又空。

        新逻辑见 ``fit_columns``：每列先要够，富余按设计比例分给所有列，
        短列有小封顶；实在放不下才压缩 / 出横向滚动条。
        """
        self._fit_jobs.pop(name, None)
        entry = self.tables.get(name)
        spec = self.table_specs.get(name)
        if not entry or not spec:
            return
        tree = entry[0]
        try:
            avail = tree.winfo_width() - self._s(3)
        except tk.TclError:
            return
        if avail <= self._s(40):
            self._schedule_fit(name, 80)
            return

        pad = self._s(30)                 # 单元格左右内边距 + 边框 + 呼吸空间
        head = self._table_head_font
        body = self._table_body_font
        content = self.table_content.get(name) or [0] * len(spec)
        mins, widths = fit_columns(spec, content, avail,
                                   head_measure=head.measure,
                                   pad=pad, floor=self._s(48))
        for index, (key, _title, _width, _stretch) in enumerate(spec):
            tree.column(key, width=max(mins[index], int(widths[index])),
                        minwidth=mins[index], stretch=False, anchor="w")
        hscroll = self.table_scroll.get(name)
        if hscroll is not None:
            if sum(max(mins[i], int(widths[i])) for i in range(len(spec))) > avail:
                hscroll.grid()
            else:
                hscroll.grid_remove()

    # ------------------------------------------------------------ 完整内容提示
    def _hide_tip(self):
        tip = getattr(self, "_tip", None)
        if tip is not None:
            try:
                tip.destroy()
            except tk.TclError:
                pass
            self._tip = None

    def _show_tip(self, text, x, y):
        tip = getattr(self, "_tip", None)
        if tip is not None:
            try:
                label = tip._label
                if label.cget("text") == text:
                    tip.geometry("+%d+%d" % (x + 14, y + 18))
                    return
            except (AttributeError, tk.TclError):
                self._hide_tip()
            else:
                self._hide_tip()
        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        label = tk.Label(tip, text=text, justify="left", bg="#FFFDF8", fg=INK,
                         font=(FONT, self._fs(10)), padx=self._s(8), pady=self._s(5),
                         wraplength=self._s(420), bd=0, highlightthickness=1,
                         highlightbackground="#E6D9C7")
        label.pack()
        tip._label = label
        tip.geometry("+%d+%d" % (x + 14, y + 18))
        self._tip = tip

    def _hover_tip(self, name, event):
        """单元格内容宽于列宽时，悬停显示完整文字。"""
        entry = self.tables.get(name)
        spec = self.table_specs.get(name)
        if not entry or not spec:
            return
        tree = entry[0]
        row = tree.identify_row(event.y)
        col = tree.identify_column(event.x)
        if not row or row == "__empty__" or not col:
            self._hide_tip()
            return
        try:
            index = int(col[1:]) - 1
        except ValueError:
            self._hide_tip()
            return
        if index < 0 or index >= len(spec):
            self._hide_tip()
            return
        values = tree.item(row, "values")
        if index >= len(values):
            self._hide_tip()
            return
        text = str(values[index])
        try:
            width = int(tree.column(spec[index][0], "width"))
        except (tk.TclError, ValueError):
            self._hide_tip()
            return
        if not text or self._table_body_font.measure(text) + self._s(18) <= width:
            self._hide_tip()
            return
        self._show_tip(text, event.x_root, event.y_root)

    def _fill(self, name, rows, key, values, *, empty="暂无记录"):
        tree, index = self.tables[name]
        selected = tree.selection()
        tree.delete(*tree.get_children())
        index.clear()
        widths = [0] * len(self.table_specs.get(name) or [])
        for row in rows:
            row_id = str(key(row))
            if row_id in index:
                continue
            index[row_id] = row
            vals = values(row)
            for i, value in enumerate(vals[:len(widths)]):
                w = self._table_body_font.measure(str(value))
                if w > widths[i]:
                    widths[i] = w
            tree.insert("", "end", iid=row_id, values=vals,
                        tags=("stripe",) if len(index) % 2 == 0 else ())
        if not rows:
            tree.insert("", "end", iid="__empty__", values=(empty,))
        self.table_content[name] = widths
        self._schedule_fit(name)
        if selected and selected[0] in index:
            tree.selection_set(selected)

    def _selected(self, name, *, required=True):
        tree, index = self.tables[name]
        selections = tree.selection()
        row = index.get(selections[0]) if selections else None
        if required and row is None:
            self.feedback.set("请先在列表中选择一项。")
        return row

    def _clear(self, widget):
        for child in widget.winfo_children():
            child.destroy()
        self.tables.clear()
        self.table_specs.clear()
        self.table_content.clear()
        self.table_scroll.clear()
        for job in self._fit_jobs.values():
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
        self._fit_jobs.clear()
        self._hide_tip()
        self.view_version += 1

    def _entry(self, parent, var, *, show="", width=20):
        """圆角单行输入框；返回的外壳可以直接 pack，也能当 ttk.Entry 用（.state/.bind）。"""
        return EntryField(parent, PAL, var, show=show, width=width)

    def _combo(self, parent, var, values, *, readonly=False, width=20):
        """圆角外壳里的下拉框；返回 (外壳, Combobox)，可编辑时仍能直接输入。"""
        shell = GlassWell(parent, PAL, radius=12)
        box = ttk.Combobox(shell, textvariable=var, values=values, width=width,
                           state="readonly" if readonly else "normal",
                           style="Well.TCombobox")
        box.pack(fill="both", expand=True, padx=10, pady=4)
        return shell, box

    def _input(self, parent, title, var, *, show="", width=20):
        field = tk.Frame(parent, bg=CARD)
        self._label(field, title, color=MUTED, bg=CARD).pack(anchor="w", pady=(0, 4))
        entry = self._entry(field, var, show=show, width=width)
        entry.pack(fill="x")
        return field, entry

    def show_setup(self, restore=None):
        """连接页。restore 是「改窗口大小要整页重建」时带回来的快照，用来保住已选状态。"""
        keep = dict(restore or {})
        fresh = not keep
        self.page = ""
        self._clear(self.body)
        if fresh:
            self.session = {}
            self.known_nicks = self._known_nicks()
            self._connection("尚未连接", MUTED)
            self.feedback.set("")
        else:
            self.feedback.set(keep.get("feedback") or "")
            self._connection(keep.get("connection") or "尚未连接",
                             keep.get("connection_color") or MUTED)
        wrap = tk.Frame(self.body, bg=BG)
        wrap.pack(fill="both", expand=True, padx=self._s(18), pady=self._s(8))
        self._label(wrap, "连接微信小号", size=24, bold=True).pack(anchor="w")
        self._label(wrap, "先选本机已登录的小号，再选 Orange 使用的模型 API。",
                    color=MUTED).pack(anchor="w", pady=(4, 10))
        grid = tk.Frame(wrap, bg=BG)
        grid.pack(fill="x")
        for col in range(2):
            # uniform 让两列等宽且固定：没有它，列宽由「卡片内容需要多宽」决定，
            # 选好 API 来源后说明文字变长，右侧卡片就会变宽、把左侧账号卡挤窄
            grid.columnconfigure(col, weight=1, uniform="cards")
        left, account_panel = self._panel(grid, 12)
        right, model_panel = self._panel(grid, 12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        right.grid(row=0, column=1, sticky="nsew", padx=(9, 0))
        self._label(account_panel, "微信账号", bg=CARD, size=13, bold=True).pack(anchor="w")
        tree = self._table(account_panel, "accounts", (("name", "微信昵称", 120, True),
                          ("id", "账号", 130, True)), height=3)
        self._setup_account = next(iter(keep.get("selected") or ()), None)
        self.small_confirmed = tk.BooleanVar(value=bool(keep.get("small_confirmed")))
        tree.bind("<<TreeviewSelect>>", self._setup_account_selected)
        small_check = self._check(account_panel, "我确认选中的账号是微信小号",
                                  self.small_confirmed)
        small_check.pack(anchor="w", pady=(8, 7))
        discover = self._button(account_panel, "重新检测账号", self.reload_accounts)
        discover.pack(anchor="w")

        self._label(model_panel, "模型 API", bg=CARD, size=13, bold=True).pack(anchor="w")
        self.model_choice = tk.StringVar(value=keep.get("model_choice") or "")
        self.model_options = {"沿用项目已有 API": "configured", "自定义 OpenAI 兼容 API": "custom"}
        choices = PillSelect(model_panel, PAL, options=tuple(self.model_options),
                             variable=self.model_choice, command=self._show_model_mode,
                             placeholder="选择 API 来源")
        choices.pack(fill="x", pady=(8, 8))
        self.model_info = tk.StringVar(value=keep.get("model_info") or "请选择 API 来源。")
        # 固定两行 + wraplength 跟着缩放：「沿用已有」的说明比「自定义」长一行，
        # 行数一变右侧卡片就长高，底部「连接并启动」会被顶下去
        self.model_info_label = self._label(model_panel, variable=self.model_info, color=MUTED,
                                            bg=CARD, anchor="w", justify="left", height=2,
                                            wraplength=self._s(365))
        self.model_info_label.pack(fill="x", pady=(1, 5))
        # custom 区始终占位（内部在「输入框」和「提示」之间切换）：不这样，
        # 选中「自定义」时卡片会长高、缩回「沿用已有」又变矮，整页跟着抖
        self.custom_model = tk.Frame(model_panel, bg=CARD)
        self.custom_model.pack(fill="x")
        self.custom_form = tk.Frame(self.custom_model, bg=CARD)
        self.custom_hint = tk.Frame(self.custom_model, bg=CARD)
        self._label(self.custom_hint, "选择「沿用项目已有 API」时无需填写此区；如需更换接口，请选择「自定义」并填写下方字段。",
                    color=MUTED, bg=CARD, anchor="w", justify="left",
                    wraplength=self._s(365)).pack(anchor="w", pady=(self._s(6), 0))
        fields = keep.get("model_fields") or {}
        self.model_fields = {
            "base_url": tk.StringVar(
                value=fields.get("base_url") or "https://api.deepseek.com"),
            "model": tk.StringVar(value=fields.get("model") or DEFAULT_MODEL),
            "api_key": tk.StringVar(value=fields.get("api_key") or ""),
        }
        # ---- 已保存的 Key 记录（api_keys.local.json，只在本机、可选用可删除）----
        self.saved_keys = keep.get("saved_keys") or {"items": [], "default": None}
        self.key_labels = [self._key_label(row)
                           for row in (self.saved_keys.get("items") or [])]
        self.key_choice = tk.StringVar(value=keep.get("key_choice") or "")
        # 地址 + 模型 ID 一行、Key + 已保存的 Key 一行：竖排四行会把卡片撑高，
        # 切换 API 来源时整张卡片和底部按钮都会被顶下去。
        model_inputs = []
        pair = tk.Frame(self.custom_form, bg=CARD)
        pair.pack(fill="x")
        pair.columnconfigure(0, weight=1)
        pair.columnconfigure(1, weight=1)
        for key, title, column, padx in (("base_url", "API 地址", 0, (0, 6)),
                                         ("model", "模型 ID", 1, (0, 0))):
            field, entry = self._input(pair, title, self.model_fields[key], width=12)
            field.grid(row=0, column=column, sticky="ew", padx=padx)
            model_inputs.append(entry)
        field, entry = self._input(pair, "API Key（必填）", self.model_fields["api_key"],
                                   show="*", width=12)
        field.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(2, 0))
        model_inputs.append(entry)
        saved = tk.Frame(pair, bg=CARD)
        saved.grid(row=1, column=1, sticky="ew", pady=(2, 0))
        self._label(saved, "已保存的 Key", color=MUTED, bg=CARD).pack(anchor="w", pady=(0, 4))
        keyline = tk.Frame(saved, bg=CARD)
        keyline.pack(fill="x")
        self.key_select = PillSelect(keyline, PAL, options=tuple(self.key_labels),
                                     variable=self.key_choice,
                                     command=self._key_selected,
                                     placeholder=("选择已保存的 Key" if self.key_labels
                                                  else "还没有记录：填一次 Key 会自动记住"),
                                     size=self._fs(9), height=self._s(30))
        self.key_select.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.key_delete = PillButton(keyline, "删除", self._delete_key, pal=PAL,
                                     size=self._fs(9), height=self._s(30),
                                     padx=self._s(10), bg=CARD)
        self.key_delete.pack(side="left")
        self.configured = dict(keep.get("configured") or {})
        if fresh:
            self._task("model-config", self.runtime.configured_model, self._set_configured)
            self._load_keys()
        bar = self._row(wrap, bg=BG)
        bar.pack_configure(pady=(6, 0))
        start = self._button(bar, "连接并启动", self.start, primary=True)
        start.pack(side="right")
        # 模型操作按钮放在底部这条栏里：右面板纵向空间有限，放进去会顶出窗口
        self.model_actions = tk.Frame(bar, bg=BG)
        self.model_actions.pack(side="right", padx=(0, 10))
        self.model_test = self._button(self.model_actions, "测试模型 API", self.test_model)
        self.model_test.pack(side="left")
        self.key_save = self._button(self.model_actions, "保存这条 Key", self.save_key_now)
        self.key_save.pack(side="left", padx=(7, 0))
        self.setup_controls = [small_check, discover, choices, self.key_select, self.key_delete,
                               self.key_save, *model_inputs, self.model_test, start]
        self._label(bar, "启动后自动回复、主动聊天和提醒均默认关闭。",
                    color=MUTED).pack(side="left")
        # 账号：重建时列表刚在几秒前拿到，直接用，别再走一次「正在检测」把表格清空
        if not fresh and keep.get("accounts_state") == "done":
            self._accounts_state = "done"
            accounts = keep.get("accounts") or []
            self._paint_accounts(accounts, keep.get("selected") or ())
            # 重建时可能正好有昵称还没读到：补一次，别让第一列一直显示「—」
            self._fill_missing_nicks(accounts)
        else:
            self.reload_accounts()
        # custom 区按内容实测一次高度再锁死：之后切 API 来源、重建页面都不会再
        # 改变右卡片高度（卡片高度差 6px 就会把两侧长度关系翻过来，整行跟着跳）
        self.custom_form.pack(fill="x")
        self.body.update_idletasks()
        self.custom_model.configure(height=max(self._s(40),
                                               self.custom_model.winfo_reqheight()))
        self.custom_model.pack_propagate(False)
        self._show_model_mode()

    def _paint_accounts(self, accounts, selected=()):
        """把账号行画进表格（不动 feedback，重建页面时直接复用）。"""
        tree = self.tables.get("accounts")
        if tree is None:
            return
        self._fill("accounts", accounts, lambda row: row["account"],
                   lambda row: (self._nick_of(row),
                                row.get("wxid") or row["account"]),
                   empty="没有找到已登录且可核验的微信")
        tree, index = self.tables["accounts"]
        keep_selection = [iid for iid in selected if iid in index]
        if keep_selection:
            tree.selection_set(keep_selection)

    def _setup_account_selected(self, _event):
        row = self._selected("accounts", required=False)
        account = row["account"] if row else None
        if account != self._setup_account:
            self._setup_account = account
            self.small_confirmed.set(False)

    def _fill_missing_nicks(self, accounts):
        """还没印象的账号在后台单独补昵称：读不到就算了，别拖慢连接页。"""
        missing = [row for row in accounts
                   if not (row.get("nick") or self.known_nicks.get(row["account"]))]
        if missing:
            self._task("nicks", lambda: self.runtime.account_nicks(missing),
                       self._nicks_loaded, fail=lambda _: None)

    def reload_accounts(self):
        if "start" in self.pending or "discover" in self.pending:
            return
        self._accounts_state = "pending"
        self._fill("accounts", [], None, None, empty="正在检测已登录账号")
        self.small_confirmed.set(False)
        self._task("discover", self.runtime.discover_accounts, self._accounts_loaded,
                   message="正在检查本机登录的微信账号…", fail=self._accounts_failed)

    def _accounts_failed(self, error):
        self._accounts_state = "done"
        self._fill("accounts", [], None, None, empty="账号检测失败")
        self.feedback.set(error)

    def _known_nicks(self):
        """上次记下的昵称；读不到就当没有，不影响连接页。"""
        try:
            return dict(self.runtime.known_nicks() or {})
        except Exception:                     # noqa: BLE001 昵称只是显示用
            return {}

    def _nick_of(self, row):
        """昵称列：本次读到的 > 上次记下的 > 「—」（账号列已经显示 wxid）。"""
        nick = (row.get("nick") or "").strip()
        return nick or self.known_nicks.get(row.get("account") or "") or "—"

    def _accounts_loaded(self, accounts):
        # 昵称读不到时留「—」，不要拿账号顶上去：两列都显示同一串 wxid，挤在一起很难看
        self._accounts_state = "done"
        self._paint_accounts(accounts or [])
        self.feedback.set("请选择小号后确认。" if accounts else
                          "请先在微信中登录小号，然后点击重新检测账号。")
        # discover_accounts 按设计不开库（秒回），昵称在后台单独补：没缓存过密钥的
        # 账号 read_nicks 会直接跳过，所以这里只补「还不知道昵称」的那几个。
        self._fill_missing_nicks(accounts or [])

    def _nicks_loaded(self, nicks):
        """后台读到昵称后回填第一列，并记在本机供下次直接用。"""
        if not isinstance(nicks, dict) or not nicks:
            return
        tree, index = self.tables.get("accounts", (None, {}))
        for account, nick in nicks.items():
            nick = str(nick or "").strip()
            if not account or not nick:
                continue
            self.known_nicks[account] = nick
            try:
                self.runtime.remember_nick(account, nick)
            except Exception:                 # noqa: BLE001 缓存写失败不影响显示
                pass
            if tree is not None and account in index:
                row = index[account]
                tree.item(account, values=(nick, row.get("wxid") or row["account"]))

    def _set_configured(self, data):
        self.configured = data
        self._show_model_mode()

    # ------------------------------------------------------------ Key 记录
    def _load_keys(self):
        self._task("api-keys", self.runtime.api_keys, self._keys_loaded, fail=lambda _: None)

    @staticmethod
    def _key_label(row):
        """下拉里只显示「★ 打码后的 Key」，把宽度让给输入框。"""
        return ("%s%s" % ("★ " if row.get("default") else "", row.get("masked") or "（空）")).strip()

    def _apply_key_row(self, row, secret):
        if row.get("base_url"):
            self.model_fields["base_url"].set(row["base_url"])
        if row.get("model"):
            self.model_fields["model"].set(row["model"])
        if secret:
            self.model_fields["api_key"].set(secret)

    def _keys_loaded(self, data):
        self.saved_keys = data if isinstance(data, dict) else {"items": [], "default": None}
        items = self.saved_keys.get("items") or []
        self.key_labels = [self._key_label(row) for row in items]
        self.key_select.set_options(self.key_labels)
        self.key_select.set_placeholder(
            "还没有记录：填一次 Key 会自动记住" if not items else "选择已保存的 Key")
        if not items:
            self.key_choice.set("")
            return
        # items 已按「默认优先」排序，第一条就是要带出来的那条
        self.key_choice.set(self.key_labels[0])
        default = self.saved_keys.get("default") or {}
        if default.get("key"):
            self._apply_key_row(items[0], default.get("key") or "")

    def _chosen_key_row(self):
        label = self.key_choice.get()
        for row, text in zip(self.saved_keys.get("items") or [], self.key_labels):
            if text == label:
                return row
        return None

    def _key_selected(self):
        row = self._chosen_key_row()
        if not row:
            return
        try:
            secret = self.runtime.api_key_secret(row["id"])
        except Exception as error:  # noqa: BLE001 读不到就退回手填
            self.feedback.set(str(error))
            return
        self._apply_key_row(row, secret)
        self.feedback.set("已载入这条 Key。")

    def _remember_key(self):
        key = self.model_fields["api_key"].get().strip()
        if not key:
            return False
        base_url = self.model_fields["base_url"].get().strip()
        model_id = self.model_fields["model"].get().strip()
        label = (base_url.split("//")[-1].split("/")[0] or "自定义 API")
        try:
            data = self.runtime.save_api_key(label, base_url, model_id, key)
        except Exception:  # noqa: BLE001 存不下来不影响本次运行
            return False
        self._keys_loaded(data)
        return True

    def save_key_now(self):
        if self._remember_key():
            self.feedback.set("已记住这条 Key（只存在本机）。")
        else:
            self.feedback.set("请先填写 API Key 再保存。")

    def _delete_key(self):
        row = self._chosen_key_row()
        if not row:
            self.feedback.set("请先在下方列表中选择要删除的记录。")
            return
        try:
            data = self.runtime.delete_api_key(row["id"])
        except Exception as error:  # noqa: BLE001
            self.feedback.set(str(error))
            return
        self.model_fields["api_key"].set("")
        self.key_choice.set("")
        self._keys_loaded(data)
        self.feedback.set("已删除这条 Key 记录（重新填写即可恢复）。")

    def _configured_key_ready(self):
        if self.configured.get("key_available"):
            return True
        try:
            host = urlsplit(self.configured.get("base_url") or "").hostname
        except ValueError:
            return False
        return host in ("localhost", "127.0.0.1", "::1")

    def _show_model_mode(self):
        mode = self.model_options.get(self.model_choice.get())
        if mode == "custom":
            self.custom_hint.pack_forget()
            self.custom_form.pack(fill="x")
            self.model_info.set("Key 只存在本机，填过会自动记住。")
            return
        # 这里只切换 custom 区的内容，外形高度由 show_setup 锁死：卡片高度不变
        self.custom_form.pack_forget()
        self.custom_hint.pack(fill="x")
        if mode == "configured":
            if self._configured_key_ready():
                self.model_info.set(f"{self.configured.get('model', '')}  ·  "
                                    f"{self.configured.get('base_url', '')}")
            else:
                self.model_info.set("项目已有 API 缺少密钥，请改用「自定义」并填写 API Key。")
        else:
            self.model_info.set("请选择 API 来源。")

    def _chosen_model(self):
        mode = self.model_options.get(self.model_choice.get())
        if not mode:
            raise ValueError("请先选择模型 API。")
        if mode == "configured" and not self._configured_key_ready():
            raise ValueError("项目已有 API 缺少密钥。")
        data = {"mode": mode, **{key: value.get().strip()
                                for key, value in self.model_fields.items()}}
        if mode == "custom" and not (data["base_url"] and data["model"]):
            raise ValueError("请填写 API 地址和模型 ID。")
        return data

    def test_model(self):
        try:
            model = self._chosen_model()
        except ValueError as error:
            self.feedback.set(str(error))
            return
        self._task("test-model", lambda: self.runtime.test_model(model),
                   self._model_tested, message="正在测试模型 API…")

    def _model_tested(self, res):
        if not res.get("ok"):
            self.feedback.set(res.get("error") or "API 测试未通过。")
            return
        remembered = False
        if self.model_options.get(self.model_choice.get()) == "custom":
            remembered = self._remember_key()
        self.feedback.set("模型 API 测试通过。" + ("Key 已记住。" if remembered else ""))

    def start(self):
        if "edit" in self.pending:
            self.feedback.set("上一个操作正在结束，请稍候。")
            return
        choice = self._selected("accounts")
        if choice is None:
            return
        if not self.small_confirmed.get() or self._setup_account != choice["account"]:
            self.feedback.set("请先确认所选账号是小号。")
            return
        try:
            model = self._chosen_model()
        except ValueError as error:
            self.feedback.set(str(error))
            return
        if model.get("mode") == "custom":
            self._remember_key()  # 填过的 Key 自动记住，下次不用重打
        name = choice.get("nick") or choice.get("label") or choice["account"]
        if self._task("start", lambda: self.runtime.start(choice["account"], model, True),
                      self._started, message=f"正在核验 {name} 并启动本地服务…",
                      fail=self._start_failed, persistent=True):
            for button in self.setup_controls:
                self._enable(button, False)

    def _start_failed(self, error):
        for button in self.setup_controls:
            self._enable(button, True)
        self.feedback.set(error or "启动失败，请检查当前微信登录状态。")

    def _started(self, status):
        self.model_fields["api_key"].set("")
        self.session = status
        # 连上时服务已经把昵称读出来了：记下来，下次连接页直接显示，不必再开库
        account, nick = status.get("account") or "", status.get("nick") or ""
        if account and nick:
            self.known_nicks[account] = nick
            try:
                self.runtime.remember_nick(account, nick)
            except Exception:                 # noqa: BLE001 缓存写失败不影响使用
                pass
        self.show_workbench()

    def _poll(self):
        if self.page and "stop" not in self.pending:
            if not self.runtime.running:
                self._show_runtime_exit()
            elif self.page == "overview":
                self.refresh(silent=True)
            else:
                self._task("health", lambda: self._request("/status"), self._session_status,
                           fail=lambda _: self._connection("服务暂不可用", ORANGE))
                self.refresh(silent=True)
        self.root.after(5000, self._poll)

    def _confirmed_stop(self):
        if not self.runtime.running:
            self.show_setup()
            return
        if messagebox.askyesno("停止 Orange", "停止当前小号的 Orange 服务？", parent=self.root):
            self._task("stop", self.runtime.stop, lambda _: self.show_setup(),
                       message="正在停止本次运行…", persistent=True)

    def close(self):
        if "start" in self.pending or "stop" in self.pending:
            self.feedback.set("请等待当前启动或停止操作完成。")
            return
        if not self.runtime.running:
            self.root.destroy()
        elif messagebox.askyesno("退出 Orange", "退出会同时停止本次运行的服务，继续吗？",
                                 parent=self.root):
            self._task("stop", self.runtime.stop, lambda _: self.root.destroy(),
                       message="正在停止服务…", persistent=True)

    def show_workbench(self, page=None):
        """重建工作台（侧栏 + 当前页）。page=None 表示首次进入，回到概览。"""
        self._clear(self.body)
        self._session_status(self.session)
        margin = self._s(14)
        sidebar = tk.Frame(self.body, width=self._s(178), bg=BG, padx=self._s(13),
                           pady=self._s(6))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self.nav = {}
        for key, title in PAGES.items():
            button = PillButton(sidebar, title, lambda target=key: self.show_page(target),
                                pal=PAL, variant="nav", size=self._fs(10),
                                height=self._s(38), align="left", padx=self._s(16))
            button.pack(fill="x", pady=(0, self._s(2)))
            self.nav[key] = button
        self.account_button = self._button(sidebar, "停止并更换账号", self._confirmed_stop)
        self.account_button.pack(side="bottom", fill="x", pady=(0, self._s(12)))
        self.workarea = tk.Frame(self.body, bg=BG, padx=margin, pady=margin)
        self.workarea.pack(side="left", fill="both", expand=True, padx=(0, margin))
        self.show_page(page or "overview")
        if page is None:
            self.feedback.set("已连接。可以在概览页按需开启自动回复。")

    def _session_status(self, status):
        if not self.runtime.running:
            return
        self.session.update(status)
        name = self.session.get("nick") or self.session.get("account") or "微信小号"
        connected = self.session.get("account_bound", bool(self.session.get("bound_pid")))
        self._connection(f"{name} · {'小号已绑定' if connected else '微信绑定不可用'}",
                         GREEN if connected else ORANGE)

    def _connection(self, label, color):
        self.connection.set(label)
        self.connection_label.configure(fg=color)

    def _show_runtime_exit(self, *, report=False):
        reason = getattr(self.runtime, "exit_reason", "")
        fallback = "后台已停止。请返回连接页重新启动。"
        if self.connection.get() == "本次服务已停止" and not report:
            if reason and self.feedback.get() == fallback:
                self.feedback.set(reason)
            return
        self._connection("本次服务已停止", ORANGE)
        self.account_button.configure(text="返回连接页")
        self.feedback.set(reason or fallback)

    def show_page(self, name):
        self.page = name
        self._clear(self.workarea)
        for key, button in self.nav.items():
            button.set_selected(key == name)
        title = self._row(self.workarea, bg=BG)
        title.pack_configure(pady=(0, self._s(12)))
        self._label(title, PAGES[name], size=21, bold=True).pack(side="left")
        self._button(title, "刷新", self.refresh).pack(side="right", pady=(self._s(2), 0))
        getattr(self, f"_page_{name}")()
        self._load_page(silent=True)

    def refresh(self, *, silent=False):
        if not self.page:
            return
        if not self.runtime.running:
            if "stop" not in self.pending:
                self._show_runtime_exit(report=not silent)
            return
        if silent and self.page not in ("overview", "blocks", "drafts", "reminders", "proactive"):
            return
        self._load_page(silent)

    def _load_page(self, silent=False):
        routes = {
            "overview": ("/status", "/stats", "/config", "/inbox"),
            "persona": ("/persona",),
            "relations": ("/persona", "/chats"),
            "chats": ("/chats", "/blacklist", "/stats"),
            "blocks": ("/blacklist", "/stats", "/chats"),
            "drafts": ("/drafts?limit=100",),
            "reminders": ("/reminders", "/chats"),
            "proactive": ("/proactive/contacts",),
            "knowledge": ("/kb",),
            "logs": ("/log?lines=200", "/usage"),
            "settings": ("/config",),
        }
        name = self.page

        def loaded(result):
            getattr(self, f"_load_{name}")(*result)
            if not silent:
                self.feedback.set("已刷新。")

        self._task(f"page:{self.view_version}", lambda: tuple(self._request(path)
                   for path in routes[name]), loaded, message=None if silent else "正在刷新…")

    def _change(self, path, payload, notice, *, timeout=12):
        if not self.runtime.running or "stop" in self.pending:
            self.feedback.set("本次服务正在停止或已停止。")
            return

        def done(_):
            self.feedback.set(notice)
            if self.page and "stop" not in self.pending:
                self._load_page(True)

        if not self._task("edit", lambda: self._request(path, payload, timeout=timeout), done,
                          message="正在处理…", persistent=True):
            self.feedback.set("当前有操作正在处理，请稍候。")

    def _switch(self, path, enabled, label, *, disclosure=""):
        if enabled and disclosure and not messagebox.askyesno(f"开启{label}", disclosure,
                                                               parent=self.root):
            return
        self._change("/config", {path: enabled}, f"{label}已{'开启' if enabled else '关闭'}。")

    def _page_overview(self):
        top, card = self._panel(self.workarea)
        top.pack(fill="x", pady=(0, 12))
        bar = self._row(card)
        self.summary = tk.StringVar()
        self._label(bar, variable=self.summary, bg=CARD, bold=True, size=12,
                    anchor="w").pack(side="left", padx=(0, 8))
        self.auto_on = None
        self.auto_button = self._button(bar, "加载自动回复状态", self._toggle_auto, primary=True)
        self.auto_button.state(["disabled"])
        self.auto_button.pack(side="right")
        self.overview_error = tk.StringVar()
        self._label(card, variable=self.overview_error, bg=CARD, color=ORANGE, anchor="w",
                    wraplength=680, justify="left").pack(fill="x", pady=(7, 0))
        metrics = self._row(self.workarea, bg=BG)
        metrics.pack_configure(pady=(0, 12))
        self.metrics = {}
        for index, (key, title) in enumerate((("inbound_pending", "未处理消息"),
                                             ("inbound_retry", "重试中"),
                                             ("outbound_draft", "排队待发"),
                                             ("outbound_hold", "待审核"),
                                             ("outbound_sent", "已发送"),
                                             ("outbound_failed", "失败"),
                                             ("unverified_total", "待核验"),
                                             ("paused_n", "暂停中"))):
            col, line = index % 4, index // 4
            metrics.columnconfigure(col, weight=1, uniform="metrics")
            panel, cell = self._panel(metrics, 10)
            panel.grid(row=line, column=col, sticky="ew",
                       padx=(0, 8) if col < 3 else 0, pady=(0, 8) if line == 0 else 0)
            self.metrics[key] = tk.StringVar(value="—")
            self._label(cell, title, bg=CARD, color=MUTED, size=9).pack(anchor="w")
            self._label(cell, variable=self.metrics[key], bg=CARD,
                        size=17, bold=True).pack(anchor="w")
        shell, inbox = self._panel(self.workarea)
        shell.pack(fill="both", expand=True)
        self._label(inbox, "最近收到的消息", bg=CARD, bold=True,
                    size=12).pack(anchor="w", pady=(0, 6))
        tree = self._table(inbox, "inbox", (("time", "时间", 105, False),
                           ("chat", "会话", 150, False), ("body", "消息", 290, True),
                           ("status", "状态", 100, False)))
        tree.bind("<Double-Button-1>", lambda _: self._inbox_details())

    def _toggle_auto(self):
        if self.auto_on is not None:
            self._switch("agent.auto_reply", not self.auto_on, "自动回复",
                         disclosure="Orange 会把符合条件的聊天内容和相关上下文发给当前模型 API，"
                                    "并从已绑定的小号发送回复。确认开启？")

    def _load_overview(self, status, stats, config, inbox):
        self._session_status(status)
        engine = stats.get("engine") or {}
        model = self.session.get("model") or (config.get("llm") or {}).get("model") or "—"
        is_bound = status.get("account_bound", bool(status.get("bound_pid")))
        self.summary.set(f"{status.get('account', '—')}  ·  {model}")
        self.overview_error.set(status.get("db_error") or status.get("last_error")
                                or ("" if is_bound and engine.get("pid") else
                                    "微信窗口暂不可用，请在微信中确认当前登录状态。"))
        self.auto_on = bool((config.get("agent") or {}).get("auto_reply"))
        self.auto_button.configure(text="关闭自动回复" if self.auto_on else "开启自动回复")
        self.auto_button.state(["!disabled"])
        counts = stats.get("queue") or {}
        paused = counts.get("paused") or []
        for key, value in self.metrics.items():
            if key == "unverified_total":
                value.set(str(counts.get("outbound_uncertain", 0)
                              + counts.get("outbound_unverified", 0)))
            elif key == "paused_n":
                value.set(str(len(paused)))
            else:
                value.set(str(counts.get(key, 0)))
        self._fill("inbox", inbox.get("messages") or [],
                   lambda r: (r.get("chat"), r.get("ts"), r.get("content")),
                   lambda r: (clock(r.get("ts")), r.get("display") or r.get("chat"),
                              clipped(r.get("content")), translated(r.get("status"))))

    def _inbox_details(self):
        row = self._selected("inbox", required=False)
        if row:
            messagebox.showinfo(row.get("chat") or "会话消息", row.get("content") or "暂无正文",
                                parent=self.root)

    def _page_chats(self):
        columns = tk.Frame(self.workarea, bg=BG)
        columns.pack(fill="both", expand=True)
        columns.columnconfigure(0, weight=2, uniform="chat")
        columns.columnconfigure(1, weight=3, uniform="chat")
        columns.rowconfigure(0, weight=1)
        left, listing = self._panel(columns, 11)
        right, composer = self._panel(columns, 11)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right.grid(row=0, column=1, sticky="nsew")
        self.chat_rows = []
        self.chat_filter = tk.StringVar()
        search, _ = self._input(listing, "按昵称或账号查找", self.chat_filter)
        search.pack(fill="x", pady=(0, 9))
        tree = self._table(listing, "chats", (("display", "会话", 160, True),
                           ("state", "状态", 83, False)))
        tree.bind("<<TreeviewSelect>>", self._select_chat)
        self.chat_filter.trace_add("write", lambda *_: self._filter_chats())
        self._label(composer, "收件人", bg=CARD, bold=True).pack(anchor="w")
        self.target_label = tk.StringVar(value="选择左侧会话，或输入微信 ID")
        self._label(composer, variable=self.target_label, bg=CARD, color=MUTED,
                    anchor="w").pack(fill="x", pady=(3, 3))
        target_row = self._row(composer)
        self.target = tk.StringVar()
        self._entry(target_row, self.target).pack(side="left", fill="x", expand=True)
        self._button(target_row, "读取消息", self._load_history).pack(side="right", padx=(6, 0))
        self.history = self._text(composer, height=5, read_only=True)
        self.history.pack(fill="both", expand=True, pady=(9, 7))
        actions = self._row(composer)
        for label, action in (("暂停自动发送", self._pause_chat), ("恢复", self._resume_chat)):
            self._button(actions, label, action).pack(side="left", padx=(0, 4))
        self.blacklist_button = self._button(actions, "加入黑名单", self._blacklist_chat)
        self.blacklist_button.pack(side="left")
        self.target.trace_add("write", self._target_changed)
        # 发送按钮先占底部，窗口压到最小高度时也不会被挤出去
        bar = self._row(composer)
        bar.pack_configure(side="bottom", pady=(7, 0))
        self._button(bar, "发送消息", self._manual_send, primary=True).pack(side="right")
        self._label(composer, "消息正文", bg=CARD, bold=True).pack(anchor="w", pady=(10, 3))
        self.composer = self._text(composer, height=3)
        self.composer.pack(fill="x", pady=(0, 2))
        self.composer.bind("<Control-Return>", self._send_shortcut)

    def _load_chats(self, chats, blacklist, stats):
        by_id = {r["username"]: dict(r) for r in chats.get("chats", []) if r.get("username")}
        paused = set((stats.get("queue") or {}).get("paused") or [])
        blocked = set(blacklist.get("blacklist") or [])
        for name in sorted(paused | blocked):
            by_id.setdefault(name, {"username": name, "display": name})
        for name, row in by_id.items():
            row["blocked"] = name in blocked
            row["paused"] = name in paused
        self.chat_rows = list(by_id.values())
        self._filter_chats()
        self._update_blacklist_button()

    def _filter_chats(self):
        word = self.chat_filter.get().strip().casefold()
        rows = [row for row in self.chat_rows if not word or any(
            word in (row.get(key) or "").casefold() for key in ("username", "display"))]
        self._fill("chats", rows, lambda r: r["username"],
                   lambda r: (r.get("display") or r["username"],
                              "已拉黑" if r["blocked"] else "已暂停" if r["paused"] else
                              f"{r['unread']} 条未读" if r.get("unread") else "—"))

    def _select_chat(self, _event=None):
        row = self._selected("chats", required=False)
        if row is None or row["username"] == self.target.get():
            return
        self.target.set(row["username"])
        self._load_history()

    def _target_changed(self, *_):
        target = self.target.get().strip()
        row = next((item for item in self.chat_rows if item["username"] == target), {})
        self.target_label.set(row.get("display") or "选择左侧会话，或输入微信 ID")
        self._put(self.history, "点击读取消息以查看此收件人的会话。" if target else "")
        self._update_blacklist_button()

    def _update_blacklist_button(self):
        self.blacklist_button.configure(text="移出黑名单" if self._target_blocked() else "加入黑名单")

    def _target_blocked(self):
        target = self.target.get().strip()
        return any(row["username"] == target and row["blocked"] for row in self.chat_rows)

    def _send_shortcut(self, _):
        self._manual_send()
        return "break"

    def _target(self):
        try:
            return chat_id(self.target.get())
        except ValueError as error:
            self.feedback.set(str(error))
            return ""

    def _load_history(self):
        target = self._target()
        if not target:
            return
        self._put(self.history, "正在加载消息…")
        query = urlencode({"chat": target, "limit": 20})

        def loaded(result):
            if self.target.get().strip() != target:
                return
            rows = result.get("messages") or []
            lines = [f"{row.get('time', '')}  {'小号' if row.get('from_me') else '对方'}\n"
                     f"{row.get('content') or '（无文本内容）'}" for row in rows]
            self._put(self.history, "\n\n".join(lines) or "此会话没有可显示的消息。")
            self.history.see("end")

        def failed(error):
            if self.target.get().strip() == target:
                self._put(self.history, "消息读取失败")
                self.feedback.set(error)

        self._task(f"history:{target}", lambda: self._request(f"/messages?{query}"),
                   loaded, fail=failed)

    def _pause_chat(self):
        target = self._target()
        if target:
            minutes = simpledialog.askinteger("暂停自动发送", "暂停多少分钟？", initialvalue=30,
                                             minvalue=1, maxvalue=1440, parent=self.root)
            if minutes:
                self._change("/pause", {"chat": target, "minutes": minutes}, "会话已暂停。")

    def _resume_chat(self):
        target = self._target()
        if target:
            self._change("/resume", {"chat": target}, "会话已恢复。")

    def _blacklist_chat(self):
        target = self._target()
        if target:
            action = "remove" if self._target_blocked() else "add"
            self._change("/blacklist", {"chat": target, "action": action},
                         "已加入黑名单。" if action == "add" else "已移出黑名单。")

    def _manual_send(self):
        if not self.runtime.running or "stop" in self.pending:
            self.feedback.set("本次服务正在停止或已停止。")
            return
        target = self._target()
        text = self.composer.get("1.0", "end-1c").strip()
        if not target or not text:
            if target:
                self.feedback.set("请输入要发送的消息。")
            return
        if "edit" in self.pending:
            self.feedback.set("当前有操作正在处理，请稍候。")
            return
        display = next((row.get("display") for row in self.chat_rows
                        if row["username"] == target), None)
        recipient = f"{display}（{target}）" if display and display != target else target
        if not messagebox.askyesno("确认发送",
                                  f"从当前绑定的小号发送给 {recipient}？\n\n{clipped(text, 240)}",
                                  parent=self.root):
            return
        version = self.view_version
        payload = {"account": self.session.get("account"), "chat": target, "text": text}

        def posted(result):
            if result.get("ok") or result.get("submitted") is not False:
                verified = result.get("ok") and result.get("verified")
                self.feedback.set("已发送，微信记录已核验。" if verified else
                                  "发送结果待核验，请在微信中确认后再决定是否补发。")
                if self.view_version == version:
                    if self.composer.get("1.0", "end-1c").strip() == text:
                        self.composer.delete("1.0", "end")
                    self._load_history()
            else:
                self.feedback.set(str(result.get("error") or "这条消息未能提交。"))

        self._task("edit", lambda: self.runtime.request("/send", payload, timeout=100), posted,
                   message="正在提交消息并检查微信记录…",
                   fail=lambda e: self.feedback.set(f"发送结果未知，请先检查微信。{e}"),
                   persistent=True)

    def _page_blocks(self):
        columns = tk.Frame(self.workarea, bg=BG)
        columns.pack(fill="both", expand=True)
        columns.columnconfigure(0, weight=1, uniform="blocks")
        columns.columnconfigure(1, weight=1, uniform="blocks")
        columns.rowconfigure(0, weight=1)
        left, blocked = self._panel(columns, 12)
        right, held = self._panel(columns, 12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right.grid(row=0, column=1, sticky="nsew")

        self._label(blocked, "黑名单", bg=CARD, bold=True, size=12).pack(anchor="w", pady=(0, 3))
        self._label(blocked, "名单内的联系人和群组不会自动回复，其余对话默认回复。",
                    bg=CARD, color=MUTED, size=9).pack(anchor="w", pady=(0, 8))
        self._table(blocked, "blocklist", (("display", "会话", 150, True),
                    ("chat", "微信号 / 群 ID", 180, True)))
        bar = self._row(blocked)
        bar.pack_configure(pady=(9, 0))
        self.block_new = tk.StringVar()
        entry = self._entry(bar, self.block_new)
        entry.pack(side="left", fill="x", expand=True)
        self._button(bar, "拉黑", self._blocks_add).pack(side="left", padx=(6, 0))
        entry.bind("<Return>", lambda _: self._blocks_add())
        bar = self._row(blocked)
        bar.pack_configure(pady=(7, 0))
        self._button(bar, "移出黑名单", self._blocks_remove).pack(side="left")

        self._label(held, "暂停中的会话（人工接管）", bg=CARD, bold=True,
                    size=12).pack(anchor="w", pady=(0, 3))
        self._label(held, "暂停期间只收不发；到点自动恢复，也可手动恢复。",
                    bg=CARD, color=MUTED, size=9).pack(anchor="w", pady=(0, 8))
        self._table(held, "held", (("display", "会话", 140, True),
                    ("until", "恢复时间", 112, False), ("reason", "原因", 80, False)))
        bar = self._row(held)
        bar.pack_configure(pady=(9, 0))
        self._button(bar, "恢复选中会话", self._blocks_resume).pack(side="left")
        bar = self._row(held)
        bar.pack_configure(pady=(7, 0))
        self.held_new = tk.StringVar()
        self.held_minutes = tk.StringVar(value="30")
        entry = self._entry(bar, self.held_new)
        entry.pack(side="left", fill="x", expand=True)
        self._entry(bar, self.held_minutes, width=5).pack(side="left", padx=(6, 0))
        self._button(bar, "暂停该会话", self._blocks_pause).pack(side="left", padx=(6, 0))
        entry.bind("<Return>", lambda _: self._blocks_pause())

    def _load_blocks(self, blacklist, stats, chats):
        names = {row["username"]: row.get("display") or row["username"]
                 for row in chats.get("chats", []) if row.get("username")}
        blocked = blacklist.get("blacklist") or []
        self._fill("blocklist", blocked, lambda chat: chat,
                   lambda chat: (names.get(chat, chat), chat),
                   empty="黑名单为空：所有联系人和群默认都会回复")
        queue = stats.get("queue") or {}
        detail = queue.get("paused_detail")
        if not detail:
            detail = [{"chat": chat} for chat in (queue.get("paused") or [])]
        self._fill("held", detail, lambda row: row["chat"],
                   lambda row: (names.get(row["chat"], row["chat"]),
                                clock(row.get("until_ts")),
                                translated(row.get("reason"))),
                   empty="当前没有暂停中的会话")

    def _blocks_add(self):
        value = self.block_new.get().strip()
        if not value:
            self.feedback.set("请输入要拉黑的联系人 ID（wxid_ 或微信号）或群 ID。")
            return
        self._change("/blacklist", {"chat": value, "action": "add"}, "已加入黑名单。")
        self.block_new.set("")

    def _blocks_remove(self):
        chat = self._selected("blocklist")
        if chat:
            self._change("/blacklist", {"chat": chat, "action": "remove"}, "已移出黑名单。")

    def _blocks_resume(self):
        row = self._selected("held")
        if row:
            self._change("/resume", {"chat": row["chat"]}, "会话已恢复。")

    def _blocks_pause(self):
        value = self.held_new.get().strip()
        if not value:
            self.feedback.set("请输入要暂停的联系人 ID（wxid_ 或微信号）或群 ID。")
            return
        try:
            minutes = int(self.held_minutes.get().strip())
        except ValueError:
            self.feedback.set("暂停时长请填分钟数。")
            return
        if not 1 <= minutes <= 1440:
            self.feedback.set("暂停时长须为 1–1440 分钟。")
            return
        self._change("/pause", {"chat": value, "minutes": minutes}, "会话已暂停。")
        self.held_new.set("")

    def _page_drafts(self):
        shell, card = self._panel(self.workarea)
        shell.pack(fill="both", expand=True)
        self._label(card, "审核待发消息；可直接改正文再批准，也可处理失败或未核实的发送记录。",
                    color=MUTED, bg=CARD).pack(anchor="w", pady=(0, 8))
        bar = self._row(card)
        bar.pack_configure(side="bottom")
        self._button(bar, "复制正文", self._copy_draft).pack(side="left")
        self._button(bar, "取消草稿", lambda: self._draft_action("reject")).pack(side="right")
        self._button(bar, "确认已发出", lambda: self._draft_action("mark_sent")
                     ).pack(side="right", padx=5)
        self._button(bar, "批准发送", lambda: self._draft_action("approve"),
                     primary=True).pack(side="right")
        self._button(bar, "保存修改", lambda: self._draft_action("update")
                     ).pack(side="right", padx=5)
        self.draft_id = None
        self.draft_original = ""
        self.draft_info = tk.StringVar()
        self._label(card, variable=self.draft_info, color=MUTED, bg=CARD, anchor="w",
                    justify="left", wraplength=780).pack(side="bottom", fill="x", pady=(9, 2))
        self.draft_text = self._text(card, height=5, read_only=False)
        self.draft_text.pack(side="bottom", fill="x", pady=(0, 8))
        tree = self._table(card, "drafts", (("time", "生成时间", 105, False),
                           ("chat", "会话", 155, False), ("status", "状态", 112, False),
                           ("text", "草稿内容", 285, True)))
        tree.bind("<<TreeviewSelect>>", self._draft_details)
        self._draft_details()

    def _load_drafts(self, data):
        self._fill("drafts", data.get("drafts") or [], lambda r: r["id"],
                   lambda r: (clock(r.get("created_ts")), r.get("display") or r.get("chat"),
                              translated(r.get("status")), clipped(r.get("text"))))
        self._draft_details()

    def _draft_details(self, _event=None):
        row = self._selected("drafts", required=False)
        if not row:
            self.draft_id = None
            self.draft_original = ""
            self.draft_info.set("选择一项查看正文与处理原因。")
            self._put_edit(self.draft_text, "")
            return
        text = row.get("text") or ""
        info = row.get("error") or row.get("gate") or translated(row.get("status"))
        self.draft_info.set(f"{row.get('display') or row.get('chat', '')}  ·  "
                            f"{translated(row.get('status'))}  ·  {info}")
        current = self.draft_text.get("1.0", "end-1c")
        if self.draft_id == row["id"] and current.strip() != (self.draft_original or "").strip():
            return                                   # 用户正在改，刷新不覆盖
        if self.draft_id == row["id"] and current == text:
            return                                   # 内容没变，保留光标位置
        self.draft_id = row["id"]
        self.draft_original = text
        self._put_edit(self.draft_text, text)

    @staticmethod
    def _put_edit(box, value):
        box.delete("1.0", "end")
        box.insert("1.0", value)

    def _copy_draft(self):
        row = self._selected("drafts")
        if row:
            text = self.draft_text.get("1.0", "end-1c").strip() or (row.get("text") or "")
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.feedback.set("已复制草稿正文。")

    def _draft_action(self, action):
        row = self._selected("drafts")
        if not row:
            return
        status = row.get("status")
        text = self.draft_text.get("1.0", "end-1c").strip()
        changed = text != (row.get("text") or "").strip()
        if action in ("approve", "update"):
            if changed and row.get("kind") in ("reminder_clarify", "relay_clarify"):
                self.feedback.set("提醒核对问句必须与排程一致，正文不可修改。")
                return
            if not text:
                self.feedback.set("草稿正文不能为空。")
                return
            if len(text) > 4000:
                self.feedback.set("草稿正文最多 4000 字。")
                return
            if changed and row.get("segments_sent"):
                self.feedback.set("此消息有已发送的分段，不能修改正文。")
                return
        if action == "approve" and not row.get("can_approve"):
            self.feedback.set("任务当前无法批准，请核对微信发送结果和任务是否已过期。")
            return
        if action == "mark_sent" and status not in ("uncertain", "unverified"):
            self.feedback.set("只有待核验或未核实的消息可以确认已发出。")
            return
        if action == "update":
            if status not in ("hold", "failed", "uncertain", "unverified"):
                self.feedback.set("该记录已处理或正在发送，不能修改正文。")
                return
            if not changed:
                self.feedback.set("正文没有改动。")
                return
            self._change("/drafts", {"id": row["id"], "action": "update", "text": text},
                         "已保存修改。")
            return
        prompt, notice = {
            "approve": ("从当前小号发送剩余未发的分段？" if row.get("segments_sent")
                        else "将此草稿交给 Orange 从当前小号发送？", "已批准，等待发送。"),
            "reject": ("取消处理这项草稿？", "已取消草稿。"),
            "mark_sent": ("你已经在微信中看到这条消息发出？", "已登记人工核验结果。"),
        }[action]
        payload = {"id": row["id"], "action": action}
        if action == "approve" and changed:
            payload["text"] = text
        preview = clipped(text if action == "approve" else row.get("text"), 200)
        if messagebox.askyesno("确认处理草稿",
                               f"{prompt}\n\n{row.get('display') or row.get('chat', '')}\n{preview}",
                               parent=self.root):
            self._change("/drafts", payload, notice)

    def _page_reminders(self):
        self.reminders_on = None
        self.reminder_contacts = {}
        shell, card = self._panel(self.workarea)
        shell.pack(fill="both", expand=True, pady=(0, 9))
        bar = self._row(card)
        self.reminder_switch = self._button(bar, "加载提醒状态", self._toggle_reminders,
                                            primary=True)
        self.reminder_switch.state(["disabled"])
        self.reminder_switch.pack(side="left")
        self._button(bar, "新建提醒", self._new_reminder, primary=True).pack(side="right")
        bar.pack_configure(pady=(0, 8))
        self._table(card, "reminders", (("display", "收件人", 115, False),
                    ("content", "提醒内容", 225, True), ("kind", "类别", 96, False),
                    ("rule", "重复", 85, False),
                    ("next", "下次发送", 135, False), ("status", "状态", 92, False)), height=6)
        bar = self._row(card)
        bar.pack_configure(pady=(7, 0))
        for action, title in (("pause", "暂停选中提醒"), ("resume", "恢复"),
                              ("cancel", "取消提醒")):
            self._button(bar, title, lambda act=action: self._reminder_action(act)
                         ).pack(side="left", padx=(0, 5))
        shell, events = self._panel(self.workarea, 10)
        shell.pack(fill="both", expand=True)
        self._label(events, "近期触发记录", bg=CARD, bold=True, size=11).pack(anchor="w", pady=(0, 5))
        self._table(events, "occurrences", (("time", "计划时间", 122, False),
                    ("target", "提醒", 200, True), ("status", "结果", 95, False),
                    ("info", "备注", 170, True)), height=3)

    def _toggle_reminders(self):
        if self.reminders_on is not None:
            self._switch("proactive.reminders.enabled", not self.reminders_on, "定时提醒",
                         disclosure="到点的有效提醒会由当前小号自动发送。"
                                    "启用后，符合规则的私聊提醒请求也可能交给模型识别。确认开启？")

    @staticmethod
    def _rule_label(rule):
        if (rule or "").startswith("weekly:"):
            return "每周"
        if (rule or "").startswith("monthly:"):
            return "每月"
        if (rule or "").startswith("interval:"):
            return f"每 {rule.split(':')[-1]} 天"
        return {"single": "一次", "daily": "每天"}.get(rule, rule or "—")

    def _load_reminders(self, data, chats):
        self.reminders_on = bool(data.get("enabled"))
        self.reminder_switch.configure(text="关闭定时提醒" if self.reminders_on else "开启定时提醒")
        self.reminder_switch.state(["!disabled"])
        self.reminder_contacts = {f"{r.get('display') or r['username']} · {r['username']}":
                                  r["username"] for r in chats.get("chats", [])
                                  if r.get("private", is_private_chat(r.get("username")))}
        items = data.get("items") or []

        def kind_of(row):
            """这条是「代传话」还是自己的「提醒」；代传话带上谁托的。"""
            if (row.get("confirm_mode") or "") == "relay":
                who = (row.get("relay_from") or "").strip()
                return ("代传话·%s" % who) if who else "代传话"
            return "提醒"

        self._fill("reminders", items, lambda r: r["id"],
                   lambda r: (r.get("display") or r.get("chat"), clipped(r.get("content")),
                              kind_of(r), self._rule_label(r.get("rule")),
                              clock(r.get("next_ts"), date=True),
                              translated(r.get("status"))))
        titles = {r["id"]: r.get("content") for r in items}
        self._fill("occurrences", data.get("occurrences") or [], lambda r: r["id"],
                   lambda r: (clock(r.get("scheduled_ts"), date=True),
                              titles.get(r.get("reminder_id")) or f"#{r.get('reminder_id', '—')}",
                              translated(r.get("status")), clipped(r.get("note") or r.get("error"))))

    def _new_reminder(self):
        dialog = tk.Toplevel(self.root, bg=CARD)
        dialog.title("新建提醒")
        dialog.transient(self.root)
        dialog.grab_set()
        panel = tk.Frame(dialog, bg=CARD, padx=20, pady=16)
        panel.pack(fill="both", expand=True)
        self._label(panel, "收件人", bg=CARD, color=MUTED).pack(anchor="w")
        recipient = tk.StringVar(dialog)
        shell, combo = self._combo(panel, recipient, tuple(self.reminder_contacts), width=52)
        shell.pack(fill="x", pady=(4, 10))
        content = tk.StringVar(dialog)
        field, _ = self._input(panel, "提醒内容（最多 60 字）", content)
        field.pack(fill="x", pady=(0, 8))
        initial = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")
        when = tk.StringVar(dialog, value=initial)
        field, _ = self._input(panel, "首次发送时间（本机时间）", when)
        field.pack(fill="x", pady=(0, 8))
        choice = tk.StringVar(dialog, value="仅一次")
        self._label(panel, "重复", bg=CARD, color=MUTED).pack(anchor="w")
        PillSelect(panel, PAL, options=tuple(RULES), variable=choice).pack(fill="x", pady=(5, 9))
        error = tk.StringVar(dialog)
        self._label(panel, variable=error, bg=CARD, color=ORANGE).pack(anchor="w")

        def save():
            try:
                raw = recipient.get()
                target = chat_id(self.reminder_contacts.get(raw, raw))
                if target.endswith("@chatroom"):
                    raise ValueError("定时提醒仅可发送到私聊。")
                text = content.get().strip()
                if not 0 < len(text) <= 60:
                    raise ValueError("提醒内容需要 1 至 60 字。")
                try:
                    moment = datetime.strptime(when.get().strip(), "%Y-%m-%d %H:%M")
                except ValueError:
                    raise ValueError("请输入 YYYY-MM-DD HH:MM 格式的时间。") from None
                if (moment - datetime.now()).total_seconds() < 5:
                    raise ValueError("请选择至少 5 秒后的时间。")
            except ValueError as exc:
                error.set(str(exc))
                return
            if "edit" in self.pending:
                error.set("当前有操作正在处理，请稍候。")
                return
            rule = RULES[choice.get()]
            if rule == "weekly":
                rule = f"weekly:{moment.isoweekday()}"
            self._change("/reminder", {"chat": target, "content": text,
                                       "at": moment.strftime("%Y-%m-%d %H:%M"), "rule": rule},
                         "提醒已保存。" if self.reminders_on else
                         "提醒已保存。开启定时提醒后才会发送。")
            dialog.destroy()

        bar = self._row(panel)
        self._button(bar, "保存提醒", save, primary=True).pack(side="right", padx=(6, 0))
        self._button(bar, "取消", dialog.destroy).pack(side="right")
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        combo.focus_set()

    def _reminder_action(self, action):
        reminder = self._selected("reminders")
        if not reminder:
            return
        if action == "cancel" and not messagebox.askyesno(
                "取消提醒", f"取消「{reminder.get('content', '')}」？", parent=self.root):
            return
        done = {"pause": "提醒已暂停。", "resume": "提醒已恢复。", "cancel": "提醒已取消。"}
        self._change(f"/reminders/{reminder['id']}", {"action": action}, done[action])

    def _page_proactive(self):
        shell, card = self._panel(self.workarea)
        shell.pack(fill="both", expand=True)
        self.proactive_on = None
        self.proactive_defaults = {}
        bar = self._row(card)
        bar.pack_configure(pady=(0, 8))
        self.proactive_switch = self._button(bar, "加载主动聊天状态", self._toggle_proactive,
                                             primary=True)
        self.proactive_switch.state(["disabled"])
        self.proactive_switch.pack(side="left")
        self._label(bar, "主动聊天的全局开关。", bg=CARD, color=MUTED).pack(side="left",
                                                                          padx=10)
        # 说明单独占一行：参数项有五个，塞进按钮栏会把开关挤变形或直接被窗口裁掉
        self._label(card,
                    "仅对白名单内的私聊联系人主动发起对话。选中联系人后可单独设置每日上限、"
                    "开口间隔与静默时长；未设置的项沿用全局配置。",
                    bg=CARD, color=MUTED, anchor="w", justify="left",
                    wraplength=self._s(730)).pack(fill="x", pady=(0, 8))
        tree = self._table(card, "proactive", (("display", "联系人", 130, True),
                          ("state", "状态", 145, True), ("next_at", "下次尝试", 100, False),
                          ("quota", "今日次数", 84, False), ("noreply", "连续未回", 84, False),
                          ("edit", "参数", 58, False)))
        tree.bind("<Button-1>", self._proactive_click)
        tree.bind("<Double-1>", lambda _: self._proactive_edit())
        bar = self._row(card)
        bar.pack_configure(pady=(8, 0))
        self._button(bar, "修改选中联系人的参数", self._proactive_edit,
                     primary=True).pack(side="left")
        self._button(bar, "允许选中联系人",
                     lambda: self._proactive_contact(True)).pack(side="left", padx=(6, 0))
        self._button(bar, "停用选中联系人", lambda: self._proactive_contact(False)
                     ).pack(side="left", padx=5)

    def _toggle_proactive(self):
        if self.proactive_on is not None:
            self._switch("proactive.enabled", not self.proactive_on, "主动聊天",
                         disclosure="Orange 会把已允许联系人的近期聊天内容交给模型，"
                                    "并可用当前小号主动开口。确认开启？")

    def _load_proactive(self, data):
        self.proactive_on = bool(data.get("enabled"))
        self.proactive_defaults = data.get("defaults") or {}
        self.proactive_switch.configure(text="关闭主动聊天" if self.proactive_on else "开启主动聊天")
        self.proactive_switch.state(["!disabled"])
        self._fill("proactive", data.get("items") or [], lambda r: r["username"],
                   lambda r: (r.get("display") or r["username"],
                              r.get("state") or ("已允许" if r.get("active") else "未开启"),
                              clock(r.get("next_at")),
                              f"{r.get('day_n', 0)} / {r.get('per_chat_daily', '—')}",
                              r.get("no_reply_streak", 0),
                              "修改"))

    def _proactive_click(self, event):
        """点「参数」列里的「修改」直接开弹窗（双击整行也可以）。"""
        tree = (self.tables.get("proactive") or (None, None))[0]
        if tree is None or tree.identify_region(event.x, event.y) != "cell":
            return None
        try:
            index = int(tree.identify_column(event.x).lstrip("#")) - 1
        except ValueError:
            return None
        columns = tree["columns"]
        if not 0 <= index < len(columns) or columns[index] != "edit":
            return None
        row = self.tables["proactive"][1].get(tree.identify_row(event.y))
        if row:
            self._proactive_edit(row)
        return "break"

    def _proactive_edit(self, row=None):
        row = row if isinstance(row, dict) else self._selected("proactive")
        if not row:
            return
        patch = self._proactive_form(row)
        if patch is None:
            return
        self._change("/proactive/params", {"chat": row["username"], "params": patch},
                     "该联系人的主动聊天参数已保存，下一次判定就按新参数来。")

    def _proactive_form(self, row):
        """某个联系人的主动聊天参数弹窗：返回要保存的 patch，取消返回 None。"""
        own = row.get("override") or {}
        dflt = self.proactive_defaults or {}
        who = row.get("display") or row["username"]
        dialog = tk.Toplevel(self.root, bg=BG)
        dialog.title("主动聊天参数 · %s" % who)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        shell, card = self._panel(dialog, 15)
        shell.pack(fill="both", expand=True, padx=self._s(14), pady=self._s(14))
        self._label(card, who, bg=CARD, bold=True, size=12).pack(anchor="w")
        self._label(card, "各项均从下拉选择；选择「跟随全局」即沿用全局设置。当前全局："
                          "每天 %s、间隔 %s~%s、时段 %s~%s 点。" % (
                              param_text("per_chat_daily", dflt.get("per_chat_daily", 3)),
                              param_text("idle_min_minutes", dflt.get("idle_min_minutes", 120)),
                              param_text("idle_max_minutes", dflt.get("idle_max_minutes", 360)),
                              (dflt.get("active_hours") or [9, 23])[0],
                              (dflt.get("active_hours") or [9, 23])[1]),
                    color=MUTED, bg=CARD, anchor="w", justify="left",
                    wraplength=self._s(390)).pack(fill="x", pady=(2, 8))
        fields = {}
        for key, title in PROACTIVE_TITLES.items():
            options = proactive_options(key, dflt.get(key), own.get(key))
            labels = [text for text, _ in options]
            current = next((text for text, value in options if value == own.get(key)),
                           labels[0])
            var = tk.StringVar(value=current)
            line = tk.Frame(card, bg=CARD)
            line.pack(fill="x", pady=(0, 6))
            line.columnconfigure(1, weight=1)
            self._label(line, title, bg=CARD).grid(row=0, column=0, sticky="w",
                                                   padx=(0, 10))
            shell, _box = self._combo(line, var, tuple(labels), readonly=True, width=17)
            shell.grid(row=0, column=1, sticky="ew")
            fields[key] = var
        hours = own.get("active_hours") or []
        hour_labels = [text for text, _ in PROACTIVE_HOURS]

        def hour_value(index):
            if len(hours) == 2:
                return next((text for text, value in PROACTIVE_HOURS
                             if value == hours[index]), PROACTIVE_FOLLOW)
            return PROACTIVE_FOLLOW

        hour_vars = [tk.StringVar(value=hour_value(0)), tk.StringVar(value=hour_value(1))]
        for column, title in enumerate(("主动时段 开始", "主动时段 结束")):
            line = tk.Frame(card, bg=CARD)
            line.pack(fill="x", pady=(0, 6))
            line.columnconfigure(1, weight=1)
            self._label(line, title, bg=CARD).grid(row=0, column=0, sticky="w",
                                                   padx=(0, 10))
            shell, _box = self._combo(line, hour_vars[column], tuple(hour_labels),
                                      readonly=True, width=17)
            shell.grid(row=0, column=1, sticky="ew")
        error = tk.StringVar()
        self._label(card, variable=error, bg=CARD, color=ORANGE, anchor="w",
                    justify="left", wraplength=self._s(390)).pack(fill="x")
        result = {}

        def submit():
            picked = {key: var.get() for key, var in fields.items()}
            picked["active_hours"] = [hour_vars[0].get(), hour_vars[1].get()]
            patch, problem = proactive_patch(picked, own, dflt)
            if problem:
                error.set(problem)
                return
            result.update(patch=patch)
            dialog.destroy()

        def reset_all():
            for key, var in fields.items():
                var.set(proactive_options(key, dflt.get(key), own.get(key))[0][0])
            for var in hour_vars:
                var.set(PROACTIVE_FOLLOW)
            error.set("")

        bar = self._row(card)
        bar.pack_configure(pady=(4, 0))
        self._button(bar, "取消", dialog.destroy).pack(side="right")
        self._button(bar, "保存", submit, primary=True).pack(side="right", padx=(0, 6))
        self._button(bar, "全部跟随全局", reset_all).pack(side="left")
        dialog.update_idletasks()
        width, height = dialog.winfo_reqwidth(), dialog.winfo_reqheight()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 4)
        dialog.geometry("+%d+%d" % (x, y))
        try:
            dialog.grab_set()
        except tk.TclError:
            pass
        dialog.wait_window()
        return result.get("patch")

    def _proactive_contact(self, enabled):
        row = self._selected("proactive")
        if not row:
            return
        if enabled and not messagebox.askyesno("允许主动聊天",
                 f"允许 Orange 向 {row.get('display') or row['username']} 主动发消息？",
                 parent=self.root):
            return
        self._change("/proactive/contact", {"chat": row["username"], "enabled": enabled},
                     "已允许该联系人。" if enabled else "已停止该联系人的主动聊天。")

    # ---------------------------------------------------------------- 固定人设 / 关系
    def _page_persona(self):
        shell, card = self._panel(self.workarea)
        shell.pack(fill="both", expand=True)
        self.persona_status = tk.StringVar(value="正在读取固定人设…")
        self._label(card, variable=self.persona_status, bg=CARD, bold=True,
                    size=12).pack(anchor="w")
        self._label(card,
                    "固定人设始终位于提示词首段，记忆、联网结果与对方的发言都不会覆盖它。"
                    "此处只需描述角色身份、性格与说话方式；人物关系请在「关系」页维护，"
                    "对话中积累的信息由记忆模块保存。",
                    bg=CARD, color=MUTED, anchor="w", justify="left",
                    wraplength=self._s(715)).pack(fill="x", pady=(4, 8))
        # 按钮先占底部：窗口压小的时候先丢正文高度，别把保存按钮挤没
        bar = self._row(card)
        bar.pack_configure(side="bottom", pady=(9, 0))
        self.persona_hint = tk.StringVar()
        self._label(bar, variable=self.persona_hint, color=MUTED, bg=CARD).pack(side="left")
        self.persona_save = self._button(bar, "保存固定人设", self._save_persona, primary=True)
        self.persona_save.state(["disabled"])
        self.persona_save.pack(side="right")
        self._button(bar, "重新读取", lambda: self._load_page()).pack(side="right",
                                                                     padx=(0, 6))
        self.persona_text = self._text(card, height=10)
        self.persona_text.pack(fill="both", expand=True)
        self.persona_text.bind("<KeyRelease>", lambda _event: self._persona_count())

    def _persona_count(self):
        text = self.persona_text.get("1.0", "end-1c").strip()
        self.persona_hint.set("%d 字 / 上限 4000。改了以后下一句回复就生效，不用重启。"
                              % len(text))

    def _load_persona(self, data):
        text = str(data.get("fixed_persona") or "")
        label = {"card": "人设卡 persona_card.json",
                 "config": "config.json 的 agent.persona（还没在控制台保存过）"}.get(
                     data.get("source"), "空")
        self.persona_status.set("来源：%s" % label)
        self.persona_text.configure(state="normal")
        self.persona_text.delete("1.0", "end")
        self.persona_text.insert("1.0", text)
        self._persona_count()
        self.persona_save.state(["!disabled"])

    def _save_persona(self):
        text = self.persona_text.get("1.0", "end-1c").strip()
        if len(text) < 10:
            self.feedback.set("固定人设过短，至少需要 10 个字。")
            return
        if len(text) > 4000:
            self.feedback.set("固定人设不能超过 4000 字，请精简后再保存。")
            return
        self._change("/persona", {"action": "save", "fixed_persona": text},
                     "固定人设已保存，下一句回复起生效。")

    def _page_relations(self):
        shell, card = self._panel(self.workarea, 12)
        shell.pack(fill="both", expand=True)
        self.relations_status = tk.StringVar(value="正在读取关系表…")
        self._label(card, variable=self.relations_status, bg=CARD, bold=True,
                    size=12).pack(anchor="w")
        self._label(card,
                    "关系表随每轮对话写入提示词，不依赖聊天摘要，历史被压缩也不会丢失对方身份。"
                    "「绑定会话」指定该关系对应的对话对象；不绑定指双方共同认识、并不直接对话"
                    "的人（如对方家人）。",
                    bg=CARD, color=MUTED, anchor="w", justify="left",
                    wraplength=self._s(730)).pack(fill="x", pady=(4, 8))
        # 按钮先占底部：表格再高也不会把增删按钮挤出去
        bar = self._row(card)
        bar.pack_configure(side="bottom", pady=(9, 0))
        self._button(bar, "新增关系", self._relation_add, primary=True).pack(side="left")
        self._button(bar, "编辑选中", self._relation_edit).pack(side="left", padx=5)
        self._button(bar, "删除选中", self._relation_delete).pack(side="left")
        self.relation_items = []
        self.relation_contacts = {}
        self.relation_chat_labels = {}
        self._table(card, "relations", (("name", "称呼", 100, False),
                    ("relation", "关系", 180, True), ("nickname", "你叫她/他", 100, False),
                    ("chat", "绑定会话", 200, False), ("note", "备注", 130, True)), height=8)

    def _load_relations(self, data, chats):
        contacts = [r for r in (chats.get("chats") or []) if r.get("username")]
        # 会话列表 → 「显示名 · wxid」选项，和定时提醒页同一口径
        self.relation_contacts = {
            f"{r.get('display') or r['username']} · {r['username']}": r["username"]
            for r in contacts}
        self.relation_chat_labels = {r["username"]: (r.get("display") or r["username"])
                                     for r in contacts}
        items = [dict(x) for x in (data.get("relations") or []) if isinstance(x, dict)]
        self.relation_items = items
        rows = [dict(item, _id=str(index)) for index, item in enumerate(items)]
        self.relations_status.set("共 %d 条关系  ·  %s"
                                  % (len(items), data.get("path") or "persona_card.json"))
        self._fill("relations", rows, lambda r: r["_id"],
                   lambda r: (r.get("name") or "—", r.get("relation") or "—",
                              r.get("nickname") or "—",
                              self.relation_chat_labels.get(r.get("chat"),
                                                            r.get("chat") or "不绑定"),
                              clipped(r.get("note") or "", 40)),
                   empty="还没有关系，点左下角「新增关系」加一条")

    def _relation_add(self):
        items = [dict(x) for x in self.relation_items]
        item = self._relation_form("新增关系")
        if item:
            self._save_relations(items + [item])

    def _relation_edit(self):
        row = self._selected("relations")
        if not row:
            return
        index = int(row["_id"])
        items = [dict(x) for x in self.relation_items]
        item = self._relation_form("编辑关系", items[index])
        if item:
            items[index] = item
            self._save_relations(items)

    def _relation_delete(self):
        row = self._selected("relations")
        if not row:
            return
        index = int(row["_id"])
        items = [dict(x) for x in self.relation_items]
        item = items[index]
        if not messagebox.askyesno(
                "删除关系", "删除「%s：%s」？删掉以后 Orange 就不知道这个人了。"
                % (item.get("name") or "—", item.get("relation") or "—"),
                parent=self.root):
            return
        self._save_relations([x for i, x in enumerate(items) if i != index])

    def _save_relations(self, items):
        self._change("/persona", {"action": "relations", "items": items},
                     "关系表已保存，下一句回复起生效。")

    def _relation_form(self, title, item=None):
        """关系编辑弹窗：返回填好的条目，取消返回 None。"""
        item = dict(item or {})
        dialog = tk.Toplevel(self.root, bg=BG)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        shell, card = self._panel(dialog, 15)
        shell.pack(fill="both", expand=True, padx=self._s(14), pady=self._s(14))
        self._label(card, title, bg=CARD, bold=True, size=12).pack(anchor="w", pady=(0, 8))
        fields = {}
        for key, label in (("name", "称呼（例：老妈、室友）"),
                           ("relation", "关系（例：你和哥哥的妈妈）"),
                           ("nickname", "你对对方的称呼（可留空）")):
            var = tk.StringVar(value=str(item.get(key) or ""))
            field, _entry = self._input(card, label, var, width=34)
            field.pack(fill="x", pady=(0, 7))
            fields[key] = var
        # 绑定会话：从会话列表里选联系人，不再手抄 wxid
        unbound = "不绑定（你们都认识的人）"
        choices = [unbound] + list(self.relation_contacts)
        current = str(item.get("chat") or "")
        current_label = next((k for k, v in self.relation_contacts.items() if v == current), "")
        if current and not current_label:
            current_label = "当前绑定 · %s" % current
            choices.insert(1, current_label)
        chat_var = tk.StringVar(value=current_label or unbound)
        field = tk.Frame(card, bg=CARD)
        hint = "" if self.relation_contacts else "（没读到会话列表，先刷新页面再绑定）"
        self._label(field, "绑定会话（选中的聊天里，这个人就是这条关系）" + hint,
                    color=MUTED, bg=CARD, justify="left",
                    wraplength=self._s(400)).pack(anchor="w", pady=(0, 4))
        shell, combo = self._combo(field, chat_var, tuple(choices), readonly=True, width=32)
        shell.pack(fill="x")
        field.pack(fill="x", pady=(0, 7))
        fields["chat"] = chat_var
        note_var = tk.StringVar(value=str(item.get("note") or ""))
        field, _entry = self._input(card, "备注（可留空）", note_var, width=34)
        field.pack(fill="x", pady=(0, 7))
        fields["note"] = note_var
        error = tk.StringVar()
        self._label(card, variable=error, bg=CARD, color=ORANGE, anchor="w",
                    justify="left", wraplength=self._s(430)).pack(fill="x")
        result = {}

        def submit():
            name = fields["name"].get().strip()
            relation = fields["relation"].get().strip()
            picked = fields["chat"].get().strip()
            if not name:
                error.set("称呼不能为空，例如：老妈。")
                return
            if not relation:
                error.set("关系不能为空，例如：你和哥哥的妈妈。")
                return
            if picked == unbound or not picked:
                chat = ""
            else:
                chat = self.relation_contacts.get(picked, "")
                if not chat:                 # 表里没有的旧绑定：从标签里取回 ID
                    match = re.search(r"(wxid_[A-Za-z0-9_-]+|[0-9A-Za-z]+@chatroom)", picked)
                    chat = match.group(1) if match else ""
                if chat:
                    try:
                        chat = chat_id(chat)
                    except ValueError as exc:
                        error.set(str(exc))
                        return
            result.update(name=name, relation=relation,
                          nickname=fields["nickname"].get().strip(), chat=chat,
                          note=fields["note"].get().strip())
            dialog.destroy()

        bar = self._row(card)
        bar.pack_configure(pady=(8, 0))
        self._button(bar, "取消", dialog.destroy).pack(side="right")
        self._button(bar, "保存", submit, primary=True).pack(side="right", padx=(0, 6))
        dialog.update_idletasks()
        width, height = dialog.winfo_reqwidth(), dialog.winfo_reqheight()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 3)
        dialog.geometry("+%d+%d" % (x, y))
        try:
            dialog.grab_set()
        except tk.TclError:
            pass
        dialog.wait_window()
        return result or None

    def _page_knowledge(self):
        self.kb_status = tk.StringVar(value="正在读取索引…")
        self.kb_inbox = ""
        shell, card = self._panel(self.workarea, 11)
        shell.pack(fill="both", expand=True, pady=(0, 9))
        bar = self._row(card)
        self._label(bar, variable=self.kb_status, bg=CARD, bold=True).pack(side="left")
        self._button(bar, "导入文件", self._kb_import, primary=True).pack(side="right")
        self._button(bar, "扫描收件目录", self._kb_scan).pack(side="right", padx=5)
        bar.pack_configure(pady=(0, 7))
        self._table(card, "kb_items", (("title", "资料", 230, True),
                    ("scope", "可用范围", 125, False), ("source", "来源", 135, True)), height=4)
        bar = self._row(card)
        bar.pack_configure(pady=(7, 0))
        self._button(bar, "撤回资料", self._kb_forget).pack(side="left")
        self._button(bar, "打开收件目录", self._kb_open_folder).pack(side="left", padx=5)
        self._button(bar, "入库记录", self._kb_show_jobs).pack(side="right")
        self.kb_jobs = []
        shell, search = self._panel(self.workarea, 11)
        shell.pack(fill="both", expand=True)
        self._label(search, "检索预览", bg=CARD, bold=True).pack(anchor="w", pady=(0, 4))
        bar = self._row(search)
        self.kb_query = tk.StringVar()
        self.kb_chat = tk.StringVar()
        query, query_input = self._input(bar, "搜索词", self.kb_query)
        query.pack(side="left", fill="x", expand=True)
        field, chat_input = self._input(bar, "私聊 ID（可留空）", self.kb_chat, width=17)
        field.pack(side="left", padx=(6, 5))
        self._button(bar, "检索", self._kb_search).pack(side="right", anchor="s")
        query_input.bind("<Return>", lambda _: self._kb_search())
        chat_input.bind("<Return>", lambda _: self._kb_search())
        self.kb_result = self._text(search, height=4, read_only=True)
        self.kb_result.pack(fill="both", expand=True, pady=(7, 0))
        self._put(self.kb_result, "留空私聊 ID 时，仅检索所有会话可用的资料。")

    def _load_knowledge(self, data):
        stats = data.get("stats") or {}
        label = "已启用" if data.get("enabled") else "已关闭"
        self.kb_status.set(f"{label}  ·  {stats.get('documents', 0)} 份资料")
        self.kb_jobs = data.get("jobs") or []
        self.kb_inbox = data.get("inbox") or ""
        self._fill("kb_items", data.get("items") or [], lambda r: r["id"],
                   lambda r: (r.get("title") or "未命名", "所有会话" if r.get("scope") == "public"
                              else r.get("chat_id") or "指定会话",
                              clipped(r.get("kind") or r.get("source") or r.get("origin"))))

    def _kb_open_folder(self):
        if not self.kb_inbox:
            self.feedback.set("当前收件目录不可用，请刷新知识库状态。")
            return
        try:
            os.startfile(self.kb_inbox)
        except OSError as exc:
            self.feedback.set(f"无法打开收件目录。{exc}")

    def _kb_import(self):
        path = filedialog.askopenfilename(title="选择要导入的知识资料", parent=self.root,
                                         filetypes=[("全部文件", "*.*")])
        if not path:
            return
        answer = simpledialog.askstring("资料使用范围",
                                        "填写私聊 ID 可限制使用范围。\n留空将允许所有会话使用。",
                                        parent=self.root)
        if answer is None:
            return
        chat = answer.strip()
        if chat:
            try:
                chat = chat_id(chat)
            except ValueError as exc:
                self.feedback.set(str(exc))
                return
        elif not messagebox.askyesno("导入共享资料", "所有符合回复条件的会话都可能引用这份资料。继续？",
                                    parent=self.root):
            return
        self._change("/kb", {"action": "ingest", "path": path, "chat": chat},
                     "资料已导入。", timeout=90)

    def _kb_scan(self):
        if messagebox.askyesno("扫描收件目录",
                               "收件目录中新入库的资料可供所有会话使用。继续？", parent=self.root):
            self._change("/kb", {"action": "scan"}, "扫描完成。", timeout=90)

    def _kb_forget(self):
        row = self._selected("kb_items")
        if row and messagebox.askyesno("撤回资料", f"撤回「{row.get('title') or '未命名'}」？",
                                      parent=self.root):
            self._change("/kb", {"action": "forget", "id": row["id"]}, "已撤回资料。")

    def _kb_search(self):
        query = self.kb_query.get().strip()
        chat = self.kb_chat.get().strip()
        if not query:
            self.feedback.set("请输入搜索词。")
            return
        if chat:
            try:
                chat = chat_id(chat)
            except ValueError as exc:
                self.feedback.set(str(exc))
                return

        def loaded(data):
            hits = data.get("results") or data.get("hits") or data.get("items") or []
            rendered = "\n\n".join(
                f"{hit.get('title') or '未命名'}  {hit.get('locator') or ''}\n"
                f"{hit.get('text') or hit.get('snippet') or ''}" for hit in hits)
            self._put(self.kb_result, rendered or "没有匹配的资料。")
            self.feedback.set(f"找到 {len(hits)} 条结果。")

        self._task("kb-search", lambda: self._request(
            "/kb", {"action": "search", "query": query, "chat": chat}), loaded,
            message="正在检索资料…")

    def _kb_show_jobs(self):
        rows = self.kb_jobs
        dialog = tk.Toplevel(self.root, bg=CARD)
        dialog.title("知识库入库记录")
        dialog.transient(self.root)
        dialog.geometry("620x350")
        text = self._text(dialog, read_only=True)
        text.pack(fill="both", expand=True, padx=12, pady=12)
        self._put(text, "\n\n".join(f"{row.get('title') or '未命名'}  "
                    f"·  {translated(row.get('state'))}\n{row.get('error_code') or ''}"
                    for row in rows) or "暂无入库记录。")

    def _page_logs(self):
        shell, card = self._panel(self.workarea, 12)
        shell.pack(fill="both", expand=True)
        self.usage = tk.StringVar(value="正在读取用量…")
        self._label(card, variable=self.usage, bg=CARD, bold=True).pack(anchor="w")
        self._label(card, "最近 200 行运行日志", bg=CARD, color=MUTED,
                    size=9).pack(anchor="w", pady=(9, 4))
        frame = self._row(card)
        frame.pack_configure(fill="both", expand=True)
        self.logs = self._text(frame, read_only=True)
        scroller = ttk.Scrollbar(frame, command=self.logs.yview)
        self.logs.configure(yscrollcommand=scroller.set)
        self.logs.pack(side="left", fill="both", expand=True)
        scroller.pack(side="right", fill="y")
        self._button(card, "复制日志", self._copy_logs).pack(anchor="e", pady=(8, 0))

    def _load_logs(self, data, usage):
        llm = usage.get("llm") or {}
        web = usage.get("web") or {}
        search = f"   ·   今日联网查询 {web['search']} 次" if "search" in web else ""
        self.usage.set(f"最近 24 小时   ·   模型调用 {llm.get('calls', '—')} 次   "
                       f"·   {llm.get('tokens', '—')} Tokens{search}")
        self._put(self.logs, "".join(data.get("lines") or []) or "暂无运行日志。")
        self.logs.see("end")

    def _copy_logs(self):
        content = self.logs.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.feedback.set("已复制当前日志。")

    def _page_settings(self):
        shell, card = self._panel(self.workarea)
        shell.pack(fill="x", pady=(0, 9))
        self._label(card, "本次连接", bg=CARD, bold=True, size=12).pack(anchor="w")
        self.settings_session = tk.StringVar()
        self._label(card, variable=self.settings_session, bg=CARD, color=MUTED,
                    anchor="w", justify="left", wraplength=600).pack(fill="x", pady=(5, 0))
        shell, card = self._panel(self.workarea)
        shell.pack(fill="x")
        self._label(card, "回复与隐私", bg=CARD, bold=True, size=12).pack(anchor="w", pady=(0, 6))
        self.preferences = {key: tk.BooleanVar() for key, _ in SETTINGS}
        options = self._row(card)
        options.columnconfigure(0, weight=1, uniform="prefs")
        options.columnconfigure(1, weight=1, uniform="prefs")
        for index, (key, title) in enumerate(SETTINGS):
            self._check(options, title, self.preferences[key]).grid(
                row=index // 2, column=index % 2, padx=(0, 8), pady=5, sticky="w")
        self.settings_original = None
        self.allowlist = tk.StringVar()
        field, _ = self._input(card, "自动回复白名单（多个 ID 用空格或逗号分隔，留空则适用普通门禁）",
                               self.allowlist)
        field.pack(fill="x", pady=(9, 0))
        bar = self._row(card)
        self.save_settings = self._button(bar, "保存设置", self._save_settings, primary=True)
        self.save_settings.state(["disabled"])
        self.save_settings.pack(side="right", pady=(9, 0))

    def _load_settings(self, config):
        llm = config.get("llm") or {}
        account = self.session.get("account") or "—"
        model = self.session.get("model") or llm.get("model") or "—"
        base = self.session.get("base_url") or llm.get("base_url") or "—"
        self.settings_session.set(f"绑定小号  {account}\n模型  {model}\nAPI 地址  {base}")
        settings = {}
        for key, var in self.preferences.items():
            section, name = key.split(".")
            settings[key] = bool((config.get(section) or {}).get(name))
            var.set(settings[key])
        settings["agent.reply_whitelist"] = (config.get("agent") or {}).get("reply_whitelist") or []
        self.allowlist.set(", ".join(settings["agent.reply_whitelist"]))
        self.settings_original = settings
        self.save_settings.state(["!disabled"])

    def _save_settings(self):
        if self.settings_original is None:
            return
        values = {key: var.get() for key, var in self.preferences.items()}
        try:
            entries = (entry for entry in re.split(r"[\s,，]+", self.allowlist.get()) if entry)
            values["agent.reply_whitelist"] = list(dict.fromkeys(chat_id(entry) for entry in entries))
        except ValueError as exc:
            self.feedback.set(str(exc))
            return
        changed = {key: value for key, value in values.items()
                   if value != self.settings_original.get(key)}
        if not changed:
            self.feedback.set("没有需要保存的改动。")
            return
        impacts = {
            "agent.group_reply": "群聊回复可能将相关群聊内容发给模型 API。",
            "web.enabled": "联网检索可能将整理后的聊天查询发送到模型服务。",
            "memory.enabled": "长期记忆会把聊天记录写入本机数据库。",
            "logging.chat_content": "运行日志会保存聊天正文。",
            "kb.enabled": "相关知识库资料可能随回复上下文发给模型 API。",
        }
        notes = [note for key, note in impacts.items() if changed.get(key)]
        if notes and not messagebox.askyesno("确认设置", "\n".join(notes + ["保存这些更改？"]),
                                            parent=self.root):
            return
        self._change("/config", changed, "设置已保存。")


def main():
    try:                                    # 让任务栏图标独立于 python.exe
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Orange.WeChat.Console")
    except Exception:
        pass
    try:
        # 声明 DPI 感知：否则 Windows 会把窗口缩小（如 125%/150% 缩放时），
        # 而 tkinter 仍按原始分辨率布局，右侧和底部内容会被裁掉。
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # 系统 DPI 感知
    except Exception:
        pass
    root = tk.Tk()
    OrangeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

