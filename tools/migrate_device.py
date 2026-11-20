# -*- coding: utf-8 -*-
"""跨机器迁移 Orange（微信小号）的"人格/关系/名单/记忆/提醒/知识库"。

为什么要这个脚本
----------------
微信在本机的账号目录名是 ``wxid_xxx_<4位随机后缀>``（例如
``wxid_abcdefghijklm_1a2b``）。这个后缀每台电脑都不一样，而项目把它当作
``account`` 用在了三处：

* ``GUI/.runtime/<sha256(account)[:24]>/``  会话数据目录名
* ``config.json`` 的 ``account``
* 数据库里所有带 ``account`` 列的表（提醒、入出站、知识库文档……）

直接拷数据过去，提醒会因为 account 不匹配而永不触发、GUI 也找不到旧会话，
所以必须"就地适配"一次。

子命令
------
    python tools\\migrate_device.py status
        看当前会话在哪、各库多少条数据。

    python tools\\migrate_device.py export [--out 文件.zip] [--with-code]
        旧机器：把数据（几 MB）打成迁移包，可加代码一起打。

    python tools\\migrate_device.py adapt [--account 新账号] [--apply]
        新机器：整个项目文件夹已拷过来后，就地把旧会话适配到本机新账号。
        默认只预演，加 --apply 才真正写。

    python tools\\migrate_device.py import 迁移包.zip [--account 新账号] [--apply]
        新机器：从迁移包恢复数据并适配。

迁移内容
--------
必带（不带你就要重新养一遍）：人设卡 persona_card.json（固定人设 + 关系档案）、
style_samples.json（学到的说话样本）、memory.db（每人的 facts/profiles/adapt）、
knowledge.db（知识库正文与分块）、queue.db（定时提醒 + 收发历史）、
GUI 会话 config.json（名单、主动聊天白名单、模型参数）。

导出的配置会清除模型 API Key 和服务令牌。本机微信解密密钥也不迁移。
--with-code 只添加项目源码、启动脚本、模板和界面资源。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import site
import sqlite3
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "GUI" / ".runtime"
BACKUP = ROOT / "backup"

# 带 account 列、需要跟着账号改的表
ACCOUNT_TABLES = {
    "queue.db": ("inbound", "outbound", "collect_watermark", "reminders",
                 "reminder_actions", "reminder_candidates", "proactive_state"),
    "knowledge.db": ("kb_documents", "kb_jobs"),
}
# 会话里的三个数据文件
DB_FILES = ("queue.db", "memory.db", "knowledge.db")

# 会话目录里必须跟着走的文件；其余（日志/锁/缓存）不迁。
# web_budget.json 是"今天已用多少"的计数，跨机器带过去会平白吃掉预算，故意不带。
SESSION_FILES = ("config.json", "queue.db", "memory.db", "knowledge.db", "usage.jsonl")
# keys.json 是本机微信的媒体解密密钥，换机器后失效（工具会自己重新探测），故不迁
PROJECT_FILES = ("config.json", "persona_card.json", "style_samples.json")
PROJECT_DIRS = ("stickers", "kb")
CODE_FILES = (
    ".gitattributes", ".gitignore", "LICENSE", "README.md", "迁移说明.md",
    "requirements.txt", "config.example.json", "persona_card.example.json",
    "run_tests.py", "*.bat",
    "agent/*.py", "memory/*.py", "stickers/*.py", "tests/*.py",
    "tools/*.py", "tools/*.bat", "tools/*.ps1", "wxbot/*.py", "wxbot/*.bat", "wxbot/*.html",
    "GUI/*.py", "GUI/*.bat", "GUI/README.md", "GUI/orange-icon.png", "GUI/orange.ico",
    "kb/README.md", "kb/inbox/.keep",
)


# ---------------------------------------------------------------- 基础工具
def session_dir_for(account: str) -> Path:
    """GUI 的会话目录名 = sha256(account) 前 24 位。"""
    return RUNTIME / hashlib.sha256(account.encode()).hexdigest()[:24]


def read_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def find_session() -> Path | None:
    """定位当前在用的会话目录：优先按 config.json 的 account 算，其次取最大的库。"""
    account = str(read_json(ROOT / "config.json").get("account") or "")
    if account:
        guess = session_dir_for(account)
        if (guess / "queue.db").is_file():
            return guess
    if not RUNTIME.is_dir():
        return None
    dirs = [d for d in RUNTIME.iterdir() if d.is_dir() and (d / "queue.db").is_file()]
    if not dirs:
        return None
    dirs.sort(key=lambda d: (d / "queue.db").stat().st_size, reverse=True)
    return dirs[0]


def discover_accounts() -> list[dict]:
    """让 wechatauto 列出本机已登录（且微信在跑）的小号。"""
    for path in (ROOT, ROOT / "agent", ROOT / "wxbot", Path(os.environ.get("WXBOT_PYLIB")
                                                            or ROOT / "pylibs")):
        if not path.is_dir():
            continue
        if path.name == "pylibs" or (path / "pywin32.pth").is_file():
            # pylibs 是 pip --target 目录，必须当 site 目录加载，pywin32.pth 才会生效
            site.addsitedir(str(path))
        elif str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import wx_identity  # noqa: WPS433 延迟导入，没装依赖时会明确报错
    return wx_identity.discover_accounts()


def bare_wxid(account: str) -> str:
    """去掉微信目录名的 4 位随机后缀，得到稳定 wxid。"""
    import re
    return re.sub(r"_\w{4}$", "", account or "")


def db_counts(path: Path) -> dict:
    out: dict[str, int] = {}
    if not path.is_file():
        return out
    conn = sqlite3.connect("file:%s?mode=ro" % path.as_posix(), uri=True)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            " AND name NOT LIKE '%_fts%' ORDER BY name")]
        for name in names:
            out[name] = conn.execute("SELECT COUNT(*) FROM [%s]" % name).fetchone()[0]
    finally:
        conn.close()
    return out


def clone_db(src: Path, dst: Path):
    """用 SQLite backup API 复制库，自动带上未合并的 -wal 内容。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    source = sqlite3.connect(str(src), timeout=20)
    target = sqlite3.connect(str(dst), timeout=20)
    try:
        with target:
            source.backup(target)
    finally:
        source.close()
        target.close()


# ---------------------------------------------------------------- status
def cmd_status(_args) -> int:
    session = find_session()
    root_cfg = read_json(ROOT / "config.json")
    print("项目根：%s" % ROOT)
    print("根 config 的 account：%s" % (root_cfg.get("account") or "（空）"))
    card = read_json(ROOT / "persona_card.json")
    print("人设卡：fixed_persona %d 字 · relations %d 条 · relationships %d 条"
          % (len(card.get("fixed_persona") or ""),
             len(card.get("relations") or []),
             len(card.get("relationships") or {})))
    if not session:
        print("没有找到 GUI 会话目录（还没在 GUI 里启动过服务？）")
        return 0
    cfg = read_json(session / "config.json")
    print("\n会话目录：%s" % session)
    print("会话 account：%s  （wxid %s）"
          % (cfg.get("account"), bare_wxid(str(cfg.get("account") or ""))))
    print("服务端口：%s   自动回复：%s   黑名单 %d 人/群   主动聊天白名单 %d 人"
          % (cfg.get("port"), (cfg.get("agent") or {}).get("auto_reply"),
             len((cfg.get("agent") or {}).get("reply_blacklist") or []),
             len((cfg.get("proactive") or {}).get("whitelist") or [])))
    for name in ("queue.db", "memory.db", "knowledge.db"):
        path = session / name
        counts = db_counts(path)
        if not counts:
            print("  %-14s 缺失" % name)
            continue
        size = path.stat().st_size / 1024
        brief = "、".join("%s %d" % (k, v) for k, v in counts.items() if v)
        print("  %-14s %6.0f KB  %s" % (name, size, brief[:120]))
    return 0


# ---------------------------------------------------------------- export
def without_credentials(config: dict) -> dict:
    result = {**config, "token": ""}
    if isinstance(config.get("llm"), dict):
        result["llm"] = {**config["llm"], "api_key": ""}
    return result


def copy_code(destination: Path):
    root = ROOT.resolve()
    for pattern in CODE_FILES:
        for source in root.glob(pattern):
            if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(root):
                continue
            target = destination / source.relative_to(root)
            if source.name == "config.example.json":
                write_json(target, without_credentials(read_json(source)))
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)


def cmd_export(args) -> int:
    session = find_session()
    if not session:
        raise SystemExit("没有找到 GUI 会话目录，先在 GUI 里启动一次服务再导出。")
    cfg = read_json(session / "config.json")
    account = str(cfg.get("account") or read_json(ROOT / "config.json").get("account") or "")
    if not account:
        raise SystemExit("会话 config.json 里没有 account，无法导出。")

    # 默认落在项目根、文件名带日期：用户一眼能找到，也不容易覆盖上一份
    out = Path(args.out) if args.out else ROOT / ("Orange数据-%s.zip" % stamp())
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = BACKUP / ("migrate-%s-tmp" % stamp())
    staging.mkdir(parents=True, exist_ok=True)

    manifest = {
        "kind": "orange-migrate",
        "version": 1,
        "created": int(time.time()),
        "created_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "project": str(ROOT),
        "account": account,
        "wxid": bare_wxid(account),
        "session": session.name,
        "with_code": bool(args.with_code),
        "counts": {name: db_counts(session / name)
                   for name in ("queue.db", "memory.db", "knowledge.db")},
    }

    try:
        data = staging / "data"
        (data / "runtime").mkdir(parents=True, exist_ok=True)

        # 会话数据：数据库走 backup，日志/锁不进包
        for name in SESSION_FILES:
            src = session / name
            if not src.is_file():
                continue
            if name == "config.json":
                write_json(data / "runtime" / name, without_credentials(cfg))
            elif src.suffix == ".db":
                clone_db(src, data / "runtime" / name)
            else:
                shutil.copy2(src, data / "runtime" / name)

        # 会话里的投喂目录（知识库放进来但还没处理/需要留档的文件）
        inbox = session / "inbox"
        if inbox.is_dir() and any(inbox.iterdir()):
            shutil.copytree(inbox, data / "runtime" / "inbox")

        # 项目级：人设卡、风格样本、配置
        for name in PROJECT_FILES:
            src = ROOT / name
            if not src.is_file():
                continue
            if name == "config.json":
                write_json(data / name, without_credentials(read_json(src)))
            else:
                shutil.copy2(src, data / name)

        # 表情包与知识库投喂目录
        for name in PROJECT_DIRS:
            src = ROOT / name
            if src.is_dir():
                shutil.copytree(src, data / name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.db-*"))

        # 仅打包项目源码，数据文件由上面的迁移清单管理。
        if args.with_code:
            copy_code(staging / "code")

        manifest["counts"]["project"] = {
            name: (ROOT / name).is_file() for name in PROJECT_FILES}
        write_json(staging / "manifest.json", manifest)

        print("打包 %s ..." % out)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(staging))
        size = out.stat().st_size / 1024 / 1024
        print("完成：%s（%.1f MB）" % (out, size))
        print("旧 account：%s  →  import 时会自动换成本机的新 account" % account)
        print("")
        print("下一步：把这个文件拷到另一台电脑，放进它的 Orange 文件夹，")
        print("        双击那边的「导入数据.bat」即可。")
        for name, counts in manifest["counts"].items():
            if isinstance(counts, dict) and counts and name != "project":
                print("  %-14s %s" % (name, "、".join("%s %d" % kv for kv in counts.items()
                                                      if kv[1])[:110]))
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------- adapt 核心
def update_accounts(path: Path, old: str, new: str) -> list[str]:
    """把库里所有 account 列从旧值改成新值，返回改过的表。"""
    touched = []
    conn = sqlite3.connect(str(path), timeout=25)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        wanted = ACCOUNT_TABLES.get(path.name, ())
        with conn:
            for table in tables:
                if table not in wanted:
                    continue
                cols = [r[1] for r in conn.execute("PRAGMA table_info([%s])" % table)]
                if "account" not in cols:
                    continue
                changed = conn.execute(
                    "UPDATE [%s] SET account=? WHERE account<>?" % table,
                    (new, new)).rowcount
                if changed:
                    touched.append("%s %d 行" % (table, changed))
            # 采集水位是微信本机库的 sort_seq，换机器后数值体系变了，必须清空，
            # 否则要么漏采（水位偏高）要么把旧历史翻出来重发。清空后 worker 会
            # 按"本次启动之后到达的消息"重新起水位。
            if path.name == "queue.db":
                removed = conn.execute("DELETE FROM collect_watermark").rowcount
                if removed:
                    touched.append("collect_watermark 清空 %d 行" % removed)
    finally:
        conn.close()
    return touched


def fix_config(cfg: dict, account: str, folder: Path) -> dict:
    """会话配置：换账号、清令牌/端口、把知识库路径改到新目录。"""
    cfg = dict(cfg)
    cfg["account"] = account
    cfg["token"] = ""
    cfg["port"] = 0  # GUI 每次启动自己分配
    kb = dict(cfg.get("kb") or {})
    kb["db_path"] = str(folder / "knowledge.db")
    kb["inbox"] = str(folder / "inbox")
    cfg["kb"] = kb
    return cfg


def adapt_session(old_folder: Path, account: str, apply: bool) -> dict:
    """把 old_folder 的会话数据迁到本机 account 对应的目录，并改 account 字段。"""
    target = session_dir_for(account)
    old_cfg = read_json(old_folder / "config.json")
    old_account = str(old_cfg.get("account") or "")
    report: dict = {"old": old_folder, "new": target, "old_account": old_account,
                    "account": account, "same_dir": target == old_folder, "tables": []}

    if target == old_folder:
        # 目录名没变（账号恰好一致），也要保证字段和路径正确
        counts = {name: db_counts(target / name) for name in DB_FILES}
        report["counts"] = counts
        if not apply:
            return report
        for name in ACCOUNT_TABLES:
            path = target / name
            if path.is_file():
                report["tables"].extend(
                    "%s: %s" % (name, item) for item in
                    update_accounts(path, old_account, account))
        write_json(target / "config.json", fix_config(old_cfg, account, target))
        (target / "inbox").mkdir(exist_ok=True)
        return report

    report["counts"] = {name: db_counts(old_folder / name) for name in DB_FILES}
    if not apply:
        return report

    # 目标目录已有数据 → 先整体备份，再覆盖
    if target.exists() and any(target.iterdir()):
        keep = BACKUP / ("%s-pre-migrate-%s" % (stamp(), target.name))
        keep.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(target, keep)
        report["backup"] = keep
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    (target / "inbox").mkdir(exist_ok=True)

    for name in SESSION_FILES:
        src = old_folder / name
        if not src.is_file():
            continue
        if src.suffix == ".db":
            clone_db(src, target / name)
        elif name == "config.json":
            continue
        else:
            shutil.copy2(src, target / name)
    inbox = old_folder / "inbox"
    if inbox.is_dir() and any(inbox.iterdir()):
        for item in inbox.iterdir():
            dest = target / "inbox" / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

    for name in ACCOUNT_TABLES:
        path = target / name
        if path.is_file():
            report["tables"].extend(
                "%s: %s" % (name, item)
                for item in update_accounts(path, old_account, account))

    write_json(target / "config.json", fix_config(old_cfg, account, target))
    report["old_kept"] = old_folder
    return report


def pick_account(args) -> dict:
    if args.account:
        return {"account": args.account, "wxid": bare_wxid(args.account), "path": ""}
    try:
        rows = discover_accounts()
    except Exception as exc:  # 依赖缺失或微信没跑
        raise SystemExit("没法自动识别本机小号（%s: %s）。\n"
                         "先用 --account wxid_xxx_abcd 指定，"
                         "或在 GUI 里启动一次服务让目录生成为好。" % (type(exc).__name__, exc))
    if not rows:
        raise SystemExit("没有发现已登录的微信小号。请先登录小号并保持微信运行，"
                         "或用 --account 手动指定账号目录名。")
    if len(rows) > 1:
        print("发现多个小号，请用 --account 指定其中一个：")
        for row in rows:
            print("  %-28s wxid=%s" % (row["account"], row["wxid"]))
        raise SystemExit(1)
    return rows[0]


def cmd_adapt(args) -> int:
    session = find_session()
    if not session:
        raise SystemExit("没有找到旧会话目录（GUI/.runtime/*/queue.db）。")
    selected = pick_account(args)
    account, wxid = selected["account"], selected["wxid"]
    old_cfg = read_json(session / "config.json")
    old_account = str(old_cfg.get("account") or "")

    print("旧会话：%s" % session)
    print("旧 account：%s" % old_account)
    print("新 account：%s（wxid %s）" % (account, wxid))
    if old_account and bare_wxid(old_account) != wxid:
        if not args.allow_other_wxid:
            raise SystemExit(
                "旧数据属于 wxid %s，本机登录的是 %s，不是同一个小号。\n"
                "如果你确实要把 A 小号的记忆搬到 B 小号上，加 --allow-other-wxid。"
                % (bare_wxid(old_account), wxid))

    report = adapt_session(session, account, apply=args.apply)
    if not args.apply:
        if report.get("same_dir"):
            print("\n[预演] 账号一致，不需要搬目录；只会把库里的 account 字段和"
                  "配置里的知识库路径核对一遍。")
        else:
            print("\n[预演] 数据会从\n  %s\n搬到\n  %s" % (report["old"], report["new"]))
        for name, counts in report.get("counts", {}).items():
            brief = "、".join("%s %d" % kv for kv in counts.items() if kv[1])
            print("  %-14s %s" % (name, brief[:110] or "空"))
        print("\n确认无误后加 --apply 执行（会先备份目标目录）。")
        return 0

    print("\n已写入：%s" % report["new"])
    for item in report["tables"]:
        print("  %s" % item)
    if report.get("backup"):
        print("目标目录原有数据已备份到：%s" % report["backup"])
    if report.get("old_kept"):
        print("旧目录保留未删（确认新数据正常后可手动删除）：%s" % report["old_kept"])
    print("\n接着改根目录 config.json 的 account ...")
    root_cfg = read_json(ROOT / "config.json")
    if root_cfg:
        root_cfg["account"] = account
        root_cfg["token"] = ""
        write_json(ROOT / "config.json", root_cfg)
        print("  config.json → account=%s, token 置空（GUI 每次启动重发令牌）" % account)
    print("\n迁移完成。启动 GUI（GUI\\启动 Orange.bat），确认：人设卡、黑名单、"
          "主动聊天白名单、定时提醒、知识库检索都还在。")
    return 0


# ---------------------------------------------------------------- import
def cmd_import(args) -> int:
    src = Path(args.package)
    if not src.is_file():
        raise SystemExit("找不到迁移包：%s" % src)
    selected = pick_account(args)
    account, wxid = selected["account"], selected["wxid"]

    staging = BACKUP / ("import-%s" % stamp())
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(src) as zf:
            zf.extractall(staging)
        manifest = read_json(staging / "manifest.json")
        if manifest.get("kind") != "orange-migrate":
            raise SystemExit("这不是 migrate_device.py export 生成的迁移包。")
        old_wxid = str(manifest.get("wxid") or bare_wxid(str(manifest.get("account") or "")))
        if old_wxid and old_wxid != wxid and not args.allow_other_wxid:
            raise SystemExit("迁移包属于 wxid %s，本机登录的是 %s，不是同一个小号。"
                             "确要搬过去加 --allow-other-wxid。" % (old_wxid, wxid))

        target = session_dir_for(account)
        print("迁移包：%s（%s 导出）" % (src.name, manifest.get("created_text")))
        print("旧 account：%s  →  新 account：%s" % (manifest.get("account"), account))
        print("目标目录：%s" % target)
        data = staging / "data"

        if not args.apply:
            for name in DB_FILES:
                counts = db_counts(data / "runtime" / name)
                if counts:
                    print("  %-14s %s" % (name, "、".join(
                        "%s %d" % kv for kv in counts.items() if kv[1])[:110]))
            print("\n[预演] 加 --apply 才会写入。")
            return 0

        backup = BACKUP / ("%s-pre-import-%s" % (stamp(), target.name))
        if target.exists() and any(target.iterdir()):
            shutil.copytree(target, backup)
            print("目标目录已有数据，已备份到：%s" % backup)
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        (target / "inbox").mkdir(exist_ok=True)

        session_config = fix_config(without_credentials(read_json(data / "runtime" / "config.json")),
                                    account, target)
        for name in SESSION_FILES:
            item = data / "runtime" / name
            if name != "config.json" and item.is_file():
                shutil.copy2(item, target / name)
        inbox = data / "runtime" / "inbox"
        if inbox.is_dir():
            for item in inbox.iterdir():
                dest = target / "inbox" / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)

        # 项目级文件：配置、人设卡、风格样本
        for name in PROJECT_FILES:
            item = data / name
            if not item.is_file():
                continue
            if name == "config.json":
                current = read_json(ROOT / name)
                cfg = without_credentials(read_json(item))
                cfg["account"] = account
                cfg["token"] = current.get("token") or secrets.token_urlsafe(32)
                local_model = current.get("llm")
                model = cfg.get("llm")
                if (isinstance(local_model, dict) and isinstance(model, dict)
                        and local_model.get("base_url") == model.get("base_url")):
                    model["api_key"] = local_model.get("api_key") or ""
                write_json(ROOT / name, cfg)
                continue
            keep = ROOT / ("%s.bak-%s" % (name, stamp())) if (ROOT / name).is_file() else None
            if keep:
                shutil.copy2(ROOT / name, keep)
            shutil.copy2(item, ROOT / name)

        for name in PROJECT_DIRS:
            item = data / name
            if item.is_dir():
                shutil.copytree(item, ROOT / name, dirs_exist_ok=True)

        old_account = str(manifest.get("account") or "")
        touched = []
        for name in ACCOUNT_TABLES:
            path = target / name
            if path.is_file():
                touched.extend("%s: %s" % (name, x)
                               for x in update_accounts(path, old_account, account))
        write_json(target / "config.json", session_config)

        print("已写入：%s" % target)
        for item in touched:
            print("  %s" % item)
        if manifest.get("with_code") and (staging / "code").is_dir():
            print("\n需要包内代码时，从这个迁移包解压 code 目录：%s" % src.resolve())
        print("\n迁移完成。启动 GUI 检查人设卡、名单、提醒、知识库。")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


VERIFIED_WECHAT = "4.2.0.13"   # 本项目实际调通/验证的微信客户端版本


def wechat_version() -> str:
    """读本机微信客户端的文件版本（读不到返回空串）。"""
    try:
        import psutil
        import win32api
    except Exception:
        return ""
    for proc in psutil.process_iter(["name", "exe"]):
        name = (proc.info.get("name") or "").lower()
        if name not in ("weixin.exe", "wechat.exe"):
            continue
        exe = proc.info.get("exe") or ""
        if not exe:
            continue
        try:
            info = win32api.GetFileVersionInfo(exe, "\\")
            return "%d.%d.%d.%d" % (
                win32api.HIWORD(info["FileVersionMS"]), win32api.LOWORD(info["FileVersionMS"]),
                win32api.HIWORD(info["FileVersionLS"]), win32api.LOWORD(info["FileVersionLS"]))
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------- doctor
def cmd_doctor(_args) -> int:
    """新机器自检：依赖齐不齐、微信在不在、小号认不认得。"""
    ok = True

    def line(flag: bool, label: str, detail: str = "", fatal: bool = True):
        nonlocal ok
        print("%s %-26s %s" % ("[OK]  " if flag else "[缺]  ", label, detail))
        if not flag and fatal:
            ok = False

    bits = "64 位" if sys.maxsize > 2**32 else "32 位"
    print("Python：%s  (%d.%d.%d, %s)"
          % (sys.executable, *sys.version_info[:3], bits))
    ok312 = sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32
    line(ok312, "Python 3.12 64 位",
         "符合" if ok312 else "pylibs 里的 .pyd 是 cp312-win_amd64，必须 3.12 64 位"
         "（Anaconda 或官方版都行）")

    lib_paths = [Path(os.environ.get("WXBOT_PYLIB") or ROOT / "pylibs"),
                 ROOT, ROOT / "wxbot", ROOT / "agent"]
    for path in lib_paths:
        if not path.is_dir():
            continue
        if path == lib_paths[0]:
            # 当 site 目录加载，pywin32.pth 才会把 win32\、win32\lib 接进来
            site.addsitedir(str(path))
        elif str(path) not in sys.path:
            sys.path.insert(0, str(path))
    pylibs = lib_paths[0]
    if pylibs.is_dir():
        line(True, "外部依赖目录 pylibs", str(pylibs))
    else:
        # 开源安装方式：依赖走 pip，不需要项目内的 pylibs
        print("[--]   外部依赖目录              未使用（依赖走 pip：pip install -r requirements.txt）")

    import importlib.util
    for mod in ("psutil", "PIL", "cryptography", "win32api", "uiautomation", "wechatauto"):
        spec = importlib.util.find_spec(mod)
        line(spec is not None, "依赖 %s" % mod, "可导入" if spec else "缺失")

    print("\n知识库文件解析（缺哪个只影响对应格式）：")
    for mod, ext in (("docx", "docx"), ("pypdf", "pdf"), ("openpyxl", "xlsx"),
                     ("pptx", "pptx")):
        spec = importlib.util.find_spec(mod)
        line(spec is not None, "解析器 %s" % mod, ".%s" % ext, fatal=False)

    print("\n微信与小号：")
    import subprocess
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
                         capture_output=True, text=True, errors="replace").stdout
    ver = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WeChat.exe", "/NH"],
                         capture_output=True, text=True, errors="replace").stdout
    running = "Weixin.exe" in (out or "") or "WeChat.exe" in (ver or "")
    line(running, "微信进程", "在跑" if running else "没检测到，先登录小号")
    client = wechat_version()
    if client:
        same = client == VERIFIED_WECHAT
        line(same, "微信版本", client + ("（已验证）" if same else
                                         "（项目验证版是 %s）" % VERIFIED_WECHAT),
             fatal=False)
        if not same:
            print("       同大版本的小版本通常能跑；跨大版本（4.1.x / 4.3.x）需要重新校准\n"
                  "       发送锚点与 UIA gate 地址，装回 %s 最稳。" % VERIFIED_WECHAT)
    else:
        line(False, "微信版本", "读不到（不影响迁移，装 %s 最稳）" % VERIFIED_WECHAT, fatal=False)
    if running:
        try:
            rows = discover_accounts()
        except Exception as exc:
            line(False, "识别本机小号", "%s: %s" % (type(exc).__name__, exc), fatal=False)
            rows = []
        if rows:
            for row in rows:
                print("       %-28s wxid=%s" % (row["account"], row["wxid"]))
        else:
            line(False, "识别本机小号", "没认出来（可能微信没完全登录）", fatal=False)
    print("\n%s" % ("自检通过，可以开始迁移。" if ok else "有必需项缺失，先补齐再迁移。"))
    return 0 if ok else 1


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(
        description="跨机器迁移 Orange 的人设/关系/名单/记忆/提醒/知识库")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="查看当前会话与各库规模").set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="新机器自检：依赖 / 微信 / 小号").set_defaults(
        func=cmd_doctor)

    p_export = sub.add_parser("export", help="旧机器：打包数据（可带代码）")
    p_export.add_argument("--out", help="输出 zip 路径，默认 backup/migrate-<时间>.zip")
    p_export.add_argument("--with-code", action="store_true", help="连项目代码一起打包")
    p_export.set_defaults(func=cmd_export)

    p_adapt = sub.add_parser("adapt", help="新机器：就地适配到本机新账号")
    p_adapt.add_argument("--account", help="指定本机账号目录名（默认自动识别）")
    p_adapt.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    p_adapt.add_argument("--allow-other-wxid", action="store_true",
                         help="允许把数据搬到另一个小号")
    p_adapt.set_defaults(func=cmd_adapt)

    p_import = sub.add_parser("import", help="新机器：从迁移包恢复")
    p_import.add_argument("package", help="migrate_device.py export 生成的 zip")
    p_import.add_argument("--account", help="指定本机账号目录名（默认自动识别）")
    p_import.add_argument("--apply", action="store_true", help="真正写入（默认只预演）")
    p_import.add_argument("--allow-other-wxid", action="store_true")
    p_import.set_defaults(func=cmd_import)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
