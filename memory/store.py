# -*- coding: utf-8 -*-
"""记忆模块：每个联系人一条独立的长期记忆线。

三层结构
--------
1. **原文窗口**  —— 最近 N 条对话，原样喂给模型（保证即时连贯）
2. **滚动摘要**  —— 老对话压缩成「这人是谁 + 聊过什么 + 关系基调」
3. **画像**      —— 摘要累积后提炼出的稳定印象，长期不变

超出窗口的老消息会被摘要后删除，所以库不会无限膨胀，但关系不会断。
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
DB_PATH = os.environ.get("WXBOT_MEMORY_DB") or os.path.join(BASE, "memory.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat    TEXT    NOT NULL,
    role    TEXT    NOT NULL,          -- user / assistant
    content TEXT    NOT NULL,
    ts      INTEGER NOT NULL,
    src_key TEXT,                      -- 幂等键：同一条消息只写一次
    sender  TEXT                       -- 群聊里是谁说的（显示名）
);
CREATE INDEX IF NOT EXISTS idx_chat_ts ON messages(chat, ts);

CREATE TABLE IF NOT EXISTS profiles (
    chat       TEXT PRIMARY KEY,
    summary    TEXT,                   -- 滚动摘要（谁 + 聊过什么 + 关系基调）
    portrait   TEXT,                   -- 稳定画像（提炼后的固有人设印象）
    turns      INTEGER DEFAULT 0,      -- 累计轮次
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS facts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat    TEXT NOT NULL,
    fact    TEXT NOT NULL,             -- 关于这个人的事实（生日/工作/口味…）
    ts      INTEGER NOT NULL,
    topic   TEXT,                      -- 事实主题（职业/住址/忌口…），用于覆盖旧值
    active  INTEGER NOT NULL DEFAULT 1,-- 0 = 已被更新的说法取代
    src     TEXT                       -- 来源（消息摘要等）
);
CREATE INDEX IF NOT EXISTS idx_facts_chat ON facts(chat);

CREATE TABLE IF NOT EXISTS media_desc (
    md5   TEXT PRIMARY KEY,
    desc  TEXT,
    ts    INTEGER
);

-- 长期观察：按聊天慢慢适应对方的说话方式/情绪/雷区
CREATE TABLE IF NOT EXISTS adapt (
    chat        TEXT PRIMARY KEY,
    notes       TEXT,
    seen        INTEGER NOT NULL DEFAULT 0,   -- 上次观察时的消息条数
    updated_ts  INTEGER
);
"""

# 旧库迁移：缺列就补
MIGRATIONS = (
    "ALTER TABLE facts ADD COLUMN topic TEXT",
    "ALTER TABLE facts ADD COLUMN active INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE facts ADD COLUMN src TEXT",
    "ALTER TABLE messages ADD COLUMN src_key TEXT",
    "ALTER TABLE messages ADD COLUMN sender TEXT",
)

EXTRA_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_src "
    "ON messages(chat, src_key) WHERE src_key IS NOT NULL",
)


class MemoryStore:
    persistent = True

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        folder = os.path.dirname(db_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._init()

    @contextlib.contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:                 # 正常退出提交、异常回滚
                yield c
        finally:
            c.close()               # 显式关闭，避免 Windows 文件占用

    def _init(self):
        with self._conn() as c:
            c.executescript(SCHEMA)
            for sql in MIGRATIONS:
                try:
                    c.execute(sql)
                except sqlite3.Error:
                    pass
            for sql in EXTRA_INDEXES:
                try:
                    c.execute(sql)
                except sqlite3.Error:
                    pass

    # ------------------------------------------------------------------ 写
    def add(self, chat: str, role: str, content: str, ts: int | None = None,
            src_key: str | None = None, sender: str | None = None) -> int:
        """写一条对话；``src_key`` 相同只写一次（重生成/重启不重复）。"""
        ts = int(ts or time.time())
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO messages(chat, role, content, ts, src_key, sender) "
                "VALUES(?,?,?,?,?,?)",
                (chat, role, content, ts, src_key, sender or ""),
            )
            if cur.rowcount == 0:
                return 0
            c.execute(
                "INSERT INTO profiles(chat, turns, updated_at) VALUES(?,1,?) "
                "ON CONFLICT(chat) DO UPDATE SET turns = turns + 1, updated_at = excluded.updated_at",
                (chat, ts),
            )
            return int(cur.lastrowid)

    # ------------------------------------------------------------------ 媒体描述缓存
    def media_desc(self, md5: str) -> str | None:
        if not md5:
            return None
        with self._conn() as c:
            r = c.execute("SELECT desc FROM media_desc WHERE md5=?", (md5,)).fetchone()
        return r["desc"] if r else None

    def save_media_desc(self, md5: str, desc: str):
        if not md5 or not desc:
            return
        with self._conn() as c:
            c.execute(
                "INSERT INTO media_desc(md5, desc, ts) VALUES(?,?,?) "
                "ON CONFLICT(md5) DO UPDATE SET desc=excluded.desc, ts=excluded.ts",
                (md5, desc[:300], int(time.time())),
            )

    def add_fact(self, chat: str, fact: str, topic: str | None = None,
                 src: str | None = None) -> dict:
        """写入一条事实；与旧事实冲突时把旧的标记为失效（不删，可追溯）。

        冲突判定：同一 topic，或文本高度相似但内容不同（如「职业是学生」→「职业是老师」）。
        """
        from difflib import SequenceMatcher

        fact = (fact or "").strip()
        if not fact:
            return {"ok": False, "reason": "空事实"}

        superseded = []
        with self._conn() as c:
            olds = c.execute(
                "SELECT id, fact, topic FROM facts WHERE chat=? AND active=1", (chat,)
            ).fetchall()
            for o in olds:
                same_topic = bool(topic) and (o["topic"] or "") == topic
                ratio = SequenceMatcher(None, o["fact"], fact).ratio()
                # 只有「同一 topic」或「几乎同一句的复述」才算新说法。
                # 早先用 ratio>=0.55，会把「他喜欢猫」和「他喜欢狗」判成一件事、
                # 把前一条置为失效 —— 并列事实互相删除，记忆越聊越少。
                conflict = same_topic or (ratio >= 0.90 and o["fact"] != fact)
                if conflict:
                    c.execute("UPDATE facts SET active=0 WHERE id=?", (o["id"],))
                    superseded.append(o["fact"])

            # 完全重复就不重复插入
            dup = c.execute(
                "SELECT id FROM facts WHERE chat=? AND fact=? AND active=1", (chat, fact)
            ).fetchone()
            if dup:
                return {"ok": True, "duplicated": True, "superseded": superseded}

            c.execute(
                "INSERT INTO facts(chat, fact, ts, topic, active, src) VALUES(?,?,?,?,1,?)",
                (chat, fact, int(time.time()), topic, src),
            )
        return {"ok": True, "superseded": superseded}

    # ------------------------------------------------------------------ 长期观察
    def get_adapt(self, chat: str) -> dict:
        """该会话的"对方习惯"观察笔记（模型每隔一段时间更新一次）。"""
        with self._conn() as c:
            row = c.execute("SELECT * FROM adapt WHERE chat=?", (chat,)).fetchone()
        return dict(row) if row else {"chat": chat, "notes": "", "seen": 0,
                                      "updated_ts": 0}

    def save_adapt(self, chat: str, notes: str, seen: int):
        with self._conn() as c:
            c.execute(
                "INSERT INTO adapt(chat, notes, seen, updated_ts) VALUES(?,?,?,?) "
                "ON CONFLICT(chat) DO UPDATE SET notes=excluded.notes, "
                "seen=excluded.seen, updated_ts=excluded.updated_ts",
                (chat, (notes or "")[:800], int(seen or 0), int(time.time())),
            )

    def chats_needing_adapt(self, every: int = 30, limit: int = 10) -> list[dict]:
        """哪些会话攒够了新消息、可以更新一次"对方习惯"观察。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT m.chat AS chat, COUNT(*) AS n, COALESCE(a.seen, 0) AS seen "
                "FROM messages m LEFT JOIN adapt a ON a.chat = m.chat "
                "GROUP BY m.chat HAVING n - seen >= ? ORDER BY n DESC LIMIT ?",
                (int(every), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 读
    def recent(self, chat: str, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT role, content, ts, sender FROM messages "
                "WHERE chat=? ORDER BY id DESC LIMIT ?",
                (chat, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def count(self, chat: str) -> int:
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM messages WHERE chat=?", (chat,)).fetchone()
        return row["n"] if row else 0

    def profile(self, chat: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT * FROM profiles WHERE chat=?", (chat,)).fetchone()
        return dict(row) if row else {"chat": chat, "summary": "", "portrait": "",
                                      "turns": 0, "updated_at": 0}

    def facts(self, chat: str, limit: int = 30) -> list[str]:
        """只返回仍有效的事实（被新说法取代的不再作为现状）。

        同一 topic 只留最新一条，剩下的名额按新旧补满。早先是直接
        ``ORDER BY id DESC LIMIT ?``，同一主题的历史版本会把这个名额占满，
        生日、忌口这类早写下的事实就永远进不了上下文。
        """
        limit = max(1, int(limit or 30))
        with self._conn() as c:
            rows = c.execute(
                "SELECT fact, topic FROM facts WHERE chat=? AND active=1 ORDER BY id DESC",
                (chat,),
            ).fetchall()
        out: list[str] = []
        seen_topic: set[str] = set()
        for r in rows:
            topic = (r["topic"] or "").strip()
            if topic:
                if topic in seen_topic:
                    continue
                seen_topic.add(topic)
            out.append(r["fact"])
            if len(out) >= limit:
                break
        return out

    def all_chats(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT chat, summary, portrait, turns, updated_at FROM profiles "
                "ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 上下文
    def build_context(self, chat: str, persona: str, limit: int = 20) -> list[dict]:
        """组装给模型的 messages：人设 + 记忆 + 最近原文。"""
        p = self.profile(chat)
        parts = [persona.strip()]

        if p.get("portrait"):
            parts.append("【你记得的关于这个人的印象】\n" + p["portrait"])
        if p.get("summary"):
            parts.append("【你们之前的聊天摘要】\n" + p["summary"])

        fs = self.facts(chat)
        if fs:
            parts.append("【你记得的关于对方的事实】\n" + "\n".join("- " + f for f in fs))

        sys_prompt = "\n\n".join(x for x in parts if x)

        msgs = [{"role": "system", "content": sys_prompt}]
        for m in self.recent(chat, limit=limit):
            role = "assistant" if m["role"] == "assistant" else "user"
            msgs.append({"role": role, "content": m["content"]})
        return msgs

    # ------------------------------------------------------------------ 压缩
    def should_summarize(self, chat: str, threshold: int = 60) -> bool:
        return self.count(chat) > threshold

    def chats_needing_summary(self, threshold: int = 60, limit: int = 20) -> list[dict]:
        """后台补偿扫描：哪些会话的原文已经超过阈值却还没摘要。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT chat, COUNT(*) AS n FROM messages GROUP BY chat "
                "HAVING n > ? ORDER BY n DESC LIMIT ?", (int(threshold), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def summarize(self, chat: str, llm_chat, keep_recent: int = 20,
                  batch: int = 40) -> dict:
        """把最老的一批消息压进摘要，然后删掉它们。

        llm_chat: 可调用对象，签名 llm_chat(messages) -> {"ok","text"}
        """
        n = self.count(chat)
        if n <= keep_recent:
            return {"ok": False, "reason": "消息数 %d 未超过保留数 %d" % (n, keep_recent)}

        # 只能处理「超出保留数」的那部分；绝不能一次性把最近 keep_recent 条也删掉
        take = max(1, min(batch, n - keep_recent))
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, role, content FROM messages WHERE chat=? ORDER BY id ASC LIMIT ?",
                (chat, take),
            ).fetchall()
        if not rows:
            return {"ok": False, "reason": "没有可摘要的消息"}

        lines = []
        for r in rows:
            who = "我" if r["role"] == "assistant" else "对方"
            lines.append("%s：%s" % (who, (r["content"] or "").replace("\n", " ")[:300]))
        convo = "\n".join(lines)

        old = self.profile(chat)
        prompt = (
            "下面是一段微信聊天记录（我是「我」，对方是「对方」）。"
            "请把它压缩成简洁的中文记忆，供以后继续聊天时参考。\n\n"
            "已有摘要（可能为空）：\n%s\n\n"
            "新对话：\n%s\n\n"
            "请输出三段，格式严格如下：\n"
            "【摘要】用不超过 150 字概括：对方是谁、聊了什么、有哪些未完成的事或约定。\n"
            "【画像】用不超过 80 字给出对对方稳定性格/关系的印象；没把握就写「暂无」。\n"
            "【事实】逐行列出以后还用得上的稳定事实，每行一条，最多 8 条。"
            "两类都要写：①关于对方本人的（忌口、生日、职业、喜好、重要日期等）；"
            "②两人共同认识的人（家里人、共同的朋友、同事），写成"
            "「老妈的称呼是老妈，是对方和我的妈妈」这样，把是谁的什么人写清楚——"
            "这类关系一旦漏掉，以后就会说「不认识」。一条都没有就写「无」。"
            % (old.get("summary") or "（空）", convo)
        )

        r = llm_chat([
            {"role": "system", "content": "你是一个精准的对话记忆压缩器。只输出要求的三段内容，"
                                          "不要额外解释，不要用 Markdown 标题。"},
            {"role": "user", "content": prompt},
        ])
        if not r.get("ok"):
            return {"ok": False, "reason": r.get("error") or "模型调用失败"}

        text = r.get("text") or ""
        summary, portrait, facts = self._parse_summary(text)

        # 校验：必须真的解析出【摘要】段，否则视为格式无效（保留原文）
        if "【摘要】" not in text:
            return {"ok": False, "reason": "摘要格式无效（缺【摘要】段），已保留原文",
                    "raw": text[:200]}
        if not summary.strip():
            return {"ok": False, "reason": "摘要为空或格式无效，已保留原文",
                    "raw": text[:200]}

        ids = [x["id"] for x in rows]
        # 事务前先算好「哪些旧事实会被新说法取代」，事务内一起提交
        from difflib import SequenceMatcher
        try:
            with self._conn() as c:
                olds = c.execute(
                    "SELECT id, fact FROM facts WHERE chat=? AND active=1", (chat,)
                ).fetchall()
                to_supersede = []
                for o in olds:
                    for f in facts:
                        ratio = SequenceMatcher(None, o["fact"], f).ratio()
                        if o["fact"] != f and ratio >= 0.55:
                            to_supersede.append(o["id"])
                            break

                c.execute("DELETE FROM messages WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
                c.execute(
                    "UPDATE profiles SET summary=?, portrait=?, updated_at=? WHERE chat=?",
                    (summary, portrait or old.get("portrait") or "", int(time.time()), chat),
                )
                for oid in set(to_supersede):
                    c.execute("UPDATE facts SET active=0 WHERE id=?", (oid,))
                for f in facts:
                    dup = c.execute(
                        "SELECT id FROM facts WHERE chat=? AND fact=? AND active=1",
                        (chat, f)).fetchone()
                    if dup:
                        continue
                    c.execute(
                        "INSERT INTO facts(chat, fact, ts, topic, active, src) "
                        "VALUES(?,?,?,?,1,?)",
                        (chat, f, int(time.time()), None, "summary"),
                    )
        except Exception as e:
            return {"ok": False, "reason": "提交失败，原文已保留: %s: %s"
                    % (type(e).__name__, e)}
        return {"ok": True, "summarized": len(ids), "summary": summary,
                "portrait": portrait, "facts": facts, "remaining": n - len(ids)}

    @staticmethod
    def _parse_summary(text: str) -> tuple[str, str, list[str]]:
        summary, portrait = "", ""
        facts: list[str] = []
        cur = None
        for line in (text or "").splitlines():
            s = line.strip()
            if not s:
                continue
            if "【摘要】" in s:
                cur = "s"
                s = s.split("【摘要】", 1)[1].strip()
            elif "【画像】" in s:
                cur = "p"
                s = s.split("【画像】", 1)[1].strip()
            elif "【事实】" in s:
                cur = "f"
                s = s.split("【事实】", 1)[1].strip()
            if cur == "s":
                summary += s
            elif cur == "p":
                portrait += s
            elif cur == "f":
                item = s.lstrip("-·• 　").strip()
                if item and item not in ("无", "暂无", "没有"):
                    facts.append(item)
            elif s:
                summary += s
        if not summary and not portrait:
            summary = text.strip()
        return summary.strip()[:600], portrait.strip()[:300], facts[:8]


if __name__ == "__main__":
    os.system("chcp 65001 >nul 2>&1")
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    m = MemoryStore()
    print("库：", m.db_path)
    for c in m.all_chats()[:10]:
        print("  会话 %s：%d 轮  摘要=%s" % (
            c["chat"], c["turns"], (c["summary"] or "")[:40]))
