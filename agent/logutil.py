# -*- coding: utf-8 -*-
"""统一日志：文件 + 控制台，支持正文脱敏、按大小轮转、按天数清理。

以前每个模块各写一套 ``print`` + ``except: pass``，出问题完全静默；聊天正文
（入站 40 字 / 回复 50 字）也会长期留在 reply.log 里。这里收口成一个东西：

* ``logging.chat_content=false`` 时，聊天正文只记「长度 + 哈希前 6 位」；
* 超过 ``logging.max_mb`` 自动归档成 ``reply.log.<ts>``；
* 启动与每小时按 ``logging.keep_days`` 删除过期归档。

**写入永不在调用线程里阻塞**：调用方只往两个有界队列里塞一行，真正的文件写入
和屏幕打印各由一个后台线程完成。Windows 控制台开了「快速编辑」时，只要有人在
窗口里点一下/选中文字/按了 Ctrl+S，控制台输出就会被冻结——以前 ``print`` 直接
在业务线程里执行，一冻结就连发送线程、主循环一起卡死（日志停在某一秒，消息不
再发送）。现在文件写入和打印互相独立，控制台卡住也不会影响文件日志和业务线程。
"""
from __future__ import annotations

import atexit
import hashlib
import os
import queue
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
LOG_PATH = os.environ.get("WXBOT_LOG") or os.path.join(ROOT, "reply.log")

FILE_QUEUE_MAX = 20000
PRINT_QUEUE_MAX = 2000


def _cfg() -> dict:
    try:
        from config import load as _load
        return _load() or {}
    except Exception:
        return {}


def disable_console_quickedit() -> bool:
    """关掉控制台「快速编辑」：否则鼠标点一下窗口就会冻结所有输出。"""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        if h in (0, -1):
            return False
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        enable_quick_edit = 0x0040
        enable_extended = 0x0080
        return bool(k32.SetConsoleMode(
            h, (mode.value | enable_extended) & ~enable_quick_edit))
    except Exception:
        return False


class Logger:
    def __init__(self, path: str = LOG_PATH, echo: bool = True):
        self.path = path
        self.echo = echo
        self._lock = threading.Lock()
        self._last_rotate = 0.0
        self._last_clean = 0.0
        self._file_q: queue.Queue[str] = queue.Queue(maxsize=FILE_QUEUE_MAX)
        self._print_q: queue.Queue[str] = queue.Queue(maxsize=PRINT_QUEUE_MAX)
        self._start_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._dropped = 0
        self._quickedit_done = False

    # ------------------------------------------------------------ 脱敏
    @staticmethod
    def chat_content_enabled(cfg: dict | None = None) -> bool:
        cfg = cfg if cfg is not None else _cfg()
        return bool((cfg.get("logging") or {}).get("chat_content", True))

    def redact(self, text, cfg: dict | None = None) -> str:
        """开关关闭时把聊天正文变成可追踪但不可读的指纹。"""
        s = "" if text is None else str(text)
        if self.chat_content_enabled(cfg):
            return s
        h = hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:6]
        return "<正文 %d 字 #%s>" % (len(s), h)

    # ------------------------------------------------------------ 轮转/清理
    def _rotate_if_needed(self):
        cfg = _cfg()
        max_mb = float((cfg.get("logging") or {}).get("max_mb", 5))
        now = time.time()
        if now - self._last_rotate < 3600:
            return
        self._last_rotate = now
        try:
            if not os.path.exists(self.path):
                return
            if os.path.getsize(self.path) < max_mb * 1024 * 1024:
                return
            stamp = time.strftime("%Y%m%d-%H%M%S")
            os.replace(self.path, "%s.%s" % (self.path, stamp))
        except Exception:
            pass

    def cleanup(self):
        """按 keep_days 删除过期归档（只在启动和每小时做一次）。"""
        cfg = _cfg()
        keep_days = float((cfg.get("logging") or {}).get("keep_days", 14))
        now = time.time()
        if now - self._last_clean < 3600:
            return
        self._last_clean = now
        cutoff = now - keep_days * 86400
        folder = os.path.dirname(self.path) or "."
        prefix = os.path.basename(self.path) + "."
        try:
            for name in os.listdir(folder):
                if not name.startswith(prefix):
                    continue
                p = os.path.join(folder, name)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.unlink(p)
                except OSError:
                    pass
        except OSError:
            pass

    # ------------------------------------------------------------ 后台线程
    def _ensure_threads(self):
        if self._threads:
            return
        with self._start_lock:
            if self._threads:
                return
            f = threading.Thread(target=self._file_loop, name="log-file",
                                 daemon=True)
            p = None
            if self.echo:
                p = threading.Thread(target=self._print_loop, name="log-print",
                                     daemon=True)
            self._threads = [f] if p is None else [f, p]
            if not self._quickedit_done:
                self._quickedit_done = True
                disable_console_quickedit()
            for t in self._threads:
                t.start()

    def _file_loop(self):
        while True:
            line = self._file_q.get()
            try:
                self._rotate_if_needed()
                with self._lock, open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
            finally:
                self._file_q.task_done()

    def _print_loop(self):
        while True:
            line = self._print_q.get()
            try:
                print(line, flush=True)
            except Exception:
                pass
            finally:
                self._print_q.task_done()

    # ------------------------------------------------------------ 写
    def _write(self, level: str, msg: str, echo: bool = True):
        line = "%s %-5s %s" % (time.strftime("%H:%M:%S"), level, msg)
        try:
            self._ensure_threads()
        except Exception:
            return
        try:
            self._file_q.put_nowait(line)
        except queue.Full:
            self._dropped += 1
        if echo and self.echo:
            try:
                self._print_q.put_nowait(line)
            except queue.Full:
                pass

    def flush(self, timeout: float = 2.0):
        """尽量把队列里剩下的行写完（退出前调用）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._file_q.empty() and self._print_q.empty():
                return
            time.sleep(0.02)

    def info(self, msg, echo: bool = True):
        self._write("INFO", str(msg), echo)

    def warning(self, msg, echo: bool = True):
        self._write("WARN", str(msg), echo)

    def error(self, msg, echo: bool = True):
        self._write("ERROR", str(msg), echo)


_logger = Logger()


def get_logger() -> Logger:
    return _logger


def flush(timeout: float = 2.0):
    _logger.flush(timeout)


def log(msg):
    _logger.info(msg)


def warn(msg):
    _logger.warning(msg)


def error(msg):
    _logger.error(msg)


def redact(text, cfg=None) -> str:
    return _logger.redact(text, cfg)


def setup_stdio():
    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    disable_console_quickedit()
    _logger.cleanup()


atexit.register(flush, 2.0)
