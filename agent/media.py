# -*- coding: utf-8 -*-
"""媒体解析：把聊天里的图片/表情解密成本地文件，供模型识别。

- 图片消息：msg/attach/<会话摘要>/<年月>/Img/<图片摘要>.dat，兼容微信的不同图片档位
- 表情消息：business/emoticon/Persist/<2位>/<md5>，AES-128-CBC(key=IV)
"""

from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from io import BytesIO

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
KEYS_FILE = os.environ.get("WXBOT_MEDIA_KEYS") or os.path.join(ROOT, "keys.json")
CONFIG = os.environ.get("WXBOT_CONFIG") or os.path.join(ROOT, "config.json")
CACHE = os.environ.get("WXBOT_MEDIA_CACHE") or os.path.join(ROOT, "media_cache")
os.makedirs(CACHE, exist_ok=True)

MAGIC_EXT = [
    (b"\x89PNG", ".png"),
    (b"GIF8", ".gif"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"wxgf", ".wxgf"),
    (b"RIFF", ".webp"),
]

# .wxgf 是微信 4.x 的 H.265 容器（动态表情常见），只有它需要 ffmpeg 抽首帧；
# PNG/JPEG/GIF/WEBP 的图片和表情直接解密就能用，不需要 ffmpeg。
# 这里不写死任何本机路径：按「别人也能装上」的顺序找，找不到就跳过这一步。
_FFMPEG_CACHE: str | None = None
_FFMPEG_WARNED = False


def _log():
    try:
        from logutil import get_logger
        return get_logger()
    except Exception:
        return None


def _cfg_ffmpeg(config_file: str) -> str:
    """读取一份配置的 media.ffmpeg。"""
    try:
        if os.path.exists(config_file):
            with open(config_file, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
            return str(((cfg.get("media") or {}).get("ffmpeg") or "")).strip()
    except Exception:
        pass
    return ""


def find_ffmpeg(refresh: bool = False) -> str:
    """找一个能用的 ffmpeg，返回可执行文件路径；找不到返回空串。

    顺序（谁先命中用谁）：

    1. 环境变量 ``WXBOT_FFMPEG``
    2. ``config.json`` 的 ``media.ffmpeg``（先会话配置，再项目配置）
    3. 项目内 ``tools/ffmpeg/``（把官方解压包丢这里，跟着仓库走）
    4. ``PATH`` 里的 ``ffmpeg``（winget / choco / 手工加 PATH）
    5. ``pip install imageio-ffmpeg`` 自带的静态版（``wechatauto-replica``
       会顺带装上，多数人装完依赖就有）

    都找不到就返回空串：.wxgf 那类表情退化成文字占位，别的格式不受影响。
    """
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE is not None and not refresh:
        return _FFMPEG_CACHE

    cands = []
    env = (os.environ.get("WXBOT_FFMPEG") or "").strip()
    if env:
        cands.append(env)
    cands.append(_cfg_ffmpeg(CONFIG))
    project_config = os.path.join(ROOT, "config.json")
    if project_config != CONFIG:
        cands.append(_cfg_ffmpeg(project_config))
    for name in ("ffmpeg.exe", "bin/ffmpeg.exe", "ffmpeg", "bin/ffmpeg"):
        cands.append(os.path.join(ROOT, "tools", "ffmpeg", *name.split("/")))
    found = ""
    for c in cands:
        if c and os.path.isfile(c):
            found = c
            break
    if not found:
        found = shutil.which("ffmpeg") or ""
    if not found:
        try:
            import imageio_ffmpeg
            p = imageio_ffmpeg.get_ffmpeg_exe()
            if p and os.path.isfile(p):
                found = p
        except Exception:
            pass
    _FFMPEG_CACHE = found
    return found


def ffmpeg_exe(refresh: bool = False) -> str:
    """取 ffmpeg 路径；第一次找不到时提示一次怎么装，之后静默跳过。"""
    global _FFMPEG_WARNED
    exe = find_ffmpeg(refresh=refresh)
    if not exe and not _FFMPEG_WARNED:
        _FFMPEG_WARNED = True
        log = _log()
        if log:
            log.warning("没找到 ffmpeg：.wxgf 动态表情/图片只能跳过（PNG/JPEG/GIF 不受影响）。"
                        "装法见 README「可选：ffmpeg」，或 pip install imageio-ffmpeg，"
                        "或把 ffmpeg.exe 放进 tools\\ffmpeg\\")
    return exe


def load_keys() -> dict:
    if os.path.exists(KEYS_FILE):
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _configured_account() -> str | None:
    cfg = {}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception:
            pass
    return os.environ.get("WXBOT_ACCOUNT") or cfg.get("account")


def account_dir(account: str | None = None) -> str | None:
    bound = os.environ.get("WXBOT_ACCOUNT")
    if bound and account and bound != account:
        return None
    acc = account or _configured_account()
    if not acc:
        return None
    selected = os.environ.get("WXBOT_ACCOUNT_DIR")
    if selected:
        return selected if os.path.basename(os.path.normpath(selected)) == acc else None
    for base in (r"D:\xwechat_files", r"C:\xwechat_files",
                 os.path.expanduser(r"~\Documents\xwechat_files")):
        p = os.path.join(base, acc)
        if os.path.isdir(p):
            return p
    return None


def sniff(data: bytes) -> str:
    for sig, ext in MAGIC_EXT:
        if data.startswith(sig):
            return ext
    return ""


def strip_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def wxgf_to_png(data: bytes, out: str) -> bool:
    """把 .wxgf（H.265 容器）的首帧抽成 PNG；这台机器没有 ffmpeg 就返回 False。

    缺 ffmpeg 只影响 .wxgf 这一类（微信 4.x 的动态表情/图片），
    调用方会退回文字占位，不会抛错。
    """
    exe = ffmpeg_exe()
    if not exe:
        return False
    idx = data.find(b"\x00\x00\x00\x01\x40\x01")
    if idx < 0:
        idx = data.find(b"\x00\x00\x00\x01")
    if idx < 0:
        return False
    with tempfile.NamedTemporaryFile(suffix=".h265", delete=False) as tf:
        tf.write(data[idx:])
        tmp = tf.name
    try:
        r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", tmp,
                            "-frames:v", "1", "-f", "image2", out],
                           capture_output=True, timeout=60)
        return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0
    except Exception:
        return False
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


# ---------------------------------------------------------------- 表情
def resolve_emoticon(md5: str, *, account: str | None = None) -> str | None:
    """根据 md5 找表情文件并解密，返回本地路径。"""
    keys = load_keys()
    info = keys.get("emoticon") or {}
    key_hex = info.get("key")
    data_dir = account_dir(account)
    if not key_hex or not data_dir:
        return None
    from Crypto.Cipher import AES
    key = bytes.fromhex(key_hex)
    emo = os.path.join(data_dir, "business", "emoticon")
    for sub in ("Persist", "PersistStore"):
        for p in glob.glob(os.path.join(emo, sub, "*", md5)):
            try:
                with open(p, "rb") as f:
                    raw = f.read()
                n = len(raw) - (len(raw) % 16)
                dec = strip_pkcs7(AES.new(key, AES.MODE_CBC, key).decrypt(raw[:n]))
            except Exception:
                continue
            ext = sniff(dec)
            if not ext:
                continue
            out = os.path.join(CACHE, "%s%s" % (md5, ext))
            if ext == ".wxgf":
                out = os.path.join(CACHE, md5 + ".png")
                if wxgf_to_png(dec, out):
                    return out
                continue
            with open(out, "wb") as f:
                f.write(dec)
            return out
    return None


# ---------------------------------------------------------------- 聊天图片
def resolve_image(md5: str, create_time: int | None = None, *, chat: str = "",
                  local_id=None, shard: str | None = None,
                  account: str | None = None) -> str | None:
    """只读已绑定账号的图片记录和本机已落盘的副本。"""
    if not md5 and not (chat and local_id is not None):
        return None
    try:
        from wx_identity import open_bound_db
        db = open_bound_db(account or _configured_account())
        from wechatauto.media import MediaDownloader
        from PIL import Image
    except Exception:
        return None

    # 附件名的摘要优先取自 packed_info。实时采集的正文可能只有「[图片]」。
    if chat and local_id is not None:
        try:
            row = db.get_message_row(chat, int(local_id), local_type=3,
                                     shard=shard or None) or {}
            packed = row.get("packed_info")
            if isinstance(packed, bytes):
                hit = re.search(rb"[0-9a-fA-F]{32}", packed)
                if hit:
                    md5 = hit.group().decode("ascii").lower()
            if not md5:
                md5 = md5_from_content(row.get("content")) or ""
        except Exception:
            pass
    if not re.fullmatch(r"[0-9a-f]{32}", md5 or ""):
        return None

    directory = os.path.join(db.account_dir, "msg", "attach")
    if chat:
        directory = os.path.join(directory, hashlib.md5(chat.encode()).hexdigest())
    wanted = (md5 + "_h.dat", md5 + ".dat", md5 + "_t.dat")
    paths = {name: [] for name in wanted}
    for folder, _, files in os.walk(directory):
        for name in wanted:
            if name in files:
                paths[name].append(os.path.join(folder, name))
    if not any(paths.values()):
        return None
    downloader = MediaDownloader(db)
    for files in paths.values():
        for path in files:
            try:
                data = downloader.decrypt_image(path)
                ext = sniff(data or b"")
                if not ext:
                    continue
                out = os.path.join(CACHE, md5 + (".png" if ext == ".wxgf" else ext))
                fd, staged = tempfile.mkstemp(dir=CACHE, suffix=os.path.splitext(out)[1])
                os.close(fd)
                try:
                    if ext == ".wxgf":
                        if not wxgf_to_png(data, staged):
                            continue
                    else:
                        with Image.open(BytesIO(data)) as image:
                            image.load()
                        with open(staged, "wb") as file:
                            file.write(data)
                    os.replace(staged, out)
                finally:
                    try:
                        os.unlink(staged)
                    except FileNotFoundError:
                        pass
                return out
            except Exception:
                continue
    return None


# ---------------------------------------------------------------- 通用入口
def md5_from_content(content: str | bytes | None) -> str | None:
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="ignore")
    m = re.search(r'md5="([0-9a-fA-F]{32})"', content or "")
    if m:
        return m.group(1).lower()
    m = re.search(r'\b([0-9a-fA-F]{32})\b', content or "")
    return m.group(1).lower() if m else None


def resolve_for_message(msg_type: str, content: str, *, chat: str = "",
                        local_id=None, shard: str | None = None,
                        account: str | None = None) -> str | None:
    """按消息类型解析出本地图片路径。"""
    md5 = md5_from_content(content)
    try:
        if msg_type in ("动画表情", "emoji") and md5:
            return resolve_emoticon(md5, account=account)
        if msg_type in ("图片", "image"):
            return resolve_image(md5 or "", chat=chat, local_id=local_id,
                                 shard=shard, account=account)
    except Exception:
        return None
    return None


def to_data_uri(path: str, max_bytes: int = 4 * 1024 * 1024) -> str | None:
    """把图片转成 data URI，供模型视觉输入。"""
    try:
        from PIL import Image
        im = Image.open(path)
        if im.mode in ("RGBA", "P", "LA"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        # 太大就缩小
        w, h = im.size
        max_side = 1024
        if max(w, h) > max_side:
            k = max_side / float(max(w, h))
            im = im.resize((int(w * k), int(h * k)))
        # 每次用独立临时文件：固定 _tmp.jpg 时两个会话并发会互相覆盖，
        # 把别人的图喂给模型（max_parallel_chats 默认 2）。
        fd, buf = tempfile.mkstemp(prefix="view_", suffix=".jpg", dir=CACHE)
        os.close(fd)
        try:
            im.save(buf, "JPEG", quality=86)
            with open(buf, "rb") as f:
                data = f.read()
        finally:
            try:
                os.unlink(buf)
            except OSError:
                pass
        if len(data) > max_bytes:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(data).decode()
    except Exception:
        return None


def to_vision_uri(path: str, max_side: int = 1600, quality: int = 90,
                  max_bytes: int = 4 * 1024 * 1024) -> tuple[str | None, int]:
    """把一张图转成 data URI，**给「看图回答问题」这类多模态请求用**。

    和 ``to_data_uri`` 的区别（那边是给「25 字情绪描述」用的，别混用）：

    * 长边上限 1600 而不是 1024：课表/截图/聊天记录这类图，缩到 1024 字就糊了
    * 小图不放大：表情包往往只有 240×240，放大只会白烧 token
    * 动图显式只取首帧：PIL 对 GIF 默认给第一帧，但多帧 WEBP/APNG 会拿到别的
    * 不做「情绪化」压缩，内容由模型自己读

    返回 ``(uri, base64 字节数)``；读不出来返回 ``(None, 0)``，调用方自己降级。
    """
    try:
        from PIL import Image
        with Image.open(path) as source:
            source.seek(0)                  # 动图只要第一帧
            if source.mode in ("RGBA", "P", "LA"):
                rgba = source.convert("RGBA")
                im = Image.new("RGB", rgba.size, (255, 255, 255))
                im.paste(rgba, mask=rgba.getchannel("A"))
            else:
                im = source.convert("RGB")
        w, h = im.size
        if max(w, h) > max_side:
            k = max_side / float(max(w, h))
            im = im.resize((max(1, int(w * k)), max(1, int(h * k))))
        with BytesIO() as buf:
            im.save(buf, "JPEG", quality=quality)
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        if not encoded or len(encoded) > max_bytes:
            return None, 0
        return "data:image/jpeg;base64," + encoded, len(encoded)
    except Exception:
        return None, 0


def resolve_any(md5: str, kind: str = "image", *, chat: str = "", local_id=None,
                shard: str | None = None, account: str | None = None) -> str | None:
    """按原消息找图，读不到原消息时查当前账号的媒体缓存。

    代传话收集时会把路径存下来，但 media_cache 可能被清；封口时按 md5
    再解一次，图还在微信库里的就能救回来。
    """
    md5 = (md5 or "").strip().lower()
    bound = os.environ.get("WXBOT_ACCOUNT")
    if bound and account and bound != account:
        return None
    has_message = bool(chat and local_id is not None)
    if has_message:
        path = resolve_for_message(kind, md5, chat=chat, local_id=local_id,
                                   shard=shard, account=account)
        if path:
            return path
    if re.fullmatch(r"[0-9a-f]{32}", md5):
        for p in glob.glob(os.path.join(CACHE, md5 + ".*")):
            try:
                if os.path.isfile(p) and not os.path.islink(p) and os.path.getsize(p) > 0:
                    return p
            except OSError:
                continue
    if has_message:
        return None
    return resolve_for_message(kind, md5, chat=chat, local_id=local_id,
                               shard=shard, account=account)


if __name__ == "__main__":
    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("account_dir:", account_dir())
    print("keys:", list(load_keys().keys()))
    if len(sys.argv) > 2:
        p = resolve_for_message(sys.argv[1], sys.argv[2])
        print("resolved:", p)
        if p:
            print("datauri len:", len(to_data_uri(p) or ""))
