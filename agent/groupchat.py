# -*- coding: utf-8 -*-
"""群聊支持：@ 识别、发送者前缀清理、群成员显示名。

微信 4.x 的群消息正文会被 wechatauto 拼上发送者 wxid::

    wxid_example_bro:
    @小橘 出来吧 我的orange！

这里的 ``@小橘`` 后面跟的是 U+2005（四分之一空格），不是普通空格，所以用
普通 ``split(" ")`` 或 ``"@小橘 "`` 匹配会漏。另外把 wxid 前缀留在正文里
会让模型看到一串 wxid，也会写进记忆，所以统一剥掉。
"""

from __future__ import annotations

import re
import time

# 群消息正文开头的 "wxid_xxx:\n"
_SENDER_PREFIX = re.compile(r"^wxid_[0-9A-Za-z_]+:\s*\n")

# 微信用 U+2005 结束真实的 @。兼容空白分隔的旧文本，同时允许微信昵称内部有空格。
_AT_BOUNDARY = r"(?=\u2005|[\r\n]|\Z|[^\S\r\n](?![^@\r\n]*\u2005))"

# 兜底名字：配置里没写 at_names 时只用微信昵称，这里不预设具体名字
DEFAULT_AT_NAMES: tuple = ()


def strip_sender_prefix(content: str) -> str:
    """去掉群消息开头的 ``wxid_xxx:`` 行。"""
    return _SENDER_PREFIX.sub("", content or "", count=1)


def _pattern(names) -> re.Pattern | None:
    names = [n for n in (names or []) if n]
    if not names:
        return None
    names.sort(key=len, reverse=True)          # 长名字优先，避免部分匹配
    nicknames = "|".join(re.escape(name) for name in names)
    return re.compile(r"@(?:%s)%s" % (nicknames, _AT_BOUNDARY))


def at_names(cfg: dict, self_nick: str = "") -> list[str]:
    """可被 @ 的名字：配置里的 at_names + 自己的微信昵称。"""
    agent = (cfg or {}).get("agent", {}) or {}
    names = list(agent.get("at_names") or [])
    if self_nick:
        names.append(self_nick)
    out = []
    for n in names:
        n = (n or "").strip()
        if n and n not in out:
            out.append(n)
    return out


def mentions_me(content: str, names) -> bool:
    pat = _pattern(names)
    return bool(pat and pat.search(content or ""))


def strip_mention(content: str, names) -> str:
    """把 ``@名字`` 去掉并压掉多余空白，便于模型理解正文。"""
    pat = _pattern(names)
    if not pat:
        return content or ""
    text = pat.sub("", content or "")
    return re.sub(r"[ \t\u2005\u00a0\u3000]+", " ", text).strip()


def prepend_at(segments: list[str], display: str) -> list[str]:
    """群聊回复在第一条前面加 ``@名字``，让人看清在回谁。

    模型有时会自己写 ``@名字``，先剥掉再统一加，避免出现 ``@🥭 @🥭 ...``。
    """
    if not segments:
        return segments
    out = list(segments)
    first = (out[0] or "").strip()
    if first.startswith("@"):
        first = re.sub(r"^@[^\s\u2005\u00a0]+[\s\u2005\u00a0]*", "", first).strip()
    if not display or not first:
        out[0] = first or out[0]
        return out
    out[0] = "@%s %s" % (display, first)
    return out


def followup_active(last_ts: int, last_sender: str, sender: str,
                    window_minutes: float, now: float | None = None) -> bool:
    """被 @ 之后的一小段时间里，同一个人接着说话，就当成还在跟它聊。"""
    if not window_minutes or not last_ts or not last_sender:
        return False
    if not sender or sender != last_sender:
        return False
    now = time.time() if now is None else float(now)
    return now - float(last_ts) <= float(window_minutes) * 60


def pick_target(msgs: list[dict], names, window_minutes: float,
                last_ts: int = 0, last_by: str = "",
                now: float | None = None) -> tuple[str, bool]:
    """决定这一批群消息该回谁。

    返回 ``(target_wxid, explicit_at)``；target 为空表示这一批不该接。
    同一批里可能混着好几个人的话，只挑最近 @ 我的那个人。
    """
    if not msgs:
        return "", False
    mentioned = [m for m in msgs if mentions_me(m.get("content") or "", names)]
    if mentioned:
        return mentioned[-1].get("sender") or "", True
    sender = msgs[-1].get("sender") or ""
    if followup_active(last_ts, last_by, sender, window_minutes, now):
        return last_by, False
    return "", False


class GroupDirectory:
    """群成员显示名（备注优先），带 120 秒缓存。"""

    def __init__(self, db, ttl: int = 120):
        self.db = db
        self.ttl = ttl
        self._cache: dict[str, tuple[float, dict]] = {}

    def members(self, chatroom: str) -> dict[str, str]:
        now = time.time()
        hit = self._cache.get(chatroom)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        mapping: dict[str, str] = {}
        try:
            for m in self.db.get_group_members(chatroom) or []:
                uname = m.get("username")
                if not uname:
                    continue
                mapping[uname] = (m.get("remark") or m.get("nick_name") or uname)
        except Exception:
            mapping = {}
        self._cache[chatroom] = (now, mapping)
        return mapping

    def display(self, chatroom: str, wxid: str) -> str:
        if not wxid:
            return ""
        return self.members(chatroom).get(wxid, "")

    def group_name(self, chatroom: str) -> str:
        try:
            return self.db.group_id_to_name(chatroom) or ""
        except Exception:
            return ""


if __name__ == "__main__":
    import os
    import sys

    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raw = "wxid_example_bro:\n@小橘\u2005出来吧 我的orange！"
    print("原始:", repr(raw))
    print("去前缀:", repr(strip_sender_prefix(raw)))
    print("是否@我:", mentions_me(strip_sender_prefix(raw), ["小橘"]))
    print("去@后:", repr(strip_mention(strip_sender_prefix(raw), ["小橘"])))
