# -*- coding: utf-8 -*-
"""Orange 控制台外观件：胶囊按钮 + 圆角渐变毛玻璃卡片 / 输入框。

Tk 本身没有圆角、渐变和模糊，所以这里用 Pillow 把外观画成 2 倍超采样的小图
再缩小（边缘才平滑），Tk 只负责布局和事件。同一「尺寸 + 状态」只渲染一次，
画好的图缓存在模块级字典里复用，切页/改窗口大小不会重复计算。

没有 Pillow 时（requirements.txt 里 pillow>=10.0 已声明）自动退化成方块外观，
功能不受影响。
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
import tkinter.ttk as ttk

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageTk
    PIL_OK = True
except Exception:  # pragma: no cover - 只做降级
    PIL_OK = False
    Image = None

SS = 2                      # 超采样倍数
SCALE = 1.0                 # 界面缩放：窗口越大字体/控件越大，由 app.py 按窗口宽度设置
_FONT_DIR = "C:/Windows/Fonts/"
_FONT_FILES = {
    False: ("msyh.ttc", "simhei.ttf", "segoeui.ttf"),
    True: ("msyhbd.ttc", "msyh.ttc", "simhei.ttf", "segoeuib.ttf"),
}
_CACHE: dict = {}
_FONTS: dict = {}
# 2 倍超采样缩小用 BOX（面积平均）：边缘平滑，且不会像 LANCZOS 那样在细描边
# 两侧产生明暗振铃（描边旁边会冒出灰线）。
_RESAMPLE = getattr(Image, "BOX", None) if PIL_OK else None


def _px(value):
    """按当前界面缩放取像素值。"""
    return int(round(value * SCALE))


def _parent_bg(parent, fallback):
    """取父控件的底色；按钮画布的底要和父级一致，圆角外才不会露出方块。"""
    try:
        value = parent.cget("bg")
    except Exception:
        return fallback
    return str(value) or fallback


class Palette:
    """一套颜色与字体。app.py 把自己的品牌色传进来，渲染层不写死配色。"""

    def __init__(self, *, bg, card, ink, muted, line, orange, orange_bg,
                 orange_light="#B85C2E", orange_dark="#8F3B17",
                 line_strong="#DCCBB5", green="#2F654D", font="Microsoft YaHei UI"):
        self.bg = bg
        self.card = card
        self.ink = ink
        self.muted = muted
        self.line = line
        self.line_strong = line_strong
        self.orange = orange
        self.orange_bg = orange_bg
        self.orange_light = orange_light
        self.orange_dark = orange_dark
        self.green = green
        self.font = font


# ---------------------------------------------------------------- 基础绘制

def _rgb(color):
    if isinstance(color, tuple):
        return color[:3]
    color = color.lstrip("#")
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))


def _fade(color, alpha):
    r, g, b = _rgb(color)
    return (r, g, b, max(0, min(255, int(alpha))))


def _solid(w, h, color):
    return Image.new("RGBA", (max(1, w), max(1, h)), color)


def _gradient(w, h, top, bottom, *, horizontal=False):
    """线性渐变：先画 1×256 的色带再拉伸，比整幅合成快一个数量级。"""
    w, h = max(1, int(w)), max(1, int(h))
    band = Image.new("RGBA", (256, 1) if horizontal else (1, 256))
    band.putdata([tuple(int(round(top[c] + (bottom[c] - top[c]) * i / 255))
                        for c in range(4)) for i in range(256)])
    return band.resize((w, h), Image.NEAREST)


def _mask(w, h, inset, radius):
    """圆角矩形蒙版；inset 是四周留白，radius 是圆角半径（设备像素）。"""
    mask = Image.new("L", (max(1, w), max(1, h)), 0)
    left, top = inset, inset
    right, bottom = w - 1 - inset, h - 1 - inset
    if right <= left + 1 or bottom <= top + 1:
        return mask
    radius = max(0, min(radius, (right - left) // 2, (bottom - top) // 2))
    ImageDraw.Draw(mask).rounded_rectangle((left, top, right, bottom),
                                           radius=radius, fill=255)
    return mask


def _apply_alpha(image, mask):
    """把圆角蒙版乘进现有的 alpha 通道（保留原有半透明）。"""
    image.putalpha(ImageChops.multiply(image.getchannel("A"), mask))
    return image


def _soft_rect(w, h, box, radius, color, blur=0, down=1, outline=0):
    """圆角矩形软图层，只模糊 alpha。

    down>1 时先在 1/down 分辨率上画（软边和阴影不需要原分辨率），再放大回来，
    大卡片的渲染时间能降到 1/3 左右。坐标都是原分辨率坐标。
    """
    s = max(1, int(down))
    sw, sh = max(1, w // s), max(1, h // s)
    mask = Image.new("L", (sw, sh), 0)
    draw = ImageDraw.Draw(mask)
    scaled = tuple(int(round(v / s)) for v in box)
    inner_radius = max(1, int(radius / s))
    if outline:
        draw.rounded_rectangle(scaled, radius=inner_radius, fill=None, outline=255,
                               width=max(1, int(outline / s)))
    else:
        draw.rounded_rectangle(scaled, radius=inner_radius, fill=255)
    if blur:
        mask = mask.filter(ImageFilter.GaussianBlur(max(0.5, blur / s)))
    if s > 1:
        mask = mask.resize((w, h), Image.BILINEAR)
    alpha = color[3] if len(color) > 3 else 255
    if alpha < 255:
        mask = mask.point([value * alpha // 255 for value in range(256)])
    layer = Image.new("RGBA", (w, h), tuple(color[:3]) + (0,))
    layer.putalpha(mask)
    return layer


def _soft_shape(w, h, shape, color, blur=0):
    """先在 L 蒙版上画形状，模糊蒙版，再填成 color。

    直接模糊 RGBA 图层会把透明区域的黑色混进颜色，描边旁边就会冒出一圈灰；
    先对蒙版模糊就不会有这个问题。
    """
    mask = Image.new("L", (max(1, w), max(1, h)), 0)
    shape(ImageDraw.Draw(mask))
    if blur:
        mask = mask.filter(ImageFilter.GaussianBlur(blur))
    alpha = color[3] if len(color) > 3 else 255
    if alpha < 255:
        mask = mask.point([value * alpha // 255 for value in range(256)])
    layer = Image.new("RGBA", mask.size, tuple(color[:3]) + (0,))
    layer.putalpha(mask)
    return layer


def _font(px, bold):
    key = (px, bold)
    font = _FONTS.get(key)
    if font is None:
        font = False
        for name in _FONT_FILES[bold]:
            try:
                font = ImageFont.truetype(_FONT_DIR + name, px)
            except Exception:
                continue
            break
        if font is False:
            font = ImageFont.load_default()
        _FONTS[key] = font
    return font


def _photo(widget, key, build):
    """按 (Tcl 解释器, 外观键) 缓存 PhotoImage，避免重复渲染。"""
    # 用 interpaddr 区分解释器：同进程里开第二个 Tk 根窗口时，两个解释器的
    # 窗口名都是 "."，只按窗口名做键会把 A 的图片发给 B（报 image 不存在）。
    cache_key = (int(widget.tk.interpaddr()), key)
    photo = _CACHE.get(cache_key)
    if photo is None:
        if len(_CACHE) > 600:            # 防止长时间运行后无限增长
            _CACHE.clear()
        photo = ImageTk.PhotoImage(build(), master=widget)
        _CACHE[cache_key] = photo
    return photo


def _ring(w, h, inset, width, radius, top, bottom):
    """圆角描边环：沿圆角矩形走一圈的渐变线。"""
    outer = _mask(w, h, inset, radius)
    inner = _mask(w, h, inset + width, max(0, radius - width))
    ring = ImageChops.subtract(outer, inner)
    line = _gradient(w, h, top, bottom)
    return _apply_alpha(line, ring)


# ---------------------------------------------------------------- 卡片

def _band_fade(w, h, inset, band, radius, down=2):
    """贴着圆角外沿、向内淡出到 0 的蒙版（分层侵蚀 + 模糊）。

    用它上色，玻璃带在靠近内容区那一侧会淡到完全透明，内容框的直角边就看不出来。
    软边不需要原分辨率，down>1 时在 1/down 分辨率上算完再放大。
    """
    s = max(1, int(down))
    sw, sh = max(1, w // s), max(1, h // s)
    inset_s, band_s = max(1, inset // s), max(1, band // s)
    radius_s = max(2, radius // s)
    steps = 3
    alpha = Image.new("L", (sw, sh), 0)
    edges = []
    for i in range(steps + 1):
        k = int(round(band_s * i / steps))
        edges.append(_mask(sw, sh, inset_s + k, max(1, radius_s - k)))
    for i in range(steps):
        ring = ImageChops.subtract(edges[i], edges[i + 1])
        alpha.paste(int(255 * (1 - (i + 0.5) / steps) ** 1.8), (0, 0), ring)
    alpha = alpha.filter(ImageFilter.GaussianBlur(max(1, int(band_s * 0.45))))
    if s > 1:
        alpha = alpha.resize((max(1, w), max(1, h)), Image.BILINEAR)
    return alpha


def render_card(w, h, pal, *, radius=22, inset=6, band=10, bg=None):
    """圆角毛玻璃卡片：外阴影 + 玻璃带 + 渐变描边 + 内侧一圈高光。

    画布按卡片所在底色（bg）不透明地铺满：整图不透明，缩放时就不会出现
    RGBA 边缘发黑的锯齿（PIL 缩放透明像素会把黑色混进来）。

    关键一点：玻璃带往里那一圈直接铺成和内容区一样的纯白（band 与内容框内缩
    一致），这样内容框的直角边就看不出来——只剩外圈那条圆角玻璃带。
    """
    w, h = max(2, int(w)), max(2, int(h))
    W, H = w * SS, h * SS
    inset *= SS
    radius *= SS
    band = max(1, band) * SS
    canvas = Image.new("RGBA", (W, H), _fade(bg or pal.bg, 255))
    # 外阴影（很浅，只负责把卡片托离底色）
    canvas.alpha_composite(_soft_rect(
        W, H, (inset, inset + 2 * SS, W - 1 - inset, H - 1 - inset), radius,
        _fade("#7A5636", 58), blur=3 * SS, down=2))
    # 卡面整块纯白：内容框也是纯白，两者之间不存在色差
    face = Image.new("RGBA", (W, H), _fade(pal.card, 255))
    _apply_alpha(face, _mask(W, H, inset, radius))
    canvas.alpha_composite(face)
    # 玻璃带：贴着描边的一圈暖色，向内淡出到透明（= 白），所以和内容区没有硬边
    band_alpha = _band_fade(W, H, inset, band, radius)
    warm = _gradient(W, H, _fade("#FBF1E2", 255), _fade("#F2DFC3", 255))
    warm.putalpha(ImageChops.multiply(warm.getchannel("A"), band_alpha))
    canvas.alpha_composite(warm)
    # 内侧玻璃高光：紧贴描边往内淡出的一圈白
    canvas.alpha_composite(_soft_rect(
        W, H, (inset, inset, W - 1 - inset, H - 1 - inset), radius,
        _fade("#FFFFFF", 150), blur=1.2 * SS, down=2, outline=int(2 * SS)))
    # 描边：上浅下暖，模拟左上角来光
    canvas.alpha_composite(_ring(W, H, inset, max(1, int(1.3 * SS)), radius,
                                 _fade("#EFDCC4", 150), _fade(pal.orange, 108)))
    return canvas.resize((w, h), _RESAMPLE)


class GlassFrame(tk.Frame):
    """圆角渐变毛玻璃卡片外壳。取 .inner 当内容容器用。

    inset 是卡面距外壳边缘的留白（放阴影用），band 是卡面到内容区的玻璃边框宽度；
    内容框必须内缩到圆角以内，否则白色内容会盖住圆角。
    """

    def __init__(self, parent, pal, *, padding=14, radius=22, inset=6, band=None,
                 fill=None):
        super().__init__(parent, bg=pal.bg, bd=0, highlightthickness=0)
        self.pal = pal
        self.radius = _px(radius)
        self.inset = _px(inset)
        self.band = _px(band if band is not None else radius * 0.46)
        self._key = None
        padding = _px(padding)
        if not PIL_OK:
            self.inner = tk.Frame(self, bg=fill or pal.card, padx=padding, pady=padding)
            self.inner.pack(fill="both", expand=True)
            self.configure(highlightbackground=pal.line, highlightthickness=1)
            return
        # 先放背景画布，再放内容框：Tk 按创建顺序叠放，后建的在上层
        self._canvas = tk.Canvas(self, bg=pal.bg, bd=0, highlightthickness=0, takefocus=0)
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._item = self._canvas.create_image(0, 0, anchor="nw")
        pad = self.inset + self.band
        self.inner = tk.Frame(self, bg=fill or pal.card, padx=padding, pady=padding)
        self.inner.pack(fill="both", expand=True, padx=pad, pady=pad)
        self.bind("<Configure>", self._draw)

    def _draw(self, event=None):
        w = event.width if event is not None else self.winfo_width()
        h = event.height if event is not None else self.winfo_height()
        if self._key == (w, h) or w < 12 or h < 12:
            return
        self._key = (w, h)
        photo = _photo(self, ("card", w, h, self.radius, self.inset, self.band,
                              self.pal.bg, self.pal.card),
                       lambda: render_card(w, h, self.pal, radius=self.radius,
                                           inset=self.inset, band=self.band,
                                           bg=self.pal.bg))
        self._canvas.itemconfigure(self._item, image=photo)
        self._canvas.image = photo


# ---------------------------------------------------------------- 输入框 / 文本域

def render_well(w, h, pal, *, radius=12, bg=None):
    """输入框的圆角细边，中间留白给真正的输入控件。"""
    w, h = max(2, int(w)), max(2, int(h))
    W, H = w * SS, h * SS
    canvas = Image.new("RGBA", (W, H), _fade(bg or pal.card, 255))
    # 细边本身是渐变的：上面很淡，下面暖一点，像玻璃边缘受光
    canvas.alpha_composite(_ring(W, H, 0, max(1, int(1.4 * SS)), radius * SS,
                                 _fade(pal.line_strong, 90), _fade(pal.line_strong, 210)))
    return canvas.resize((w, h), _RESAMPLE)


class GlassWell(tk.Frame):
    """给输入控件套一层圆角细边。控件直接 pack 到这个 Frame 里。"""

    def __init__(self, parent, pal, *, radius=12):
        super().__init__(parent, bg=pal.card, bd=0, highlightthickness=0)
        self.pal = pal
        self.radius = _px(radius)
        self._key = None
        if not PIL_OK:
            self.configure(highlightbackground=pal.line_strong, highlightthickness=1)
            return
        self._canvas = tk.Canvas(self, bg=pal.card, bd=0, highlightthickness=0, takefocus=0)
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._item = self._canvas.create_image(0, 0, anchor="nw")
        tk.Frame.bind(self, "<Configure>", self._draw)

    def _draw(self, event=None):
        w = event.width if event is not None else self.winfo_width()
        h = event.height if event is not None else self.winfo_height()
        if self._key == (w, h) or w < 8 or h < 8:
            return
        self._key = (w, h)
        photo = _photo(self, ("well", w, h, self.radius),
                       lambda: render_well(w, h, self.pal, radius=self.radius,
                                           bg=self.pal.card))
        self._canvas.itemconfigure(self._item, image=photo)
        self._canvas.image = photo


class GlassText(tk.Frame):
    """圆角外壳 + tk.Text。外观方法都转给内部的 Text，用法和 tk.Text 一致。"""

    _FORWARD = frozenset((
        "get", "insert", "delete", "see", "index", "yview", "yview_scroll",
        "yview_moveto", "tag_add", "tag_remove", "tag_configure", "tag_config",
        "mark_set", "mark_gravity", "search", "edit_undo", "edit_redo",
        "window_create", "image_create", "peer_create",
    ))

    def __init__(self, parent, pal, *, height=5, width=36, read_only=False, radius=12,
                 size=10):
        super().__init__(parent, bg=pal.card, bd=0, highlightthickness=0)
        self.well = GlassWell(self, pal, radius=radius)
        self.well.pack(fill="both", expand=True)
        self.text = tk.Text(self.well, undo=not read_only, wrap="word", height=height,
                            width=width, bg=pal.card, fg=pal.ink, insertbackground=pal.ink,
                            relief="flat", bd=0, highlightthickness=0, font=(pal.font, size),
                            padx=2, pady=2, spacing1=_px(2), spacing3=_px(3),
                            selectbackground=pal.orange_bg, selectforeground=pal.ink)
        self.text.pack(fill="both", expand=True, padx=_px(11), pady=_px(7))
        if read_only:
            self.text.configure(state="disabled")

    def __getattr__(self, name):
        if name in GlassText._FORWARD:
            return getattr(object.__getattribute__(self, "text"), name)
        raise AttributeError(name)

    def configure(self, cnf=None, **kw):
        # 外观参数交给外壳，其余（state / yscrollcommand / font 等）交给 Text
        if cnf:
            self.text.configure(cnf)
        if kw:
            self.text.configure(**kw)
        return None

    config = configure

    def cget(self, key):
        return self.text.cget(key)

    def bind(self, sequence=None, func=None, add=None):
        return self.text.bind(sequence, func, add)


class EntryField(GlassWell):
    """单行输入框：圆角外壳 + 无边框 ttk.Entry，接口和 ttk.Entry 一样能用。"""

    def __init__(self, parent, pal, var, *, show="", width=20, radius=12):
        super().__init__(parent, pal, radius=radius)
        self.entry = ttk.Entry(self, textvariable=var, show=show, width=width,
                               style="Well.TEntry")
        self.entry.pack(fill="both", expand=True, padx=_px(11), pady=_px(6))

    def state(self, spec=None):
        return self.entry.state() if spec is None else self.entry.state(spec)

    def bind(self, sequence=None, func=None, add=None):
        entry = self.__dict__.get("entry")
        if entry is None:
            return tk.Frame.bind(self, sequence, func, add)
        return entry.bind(sequence, func, add)

    def get(self):
        return self.entry.get()

    def insert(self, index, text):
        return self.entry.insert(index, text)

    def cget(self, key):
        return self.entry.cget(key)


# ---------------------------------------------------------------- 按钮

def render_button(w, h, pal, *, variant, state, text, size=10, bold=False,
                  align="center", padx=18, caret=False, bg=None):
    """胶囊按钮。variant: primary / glass / nav / navsel。画布底色 = 按钮所在底色。"""
    w, h = max(2, int(w)), max(2, int(h))
    W, H = w * SS, h * SS
    canvas = Image.new("RGBA", (W, H), _fade(bg or pal.bg, 255))
    fill_top, fill_bottom, rim_top, rim_bottom, ink = None, None, None, None, pal.ink
    shadow = None
    if variant == "primary":
        ink = "#FFFFFF"
        if state == "disabled":
            fill_top, fill_bottom = "#E7DCD0", "#DCCFBF"
            ink = "#98907F"
            rim_top, rim_bottom = "#F6F1E9", "#E0D3C2"
        else:
            press = state == "press"
            fill_top = pal.orange_light
            fill_bottom = "#A24A21" if state == "hover" else pal.orange
            if press:
                fill_top, fill_bottom = "#8A3814", "#7C3013"
            rim_top, rim_bottom = "#FFFFFF", "#D79A6E"
            shadow = _fade("#8B4A28", 34)
        bold = True
    elif variant == "navsel":
        ink = pal.orange
        fill_top, fill_bottom = "#FCEFE3", "#F7DDC7"
        rim_top, rim_bottom = "#FFFFFF", "#E8B994"
        bold = True
    elif variant == "nav":
        ink = pal.muted
        if state == "hover":
            fill_top, fill_bottom = "#FFFFFF", "#FBF3E9"
            rim_top, rim_bottom = "#FFFFFF", pal.line_strong
            shadow = _fade("#6B4526", 16)
        elif state == "press":
            fill_top, fill_bottom = "#FBF3E9", "#F6EBDE"
            rim_top, rim_bottom = "#F1E5D5", pal.line_strong
        else:
            fill_top = fill_bottom = None          # 导航未选中：只画字
    else:                                          # glass
        if state == "hover":
            fill_top, fill_bottom = "#FFFFFF", "#FAF1E6"
            rim_top, rim_bottom = "#FFFFFF", "#C8AB8C"
            shadow = _fade("#6B4526", 24)
        elif state == "press":
            fill_top, fill_bottom = "#FBF3E9", "#F5E9D9"
            rim_top, rim_bottom = "#F1E5D5", "#C8AB8C"
        else:
            fill_top, fill_bottom = "#FFFFFF", "#FBF3E7"
            rim_top, rim_bottom = "#FFFFFF", "#D2B394"
            shadow = _fade("#6B4526", 24)
        if state == "disabled":
            ink = "#A69C8C"

    radius = (H - 1) // 2
    if fill_top is not None:
        if shadow:
            drop = min(radius, (H - 1 - 2 * SS) // 2)
            canvas.alpha_composite(_soft_shape(
                W, H, lambda d: d.rounded_rectangle(
                    (SS, SS * 2, W - 1 - SS, H - 1), radius=drop, fill=255),
                shadow, SS))
        face = _gradient(W, H, _fade(fill_top, 255), _fade(fill_bottom, 255))
        _apply_alpha(face, _mask(W, H, SS, radius))
        canvas.alpha_composite(face)
        canvas.alpha_composite(_ring(W, H, SS, max(1, int(1.4 * SS)), radius,
                                     _fade(rim_top, 225), _fade(rim_bottom, 215)))

        # 顶部内高光：一条很淡的白线，玻璃感主要来自这里
        def gloss(mask_draw):
            mask_draw.rounded_rectangle((SS, SS, W - 1 - SS, H - 1 - SS),
                                        radius=radius, fill=255)
            mask_draw.rectangle((0, H // 2, W, H), fill=0)   # 只要上半部分

        canvas.alpha_composite(_soft_shape(
            W, H, gloss, _fade("#FFFFFF", 70 if variant != "primary" else 46), SS))

    if text:
        px = int(round(size * 4 / 3)) * SS
        font = _font(px, bold)
        if align == "left":
            x, anchor = padx * SS, "lm"
            room = W - padx * SS - int(padx * 0.75 * SS)
        else:
            x, anchor = W / 2, "mm"
            room = W - 6 * SS
        # 字比按钮宽时缩一点字号：宁可小一点，别把标签切掉（中文加粗尤其容易估短）
        while px > 9 * SS and font.getlength(text) > room:
            px -= SS
            font = _font(max(px, 9 * SS), bold)
        y = H / 2
        if variant == "primary" and state != "disabled":
            shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(shadow_layer).text((x, y + SS), text, font=font,
                                              fill=(0, 0, 0, 46), anchor=anchor)
            canvas.alpha_composite(shadow_layer)
        ImageDraw.Draw(canvas).text((x, y), text, font=font, fill=ink, anchor=anchor)
    if caret:
        cx, cy = W - int(padx * 0.75 * SS) - 3 * SS, H / 2 + SS
        draw = ImageDraw.Draw(canvas)
        draw.line([(cx - 4 * SS, cy - 2 * SS), (cx, cy + 2 * SS), (cx + 4 * SS, cy - 2 * SS)],
                  fill=pal.muted if state == "normal" else ink,
                  width=max(1, int(1.7 * SS)), joint="curve")
    return canvas.resize((w, h), _RESAMPLE)


class PillButton(tk.Canvas):
    """胶囊按钮。variant: primary / glass / nav（导航项）。"""

    def __init__(self, parent, text="", command=None, *, pal, variant="glass",
                 size=10, bold=None, padx=18, height=36, width=None, align="center",
                 bg=None, caret=False):
        self.pal = pal
        self._text = text
        self._command = command
        self._variant = variant
        self._size = size
        self._bold = (variant in ("primary", "navsel")) if bold is None else bold
        self._padx = padx
        self._align = align
        self._caret = caret
        self._enabled = True
        self._hover = False
        self._pressed = False
        self._selected = False
        self._key = None
        self._bg = bg or _parent_bg(parent, pal.bg)
        self._font = tkfont.Font(family=pal.font, size=size,
                                weight="bold" if self._bold else "normal")
        wanted = int(width) if width else self._font.measure(text) + 2 * padx
        super().__init__(parent, width=max(24, wanted), height=height, bg=self._bg,
                         bd=0, highlightthickness=0, takefocus=1)
        self._item = self.create_image(0, 0, anchor="nw")
        self._image = None
        self.configure(cursor="hand2")
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<Return>", lambda _event: self._invoke())
        self.bind("<space>", lambda _event: self._invoke())

    # -- 状态 ---------------------------------------------------------
    def _state(self):
        if not self._enabled:
            return "disabled"
        if self._pressed:
            return "press"
        if self._hover:
            return "hover"
        return "normal"

    def _shape(self):
        if self._variant == "nav" and self._selected:
            return "navsel"
        return self._variant

    def _draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 8 or h < 6:
            return
        shape = self._shape()
        key = (w, h, shape, self._state(), self._text, self._size, self._bold,
               self._align, self._padx, self._bg, self._caret)
        if key == self._key:
            return
        self._key = key
        photo = _photo(self, ("btn",) + key,
                       lambda: render_button(w, h, self.pal, variant=shape,
                                             state=self._state(), text=self._text,
                                             size=self._size, bold=self._bold,
                                             align=self._align, padx=self._padx,
                                             caret=self._caret, bg=self._bg))
        self._image = photo
        self.itemconfigure(self._item, image=photo)

    # -- 事件 ---------------------------------------------------------
    def _on_enter(self, _event=None):
        if self._enabled:
            self._hover = True
            self._draw()

    def _on_leave(self, _event=None):
        self._hover = False
        self._pressed = False
        self._draw()

    def _on_press(self, _event=None):
        if not self._enabled:
            return
        self._pressed = True
        self._draw()

    def _on_release(self, event=None):
        if not self._enabled or not self._pressed:
            return
        self._pressed = False
        inside = (event is None or (0 <= event.x <= self.winfo_width()
                                    and 0 <= event.y <= self.winfo_height()))
        self._draw()
        if inside:
            self._invoke()

    def _invoke(self):
        if self._enabled and callable(self._command):
            self._command()

    # -- 与 ttk 兼容的接口 --------------------------------------------
    def state(self, spec=None):
        if spec is None:
            return () if self._enabled else ("disabled",)
        for item in (spec if isinstance(spec, (list, tuple)) else [spec]):
            if item == "disabled":
                self._enabled = False
            elif item == "!disabled":
                self._enabled = True
        if not self._enabled:
            self._hover = self._pressed = False
        self._draw()
        return ()

    @property
    def selected(self):
        return self._selected

    @selected.setter
    def selected(self, value):
        self._selected = bool(value)
        self._draw()

    def set_text(self, text):
        self._text = text or ""
        if self._align == "center":
            self.configure(width=max(24, self._font.measure(self._text) + 2 * self._padx))
        self._draw()

    def set_variant(self, variant):
        self._variant = variant
        self._bold = variant in ("primary", "navsel")
        self._font.configure(weight="bold" if self._bold else "normal")
        self._draw()

    def set_selected(self, value):
        self._selected = bool(value)
        self._draw()

    def configure(self, cnf=None, **kw):
        if cnf:
            kw = dict(cnf, **kw)
        for key, value in kw.items():
            if key == "text":
                self.set_text(value)
            elif key == "command":
                self._command = value
            elif key == "state":
                if value in ("normal", "active"):
                    self.state(["!disabled"])
                else:
                    self.state(["disabled"])
            elif key == "style":
                style = str(value)
                self.set_variant("primary" if style.startswith("Accent") else "glass")
            elif key == "bg":
                self._bg = value
                self._key = None
                super().configure(bg=value)
                self._draw()
            elif key == "cursor":
                super().configure(cursor=value)
            else:
                super().configure(**{key: value})
        return None

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        return super().cget(key)


class PillSelect(PillButton):
    """圆角下拉选择：收起时是胶囊，点开后是一张玻璃卡片里的一列胶囊选项。"""

    def __init__(self, parent, pal, *, options=(), variable=None, command=None,
                 height=36, placeholder="请选择", **kw):
        self._choices = list(options)
        self._var = variable if variable is not None else tk.StringVar()
        self._on_choose = command
        self._placeholder = placeholder
        self._popup = None
        super().__init__(parent, self._display(), self._open, pal=pal, variant="glass",
                         align="left", padx=16, height=height, caret=True, **kw)
        self._var.trace_add("write", self._on_var)
        self.bind("<Destroy>", lambda _event: self._close(), add="+")

    # -- 值 -----------------------------------------------------------
    def _display(self):
        return self._var.get() or self._placeholder

    def _on_var(self, *_args):
        self.set_text(self._display())

    def get(self):
        return self._var.get()

    def set(self, value):
        self._var.set(value or "")

    def set_options(self, options):
        self._choices = list(options)

    def set_placeholder(self, text):
        self._placeholder = text
        self.set_text(self._display())

    # -- 弹层 ---------------------------------------------------------
    def _open(self):
        if self._popup is not None:
            self._close()
            return
        if not self._choices or not self._enabled:
            return
        # 弹层里每一行都要跟着窗口缩放：写死 34/14 时，全屏下整排比外层胶囊还小，
        # 而且窗口高度不够会把最后一项压成几像素（看起来就是「选项消失了」）
        row_height = max(24, _px(34))
        row_pad = max(8, _px(14))
        row_size = max(8, _px(10))
        top = tk.Toplevel(self, bg=self.pal.bg, bd=0, highlightthickness=0)
        top.overrideredirect(True)
        # 先藏起来量尺寸：overrideredirect 的窗口一旦被 map 就会先出现在左上角，
        # 之后再 geometry 未必挪得动（看起来就是「选项跑到左上角」）
        top.withdraw()
        card = GlassFrame(top, self.pal, padding=4, radius=16, inset=4, band=8)
        card.pack(fill="both", expand=True)
        for option in self._choices:
            row = PillButton(card.inner, option,
                             lambda value=option: self._choose(value),
                             pal=self.pal, variant="nav", size=row_size,
                             height=row_height, align="left", padx=row_pad,
                             bg=self.pal.card)
            row.pack(fill="x")
            if option == self._var.get():
                row.set_selected(True)
        # 藏着的窗口也会算布局，所以这里能量到内容真正需要多高
        top.update_idletasks()
        width = max(self.winfo_width(), _px(176))
        height = max(card.winfo_reqheight(),
                     _px(16) + len(self._choices) * row_height)
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 5
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        x = min(max(4, x), max(4, screen_w - width - 4))
        if y + height > screen_h - 8:
            y = max(8, self.winfo_rooty() - height - 5)
        top.geometry(f"{width}x{height}+{x}+{y}")
        top.attributes("-topmost", True)
        top.deiconify()                 # 尺寸和位置都定好了，再一次性显示出来
        top.update_idletasks()
        top.bind("<Button-1>", self._maybe_close)
        top.bind("<Escape>", lambda _event: self._close())
        try:
            top.grab_set()
        except tk.TclError:
            pass
        self._popup = top

    def _maybe_close(self, event):
        if event.widget is self._popup:
            self._close()

    def _choose(self, value):
        self._close()
        self._var.set(value)
        if callable(self._on_choose):
            self._on_choose()

    def _close(self):
        popup, self._popup = self._popup, None
        if popup is None:
            return
        try:
            popup.grab_release()
        except tk.TclError:
            pass
        try:
            popup.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------- 勾选框

def render_check(w, h, pal, *, checked, state="normal", text="", size=10, bg=None,
                 box=20, hover=False):
    """圆角勾选框：一个明显的实心圆角方块 + 文字，替代 tk 那圈发虚的小方框。"""
    w, h = max(2, int(w)), max(2, int(h))
    W, H = w * SS, h * SS
    canvas = Image.new("RGBA", (W, H), _fade(bg or pal.card, 255))
    disabled = state == "disabled"
    if hover and not disabled:
        # 悬停时垫一层很淡的暖色胶囊，明确「这一整行可以点」
        canvas.alpha_composite(_soft_shape(
            W, H, lambda d: d.rounded_rectangle(
                (0, SS, W - 1, H - 1), radius=(H - 1) // 2, fill=255),
            _fade("#F7E7D6", 210), 0))
    box = int(round(box * SS))
    top = (H - box) // 2
    left = _px(3) * SS
    box_rect = (left, top, left + box, top + box)
    radius = max(3, int(box * 0.32))
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box_rect, radius=radius, fill=255)
    # 方块底下垫一点阴影，勾选框才不会和白色卡片糊在一起
    if not disabled:
        canvas.alpha_composite(_soft_shape(
            W, H, lambda d: d.rounded_rectangle(
                (left, top + SS, left + box, top + box + SS), radius=radius, fill=255),
            _fade("#8A5B33", 46), SS))
    if checked:
        face = _gradient(W, H, _fade("#C25E2A" if not disabled else "#D8C6B4", 255),
                         _fade(pal.orange if not disabled else "#C4B3A0", 255))
    else:
        face = _gradient(W, H, _fade("#FDF6EC", 255), _fade("#F8EDDD", 255))
    _apply_alpha(face, mask)
    canvas.alpha_composite(face)
    # 描边：未选中也要看得清，所以用偏深的暖棕；选中用深橙
    rim_low = "#9C421E" if checked else "#B08A5F"
    if disabled:
        rim_low = "#D9C9B6"
    canvas.alpha_composite(_rounded_ring(W, H, box_rect, radius, max(2, int(1.7 * SS)),
                                         _fade("#FFFFFF", 230), _fade(rim_low, 250)))
    if checked:
        line = ImageDraw.Draw(canvas)
        line.line([(left + 0.26 * box, top + 0.52 * box),
                   (left + 0.43 * box, top + 0.70 * box),
                   (left + 0.76 * box, top + 0.30 * box)],
                  fill=(255, 255, 255, 255), width=max(2, int(2.4 * SS)), joint="curve")
    if text:
        font = _font(int(round(size * 4 / 3)) * SS, False)
        ink = _fade("#9A9184" if disabled else pal.ink, 255)
        ImageDraw.Draw(canvas).text((left + box + _px(9) * SS, H / 2), text,
                                    font=font, fill=ink, anchor="lm")
    return canvas.resize((w, h), _RESAMPLE)


def _rounded_ring(w, h, rect, radius, width, top, bottom):
    """按任意矩形导出圆角描边环（勾选框尺寸不是整张画布）。"""
    outer = Image.new("L", (w, h), 0)
    inner = Image.new("L", (w, h), 0)
    ImageDraw.Draw(outer).rounded_rectangle(rect, radius=radius, fill=255)
    inner_rect = (rect[0] + width, rect[1] + width, rect[2] - width, rect[3] - width)
    ImageDraw.Draw(inner).rounded_rectangle(inner_rect, radius=max(0, radius - width),
                                            fill=255)
    ring = ImageChops.subtract(outer, inner)
    line = _gradient(w, h, top, bottom)
    return _apply_alpha(line, ring)


class PillCheck(tk.Canvas):
    """圆角勾选框。绑定 BooleanVar，点一下切一次；整行都能点，悬停有反馈。"""

    def __init__(self, parent, pal, *, text="", variable, size=10, height=30, box=20):
        self.pal = pal
        self._text = text
        self._size = size
        self._box = box
        self._enabled = True
        self._hover = False
        self._key = None
        self._var = variable
        self._font = tkfont.Font(family=pal.font, size=size)
        wanted = self._wanted()
        super().__init__(parent, width=wanted, height=height,
                         bg=_parent_bg(parent, pal.card), bd=0, highlightthickness=0,
                         takefocus=1)
        self._item = self.create_image(0, 0, anchor="nw")
        self._image = None
        self.configure(cursor="hand2")
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Configure>", lambda _event: self._draw())
        self._var.trace_add("write", lambda *_args: self._draw())

    def _wanted(self):
        return int(self._box * SCALE + _px(12) + self._font.measure(self._text) + _px(6))

    def _draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 8 or h < 6:
            return
        checked = bool(self._var.get())
        bg = str(self.cget("bg"))
        key = (w, h, checked, self._enabled, self._text, bg, self._hover, self._box)
        if key == self._key:
            return
        self._key = key
        photo = _photo(self, ("check",) + key,
                       lambda: render_check(w, h, self.pal, checked=checked,
                                            state="normal" if self._enabled else "disabled",
                                            text=self._text, size=self._size, bg=bg,
                                            box=self._box, hover=self._hover))
        self._image = photo
        self.itemconfigure(self._item, image=photo)

    def _on_enter(self, _event=None):
        if self._enabled:
            self._hover = True
            self._draw()

    def _on_leave(self, _event=None):
        self._hover = False
        self._draw()

    def _toggle(self, _event=None):
        if not self._enabled:
            return
        self._var.set(not self._var.get())

    def state(self, spec=None):
        if spec is None:
            return () if self._enabled else ("disabled",)
        for item in (spec if isinstance(spec, (list, tuple)) else [spec]):
            if item == "disabled":
                self._enabled = False
            elif item == "!disabled":
                self._enabled = True
        if not self._enabled:
            self._hover = False
        self._key = None
        self.configure(cursor="hand2" if self._enabled else "")
        self._draw()
        return ()

    def configure(self, cnf=None, **kw):
        if cnf:
            kw = dict(cnf, **kw)
        for key, value in kw.items():
            if key == "state":
                self.state([value] if value == "disabled" else ["!disabled"])
            elif key == "text":
                self._text = value or ""
                self.configure(width=self._wanted())
            elif key == "bg":
                super().configure(bg=value)
                self._key = None
                self._draw()
            else:
                super().configure(**{key: value})
        return None

    config = configure
