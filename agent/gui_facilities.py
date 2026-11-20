"""Operations used by the local desktop console."""
from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path

import gate
import kb
import persona_card
from wxqueue import Queue


def persona_request(data: dict | None = None) -> dict:
    """固定人设与关系表：控制台「固定人设」「关系」两页用。

    GET  → 当前生效的固定人设（来源 card / config）+ 关系表
    POST → action=save 保存固定人设；action=relations 保存整张关系表

    这两项是全局的（不按账号分），写在 persona_card.json 里，
    服务进程按 mtime 热更新，改完下一句话就生效，不用重启。
    """
    if data is None:
        card = persona_card.load_card(force=True) or {}
        text, source = persona_card.effective_persona(card)
        return {
            "ok": True,
            "fixed_persona": text,
            "source": source,
            "relations": persona_card.effective_relations(card),
            "path": persona_card.path_label(),
            "version": card.get("version"),
        }
    if not isinstance(data, dict):
        return {"ok": False, "error": "请求格式有误"}
    action = str(data.get("action") or "").strip()
    if action == "save":
        return persona_card.set_fixed_persona(data.get("fixed_persona"))
    if action == "relations":
        return persona_card.set_relations(data.get("items"))
    return {"ok": False, "error": "未知的人设操作"}


def pause_chat(account: str, data: dict) -> dict:
    chat = str(data.get("chat") or "").strip()
    # 群聊或真人私聊都行；微信号是自定义 alias 的联系人也算私聊
    if not (chat.endswith("@chatroom") or gate.is_private_chat(chat)):
        return {"ok": False, "error": "请选择联系人或群聊"}
    minutes = data.get("minutes", 30)
    if type(minutes) is not int or not 1 <= minutes <= 1440:
        return {"ok": False, "error": "暂停时长须为 1–1440 分钟"}
    q = Queue()
    q.pause_chat(chat, minutes=minutes, reason="manual")
    q.cancel_chat_drafts(chat)
    return {"ok": True, "chat": chat, "minutes": minutes}


def _ingest_roots(cfg: dict | None = None) -> list[str]:
    """允许导入知识库的目录。

    /kb 的 ingest 原先只校验「绝对路径 + 是文件」，等于给持 token 的调用方
    开了任意本机文件读（内容进库后还能用 search 取回来）。这里收窄到
    真正会放资料的地方，需要放别处时用 kb.ingest_roots 显式追加。
    """
    cfg = cfg if cfg is not None else kb.load_cfg()
    roots: list[str] = []
    try:
        roots.append(kb.inbox_dir())
    except Exception:
        pass
    cache = os.environ.get("WXBOT_MEDIA_CACHE")
    if cache:
        roots.append(cache)
    home = os.path.expanduser("~")
    for name in ("Desktop", "Documents", "Downloads", "桌面", "文档", "下载"):
        roots.append(os.path.join(home, name))
    extra = (cfg.get("kb") or {}).get("ingest_roots") or []
    if isinstance(extra, (list, tuple)):
        roots.extend(str(x) for x in extra if x)

    out: list[str] = []
    for item in roots:
        try:
            real = os.path.realpath(os.path.expanduser(str(item)))
        except (OSError, ValueError):
            continue
        if real and real not in out:
            out.append(real)
    return out


def _ingest_allowed(path: str, cfg: dict | None = None) -> bool:
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    for root in _ingest_roots(cfg):
        if real == root or real.startswith(root + os.sep):
            return True
    return False


def kb_request(data: dict | None = None) -> dict:
    cfg = kb.load_cfg()
    account = cfg.get("account")
    if (not account or account != os.environ.get("WXBOT_ACCOUNT")
            or not os.environ.get("WXBOT_CONFIG") or not os.environ.get("DSHWX_TOKEN")):
        return {"ok": False, "error": "请在桌面控制台中管理选定小号的知识库"}

    if data is None:
        with closing(kb.connect()) as conn:
            rows = conn.execute(
                "SELECT id, title, kind, scope, chat_id, checked_at, expires_at "
                "FROM kb_documents WHERE account=? AND revoked_at IS NULL "
                "ORDER BY id DESC LIMIT 200", (account,)
            ).fetchall()
            summary = kb.stats(conn)
        return {
            "ok": True,
            "enabled": bool(kb.kb_cfg(cfg).get("enabled")),
            "stats": summary, "items": [dict(row) for row in rows],
            "jobs": kb.jobs_state(25),
            "inbox": kb.inbox_dir(),
        }

    action = data.get("action")
    chat = str(data.get("chat") or "").strip()
    if action == "ingest":
        path = Path(str(data.get("path") or ""))
        if not path.is_absolute() or not path.is_file():
            return {"ok": False, "error": "请选择本机文件"}
        if not _ingest_allowed(str(path), cfg):
            return {"ok": False,
                    "error": "只能导入「桌面/文档/下载」或知识库投喂目录里的文件；"
                             "其他目录请加进 config.json 的 kb.ingest_roots"}
        return kb.ingest_file(str(path), scope="chat" if chat else "public",
                              chat_id=chat, account_=account)
    if action == "scan":
        return kb.scan_inbox()
    if action == "search":
        query = str(data.get("query") or "").strip()[:500]
        if not query:
            return {"ok": False, "error": "请输入搜索词"}
        return {"ok": True, "items": kb.search(query, chat_id=chat, account_=account, k=30)}
    if action == "forget":
        doc_id = data.get("id")
        with closing(kb.connect()) as conn:
            row = conn.execute("SELECT 1 FROM kb_documents WHERE id=? AND account=?",
                               (doc_id, account)).fetchone()
            if row:
                return kb.forget(doc_id, conn=conn)
        return {"ok": False, "error": "当前账号的知识库中没有该文档"}
    return {"ok": False, "error": "未知的知识库操作"}

