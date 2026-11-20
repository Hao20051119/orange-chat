# -*- coding: utf-8 -*-
"""跑全部测试：每个用例独立进程 + 临时库，互不干扰。

用法：
    python run_tests.py            # 全跑
    python run_tests.py queue      # 只跑文件名含 queue 的
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(ROOT, "tests")


def main() -> int:
    # 控制台编码不可信（GBK/UTF-8 都常见）：编码不了就用 ? 代替，别再让汇总崩掉。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    keyword = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(glob.glob(os.path.join(TESTS, "test_*.py")))
    if keyword:
        files = [f for f in files if keyword in os.path.basename(f)]
    if not files:
        print("没有找到测试")
        return 1

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("WXBOT_MEMORY_DB", None)
    env.pop("WXBOT_QUEUE_DB", None)
    env.pop("WXBOT_LOG", None)

    results = []
    t0 = time.time()
    for path in files:
        name = os.path.basename(path)
        print("\n" + "#" * 62)
        print("# " + name)
        print("#" * 62)
        p = subprocess.run([sys.executable, path], cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600)
        out = p.stdout or ""
        err = p.stderr or ""
        ok = p.returncode == 0
        results.append((name, ok, out + "\n" + err))
        if ok:
            tail = [l for l in out.splitlines() if "通过" in l or "失败" in l]
            print(tail[-1] if tail else "通过")
        else:
            print(out[-4000:])
            if p.stderr:
                print("--- stderr ---")
                print(p.stderr[-3000:])

    print("\n" + "=" * 62)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        # 不要用 ✅/❌：中文 Windows 的 cmd 是 GBK，打印 emoji 会抛
        # UnicodeEncodeError，让"全绿"的测试以退出码 1 收场。
        print("%s %s" % ("[OK]  " if ok else "[FAIL]", name))
    print("-" * 62)
    print("用例文件 %d，通过 %d，失败 %d，用时 %.1fs"
          % (len(results), passed, len(results) - passed, time.time() - t0))

    # 缺第三方依赖时补一句人话，别让人对着 9 个 traceback 猜
    missing = set()
    for name, ok, out in results:
        if ok:
            continue
        for line in out.splitlines():
            if "ModuleNotFoundError" in line:
                mod = line.split("No module named")[-1].strip(" '\"")
                missing.add(mod.split(".")[0])
    if missing:
        print("")
        print("提示：有 %d 个用例因为没装依赖而失败（缺 %s）。"
              % (len(results) - passed, "、".join(sorted(missing))))
        print("      先双击「安装.bat」，或手动跑：")
        print("      python -m pip install -r requirements.txt")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
