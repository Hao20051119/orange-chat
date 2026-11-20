# -*- coding: utf-8 -*-
"""语音消息转写（可选功能，默认关闭）。

链路：``MediaDownloader.download_voice`` → ``.silk`` → ``pilk`` 解码成 PCM →
``faster-whisper`` 转中文文本。

依赖缺失、音频没落盘、模型目录不存在时**一律返回 None**，由上层写成
「对方发来一条语音（没转成文字）」，绝不编造内容。开启方式：
config.json 的 ``media.voice_asr.enabled = true``。
"""

from __future__ import annotations

import os
import threading
import wave

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
CACHE = os.path.join(os.environ.get("WXBOT_MEDIA_CACHE") or os.path.join(ROOT, "media_cache"),
                     "voice")

_model = {"obj": None, "lock": threading.Lock(), "error": ""}
_asr_lock = threading.Lock()

DEFAULT_MODEL = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                             "Temp", "dsh_asr", "models", "large-v3-turbo")


def _cfg() -> dict:
    try:
        from config import load as _load
        return _load() or {}
    except Exception:
        return {}


def available() -> tuple[bool, str]:
    """检查依赖是否齐全。"""
    try:
        import pilk  # noqa: F401
    except Exception:
        return False, "缺少 pilk（pip install pilk）"
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False, "缺少 faster-whisper"
    va = (_cfg().get("media") or {}).get("voice_asr") or {}
    model_dir = va.get("model_dir") or DEFAULT_MODEL
    if not os.path.isdir(model_dir):
        return False, "模型目录不存在：%s" % model_dir
    return True, ""


def _load_model():
    with _model["lock"]:
        if _model["obj"] is not None:
            return _model["obj"]
        va = (_cfg().get("media") or {}).get("voice_asr") or {}
        model_dir = va.get("model_dir") or DEFAULT_MODEL
        from faster_whisper import WhisperModel
        _model["obj"] = WhisperModel(model_dir, device="cpu", compute_type="int8")
        return _model["obj"]


def _silk_to_wav(silk_path: str, wav_path: str) -> bool:
    """SILK（微信语音，含 #!SILK_V3 头）→ 24k 单声道 16bit wav。"""
    try:
        import pilk
    except Exception:
        return False
    pcm_path = silk_path + ".pcm"
    try:
        with open(silk_path, "rb") as f:
            head = f.read(10)
        if head.startswith(b"#!SILK"):
            # pilk 直接吃带头的文件
            pilk.decode(silk_path, pcm_path)
        else:
            pilk.decode(silk_path, pcm_path)
        with open(pcm_path, "rb") as f:
            pcm = f.read()
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
        return os.path.getsize(wav_path) > 1024
    except Exception:
        return False
    finally:
        try:
            os.unlink(pcm_path)
        except OSError:
            pass


def transcribe(chat: str, local_id, shard: str | None = None) -> str | None:
    """把一条语音转成文字；任何环节失败返回 None。"""
    va = (_cfg().get("media") or {}).get("voice_asr") or {}
    if not va.get("enabled", False):
        return None
    ok, why = available()
    if not ok:
        return None

    os.makedirs(CACHE, exist_ok=True)
    silk_path = os.path.join(CACHE, "%s_%s.silk" % (
        "".join(c for c in chat if c.isalnum() or c in "_-")[:24], local_id))
    wav_path = silk_path[:-5] + ".wav"
    try:
        from wx_identity import open_bound_db
        from wechatauto.media import MediaDownloader

        db = open_bound_db(_cfg().get("account") or "")
        got = MediaDownloader(db).download_voice(chat, int(local_id),
                                                 save_dir=CACHE, shard=shard)
        if not got:
            return None
        if got != silk_path:
            try:
                if os.path.exists(got):
                    silk_path = got
            except OSError:
                pass
        if not _silk_to_wav(silk_path, wav_path):
            return None
    except Exception:
        return None

    with _asr_lock:
        try:
            import numpy as np
            model = _load_model()
            with wave.open(wav_path, "rb") as w:
                frames = w.readframes(w.getnframes())
                rate = w.getframerate()
            audio = np.frombuffer(frames, dtype=np.int16).astype("float32") / 32768.0
            segments, _info = model.transcribe(audio, language="zh", vad_filter=True,
                                               beam_size=1, condition_on_previous_text=False)
            text = "".join((s.text or "") for s in segments).strip()
            return text[:200] or None
        except Exception:
            return None
        finally:
            for p in (wav_path,):
                try:
                    os.unlink(p)
                except OSError:
                    pass


if __name__ == "__main__":
    import sys

    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ok, why = available()
    print("可用:", ok, why)
    cfg = _cfg()
    print("开关:", ((cfg.get("media") or {}).get("voice_asr") or {}).get("enabled"))
    if len(sys.argv) > 2:
        print(transcribe(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
