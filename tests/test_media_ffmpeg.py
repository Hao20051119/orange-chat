# -*- coding: utf-8 -*-
"""ffmpeg 定位：不写死任何本机路径，开源后别人也能用。

只有微信 4.x 的 .wxgf（H.265 容器）需要 ffmpeg 抽首帧；PNG/JPEG/GIF/WEBP
解密后直接用，跟 ffmpeg 无关。找不到 ffmpeg 时不能报错，只是那类表情退化成
文字占位。
"""
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
from support import check, section

os.environ["WXBOT_MEDIA_CACHE"] = os.path.join(support.TMP, "media_cache")

import media      # noqa: E402

TMP = os.path.join(support.TMP, "ffprobe")
os.makedirs(os.path.join(TMP, "tools", "ffmpeg"), exist_ok=True)


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"stub")
    return path


def with_env(tmp_conf="", path_hit="", imageio=None, env=""):
    """一次性布好所有查找来源，用完由调用方退出 with。"""
    conf = os.path.join(TMP, "config.json")
    if tmp_conf is not None:
        with open(conf, "w", encoding="utf-8") as f:
            json.dump({"media": {"ffmpeg": tmp_conf}}, f)
    patches = [
        patch.object(media, "ROOT", TMP),
        patch.object(media, "CONFIG", conf),
        patch.object(media.shutil, "which", lambda name: path_hit or None),
        patch.dict(sys.modules, {"imageio_ffmpeg": imageio}),
        patch.dict(os.environ, {"WXBOT_FFMPEG": env}),
    ]
    return patches


def find(**kw):
    ps = with_env(**kw)
    for p in ps:
        p.start()
    try:
        return media.find_ffmpeg(refresh=True)
    finally:
        for p in ps:
            p.stop()


def clear_tools():
    d = os.path.join(TMP, "tools", "ffmpeg")
    for n in ("ffmpeg.exe", "ffmpeg"):
        p = os.path.join(d, n)
        if os.path.exists(p):
            os.remove(p)


section("一个都没有：返回空串，不报错")
clear_tools()
check("找不到就是空串", find() == "", repr(find()))

section("顺序：环境变量 > 配置 > tools > PATH > imageio")
clear_tools()
env_exe = touch(os.path.join(TMP, "env.exe"))
cfg_exe = touch(os.path.join(TMP, "cfg.exe"))
tools_exe = touch(os.path.join(TMP, "tools", "ffmpeg", "ffmpeg.exe"))
path_exe = touch(os.path.join(TMP, "path.exe"))


class FakeImageio:
    @staticmethod
    def get_ffmpeg_exe():
        return os.path.join(TMP, "imageio.exe")


touch(os.path.join(TMP, "imageio.exe"))

check("环境变量优先", find(env=env_exe, tmp_conf=cfg_exe, path_hit=path_exe,
                          imageio=FakeImageio) == env_exe, "")
os.environ.pop("WXBOT_FFMPEG", None)
check("其次是 config.json 的 media.ffmpeg",
      find(tmp_conf=cfg_exe, path_hit=path_exe, imageio=FakeImageio) == cfg_exe, "")
check("再是项目内 tools\\ffmpeg",
      find(tmp_conf="", path_hit=path_exe, imageio=FakeImageio) == tools_exe, "")
clear_tools()
check("PATH 里的也算",
      find(tmp_conf="", path_hit=path_exe, imageio=FakeImageio) == path_exe, "")
check("imageio-ffmpeg 自带的也算",
      find(tmp_conf="", imageio=FakeImageio) == os.path.join(TMP, "imageio.exe"), "")
check("找不到就空串，不会去用任何本机私货",
      find(tmp_conf="") == "", repr(find(tmp_conf="")))

section("配置写了不存在的路径：跳过，继续往后找")
check("坏的配置不影响兜底",
      find(tmp_conf=os.path.join(TMP, "没有这个.exe"), path_hit=path_exe) == path_exe, "")

section("桌面会话沿用项目的 ffmpeg 设置")
root_config = Path(TMP, "config.json")
session_config = Path(TMP, "session.json")
root_config.write_text(json.dumps({"media": {"ffmpeg": cfg_exe}}), encoding="utf-8")
session_config.write_text("{}", encoding="utf-8")
with patch.object(media, "ROOT", TMP), patch.object(media, "CONFIG", str(session_config)), \
        patch.dict(os.environ, {"WXBOT_FFMPEG": ""}):
    check("会话缺配置时使用项目配置", media.find_ffmpeg(refresh=True) == cfg_exe)
    session_config.write_text(json.dumps({"media": {"ffmpeg": env_exe}}), encoding="utf-8")
    check("会话明确配置时优先使用", media.find_ffmpeg(refresh=True) == env_exe)

section("没有 ffmpeg：.wxgf 只跳过，不抛错")
clear_tools()
with_env_kw = dict(tmp_conf="")
ps = with_env(**with_env_kw)
for p in ps:
    p.start()
try:
    media.find_ffmpeg(refresh=True)
    check("ffmpeg_exe 返回空", media.ffmpeg_exe(refresh=True) == "", "")
    check("wxgf 转 PNG 安静失败",
          media.wxgf_to_png(b"wxgf" + b"\x00" * 32,
                            os.path.join(TMP, "out.png")) is False, "")
    check("文件没被生成", not os.path.exists(os.path.join(TMP, "out.png")), "")
finally:
    for p in ps:
        p.stop()

section("普通图片/表情不需要 ffmpeg")
from PIL import Image                                    # noqa: E402

png = os.path.join(TMP, "a.png")
Image.new("RGB", (64, 48), (10, 20, 30)).save(png, "PNG")
with_env_kw = dict(tmp_conf="")
ps = with_env(**with_env_kw)
for p in ps:
    p.start()
try:
    media.find_ffmpeg(refresh=True)
    uri, size = media.to_vision_uri(png)
    check("没有 ffmpeg 也能转 data URI", bool(uri) and size > 0, str(size))
    check("返回的预算大小是 base64 长度", size == len(uri.split(",", 1)[1]))
    check("单张预算也按 base64 限制", media.to_vision_uri(png, max_bytes=size - 1) == (None, 0))
finally:
    for p in ps:
        p.stop()

section("识别 wxgf 容器")
check("头部 wxgf 认得出", media.sniff(b"wxgf" + b"\x00" * 8) == ".wxgf", "")
check("PNG 头不被误判", media.sniff(b"\x89PNG\r\n\x1a\n") == ".png", "")

section("正文只有占位时按原始数据库行定位本机图片")
directory = Path(TMP, "account")
chat = "test@chatroom"
digest = "1" * 32
local_id = 123
expected_shard = "message_1.db"
image_dir = directory / "msg" / "attach" / hashlib.md5(chat.encode()).hexdigest() / "2026-11" / "Img"
image_dir.mkdir(parents=True)
(image_dir / f"{digest}_h.dat").write_bytes(b"\x89PNG\r\n\x1a\n")
(image_dir / f"{digest}_t.dat").write_bytes(Path(png).read_bytes())
other_dir = directory / "msg" / "attach" / ("2" * 32) / "2026-11" / "Img"
other_dir.mkdir(parents=True)
Image.new("RGB", (32, 16)).save(other_dir / f"{digest}.dat", format="PNG")


class BoundDatabase:
    account_dir = directory

    def get_message_row(self, chat_arg, local_id_arg, *, local_type, shard):
        if (chat_arg, local_id_arg, local_type, shard) == (chat, local_id, 3, expected_shard):
            return {"packed_info": b"\x01" + digest.encode("ascii"), "content": "[图片]"}
        return None


class LocalDownloader:
    def __init__(self, db):
        self.db = db

    def decrypt_image(self, path):
        return Path(path).read_bytes()


with patch("wx_identity.open_bound_db", return_value=BoundDatabase()) as bind_db, \
        patch.dict(sys.modules, {"wechatauto.media": SimpleNamespace(MediaDownloader=LocalDownloader)}):
    saved = media.resolve_for_message("image", "[图片]", chat=chat, local_id=local_id,
                                      shard=expected_shard, account="wxid_test")
    check("按指定账号绑定", bind_db.call_args.args == ("wxid_test",))
    check("原图无法读取时取当前会话的缩略图",
          bool(saved) and Path(saved).read_bytes() == Path(png).read_bytes())
    if saved:
        Path(saved).unlink()
    restored = media.resolve_any("", chat=chat, local_id=local_id,
                                 shard=expected_shard, account="wxid_test")
    check("缓存清空后仍按原消息找回图片", bool(restored) and Path(restored).is_file())
    check("分片无法定位时不从其他会话猜测图片",
          media.resolve_for_message("image", "[图片]", chat=chat, local_id=local_id,
                                    shard="message_2.db", account="wxid_test") is None)
    with patch.dict(os.environ, {"WXBOT_ACCOUNT": "wxid_test"}):
        check("其他账号不能读取当前进程的图片缓存",
              media.resolve_any(digest, account="wxid_other") is None)

with patch.dict(os.environ, {"WXBOT_ACCOUNT": "wxid_test"}), \
        patch("wx_identity.open_bound_db", side_effect=RuntimeError("迁移后原微信数据库不可用")):
    check("迁移后的缓存图片能与原消息元数据共存",
          media.resolve_any(digest, chat=chat, local_id=local_id, shard=expected_shard,
                            account="wxid_test") == restored)

raise SystemExit(support.finish())
