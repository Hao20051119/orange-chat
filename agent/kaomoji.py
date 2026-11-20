# -*- coding: utf-8 -*-
"""颜文字轮换：别让 orange 每条都挂同一个 (￣▽￣)。

为什么需要它
------------
人设卡里已经有 8 组、每组分门别类的颜文字，但模型有个稳定毛病：提示词里
只要出现过具体例子（比如"得意/坏笑用 (￣▽￣) (¬‿¬)"），它就会死死抓住第一个
反复用。实测 20 条带颜文字的回复里 13 条是 ``(￣▽￣)``——扩大列表没用，因为
模型不是"没得挑"，是"照抄提示词里的那个"。

所以这里不再指望模型自觉，改成两层：

1. 提示词只给规则、不点名具体颜文字；再把「最近用过的」列给模型看；
2. 生成后做**确定性轮换**：回复里的颜文字只要最近出现过，就换成同心情组里
   最近没用过的那个。只替换完整的颜文字 token，不动句中其他文字。
"""
from __future__ import annotations

import random
import re

def load_groups(force: bool = False) -> dict[str, list[str]]:
    """读人设卡里的心情分组：{"开心": [...], "得意坏笑": [...]}。

    不在这里做缓存：``persona_card.load_card`` 自己按 mtime 缓存，用户改完
    persona_card.json 立刻就能生效。
    """
    groups: dict[str, list[str]] = {}
    try:
        from persona_card import load_card
        st = (load_card(force) or {}).get("style") or {}
        moods = st.get("kaomoji_moods") or {}
        for mood, items in moods.items():
            vals = [str(x).strip() for x in (items or []) if str(x or "").strip()]
            if vals:
                groups[str(mood)] = vals
    except Exception:
        groups = {}
    return groups


def mood_index(groups: dict[str, list[str]] | None = None) -> dict[str, str]:
    """颜文字 -> 心情。同一 token 出现在多组时，先出现的组为准。"""
    groups = groups if groups is not None else load_groups()
    flat: dict[str, str] = {}
    for mood, vals in groups.items():
        for v in vals:
            flat.setdefault(v, mood)
    return flat


def _pattern(groups: dict[str, list[str]] | None = None) -> re.Pattern | None:
    groups = groups if groups is not None else load_groups()
    tokens = [t for vals in groups.values() for t in vals]
    if not tokens:
        return None
    # 长的排前面：(￣▽￣)ゞ 必须先于 (￣▽￣) 匹配
    tokens.sort(key=len, reverse=True)
    return re.compile("|".join(re.escape(t) for t in tokens))


def scan(text: str, groups: dict[str, list[str]] | None = None) -> list[str]:
    """按出现顺序取出文本里所有"清单内"的颜文字。"""
    pat = _pattern(groups)
    if not pat or not text:
        return []
    return pat.findall(text)


def _assistant_texts(mem, chat: str, limit: int = 6) -> list[str]:
    """最近几条自己发过的话（旧 -> 新）。"""
    if mem is None or not chat:
        return []
    try:
        rows = mem.recent(chat, limit=limit) or []
    except Exception:
        return []
    return [str(m.get("content") or "") for m in rows
            if (m.get("role") or "") == "assistant"]


def recent_from_mem(mem, chat: str, limit: int = 8,
                    groups: dict[str, list[str]] | None = None) -> list[str]:
    """从记忆里取最近几条自己发过的话里用过的颜文字（旧 -> 新）。"""
    pat = _pattern(groups)
    if not pat:
        return []
    out: list[str] = []
    for txt in _assistant_texts(mem, chat, limit):
        out.extend(pat.findall(txt))
    return out


def frequency_hint(mem, chat: str, groups: dict[str, list[str]] | None = None) -> str:
    """带太多或太少时提醒一句，把"带不带"的节奏也管起来。"""
    texts = _assistant_texts(mem, chat, 6)
    if len(texts) < 3:
        return ""
    pat = _pattern(groups)
    if not pat:
        return ""
    has = [bool(pat.findall(t)) for t in texts]
    if len(has) >= 2 and has[-1] and has[-2]:
        return "刚连着两条都带颜文字了，这条先别带。"
    if len(has) >= 3 and not any(has[-3:]):
        return "最近几条都没带颜文字，这条可以在句尾自然地带一个（按心情挑）。"
    return ""


def usage_hint(mem, chat: str, groups: dict[str, list[str]] | None = None) -> str:
    """拼给模型的颜文字提示：别重复 + 该带/不该带的节奏。"""
    toks = recent_from_mem(mem, chat, limit=8, groups=groups)
    parts = []
    h = recent_hint(toks)
    if h:
        parts.append(h)
    f = frequency_hint(mem, chat, groups)
    if f:
        parts.append(f)
    return "\n".join(parts)


def pick_alt(cands: list[str], exclude: set, fallback: str,
             rng=random, recent: list[str] | None = None) -> str:
    """同心情组里挑一个没用过的。

    整组都轮过一遍后不再随机乱挑，而是挑**最久没出现过**的那个，这样长期
    看就是一个稳定的循环，不会连续两条挂同一个。
    """
    pool = [c for c in cands if c not in exclude]
    if pool:
        return rng.choice(pool)
    rest = [c for c in cands if c != fallback] or list(cands)
    if not rest:
        return fallback
    last: dict[str, int] = {}
    for i, t in enumerate(recent or []):
        last[t] = i
    oldest = min(last.get(c, -1) for c in rest)
    ties = [c for c in rest if last.get(c, -1) == oldest]
    return rng.choice(ties)


def rotate(reply: str, recent: list[str] | None = None,
           groups: dict[str, list[str]] | None = None, rng=random) -> tuple[str, dict]:
    """把"最近用过"的颜文字换成同组里没用过的，返回 (新文本, 说明)。"""
    groups = groups if groups is not None else load_groups()
    pat = _pattern(groups)
    if not pat or not reply:
        return reply, {"changed": False, "replaced": []}
    flat = mood_index(groups)
    recent = list(recent or [])
    recent_set = {t for t in recent if t}
    replaced: list[tuple[str, str]] = []
    used_now: set[str] = set()

    def _sub(m):
        tok = m.group(0)
        mood = flat.get(tok)
        if not mood or tok not in recent_set:
            return tok
        alt = pick_alt(groups.get(mood) or [], recent_set | used_now, tok,
                       rng, recent + list(used_now))
        if not alt or alt == tok:
            return tok
        used_now.add(alt)
        replaced.append((tok, alt))
        return alt

    out = pat.sub(_sub, reply)
    return out, {"changed": bool(replaced), "replaced": replaced}


def recent_hint(recent: list[str], limit: int = 6) -> str:
    """给模型的提醒：最近挂过哪些，这次换一个。"""
    seen: list[str] = []
    for t in reversed(recent or []):
        if t not in seen:
            seen.append(t)
        if len(seen) >= limit:
            break
    if not seen:
        return ""
    return ("【颜文字别重复】最近几条你已经用过：%s。这次要么不带，"
            "要么从对应心情里换一个没出现过的。" % " ".join(seen))
