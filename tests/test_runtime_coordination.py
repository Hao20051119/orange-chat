# -*- coding: utf-8 -*-
"""验证后台进程互斥和服务配置的并发更新。"""
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import support
from support import check, section

import config as C
import runtime_lock
import wx_service


def can_start(root, role, gui_child=False):
    """从另一个进程试锁，不启动微信服务。"""
    code = """
import sys
sys.path.insert(0, sys.argv[1])
import runtime_lock
lock = runtime_lock.acquire(sys.argv[2], sys.argv[3], gui_child=sys.argv[4] == 'True')
if lock is None:
    sys.exit(2)
lock.close()
"""
    result = subprocess.run([sys.executable, "-c", code, support.AGENT, str(root), role,
                             str(gui_child)], capture_output=True, timeout=10)
    if result.returncode not in (0, 2):
        raise RuntimeError(result.stderr.decode(errors="replace"))
    return result.returncode == 0


section("同一项目中的后台在整个运行期间互斥")
root = Path(support.TMP)
old = root / "GUI" / ".runtime" / "old-account"
old.mkdir(parents=True)
(old / "config.json").write_text(json.dumps({"agent": {"auto_reply": True}}), encoding="utf-8")
with runtime_lock.acquire(root, "gui"):
    check("GUI 阻止旧服务和旧回复进程启动",
          not can_start(root, "service") and not can_start(root, "worker"))
    check("旧账号打开过自动回复也不妨碍 GUI 子进程启动",
          can_start(root, "service", gui_child=True) and can_start(root, "worker", gui_child=True))
    check("拒绝另一个 GUI", not can_start(root, "gui"))

with runtime_lock.acquire(root, "worker", gui_child=True):
    check("GUI 父进程退出后，后台未退出前仍禁止切换链路",
          not can_start(root, "service") and not can_start(root, "gui"))

with runtime_lock.acquire(root, "service"):
    check("旧回复进程可以与自己的服务并存", can_start(root, "worker"))
    check("拒绝重复的旧服务和并行 GUI", not can_start(root, "service") and not can_start(root, "gui"))
with runtime_lock.acquire(root, "worker"):
    check("旧服务可以与已启动的旧回复进程并存", can_start(root, "service"))
    check("拒绝重复的旧回复进程", not can_start(root, "worker"))
check("所有后台退出后可以重新启动 GUI", can_start(root, "gui"))

section("保存联系人参数不撤销同时关闭的自动回复")
path = root / "config.json"
path.write_text(json.dumps(support.base_cfg(agent={"auto_reply": True})), encoding="utf-8")
C.CONFIG_PATH = str(path)
service = wx_service.Service("wxid_test")
snapshot_taken = Event()
allow_save = Event()
disable_started = Event()
disable_done = Event()
normalize_patch = wx_service.proactive_rules.normalize_patch


def wait_after_snapshot(value):
    snapshot_taken.set()
    if not allow_save.wait(5):
        raise TimeoutError("没有收到继续保存的信号")
    return normalize_patch(value)


def disable_replies():
    disable_started.set()
    result = service.update_config({"agent.auto_reply": False})
    disable_done.set()
    return result


with patch.object(wx_service.proactive_rules, "normalize_patch", side_effect=wait_after_snapshot), \
        patch.object(wx_service, "WxQueue", None), ThreadPoolExecutor(max_workers=2) as pool:
    params = pool.submit(service.set_proactive_params, "wxid_contact", {"per_chat_daily": 2})
    try:
        check("联系人更新已获取配置快照", snapshot_taken.wait(5))
        disable = pool.submit(disable_replies)
        check("关闭自动回复等待正在保存的操作", disable_started.wait(5) and not disable_done.wait(0.2))
    finally:
        allow_save.set()
    check("并发操作均成功", params.result(timeout=5)["ok"] and disable.result(timeout=5)["ok"])

stored = json.loads(path.read_text(encoding="utf-8"))
check("两次修改都保留", not stored["agent"]["auto_reply"] and
      stored["contacts"]["wxid_contact"]["proactive_daily"] == 2)

sys.exit(support.finish())
