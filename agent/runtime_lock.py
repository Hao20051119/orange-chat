"""本项目的 GUI 与旧链路在整个运行期间互斥。"""

from pathlib import Path


def acquire(root, role: str, *, gui_child: bool = False):
    """返回持锁文件；已有冲突进程时返回 None。关闭文件即释放锁。"""
    import msvcrt

    import pywintypes
    import win32con
    import win32file
    import winerror

    path = Path(root) / "state" / "runtime.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    descriptor = msvcrt.get_osfhandle(handle.fileno())

    def lock(start: int, length: int, *, exclusive: bool):
        offset = pywintypes.OVERLAPPED()
        offset.Offset = start
        flags = win32con.LOCKFILE_FAIL_IMMEDIATELY
        if exclusive:
            flags |= win32con.LOCKFILE_EXCLUSIVE_LOCK
        win32file.LockFileEx(descriptor, flags, length, 0, offset)
        return offset

    try:
        # 字节 0、1 保留给旧链路的服务/回复进程，GUI 父进程同时占两位。
        # 字节 2、3 标记 GUI 子进程，父进程意外结束时仍能阻止旧链路启动。
        if role == "gui":
            lock(0, 2, exclusive=True)
            children = lock(2, 2, exclusive=False)
            win32file.UnlockFileEx(descriptor, 2, 0, children)
        else:
            slot = {"service": 0, "worker": 1}[role]
            lock(slot + (2 if gui_child else 0), 1, exclusive=True)
            if not gui_child:
                lock(2, 2, exclusive=False)
    except pywintypes.error as exc:
        handle.close()
        if exc.winerror == winerror.ERROR_LOCK_VIOLATION:
            return None
        raise
    except BaseException:
        handle.close()
        raise
    return handle
