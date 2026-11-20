from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIBRARY = ROOT / "pylibs"
# 微信库密钥缓存目录（wechatauto 的工作缓存）：只有这里已经有 keys.json
# 的账号，连接页才去读 contact.db 拿昵称，绝不为了显示昵称去扫微信内存。
NICK_CACHE_ROOT = Path(tempfile.gettempdir()) / "wechatauto_db"


def _dependencies():
    for path in (ROOT / "wxbot", Path(os.environ.get("WXBOT_PYLIB") or DEFAULT_LIBRARY)):
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from wechatauto.db import WeChatDB, list_accounts
    from wx_engine import active_account_binding
    return WeChatDB, list_accounts, active_account_binding


def _account_path(account: str, path: str) -> Path:
    if not re.fullmatch(r"wxid_[a-zA-Z0-9_-]+", account or ""):
        raise RuntimeError("微信账号标识无效")
    candidate = Path(path)
    resolved = candidate.resolve()
    if candidate.name != account or resolved.name != account or not (resolved / "db_storage").is_dir():
        raise RuntimeError("选定账号的数据目录不可用")
    return resolved


def discover_accounts() -> list[dict]:
    """只枚举已由微信进程打开的账号目录，无需打开聊天数据库。"""
    _, list_accounts, active_binding = _dependencies()
    result = []
    for row in list_accounts():
        account = row.get("account") or ""
        try:
            directory = _account_path(account, row.get("path") or "")
            binding = active_binding(account, str(directory))
            if not binding:
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        wxid = row.get("wxid") or re.sub(r"_\w{4}$", "", account)
        nick = row.get("self_nick") or ""
        result.append({
            "account": account, "wxid": wxid, "nick": nick, "path": str(directory),
            # 没昵称时用纯 wxid：目录名末尾那 4 位哈希不该出现在界面上
            "label": f"{nick} · {wxid}" if nick else wxid,
        })
    return result


def open_bound_db(account: str, *, expected_wxid: str | None = None):
    """只打开明确选择、仍在线的账号，禁止驱动替换为其他账号。"""
    configured = os.environ.get("WXBOT_ACCOUNT")
    if not account or (configured and configured != account):
        raise RuntimeError("没有选定微信小号，或当前配置与启动账号不符")

    base, list_accounts, active_binding = _dependencies()
    selected_path = os.environ.get("WXBOT_ACCOUNT_DIR")
    if selected_path:
        candidates = [selected_path]
    else:
        candidates = [row["path"] for row in list_accounts() if row.get("account") == account]
    if len(candidates) != 1:
        raise RuntimeError("无法定位唯一的微信账号数据目录")
    directory = _account_path(account, candidates[0])
    binding = active_binding(account, str(directory))
    if not binding:
        raise RuntimeError("选定账号未登录，或无法核验它的微信窗口")

    class BoundWeChatDB(base):
        def _find_weixin_pids(self):
            if active_binding(account, str(directory)) == binding:
                return [binding[0]]
            return []

        def _try_other_accounts(self):
            return False

        def _select_account_by_keys(self, candidates):
            return False

        def _load_key_cache(self, path):
            cache = Path(path).resolve()
            if cache.name != account + ".json" and cache != Path(self.keys_file).resolve():
                return {}
            return super()._load_key_cache(path)

    db = BoundWeChatDB(db_dir=str(directory.parent), account=account)
    expected = expected_wxid or os.environ.get("WXBOT_ACCOUNT_WXID") or re.sub(r"_\w{4}$", "", account)
    if (db.account != account or Path(db.account_dir).resolve() != directory
            or db.wxid != expected or (db.get_self_info() or {}).get("username") != expected
            or active_binding(account, str(directory)) != binding):
        raise RuntimeError("实际微信账号与所选账号不一致，已停止绑定")
    return db


def read_nicks(accounts: list[dict]) -> dict[str, str]:
    """给连接页补昵称：只读「密钥已缓存」的账号，任何失败都留空。

    ``discover_accounts`` 刻意不开库（只枚举在线账号，秒回）；昵称只存在加密的
    contact.db 里，所以单独走这一趟，并且只复用已有密钥缓存——没缓存过密钥的
    账号直接跳过，不为一行昵称去扫微信内存。开库仍走 ``open_bound_db``，
    因此账号自愈/换库都被禁掉，读到的必定是所选账号自己的昵称。
    """
    out: dict[str, str] = {}
    for row in accounts or []:
        account = row.get("account") or ""
        if not account or not (NICK_CACHE_ROOT / account / "keys.json").is_file():
            continue
        wxid = row.get("wxid") or re.sub(r"_\w{4}$", "", account)
        try:
            db = open_bound_db(account, expected_wxid=wxid)
            nick = " ".join(str((db.get_self_info() or {}).get("nick_name") or "").split())
        except Exception:                     # noqa: BLE001 昵称只是显示用，读不到就留空
            continue
        if nick:
            out[account] = nick
    return out

