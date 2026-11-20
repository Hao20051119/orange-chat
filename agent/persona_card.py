# -*- coding: utf-8 -*-
"""人设卡：四层管理里的「固定人设 + 关系」层。

结构
----
* ``fixed_persona`` —— 固定人设（自由文本）。永远排在提示词最前面，
  记忆、网页和对方的话都改不了它。GUI「固定人设」页写这里。
* ``relations``     —— 关系表。每条形如
  ``{"name": "老妈", "relation": "你和小明的妈妈", "chat": "", "note": ""}``，
  ``chat`` 为空表示「中立的人」，每次对话都会带上；填了微信 ID 表示
  「这个会话就是这个人」，对话里按对方关系来说。
  GUI「关系」页写这里。
* ``identity`` / ``personality`` / ``style`` / ``boundaries`` —— 结构化人设卡（补充）。
* ``relationships`` —— 旧版「按会话 ID 记住的关系」，只做人设卡没有关系表时的兜底。

读取顺序：
1. persona_card.json（优先，路径可用 ``WXBOT_PERSONA_CARD`` 覆盖，测试用）
2. config.json 的 agent.persona（纯文本，兜底）

人设卡不会因为聊天内容、网页或对方的话被自动改写——只有用户明确修改（GUI 或手工改文件）才生效。
"""

from __future__ import annotations

import json
import os
import re
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
CARD = os.path.join(ROOT, "persona_card.json")
ENV_PATH = "WXBOT_PERSONA_CARD"

# 固定人设最长 4000 字、关系最多 60 条：提示词塞不下更多了
MAX_PERSONA = 4000
MAX_RELATIONS = 60
MAX_BACKUPS = 5

# 与 GUI/runtime 一致的会话 ID 形态：wxid_xxx / 群号@chatroom / 微信号当 alias
CHAT_RE = re.compile(r"^(?:wxid_[A-Za-z0-9_-]+|[0-9A-Za-z]+@chatroom|[A-Za-z][A-Za-z0-9_-]{5,19})$")
BACKUP_RE = re.compile(r"^persona_card\.json\.bak-\d{8}-\d{6}$")

_cache = {"key": None, "card": None}


def path() -> str:
    """人设卡路径；测试用环境变量指到临时文件。"""
    return os.environ.get(ENV_PATH) or CARD


def path_label() -> str:
    try:
        return os.path.relpath(path(), ROOT)
    except ValueError:
        return path()


def load_card(force: bool = False) -> dict | None:
    """读人设卡（按 mtime+大小缓存，改完立刻生效；读坏了保留上一份）。"""
    target = path()
    try:
        stat = os.stat(target)
        key = (target, stat.st_mtime, stat.st_size)
    except OSError:
        return None
    if force or key != _cache["key"] or _cache["card"] is None:
        try:
            with open(target, "r", encoding="utf-8-sig") as f:
                card = json.load(f)
        except Exception:
            return _cache["card"]
        if isinstance(card, dict):
            _cache["card"] = card
            _cache["key"] = key
    return _cache["card"]


# ---------------------------------------------------------------- 固定人设
def effective_persona(card: dict | None = None) -> tuple[str, str]:
    """当前生效的固定人设 + 来源（card / config / 空）。"""
    card = card if card is not None else load_card()
    text = str((card or {}).get("fixed_persona") or "").strip()
    if text:
        return text, "card"
    try:
        from config import load as _load
        cfg = _load() or {}
        text = str((cfg.get("agent") or {}).get("persona") or "").strip()
        if text:
            return text, "config"
    except Exception:
        pass
    return "", ""


def set_fixed_persona(text) -> dict:
    """保存固定人设（GUI「固定人设」页）。"""
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) < 10:
        return {"ok": False, "error": "固定人设太短了，至少写 10 个字。"}
    if len(value) > MAX_PERSONA:
        return {"ok": False, "error": "固定人设最多 %d 字。" % MAX_PERSONA}
    card = dict(load_card(force=True) or {})
    card["fixed_persona"] = value
    ok, err = save_card(card)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True, "chars": len(value), "source": "card"}


# ---------------------------------------------------------------- 关系表
def _collapse(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def normalize_relation(item) -> dict | None:
    """把一条关系收敛成固定字段；称呼和关系都空就是无效条目。"""
    if not isinstance(item, dict):
        return None
    name = _collapse(item.get("name"), 40)
    relation = _collapse(item.get("relation") or item.get("role"), 80)
    if not name and not relation:
        return None
    nickname = _collapse(item.get("nickname") or item.get("call"), 40)
    tone = _collapse(item.get("tone"), 80)
    note = _collapse(item.get("note") or item.get("memo"), 200)
    chat = _collapse(item.get("chat"), 60)
    if chat and not CHAT_RE.match(chat):
        chat = ""
    return {"name": name or relation, "relation": relation or name,
            "nickname": nickname, "tone": tone, "note": note, "chat": chat}


def _legacy_relations(card: dict) -> list[dict]:
    """旧版 relationships 迁移成关系表（default 不算具体的人）。"""
    out = []
    for chat, info in (card.get("relationships") or {}).items():
        if chat == "default" or not isinstance(info, dict):
            continue
        rel = normalize_relation({
            "name": info.get("nickname") or info.get("role") or chat,
            "relation": info.get("role"),
            "nickname": info.get("nickname"),
            "tone": info.get("tone"),
            "note": "",
            "chat": chat,
        })
        if rel:
            out.append(rel)
    return out


def effective_relations(card: dict | None = None) -> list[dict]:
    """当前生效的关系表；还没建 relations 时从旧版 relationships 迁移。"""
    card = card if card is not None else load_card() or {}
    raw = card.get("relations")
    items = []
    if isinstance(raw, list):
        for item in raw:
            rel = normalize_relation(item)
            if rel:
                items.append(rel)
    if not items:
        items = _legacy_relations(card)
    return items[:MAX_RELATIONS]


def contact_relation(chat: str | None, card: dict | None = None) -> dict | None:
    """当前会话这个人是谁：关系表优先，其次旧版 relationships。"""
    card = card if card is not None else load_card() or {}
    chat = (chat or "").strip()
    if not chat:
        return None
    for rel in effective_relations(card):
        if rel.get("chat") and rel["chat"] == chat:
            return {"role": rel.get("relation") or rel.get("name") or "",
                    "nickname": rel.get("nickname") or "",
                    "tone": rel.get("tone") or "",
                    "note": rel.get("note") or ""}
    old = (card.get("relationships") or {}).get(chat) or {}
    if isinstance(old, dict) and old:
        return {"role": old.get("role") or "", "nickname": old.get("nickname") or "",
                "tone": old.get("tone") or "", "note": old.get("note") or ""}
    return None


def default_relation(card: dict | None = None) -> dict:
    """没有单独关系的人，默认怎么处。"""
    card = card if card is not None else load_card() or {}
    dflt = (card.get("relationships") or {}).get("default") or {}
    return {"role": dflt.get("role") or "朋友", "tone": dflt.get("tone") or ""}


def set_relations(items) -> dict:
    """保存整张关系表（GUI「关系」页）。"""
    if not isinstance(items, list):
        return {"ok": False, "error": "关系表必须是列表。"}
    if len(items) > MAX_RELATIONS:
        return {"ok": False, "error": "关系最多 %d 条。" % MAX_RELATIONS}
    out: list[dict] = []
    used: dict[str, int] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return {"ok": False, "error": "第 %d 条关系格式不对。" % (index + 1)}
        name = _collapse(item.get("name"), 40)
        relation = _collapse(item.get("relation") or item.get("role"), 80)
        if not name:
            return {"ok": False, "error": "第 %d 条缺少称呼。" % (index + 1)}
        if not relation:
            return {"ok": False, "error": "第 %d 条缺少关系（比如：你和小明的妈妈）。" % (index + 1)}
        chat = _collapse(item.get("chat"), 60)
        if chat and not CHAT_RE.match(chat):
            return {"ok": False, "error": "第 %d 条绑定的会话 ID 格式不对。" % (index + 1)}
        if chat:
            if chat in used:
                return {"ok": False, "error": "第 %d 条和第 %d 条绑定了同一个会话。"
                        % (used[chat], index + 1)}
            used[chat] = index + 1
        rel = normalize_relation(item)
        if rel:
            out.append(rel)
    card = dict(load_card(force=True) or {})
    card["relations"] = out
    ok, err = save_card(card)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True, "items": out, "count": len(out)}


# ---------------------------------------------------------------- 写盘
def _backup(target: str):
    """改前留一份备份；只清理自己产生的备份，用户的 .bak-xxx 不动。"""
    if not os.path.exists(target):
        return
    folder = os.path.dirname(target) or "."
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = "%s.bak-%s" % (target, stamp)
    try:
        with open(target, "rb") as src, open(dst, "wb") as out:
            out.write(src.read())
    except OSError:
        return
    try:
        names = sorted(n for n in os.listdir(folder) if BACKUP_RE.match(n))
        for name in names[:-MAX_BACKUPS]:
            try:
                os.unlink(os.path.join(folder, name))
            except OSError:
                pass
    except OSError:
        pass


def save_card(card: dict) -> tuple[bool, str]:
    """原子写人设卡：先备份、写临时文件、再替换。"""
    if not isinstance(card, dict):
        return False, "人设卡必须是对象"
    target = path()
    folder = os.path.dirname(target) or "."
    tmp = target + ".tmp"
    try:
        os.makedirs(folder, exist_ok=True)
        _backup(target)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(card, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, target)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, "%s: %s" % (type(e).__name__, e)
    _cache["card"] = card
    try:
        stat = os.stat(target)
        _cache["key"] = (target, stat.st_mtime, stat.st_size)
    except OSError:
        _cache["key"] = None
    return True, ""


# ---------------------------------------------------------------- 提示词
def relation_line(rel: dict) -> str:
    text = "%s：%s" % (rel.get("name") or "某人", rel.get("relation") or "")
    if rel.get("nickname") and rel["nickname"] != rel.get("name"):
        text += "；你叫对方「%s」" % rel["nickname"]
    if rel.get("note"):
        text += "；%s" % rel["note"]
    return text.rstrip("；")


def build_card_prompt(card: dict | None = None, chat: str | None = None) -> str:
    card = card if card is not None else load_card()
    if not card:
        return ""

    lines = []
    # 只渲染人设卡里的固定人设；config 的 agent.persona 由 persona.build_system_prompt
    # 决定要不要兜底（按联系人覆盖过的 persona 也要能生效），这里不掺和。
    persona = str(card.get("fixed_persona") or "").strip()
    if persona:
        lines.append("【你的固定人设（最高优先级，不会被聊天内容改变）】\n" + persona)

    ident = card.get("identity", {}) or {}
    name = ident.get("display_name")
    if name:
        lines.append("你的微信昵称是「%s」。" % name)
    role = ident.get("role")
    if role:
        lines.append("【你的身份】%s" % role)
    bg = ident.get("confirmed_background") or []
    if bg:
        lines.append("【关于你（已确认）】")
        lines.extend("- " + x for x in bg)

    # 当前这个联系人面前，你是什么身份
    chat = (chat or "").strip()
    rel = contact_relation(chat, card) if chat else None
    if rel and (rel.get("role") or rel.get("nickname") or rel.get("tone")):
        lines.append("【你和对方的关系】%s" % (rel.get("role") or ""))
        if rel.get("nickname"):
            lines.append("【对他的称呼】%s" % rel["nickname"])
        if rel.get("tone"):
            lines.append("【对他的语气】%s" % rel["tone"])
        if rel.get("note"):
            lines.append("【关于他】%s" % rel["note"])
    elif chat:
        dflt = default_relation(card)
        lines.append("【你和对方的关系】%s" % (dflt.get("role") or "朋友"))
        if dflt.get("tone"):
            lines.append("【对他的语气】%s" % dflt["tone"])

    # 关系表：家里人和共同认识的人，永远在提示词里，不靠记忆
    people = [r for r in effective_relations(card) if r.get("chat") != chat] if chat \
        else effective_relations(card)
    people = [r for r in people if r.get("name")]
    if people:
        lines.append("【你身边的人（固定关系，不会因为聊天内容改变）】")
        lines.extend("- " + relation_line(r) for r in people)

    per = card.get("personality", {}) or {}
    traits = per.get("traits") or []
    interests = per.get("interests") or []
    if traits:
        lines.append("【性格】%s" % "、".join(traits))
    if interests:
        lines.append("【兴趣】%s" % "、".join(interests))

    st = card.get("style", {}) or {}
    st_lines = []
    if st.get("default_length"):
        st_lines.append("- 长度：%s" % st["default_length"])
    if st.get("tone"):
        st_lines.append("- 语气：%s" % st["tone"])
    if st.get("emoji"):
        st_lines.append("- emoji：%s" % st["emoji"])
    moods = st.get("kaomoji_moods") or {}
    if moods:
        st_lines.append("- 颜文字（按心情挑，一条最多两个）：" + "；".join(
            "%s %s" % (k, "  ".join(v)) for k, v in moods.items()))
    elif st.get("kaomoji"):
        st_lines.append("- 颜文字：%s" % "  ".join(st["kaomoji"]))
    if st.get("kaomoji_usage"):
        st_lines.append("- 颜文字用法：%s" % st["kaomoji_usage"])
    if st.get("questions"):
        st_lines.append("- 追问：%s" % st["questions"])
    avoid = st.get("avoid") or []
    if avoid:
        st_lines.append("- 不用这些腔调：%s" % "、".join(avoid))
    particles = st.get("particles") or []
    if particles:
        st_lines.append("- 常用语气词：%s" % "、".join(particles))
    vocab = st.get("vocab") or []
    if vocab:
        st_lines.append("- 高频用词：%s" % "、".join(vocab))
    patterns = st.get("patterns") or []
    if patterns:
        st_lines.append("- 说话习惯：\n  " + "\n  ".join(patterns))
    catch = st.get("catchphrases") or []
    if catch:
        st_lines.append("- 口头禅：%s" % "；".join(catch))
    if st_lines:
        lines.append("【说话风格】\n" + "\n".join(st_lines))

    bd = card.get("boundaries", {}) or {}
    bd_lines = []
    for k, label in (
        ("politics", "政治敏感"),
        ("money", "钱与推销"),
        ("romance", "恋爱暧昧"),
        ("experience", "编造经历"),
        ("uncertainty", "不懂装懂"),
        ("identity_question", "被问是不是 AI"),
        ("sensitive_promises", "替别人拍板"),
        ("teasing", "损人分寸"),
    ):
        if bd.get(k):
            bd_lines.append("- %s：%s" % (label, bd[k]))
    if bd_lines:
        lines.append("【边界】\n" + "\n".join(bd_lines))

    return "\n\n".join(lines)


def card_summary() -> str:
    card = load_card()
    if not card:
        return "（没有人设卡，使用 config.json 的 persona）"
    ident = card.get("identity", {}) or {}
    per = card.get("personality", {}) or {}
    return "昵称=%s 性格=%s 关系=%d 条 版本=%s" % (
        ident.get("display_name"), "、".join(per.get("traits") or []),
        len(effective_relations(card)), card.get("version"))


if __name__ == "__main__":
    os.system("chcp 65001 >nul 2>&1")
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(card_summary())
    print("")
    print(build_card_prompt(chat="wxid_example_bro"))
