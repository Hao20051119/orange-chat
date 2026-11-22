# -*- coding: utf-8 -*-
"""自动回复工作进程 v3 —— 采集 / 生成 / 发送 / 对账 四段式。

关键变化（对应修正计划批次 A/B/C）
----------------------------------
* 采集按会话持久化 ``sort_seq`` 水位，只有该水位上的消息全部处理后才推进。
* 生成按会话并行（线程池），同一会话串行；慢请求不再堵住其他人。
* 记忆写入只发生在两处：批次边界（user，``in:`` 键）与确认发出后（assistant，
  ``out:`` 键），重生成/重启都不会重复写。
* 发送前过完整门禁（``gate.evaluate``），暂停/关开关/时段外真正拦得住。
* 长回复按气泡分段，``segments_sent`` 记录进度，断点只补未发的段。
* ``uncertain`` 任务由对账线程回读微信库收敛，不盲目重发。
* 进程中断留下的 ``sending`` 在启动时转 ``uncertain``。

用法：python agent\\reply_worker.py [--dry]
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from itertools import groupby

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(ROOT, "memory"))
sys.path.insert(0, os.path.join(ROOT, "stickers"))

from logutil import get_logger, redact, setup_stdio

setup_stdio()
log = get_logger()

PYLIB = os.environ.get("WXBOT_PYLIB") or os.path.join(ROOT, "pylibs")
if os.path.isdir(PYLIB) and PYLIB not in sys.path:
    sys.path.insert(0, PYLIB)

import gate as gate_mod                                 # noqa: E402
import groupchat                                        # noqa: E402
import initiative                                       # noqa: E402
import media                                            # noqa: E402
import msgtype                                          # noqa: E402
import proactive as proactive_mod                       # noqa: E402
import relay as relay_mod                               # noqa: E402
import reminder as reminder_mod                         # noqa: E402
import runtime_lock                                     # noqa: E402
from brain import Brain                                 # noqa: E402
from split import split_reply                           # noqa: E402
from wx_identity import open_bound_db                   # noqa: E402
from wxqueue import OUTBOUND_MAX_ATTEMPTS, Queue        # noqa: E402

DRY = "--dry" in sys.argv

# 图片相关默认值：配置里缺键时用它，避免老配置把「看图」整个关掉
_VISION_DEFAULTS = {
    "show_images": True,
    "max_images_per_turn": 4,
    "max_bytes": 6 * 1024 * 1024,
    "content_desc": True,
    "content_desc_chars": 240,
}

_stop = threading.Event()


def load_cfg() -> dict:
    """统一走 config.py：校验 + 半写入回退 + 环境变量密钥。"""
    try:
        from config import load as _load
        cfg = _load()
        if cfg:
            return cfg
    except Exception as e:
        log.warning("读取配置失败，回退原始文件：%s" % e)
    return {}


def post(path: str, obj: dict, timeout: int = 90) -> dict:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    cfg = load_cfg()
    tok = (cfg.get("token") or "").strip()
    if tok:
        headers["X-Token"] = tok
    port = int(os.environ.get("WXBOT_PORT") or cfg.get("port") or 8765)
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as response:
        with response:
            return json.loads(response.read().decode("utf-8"))
    with response:
        return json.loads(response.read().decode("utf-8"))


def segments_of(task: dict) -> list[str]:
    raw = task.get("segments")
    if raw:
        try:
            segs = json.loads(raw)
            if isinstance(segs, list) and segs:
                return [str(x) for x in segs]
        except Exception:
            pass
    return [task.get("text") or ""]


def media_only_hint(msgs: list[dict], recent_reply: bool) -> str:
    """纯表情/图片不是"一定不回"，而是给模型一个判断依据。

    表情包本身可能在表达吐槽/撒娇/回应上一句，该回就回；但如果刚回过一句，
    对方只是补了个表情，通常就不必再回。

    注意只有**图片/表情/视频**才算"没有字"：引用回复、链接卡片、文件、位置
    这些虽然也不是文本类型，但带着真实内容（引用回复里还有被引用的原文和
    这次说的话），把它们当表情会诱导模型漏回。
    """
    has_words = any(
        msgtype.kind(m.get("msg_type"), m.get("type_code")) not in _MEDIA_KINDS
        for m in msgs)
    if has_words:
        return ""
    if recent_reply:
        return ("对方这条只有表情/图片，而且你刚回过一句；如果它只是附和或催回复，"
                "就别再回一条，必要时只回一两个字。")
    return ("对方这条只有表情/图片，本身也可能在表达意思（吐槽、撒娇、回应你之前的话）；"
            "看得懂就接一句，看不懂或只是附和就别回。")


# 这些类型才算"只有媒体、没有字"
_MEDIA_KINDS = ("image", "emoji", "video")

# ---------------------------------------------------------------- 回复节奏
# 对方一次说话可能被拆成好几条（一句话分两次发、句子后面补个表情包）。
# 以前每条都单独算一轮 → Orange 一句一句跟着回，看着很死板。这里按"最后一条
# 是不是说完了"给三档静默，让同一句话尽量落进同一轮。
_UNFINISHED_PUNCT = ("，", ",", "、", "：", ":", "…", "...", "～", "~", "（", "(")
_UNFINISHED_TAILS = (
    "要不", "要么", "不然", "但是", "但", "不过", "然后", "而且", "因为", "所以",
    "如果", "要是", "其实", "反正", "还有", "对了", "话说", "我们", "我跟你",
    "我觉得", "我觉着", "我想说", "想说", "刚想", "我刚", "就是说", "那个",
    "这个", "等我", "跟他说", "问你个事", "帮我", "我有点",
)
_COMPLETE_TAILS = ("吗", "嘛", "么", "呢", "吧", "？", "?", "。", "！", "!")

# 「嗯」「哈哈」这类附和：刚回过一句时没必要再答一遍
_TRIVIAL_TEXTS = {
    "嗯", "嗯嗯", "嗯呢", "哦", "哦哦", "喔", "噢", "好", "好的", "好啊", "好呀",
    "行", "行吧", "行啊", "对", "对对", "是", "是的", "收到", "ok", "OK", "okay",
    "哈哈", "哈哈哈", "哈哈哈哈", "嘿嘿", "呵", "在", "在的", "可以", "知道了",
    "啊这", "啊", "哦好", "好嘞", "好滴",
}


def _last_text(msg: dict) -> str:
    return (msg.get("content") or "").strip()


def ends_unfinished(text: str) -> bool:
    """这句话看起来还没说完（「要不我们」「我刚想说」「不过，」…）。"""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith(_UNFINISHED_PUNCT):
        return True
    tail = t.rstrip("。！？!?~～… ")
    return any(tail.endswith(w) for w in _UNFINISHED_TAILS)


def looks_complete(text: str) -> bool:
    """意思已经完整（问句、带句末标点、或者就一两个字的短句）。"""
    t = (text or "").strip()
    if not t:
        return False
    if "？" in t or "?" in t:
        return True
    if t.endswith(_COMPLETE_TAILS):
        return True
    # 只认一两个字的「好」「嗯」「在」：四个字的「我刚下班」还可能是半句
    return len(t) <= 2


def quiet_seconds_for(msgs, merge_cfg: dict | None = None) -> float:
    """按最后一条消息的完整程度决定"等多久再开口"。

    * 意思明确的短句 ≈ 2 秒（``quiet_complete_seconds``）
    * 普通消息 ≈ 3.5 秒（``quiet_seconds``）
    * 明显半句 ≈ 5 秒（``quiet_incomplete_seconds``）
    * 纯表情/图片 ≈ 1.5 秒（``quiet_media_seconds``，后面基本不会有下文）

    只对**明显的半句**用长窗口：微信里省略标点很常见，不能一见没句号就等 5 秒。
    """
    mc = merge_cfg or {}
    base = float(mc.get("quiet_seconds", 3.5))
    if not msgs:
        return base
    last = msgs[-1] if isinstance(msgs, (list, tuple)) else msgs
    if not isinstance(last, dict):
        return base
    k = msgtype.kind(last.get("msg_type"), last.get("type_code"))
    if k in ("emoji", "image", "video"):
        return float(mc.get("quiet_media_seconds", 1.5))
    text = _last_text(last)
    if not text:
        return base
    if ends_unfinished(text):
        return float(mc.get("quiet_incomplete_seconds", 5.0))
    if looks_complete(text):
        return float(mc.get("quiet_complete_seconds", 2.0))
    return base


def is_trivial_text(text: str) -> bool:
    if "?" in (text or "") or "？" in (text or ""):
        return False
    t = (text or "").strip().strip("。，、…~～!！ ")
    return bool(t) and len(t) <= 4 and t.lower() in _TRIVIAL_TEXTS


def absorbable_supplement(msgs: list[dict]) -> bool:
    """这批补充消息是不是"只起语气作用"：纯表情，或纯「嗯/哈哈」这类附和。

    是的话就并进已有的那一轮，不再单独回一条——对方发个表情包只是修饰，
    不是新话题。图片、视频、语音、文件、引用都**不算**：它们可能带着真问题，
    仍然交给模型判断要不要接。
    """
    if not msgs:
        return False
    for m in msgs:
        k = msgtype.kind(m.get("msg_type"), m.get("type_code"))
        if k == "emoji":
            continue
        if k == "text" and is_trivial_text(m.get("content")):
            continue
        return False
    return True


def batch_hint(msgs: list[dict]) -> str:
    """几条连着发 → 告诉模型这是同一次说话，只回一次。"""
    if not msgs or len(msgs) < 2:
        return ""
    return ("【对方连着发了 %d 条】这几条是同一次说话：可能一句话拆成两次发，"
            "也可能说完又补了个表情；合起来理解，**只回一次**，"
            "别一句一条地跟着回。" % len(msgs))


class Worker:
    # 被 @ 但模型两次都想跳过时的兜底（必须回）
    FORCED_REPLIES = ["在呀，说事", "叫我就说话呀", "在，怎么了", "嗯？我在呢"]

    def __init__(self):
        self.cfg = load_cfg()
        self.acc = self.cfg.get("account")
        self._check_bound_cfg(self.cfg)
        self._collector_started_at = int(time.time())
        self.db = open_bound_db(self.acc)
        info = self.db.get_self_info() or {}
        self.me = info.get("username") or ""
        expected = os.environ.get("WXBOT_ACCOUNT_WXID") or self.db.wxid
        if self.db.account != self.acc or not expected or self.db.wxid != expected or self.me != expected:
            raise SystemExit("实际微信数据库与选定的小号不一致，停止启动")
        self.nick = info.get("nick_name") or ""
        self.q = Queue()
        self.brain = Brain(self.cfg)
        self.directory = groupchat.GroupDirectory(self.db)
        self._watermark: dict[str, int] = {}
        self._HOOK_LAST: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()
        # 采集健康度：每个状态周期汇总一次（长期为 0 就是采集器没在扫）
        self._scan_stats = {"rounds": 0, "sessions": 0, "enqueued": 0}

        log.info("启动 账号=%s 昵称=%s dry=%s" % (self.acc, info.get("nick_name"), DRY))
        log.info("白名单=%s 群聊=%s 记忆=%s" % (
            self.brain.agent.get("reply_whitelist"),
            self.brain.agent.get("group_reply"),
            "长期" if getattr(self.brain.mem, "persistent", True) else "临时"))

        n = self.q.recover_inflight(age=0, account=self.acc)
        if n:
            log.warning("恢复中断任务 %d 条 → uncertain（先核验不重发）" % n)
        stale = self.q.clear_locks()
        if stale:
            log.warning("清理上次进程残留的会话锁 %d 个" % stale)
        self.q.prune_robot_sent(days=7)

    def _scan_tick(self, field: str = "", n: int = 1) -> dict:
        """更新/取采集健康度计数；懒建立，兼容只 new 出实例的测试与旧对象。"""
        stats = getattr(self, "_scan_stats", None)
        if stats is None:
            stats = self._scan_stats = {"rounds": 0, "sessions": 0, "enqueued": 0}
        if field:
            stats[field] = stats.get(field, 0) + n
        return stats

    def _check_bound_cfg(self, cfg: dict):
        pinned = os.environ.get("WXBOT_ACCOUNT") or self.acc
        if not self.acc or pinned != self.acc or cfg.get("account") != self.acc:
            raise RuntimeError("微信账号配置与本次绑定的小号不一致，请重新启动")

    def _load_bound_cfg(self) -> dict:
        cfg = load_cfg()
        self._check_bound_cfg(cfg)
        return cfg

    # ================================================================ 采集
    def _on_msg(self, msg: dict, lst=None) -> bool:
        """Listener 回调：只做判断 + 入队，绝不等待模型或发送。

        返回 True 表示真的新入队了一条（补读时用来计数，避免把重复扫描算进去）。
        """
        try:
            chat = msg.get("username") or ""
            if not chat:
                return
            ts = int(msg.get("create_time") or 0)
            mtype = msg.get("type")
            type_code = msg.get("type_code")
            if isinstance(mtype, int) and type_code is None:
                type_code = mtype
            content = msg.get("content") or ""
            sender = msg.get("sender_username") or ""

            # 群消息正文前面会拼一行 "wxid_xxx:"，留着会进模型和记忆，
            # 也会让"自己发的消息"匹配不上 robot_sent 记录，所以先剥掉
            if (chat or "").endswith("@chatroom"):
                content = groupchat.strip_sender_prefix(content)

            # 自己发的：机器人登记过的忽略；没登记的是本人手动发言
            if sender and self.me and sender == self.me:
                if not self.q.was_robot_sent(chat, content, ts or int(time.time())):
                    # self.cfg 由处理线程每 0.5 秒刷新，别在这里再读一次盘
                    tk = self.cfg.get("takeover") or {}
                    try:
                        if getattr(self.brain.mem, "persistent", True):
                            kind = msgtype.kind(mtype, type_code)
                            text = content if kind == "text" else \
                                msgtype.describe(mtype, content, type_code)["text"]
                            if text:
                                self.brain.mem.add(
                                    chat, "assistant", text, ts=ts or None,
                                    src_key="self:%s:%s"
                                            % (chat, msg.get("local_id") or ts))
                    except Exception as e:
                        log.warning("写入手动发言记忆失败：%s" % e)
                    if tk.get("enabled", False):
                        minutes = int(tk.get("pause_minutes", 30))
                        self.q.pause_chat(chat, minutes=minutes, reason="manual")
                        n = self.q.cancel_chat_drafts(chat, account=self.acc)
                        log.info("检测到本人手动发言，暂停 [%s] %d 分钟，取消草稿 %d 条"
                                 % (chat, minutes, n))
                    else:
                        log.info("检测到本人手动发言（接管暂停已关闭，继续自动回复）[%s]"
                                 % chat)
                    self.q.bump_activity(self.acc, chat)
                return False

            if msgtype.kind(mtype, type_code) == "system":
                return False

            if self.q.enqueue_inbound(
                    self.acc, chat, msg.get("local_id"), ts, str(mtype or ""),
                    content, sender=sender, shard=msg.get("shard"),
                    sort_seq=msg.get("sort_seq"), type_code=type_code):
                log.info("入队 [%s] %s %s" % (
                    chat, msgtype.kind(mtype, type_code),
                    redact((content or "")[:40].replace("\n", " "))))
                return True
            # 入队失败只可能是"库里已有同一条"（UNIQUE(account, chat, local_id, msg_ts)
            # 命中）。必须留痕：以前这里静默 return False，看起来像消息没来过。
            log.info("跳过 [%s]：库里已有同一条消息（local_id=%s，msg_ts=%s）"
                     % (chat, msg.get("local_id"), ts))
            return False
        except Exception as e:
            log.error("采集回调异常：%s: %s" % (type(e).__name__, e))
            raise

    def run_collector(self):
        log.info("持续采集已启动")
        while not _stop.is_set():
            try:
                self._load_bound_cfg()
                self.catch_up()
            except Exception as e:
                log.error("采集异常：%s: %s" % (type(e).__name__, e))
            _stop.wait(1.0)

    def catch_up(self):
        """补读已知会话，首次看见的会话从程序启动时刻开始。"""
        sessions = self.db.get_sessions(limit=500)
        self._scan_tick("rounds")
        self._scan_tick("sessions", len(sessions))
        for session in sessions:
            chat = session.get("username")
            if not chat or _stop.is_set():
                continue
            try:
                self._collect_chat(chat)
            except Exception as e:
                log.warning("采集 [%s] 将重试：%s: %s" % (chat, type(e).__name__, e))

    def _collect_chat(self, chat: str):
        since = self._watermark.get(chat)
        if since is None:
            since = self.q.get_collect_watermark(self.acc, chat)
        if since is None:
            since = self.q.max_seq(chat, account=self.acc) or self._initial_watermark(chat)
            self.q.set_collect_watermark(self.acc, chat, since)
            self._watermark[chat] = since

        limit = 201
        rows = self.db.get_new_messages(chat, since_seq=since, limit=limit)
        # 同一 sort_seq 的消息一起确认，分页不得从这组中间截断。
        while len(rows) == limit and rows[0]["sort_seq"] == rows[-1]["sort_seq"]:
            limit *= 2
            rows = self.db.get_new_messages(chat, since_seq=since, limit=limit)
        if len(rows) == limit:
            last_seq = rows[-1]["sort_seq"]
            rows = [row for row in rows if row["sort_seq"] < last_seq]
        for seq, messages in groupby(rows, key=lambda row: int(row["sort_seq"])):
            for msg in messages:
                if self._on_msg(dict(msg, username=chat)):
                    self._scan_tick("enqueued")
            self.q.set_collect_watermark(self.acc, chat, seq)
            self._watermark[chat] = seq

    def _initial_watermark(self, chat: str) -> int:
        limit = 8
        rows = self.db.get_messages(chat, limit=limit)
        while len(rows) == limit and int(rows[-1].get("create_time") or 0) >= self._collector_started_at:
            limit *= 2
            rows = self.db.get_messages(chat, limit=limit)
        live = [int(row["sort_seq"]) for row in rows
                if int(row.get("create_time") or 0) >= self._collector_started_at]
        if live:
            return max(0, min(live) - 1)
        return int(rows[0]["sort_seq"]) if rows else 0

    # ================================================================ 生成
    def _acquire(self, chat: str) -> bool:
        with self._inflight_lock:
            if chat in self._inflight:
                return False
            self._inflight.add(chat)
            return True

    def _release(self, chat: str):
        with self._inflight_lock:
            self._inflight.discard(chat)

    def _backfill(self, chat: str, msgs: list[dict]) -> list[dict]:
        """只过期超时的消息，一次正常连发不会挤掉本轮的开头。"""
        merge_cfg = self.cfg.get("merge") or {}
        keep, expired = gate_mod.backfill(
            msgs,
            keep=len(msgs),
            max_age=int(merge_cfg.get("max_age_seconds", 21600)),
        )
        if expired:
            self.q.mark_inbounds(expired, "expired")
            log.info("过期/积压：%d 条不再回复 [%s]" % (len(expired), chat))
        return keep

    def _kb_enqueue(self, m: dict, info: dict):
        """微信收到真附件时登记入库任务。只入队，解析交给后台线程。"""
        if not (self.cfg.get("kb") or {}).get("enabled", False):
            return
        if not self.cfg.get("kb", {}).get("auto_ingest_files", True):
            return
        try:
            import kb
            key = "inbound:%s" % m["id"]
            r = kb.enqueue_file(
                event_key=key, chat=m.get("chat") or "",
                title=info.get("title") or "", ext=info.get("ext") or "",
                msg_ts=m.get("msg_ts"), sender=m.get("sender") or "",
                file_md5=info.get("file_md5") or "")
            if r.get("queued"):
                log.info("kb 收到文件待入库：%s" % (info.get("title") or "")[:60])
            elif not r.get("ok"):
                log.info("kb 附件未自动入库：%s" % r.get("error"))
        except Exception as e:
            log.warning("kb 入队失败：%s: %s" % (type(e).__name__, e))

    # ------------------------------------------------------------ 节奏与合并
    def _unsent_drafts(self, chat: str) -> list[dict]:
        """这条会话最近还没发出的自动回复草稿（含已被作废的，用来取文字）。"""
        within = int((self.cfg.get("merge") or {}).get("draft_merge_seconds", 300))
        try:
            return self.q.unsent_reply_drafts(chat, within=within, account=self.acc)
        except Exception as e:
            log.warning("查询未发草稿失败：%s" % e)
            return []

    def _note_absorbed(self, msgs: list[dict]):
        """把"只是语气"的补充消息写进记忆（不进模型，不再回一条）。"""
        pairs = []
        for m in msgs:
            info = msgtype.describe(m.get("msg_type") or "",
                                    m.get("content") or "", m.get("type_code"))
            if info.get("text"):
                pairs.append((m, info["text"]))
        if pairs:
            self._record_user(pairs)

    def _absorb_into_draft(self, chat: str, msgs: list[dict]) -> bool:
        """对方只是补了个表情/「嗯」，而已有一条没发出的草稿 → 并进去，不另回。"""
        if not absorbable_supplement(msgs):
            return False
        drafts = [d for d in self._unsent_drafts(chat) if d.get("status") == "draft"]
        if not drafts:
            return False
        newest = max(int(m["id"]) for m in msgs)
        if int(drafts[0].get("src_msg_id") or 0) >= newest:
            return False                   # 这条草稿已经答过它们了
        self._note_absorbed(msgs)
        self.q.mark_inbounds([m["id"] for m in msgs], "done")
        log.info("补充并进未发草稿 #%d（%d 条只是语气），不另回 [%s]"
                 % (drafts[0]["id"], len(msgs), chat))
        return True

    def _absorb_silently(self, chat: str, msgs: list[dict]) -> bool:
        """刚回过一句（或正在发），对方只补了表情/附和 → 这轮自然结束，不再回。"""
        if not absorbable_supplement(msgs):
            return False
        window = float((self.cfg.get("merge") or {}).get("absorb_after_seconds", 30))
        try:
            last = self.q.last_reply_activity(chat, within=int(window), account=self.acc)
        except Exception as e:
            log.warning("查询最近回复失败：%s" % e)
            last = None
        if not last:
            return False
        stamp = int(last.get("sent_ts") or last.get("created_ts") or 0)
        if not stamp or time.time() - stamp > window:
            return False
        self._note_absorbed(msgs)
        self.q.mark_inbounds([m["id"] for m in msgs], "done")
        log.info("刚回过话（#%d %s），对方只补了表情/附和，不再回 [%s]"
                 % (last["id"], last.get("status"), chat))
        return True

    def _fold_unsent_drafts(self, chat: str, msgs: list[dict]) -> str:
        """回复还没发出就收到补充 → 旧草稿作废，它的文字并进本轮一起回。

        返回给模型的提示（没有可并的草稿时返回空串）。只并**一条**回复：
        对方补了几次，最终也只回一次。
        """
        drafts = self._unsent_drafts(chat)
        if not drafts or (chat or "").endswith("@chatroom"):
            return ""                      # 群里别人插话不算"补充"，别把回复弄丢
        newest = max(int(m["id"]) for m in msgs)
        texts = []
        for d in drafts:
            if int(d.get("src_msg_id") or 0) >= newest:
                continue                   # 这条草稿是在新消息之后写的，不用并
            if d.get("status") == "draft":
                self.q.supersede_outbound(
                    d["id"], "对方在发出前又补了话，并入下一轮")
            if (d.get("text") or "").strip():
                texts.append(d["text"].strip())
        if not texts:
            return ""
        log.info("旧草稿并入本轮（前面那条没发出去）[%s]" % chat)
        return ("【并上之前那半句】你刚才已经想好怎么回了：「%s」。"
                "对方在你发出去之前又补了话，把两轮并成**一条**最终回复："
                "只回一次、别分成两条，也别重复已经说过的内容。" % texts[0][:80])

    def _vision_cfg(self) -> dict:
        """图片相关开关：默认把图真给模型看（图里常常带信息）。

        ``config._merge_defaults`` 只做浅合并，老配置里的 ``media`` 段会整体覆盖
        默认值，所以这里再兜一遍默认值，别让缺键把图片功能关掉。
        """
        raw = (self.cfg.get("media") or {}).get("vision")
        out = dict(_VISION_DEFAULTS)
        if isinstance(raw, dict):
            out.update({k: v for k, v in raw.items() if v is not None})
        return out

    def _resolve_media(self, m: dict, kind: str):
        return media.resolve_for_message(
            kind, m.get("content") or "", chat=m.get("chat") or "",
            local_id=m.get("local_id"), shard=m.get("shard"), account=self.acc)

    def _image_for_model(self, m: dict, remaining_bytes: int | None,
                         want_desc: bool = True) -> tuple[str, int, str | None]:
        """一条图片消息 → (data URI, 字节数, 内容描述)。

        ``remaining_bytes`` 是这一轮还剩多少 base64 预算；``None`` 表示不限。
        读不出来（没解密、缺 ffmpeg、坏图）就都返空，调用方退回文字占位。
        """
        content = m.get("content") or ""
        uri, size = "", 0
        try:
            path = self._resolve_media(m, "image")
        except Exception as e:
            log.warning("解图失败：%s" % e)
            path = None
        if path:
            try:
                uri, size = media.to_vision_uri(path)
            except Exception as e:
                log.warning("图片转 data URI 失败：%s" % e)
                uri, size = "", 0
        desc = None
        if want_desc and uri:
            try:
                # 图已经编码好了就顺手传进去，省一次 PIL 重编码
                desc = self.brain.describe_media(
                    m.get("msg_type") or "", content, m.get("type_code"),
                    style="content", image_uri=uri or None)
            except Exception as e:
                log.warning("图片内容描述失败：%s" % e)
        if uri and remaining_bytes is not None and size > remaining_bytes:
            uri, size = "", 0
        return uri, size, desc

    def _vision_used(self, images) -> int:
        return sum(len(im["uri"]) - im["uri"].index(",") - 1 for im in images)

    def _absorb(self, msgs: list[dict], prior_images=()) -> tuple[str, list[tuple[dict, str]], list[dict]]:
        """把一批消息变成 (合并文本, [(消息, 单条文本)], [(图片 uri, 标签)])。

        * **图片**：能解密就随本轮一起真给模型看——聊天里的图常常带着信息
          （课表、菜单、截图），只给一句「在传达什么情绪」等于什么都没说。
          同时让模型写一句内容描述，既当文字占位，也写进记忆供以后翻。
        * **表情包**：仍然只要一句情绪描述，不塞图（省钱，也不容易猜错）。
        * **语音**：转写（可用时）。
        """
        pairs: list[tuple[dict, str]] = []
        images: list[dict] = []
        emoji_done = 0
        img_desc_extra = 0
        vcfg = self._vision_cfg()
        max_imgs = int(vcfg.get("max_images_per_turn", 4) or 0)
        budget = int(vcfg.get("max_bytes", 6 * 1024 * 1024) or 0)
        used = self._vision_used(prior_images)
        describe_images = vcfg.get("content_desc", True)
        for m in msgs:
            k = msgtype.kind(m.get("msg_type"), m.get("type_code"))
            text = ""
            desc = None
            if k == "text":
                text = m.get("content") or ""
            elif k == "voice":
                try:
                    import voice
                    desc = voice.transcribe(m["chat"], m.get("local_id"),
                                           m.get("shard"))
                except Exception as e:
                    log.warning("语音转写失败：%s" % e)
            elif k == "image":
                image_count = len(prior_images) + len(images)
                attach = (vcfg.get("show_images", True) and image_count < max_imgs
                          and used < budget)
                if attach or (describe_images and img_desc_extra < 2):
                    uri, size, desc = self._image_for_model(
                        m, budget - used if attach else 0, want_desc=describe_images)
                    if uri and attach:
                        cap = "图%d" % (image_count + 1)
                        images.append({"uri": uri, "cap": cap})
                        used += size
                        text = ("[对方发来一张图片（就在下面的「%s」里%s）]"
                                % (cap, ("：" + desc) if desc else ""))
                    else:
                        img_desc_extra += 1
            elif msgtype.needs_vision(k):
                if emoji_done < 2:
                    desc = self.brain.describe_media(
                        m.get("msg_type") or "", m.get("content") or "",
                        m.get("type_code"))
                    if desc:
                        emoji_done += 1
            if not text:
                info = msgtype.describe(m.get("msg_type") or "",
                                        m.get("content") or "",
                                        m.get("type_code"), desc=desc)
                text = info["text"]
                if k == "file" and info.get("card_type") == 6 and info.get("title"):
                    self._kb_enqueue(m, info)
            if text:
                pairs.append((m, text))
        return ("\n".join(t for _, t in pairs).strip(), pairs, images)

    def _group_context(self, chat: str, exclude_ids=(), prior_images=()) -> tuple[str, list[dict]]:
        """群里「没人 @ 我」的那几句也带上，让模型知道刚才在聊什么。

        只读入站表：不写记忆、不改状态、不触发回复。图片会在这一轮真给模型
        看（标成「群图N」），否则别人刚发的图在模型眼里只剩一个占位符。
        """
        gc = (self.cfg.get("agent") or {}).get("group_context")
        if not isinstance(gc, dict):
            gc = {}
        limit = int(gc.get("window", 10) or 0)
        if limit <= 0 or not (chat or "").endswith("@chatroom"):
            return "", []
        minutes = int(gc.get("minutes", 30) or 0)
        try:
            rows = self.q.recent_inbound(
                chat, limit=limit, account=self.acc, exclude_ids=exclude_ids,
                within_seconds=int(minutes * 60) if minutes > 0 else 0)
        except Exception as e:                  # 读失败就这一轮不带背景，照常回
            log.warning("群背景读取失败：%s: %s" % (type(e).__name__, e))
            return "", []
        if not rows:
            return "", []

        vcfg = self._vision_cfg()
        max_imgs = int(vcfg.get("max_images_per_turn", 4) or 0) - len(prior_images)
        budget = int(vcfg.get("max_bytes", 6 * 1024 * 1024) or 0)
        max_chars = int(gc.get("chars", 700) or 0)
        used = self._vision_used(prior_images)
        chars = 0
        # 从最新的往回装：装不下就先丢最老的，别把刚发生的那句挤掉
        entries: list[tuple[str, object]] = []
        names: dict[str, str] = {}
        for r in reversed(rows):
            if max_chars > 0 and chars >= max_chars:
                break                       # 背景只当背景，别把它堆成正文
            sender = r.get("sender") or ""
            who = ""
            if sender:
                if sender not in names:
                    try:
                        names[sender] = self.directory.display(chat, sender)
                    except Exception:
                        names[sender] = ""
                who = names[sender]
            k = msgtype.kind(r.get("msg_type"), r.get("type_code"))
            body = ""
            if (k == "image" and vcfg.get("show_images", True)
                    and used < budget
                    and sum(1 for t, _ in entries if t == "image") < max_imgs):
                uri, size, _ = self._image_for_model(
                    r, budget - used,
                    want_desc=False)
                if uri:
                    used += size
                    entries.append(("image", {"uri": uri, "who": who}))
                    chars += 12             # 图片占位行大约这么长
                    continue
            if k != "text":
                try:
                    body = msgtype.describe(r.get("msg_type"),
                                            r.get("content") or "",
                                            r.get("type_code"))["text"]
                except Exception:
                    body = "[%s]" % k
            else:
                body = (r.get("content") or "").strip()
            if not body:
                continue
            line = ("%s：%s" % (who, body)) if who else body
            entries.append(("line", line))
            chars += len(line)
        entries.reverse()

        images: list[dict] = []
        lines: list[str] = []
        for kind_, val in entries:
            if kind_ != "image":
                lines.append(str(val))
                continue
            n = len(images) + 1
            who = (val or {}).get("who") or ""
            images.append({"uri": val["uri"],
                           "cap": "群图%d%s" % (
                               n, ("（%s 刚发的）" % who) if who else "")})
            lines.append("%s[群图%d]" % ((who + "：") if who else "", n))
        if not lines:
            return "", images
        text = ("【群里刚才说了什么（背景，不是让你一条条去回；标了群图N的图片"
                "就在下面）】\n" + "\n".join(lines))
        return text, images

    def _record_user(self, pairs: list[tuple[dict, str]]):
        """逐条写入入站记忆（``in:{id}`` 幂等，重生成/重启不重复）。

        群聊顺便记下"谁说的"，否则模型读历史时分不清哪句是谁讲的。
        """
        if not getattr(self.brain.mem, "persistent", True):
            return
        for m, text in pairs:
            chat = m["chat"]
            sender = ""
            if (chat or "").endswith("@chatroom"):
                try:
                    sender = self.directory.display(chat, m.get("sender") or "")
                except Exception:
                    sender = ""
            try:
                self.brain.mem.add(chat, "user", text,
                                   ts=int(m.get("msg_ts") or 0) or None,
                                   src_key="in:%s" % m["id"], sender=sender)
            except Exception as e:
                log.warning("写入入站记忆失败：%s" % e)

    def _note_relay_replies(self, watch: dict, msgs: list[dict]):
        """把 B 这批消息记进代传话回传：文字存原文，图片/表情存本地路径。

        以前一律折算成一句文字描述，图就变成「[对方发来一张图片]」，封口时
        模型只能回「他发了张图，没正面回你」。这里把图落成路径（不存 base64，
        免得 queue.db 疯长），封口时再一次性喂给模型自己看。
        """
        for _m in msgs:
            kind = msgtype.kind(_m.get("msg_type"), _m.get("type_code"))
            content = _m.get("content") or ""
            ts = int(_m.get("msg_ts") or time.time())
            if not self.q.relay_reply_is_new(watch.get("asked_ts") or 0,
                                             watch.get("reply_after_seq"),
                                             ts, _m.get("sort_seq")):
                continue
            identity = {"message_id": _m.get("id"), "sort_seq": _m.get("sort_seq")}
            if kind in ("image", "emoji"):
                path = ""
                try:
                    path = self._resolve_media(_m, kind) or ""
                except Exception as e:
                    log.warning("回传解图失败 [%s]：%s" % (watch.get("target_chat"), e))
                md5 = media.md5_from_content(content) or ""
                local_id = _m.get("local_id") if kind == "image" else None
                if path or md5 or local_id:
                    self.q.add_relay_media(
                        watch["id"], path=path, md5=md5, ts=ts,
                        label="表情包" if kind == "emoji" else "图片",
                        local_id=local_id, shard=_m.get("shard"), **identity)
                    continue
            text_m = content
            if kind != "text":
                try:
                    text_m = msgtype.describe(_m.get("msg_type"), content,
                                              _m.get("type_code"))["text"]
                except Exception:
                    text_m = "[%s]" % kind
            self.q.add_relay_reply(watch["id"], text_m, ts, **identity)

    def _capture_relay_batch(self, chat: str, msgs: list[dict] | None = None):
        """收集回读确认前就已经入队的快速回复，写入按入站 ID 去重。"""
        try:
            watch = self.q.relay_watch_for(self.acc, chat)
            if watch:
                if msgs is None:
                    msgs = self.q.recent_inbound(chat, limit=30, account=self.acc, within_seconds=120)
                self._note_relay_replies(watch, msgs)
        except Exception as e:
            log.warning("回传补收失败 [%s]：%s" % (chat, e))

    def process(self, chat: str):
        if not self.q.acquire_chat(chat, ttl=300):
            self._release(chat)
            return
        try:
            msgs = self.q.newest_pending(chat, limit=200, account=self.acc, ready_only=True)
            if not msgs:
                return
            self.cfg = self._load_bound_cfg()
            self.brain.reload(self.cfg)
            msgs = self._backfill(chat, msgs)
            if not msgs:
                return

            merge_cfg = self.cfg.get("merge") or {}
            max_regen = int(merge_cfg.get("max_regen", 2))

            # 群聊：一次只回应一个人。否则 A、B 的消息被合并成一批，
            # 模型会对着 A 说话、发送却 @ 了 B（真出过）。
            is_group = (chat or "").endswith("@chatroom")
            names = groupchat.at_names(self.cfg, self.nick) if is_group else []
            explicit_at = False
            at_me = False
            if is_group:
                window = float((self.cfg.get("agent") or {}).get(
                    "group_followup_minutes", 5) or 0)
                last_ts, last_by = self.q.last_at(chat)
                target, explicit_at = groupchat.pick_target(
                    msgs, names, window, last_ts, last_by)
                if target and not explicit_at:
                    log.info("群聊接话窗口内（%s），继续回应 [%s]"
                             % (self.directory.display(chat, target) or target, chat))
                if target:
                    keep = [m for m in msgs
                            if (m.get("sender") or "") == target]
                    if keep and len(keep) != len(msgs):
                        log.info("群聊只处理 %s 的 %d 条，其余 %d 条留到下一轮 [%s]"
                                 % (self.directory.display(chat, target) or target,
                                    len(keep), len(msgs) - len(keep), chat))
                    msgs = keep or msgs
                    at_me = True
                    self.q.mark_at(chat, target)

            # 提醒委托和代传话先过一遍：它们有自己的开关（proactive.reminders
            # + 黑名单），不该被「自动回复关着」一起挡掉——关自动回复只是不聊天，
            # 用户显式委托的提醒还得记下来，否则只开定时提醒的人一条也收不到。
            relayed = []
            captured = []
            prompts = []
            if not is_group and gate_mod.reminder_entry_ok(self.cfg, chat)[0]:
                # 代传话回传：他可能正在回答「你替 A 问的那件事」
                self._capture_relay_batch(chat, msgs)
                # 时区线索可能出现在普通聊天里（「我在英国」「我这边比国内晚八小时」），
                # 不只在提醒委托那一句——先扫一遍记下来，之后所有提醒都按他当地算。
                for _m in msgs:
                    try:
                        reminder_mod.note_tz_hint(
                            self.cfg, self.q, chat, _m.get("content") or "",
                            int(_m.get("msg_ts") or time.time()), self.acc)
                    except Exception as e:
                        log.warning("时区线索扫描失败 [%s]：%s" % (chat, e))
                # 代传话：「你主动提醒一下他该睡觉了」——跨会话的委托，先兑现，
                # 记了动作以后提醒扫描就不会把同一句再当成自己的提醒。
                try:
                    relayed = relay_mod.maybe_relay(self.cfg, self.q, chat, msgs,
                                                    self.acc)
                except Exception as e:
                    log.warning("代传话处理失败 [%s]：%s: %s"
                                % (chat, type(e).__name__, e))
                # 委托扫描也处理不需要生成回复的消息，如「20 秒后提醒我喝水」。
                captured = self._capture_reminders(chat, msgs, prompt_outbounds=prompts)

            allowed, why = gate_mod.identity_ok(self.cfg, chat, at_me=at_me)
            if (prompts or relayed) and not captured and all(it.get("pending") for it in relayed):
                if allowed:
                    _, pairs, _ = self._absorb(msgs)
                    self._record_user(pairs)
                self.q.mark_inbounds([m["id"] for m in msgs], "done")
                return
            if not allowed:
                self.q.mark_inbounds([m["id"] for m in msgs], "skipped")
                log.info("跳过 [%s]: %s" % (chat, why))
                return

            if not is_group:
                proactive_mod.note_ending(self.q, self.acc, chat, msgs[-1].get("content") or "")
                self._maybe_hooks(chat, msgs)
                if self._absorb_into_draft(chat, msgs) or self._absorb_silently(chat, msgs):
                    return

            combined, pairs, images = self._absorb(msgs)
            if not combined:
                self.q.mark_inbounds([m["id"] for m in msgs], "skipped")
                return

            sender_wxid = msgs[-1].get("sender") or ""
            sender_display = self.directory.display(chat, sender_wxid) if is_group else ""

            # 注意：入站记忆必须等生成完再写。先写的话，build_messages 会把这句话
            # 从历史里读出来、又作为"本轮用户消息"追加一次，模型就看到两遍。
            ok, why2 = self.brain.should_reply(chat, combined, at_me=at_me)
            if not ok and why2 != "随机不回":
                self.q.mark_inbounds([m["id"] for m in msgs], "skipped")
                log.info("跳过 [%s]: %s" % (chat, why2))
                return
            if not ok:                              # 随机不回：记下但不答
                self._record_user(pairs)
                self.q.mark_inbounds([m["id"] for m in msgs], "done")
                log.info("随机不回 [%s]" % chat)
                return

            processed = [m["id"] for m in msgs]
            last_seq_id = max(processed)
            regen = 0

            # 群聊：先让模型判断"谁在跟谁说、在回应哪一句"，再组织回复
            analysis = ""
            if is_group and at_me:
                analysis = self.brain.analyze_group(chat, sender_display, combined)
                if analysis:
                    log.info("群聊关系判断 [%s]：%s" % (
                        chat, redact(" / ".join(analysis.splitlines())[:120], self.cfg)))

            # 刚回过一句时，给纯表情/图片一个"通常不用再回"的判断依据
            last_sent = self.q.last_sent_for(chat, account=self.acc)
            avoid_dup = ""
            if last_sent and last_sent.get("sent_ts") and \
                    time.time() - int(last_sent["sent_ts"]) < 180:
                avoid_dup = ("你刚才已经回过「%s」，这次换个说法，别重复开头。"
                             % (last_sent.get("text") or "")[:40])
            media_hint = media_only_hint(msgs, bool(avoid_dup))
            if media_hint:
                log.info("纯表情/图片交给模型判断 [%s]" % chat)

            # 上一条回复还没发出去，对方就补了话：作废旧稿，两轮并成一条回
            merge_hint = self._fold_unsent_drafts(chat, msgs)
            multi_hint = batch_hint(msgs)

            while True:
                extra = "\n\n".join(x for x in (avoid_dup, media_hint, merge_hint,
                                                multi_hint) if x)
                # 群里没点名的最近几句也当背景带上（含图片）
                turn_images = list(images)
                if is_group:
                    window, win_images = self._group_context(chat, processed, prior_images=images)
                    if window:
                        extra = (window + "\n\n" + extra).strip()
                    if win_images:
                        turn_images.extend(win_images)
                # 这一轮刚建了提醒：告诉模型「你已经记下了」，否则它会嘴上否认
                try:
                    acc_hint = reminder_mod.accepted_hint(self.q, captured,
                                                          self.acc, chat)
                    if acc_hint:
                        extra = (extra + "\n\n" + acc_hint).strip()
                    rl_hint = relay_mod.hint_line([it for it in relayed if not it.get("pending")])
                    if rl_hint:
                        extra = (extra + "\n\n" + rl_hint).strip()
                except Exception as e:
                    log.warning("提醒已记下提示注入失败：%s" % e)
                if analysis:
                    extra = (extra + "\n\n【对话关系判断（按这个理解再回）】\n"
                             + analysis).strip()
                refusal = relay_mod.refusal_reply(relayed)
                if refusal:
                    # 回执是程序写的（发没发、谁被排除），发之前换成橙子的口吻；
                    # 事实缺一条就退回原句，见 relay.polish_receipt
                    refusal = relay_mod.polish_receipt(
                        self.cfg, refusal,
                        names=[it.get("name") for it in relayed if it.get("name")])
                    comp = {"ok": True, "reply": refusal}
                else:
                    comp = self.brain.compose(chat, combined,
                                              extra=extra,
                                              sender=sender_wxid,
                                              at_me=at_me,
                                              sender_display=sender_display,
                                              images=turn_images)
                if not comp.get("ok"):
                    log.error("生成失败 [%s]: %s" % (chat, comp.get("error")))
                    self._record_user(pairs)       # 失败了也要记住对方说了什么
                    for m, _ in pairs:
                        n = int(m.get("retry_count") or 0)
                        self.q.mark_retry(m["id"], delay=min(60, 5 * (2 ** n)))
                    break

                # 模型用 [[SKIP]] 表示"这条我不回"
                if comp.get("skip"):
                    if at_me:
                        comp = self.brain.compose(
                            chat, combined, extra=extra, sender=sender_wxid,
                            at_me=at_me, sender_display=sender_display,
                            force_reply=True, images=turn_images)
                        if (not comp.get("ok")) or comp.get("skip"):
                            reply = random.choice(self.FORCED_REPLIES)
                            log.warning("被@但模型仍想跳过，用兜底回复 [%s]" % chat)
                            comp = {"ok": True, "reply": reply}
                    else:
                        self._record_user(pairs)
                        self.q.mark_inbounds(processed, "done")
                        log.info("模型判断不回 [%s]：%s" % (
                            chat, redact(combined[:40])))
                        return

                newer = [m for m in self.q.newest_pending(chat, limit=200, account=self.acc,
                                                          ready_only=True)
                         if m["id"] > last_seq_id]
                # 群里只把同一个人的补充并进来，别人的话留给各自那一轮
                if is_group and at_me:
                    newer = [m for m in newer
                             if (m.get("sender") or "") == sender_wxid]
                if merge_cfg.get("invalidate_on_new", True) and newer and regen < max_regen:
                    regen += 1
                    # 合并期间新到的「帮我提醒小王」也得走代传话，否则会被
                    # 当成发起人自己的普通提醒，目标那边什么都收不到。
                    # 顺序和主流程一致：先代传话（它会把消息记成已处理），
                    # 再扫普通提醒。
                    if not is_group and gate_mod.reminder_entry_ok(self.cfg, chat)[0]:
                        self._capture_relay_batch(chat, newer)
                        try:
                            relayed += relay_mod.maybe_relay(self.cfg, self.q, chat,
                                                             newer, self.acc)
                        except Exception as e:
                            log.warning("补充消息代传话失败 [%s]：%s: %s"
                                        % (chat, type(e).__name__, e))
                        captured += self._capture_reminders(chat, newer, prompt_outbounds=prompts)
                    add_text, add_pairs, add_images = self._absorb(newer, prior_images=images)
                    if (prompts or relayed) and not captured and all(it.get("pending") for it in relayed):
                        self._record_user(pairs + add_pairs)
                        self.q.mark_inbounds(processed + [m["id"] for m in newer], "done")
                        return
                    if add_text:
                        combined = (combined + "\n" + add_text).strip()
                        pairs.extend(add_pairs)
                        if add_images:
                            images.extend(add_images)
                        processed.extend(m["id"] for m in newer)
                        if is_group:
                            at_me = at_me or any(
                                groupchat.mentions_me(t, names) for _, t in add_pairs)
                        # 旧的关系判断是按「补发之前那条」做的：对方这一补可能带着更正
                        # （先说「没那么爱对人了」再补「怼*」），拿旧判断回会接着说错。
                        # 按合并后的整轮重做一次；做不出来就丢掉，别让旧结论误导。
                        if is_group and at_me:
                            analysis = self.brain.analyze_group(
                                chat, sender_display, combined) or ""
                            if analysis:
                                log.info("补充消息后重做关系判断 [%s]" % chat)
                    else:
                        self.q.mark_inbounds([m["id"] for m in newer], "skipped")
                    last_seq_id = max(m["id"] for m in newer)
                    log.info("生成期间有新消息，重生成 (%d/%d) [%s]"
                             % (regen, max_regen, chat))
                    continue

                reply = (comp.get("reply") or "").strip()
                label = self.brain.sensitive_label(reply)
                send_cfg = self.cfg.get("send") or {}
                segs = ([reply] if not send_cfg.get("bubble", True)
                        else split_reply(reply,
                                         max_chars=int(send_cfg.get("bubble_max_chars", 40)),
                                         max_bubbles=int(send_cfg.get("max_bubbles", 3))))
                if not segs:
                    self.q.mark_inbounds(processed, "done")
                    break

                # 群里被 @ 时，回复带上对方名字，别让全群分不清在回谁
                if (is_group and at_me and sender_display
                        and (self.cfg.get("agent") or {}).get("group_at_reply", True)):
                    segs = groupchat.prepend_at(segs, sender_display)

                status, gate_note = "draft", None
                if label:
                    status, gate_note = "hold", "sensitive:%s" % label
                elif comp.get("truncated"):
                    status, gate_note = "hold", "truncated"
                elif self.brain.contact_cfg(chat).get("draft_only"):
                    status, gate_note = "hold", "draft_only"

                # 生成完成后才写入站记忆：既保证不重复进上下文，也保证失败/跳过时不丢
                self._record_user(pairs)

                with self.q.tx() as conn:
                    tid = self.q.create_outbound(self.acc, chat, reply,
                                                 src_msg_id=last_seq_id,
                                                 status=status, gate=gate_note,
                                                 segments=segs, at_me=at_me,
                                                 covered=processed, conn=conn)
                    self.q.mark_inbounds(processed, "done", conn=conn)
                log.info("生成 #%d [%s] 合并%d条%s -> %s%s" % (
                    tid, chat, len(msgs),
                    ("+" + str(len(processed) - len(msgs)) if len(processed) > len(msgs) else ""),
                    redact(reply[:60]),
                    ("（%s，待人工）" % gate_note) if gate_note else ""))
                break
        finally:
            try:
                self.q.release_chat(chat)
            finally:
                self._release(chat)

    def _batch_image_refs(self, messages: list[dict], limit: int = 3) -> list[dict]:
        """这一批里的图片 → ``[{"p":路径,"m":md5,"l":"图片"}]``（只解路径，不调模型）。

        提醒判定要用：课表、日程、海报里的时间只有图上才有。
        """
        refs: list[dict] = []
        for m in messages or []:
            if len(refs) >= limit:
                break
            if msgtype.kind(m.get("msg_type"), m.get("type_code")) != "image":
                continue
            content = m.get("content") or ""
            path = ""
            try:
                path = self._resolve_media(m, "image") or ""
            except Exception as e:
                log.warning("提醒解图失败：%s" % e)
            md5 = media.md5_from_content(content) or ""
            if path or md5 or m.get("local_id"):
                refs.append({"p": path, "m": md5, "l": "图片", "chat": m.get("chat"),
                             "local_id": m.get("local_id"), "shard": m.get("shard")})
        return refs

    def _capture_reminders(self, chat: str, messages: list[dict], *, prompt_outbounds=None) -> list:
        """扫一轮委托，返回新建的提醒 id（给回复侧说一句「我记下了」用）。

        图片一起送进去：粗筛命中之后，「以后每次化学前沿课前十分钟」这种话
        只有看图才知道那门课是周几几点。
        """
        try:
            return reminder_mod.maybe_capture(
                self.cfg, self.q, chat, messages, self.acc,
                images=self._batch_image_refs(messages), prompt_outbounds=prompt_outbounds) or []
        except Exception as e:
            log.warning("提醒扫描失败 [%s]：%s: %s" % (chat, type(e).__name__, e))
            return []

    def run_processor(self):
        last_expire = 0.0
        while not _stop.is_set():
            try:
                self.cfg = self._load_bound_cfg()
                merge_cfg = self.cfg.get("merge") or {}
                max_wait = float(merge_cfg.get("max_wait_seconds", 8.0))
                workers = max(1, int((self.cfg.get("bot") or {}).get("max_parallel_chats", 2)))
                now = time.time()
                if now - last_expire > 60:
                    last_expire = now
                    n = self.q.expire_inbound(int(merge_cfg.get("max_age_seconds", 21600)),
                                              account=self.acc)
                    if n:
                        log.warning("过期清理：%d 条太旧的待处理消息不再回复" % n)

                groups = self.q.pending_by_chat(limit=200, account=self.acc, skip_paused=True)
                for chat, msgs in groups.items():
                    if len(self._inflight) >= workers:
                        break
                    if self.q.is_paused(chat) or not self._acquire(chat):
                        continue
                    newest = msgs[-1]
                    # 静默从"我们看到它的那一刻"算起：采集有 1~3 秒延迟，
                    # 只按对方发送时间算的话，窗口会被这段延迟吃掉，
                    # 于是表情包/后半句赶到时第一轮已经开写了。
                    seen = max(float(newest.get("msg_ts") or 0),
                               float(newest.get("created_ts") or 0))
                    oldest = max(float(msgs[0].get("msg_ts") or 0),
                                 float(msgs[0].get("created_ts") or 0))
                    quiet = quiet_seconds_for(msgs, merge_cfg)
                    if now - seen < quiet and now - oldest < max_wait:
                        self._release(chat)             # 还在静默窗口内
                        continue
                    threading.Thread(target=self.process, args=(chat,),
                                     daemon=True).start()
            except Exception as e:
                log.error("处理循环异常：%s: %s" % (type(e).__name__, e))
            time.sleep(0.5)

    # ================================================================ 发送
    def _gate(self, chat: str, created_ts=None, at_me: bool = False,
              kind: str = "reply", approved: bool = False) -> dict:
        return gate_mod.evaluate(self.cfg, chat, paused=self.q.is_paused(chat),
                                 created_ts=created_ts, at_me=at_me, kind=kind,
                                 approved=approved)

    def _maybe_hooks(self, chat: str, msgs: list):
        """抽钩子：同会话 5 分钟只跑一次，后台线程，不影响回复速度。"""
        if not proactive_mod.hooks_allowed(self.q, self.cfg, chat):
            return
        now = time.time()
        if now - self._HOOK_LAST.get(chat, 0) < 300:
            return
        self._HOOK_LAST[chat] = now
        texts = [(m.get("content") or "") for m in msgs[-6:]
                 if (m.get("content") or "").strip()
                 and msgtype.kind(m.get("msg_type"), m.get("type_code")) == "text"]
        if not texts:
            return
        threading.Thread(
            target=self._extract_hooks,
            args=(chat, texts),
            daemon=True).start()

    def _extract_hooks(self, chat: str, texts: list[str]):
        try:
            proactive_mod.extract_hooks(self.brain, self.q, self.acc, chat, texts,
                                        self._load_bound_cfg())
        except Exception as e:
            log.warning("话题提取已暂停 [%s]：%s" % (chat, e))

    def _verify_sent(self, chat: str, text: str, t0: float,
                     after_seq: int | None = None) -> int | None:
        """回读微信库确认自己真的发出了（比"输入框已清空"可靠）。"""
        try:
            for m in self.db.get_messages(chat, limit=30):
                if (m.get("sender_username") or "") != self.me:
                    continue
                content = m.get("content") or ""
                if chat.endswith("@chatroom"):
                    content = groupchat.strip_sender_prefix(content)
                    content = " ".join(content.split())
                    expected = " ".join((text or "").split())
                else:
                    expected = (text or "").strip()
                if content.strip() != expected:
                    continue
                seq = int(m.get("sort_seq") or 0)
                if not seq or (after_seq is not None and seq <= after_seq):
                    continue
                if int(m.get("create_time") or 0) >= int(t0):
                    return seq
        except Exception as e:
            log.warning("回读核验失败：%s" % e)
        return None

    def _presend_supplement(self, task: dict, chat: str) -> str:
        """首段提交前的复检：这条草稿还没发出去，对方有没有又补话。

        返回 ``""``（照常发）／``"absorbed"``（补充只是表情附和，已算进这条）／
        ``"superseded"``（有新话，草稿作废，等处理线程并起来重写一条）。
        """
        covered = set()
        raw = task.get("covered")
        if raw:
            try:
                covered = {int(x) for x in json.loads(raw)}
            except Exception:
                covered = set()
        if (chat or "").endswith("@chatroom"):
            return ""                      # 群里别人的话不是"补充"，别把 @ 的回复作废
        after_id = int(task.get("src_msg_id") or 0)
        try:
            newer = [m for m in self.q.newest_pending(chat, limit=50, account=self.acc)
                     if int(m["id"]) > after_id and int(m["id"]) not in covered]
        except Exception as e:
            log.warning("发送前复查失败：%s" % e)
            return ""
        if not newer:
            return ""
        if absorbable_supplement(newer):
            if gate_mod.reminder_entry_ok(self.cfg, chat)[0]:
                self._capture_relay_batch(chat, newer)
            self._note_absorbed(newer)
            self.q.mark_inbounds([m["id"] for m in newer], "done")
            log.info("发送前把 %d 条补充并进这条回复（只是语气）#%d [%s]"
                     % (len(newer), task["id"], chat))
            return "absorbed"
        if self.q.supersede_outbound(task["id"], "对方又补充了内容，这条并入下一轮",
                                     allow_sending=True):
            log.info("发送前收到补充，草稿作废等合并 #%d [%s]" % (task["id"], chat))
            return "superseded"
        return ""

    def run_sender(self):
        while not _stop.is_set():
            task = None
            try:
                self._check_bound_cfg(load_cfg())
                task = self.q.next_outbound(account=self.acc)
                if not task:
                    time.sleep(1.0)
                    continue
                self.send_task(task)
            except Exception as e:
                if task:
                    try:
                        row = self.q.get_outbound(task["id"])
                        if row and row["status"] == "sending":
                            if row.get("attempt_started_ts") is not None:
                                self.q.mark_uncertain(task["id"], str(e))
                            else:
                                self._retry_unsubmitted(row, str(e), 0)
                    except Exception:
                        pass
                log.error("发送循环异常：%s: %s" % (type(e).__name__, e))
                time.sleep(1.0)

    def _send_block(self, task: dict) -> tuple[str, str] | None:
        if _stop.is_set():
            return "draft", "进程正在停止，任务留待重启"
        self._check_bound_cfg(self.cfg)
        kind = task.get("kind") or "reply"
        if kind == "reminder":
            reason = reminder_mod.send_block_reason(self.q, self.cfg, task)
        elif kind == "reminder_clarify":
            reason = reminder_mod.clarify_send_block_reason(self.q, task)
        elif kind == "relay_clarify":
            reason = relay_mod.clarify_send_block_reason(self.q, task)
        elif kind == "proactive":
            reason = proactive_mod.send_block_reason(self.q, self.cfg, task)
        else:
            reason = None
        if reason:
            return ("expired" if "过期" in reason else "cancelled"), reason
        verdict = self._gate(task["chat"], created_ts=gate_mod.draft_timestamp(task),
                             at_me=bool(task.get("at_me")), kind=kind,
                             approved=bool(task.get("approved_ts")))
        if verdict["action"] == "ok":
            return None
        status = "expired" if "过期" in verdict["reason"] else verdict["action"]
        return ("cancelled" if status == "cancel" else status), verdict["reason"]

    def _apply_send_block(self, task: dict, block: tuple[str, str]) -> dict:
        status, reason = block
        if status == "draft":
            self.q.requeue_outbound(task["id"], note=reason)
        elif status == "expired" and task.get("kind") == "reminder":
            self.q.mark_missed(task["id"], reason)
        else:
            self.q.finish_outbound(task["id"], status, reason, bump_attempt=False,
                                   expected="sending")
        log.info("出站 #%d [%s] %s：%s" % (task["id"], task["chat"], status, reason))
        return {"status": status, "reason": reason}

    # ------------------------------------------------------------ 未提交的软重试
    # 「未提交」= 微信那边一个字符都没发出去，重试绝不会重复。以前的出路是
    # 「重试 3 次 → failed」，可一次身份误判就进失败列表；现在：
    # * 等环境恢复的（托盘/全屏）不计次数、一直等，用户一打开就能发；
    # * 其余未提交失败按 attempts 计数，到 OUTBOUND_MAX_ATTEMPTS 转 hold
    #   （待人工，草稿保留、可批准），不再落 failed。
    # 只有「重试也不会好」的才直接转人工。
    NOT_COUNTED = ("main_in_tray", "fullscreen")
    PERMANENT = ("bad_args", "account_mismatch", "identity_mismatch", "input_busy")

    def _retry_unsubmitted(self, task: dict, reason: str, index: int,
                           code: str = "") -> dict:
        used = int(task.get("attempts") or 0)
        send_cfg = self.cfg.get("send") or {}
        if code in self.PERMANENT:
            note = "发送未提交（%s），重试无用，已转人工待审" % (code or reason)
            if not self.q.finish_outbound(task["id"], "hold", note, bump_attempt=False,
                                          retryable=0, failure_code=code or None,
                                          expected="sending"):
                log.info("任务已被撤回或结案，不再转待审核 #%d" % task["id"])
                return {"status": "blocked", "reason": "任务已撤回或结案", "segment": index}
            log.warning("发送未提交 #%d [%s]：%s" % (task["id"], task["chat"], note))
            return {"status": "hold", "reason": note, "segment": index}
        counted = code not in self.NOT_COUNTED
        if counted and used >= OUTBOUND_MAX_ATTEMPTS:
            note = ("连续 %d 次未提交（最后原因：%s），已转人工待审，草稿保留"
                    % (used, reason))
            if not self.q.finish_outbound(task["id"], "hold", note, bump_attempt=False,
                                          retryable=0, failure_code=code or None,
                                          expected="sending"):
                log.info("任务已被撤回或结案，不再转待审核 #%d" % task["id"])
                return {"status": "blocked", "reason": "任务已撤回或结案", "segment": index}
            log.warning("发送未提交 #%d [%s]：%s" % (task["id"], task["chat"], note))
            return {"status": "hold", "reason": note, "segment": index}
        base = float(send_cfg.get("retry_delay", 45) or 45)
        ceiling = float(send_cfg.get("retry_max_delay", 600) or 0)
        delay = base * (2 ** min(used, 6))
        if ceiling > 0:
            delay = min(ceiling, delay)
        self.q.requeue_outbound(task["id"], delay=delay, note=reason,
                                count_attempt=counted)
        log.info("发送未提交 #%d [%s]，%.0f 秒后重试（第 %d 次）：%s"
                 % (task["id"], task["chat"], delay, used + (1 if counted else 0), reason))
        return {"status": "draft", "reason": reason, "segment": index,
                "retry_in": delay}

    def send_task(self, task: dict, sleep_delay: bool = True,
                  reload_cfg: bool = True) -> dict:
        """逐段确认实际发送，每一段提交前都重新校验当前状态。"""
        if task.get("account") != self.acc:
            return {"status": "blocked", "reason": "出站任务与绑定的小号不一致"}
        current = self.q.get_outbound(task["id"])
        if not current or current["account"] != self.acc or current["status"] != "sending":
            return {"status": "blocked", "reason": "任务已经撤回或不在发送中"}
        task = current
        if reload_cfg:
            self.cfg = self._load_bound_cfg()
            self.brain.reload(self.cfg)
        block = self._send_block(task)
        if block:
            return self._apply_send_block(task, block)
        if (task.get("kind") or "") == "relay_clarify":
            # 过了门禁再润色：代传关着的时候，不该为一句根本发不出去的话
            # 花一次模型调用、还占着发送线程。
            try:
                task = relay_mod.polish_clarify_task(self.q, self.cfg, task)
            except Exception as e:
                log.warning("追问措辞改写失败 #%s：%s: %s"
                            % (task.get("id"), type(e).__name__, e))
            else:
                if task.get("text") != current.get("text"):
                    # 改写同时改了正文和 ask，改写后按新正文再核一次门禁
                    block = self._send_block(task)
                    if block:
                        return self._apply_send_block(task, block)
        if DRY:
            self.q.finish_outbound(task["id"], "cancelled", "dry-run", bump_attempt=False)
            return {"status": "dry-run"}
        chat = task["chat"]
        kind = task.get("kind") or "reply"
        if sleep_delay and kind not in ("reminder", "reminder_clarify", "relay_clarify"):
            _stop.wait(self.brain.human_delay(chat))

        # 到点提醒的措辞必须由模型现写：准点由调度线程保证，这里多一点延迟
        # 换一句像人说的话。模型没写出来就退回待发重试，**绝不拿系统模板
        # 顶替**——模板只当占位，过期未发出会明确记成「错过」。
        if kind == "reminder":
            try:
                task = reminder_mod.polish_task(self.q, self.cfg, self.brain, task)
            except Exception as e:
                log.warning("提醒措辞润色失败 #%s：%s: %s"
                            % (task.get("id"), type(e).__name__, e))
            source = (task.get("text_source") or "")
            if source not in ("model", "manual", "legacy"):
                used = int(task.get("attempts") or 0)
                limit = int((self.cfg.get("send") or {}).get(
                    "reminder_text_attempts", 4) or 4)
                if used >= limit:
                    self.q.mark_missed(task["id"], "模型一直没写出提醒正文，判定错过")
                    log.warning("提醒正文生成失败，判定错过 #%d [%s]"
                                % (task["id"], chat))
                    return {"status": "missed", "reason": "提醒正文生成失败"}
                self.q.requeue_outbound(task["id"], delay=15,
                                        note="等模型写提醒正文", count_attempt=True)
                log.info("提醒正文未就绪，15 秒后重试 #%d [%s]" % (task["id"], chat))
                return {"status": "waiting_model", "reason": "提醒正文还没写好"}

        segs = segments_of(task)
        start = int(task.get("segments_sent") or 0)
        if kind == "reply" and start == 0 and self._presend_supplement(task, chat) == "superseded":
            return {"status": "superseded"}
        if not any(segs):
            return self._apply_send_block(task, ("cancelled", "草稿内容为空"))

        sent_seq = None
        for index in range(start, len(segs)):
            if reload_cfg:
                self.cfg = self._load_bound_cfg()
            block = self._send_block(task)
            if block:
                return self._apply_send_block(task, block)
            seg = segs[index]
            if not seg:
                self.q.bump_segments(task["id"], index + 1, complete=index + 1 == len(segs),
                                     sent_after_seq=sent_seq, expected="sending")
                continue

            started = int(time.time())
            rows = self.db.get_messages(chat, limit=1)
            after_seq = int(rows[0]["sort_seq"]) if rows else 0
            if not self.q.begin_outbound_attempt(task["id"], index, after_seq, started):
                return {"status": "blocked", "reason": "任务已撤回或已有未核验的发送"}
            intent = self.q.record_robot_intent(chat, seg, outbound_id=task["id"], ts=started)
            send_cfg = self.cfg.get("send") or {}
            failure = "回读尚未确认"
            try:
                response = post("/send", {
                    "account": self.acc, "chat": chat, "text": seg,
                    "source": "bot" if kind == "reply" else kind,
                    "outbound_id": task["id"], "after_seq": after_seq,
                })
            except (OSError, ValueError) as e:
                response = {}
                failure = "%s: %s" % (type(e).__name__, e)
            if not isinstance(response, dict):
                response = {}
            reason = str(response.get("error") or response.get("reason") or failure)
            if response.get("submitted") is False:
                self.q.drop_robot_intent(intent)
                self.q.clear_outbound_attempt(task["id"])
                status = response.get("outbound_status")
                if status in ("cancelled", "expired", "hold"):
                    return self._apply_send_block(task, (status, reason))
                # 没提交 = 一个字符都没发出去，重试绝对安全。不再按 retryable
                # 白名单决定「失败还是重试」，统一走退避软重试，到上限转 hold。
                return self._retry_unsubmitted(task, reason, index,
                                               str(response.get("reason") or ""))
            if not (response.get("ok") and response.get("verified")):
                self.q.mark_uncertain(task["id"], reason)
                log.warning("发送待核实 #%d 第 %d 段：%s" % (task["id"], index + 1, reason))
                return {"status": "uncertain", "reason": reason, "segment": index}

            self.q.confirm_robot_sent(intent, ts=started)
            self._remember_sent_segment(task, index, seg)
            complete = index + 1 == len(segs)
            sent_seq = response.get("verified_seq")
            if not self.q.bump_segments(task["id"], index + 1, complete=complete,
                                        sent_after_seq=sent_seq, expected="sending"):
                return {"status": "blocked", "reason": "该任务已结案"}
            if complete and kind == "reminder":
                self._capture_relay_batch(chat)
            log.info("回读已确认 #%d 第 %d/%d 段 [%s]" %
                     (task["id"], index + 1, len(segs), chat))
            if index + 1 < len(segs) and sleep_delay:
                lo = float(send_cfg.get("min_bubble_gap", 0.6))
                hi = float(send_cfg.get("max_bubble_gap", 1.5))
                _stop.wait(random.uniform(lo, max(lo, hi)))

        if start >= len(segs):
            self.q.finish_outbound(task["id"], "sent", verified=1, retryable=0,
                                   expected="sending")
        allowed, _ = gate_mod.identity_ok(self.cfg, chat, at_me=bool(task.get("at_me")))
        if allowed:
            try:
                self.brain.maybe_summarize(chat)
                self.brain.maybe_adapt(chat)
            except Exception as e:
                log.warning("记忆整理失败：%s" % e)
        return {"status": "sent", "segments": len(segs)}

    def _remember_sent_segment(self, task: dict, index: int, text: str):
        try:
            self.brain.mem.add(task["chat"], "assistant", text,
                               src_key="out:%d:%d" % (task["id"], index))
        except Exception as e:
            log.warning("写入回复记忆失败：%s" % e)

    # ================================================================ 对账
    def _reconcile_task(self, task: dict, now: float, timeout: float):
        task = self.q.get_outbound(task["id"])
        if not task or task["status"] != "uncertain" or task["account"] != self.acc:
            return
        segs = segments_of(task)
        index = int(task.get("segments_sent") or 0)
        if segs and index == len(segs):
            self.q.settle_uncertain(task["id"], status="sent", note="各段均已核实")
            return
        if not task.get("attempt_dispatched") and index < len(segs):
            delay = int((self.cfg.get("send") or {}).get("retry_delay", 45))
            self.q.settle_uncertain(task["id"], status="draft", delay=delay,
                                    note="服务尚未取走该段，可安全重试")
            return
        started = task.get("attempt_started_ts")
        after_seq = task.get("attempt_after_seq")
        if started is None or after_seq is None or index >= len(segs):
            self.q.settle_uncertain(task["id"], status="unverified",
                                    note="缺少该段的提交凭据，请人工核对")
            return
        sent_seq = self._verify_sent(task["chat"], segs[index], started, after_seq)
        if sent_seq:
            status = "sent" if index + 1 == len(segs) else "draft"
            note = "回读确认已发出" if status == "sent" else "已核实当前段，待发送其余段"
            if self.q.settle_uncertain(task["id"], status=status, note=note,
                                       segments_sent=index + 1, sent_after_seq=sent_seq):
                self._remember_sent_segment(task, index, segs[index])
                if status == "sent" and task.get("kind") == "reminder":
                    self._capture_relay_batch(task["chat"])
                log.info("对账 #%d：第 %d 段已核实" % (task["id"], index + 1))
        elif now - started > max(600.0, timeout * 4):
            if self.q.settle_uncertain(task["id"], status="unverified",
                                        note="回读未找到，请核对该段，禁止自动补发"):
                log.warning("对账 #%d：第 %d 段未核实" % (task["id"], index + 1))

    def run_reconciler(self):
        last_recover = 0.0
        while not _stop.is_set():
            try:
                self.cfg = self._load_bound_cfg()
                now = time.time()
                if now - last_recover > 60:
                    last_recover = now
                    # HTTP 调用最长 90 秒，不能提前把有效的发送回收到对账队列。
                    n = self.q.recover_inflight(age=180, account=self.acc)
                    if n:
                        log.warning("回收中断发送任务 %d 条" % n)
                timeout = float((self.cfg.get("send") or {}).get("verify_seconds", 8))
                for task in self.q.list_outbound("uncertain", 50, account=self.acc):
                    self._reconcile_task(task, now, timeout)
            except Exception as e:
                log.error("对账异常：%s: %s" % (type(e).__name__, e))
            _stop.wait(5)

    # ================================================================ 摘要补偿
    def run_summarizer(self):
        while not _stop.is_set():
            if _stop.wait(60):
                break
            try:
                self.cfg = self._load_bound_cfg()
                mc = self.cfg.get("memory") or {}
                if not mc.get("enabled", True) or not getattr(self.brain.mem, "persistent", True):
                    continue
                threshold = int(mc.get("summary_threshold", 30))
                for row in self.brain.mem.chats_needing_summary(threshold, limit=5):
                    if not gate_mod.identity_ok(self.cfg, row["chat"], at_me=True)[0]:
                        continue
                    r = self.brain.maybe_summarize(row["chat"])
                    if r.get("ok"):
                        log.info("补偿摘要 [%s]：%d 条" % (row["chat"], r.get("summarized", 0)))
                every = int(mc.get("adapt_every", 30))
                for row in self.brain.mem.chats_needing_adapt(every, limit=5):
                    if not gate_mod.identity_ok(self.cfg, row["chat"], at_me=True)[0]:
                        continue
                    r = self.brain.maybe_adapt(row["chat"])
                    if r.get("ok"):
                        log.info("补偿观察 [%s]" % row["chat"])
            except Exception as e:
                log.error("补偿摘要异常：%s: %s" % (type(e).__name__, e))

    def run_kb_inbox(self):
        """按当前知识库开关扫描，切换开关无需重启。"""
        try:
            import kb
        except Exception as e:
            log.warning("知识库模块加载失败：%s: %s" % (type(e).__name__, e))
            return
        tick = 0
        while not _stop.is_set():
            try:
                cfg = self._load_bound_cfg()
                if not (cfg.get("kb") or {}).get("enabled", False):
                    tick = 0
                    _stop.wait(3)
                    continue
                tick += 1
                if tick % 3 == 1:                      # 每 ~90 秒扫一次 inbox
                    r = kb.scan_inbox()
                    if r.get("ingested"):
                        log.info("kb inbox 新增 %d 份资料" % r["ingested"])
                    for f in (r.get("failed") or [])[:3]:
                        log.warning("kb 入库失败：%s" % f)
                jr = kb.process_jobs(limit=2)          # 每 ~30 秒处理收件任务
                if jr.get("done"):
                    log.info("kb 微信文件入库 %d 份" % jr["done"])
            except Exception as e:
                log.warning("kb 后台任务异常：%s: %s" % (type(e).__name__, e))
            _stop.wait(30)

    # ================================================================ 主循环
    def run(self):
        for fn in (self.run_collector, self.run_processor, self.run_sender,
                   self.run_reconciler, self.run_summarizer, self.run_kb_inbox):
            threading.Thread(target=fn, daemon=True).start()
        # 主动调度（提醒必须准点，所以单独一个 1 秒 tick 的线程）
        threading.Thread(target=initiative.run,
                         args=(self.q, self._load_bound_cfg, self.brain),
                         daemon=True).start()
        ready_file = os.environ.get("WXBOT_READY_FILE")
        if ready_file:
            with open(ready_file, "w", encoding="utf-8") as ready:
                ready.write(str(os.getpid()))
        try:
            while not _stop.wait(30):
                s = self.q.stats()
                scan = self._scan_tick()
                self._scan_stats = {"rounds": 0, "sessions": 0, "enqueued": 0}
                log.info("状态 待处理=%d 重试=%d 草稿=%d 待审=%d 发送中=%d "
                         "已发=%d 失败=%d 不确定=%d 未核实=%d ｜ 采集 %d轮 会话%d 新增%d" % (
                             s["inbound_pending"], s["inbound_retry"],
                             s["outbound_draft"], s["outbound_hold"],
                             s["outbound_sending"], s["outbound_sent"],
                             s["outbound_failed"], s["outbound_uncertain"],
                             s.get("outbound_unverified", 0),
                             scan["rounds"], scan["sessions"], scan["enqueued"]))
        except KeyboardInterrupt:
            _stop.set()
        finally:
            initiative.stop()


if __name__ == "__main__":
    lock = runtime_lock.acquire(ROOT, "worker", gui_child=os.environ.get("WXBOT_GUI_SESSION") == "1")
    if lock is None:
        log.error("已有 Orange 后台在运行，请先关闭对应的旧链路或 GUI 会话。")
        sys.exit(2)
    with lock:
        try:
            Worker().run()
        except KeyboardInterrupt:
            _stop.set()
