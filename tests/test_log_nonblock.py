# -*- coding: utf-8 -*-
"""验证日志不会因为控制台卡死而阻塞业务线程。"""
import io
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
import logutil  # noqa

tmp = os.path.join(tempfile.mkdtemp(), "t.log")


class BlockingStdout:
    def __init__(self):
        self.gate = threading.Event()

    def write(self, s):
        if not self.gate.wait(5):
            raise TimeoutError("stdout blocked")
        return len(s)

    def flush(self):
        pass


logger = logutil.Logger(tmp, echo=True)
real_stdout = sys.stdout
blocker = BlockingStdout()
sys.stdout = blocker
try:
    t0 = time.time()
    for i in range(20):
        logger.info("第 %d 行" % i)
    cost = time.time() - t0

    def count():
        try:
            return len(io.open(tmp, encoding="utf-8").read().splitlines())
        except OSError:
            return -1

    deadline = time.time() + 3
    while time.time() < deadline and count() < 20:
        time.sleep(0.05)
    print_ok = False
finally:
    sys.stdout = real_stdout

lines = count()
blocker.gate.set()
time.sleep(0.3)
print("写入 20 行耗时 %.3fs" % cost)
print("卡住控制台时文件行数：%d（应为 20）" % lines)
print("业务线程未被阻塞：%s" % (cost < 1.0))
print("结果：%s" % ("PASS" if (lines == 20 and cost < 1.0) else "FAIL"))
