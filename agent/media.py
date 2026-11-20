# -*- coding: utf-8 -*-
"""媒体解析：把聊天里的图片/表情解密成本地文件，供模型识别。

- 图片消息：msg/attach/<md5>/<年月>/Img/<md5>.dat，V2 容器（AES-128-ECB + XOR）
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

FFMPEG = r"D:\oopz\ffmpeg.exe"


def load_keys() -> dict:
    if os.path.exists(KEYS_FILE):
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def account_dir() -> str | None:
    cfg = {}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception:
            pass
    acc = cfg.get("account")
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
    if not os.path.exists(FFMPEG):
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
        r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", tmp,
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
def resolve_emoticon(md5: str) -> str | None:
    """根据 md5 找表情文件并解密，返回本地路径。"""
    keys = load_keys()
    info = keys.get("emoticon") or {}
    key_hex = info.get("key")
    data_dir = account_dir()
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
def resolve_image(md5: str, create_time: int | None = None) -> str | None:
    """找到 .dat 并解密（复用 wechatauto 的图片解密）。"""
    data_dir = account_dir()
    if not data_dir:
        return None
    hits = []
    for p in glob.glob(os.path.join(data_dir, "msg", "attach", md5, "*", "Img", "*.dat")):
        hits.append(p)
    # 有时 md5 目录名带前缀
    if not hits:
        for p in glob.glob(os.path.join(data_dir, "msg", "attach", "*", "*", "Img", "%s*.dat" % md5)):
            hits.append(p)
    if not hits:
        return None
    # 优先取最大的（原图）
    hits.sort(key=lambda x: -os.path.getsize(x))
    target = CacheKey = None
    for p in hits:
        try:
            from wx_identity import open_bound_db
            from wechatauto.media import MediaDownloader
            cfg = {}
            if os.path.exists(CONFIG):
                with open(CONFIG, "r", encoding="utf-8-sig") as f:
                    cfg = json.load(f)
            db = open_bound_db(cfg.get("account"))
            dl = MediaDownloader(db)
            dec = dl.decrypt_image(p)
            if not dec:
                continue
            ext = sniff(dec)
            if not ext:
                # 可能是 wxgf
                ext = ".bin"
            out = os.path.join(CACHE, "%s%s" % (md5, ext if ext != ".bin" else ".dat.bin"))
            if dec.startswith(b"wxgf"):
                out = os.path.join(CACHE, md5 + ".png")
                if wxgf_to_png(dec, out):
                    return out
                continue
            with open(out, "wb") as f:
                f.write(dec)
            return out
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- 通用入口
def md5_from_content(content: str) -> str | None:
    m = re.search(r'md5="([0-9a-fA-F]{32})"', content or "")
    if m:
        return m.group(1).lower()
    m = re.search(r'\b([0-9a-fA-F]{32})\b', content or "")
    return m.group(1).lower() if m else None


def resolve_for_message(msg_type: str, content: str) -> str | None:
    """按消息类型解析出本地图片路径。"""
    md5 = md5_from_content(content)
    if not md5:
        return None
    if msg_type in ("动画表情", "emoji"):
        return resolve_emoticon(md5)
    if msg_type in ("图片", "image"):
        return resolve_image(md5)
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
