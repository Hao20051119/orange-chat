# -*- coding: utf-8 -*-
"""把一条回复拆成几条微信气泡。

人设卡里写着「情绪一上来会拆成两三条发」，但以前整段（含空行）一次性发出，
看起来就像一段排版好的文章。这里只做确定性拆分，不调用模型：

1. 先按空行、再按换行拆；
2. 仍超长的段落按句末标点（。！？!?…~）拆；
3. 太短的碎片并回相邻气泡；
4. 最多 ``max_bubbles`` 段，多出来的并进最后一段。
"""

from __future__ import annotations

import re

_SENT_RE = re.compile(r"(?<=[。！？!?…~；;])")
_CLAUSE_RE = re.compile(r"(?<=[，,、：:])")


def _clean(s: str) -> str:
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t\u3000]+", " ", s)
    return "\n".join(line.strip() for line in s.split("\n")).strip()


def _chunks(block: str, max_chars: int) -> list[str]:
    """按句末标点切；单句仍超长时退到逗号切。"""
    if len(block) <= max_chars:
        return [block]

    def _split_by(pattern: str, text: str) -> list[str]:
        out, buf = [], ""
        for part in re.split(pattern, text):
            if not part:
                continue
            cand = buf + part
            if len(cand) > max_chars and buf:
                out.append(buf)
                buf = part
            else:
                buf = cand
        if buf:
            out.append(buf)
        return out

    pieces = _split_by(_SENT_RE, block)
    result: list[str] = []
    for p in pieces:
        if len(p) > max_chars:
            result.extend(_split_by(_CLAUSE_RE, p))
        else:
            result.append(p)
    return result


def split_reply(text: str, max_chars: int = 40, max_bubbles: int = 3) -> list[str]:
    """返回 1..max_bubbles 个气泡文本；空输入返回 []。

    规则：换行/空行本身就是对方想分条的信号，**一条换行 = 一个气泡**；
    只有单行超过 ``max_chars`` 时才继续按句末标点、再按逗号拆。
    以前会把相邻的短气泡再合并回去（"在呀" + "怎么啦" → "在呀怎么啦"），
    等于白拆，所以这里不再做合并。
    """
    raw = _clean(text)
    if not raw:
        return []
    max_chars = max(8, int(max_chars or 40))
    max_bubbles = max(1, int(max_bubbles or 3))

    units: list[str] = []
    for para in re.split(r"\n{2,}", raw):
        para = para.strip()
        if not para:
            continue
        for line in para.split("\n"):
            line = line.strip()
            if not line:
                continue
            units.extend(_chunks(line, max_chars))

    # 只剩标点的碎片没有意义
    units = [u for u in (u.strip() for u in units)
             if u and u.strip(" 　。，、…~～!！?？-—")]

    if len(units) > max_bubbles:
        units = units[:max_bubbles - 1] + ["".join(units[max_bubbles - 1:])]
    return units[:max_bubbles]


if __name__ == "__main__":
    import os
    import sys
    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    samples = [
        "在的在的，怎么啦～（探头）\n\n哥你换新手机啦？哇好耶，快给我看看啥样！",
        "行",
        "今天上课好累啊，老师拖堂拖到十二点半，我饭都没吃上，饿死了真的，"
        "你那边怎么样呀，今天忙不忙？",
    ]
    for s in samples:
        print("原始:", repr(s))
        for i, b in enumerate(split_reply(s), 1):
            print("  气泡%d(%d字): %s" % (i, len(b), b))
        print()
