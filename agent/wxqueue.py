# -*- coding: utf-8 -*-
"""持久化队列：入站消息与出站回复任务。

设计要点
--------
* 入站：只做「读库 → 去重 → 入队」，不等待模型；状态
  ``pending / retry / done / skipped / expired / failed``。
* 游标不再用墙钟时间：水位取 inbound 里该会话的 ``max(sort_seq)``，
  由 ``wechatauto`` 的 ``sort_seq``（同秒多条可稳定排序）保证顺序。
* 出站状态机::

      draft ──> sending ──> sent / failed / uncertain
        │
        ├──> hold（敏感/只出草稿/时段外，等人工处理）
        ├──> cancelled（关自动回复 / 接管 / 拒绝）
        ├──> superseded（对方在发出前又补了话，这条作废，内容并入下一轮）
        └──> expired（草稿太旧）

* ``sending`` 带 ``updated_ts``，进程崩溃后由 ``recover_inflight()`` 转
  ``uncertain``，绝不盲目重发。
* 长回复按 ``segments`` 分段发送，``segments_sent`` 记录进度，断点只补未发的段。
* 机器人发言在**发送前**写 intent（``stage='pending'``），解决"机器人自己的
  消息被误判成本人手动发言"的竞态。
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from ast import literal_eval
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
DB_PATH = os.environ.get("WXBOT_QUEUE_DB") or os.path.join(BASE, "queue.db")

RELAY_CLARIFY_SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_clarify (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account      TEXT NOT NULL,
    chat         TEXT NOT NULL,
    src_key      TEXT,
    content      TEXT NOT NULL,
    candidates   TEXT,
    ask          TEXT,
    ask_core     TEXT,
    kind         TEXT NOT NULL DEFAULT 'remind',
    options      TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_ts   INTEGER,
    expires_ts   INTEGER,
    asks         INTEGER NOT NULL DEFAULT 0,
    prompt_outbound_id INTEGER,
    confirm_chat TEXT NOT NULL DEFAULT ''
);
"""

RELAY_CLARIFY_SENT_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS relay_clarify_prompt_sent
AFTER UPDATE OF status, verified ON outbound
WHEN NEW.kind='relay_clarify' AND NEW.status='sent' AND NEW.verified=1
     AND (OLD.status!='sent' OR COALESCE(OLD.verified,0)!=1)
BEGIN
    UPDATE relay_clarify SET asks=asks+1
    WHERE id=NEW.ref_id AND account=NEW.account AND chat=NEW.chat
      AND prompt_outbound_id=NEW.id;
END;
"""

RELAY_CLARIFY_WITH_PROMPT = """
SELECT r.*, o.status AS prompt_status,
       CASE WHEN o.status='sent' AND o.verified=1 THEN o.sent_ts END AS prompt_sent_ts,
       CASE WHEN o.status='sent' AND o.verified=1 THEN o.verified_seq END AS prompt_verified_seq
FROM relay_clarify r LEFT JOIN outbound o ON o.id=r.prompt_outbound_id
AND o.kind='relay_clarify' AND o.ref_id=r.id AND o.account=r.account AND o.chat=r.chat
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account      TEXT NOT NULL,
    chat         TEXT NOT NULL,
    local_id     TEXT,
    shard        TEXT,
    sort_seq     INTEGER,
    type_code    INTEGER,
    msg_ts       INTEGER NOT NULL,
    msg_type     TEXT,
    content      TEXT,
    sender       TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    retry_count  INTEGER NOT NULL DEFAULT 0,
    not_before   INTEGER NOT NULL DEFAULT 0,
    created_ts   INTEGER NOT NULL,
    UNIQUE(account, chat, local_id, msg_ts)
);

CREATE TABLE IF NOT EXISTS outbound (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account       TEXT NOT NULL,
    chat          TEXT NOT NULL,
    text          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft',
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_ts    INTEGER NOT NULL,
    updated_ts    INTEGER,
    sent_ts       INTEGER,
    not_before    INTEGER NOT NULL DEFAULT 0,
    segments      TEXT,
    segments_sent INTEGER NOT NULL DEFAULT 0,
    verified      INTEGER,
    gate          TEXT,
    at_me         INTEGER NOT NULL DEFAULT 0,   -- 群聊：这条是因为被 @ 才回的
    error         TEXT,
    failure_code  TEXT,                          -- 明确未提交时的失败原因代码
    src_msg_id    INTEGER,
    kind          TEXT NOT NULL DEFAULT 'reply', -- reply | reminder | proactive
    ref_id        INTEGER,                       -- reminder→occurrence, proactive→hook
    expires_ts    INTEGER,                       -- 到点截止（提醒宽限）
    activity_version INTEGER,                    -- 主动闲聊：生成时的会话活动版本
    covered       TEXT,                          -- 普通回复：这条草稿已经答过的入站消息 id（JSON）
    approved_ts   INTEGER,                       -- 人工批准时间
    attempt_started_ts INTEGER,                  -- 进入 UI 提交的时刻
    attempt_after_seq  INTEGER,                  -- 提交前的微信消息水位
    attempt_dispatched INTEGER NOT NULL DEFAULT 0, -- 发送服务已领取该段提交权限
    retryable     INTEGER NOT NULL DEFAULT 1     -- 0 = 禁止自动重试
);

CREATE TABLE IF NOT EXISTS cursor (
    account      TEXT PRIMARY KEY,
    last_ts      INTEGER NOT NULL DEFAULT 0,
    updated_ts   INTEGER
);

CREATE TABLE IF NOT EXISTS collect_watermark (
    account      TEXT NOT NULL,
    chat         TEXT NOT NULL,
    sort_seq     INTEGER NOT NULL,
    PRIMARY KEY(account, chat)
);

CREATE TABLE IF NOT EXISTS lock_chat (
    chat         TEXT PRIMARY KEY,
    until_ts     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pause (
    chat         TEXT PRIMARY KEY,
    until_ts     INTEGER NOT NULL,
    reason       TEXT
);

CREATE TABLE IF NOT EXISTS robot_sent (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat         TEXT NOT NULL,
    text         TEXT,
    ts           INTEGER NOT NULL,
    outbound_id  INTEGER,
    stage        TEXT DEFAULT 'confirmed'
);

CREATE TABLE IF NOT EXISTS chat_state (
    chat         TEXT PRIMARY KEY,
    last_at_ts   INTEGER NOT NULL DEFAULT 0,   -- 最近一次被 @ 的时间
    last_at_by   TEXT                          -- 最近一次 @ 我的人
);

-- ---------------- 定时提醒（P2）
CREATE TABLE IF NOT EXISTS reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account      TEXT NOT NULL,
    chat         TEXT NOT NULL,
    content      TEXT NOT NULL,                -- 提醒什么，例如「喝水」
    rule         TEXT NOT NULL DEFAULT 'single', -- single|daily|weekly:1,3|interval:2|monthly:15
    anchor_ts    INTEGER,                      -- 原始约定的钟点锚（循环按它推进，不用上次实际发送）
    next_ts      INTEGER,                      -- 下次到点；单次到期后置 NULL
    until_ts     INTEGER,                      -- 约定截止日
    confirm_mode TEXT NOT NULL DEFAULT 'ok',   -- ok | ask_at_fire | relay
    relay_from   TEXT,                         -- 代传话：谁托的（relay 时用）
    short_delay  INTEGER NOT NULL DEFAULT 0,
    tz_zone      TEXT,                         -- 建单时联系人的 IANA 时区名；周期推进按它
    relay_verbatim INTEGER NOT NULL DEFAULT 0, -- 代传话「代说」：正文直出，不套提醒模板
    relay_from_chat TEXT,                      -- 代传话发起人的会话（谁托的）
    revision     INTEGER NOT NULL DEFAULT 1,   -- 改期/暂停递增，旧任务靠它失效
    status       TEXT NOT NULL DEFAULT 'active', -- active|paused|cancelled|done
    created_ts   INTEGER NOT NULL,
    updated_ts   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_rem_due ON reminders(account, status, next_ts);
CREATE INDEX IF NOT EXISTS ix_rem_chat ON reminders(chat, status);

CREATE TABLE IF NOT EXISTS reminder_occurrences (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id  INTEGER NOT NULL,
    scheduled_ts INTEGER NOT NULL,
    revision     INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued', -- queued|sent|missed|cancelled|uncertain|unverified
    outbound_id  INTEGER,                      -- 对应的出站任务，对账用
    sent_ts      INTEGER,
    reason       TEXT,
    UNIQUE(reminder_id, scheduled_ts)
);

CREATE TABLE IF NOT EXISTS reminder_actions (
    account      TEXT NOT NULL,
    chat         TEXT NOT NULL,
    request_key  TEXT NOT NULL,                -- 聊天端 inbound.id / 控制台 request_id
    reminder_id  INTEGER,
    result       TEXT,
    created_ts   INTEGER,
    PRIMARY KEY(account, chat, request_key)
);

-- 候选队列：规则解析不了、但要交给模型判定「这句话是不是提醒委托」的消息
CREATE TABLE IF NOT EXISTS reminder_candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account      TEXT NOT NULL,
    chat         TEXT NOT NULL,
    msg_id       INTEGER NOT NULL,
    text         TEXT NOT NULL,
    msg_ts       INTEGER NOT NULL,
    images       TEXT,                       -- 同批的图（JSON），判定时一起给模型看
    status       TEXT NOT NULL DEFAULT 'pending', -- pending|done|skipped|error
    result       TEXT,
    created_ts   INTEGER,
    attempts     INTEGER NOT NULL DEFAULT 0,
    not_before   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(account, chat, msg_id)
);

-- 待澄清：单独发送核对问句，回读确认送达后才接受简短肯定
CREATE TABLE IF NOT EXISTS reminder_clarify (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account      TEXT NOT NULL,
    chat         TEXT NOT NULL,
    content      TEXT NOT NULL,
    base_ts      INTEGER NOT NULL,
    ask          TEXT,
    rule         TEXT,                       -- 模型给的提案：weekly:2 / daily …
    proposed_ts  INTEGER,                    -- 提案的第一个到点（回「对」就按它建）
    images       TEXT,                       -- 提案依据的图（JSON）
    asks         INTEGER NOT NULL DEFAULT 0, -- 已经问了几次，到上限就放弃
    status       TEXT NOT NULL DEFAULT 'pending', -- pending|asked|done|expired
    created_ts   INTEGER,
    expires_ts   INTEGER,
    source_text  TEXT,
    prompt_outbound_id INTEGER,
    tz_zone      TEXT
);

""" + RELAY_CLARIFY_SCHEMA + """

-- 代传话「回传」：问了谁、他回了什么，够数或超时后由模型组织一句回给发起人。
CREATE TABLE IF NOT EXISTS relay_watch (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account      TEXT NOT NULL,
    from_chat    TEXT NOT NULL,              -- 发起人 A
    target_chat  TEXT NOT NULL,              -- 被问的人 B
    question     TEXT NOT NULL,              -- 问的什么事
    from_name    TEXT,                       -- A 的显示名
    reminder_id  INTEGER,                    -- 对应的代传话提醒
    asked_ts     INTEGER NOT NULL,
    started_ts   INTEGER,                    -- B 开始回复的时间
    msg_count    INTEGER NOT NULL DEFAULT 0,
    reply_text   TEXT,                       -- 收集到的 B 的回复（JSON 数组）
    batch        TEXT,                       -- 同一句委托问了好几个人：归到一组
    target_name  TEXT,                       -- B 的显示名（汇总时用）
    status       TEXT NOT NULL DEFAULT 'waiting',
    reply_after_seq INTEGER,                 -- 已核验的问题在微信里的序号
    back_ts      INTEGER NOT NULL DEFAULT 0, -- 反托（他让我把话带回给发起人）的时间
    created_ts   INTEGER NOT NULL,
    updated_ts   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_relay_watch ON relay_watch(account, target_chat, status);
CREATE TABLE IF NOT EXISTS relay_reply_events (
    watch_id     INTEGER NOT NULL,
    inbound_id   INTEGER NOT NULL,
    PRIMARY KEY(watch_id, inbound_id)
);

-- 每个联系人所在地与主机的时差（秒，对方本地 - 主机本地）。说过一次就记住，
-- 之后「明天下午三点」这类钟点都按他那边的时间算。
CREATE TABLE IF NOT EXISTS chat_tz (
    account        TEXT NOT NULL,
    chat           TEXT NOT NULL,
    offset_seconds INTEGER NOT NULL,
    zone           TEXT,                     -- IANA 时区名；有它时偏移按它现算（夏令时）
    updated_ts     INTEGER,
    PRIMARY KEY(account, chat)
);

-- 主动聊天：每个联系人一行节奏与状态
CREATE TABLE IF NOT EXISTS proactive_state (
    account         TEXT NOT NULL,
    chat            TEXT NOT NULL,
    next_at         INTEGER,                 -- 下次允许开口的时刻
    quiet_until     INTEGER DEFAULT 0,       -- 对方说「晚安/先忙」后的静默截止
    cooldown_until  INTEGER DEFAULT 0,       -- 连续无回应后的冷却截止
    muted           INTEGER DEFAULT 0,       -- 明确说「别主动找我」→ 持久停
    last_self_ts    INTEGER DEFAULT 0,       -- 我方最后一条消息时间
    activity_version INTEGER DEFAULT 0,      -- 有新消息/手动发言就 +1，用来丢弃过期草稿
    trial_until     INTEGER DEFAULT 0,       -- 试发期截止，期间草稿只进待审
    trial_passed    INTEGER DEFAULT 0,       -- 已有一条草稿被人工批准
    last_proactive_id INTEGER DEFAULT 0,
    await_reply_until INTEGER DEFAULT 0,
    no_reply_streak INTEGER DEFAULT 0,       -- 连续几次主动都没被回
    day             TEXT,                    -- 北京时间自然日 YYYY-MM-DD
    day_n           INTEGER DEFAULT 0,       -- 当日已用配额
    created_ts      INTEGER,
    updated_ts      INTEGER,
    PRIMARY KEY(account, chat)
);

-- 钩子库：从聊天里抽「还没聊完、值得追问」的东西，作为主动开口的由头
CREATE TABLE IF NOT EXISTS hooks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account      TEXT NOT NULL,
    chat         TEXT NOT NULL,
    source_msg_id INTEGER,
    topic_key    TEXT NOT NULL,
    kind         TEXT NOT NULL,              -- event|question|topic
    text         TEXT NOT NULL,
    earliest_ts  INTEGER DEFAULT 0,          -- 最早可以提的时间
    expires_ts   INTEGER NOT NULL,           -- 过期就烂了
    status       TEXT NOT NULL DEFAULT 'open', -- open|used|expired|dropped
    created_ts   INTEGER,
    UNIQUE(chat, topic_key)
);
CREATE INDEX IF NOT EXISTS ix_hooks_pick ON hooks(chat, status, expires_ts);
"""

STATES = ("draft", "hold", "sending", "sent", "failed", "uncertain",
          "cancelled", "expired", "unverified", "superseded")

# 出站任务类型：普通回复 / 定时提醒 / 主动闲聊
CLARIFY_KINDS = ("reminder_clarify", "relay_clarify")
KINDS = ("reply", "reminder", *CLARIFY_KINDS, "proactive")

INBOUND_STATES = ("pending", "retry", "done", "skipped", "expired", "failed")
# 未提交的软重试上限：到达后转 hold（待人工），不再落 failed。
# 45s 起指数退避、封顶 send.retry_max_delay，12 次约覆盖 1.5 小时。
OUTBOUND_MAX_ATTEMPTS = 12
PRE_SUBMIT_RETRY_CODES = {"no_row", "no_window", "session_list_unavailable",
                          "identity_unreadable", "chat_window_closed"}

# 旧库迁移：缺列就补（幂等，失败说明已存在）
MIGRATIONS = (
    "ALTER TABLE inbound ADD COLUMN sender TEXT",
    "ALTER TABLE inbound ADD COLUMN shard TEXT",
    "ALTER TABLE inbound ADD COLUMN sort_seq INTEGER",
    "ALTER TABLE inbound ADD COLUMN type_code INTEGER",
    "ALTER TABLE inbound ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE inbound ADD COLUMN not_before INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE outbound ADD COLUMN updated_ts INTEGER",
    "ALTER TABLE outbound ADD COLUMN not_before INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE outbound ADD COLUMN segments TEXT",
    "ALTER TABLE outbound ADD COLUMN segments_sent INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE outbound ADD COLUMN verified INTEGER",
    "ALTER TABLE outbound ADD COLUMN verified_seq INTEGER",
    "ALTER TABLE outbound ADD COLUMN gate TEXT",
    "ALTER TABLE robot_sent ADD COLUMN outbound_id INTEGER",
    "ALTER TABLE robot_sent ADD COLUMN stage TEXT DEFAULT 'confirmed'",
    "ALTER TABLE outbound ADD COLUMN at_me INTEGER NOT NULL DEFAULT 0",
    # P1：出站任务类型化（主动聊天与定时提醒）
    "ALTER TABLE outbound ADD COLUMN kind TEXT NOT NULL DEFAULT 'reply'",
    "ALTER TABLE outbound ADD COLUMN ref_id INTEGER",
    "ALTER TABLE outbound ADD COLUMN expires_ts INTEGER",
    "ALTER TABLE outbound ADD COLUMN activity_version INTEGER",
    "ALTER TABLE outbound ADD COLUMN approved_ts INTEGER",
    "ALTER TABLE outbound ADD COLUMN attempt_started_ts INTEGER",
    "ALTER TABLE outbound ADD COLUMN attempt_after_seq INTEGER",
    "ALTER TABLE outbound ADD COLUMN attempt_dispatched INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE outbound ADD COLUMN retryable INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE outbound ADD COLUMN failure_code TEXT",
    # P2：普通回复草稿记录它覆盖了哪些入站消息，发送前据此判断"对方有没有又补话"
    "ALTER TABLE outbound ADD COLUMN covered TEXT",
    "ALTER TABLE reminders ADD COLUMN short_delay INTEGER NOT NULL DEFAULT 0",
    # 代传话：谁托的（「联系人A让我提醒你」里的联系人A）
    "ALTER TABLE reminders ADD COLUMN relay_from TEXT",
    # 每条提醒记下建单时的联系人时区名：周期推进按它算，旧记录（NULL）按主机算
    "ALTER TABLE reminders ADD COLUMN tz_zone TEXT",
    # 代传话「代说」：正文直出，不套「X让我提醒你」模板
    "ALTER TABLE reminders ADD COLUMN relay_verbatim INTEGER NOT NULL DEFAULT 0",
    # 待确认记录也要记住是「提醒」还是「代说」
    "ALTER TABLE relay_clarify ADD COLUMN kind TEXT NOT NULL DEFAULT 'remind'",
    # 代传话发起人的会话：让发起人自己也能一句话取消
    "ALTER TABLE reminders ADD COLUMN relay_from_chat TEXT",
    # 一句话问了好几个人：同一次委托的回传归到一组，等人齐了再汇总
    "ALTER TABLE relay_watch ADD COLUMN batch TEXT",
    # 被问的人叫什么（汇总时说「组长说…」，不用回查人设卡）
    "ALTER TABLE relay_watch ADD COLUMN target_name TEXT",
    "ALTER TABLE relay_watch ADD COLUMN reply_after_seq INTEGER",
    # 追问时摆出来的候选（名字或序号），答复「1」也能对上人
    "ALTER TABLE relay_clarify ADD COLUMN options TEXT",
    # 问句的模板原文：发出去的是模型改写过的版本（ask），「同一句别重复问」
    # 还得拿模板比，否则改写过就永远对不上
    "ALTER TABLE relay_clarify ADD COLUMN ask_core TEXT",
    # 反托：被问的人让橙子把话带回给发起人，一次委托只允许一次
    "ALTER TABLE relay_watch ADD COLUMN back_ts INTEGER NOT NULL DEFAULT 0",
    # 提醒正文只认模型写的那句：NULL = 模板占位/还没改写，不许发出去
    "ALTER TABLE outbound ADD COLUMN text_source TEXT",
    # 联系人时区：存 IANA 名（Europe/London），偏移每次现算，夏令时自动对
    "ALTER TABLE chat_tz ADD COLUMN zone TEXT",
    # 提醒候选/待确认也带图：课表、日程、海报里的时间只有图里才有
    "ALTER TABLE reminder_candidates ADD COLUMN images TEXT",
    "ALTER TABLE reminder_candidates ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE reminder_candidates ADD COLUMN not_before INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE reminder_clarify ADD COLUMN rule TEXT",
    "ALTER TABLE reminder_clarify ADD COLUMN proposed_ts INTEGER",
    "ALTER TABLE reminder_clarify ADD COLUMN images TEXT",
    "ALTER TABLE reminder_clarify ADD COLUMN asks INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE reminder_clarify ADD COLUMN source_text TEXT",
    "ALTER TABLE reminder_clarify ADD COLUMN prompt_outbound_id INTEGER",
    "ALTER TABLE reminder_clarify ADD COLUMN tz_zone TEXT",
)

EXTRA_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_inbound_status ON inbound(status, id)",
    "CREATE INDEX IF NOT EXISTS idx_inbound_chat_seq ON inbound(chat, sort_seq)",
    "CREATE INDEX IF NOT EXISTS idx_inbound_account_status ON inbound(account, status, id)",
    "CREATE INDEX IF NOT EXISTS idx_outbound_status ON outbound(status, id)",
    "CREATE INDEX IF NOT EXISTS idx_outbound_account_status ON outbound(account, status, id)",
    "CREATE INDEX IF NOT EXISTS idx_robot_sent ON robot_sent(chat, ts)",
    "CREATE INDEX IF NOT EXISTS idx_robot_sent_ob ON robot_sent(outbound_id)",
    # 按类型领取要快
    "CREATE INDEX IF NOT EXISTS idx_outbound_kind ON outbound(kind, status, id)",
    # 同一条提醒的同一个到点，只允许有一个出站任务
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_out_ref ON outbound(kind, ref_id) "
    "WHERE ref_id IS NOT NULL AND kind='reminder'",
)


def _account_clause(account: str | None, column: str = "account") -> tuple[str, tuple]:
    return ("", ()) if account is None else (" AND " + column + "=?", (account,))


def _legacy_missing_session(error: str) -> bool:
    """仅匹配旧版发送引擎的「会话列表里没有 %r」，保留已有失败记录的处理能力。"""
    prefix = "会话列表里没有 "
    if not error.startswith(prefix):
        return False
    value = error[len(prefix):]
    if len(value) < 3 or value[0] not in ("'", '"') or value[-1] != value[0]:
        return False
    try:
        name = literal_eval(value)
    except (ValueError, SyntaxError):
        return False
    return isinstance(name, str) and bool(name) and repr(name) == value


class Queue:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        folder = os.path.dirname(db_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._init()

    # ---------------------------------------------------------------- 连接
    @contextlib.contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            with c:                     # 正常退出提交、异常回滚
                yield c
        finally:
            c.close()                   # 显式关闭，避免 Windows 文件占用

    def _init(self):
        with self._conn() as c:
            try:
                c.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            cols_before: set[str] = set()
            try:
                cols_before = {r[1] for r in c.execute("PRAGMA table_info(outbound)")}
            except sqlite3.Error:
                pass
            c.executescript(SCHEMA)
            for sql in MIGRATIONS:
                try:
                    c.execute(sql)
                except sqlite3.Error:
                    pass
            if cols_before and "attempt_dispatched" not in cols_before:
                # 旧发送器没有提交位，不能把未知状态误当作确定未提交。
                c.execute("UPDATE outbound SET attempt_dispatched=1 "
                          "WHERE status IN ('sending','uncertain')")
            self._migrate_relay_clarify(c)
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_relay_clarify_pending "
                      "ON relay_clarify(account,chat) WHERE status='pending'")
            c.execute(RELAY_CLARIFY_SENT_TRIGGER)
            for sql in EXTRA_INDEXES:
                try:
                    c.execute(sql)
                except sqlite3.Error:
                    pass
            try:
                # 数据迁移只做一次：每次 WxQueue() 都跑 UPDATE 会和后台
                # 写线程抢 SQLite 写锁（Windows 上容易 database is locked）。
                ver = 0
                try:
                    ver = int(c.execute("PRAGMA user_version").fetchone()[0])
                except sqlite3.Error:
                    ver = 0
                if ver < 1:
                    # 旧库里已经在待发的提醒正文是旧版本写好的（模板或已润色），
                    # 标成 legacy 才不会被「等模型写」卡到错过。
                    c.execute("UPDATE outbound SET text_source='legacy' "
                              "WHERE kind='reminder' AND text_source IS NULL")
                    c.execute("PRAGMA user_version=1")
                if ver < 2:
                    c.execute(
                        "UPDATE reminder_candidates SET status='pending' WHERE status='error' "
                        "AND msg_ts>?", (int(time.time()) - 86400,))
                    c.execute("PRAGMA user_version=2")
            except sqlite3.Error:
                pass
        self._migrate_weekly_tz()

    @staticmethod
    def _migrate_relay_clarify(conn):
        """拆除全状态的唯一限制并保留旧记录，只限制每个会话有一条待确认。"""
        if not any(r["origin"] == "u" for r in conn.execute("PRAGMA index_list(relay_clarify)")):
            return
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if not any(r["origin"] == "u" for r in conn.execute("PRAGMA index_list(relay_clarify)")):
            return
        conn.execute("ALTER TABLE relay_clarify RENAME TO relay_clarify_legacy")
        conn.execute(RELAY_CLARIFY_SCHEMA)
        columns = "id,account,chat,src_key,content,candidates,ask,kind,status,created_ts,expires_ts"
        conn.execute(f"INSERT INTO relay_clarify({columns}) SELECT {columns} FROM relay_clarify_legacy")
        conn.execute("DROP TABLE relay_clarify_legacy")

    def _migrate_weekly_tz(self):
        """旧库里的 weekly 提醒：把**单日**规则的星期对齐到收件人当地。

        早期手工建单按主机星期写 ``weekly:N``，调度却按联系人时区推进，
        第二次就会偏一天。这里按 ``anchor_ts`` 的当地时间重算一次（幂等）。

        只动「``weekly:N`` 单个数字且没有 tz_zone」的旧记录：
        多日规则（``weekly:1,3``）没法判断该怎么平移，硬改会把周三弄丢；
        新建的计划已经带 tz_zone，也不该被迁移覆盖。
        """
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT id, rule, anchor_ts, account, chat FROM reminders "
                    "WHERE rule LIKE 'weekly:%' AND anchor_ts IS NOT NULL "
                    "AND tz_zone IS NULL").fetchall()
                for row in rows:
                    days = row["rule"].split(":", 1)[-1].split(",")
                    if len(days) != 1 or not days[0].strip().isdigit():
                        continue
                    off = self.get_chat_tz(row["account"], row["chat"]) or 0
                    if not off:
                        continue
                    local = datetime.fromtimestamp(int(row["anchor_ts"]) + int(off))
                    rule = "weekly:%d" % local.isoweekday()
                    if rule != row["rule"]:
                        c.execute("UPDATE reminders SET rule=? WHERE id=?",
                                  (rule, row["id"]))
        except Exception:
            pass

    # ---------------------------------------------------------------- 显式事务
    @contextlib.contextmanager
    def tx(self, immediate: bool = True):
        """一次完成多步写入，异常整体回滚。

        与 ``_conn()`` 的区别：``_conn`` 是「with 块结束即提交」，跨两次调用
        就是两个事务。提醒调度要求「写 occurrence + 进 outbound + 推进 next_ts」
        原子完成，必须用这个，否则进程中断会留下发了一半的状态。
        """
        c = sqlite3.connect(self.db_path, timeout=15)
        c.row_factory = sqlite3.Row
        c.isolation_level = None            # 手动控制 BEGIN/COMMIT
        try:
            c.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield c
                c.execute("COMMIT")
            except Exception:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            c.close()

    # ---------------------------------------------------------------- 游标（兼容保留）
    def get_cursor(self, account: str, default: int = 0) -> int:
        with self._conn() as c:
            r = c.execute("SELECT last_ts FROM cursor WHERE account=?", (account,)).fetchone()
        return r["last_ts"] if r else default

    def set_cursor(self, account: str, ts: int):
        with self._conn() as c:
            c.execute(
                "INSERT INTO cursor(account, last_ts, updated_ts) VALUES(?,?,?) "
                "ON CONFLICT(account) DO UPDATE SET last_ts=excluded.last_ts, "
                "updated_ts=excluded.updated_ts",
                (account, ts, int(time.time())),
            )

    # ---------------------------------------------------------------- 群聊状态
    def mark_at(self, chat: str, sender: str = "", ts: int | None = None):
        """记下"在群里被 @ 了"，用于后续几分钟的接话窗口。"""
        with self._conn() as c:
            c.execute(
                "INSERT INTO chat_state(chat, last_at_ts, last_at_by) VALUES(?,?,?) "
                "ON CONFLICT(chat) DO UPDATE SET last_at_ts=excluded.last_at_ts, "
                "last_at_by=excluded.last_at_by",
                (chat, int(ts or time.time()), sender or ""),
            )

    def last_at(self, chat: str) -> tuple[int, str]:
        with self._conn() as c:
            r = c.execute(
                "SELECT last_at_ts, last_at_by FROM chat_state WHERE chat=?", (chat,)
            ).fetchone()
        if not r:
            return 0, ""
        return int(r["last_at_ts"] or 0), (r["last_at_by"] or "")

    # ---------------------------------------------------------------- 入站
    def enqueue_inbound(self, account: str, chat: str, local_id, msg_ts: int,
                        msg_type: str, content: str, sender: str = "",
                        shard: str | None = None, sort_seq: int | None = None,
                        type_code: int | None = None) -> bool:
        """入队一条入站消息；已存在返回 False。"""
        now = int(time.time())
        with self.tx() as c:
            # 不要用 (shard, local_id) 做去重：local_id 只是微信消息表里的自增号，
            # 消息表被重建/重编号（清理缓存、重新登录、迁移账号、升级、恢复聊天记录）
            # 后它会从 1 重新开始，旧号会和新消息撞车。那种预检会把新消息静默丢掉，
            # 而采集水位照样前进 → 消息永久丢失、日志一片空白。
            # 去重只认表约束 UNIQUE(account, chat, local_id, msg_ts)，与下面的
            # ON CONFLICT 保持一致；任何预检都必须和它同宽，不能更严。
            result = c.execute(
                "INSERT INTO inbound(account, chat, local_id, shard, sort_seq, "
                "type_code, msg_ts, msg_type, content, sender, status, created_ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?) "
                "ON CONFLICT(account, chat, local_id, msg_ts) DO NOTHING",
                (account, chat, str(local_id or ""), shard or None,
                 int(sort_seq) if sort_seq is not None else None,
                 int(type_code) if type_code is not None else None,
                 int(msg_ts or 0), msg_type or "", (content or "")[:20000],
                 sender or "", now),
            )
            if not result.rowcount:
                return False
            c.execute(
                "UPDATE proactive_state SET activity_version=activity_version+1, "
                "await_reply_until=0, no_reply_streak=0, updated_ts=? "
                "WHERE account=? AND chat=?",
                (now, account, chat),
            )
            return True

    def get_collect_watermark(self, account: str, chat: str) -> int | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT sort_seq FROM collect_watermark WHERE account=? AND chat=?",
                (account, chat),
            ).fetchone()
        return int(row[0]) if row is not None else None

    def set_collect_watermark(self, account: str, chat: str, sort_seq: int):
        with self._conn() as c:
            c.execute(
                "INSERT INTO collect_watermark(account, chat, sort_seq) VALUES(?,?,?) "
                "ON CONFLICT(account, chat) DO UPDATE SET "
                "sort_seq=MAX(collect_watermark.sort_seq, excluded.sort_seq)",
                (account, chat, int(sort_seq)),
            )

    def rewind_collect_watermark(self, account: str, chat: str, sort_seq: int) -> int | None:
        """把采集水位回退到 ``sort_seq``，用于重放被误判丢弃的消息。

        ``set_collect_watermark`` 是 MAX 语义（只增不减），所以重放必须显式回退。
        回退之后采集器会从 ``sort_seq`` 再往后扫一遍；已经入过库的消息会命中
        UNIQUE(account, chat, local_id, msg_ts) 而幂等跳过，不会重复入队。
        返回回退前的旧水位（没有记录时返回 None）。
        """
        old = self.get_collect_watermark(account, chat)
        with self._conn() as c:
            c.execute(
                "INSERT INTO collect_watermark(account, chat, sort_seq) VALUES(?,?,?) "
                "ON CONFLICT(account, chat) DO UPDATE SET sort_seq=excluded.sort_seq",
                (account, chat, int(sort_seq)),
            )
        return old

    def claim_inbound(self, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM inbound WHERE status='pending' "
                "ORDER BY msg_ts, COALESCE(sort_seq, 0), id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_inbound(self, id_: int, status: str):
        with self._conn() as c:
            c.execute("UPDATE inbound SET status=? WHERE id=?", (status, id_))

    def mark_inbounds(self, ids, status: str, *, conn=None):
        ids = [int(i) for i in ids]
        if not ids:
            return
        query = "UPDATE inbound SET status=? WHERE id=?"
        args = [(status, i) for i in ids]
        if conn is not None:
            conn.executemany(query, args)
            return
        with self._conn() as c:
            c.executemany(query, args)

    def mark_retry(self, id_: int, delay: float, max_retry: int = 3) -> int:
        """生成失败：退避重试；超过上限转 failed。返回当前重试次数。"""
        now = int(time.time())
        with self._conn() as c:
            row = c.execute("SELECT retry_count FROM inbound WHERE id=?", (id_,)).fetchone()
            n = int(row["retry_count"] if row else 0) + 1
            if n > max_retry:
                c.execute("UPDATE inbound SET status='failed', retry_count=? WHERE id=?",
                          (n, id_))
            else:
                c.execute(
                    "UPDATE inbound SET status='retry', retry_count=?, not_before=? "
                    "WHERE id=?", (n, now + max(1, int(delay)), id_),
                )
        return n

    def pending_inbound_count(self) -> int:
        with self._conn() as c:
            r = c.execute(
                "SELECT COUNT(*) FROM inbound WHERE status IN ('pending','retry')"
            ).fetchone()
        return r[0] if r else 0

    def pending_by_chat(self, limit: int = 200, *, account: str | None = None,
                        skip_paused: bool = False) -> dict[str, list[dict]]:
        """按会话聚合待处理消息（按时间 + sort_seq 排序）。"""
        now = int(time.time())
        scope, args = _account_clause(account)
        if skip_paused:
            scope += " AND NOT EXISTS (SELECT 1 FROM pause WHERE pause.chat=inbound.chat AND until_ts>?)"
            args += (now,)
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM inbound WHERE status IN ('pending','retry') "
                "AND not_before<=?" + scope +
                " ORDER BY msg_ts, COALESCE(sort_seq, 0), id LIMIT ?",
                (now, *args, limit),
            ).fetchall()
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r["chat"], []).append(dict(r))
        return groups

    def recent_inbound(self, chat: str, limit: int = 10, *,
                       account: str | None = None, exclude_ids=(),
                       within_seconds: int = 0) -> list[dict]:
        """该会话最近的入站消息（只读：不写记忆、不改状态）。

        群聊背景用它：别人没 @ 我的那几句、以及他们刚发的图片，都能在这一轮
        带进模型，接话才不会接错。``exclude_ids`` 排掉本轮已经在处理的消息，
        ``within_seconds`` 只看最近一段时间（0 = 不限）。
        """
        limit = max(1, int(limit or 1))
        scope, args = _account_clause(account)
        sql = "SELECT * FROM inbound WHERE chat=?" + scope
        qargs: list = [chat, *args]
        ids = []
        for i in (exclude_ids or ()):
            try:
                ids.append(int(i))
            except (TypeError, ValueError):
                continue
        if ids:
            sql += " AND id NOT IN (%s)" % ",".join("?" * len(ids))
            qargs.extend(ids)
        if within_seconds:
            sql += " AND msg_ts>=?"
            qargs.append(int(time.time()) - int(within_seconds))
        sql += " ORDER BY msg_ts DESC, COALESCE(sort_seq,0) DESC, id DESC LIMIT ?"
        qargs.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, qargs).fetchall()
        return [dict(r) for r in reversed(rows)]

    def has_newer_inbound(self, chat: str, after_id: int, *,
                          account: str | None = None) -> bool:
        scope, args = _account_clause(account)
        with self._conn() as c:
            r = c.execute(
                "SELECT COUNT(*) FROM inbound WHERE chat=? AND status IN ('pending','retry') "
                "AND id>?" + scope, (chat, after_id, *args),
            ).fetchone()
        return bool(r and r[0])

    def newest_pending(self, chat: str, limit: int = 100, *,
                       account: str | None = None, ready_only: bool = False) -> list[dict]:
        scope, args = _account_clause(account)
        if ready_only:
            scope += " AND not_before<=?"
            args += (int(time.time()),)
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM inbound WHERE chat=? AND status IN ('pending','retry')" +
                scope + " ORDER BY msg_ts DESC, COALESCE(sort_seq, 0) DESC, id DESC LIMIT ?",
                (chat, *args, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def expire_inbound(self, max_age: int = 21600, *, account: str | None = None) -> int:
        """太旧的待处理消息不再回复（暂停很久后解禁，别补一堆过时回复）。"""
        cutoff = int(time.time()) - int(max_age)
        scope, args = _account_clause(account)
        with self._conn() as c:
            cur = c.execute(
                "UPDATE inbound SET status='expired' "
                "WHERE status IN ('pending','retry') AND msg_ts < ?" + scope,
                (cutoff, *args),
            )
            return cur.rowcount

    def list_inbound(self, limit: int = 30, *, account: str | None = None) -> list[dict]:
        """给控制台/网页看最近收到的消息（读表，不再维护一份内存缓冲）。"""
        scope, args = _account_clause(account)
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, chat, msg_type, type_code, content, msg_ts, status "
                "FROM inbound WHERE 1=1" + scope + " ORDER BY id DESC LIMIT ?",
                (*args, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def max_seq(self, chat: str, *, account: str | None = None) -> int:
        scope, args = _account_clause(account)
        with self._conn() as c:
            r = c.execute(
                "SELECT MAX(sort_seq) AS s FROM inbound WHERE chat=?" + scope, (chat, *args)
            ).fetchone()
        return int(r["s"]) if r and r["s"] is not None else 0

    def last_inbound_ts(self, chat: str, *, account: str | None = None) -> int:
        scope, args = _account_clause(account)
        with self._conn() as c:
            r = c.execute(
                "SELECT MAX(msg_ts) AS t FROM inbound WHERE chat=?" + scope, (chat, *args)
            ).fetchone()
        return int(r["t"]) if r and r["t"] is not None else 0

    # ---------------------------------------------------------------- 人工接管
    def pause_chat(self, chat: str, minutes: int = 30, reason: str = "manual"):
        with self._conn() as c:
            c.execute(
                "INSERT INTO pause(chat, until_ts, reason) VALUES(?,?,?) "
                "ON CONFLICT(chat) DO UPDATE SET until_ts=excluded.until_ts, "
                "reason=excluded.reason",
                (chat, int(time.time()) + int(minutes) * 60, reason),
            )

    def is_paused(self, chat: str) -> bool:
        with self._conn() as c:
            r = c.execute("SELECT until_ts FROM pause WHERE chat=?", (chat,)).fetchone()
        return bool(r and r["until_ts"] > int(time.time()))

    def resume_chat(self, chat: str):
        with self._conn() as c:
            c.execute("DELETE FROM pause WHERE chat=?", (chat,))

    def clear_pauses(self) -> int:
        """人工接管已关闭时，把此前留下的暂停全部解除。"""
        with self._conn() as c:
            cur = c.execute("DELETE FROM pause")
            return cur.rowcount

    def paused_chats(self) -> list[dict]:
        now = int(time.time())
        with self._conn() as c:
            rows = c.execute("SELECT * FROM pause WHERE until_ts > ?", (now,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 机器人发言
    def record_robot_intent(self, chat: str, text: str, outbound_id: int | None = None,
                            ts: int | None = None) -> int:
        """发送**之前**登记，避免采集线程抢先把自己发的消息当成本人手动发言。"""
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO robot_sent(chat, text, ts, outbound_id, stage) "
                "VALUES(?,?,?,?,'pending')",
                (chat, (text or "")[:2000], int(ts or time.time()), outbound_id),
            )
            return int(cur.lastrowid)

    def confirm_robot_sent(self, intent_id: int, ts: int | None = None):
        with self._conn() as c:
            c.execute(
                "UPDATE robot_sent SET ts=?, stage='confirmed' WHERE id=?",
                (int(ts or time.time()), int(intent_id)),
            )

    def drop_robot_intent(self, intent_id: int):
        with self._conn() as c:
            c.execute("DELETE FROM robot_sent WHERE id=? AND stage='pending'",
                      (int(intent_id),))

    def record_robot_sent(self, chat: str, text: str, ts: int | None = None):
        """兼容旧调用：直接记一条 confirmed。"""
        with self._conn() as c:
            c.execute(
                "INSERT INTO robot_sent(chat, text, ts, outbound_id, stage) "
                "VALUES(?,?,?,NULL,'confirmed')",
                (chat, (text or "")[:2000], int(ts or time.time())),
            )

    def was_robot_sent(self, chat: str, text: str, ts: int,
                       before: int = 90, after: int = 900) -> bool:
        """这条「自己发的」消息是不是机器人发的（intent 阶段也算）。

        群里发出的 ``@名字 内容`` 会被微信存成带 U+2005 的 mention
        （``@名字\\u2005内容``），和记录下来的原文不完全相等，所以按"去掉所有
        空白后相等/包含"来比，避免把自己的消息误判成本人手动发言。
        """
        import re as _re

        def norm(s):
            return _re.sub(r"[\s\u2005\u00a0\u3000]+", "", s or "")

        want = norm(text)
        with self._conn() as c:
            rows = c.execute(
                "SELECT text FROM robot_sent WHERE chat=? AND ts>=? AND ts<=?",
                (chat, int(ts) - before, int(ts) + after),
            ).fetchall()
        for r in rows:
            got = norm(r["text"])
            if not got or not want:
                continue
            if got == want or (len(got) >= 6 and got in want) or \
                    (len(want) >= 6 and want in got):
                return True
        return False

    def prune_robot_sent(self, days: int = 7):
        cutoff = int(time.time()) - int(days) * 86400
        with self._conn() as c:
            c.execute("DELETE FROM robot_sent WHERE ts < ?", (cutoff,))

    # ---------------------------------------------------------------- 出站
    def create_outbound(self, account: str, chat: str, text: str,
                        src_msg_id: int | None = None, status: str = "draft",
                        gate: str | None = None,
                        segments: list[str] | None = None,
                        at_me: bool = False,
                        kind: str = "reply",
                        ref_id: int | None = None,
                        expires_ts: int | None = None,
                        activity_version: int | None = None,
                        covered: list[int] | None = None,
                        conn=None) -> int:
        """建一条出站任务。

        ``kind`` 决定门禁与取消语义：``reply`` / ``reminder`` / ``proactive``。
        ``covered`` 是这条草稿已经答过的入站消息 id：草稿躺在队列里等发送的
        那几秒，对方可能又补一句或补个表情，发送前要靠它判断"哪些是新的"。
        传 ``conn`` 时复用外部事务（提醒调度要求原子写入）。
        """
        import json as _json

        now = int(time.time())
        sql = (
            "INSERT INTO outbound(account, chat, text, status, created_ts, "
            "updated_ts, gate, segments, at_me, src_msg_id, kind, ref_id, "
            "expires_ts, activity_version, covered) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        args = (account, chat, text, status, now, now, gate,
                _json.dumps(segments, ensure_ascii=False) if segments else None,
                1 if at_me else 0, src_msg_id, kind or "reply", ref_id,
                expires_ts, activity_version,
                _json.dumps([int(x) for x in covered], ensure_ascii=False)
                if covered else None)
        if conn is not None:
            cur = conn.execute(sql, args)
            return int(cur.lastrowid)
        with self._conn() as c:
            cur = c.execute(sql, args)
            return int(cur.lastrowid)

    def unsent_reply_drafts(self, chat: str, within: int = 300, *,
                            account: str | None = None) -> list[dict]:
        """该会话最近还没发出去的普通回复草稿（新的在前）。

        用来判断"对方在我发出回复之前又补了话"：``superseded`` 也算在内，
        这样即使发送线程先把它作废了，处理线程仍能拿到那段文字当作合并参考。
        """
        scope, args = _account_clause(account)
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM outbound WHERE chat=? AND kind='reply' "
                "AND status IN ('draft','superseded') AND created_ts>=?" +
                scope + " ORDER BY id DESC LIMIT 5",
                (chat, int(time.time()) - max(0, int(within)), *args),
            ).fetchall()
        return [dict(r) for r in rows]

    def supersede_outbound(self, id_: int,
                           reason: str = "对方又补充了内容，这条并入下一轮",
                           allow_sending: bool = False) -> bool:
        """把**还没开始发**的草稿作废（永不发送）。

        只认 ``draft``：已经在发（``sending``）或转人工（``hold``）的不动，
        避免把半条已经发出去的消息当成没发过。发送线程在真正提交第一段之前
        复检发现对方补了话时，可以传 ``allow_sending=True`` 把自己刚领走的
        任务退回作废（此时一个字都还没发出去）。
        """
        states = "('draft','sending')" if allow_sending else "('draft')"
        with self._conn() as c:
            cur = c.execute(
                "UPDATE outbound SET status='superseded', retryable=0, error=?, "
                "updated_ts=? WHERE id=? AND status IN " + states,
                ((reason or "")[:500], int(time.time()), int(id_)),
            )
            return cur.rowcount > 0

    def newer_pending(self, chat: str, after_ids=(0,),
                      limit: int = 50, *, account: str | None = None) -> list[dict]:
        """还没处理、且不在 ``after_ids`` 里的入站消息（按时间正序）。"""
        skip = {int(x) for x in (after_ids or ()) if x is not None}
        msgs = self.newest_pending(chat, limit=limit, account=account)
        return [m for m in msgs if int(m["id"]) not in skip]

    def next_outbound(self, kinds: tuple[str, ...] | None = None, *,
                      account: str | None = None) -> dict | None:
        """取一条可发送任务：提醒优先，其次普通回复，最后主动闲聊。

        * 只领取 ``draft``，或「**确定未提交**」的可重试失败
          （``retryable=1`` 且 ``attempts<3``）。回读查不实的任务被标成
          ``unverified`` 且 ``retryable=0``，永远不会被这里重新领走。
        * 过期判断交给调用方（提醒要记 missed，不能静默丢）。
        """
        now = int(time.time())
        order = ("ORDER BY CASE WHEN kind IN ('reminder','reminder_clarify','relay_clarify') THEN 0 "
                 "WHEN kind='reply' THEN 1 ELSE 2 END, id")
        where, params = _account_clause(account)
        extra = list(params)
        if kinds:
            where += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            extra.extend(kinds)
        with self.tx() as c:
            row = c.execute(
                "SELECT * FROM outbound WHERE status='draft' AND not_before<=?"
                + where + " " + order, (now, *extra),
            ).fetchone()
            if row is None:
                row = c.execute(
                    "SELECT * FROM outbound WHERE status='failed' AND attempts<? "
                    "AND retryable=1 AND not_before<=?" + where + " " + order,
                    (OUTBOUND_MAX_ATTEMPTS, now, *extra),
                ).fetchone()
            if row is None:
                return None
            cur = c.execute(
                "UPDATE outbound SET status='sending', updated_ts=? "
                "WHERE id=? AND status IN ('draft','failed')",
                (now, row["id"]),
            )
            if cur.rowcount == 0:
                return None
            return dict(row)

    def begin_outbound_attempt(self, id_: int, segment_index: int, after_seq: int | None,
                               started_ts: int | None = None) -> bool:
        now = int(time.time())
        with self._conn() as c:
            result = c.execute(
                "UPDATE outbound SET attempt_started_ts=?, attempt_after_seq=?, "
                "attempt_dispatched=0, updated_ts=? "
                "WHERE id=? AND status='sending' AND segments_sent=? AND attempt_started_ts IS NULL",
                (int(started_ts if started_ts is not None else now), after_seq, now,
                 id_, segment_index),
            )
        return result.rowcount == 1

    def dispatch_outbound_attempt(self, id_: int, account: str, after_seq: int) -> bool:
        now = int(time.time())
        with self._conn() as c:
            result = c.execute(
                "UPDATE outbound SET attempt_dispatched=1, attempt_after_seq=?, updated_ts=? "
                "WHERE id=? AND account=? AND status='sending' AND "
                "attempt_started_ts IS NOT NULL AND attempt_dispatched=0 "
                "AND (kind NOT IN ('reminder','reminder_clarify','relay_clarify','proactive') "
                "OR expires_ts>?) "
                "AND (kind!='proactive' OR activity_version=("
                "SELECT p.activity_version FROM proactive_state p "
                "WHERE p.account=outbound.account AND p.chat=outbound.chat))",
                (after_seq, now, id_, account, now),
            )
        return result.rowcount == 1

    def clear_outbound_attempt(self, id_: int):
        with self._conn() as c:
            c.execute(
                "UPDATE outbound SET attempt_started_ts=NULL, attempt_after_seq=NULL, "
                "attempt_dispatched=0 WHERE id=?",
                (id_,),
            )

    def touch_outbound(self, id_: int):
        with self._conn() as c:
            c.execute("UPDATE outbound SET updated_ts=? WHERE id=?",
                      (int(time.time()), id_))

    def finish_outbound(self, id_: int, status: str, error: str | None = None,
                        bump_attempt: bool = True, verified: int | None = None,
                        retryable: int | None = None, *, failure_code: str | None = None,
                        expected: str | None = None) -> bool:
        """结案。``retryable=0`` 表示这个结果不允许后续自动重试。

        ``expected`` 给定时只在这种状态下才写：重试/转待审和人工取消撞车时，
        不能把已经 cancelled 的任务改回 hold（那会重新变成可批准、可发送）。
        """
        now = int(time.time())
        fields = ("status=?, error=?, updated_ts=?, sent_ts=COALESCE(?, sent_ts), "
                  "attempts=attempts+?, verified=COALESCE(?, verified), "
                  "retryable=COALESCE(?, retryable), failure_code=?")
        values = [status, (error or "")[:500], now, now if bump_attempt else None,
                  int(bump_attempt), verified, retryable, failure_code]
        sql = "UPDATE outbound SET " + fields + " WHERE id=?"
        values.append(id_)
        if expected:
            sql += " AND status=?"
            values.append(expected)
        with self._conn() as c:
            result = c.execute(sql, tuple(values))
            if result.rowcount == 1 and status == "sent":
                self._activate_relay_watch(c, id_, now)
            return result.rowcount == 1

    def set_outbound_status(self, id_: int, status: str, note: str | None = None):
        with self._conn() as c:
            c.execute(
                "UPDATE outbound SET status=?, error=?, updated_ts=? WHERE id=?",
                (status, (note or "")[:500], int(time.time()), id_),
            )

    def reset_not_before(self, id_: int):
        """人工批准后立即允许发送（清掉重试冷却）。"""
        with self._conn() as c:
            c.execute("UPDATE outbound SET not_before=0, updated_ts=? WHERE id=?",
                      (int(time.time()), int(id_)))

    def rewrite_outbound_text(self, id_: int, text: str,
                              segments: list[str] | None = None,
                              source: str = "model", conn=None) -> bool:
        """发送线程领取之后、一个字符都没提交之前，改掉正文与分段。

        提醒的措辞是发送前才找模型写的，那时任务已经被领成 ``sending``；
        用「一段都没发、也没开始提交」卡死，绝不改写已经发出去的任务。

        ``source`` 记进 ``text_source``：``model`` 表示这句话是模型写的，
        发送线程只认它（模板占位不许发出去）。
        """
        import json as _json

        text = (text or "").strip()
        if not text:
            return False
        sql = ("UPDATE outbound SET text=?, segments=?, text_source=?, updated_ts=? "
               "WHERE id=? "
               "AND status='sending' AND segments_sent=0 "
               "AND attempt_started_ts IS NULL AND attempt_after_seq IS NULL "
               "AND attempt_dispatched=0")
        args = (text[:4000],
                _json.dumps(segments, ensure_ascii=False) if segments else None,
                source, int(time.time()), id_)
        if conn is not None:
            result = conn.execute(sql, args)
        else:
            with self._conn() as c:
                result = c.execute(sql, args)
        return result.rowcount == 1

    def update_outbound_text(self, id_: int, text: str,
                             account: str | None = None) -> bool:
        """人工改写还没发出去的草稿正文。

        只允许未发送状态，避免把已在微信里核验过的记录改掉。

        **已经发出过段落的草稿**（``segments_sent > 0``）不能整条替换：那样
        要么把已发的前段算成没发，要么新正文被当成「已发」而根本不提交。
        这里保留已发段落，把新正文接在后面，发送从下一段继续。
        """
        import json as _json

        text = (text or "").strip()
        if not text:
            return False
        scope, args = _account_clause(account)
        now = int(time.time())
        with self._conn() as c:
            row = c.execute("SELECT * FROM outbound WHERE id=?" + scope,
                            (int(id_), *args)).fetchone()
            if not row:
                return False
            if row["status"] not in ("hold", "failed", "uncertain", "unverified"):
                return False
            if row["kind"] in CLARIFY_KINDS and text != row["text"]:
                return False
            if (row["segments_sent"] or 0) and (
                    row["attempt_started_ts"] is not None
                    or row["attempt_after_seq"] is not None
                    or row["attempt_dispatched"]):
                return False
            sent = int(row["segments_sent"] or 0)
            segs = None
            if sent > 0:
                try:
                    old = _json.loads(row["segments"] or "[]")
                except Exception:
                    old = []
                if not (isinstance(old, list) and len(old) >= sent):
                    return False
                sent_text = "".join(str(x) for x in old[:sent]).strip()
                body = text.strip()
                if sent_text and body.startswith(sent_text):
                    # 界面提交的是**整条正文**：去掉已经发出去的前缀，
                    # 只把剩下的部分当作新的待发段，避免第一段被重发。
                    remainder = body[len(sent_text):].strip()
                    if not remainder:
                        return False
                    segs = [str(x) for x in old[:sent]] + [remainder]
                else:
                    # 只提交了还没发的那部分，直接接在后面
                    segs = [str(x) for x in old[:sent]] + [body]
            result = c.execute(
                "UPDATE outbound SET segments=?, text=?, text_source='manual', "
                "updated_ts=? WHERE id=?" + scope +
                " AND status IN ('hold','failed','uncertain','unverified')"
                " AND segments_sent=? AND attempt_started_ts IS NULL"
                " AND attempt_after_seq IS NULL AND attempt_dispatched=0",
                (_json.dumps(segs, ensure_ascii=False) if segs else None,
                 text[:4000], now, int(id_), *args, sent),
            )
        return result.rowcount == 1

    @staticmethod
    def can_approve_outbound(task: dict, *, now: int | None = None) -> bool:
        if (task.get("attempt_started_ts") is not None
                or task.get("attempt_after_seq") is not None
                or task.get("attempt_dispatched") or task.get("verified")):
            return False
        now = int(time.time()) if now is None else now
        if task.get("kind") in ("reminder", *CLARIFY_KINDS, "proactive") and (task.get("expires_ts") or 0) <= now:
            return False
        if task["status"] == "hold":
            return True
        if task["status"] != "failed" or not task.get("attempts"):
            return False
        code = task.get("failure_code")
        if code:
            return code in PRE_SUBMIT_RETRY_CODES
        return _legacy_missing_session(task.get("error") or "")

    def approve_outbound(self, id_: int, account: str, *, text: str | None = None) -> bool:
        with self.tx() as c:
            now = int(time.time())
            row = c.execute("SELECT * FROM outbound WHERE id=? AND account=?",
                            (id_, account)).fetchone()
            if row is None or not self.can_approve_outbound(dict(row), now=now):
                return False
            if text is not None:
                text = text.strip()
                if row["kind"] in CLARIFY_KINDS and text != row["text"]:
                    return False
                if not text or len(text) > 4000 or (row["segments_sent"] and text != row["text"]):
                    return False
            new_segs = row["segments"]
            if text is not None and text != (row["text"] or ""):
                # 正文改了，旧分段就作废：不清掉的话发送仍按旧分段发模板
                new_segs = None
            c.execute(
                "UPDATE outbound SET status='draft', approved_ts=?, not_before=0, attempts=0, "
                "updated_ts=?, error='人工批准', failure_code=NULL, segments=?, "
                "text=COALESCE(?, text), "
                "text_source=CASE WHEN ? IS NULL THEN text_source ELSE 'manual' END "
                "WHERE id=? AND account=?",
                (now, now, new_segs, text, text, id_, account),
            )
        return True

    def operator_outbound_result(self, id_: int, account: str, expected: str,
                                 status: str, note: str) -> bool:
        now = int(time.time())
        with self._conn() as c:
            result = c.execute(
                "UPDATE outbound SET status=?, error=?, updated_ts=?, retryable=0, "
                "sent_ts=COALESCE(?, sent_ts), verified=COALESCE(?, verified) "
                "WHERE id=? AND account=? AND status=? AND (?!='sent' OR kind!='relay_clarify')",
                (status, note, now, now if status == "sent" else None,
                 1 if status == "sent" else None, id_, account, expected, status),
            )
            if result.rowcount == 1 and status == "sent":
                self._activate_relay_watch(c, id_, now)
        return result.rowcount == 1

    def requeue_outbound(self, id_: int, delay: float = 45, note: str | None = None,
                         *, count_attempt: bool = False, refresh_age: bool = True):
        """暂时性失败退回草稿，可选择计入重试次数。

        ``refresh_age``：把 ``created_ts`` 顺延同样时长。否则等系统恢复的时间会被
        ``max_draft_age`` 算成「草稿放久了」，恢复那一刻正好判过期，白等。
        """
        now = int(time.time())
        wait = max(1, int(delay))
        fields = "status='draft', not_before=?, updated_ts=?, error=?, attempts=attempts+?"
        values: list = [now + wait, now, (note or "")[:500], int(count_attempt)]
        if refresh_age:
            fields += ", created_ts=created_ts+?"
            values.append(wait)
        values.append(id_)
        with self._conn() as c:
            # 只允许「正在发送」的任务回退：重试和人工取消/撤回撞车时，
            # 不能把已 cancelled / sent 的任务又改回待发（那是重复发送）。
            c.execute("UPDATE outbound SET " + fields
                      + " WHERE id=? AND status='sending'", tuple(values))

    def bump_segments(self, id_: int, sent: int, *, complete: bool = False,
                      sent_after_seq: int | None = None, expected: str | None = None) -> bool:
        now = int(time.time())
        fields = ("segments_sent=?, updated_ts=?, attempt_started_ts=NULL, "
                  "attempt_after_seq=NULL, attempt_dispatched=0")
        values = [int(sent), now]
        if complete:
            fields += (", status='sent', sent_ts=?, verified=1, verified_seq=?, retryable=0, "
                       "attempts=attempts+1, error=''")
            values.extend((now, sent_after_seq))
            expected = "sending"
        sql = "UPDATE outbound SET " + fields + " WHERE id=?"
        values.append(id_)
        if expected is not None:
            sql += " AND status=?"
            values.append(expected)
        with self._conn() as c:
            result = c.execute(sql, values)
            if complete and result.rowcount == 1:
                self._activate_relay_watch(c, id_, now, sent_after_seq)
            return result.rowcount == 1

    def settle_uncertain(self, id_: int, *, status: str, note: str,
                         segments_sent: int | None = None, delay: int = 1,
                         sent_after_seq: int | None = None) -> bool:
        """只结案仍在对账的任务，避免覆盖人工操作。"""
        if status not in ("draft", "sent", "unverified"):
            raise ValueError("无效的对账结果")
        now = int(time.time())
        fields = ("status=?, error=?, updated_ts=?, segments_sent=COALESCE(?, segments_sent), "
                  "retryable=0")
        values = [status, note[:500], now, segments_sent]
        if status != "unverified":
            fields += ", attempt_started_ts=NULL, attempt_after_seq=NULL, attempt_dispatched=0"
        if status == "sent":
            fields += ", verified=1, sent_ts=?, verified_seq=?"
            values.extend((now, sent_after_seq))
        elif status == "draft":
            fields += ", not_before=?"
            values.append(now + max(1, int(delay)))
        with self._conn() as c:
            result = c.execute(
                "UPDATE outbound SET " + fields + " WHERE id=? AND status='uncertain'",
                (*values, id_),
            )
            if result.rowcount == 1 and status == "sent":
                self._activate_relay_watch(c, id_, now, sent_after_seq)
        return result.rowcount == 1

    def mark_uncertain(self, id_: int, error: str):
        self.finish_outbound(id_, "uncertain", error, retryable=0)

    def mark_unverified(self, id_: int, error: str):
        """回读查不实：既不能确认发出、也不能确认没发。

        禁止自动重试（否则可能重复发一句话），进人工列表等回读证据。
        """
        self.finish_outbound(id_, "unverified", error, bump_attempt=False,
                             retryable=0)

    def mark_missed(self, id_: int, error: str):
        """超过宽限的提醒：不再发送，记原因。"""
        self.finish_outbound(id_, "expired", error, bump_attempt=False,
                             retryable=0)

    def recover_inflight(self, age: int = 120, *, account: str | None = None) -> int:
        """进程中断留下的 sending 任务 → uncertain（先核验，不盲目重发）。"""
        now = int(time.time())
        scope, args = _account_clause(account)
        with self._conn() as c:
            cur = c.execute(
                "UPDATE outbound SET status='uncertain', updated_ts=?, retryable=0, "
                "error='进程中断，发送结果未确认' "
                "WHERE status='sending' AND COALESCE(updated_ts, created_ts) <= ?" + scope,
                (now, now - int(age), *args),
            )
            return cur.rowcount

    def last_sent_for(self, chat: str, *, account: str | None = None) -> dict | None:
        """该会话最近一条已发出的回复，用于避免连着两句一样的开头。"""
        scope, args = _account_clause(account)
        with self._conn() as c:
            r = c.execute(
                "SELECT id, text, sent_ts FROM outbound WHERE chat=? AND status='sent' "
                "AND text IS NOT NULL AND text != ''" + scope + " ORDER BY id DESC LIMIT 1",
                (chat, *args),
            ).fetchone()
        return dict(r) if r else None

    def last_reply_activity(self, chat: str, within: int = 30, *,
                            account: str | None = None) -> dict | None:
        """最近 ``within`` 秒内有没有一条回复「正在发」或「刚发出」。

        用来判断"对方只是补个表情/附和，这轮其实已经答过了"：只看 ``sent``
        不够——发送线程还在那 1～3 秒的拟人延迟里时，草稿已经是 ``sending``，
        这时对方补的表情包恰恰应该算进这条回复，而不是另起一轮。
        """
        cutoff = int(time.time()) - max(0, int(within))
        scope, args = _account_clause(account)
        with self._conn() as c:
            r = c.execute(
                "SELECT id, text, status, created_ts, sent_ts FROM outbound "
                "WHERE chat=? AND kind IN ('reply','proactive') "
                "AND status IN ('sending','sent') AND created_ts>=?" + scope +
                " ORDER BY id DESC LIMIT 1", (chat, cutoff, *args)).fetchone()
        return dict(r) if r else None

    def list_outbound(self, status: str | None = None, limit: int = 20, *,
                      account: str | None = None) -> list[dict]:
        scope, params = _account_clause(account)
        q = "SELECT * FROM outbound WHERE 1=1" + scope
        args = list(params)
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def get_outbound(self, id_: int, *, account: str | None = None) -> dict | None:
        scope, args = _account_clause(account)
        with self._conn() as c:
            r = c.execute("SELECT * FROM outbound WHERE id=?" + scope, (id_, *args)).fetchone()
        return dict(r) if r else None

    def has_newer_clarify_prompt(self, account: str, chat: str, prompt_id: int | None,
                                 *, msg_ts: int, msg_seq: int | None = None, conn=None) -> bool:
        """检查简短答复之前是否还有更新的消息可能是它在回应的对象。"""
        if not prompt_id:
            return False
        if conn is None:
            with self._conn() as c:
                return self.has_newer_clarify_prompt(account, chat, prompt_id, msg_ts=msg_ts,
                                                      msg_seq=msg_seq, conn=c)
        seq = int(msg_seq or 0)
        return conn.execute(
            "SELECT 1 FROM outbound original JOIN outbound later "
            "ON later.account=original.account AND later.chat=original.chat AND later.id<>original.id "
            "WHERE original.account=:account AND original.chat=:chat AND original.id=:prompt_id "
            "AND original.kind IN ('reminder_clarify','relay_clarify') "
            "AND original.status='sent' AND original.verified=1 AND original.sent_ts IS NOT NULL "
            "AND (later.kind IN ('reminder_clarify','relay_clarify') "
            "OR (original.kind='relay_clarify' AND later.kind='reply')) "
            "AND later.status='sent' AND later.verified=1 AND later.sent_ts IS NOT NULL "
            "AND CASE WHEN original.verified_seq>0 AND later.verified_seq>0 "
            "THEN later.verified_seq>original.verified_seq ELSE later.sent_ts>=original.sent_ts END "
            "AND CASE WHEN later.verified_seq>0 AND :msg_seq>0 "
            "THEN later.verified_seq<:msg_seq ELSE later.sent_ts<=:msg_ts END LIMIT 1",
            {"account": account, "chat": chat, "prompt_id": prompt_id,
             "msg_seq": seq, "msg_ts": int(msg_ts)}).fetchone() is not None

    def pending_non_reply(self, *, account: str | None = None) -> list[dict]:
        """列出所有非普通回复的未决任务。

        回退旧 Worker 前必须先把它们结案——旧 sender 不认 ``kind``，
        会把提醒和主动消息当普通回复发出去。
        """
        scope, args = _account_clause(account)
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, chat, kind, status, text FROM outbound "
                "WHERE kind != 'reply' AND status IN "
                "('draft','hold','sending','uncertain','failed','unverified')" +
                scope + " ORDER BY id", args,
            ).fetchall()]

    def cancel_pending(self, kinds: tuple[str, ...] | None = None,
                       reason: str = "自动回复已关闭", *,
                       account: str | None = None) -> int:
        """取消还没发出去的任务。hold 留着给人看。

        ``kinds`` 为空 = 全部取消（保持旧行为）；传 ``('reply','proactive')``
        只取消这两类，**提醒不会因为关自动回复被误杀**。
        """
        scope, args = _account_clause(account)
        where = "status IN ('draft','sending') AND attempt_started_ts IS NULL" + scope
        extra = list(args)
        if kinds:
            where += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            extra.extend(kinds)
        with self._conn() as c:
            cur = c.execute(
                "UPDATE outbound SET status='cancelled', error=?, updated_ts=? "
                "WHERE " + where, (reason, int(time.time()), *extra),
            )
            return cur.rowcount

    def cancel_chat_drafts(self, chat: str,
                           kinds: tuple[str, ...] | None = None,
                           reason: str = "人工接管", *,
                           account: str | None = None) -> int:
        """人工接管：取消该会话尚未发出的自动草稿。"""
        scope, args = _account_clause(account)
        where = "chat=? AND status IN ('draft','sending') AND attempt_started_ts IS NULL" + scope
        extra = list(args)
        if kinds:
            where += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            extra.extend(kinds)
        with self._conn() as c:
            cur = c.execute(
                "UPDATE outbound SET status='cancelled', error=?, updated_ts=? "
                "WHERE " + where, (reason, int(time.time()), chat, *extra),
            )
            return cur.rowcount

    def stats(self, *, account: str | None = None) -> dict:
        scope, args = _account_clause(account)
        with self._conn() as c:
            def one(sql, *a):
                r = c.execute(sql + scope, (*a, *args)).fetchone()
                return r[0] if r else 0

            return {
                "inbound_pending": one(
                    "SELECT COUNT(*) FROM inbound WHERE status IN ('pending','retry')"),
                "inbound_retry": one(
                    "SELECT COUNT(*) FROM inbound WHERE status='retry'"),
                "inbound_failed": one(
                    "SELECT COUNT(*) FROM inbound WHERE status='failed'"),
                "outbound_draft": one(
                    "SELECT COUNT(*) FROM outbound WHERE status='draft'"),
                "outbound_hold": one(
                    "SELECT COUNT(*) FROM outbound WHERE status='hold'"),
                "outbound_sending": one(
                    "SELECT COUNT(*) FROM outbound WHERE status='sending'"),
                "outbound_sent": one("SELECT COUNT(*) FROM outbound WHERE status='sent'"),
                "outbound_failed": one(
                    "SELECT COUNT(*) FROM outbound WHERE status='failed'"),
                "outbound_uncertain": one(
                    "SELECT COUNT(*) FROM outbound WHERE status='uncertain'"),
                "outbound_unverified": one(
                    "SELECT COUNT(*) FROM outbound WHERE status='unverified'"),
                "outbound_by_kind": {
                    k: one("SELECT COUNT(*) FROM outbound WHERE kind=?", k)
                    for k in KINDS
                },
                "reminders_active": one(
                    "SELECT COUNT(*) FROM reminders WHERE status='active'"),
                "reminders_due": one(
                    "SELECT COUNT(*) FROM reminders WHERE status='active' "
                    "AND next_ts IS NOT NULL AND next_ts<=?", int(time.time())),
                "outbound_expired": one(
                    "SELECT COUNT(*) FROM outbound WHERE status='expired'"),
            }

    # ---------------------------------------------------------------- 定时提醒
    def add_reminder(self, account: str, chat: str, content: str, rule: str,
                     next_ts: int | None, anchor_ts: int | None = None,
                     until_ts: int | None = None, confirm_mode: str = "ok",
                     conn=None, *, short_delay: bool = False,
                     relay_from: str | None = None,
                     tz_zone: str | None = None,
                     relay_verbatim: bool = False,
                     relay_from_chat: str | None = None) -> int:
        now = int(time.time())
        sql = ("INSERT INTO reminders(account, chat, content, rule, anchor_ts, "
               "next_ts, until_ts, confirm_mode, relay_from, short_delay, tz_zone, "
               "relay_verbatim, relay_from_chat, revision, status, created_ts, updated_ts) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,'active',?,?)")
        args = (account, chat, content, rule, anchor_ts, next_ts, until_ts,
                confirm_mode, relay_from, int(short_delay), tz_zone or None,
                int(bool(relay_verbatim)), relay_from_chat or None, now, now)
        if conn is not None:
            return int(conn.execute(sql, args).lastrowid)
        with self._conn() as c:
            return int(c.execute(sql, args).lastrowid)

    def get_reminder(self, id_: int, *, account: str | None = None) -> dict | None:
        scope, args = _account_clause(account)
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM reminders WHERE id=?" + scope, (int(id_), *args),
            ).fetchone()
        return dict(r) if r else None

    def list_reminders(self, chat: str | None = None, status: str | None = None,
                       limit: int = 50, *, account: str | None = None) -> list[dict]:
        q = "SELECT * FROM reminders"
        where, args = [], []
        if account is not None:
            where.append("account=?")
            args.append(account)
        if chat:
            where.append("chat=?")
            args.append(chat)
        if status:
            where.append("status=?")
            args.append(status)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY "
        if not status:
            q += "CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, "
            q += "CASE WHEN status NOT IN ('active','paused') THEN updated_ts END DESC, "
        q += "COALESCE(next_ts, 0), id LIMIT ?"
        args.append(int(limit))
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def due_reminders(self, now: float | None = None, limit: int = 5, *,
                      account: str | None = None) -> list[dict]:
        """到点该发的提醒。循环提醒在推进 next_ts 前会一直命中，调用方必须在
        事务里推进，否则同一到点会被反复触发。"""
        now = int(time.time() if now is None else now)
        scope, args = _account_clause(account)
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM reminders WHERE status='active' AND next_ts IS NOT NULL "
                "AND next_ts<=?" + scope + " ORDER BY next_ts, id LIMIT ?",
                (now, *args, int(limit)),
            ).fetchall()]

    def count_relays_since(self, since_ts: int, *,
                           account: str | None = None) -> int:
        """最近这段时间建了几条代传话（防止被当成遥控器刷屏）。"""
        scope, args = _account_clause(account)
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM reminders WHERE confirm_mode='relay' "
                "AND created_ts>=?" + scope, (int(since_ts), *args)).fetchone()
        return int(row[0] or 0)

    def relay_reminders_from(self, chat: str, *,
                             account: str | None = None) -> list[dict]:
        """这个人**托出去**的代传话（他可以说一句「别每天叫他去上课了」）。"""
        scope, args = _account_clause(account)
        with self._conn() as c:
            return [dict(row) for row in c.execute(
                "SELECT * FROM reminders WHERE relay_from_chat=? AND "
                "confirm_mode='relay' AND status IN ('active','paused')" + scope,
                (chat, *args),
            ).fetchall()]

    def cancellable_reminders(self, chat: str, *, account: str | None = None) -> list[dict]:
        """查找会话内还可撤回的提醒，包括已到点但仍待发的单次提醒。"""
        scope, args = _account_clause(account, "r.account")
        with self._conn() as c:
            return [dict(row) for row in c.execute(
                "SELECT r.* FROM reminders r WHERE r.chat=?" + scope + " AND "
                "(r.status IN ('active','paused') OR (r.status='done' AND EXISTS ("
                "SELECT 1 FROM reminder_occurrences o JOIN outbound b ON o.outbound_id=b.id "
                "WHERE o.reminder_id=r.id AND o.revision=r.revision "
                "AND o.status IN ('queued','uncertain') "
                "AND (b.status IN ('draft','hold','sending','uncertain') "
                "OR (b.status='failed' AND b.retryable=1 AND b.attempts<3)))))",
                (chat, *args),
            ).fetchall()]

    def update_reminder(self, id_: int, expected_revision: int | None = None,
                        **fields) -> int:
        """改期/暂停/恢复。revision 递增，旧待发任务靠它失效。"""
        allowed = {"content", "rule", "anchor_ts", "next_ts", "until_ts",
                   "confirm_mode", "status", "short_delay"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append("%s=?" % k)
            args.append(v)
        if not sets:
            return 0
        sets.append("revision=revision+1")
        sets.append("updated_ts=?")
        args.append(int(time.time()))
        where = "id=?"
        args.append(int(id_))
        if expected_revision is not None:
            where += " AND revision=?"
            args.append(int(expected_revision))
        with self.tx() as c:
            changed = c.execute(
                "UPDATE reminders SET " + ", ".join(sets) + " WHERE " + where,
                args).rowcount
            if changed:
                c.execute(
                    "UPDATE outbound SET status='cancelled', retryable=0, updated_ts=?, "
                    "error='提醒设置已更新' WHERE kind='reminder' AND attempt_dispatched=0 "
                    "AND status IN ('draft','hold','sending','failed') AND ref_id IN "
                    "(SELECT id FROM reminder_occurrences WHERE reminder_id=?)",
                    (int(time.time()), id_),
                )
            return changed

    def add_occurrence(self, conn, reminder_id: int, scheduled_ts: int,
                       revision: int) -> int:
        """事务内登记一次到点。同一 (reminder, scheduled) 只建一条，返回其 id。"""
        reminder_id, scheduled_ts = int(reminder_id), int(scheduled_ts)
        cur = conn.execute(
            "INSERT OR IGNORE INTO reminder_occurrences"
            "(reminder_id, scheduled_ts, revision) VALUES(?,?,?)",
            (reminder_id, scheduled_ts, int(revision)))
        if cur.rowcount:
            return int(cur.lastrowid)
        r = conn.execute(
            "SELECT id FROM reminder_occurrences WHERE reminder_id=? AND scheduled_ts=?",
            (reminder_id, scheduled_ts)).fetchone()
        return int(r["id"]) if r else 0

    def get_occurrence(self, id_: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM reminder_occurrences WHERE id=?",
                          (int(id_),)).fetchone()
        return dict(r) if r else None

    def set_occurrence_status(self, id_: int, status: str,
                              reason: str | None = None,
                              sent_ts: int | None = None):
        with self._conn() as c:
            c.execute(
                "UPDATE reminder_occurrences SET status=?, reason=?, "
                "sent_ts=COALESCE(?, sent_ts) WHERE id=?",
                (status, (reason or "")[:300], sent_ts, int(id_)))

    def list_occurrences(self, reminder_id: int | None = None,
                         limit: int = 50, *, account: str | None = None) -> list[dict]:
        scope, params = _account_clause(account, "r.account")
        q = ("SELECT o.* FROM reminder_occurrences o JOIN reminders r ON r.id=o.reminder_id "
             "WHERE 1=1" + scope)
        args = list(params)
        if reminder_id:
            q += " AND o.reminder_id=?"
            args.append(int(reminder_id))
        q += " ORDER BY o.scheduled_ts DESC, o.id DESC LIMIT ?"
        args.append(int(limit))
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def pending_occurrences(self, limit: int = 50, *,
                            account: str | None = None) -> list[dict]:
        """还没结案的到点记录（有出站任务的），用于回写发送结果。"""
        scope, args = _account_clause(account, "r.account")
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT o.* FROM reminder_occurrences o JOIN reminders r ON r.id=o.reminder_id "
                "WHERE o.status IN ('queued','uncertain') AND o.outbound_id IS NOT NULL" +
                scope + " ORDER BY o.id LIMIT ?", (*args, int(limit)),
            ).fetchall()]

    def record_action(self, account: str, chat: str, request_key: str,
                      reminder_id: int | None = None, result: str = "",
                      conn=None) -> bool:
        """同一条入站消息只处理一次（重复入站 / HTTP 重试靠它去重）。

        ``conn`` 传进来时用调用方的事务，让「建单 + 登记」原子落库。
        """
        sql = ("INSERT OR IGNORE INTO reminder_actions(account, chat, request_key, "
               "reminder_id, result, created_ts) VALUES(?,?,?,?,?,?)")
        args = (account, chat, str(request_key), reminder_id, result[:200],
                int(time.time()))
        if conn is not None:
            return conn.execute(sql, args).rowcount > 0
        with self._conn() as c:
            cur = c.execute(sql, args)
            return cur.rowcount > 0

    def has_action(self, account: str, chat: str, request_key: str,
                   conn=None) -> bool:
        return self.action_result(account, chat, request_key, conn=conn) is not None

    def action_result(self, account: str, chat: str, request_key: str,
                      conn=None) -> str | None:
        sql = ("SELECT result FROM reminder_actions WHERE account=? AND chat=? "
               "AND request_key=?")
        args = (account, chat, str(request_key))
        if conn is not None:
            row = conn.execute(sql, args).fetchone()
        else:
            with self._conn() as c:
                row = c.execute(sql, args).fetchone()
        return (row[0] or "") if row else None

    def reminder_source_text(self, reminder_id: int, *,
                             account: str | None = None) -> str:
        """建这条提醒时对方说的原话（给「到点怎么开口」当上下文用）。

        ``reminder.content`` 是剥掉时间词和委托词之后的残渣，单独丢给模型
        容易被它照抄；原话才看得出他到底想让你提醒什么。
        """
        scope, args = _account_clause(account)
        with self._conn() as c:
            row = c.execute(
                "SELECT request_key FROM reminder_actions WHERE reminder_id=?"
                + scope + " ORDER BY created_ts DESC LIMIT 1",
                (int(reminder_id), *args)).fetchone()
            if not row:
                return ""
            try:
                inbound_id = int(str(row[0]))
            except (TypeError, ValueError):
                return ""
            msg = c.execute("SELECT content FROM inbound WHERE id=?",
                            (inbound_id,)).fetchone()
        return (msg[0] or "").strip()[:200] if msg else ""

    def set_chat_tz(self, account: str, chat: str, offset_seconds: int,
                    zone: str | None = None) -> None:
        """记下这个联系人所在地与主机的时差（对方本地 - 主机本地，秒）。

        ``zone`` 是 IANA 时区名（``Europe/London``）。有名字就按名字现算偏移，
        夏令时切换和对方回国都不会把钟点算错；没有名字时才退回落库的秒数。
        """
        with self._conn() as c:
            c.execute(
                "INSERT INTO chat_tz(account, chat, offset_seconds, zone, updated_ts) "
                "VALUES(?,?,?,?,?) ON CONFLICT(account, chat) DO UPDATE SET "
                "offset_seconds=excluded.offset_seconds, zone=excluded.zone, "
                "updated_ts=excluded.updated_ts",
                (account, chat, int(offset_seconds), zone or None, int(time.time())))

    def get_chat_tz(self, account: str, chat: str) -> int | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT offset_seconds, zone FROM chat_tz WHERE account=? AND chat=?",
                (account, chat)).fetchone()
        if not row:
            return None
        zone = row["zone"] if "zone" in row.keys() else None
        if zone:
            try:
                import reminder_parse
                off = reminder_parse.tz_from_zone(zone)
                if off is not None:
                    return int(off)
            except Exception:
                pass
        return int(row["offset_seconds"])

    def get_chat_zone(self, account: str, chat: str) -> str | None:
        """这个联系人存的 IANA 时区名（没有就 None）。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT zone FROM chat_tz WHERE account=? AND chat=?",
                (account, chat)).fetchone()
        if not row:
            return None
        try:
            zone = row["zone"]
        except Exception:
            zone = None
        return (zone or "").strip() or None

    # ---------------------------------------------------- 候选队列（模型判定）
    def add_candidate(self, account: str, chat: str, msg_id, text: str,
                      msg_ts: int, images=None) -> bool:
        """入候选队列。同一条入站消息只进一次；后来补上了图就把图补上。"""
        import json as _json

        blob = _json.dumps(images[:3], ensure_ascii=False) if images else ""
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO reminder_candidates"
                "(account, chat, msg_id, text, msg_ts, images, status, created_ts) "
                "VALUES(?,?,?,?,?,?,'pending',?) "
                "ON CONFLICT(account, chat, msg_id) DO UPDATE SET "
                "images=CASE WHEN excluded.images<>'' THEN excluded.images "
                "ELSE reminder_candidates.images END "
                "WHERE reminder_candidates.status='pending'",
                (account, chat, int(msg_id), (text or "")[:300], int(msg_ts),
                 blob, int(time.time())))
            return cur.rowcount > 0

    def pending_candidates(self, limit: int = 1, *,
                           account: str | None = None) -> list[dict]:
        scope, args = _account_clause(account)
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM reminder_candidates WHERE status='pending' "
                "AND not_before<=?" + scope + " ORDER BY not_before, id LIMIT ?",
                (int(time.time()), *args, int(limit)),
            ).fetchall()]

    def retry_candidate(self, id_: int, result: str) -> None:
        with self.tx() as c:
            row = c.execute(
                "SELECT attempts FROM reminder_candidates WHERE id=? AND status='pending'",
                (int(id_),)).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"]) + 1
            delay = min(300, 5 * 2 ** min(attempts - 1, 6))
            c.execute(
                "UPDATE reminder_candidates SET attempts=?, not_before=?, result=? WHERE id=?",
                (attempts, int(time.time()) + delay, result[:300], int(id_)))

    def set_candidate(self, id_: int, status: str, result: str = "",
                      conn=None):
        sql = ("UPDATE reminder_candidates SET status=?, result=? "
               "WHERE id=? AND status='pending'")
        args = (status, (result or "")[:300], int(id_))
        if conn is not None:
            conn.execute(sql, args)
            return
        with self._conn() as c:
            c.execute(sql, args)

    def is_candidate_pending(self, id_: int, conn) -> bool:
        return conn.execute(
            "SELECT 1 FROM reminder_candidates WHERE id=? AND status='pending'",
            (int(id_),)).fetchone() is not None

    # ---------------------------------------------------- 代传话待确认（问发给谁）
    def add_relay_clarify(self, account: str, chat: str, content: str,
                          candidates: list[str], ask: str, src_key: str = "",
                          ttl_hours: int = 6, kind: str = "remind", *,
                          confirm_chat: str = "", options: list[str] | None = None,
                          ask_core: str | None = None, conn=None) -> int:
        """记下待确认委托，并撤回该会话此前尚未提交的问句。

        ``ask`` 是这条问句最后一次实际使用的文本（发之前可能被改成模型口吻），
        ``ask_core`` 是模板原文：判断「是不是同一句话又来了」时只认它。
        """
        import json as _json

        if conn is None:
            with self.tx() as c:
                return self.add_relay_clarify(account, chat, content, candidates, ask, src_key,
                                              ttl_hours, kind, confirm_chat=confirm_chat,
                                              options=options, ask_core=ask_core, conn=c)
        now = int(time.time())
        previous = conn.execute(
            "SELECT id FROM relay_clarify WHERE account=? AND chat=? AND status='pending'",
            (account, chat)).fetchone()
        if previous:
            self.set_relay_clarify(previous["id"], "cancelled", conn=conn)
        cur = conn.execute(
            "INSERT INTO relay_clarify(account, chat, src_key, content, candidates,"
            " ask, ask_core, kind, options, status, created_ts, expires_ts, confirm_chat)"
            " VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
            (account, chat, str(src_key or ""), (content or "")[:60],
             _json.dumps([x for x in candidates if x], ensure_ascii=False),
             (ask or "").strip(),
             (ask if ask_core is None else ask_core or "").strip()[:400],
             "say" if kind == "say" else "remind",
             _json.dumps([x for x in (options or []) if x], ensure_ascii=False),
             now, now + int(ttl_hours) * 3600, (confirm_chat or "").strip()))
        return int(cur.lastrowid)

    def get_relay_clarify(self, account: str, chat: str, *, conn=None) -> dict | None:
        if conn is None:
            with self._conn() as c:
                return self.get_relay_clarify(account, chat, conn=c)
        r = conn.execute(
            RELAY_CLARIFY_WITH_PROMPT +
            "WHERE r.account=? AND r.chat=? AND r.status='pending' AND r.expires_ts>?",
            (account, chat, int(time.time()))).fetchone()
        return dict(r) if r else None

    def get_relay_clarify_by_id(self, id_: int, *, conn=None) -> dict | None:
        if conn is None:
            with self._conn() as c:
                return self.get_relay_clarify_by_id(id_, conn=c)
        row = conn.execute(RELAY_CLARIFY_WITH_PROMPT + "WHERE r.id=?", (int(id_),)).fetchone()
        return dict(row) if row else None

    def enqueue_relay_clarify_prompt(self, account: str, chat: str, ask: str, *,
                                     conn=None) -> int | None:
        ask = (ask or "").strip()
        if not ask:
            return None
        if conn is None:
            with self.tx() as c:
                return self.enqueue_relay_clarify_prompt(account, chat, ask, conn=c)
        row = self.get_relay_clarify(account, chat, conn=conn)
        if not row or row["prompt_outbound_id"] is not None:
            return None
        outbound_id = self.create_outbound(
            account, chat, ask, segments=[ask], kind="relay_clarify", ref_id=row["id"],
            expires_ts=row["expires_ts"], conn=conn)
        conn.execute("UPDATE relay_clarify SET prompt_outbound_id=?, ask=? WHERE id=?",
                     (outbound_id, ask, row["id"]))
        return outbound_id

    @staticmethod
    def _cancel_relay_clarify_prompt(conn, row: dict):
        conn.execute(
            "UPDATE outbound SET status='cancelled', retryable=0, updated_ts=?, "
            "error='代传话核对已结案' WHERE kind='relay_clarify' AND id=? AND ref_id=? "
            "AND account=? AND chat=? AND status IN ('draft','hold','sending','failed','uncertain') "
            "AND attempt_dispatched=0",
            (int(time.time()), row["prompt_outbound_id"], row["id"], row["account"], row["chat"]))

    def advance_relay_clarify(self, id_: int, candidates: list[str], ask: str, *,
                             confirm_chat: str = "", options: list[str] | None = None,
                             ask_core: str | None = None, conn=None) -> bool:
        """换一轮提问，保留先前确实送达的问句计数。"""
        import json as _json

        if conn is None:
            with self.tx() as c:
                return self.advance_relay_clarify(id_, candidates, ask,
                                                  confirm_chat=confirm_chat,
                                                  options=options,
                                                  ask_core=ask_core, conn=c)
        row = self.get_relay_clarify_by_id(id_, conn=conn)
        if not row or row["status"] != "pending" or (row["expires_ts"] or 0) <= int(time.time()):
            return False
        if row["prompt_status"] == "uncertain":
            return False
        if row["prompt_status"] == "sending":
            live = conn.execute("SELECT attempt_dispatched FROM outbound WHERE id=?",
                                (row["prompt_outbound_id"],)).fetchone()
            if live and live["attempt_dispatched"]:
                return False
        self._cancel_relay_clarify_prompt(conn, row)
        sets = ["candidates=?", "ask=?", "confirm_chat=?", "prompt_outbound_id=NULL"]
        args = [_json.dumps([x for x in candidates if x], ensure_ascii=False),
                (ask or "").strip(), (confirm_chat or "").strip()]
        if options is not None:
            sets.append("options=?")
            args.append(_json.dumps([x for x in options if x], ensure_ascii=False))
        if ask_core is not None:
            sets.append("ask_core=?")
            args.append((ask_core or "").strip()[:400])
        args.append(int(id_))
        conn.execute("UPDATE relay_clarify SET " + ", ".join(sets) + " WHERE id=?", args)
        return True

    def set_relay_clarify_ask(self, id_: int, ask: str, conn=None) -> bool:
        """只改「已经问出去的那句」的显示文本（发送前换成人设口吻）。

        不改 ``ask_core``：判重仍按模板走，换过说法也不会把同一句再问一遍。
        """
        ask = (ask or "").strip()
        if not ask:
            return False
        if conn is None:
            with self.tx() as c:
                return self.set_relay_clarify_ask(id_, ask, conn=c)
        cur = conn.execute(
            "UPDATE relay_clarify SET ask=? WHERE id=? AND status='pending'",
            (ask[:400], int(id_)))
        return cur.rowcount == 1

    def set_relay_clarify(self, id_: int, status: str, conn=None) -> bool:
        if conn is None:
            with self.tx() as c:
                return self.set_relay_clarify(id_, status, conn=c)
        row = self.get_relay_clarify_by_id(id_, conn=conn)
        if not row or row["status"] != "pending":
            return False
        conn.execute("UPDATE relay_clarify SET status=? WHERE id=?", (status, int(id_)))
        if status != "pending":
            self._cancel_relay_clarify_prompt(conn, row)
        return True

    def expire_relay_clarify(self) -> int:
        with self.tx() as c:
            rows = c.execute(
                "SELECT id FROM relay_clarify WHERE status='pending' AND expires_ts<=?",
                (int(time.time()),)).fetchall()
            for row in rows:
                self.set_relay_clarify(row["id"], "expired", conn=c)
            return len(rows)

    # ---------------------------------------------------- 代传话回传
    def add_relay_watch(self, account: str, from_chat: str, target_chat: str,
                        question: str, from_name: str = "",
                        reminder_id: int | None = None,
                        asked_ts: int | None = None,
                        batch: str = "", target_name: str = "", *,
                        pending_send: bool = False, conn=None) -> int:
        """记下「问了 B 一件事，等他回」。

        ``batch`` 把「同一句话问了好几个人」的几条回传串起来：等人齐了再
        合成一句告诉发起人，免得 B、C 各刷一条。
        """
        now = int(asked_ts or time.time())
        sql = ("INSERT INTO relay_watch(account, from_chat, target_chat, question,"
               " from_name, reminder_id, batch, target_name, asked_ts, status,"
               " created_ts, updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)")
        args = (account, from_chat, target_chat, (question or "")[:120],
                (from_name or "")[:40], reminder_id, (batch or "")[:80] or None,
                (target_name or "")[:40] or None, now,
                "pending_send" if pending_send else "waiting", now, now)
        if conn is not None:
            return int(conn.execute(sql, args).lastrowid)
        with self._conn() as c:
            cur = c.execute(sql, args)
            return int(cur.lastrowid)

    def _activate_relay_watch(self, conn, outbound_id: int, sent_ts: int,
                              sent_after_seq: int | None = None):
        conn.execute(
            "UPDATE relay_watch SET status='waiting', asked_ts=?, updated_ts=?, reply_after_seq=? "
            "WHERE status='pending_send' AND EXISTS ("
            "SELECT 1 FROM outbound o JOIN reminder_occurrences oc ON oc.id=o.ref_id "
            "WHERE o.id=? AND o.kind='reminder' AND o.account=relay_watch.account "
            "AND o.chat=relay_watch.target_chat AND oc.reminder_id=relay_watch.reminder_id)",
            (sent_ts, sent_ts, sent_after_seq or 0, outbound_id))

    def relay_watch_open(self, account: str, batch: str) -> list[int]:
        """同一组里还没结案的回传 id（用来判断人齐没齐）。"""
        if not (batch or "").strip():
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT id FROM relay_watch WHERE account=? AND batch=? "
                "AND status IN ('pending_send','waiting','collecting') ORDER BY id",
                (account, batch)).fetchall()
        return [int(r["id"]) for r in rows]

    def relay_watch_for(self, account: str, target_chat: str) -> dict | None:
        """仅返回能唯一归属的一条已送达代问。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM relay_watch WHERE account=? AND target_chat=? "
                "AND status IN ('waiting','collecting') LIMIT 2",
                (account, target_chat)).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

    def recent_relay_partner(self, account: str, chat: str,
                             within_seconds: int = 1800) -> str:
        """这个会话最近一次代传话的对面是谁（消解「他/她」指代用）。

        我是发起人 → 返回被问的人；我是被问的人 → 返回发起人。没有就空串。
        只认**确实送达过**的记录：``pending_send`` 是还没发出去的委托，
        ``undelivered`` 是发送失败，都不能拿来当「上一位委托人」。
        """
        since = int(time.time()) - max(0, int(within_seconds or 0))
        with self._conn() as c:
            row = c.execute(
                "SELECT from_chat, target_chat FROM relay_watch WHERE account=? "
                "AND created_ts>=? AND (from_chat=? OR target_chat=?) "
                "AND status IN ('waiting','collecting','answered','timeout','ambiguous') "
                "ORDER BY id DESC LIMIT 1",
                (account, since, chat, chat)).fetchone()
        if not row:
            return ""
        return (row["target_chat"] or "") if (row["from_chat"] or "") == chat \
            else (row["from_chat"] or "")

    def mark_relay_watch_back(self, id_: int, conn=None) -> bool:
        """记下「他已经让我把话带回给发起人」：同一次委托不再反托第二次。

        和建单放同一个事务里：崩在中间时要么都成、要么都不成，不会出现
        「消息已发出但没打标」→ 下次再反托一遍。
        """
        now = int(time.time())
        if conn is None:
            with self.tx() as c:
                return self.mark_relay_watch_back(id_, conn=c)
        cur = conn.execute(
            "UPDATE relay_watch SET back_ts=?, updated_ts=? WHERE id=? AND back_ts=0",
            (now, now, int(id_)))
        return cur.rowcount == 1

    @staticmethod
    def relay_reply_is_new(asked_ts: int, after_seq: int | None,
                           msg_ts: int, sort_seq: int | None) -> bool:
        if after_seq and sort_seq:
            return int(sort_seq) > int(after_seq)
        return msg_ts > asked_ts or (after_seq is None and msg_ts == asked_ts)

    def _append_relay_item(self, watch_id: int, item, ts: int | None = None, *,
                           message_id: int | None = None, sort_seq: int | None = None) -> bool:
        """往代传话里追加一条：字符串=文本，dict=图片/表情（``{"k":"image"}``）。"""
        import json as _json

        now = int(ts or time.time())
        if isinstance(item, dict):
            item = {k: v for k, v in item.items() if v not in (None, "")}
            if not item.get("k"):
                return False
        else:
            item = (item or "").strip()[:400]
        if not item:
            return False
        with self.tx() as c:
            row = c.execute("SELECT * FROM relay_watch WHERE id=? AND status IN "
                            "('waiting','collecting')", (int(watch_id),)).fetchone()
            if not row or int(row["msg_count"] or 0) >= 10:
                return False
            if not self.relay_reply_is_new(row["asked_ts"], row["reply_after_seq"], now, sort_seq):
                return False
            if message_id is not None:
                inserted = c.execute(
                    "INSERT OR IGNORE INTO relay_reply_events(watch_id,inbound_id) VALUES(?,?)",
                    (watch_id, int(message_id)))
                if not inserted.rowcount:
                    return False
            try:
                items = _json.loads(row["reply_text"] or "[]")
            except Exception:
                items = []
            items.append(item)
            started = row["started_ts"] or now
            c.execute(
                "UPDATE relay_watch SET reply_text=?, msg_count=msg_count+1, "
                "started_ts=?, status='collecting', updated_ts=? WHERE id=?",
                (_json.dumps(items[-10:], ensure_ascii=False), started, now,
                 int(watch_id)))
        return True

    def add_relay_reply(self, watch_id: int, text: str,
                        ts: int | None = None, *, message_id: int | None = None,
                        sort_seq: int | None = None) -> bool:
        """把 B 的一条消息记进这次代传话；返回是否记上。"""
        return self._append_relay_item(watch_id, text, ts, message_id=message_id,
                                       sort_seq=sort_seq)

    def add_relay_media(self, watch_id: int, path: str = "", label: str = "图片",
                        md5: str = "", ts: int | None = None, *,
                        message_id: int | None = None, sort_seq: int | None = None,
                        local_id=None, shard=None) -> bool:
        """把 B 发来的一张图/一个表情记进这次代传话。

        只存本地路径（和 md5），**不存 base64**：库里塞图片会让 queue.db 疯长；
        而 ``media_cache`` 里本来就是解密好的成品。万一缓存被清，封口时还能拿
        md5 重新解一次（``media.resolve_any``）。
        """
        if not (path or md5 or local_id):
            return False
        return self._append_relay_item(
            watch_id, {"k": "image", "p": path or "", "m": (md5 or "").lower(),
                       "l": label or "图片", "local_id": local_id, "shard": shard},
            ts, message_id=message_id, sort_seq=sort_seq)

    def due_relay_watch(self, now: float | None = None, *,
                        account: str | None = None) -> list[dict]:
        """该汇总的代传话：开始回复后满 5 分钟或 10 条，或者问出去 60 分钟没回。"""
        now = int(time.time() if now is None else now)
        scope, args = _account_clause(account)
        with self._conn() as c:
            # 升级前可能同一账号、同一收件人同时有两个已送达的代问，无法区分答复。
            c.execute(
                "UPDATE relay_watch SET status='ambiguous', updated_ts=? "
                "WHERE status IN ('waiting','collecting')" + scope + " AND (account,target_chat) IN ("
                "SELECT account,target_chat FROM relay_watch WHERE status IN ('waiting','collecting') "
                "GROUP BY account,target_chat HAVING COUNT(*)>1)", (now, *args))
            c.execute(
                "UPDATE relay_watch SET status='undelivered', updated_ts=? "
                "WHERE status='pending_send'" + scope + " AND EXISTS ("
                "SELECT 1 FROM reminders r LEFT JOIN reminder_occurrences oc ON oc.reminder_id=r.id "
                "LEFT JOIN outbound o ON o.id=oc.outbound_id "
                "WHERE r.id=relay_watch.reminder_id AND r.account=relay_watch.account AND ("
                "r.status='cancelled' OR oc.status='missed' OR o.status IN ('expired','cancelled') "
                "OR (o.status IN ('hold','failed','unverified') AND o.expires_ts<=?)))",
                (now, *args, now))
            return [dict(row) for row in c.execute(
                "SELECT * FROM relay_watch WHERE ("
                "(status='collecting' AND (started_ts<=? OR msg_count>=10)) "
                "OR (status='waiting' AND asked_ts<=?))" + scope +
                " ORDER BY id",
                (now - 300, now - 3600, *args),
            ).fetchall()]

    def set_relay_watch(self, id_: int, status: str, conn=None) -> None:
        sql = "UPDATE relay_watch SET status=?, updated_ts=? WHERE id=?"
        args = (status, int(time.time()), int(id_))
        if conn is not None:
            conn.execute(sql, args)
            return
        with self._conn() as c:
            c.execute(sql, args)

    # ---------------------------------------------------- 待澄清（缺钟点）
    def add_clarify(self, account: str, chat: str, content: str, base_ts: int,
                    ask: str, ttl_hours: int = 24, rule: str = "",
                    proposed_ts: int | None = None, images=None, *,
                    source_text: str = "", tz_zone: str | None = None,
                    conn=None) -> int:
        """记一条「还差口气，得问他」的待确认。

        ``rule`` + ``proposed_ts`` 是模型给的提案（「每周二 13:50」）；
        他回一句「对」就直接按提案建单，回「不对，周三」就按新说法重建。
        """
        import json as _json

        now = int(time.time())
        blob = _json.dumps(images[:3], ensure_ascii=False) if images else ""
        sql = ("INSERT INTO reminder_clarify(account, chat, content, base_ts, ask, "
               "rule, proposed_ts, images, status, created_ts, expires_ts, source_text, tz_zone) "
               "VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?,?)")
        args = (account, chat, (content or "")[:60], int(base_ts), (ask or "")[:120],
                (rule or "")[:40], int(proposed_ts) if proposed_ts else None, blob,
                now, now + int(ttl_hours) * 3600, (source_text or "")[:300], tz_zone)
        if conn is not None:
            return int(conn.execute(sql, args).lastrowid)
        with self._conn() as c:
            return int(c.execute(sql, args).lastrowid)

    def _sync_clarify_prompt(self, conn, chat: str, account: str | None) -> None:
        scope, args = _account_clause(account)
        conn.execute(
            "UPDATE reminder_clarify SET status='asked', asks=asks+1 "
            "WHERE chat=? AND status='pending'" + scope +
            " AND prompt_outbound_id IN (SELECT id FROM outbound WHERE status='sent' AND verified=1)",
            (chat, *args))
        conn.execute(
            "UPDATE reminder_clarify SET status='expired' "
            "WHERE chat=? AND status='pending'" + scope +
            " AND prompt_outbound_id IN (SELECT id FROM outbound "
            "WHERE status IN ('cancelled','expired','unverified') "
            "OR (status='failed' AND (retryable=0 OR attempts>=?)))",
            (chat, *args, OUTBOUND_MAX_ATTEMPTS))

    def get_clarify(self, chat: str, status: str = "pending", *,
                    account: str | None = None, conn=None) -> dict | None:
        """取该会话最先等待确认的记录，附上核对消息的实际送达时间。"""
        scope, args = _account_clause(account, "r.account")
        if conn is None:
            with self.tx() as c:
                return self.get_clarify(chat, status, account=account, conn=c)
        self._sync_clarify_prompt(conn, chat, account)
        row = conn.execute(
            "SELECT r.*, o.sent_ts AS prompt_sent_ts, o.verified_seq AS prompt_verified_seq "
            "FROM reminder_clarify r LEFT JOIN outbound o ON o.id=r.prompt_outbound_id "
            "AND o.status='sent' AND o.verified=1 "
            "WHERE r.chat=? AND r.status=? AND r.expires_ts>?" + scope +
            " ORDER BY r.id LIMIT 1",
            (chat, status, int(time.time()), *args)).fetchone()
        return dict(row) if row else None

    def get_clarify_by_id(self, id_: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM reminder_clarify WHERE id=?", (int(id_),)).fetchone()
        return dict(row) if row else None

    def enqueue_clarify_prompt(self, account: str, chat: str, format_ask, conn=None) -> int | None:
        """同一会话一次只发送一个待核对的问句。"""
        if conn is None:
            with self.tx() as c:
                return self.enqueue_clarify_prompt(account, chat, format_ask, conn=c)
        asked = self.get_clarify(chat, "asked", account=account, conn=conn)
        if asked and asked["prompt_sent_ts"]:
            return None
        if asked:
            # 旧版用普通回复生成核对，无法知道对方是否看到准确问句。
            conn.execute(
                "UPDATE reminder_clarify SET status='pending' WHERE id=?", (asked["id"],))
        rec = self.get_clarify(chat, account=account, conn=conn)
        if not rec or rec["prompt_outbound_id"]:
            return None
        ask = format_ask(rec)
        out_id = self.create_outbound(
            account, chat, ask, segments=[ask], kind="reminder_clarify",
            ref_id=rec["id"], expires_ts=rec["expires_ts"], conn=conn)
        conn.execute(
            "UPDATE reminder_clarify SET prompt_outbound_id=?, ask=? WHERE id=?",
            (out_id, ask, rec["id"]))
        return out_id

    def clear_clarify_proposal(self, id_: int, ask: str, conn) -> None:
        conn.execute(
            "UPDATE reminder_clarify SET proposed_ts=NULL, ask=? WHERE id=?",
            (ask, int(id_)))

    def set_clarify(self, id_: int, status: str, conn=None) -> None:
        if conn is None:
            with self.tx() as c:
                self.set_clarify(id_, status, conn=c)
                return
        conn.execute("UPDATE reminder_clarify SET status=? WHERE id=?", (status, int(id_)))
        if status in ("done", "expired", "cancelled"):
            self._cancel_clarify_outbound(conn, int(id_))
        if status == "pending":
            conn.execute(
                "UPDATE reminder_clarify SET prompt_outbound_id=NULL WHERE id=?", (int(id_),))

    @staticmethod
    def _cancel_clarify_outbound(conn, id_: int) -> None:
        conn.execute(
            "UPDATE outbound SET status='cancelled', retryable=0, updated_ts=?, "
            "error='提醒核对已结案' WHERE kind='reminder_clarify' AND ref_id=? "
            "AND status IN ('draft','hold','sending','failed') AND attempt_dispatched=0",
            (int(time.time()), id_))

    def cancel_pending_reminders(self, account: str | None, chat: str, target: str) -> int:
        scope, args = _account_clause(account)
        with self.tx() as c:
            rows = c.execute(
                "SELECT id, content FROM reminder_clarify "
                "WHERE chat=? AND status IN ('pending','asked') AND expires_ts>?" + scope,
                (chat, int(time.time()), *args)).fetchall()
            n = 0
            for row in rows:
                if not target or target in row["content"]:
                    self.set_clarify(row["id"], "cancelled", conn=c)
                    n += 1
            candidates = c.execute(
                "SELECT id, account, msg_id FROM reminder_candidates "
                "WHERE chat=? AND status='pending' AND instr(text,?)>0" + scope,
                (chat, target, *args)).fetchall()
            for row in candidates:
                self.set_candidate(row["id"], "skipped", "用户取消委托", conn=c)
                self.record_action(row["account"], chat, str(row["msg_id"]), None,
                                   "candidate_cancelled", conn=c)
                n += 1
            return n

    def expire_clarify(self) -> int:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE reminder_clarify SET status='expired' "
                "WHERE status IN ('pending','asked') AND expires_ts<=?",
                (int(time.time()),))
            return cur.rowcount

    # ---------------------------------------------------------------- 主动聊天
    def get_proactive_state(self, account: str, chat: str) -> dict | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM proactive_state WHERE account=? AND chat=?",
                (account, chat)).fetchone()
        return dict(r) if r else None

    def ensure_proactive_state(self, account: str, chat: str,
                               trial_days: int = 3) -> dict:
        """没有就建一行，并在首次启用时开始计试发期。"""
        now = int(time.time())
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO proactive_state(account, chat, next_at, "
                "trial_until, created_ts, updated_ts, day, day_n) "
                "VALUES(?,?,0,?,?,?,?,0)",
                (account, chat, now + max(0, int(trial_days)) * 86400, now, now,
                 time.strftime("%Y-%m-%d")))
        return self.get_proactive_state(account, chat) or {}

    def update_proactive_state(self, account: str, chat: str, **fields) -> int:
        allowed = {"next_at", "quiet_until", "cooldown_until", "muted",
                   "last_self_ts", "activity_version", "trial_until",
                   "trial_passed", "last_proactive_id", "await_reply_until",
                   "no_reply_streak", "day", "day_n"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append("%s=?" % k)
            args.append(v)
        if not sets:
            return 0
        sets.append("updated_ts=?")
        args.extend([int(time.time()), account, chat])
        with self._conn() as c:
            return c.execute(
                "UPDATE proactive_state SET " + ", ".join(sets) +
                " WHERE account=? AND chat=?", args).rowcount

    def bump_activity(self, account: str, chat: str) -> int:
        """会话有新动静：活动版本 +1。生成前记版本、入队/发送前比对，
        不一致就把过期草稿丢掉——避免在对方正说话时插嘴。"""
        with self._conn() as c:
            c.execute(
                "UPDATE proactive_state SET activity_version=activity_version+1, "
                "updated_ts=? WHERE account=? AND chat=?",
                (int(time.time()), account, chat))
            r = c.execute(
                "SELECT activity_version FROM proactive_state "
                "WHERE account=? AND chat=?", (account, chat)).fetchone()
        return int(r["activity_version"]) if r else 0

    def list_proactive_states(self, account: str,
                              limit: int = 200) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM proactive_state WHERE account=? ORDER BY next_at "
                "LIMIT ?", (account, int(limit))).fetchall()]

    def claim_proactive_slot(self, account: str, chat: str, day: str,
                             per_chat_daily: int, next_at: int) -> bool:
        """原子地占一个当日配额并推进 next_at。

        检查与预留在同一个 UPDATE 里完成，避免并发 tick 重复开口。
        ``per_chat_daily<=0`` 表示不限。
        """
        now = int(time.time())
        with self._conn() as c:
            result = c.execute(
                "UPDATE proactive_state SET day=?, "
                "day_n=CASE WHEN day=? THEN COALESCE(day_n, 0) + 1 ELSE 1 END, "
                "next_at=?, updated_ts=? WHERE account=? AND chat=? "
                "AND (? <= 0 OR day IS NOT ? OR COALESCE(day_n, 0) < ?)",
                (day, day, int(next_at), now, account, chat, per_chat_daily, day, per_chat_daily))
            return result.rowcount == 1

    # ---------------------------------------------------------------- 钩子库
    def add_hook(self, account: str, chat: str, topic_key: str, kind: str,
                 text: str, expires_ts: int, source_msg_id: int | None = None,
                 earliest_ts: int = 0) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO hooks(account, chat, source_msg_id, "
                "topic_key, kind, text, earliest_ts, expires_ts, status, created_ts) "
                "VALUES(?,?,?,?,?,?,?,?,'open',?)",
                (account, chat, source_msg_id, (topic_key or "")[:40],
                 kind or "topic", (text or "")[:120], int(earliest_ts),
                 int(expires_ts), int(time.time())))
            return cur.rowcount > 0

    def pick_hook(self, chat: str, now: float | None = None, *,
                  account: str | None = None) -> dict | None:
        """挑一个可用钩子：快过期的优先，其次最新的。"""
        now = int(time.time() if now is None else now)
        scope, args = _account_clause(account)
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM hooks WHERE chat=? AND status='open' "
                "AND expires_ts>? AND earliest_ts<=?" + scope +
                " ORDER BY expires_ts ASC, id DESC LIMIT 1",
                (chat, now, now, *args)).fetchone()
        return dict(r) if r else None

    def mark_hook(self, id_: int, status: str):
        with self._conn() as c:
            c.execute("UPDATE hooks SET status=? WHERE id=?",
                      (status, int(id_)))

    def list_hooks(self, chat: str, status: str = "open") -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM hooks WHERE chat=? AND status=? ORDER BY id DESC",
                (chat, status)).fetchall()]

    def expire_hooks(self, now: float | None = None) -> int:
        now = int(time.time() if now is None else now)
        with self._conn() as c:
            return c.execute(
                "UPDATE hooks SET status='expired' WHERE status='open' "
                "AND expires_ts<=?", (now,)).rowcount

    # ---------------------------------------------------------------- 会话锁
    def acquire_chat(self, chat: str, ttl: int = 180) -> bool:
        now = int(time.time())
        with self._conn() as c:
            row = c.execute("SELECT until_ts FROM lock_chat WHERE chat=?", (chat,)).fetchone()
            if row and row["until_ts"] > now:
                return False
            c.execute(
                "INSERT INTO lock_chat(chat, until_ts) VALUES(?,?) "
                "ON CONFLICT(chat) DO UPDATE SET until_ts=excluded.until_ts",
                (chat, now + ttl),
            )
            return True

    def release_chat(self, chat: str):
        with self._conn() as c:
            c.execute("DELETE FROM lock_chat WHERE chat=?", (chat,))

    def clear_locks(self) -> int:
        """进程启动时清掉上一进程留下的会话锁（否则要等 TTL 过期才处理）。"""
        with self._conn() as c:
            cur = c.execute("DELETE FROM lock_chat")
            return cur.rowcount


if __name__ == "__main__":
    import json
    import sys

    os.system("chcp 65001 >nul 2>&1")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    q = Queue()
    print("队列库:", q.db_path)
    print(json.dumps(q.stats(), ensure_ascii=False, indent=2))
