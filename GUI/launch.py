"""Report desktop startup errors even when launched with pythonw.exe."""

import ctypes
import traceback
from pathlib import Path


def main():
    try:
        from app import main as open_window

        open_window()
    except Exception as error:
        log_path = Path(__file__).with_name(".runtime") / "startup.log"
        try:
            log_path.parent.mkdir(exist_ok=True)
            log_path.write_text(traceback.format_exc(), encoding="utf-8")
            details = f"\n\n诊断日志：{log_path}"
        except OSError:
            details = ""
        message = f"Orange 未能打开窗口。\n\n{type(error).__name__}: {error}{details}"
        ctypes.windll.user32.MessageBoxW(None, message, "Orange 启动失败", 0x10)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
