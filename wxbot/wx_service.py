# -*- coding: utf-8 -*-
"""微信小号后台代理 —— HTTP 服务。

启动：
    python wxbot\\wx_service.py [--account wxid_xxx] [--port 8765]

接口：
    GET  /status                     服务与绑定状态
    GET  /stats                      队列进度、待审草稿、暂停会话、最近错误
    GET  /config                     配置（密钥脱敏）
    POST /config                     改配置（白名单字段）
    GET  /chats                      会话列表
    GET  /messages?chat=&limit=      会话消息
    GET  /inbox?since=               最近收到的消息（读 inbound 表）
    GET  /drafts                     待人工处理的回复（hold / failed / uncertain）
    POST /drafts {id, action}         approve / reject / mark_sent
    POST /resume {chat}              解除人工接管
    GET  /usage                      LLM 用量与联网预算
    GET  /log?lines=80               reply.log 尾部
    GET  /persona                    固定人设 + 关系表
    POST /persona {action, ...}      保存固定人设 / 整张关系表
    POST /send {chat, text}          手动发送（会回读核验 + 记记忆）
    POST /open {chat}                预热开窗
    POST /quit                       退出

所有发送都不抢焦点、不移动鼠标；开窗那一步会短暂激活微信主窗口。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, os.path.join(ROOT, "agent"))
sys.path.insert(0, os.path.join(ROOT, "memory"))

from logutil import get_logger, redact, setup_stdio       # noqa: E402

setup_stdio()
log = get_logger()

import gate                                               # noqa: E402
import proactive as proactive_rules                       # noqa: E402

PYLIB = os.environ.get("WXBOT_PYLIB") or os.path.join(ROOT, "pylibs")
if os.path.isdir(PYLIB) and PYLIB not in sys.path:
    sys.path.insert(0, PYLIB)

import config as C                                        # noqa: E402
import runtime_lock                                       # noqa: E402
from wx_identity import open_bound_db                    # noqa: E402

from wx_engine import WxWindowManager                     # noqa: E402

try:
    from wxqueue import Queue as WxQueue
except Exception as e:                                    # pragma: no cover
    WxQueue = None
    log.warning("队列模块不可用：%s" % e)

try:
    from store import MemoryStore
except Exception:                                         # pragma: no cover
    MemoryStore = None

DEFAULT_PORT = 8765
LOG_PATH = os.environ.get("WXBOT_LOG") or os.path.join(ROOT, "reply.log")
CLARIFY_KINDS = ("reminder_clarify", "relay_clarify")

# 允许通过 /config 修改的字段（白名单，避免被覆盖成半个配置）
PATCHABLE = {
    ("agent", "auto_reply"), ("agent", "reply_whitelist"),
    ("agent", "reply_blacklist"), ("agent", "group_reply"),
    ("agent", "min_delay"), ("agent", "max_delay"),
    ("agent", "name"), ("agent", "persona"), ("agent", "style"),
    ("memory", "enabled"), ("memory", "max_turns"),
    ("memory", "summary_threshold"), ("web", "enabled"),
    ("send", "bubble"), ("send", "bubble_max_chars"),
    ("send", "max_bubbles"), ("send", "max_draft_age"),
    ("takeover", "pause_minutes"), ("bot", "max_parallel_chats"),
    ("logging", "chat_content"), ("kb", "enabled"),
}

# 嵌套字段用点路径单独处理（PATCHABLE 的 (section, key) 二元组表达不了三段的）
NESTED_PATCHABLE = {
    "proactive.enabled",
    "proactive.whitelist",
    "proactive.active_hours",
    "proactive.per_chat_daily",
    "proactive.idle_min_minutes",
    "proactive.idle_max_minutes",
    "proactive.reminders.enabled",
    "proactive.reminders.grace_seconds",
}


def _set_path(cfg: dict, path: str, value):
    node = cfg
    parts = path.split(".")
    for p in parts[:-1]:
        nxt = node.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            node[p] = nxt
        node = nxt
    node[parts[-1]] = value


def _clarify_send_block_reason(queue, task: dict) -> str | None:
    if task["kind"] == "relay_clarify":
        from relay import clarify_send_block_reason
    else:
        from reminder import clarify_send_block_reason
    return clarify_send_block_reason(queue, task)


def local_weekday_rule(inst, account: str, chat: str, rule: str, ts: int) -> str:
    """控制台手工建单：把 ``weekly:N`` 的 N 换成**联系人当地**的星期。

    ``ts`` 是主人本机时间；调度器（``reminder_parse.advance``）按联系人时差
    推进，两边必须用同一个星期口径，否则「每周一」会在第二次触发时漂一天。
    """
    if not str(rule or "").startswith("weekly:"):
        return rule
    try:
        tz = int(inst.get_chat_tz(account, chat) or 0)
    except Exception:
        return rule
    if not tz:
        return rule
    from datetime import datetime as _dt
    return "weekly:%d" % _dt.fromtimestamp(int(ts) + tz).isoweekday()


class Service:
    def __init__(self, account: str):
        self.account = account
        self.db = None
        self.db_error = ""
        self.me = ""
        self.nick = ""
        self.engine = WxWindowManager(account, log=self._log)
        self.started_at = time.time()
        self.token = ""
        self._stop = False
        self._send_lock = threading.Lock()
        self.last_error = ""
        self.last_error_ts = 0

    # ------------------------------------------------------------ 基础
    def _log(self, msg: str):
        log.info(msg)

    def _note_error(self, msg: str):
        self.last_error = str(msg)[:300]
        self.last_error_ts = int(time.time())
        log.error(self.last_error)

    def boot(self):
        cfg = C.load()
        self.token = (cfg.get("token") or "").strip()
        if self.token:
            log.info("已启用访问令牌（请求需带 X-Token）")
        else:
            # 空 token 时所有请求都放行，等于本机任何进程/页面都能调
            # /send、/config、/quit。本机部署可以接受，但必须说得清楚。
            log.warning("未设置 token：本机任何程序都能调用 /send /config /quit，"
                        "建议在 config.json 填一个 token 或用 DSHWX_TOKEN 传入")
        self._bind()
        threading.Thread(target=self._keepalive, daemon=True).start()

    def _config_matches(self) -> bool:
        return bool(self.account and C.load().get("account") == self.account)

    def _account_bound(self) -> bool:
        return bool(self.db is not None and self.me and self._config_matches()
                    and self.engine.account_is_bound())

    def _bind(self):
        self.db = None
        try:
            if not self._config_matches():
                raise RuntimeError("配置账号与启动时选定的小号不一致")
            db = open_bound_db(self.account)
            info = db.get_self_info() or {}
            self.engine.account_dir = db.account_dir
            self.engine.me_username = info["username"]
            if not self.engine.connect():
                raise RuntimeError("无法绑定所选小号的主窗口")
            self.me = info["username"]
            self.nick = info.get("nick_name") or ""
            self.db_error = ""
            self.db = db
        except Exception as e:
            error = "%s: %s" % (type(e).__name__, e)
            if self.db_error != error:
                self._note_error("绑定微信小号失败：%s" % error)
            self.db_error = error

    def _keepalive(self):
        """断开后只重绑启动时选定的小号。"""
        while not self._stop:
            time.sleep(15)
            with self._send_lock:
                if not self._account_bound():
                    self._bind()

    # ------------------------------------------------------------ 目标解析
    def resolve_target(self, chat: str, strict: bool = False,
                       allow_group: bool = False) -> dict:
        """解析唯一的微信会话，发送时另外核验窗口自身的会话 ID。

        失败时带 ``retryable``：联系人库/数据库暂时读不到属于瞬态（keepalive
        会重绑），重名这种是硬歧义，绝不自动重试。
        """
        chat = (chat or "").strip()
        if not chat:
            return {"ok": False, "error": "缺少会话", "retryable": False}
        if self.db is None:
            return {"ok": False, "error": "数据库不可用：%s" % (self.db_error or "未知"),
                    "retryable": True}

        try:
            names = self.db.nickname_map(refresh=True)
            if chat in names:
                username = chat
            elif strict:
                return {"ok": False, "error": "联系人库无法核验选定的会话 ID",
                        "retryable": True}
            else:
                users = {username for username, display in names.items() if display == chat}
                if not users:
                    hits = self.db.search_contact(chat) or []
                    users = {h["username"] for h in hits if h.get("username")
                             and chat in (h.get("remark"), h.get("nick_name"))}
                if len(users) != 1:
                    error = "显示名重名，请从会话列表选定明确的联系人" if users else "找不到会话"
                    return {"ok": False, "error": error, "retryable": not users}
                username = next(iter(users))
            if strict and username.endswith("@chatroom") and not allow_group:
                return {"ok": False, "error": "提醒和主动聊天只支持私聊", "retryable": False}
            display = names.get(username)
            if username.endswith("@chatroom"):
                display = self.db.group_id_to_name(username) or display
            if not display or display == username:
                return {"ok": False, "error": "无法核验选定会话的显示名", "retryable": True}
            if any(other != username and name == display for other, name in names.items()):
                return {"ok": False, "error": "会话显示名重名，无法安全定位窗口",
                        "retryable": False}
            return {"ok": True, "username": username, "display": display}
        except Exception:
            return {"ok": False, "error": "联系人库不可用，无法核验发送目标",
                    "retryable": True}

    # ------------------------------------------------------------ 发送
    def _verify(self, chat: str, text: str, after_seq: int, seconds: float) -> int | None:
        if self.db is None:
            return None
        deadline = time.time() + max(1.0, seconds)
        while time.time() < deadline:
            try:
                for m in self.db.get_messages(chat, limit=20):
                    if (m.get("sender_username") or "") != self.me:
                        continue
                    if (m.get("content") or "").strip() != (text or "").strip():
                        continue
                    seq = int(m.get("sort_seq") or 0)
                    if seq > after_seq:
                        return seq
            except Exception:
                pass
            time.sleep(0.8)
        return None

    @staticmethod
    def _reject(reason: str, error: str, **extra) -> dict:
        return {"ok": False, "verified": False, "submitted": False,
                "reason": reason, "error": error, **extra}

    def _send_preflight(self, queue, outbound_id, kind: str, chat: str, text: str):
        if not self._config_matches():
            return self._reject("account_mismatch", "启动账号与当前配置不一致")
        if kind == "manual":
            return None
        task = queue.get_outbound(outbound_id, account=self.account)
        if (not task or task["chat"] != chat or task["kind"] != kind
                or task["status"] != "sending" or not task.get("attempt_started_ts")):
            return self._reject("gate_changed", "当前会话没有对应的待发任务",
                                outbound_status="cancelled")
        if task.get("attempt_dispatched"):
            return self._reject("duplicate_attempt", "该段已经提交，等待微信回读", submitted=None)
        try:
            segments = json.loads(task["segments"]) if task.get("segments") else [task["text"]]
            matches = text == segments[task["segments_sent"]]
        except (IndexError, KeyError, TypeError, ValueError):
            matches = False
        if not matches:
            return self._reject("gate_changed", "待发送正文与队列记录不一致",
                                outbound_status="cancelled")

        import gate

        cfg = C.load()
        verdict = gate.evaluate(cfg, chat, paused=queue.is_paused(chat), kind=kind,
                                at_me=bool(task.get("at_me")), created_ts=gate.draft_timestamp(task),
                                approved=bool(task.get("approved_ts")))
        if verdict["action"] != "ok":
            reason = verdict["reason"]
            status = ("expired" if "过期" in reason else
                      "hold" if verdict["action"] == "hold" else "cancelled")
            return self._reject("gate_changed", reason, outbound_status=status)
        if kind in CLARIFY_KINDS:
            reason = _clarify_send_block_reason(queue, task)
        elif kind == "reminder":
            from reminder import send_block_reason

            reason = send_block_reason(queue, cfg, task)
        elif kind == "proactive":
            from proactive import send_block_reason

            reason = send_block_reason(queue, cfg, task)
        else:
            return None
        if reason:
            return self._reject("gate_changed", reason, outbound_status=(
                "expired" if "过期" in reason else "cancelled"))
        return None

    def _read_watermark(self, chat: str) -> int:
        recent = self.db.get_messages(chat, limit=1)
        return int(recent[0]["sort_seq"]) if recent else 0

    def send(self, chat: str, text: str, source: str = "manual", *,
             account: str | None = None, outbound_id: int | None = None,
             after_seq: int | None = None) -> dict:
        """账号校验、窗口提交与回读在同一服务内串行。"""
        kind = "reply" if source == "bot" else source
        if kind not in ("manual", "reply", "reminder", *CLARIFY_KINDS, "proactive"):
            return self._reject("bad_args", "未知发送来源")
        automated = kind != "manual"
        if not isinstance(chat, str) or not chat.strip() or not isinstance(text, str) or not text.strip():
            return self._reject("bad_args", "会话或消息内容为空")
        if (automated or account is not None) and account != self.account:
            return self._reject("account_mismatch", "消息所属账号与选定小号不一致")
        if automated and (WxQueue is None or type(outbound_id) is not int or outbound_id <= 0):
            return self._reject("bad_args", "缺少出站任务 ID")
        if after_seq is not None and type(after_seq) is not int:
            return self._reject("bad_args", "消息水位无效")
        with self._send_lock:
            if not self._config_matches():
                return self._reject("account_mismatch", "启动账号与当前配置不一致")
            if not self._account_bound():
                return self._reject("account_unavailable", "无法核验选定小号", retryable=True)
            target = self.resolve_target(chat, strict=automated, allow_group=kind == "reply")
            if not target["ok"]:
                return self._reject("ambiguous_target", target["error"],
                                    retryable=bool(target.get("retryable")))
            username, display = target["username"], target["display"]
            queue = WxQueue() if WxQueue is not None else None
            baseline = after_seq or 0

            def preflight(submitting: bool):
                nonlocal baseline
                rejection = self._send_preflight(queue, outbound_id, kind, username, text)
                if rejection or not submitting:
                    return rejection
                try:
                    baseline = max(baseline, self._read_watermark(username))
                except Exception:
                    return self._reject("db_unavailable", "读取微信回读水位失败", retryable=True)
                if automated and not queue.dispatch_outbound_attempt(outbound_id, self.account, baseline):
                    return (self._send_preflight(queue, outbound_id, kind, username, text)
                            or self._reject("duplicate_attempt", "无法领取该段的提交权限", submitted=None))
                return None

            blocked = preflight(False)
            if blocked:
                return blocked
            # 输入框里已有草稿时默认不清空（可能是人手写的）；清空需显式开配置
            try:
                self.engine.clear_input_draft = bool(
                    (C.load().get("send") or {}).get("clear_input_draft", False))
            except Exception:
                self.engine.clear_input_draft = False
            intent = queue.record_robot_intent(username, text) if queue and not automated else None
            res = self.engine.send(display, text, username=username, preflight=preflight)
            submitted = res.get("submitted")
            verified_seq = None
            verified = False
            if submitted:
                secs = float((C.load().get("send") or {}).get("verify_seconds", 8))
                readback = self._verify(username, text, baseline, secs)
                verified = bool(readback)
                if type(readback) is int:
                    verified_seq = readback
            res.update(account=self.account, requested=chat, username=username, resolved=display,
                       after_seq=baseline, verified=verified, verified_seq=verified_seq)
            if intent is not None:
                if verified:
                    queue.confirm_robot_sent(intent)
                    self._record_manual_message(username, text, res)
                elif submitted is False:
                    queue.drop_robot_intent(intent)
            if verified:
                log.info("微信已确认 [%s] %s" % (username, redact(text[:50])))
            elif submitted is not False:
                log.warning("微信发送结果待确认 [%s]" % username)
            return res

    @staticmethod
    def _record_manual_message(username: str, text: str, result: dict):
        try:
            if MemoryStore is not None and (C.load().get("memory") or {}).get("enabled", True):
                t0 = float(result.get("t0") or time.time())
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
                key = "manual:%s:%d:%s" % (username, int(t0 * 1000), digest)
                MemoryStore().add(username, "assistant", text, src_key=key)
        except Exception as e:
            log.warning("手动发送记忆写入失败：%s" % e)

    def status(self) -> dict:
        return {
            "ok": True,
            "account": self.account,
            "nick": self.nick,
            "account_bound": self._account_bound(),
            "me": self.me,
            "db_error": self.db_error,
            "bound_pid": self.engine.pid,
            "main_hwnd": self.engine.main_hwnd,
            "chat_windows": dict(self.engine.chat_windows),
            # 最近一次身份核验的结局：ok / unreadable / mismatch / not_ours / closed
            "identity": getattr(self.engine, "last_identity_kind", ""),
            "open_error": list(getattr(self.engine, "last_open_error", ()) or ()),
            "uptime": int(time.time() - self.started_at),
            "last_error": self.last_error,
            "last_error_ts": self.last_error_ts,
        }

    def public_config(self) -> dict:
        """给 UI 的配置：密钥与令牌不返回明文。"""
        cfg = json.loads(json.dumps(C.load(), ensure_ascii=False))
        if cfg.get("llm"):
            k = cfg["llm"].get("api_key") or ""
            cfg["llm"]["api_key"] = ("***" + k[-4:]) if len(k) > 4 else ""
        cfg.pop("token", None)
        cfg["_health"] = C.health()
        return cfg

    @C.synchronized
    def update_config(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            return {"ok": False, "error": "patch 必须是对象"}
        patch = dict(patch)
        cfg = json.loads(json.dumps(C.load(force=True), ensure_ascii=False))
        applied, skipped = [], []
        # 嵌套字段（proactive.*）：先摘出来，避免被主循环当普通二元组误存
        for key in list(NESTED_PATCHABLE):
            if key not in patch:
                continue
            _set_path(cfg, key, patch.pop(key))
            applied.append(key)
        for (section, key), value in [(tuple(k.split(".", 1)), v)
                                      for k, v in patch.items() if "." in k]:
            if (section, key) not in PATCHABLE:
                skipped.append("%s.%s" % (section, key))
                continue
            cfg.setdefault(section, {})[key] = value
            applied.append("%s.%s" % (section, key))
        # 兼容旧前端：扁平字段
        if "auto_reply" in patch and isinstance(patch["auto_reply"], bool):
            cfg.setdefault("agent", {})["auto_reply"] = patch["auto_reply"]
            applied.append("agent.auto_reply")
        if isinstance(patch.get("contacts"), dict):
            cfg["contacts"] = patch["contacts"]
            applied.append("contacts")

        problems = C._validate(cfg)
        if problems:
            return {"ok": False, "error": "配置校验失败: " + "; ".join(problems)}
        ok = C.save(cfg)
        C.load(force=True)
        if not ok:
            return {"ok": False, "error": "写配置失败"}
        if "agent.auto_reply" in applied and not cfg["agent"]["auto_reply"]:
            try:
                if WxQueue is not None:
                    # 只取消普通回复与主动闲聊。定时提醒是用户显式委托的，
                    # 不该因为关掉自动回复被一起误杀。
                    WxQueue().cancel_pending(
                        kinds=("reply", "proactive"),
                        reason="自动回复已关闭", account=self.account)
            except Exception as e:
                log.warning("取消待发任务失败：%s" % e)
        if applied:
            log.info("配置已更新：%s" % ", ".join(applied))
        return {"ok": True, "applied": applied, "skipped": skipped,
                "auto_reply": (cfg.get("agent") or {}).get("auto_reply")}

    def stats(self) -> dict:
        out = {
            "ok": True,
            "config": C.health(),
            "queue": {},
            "engine": {"pid": self.engine.pid,
                       "main_hwnd": self.engine.main_hwnd,
                       "chat_windows": len(self.engine.chat_windows)},
            "errors": {"last": self.last_error, "ts": self.last_error_ts},
        }
        if WxQueue is None:
            out["queue"] = {"error": "队列模块不可用"}
            return out
        try:
            inst = WxQueue()
            q = inst.stats(account=self.account)
            paused_rows = inst.paused_chats()
            q["paused"] = [x["chat"] for x in paused_rows]
            q["paused_detail"] = paused_rows
            q["recent_sent"] = [
                {"id": x["id"], "chat": x["chat"], "text": (x["text"] or "")[:60],
                 "ts": x["sent_ts"], "verified": x["verified"]}
                for x in inst.list_outbound("sent", 5, account=self.account)]
            q["recent_failed"] = [
                {"id": x["id"], "chat": x["chat"], "error": (x["error"] or "")[:80],
                 "ts": x["sent_ts"]}
                for x in inst.list_outbound("failed", 5, account=self.account)]
            q["uncertain"] = [
                {"id": x["id"], "chat": x["chat"], "text": (x["text"] or "")[:60],
                 "ts": x["created_ts"]}
                for x in inst.list_outbound("uncertain", 5, account=self.account)]
            q["unverified"] = [
                {"id": x["id"], "chat": x["chat"], "kind": x.get("kind"),
                 "text": (x["text"] or "")[:60], "ts": x["created_ts"]}
                for x in inst.list_outbound("unverified", 5, account=self.account)]
            out["queue"] = q
        except Exception as e:
            out["queue"] = {"error": "%s: %s" % (type(e).__name__, e)}
        return out

    def drafts(self, limit: int = 30) -> list[dict]:
        """待人工处理：敏感/截断/只出草稿（hold）、失败、不确定。"""
        if WxQueue is None:
            return []
        inst = WxQueue()
        rows = []
        for status in ("hold", "failed", "uncertain", "unverified"):
            for x in inst.list_outbound(status, limit, account=self.account):
                rows.append({
                    "id": x["id"], "chat": x["chat"], "status": status,
                    "text": x["text"], "gate": x.get("gate"),
                    "error": (x.get("error") or "")[:200],
                    "created_ts": x["created_ts"],
                    "segments_sent": x.get("segments_sent") or 0,
                    "retryable": bool(x.get("retryable")), "kind": x.get("kind"),
                    "can_approve": inst.can_approve_outbound(x),
                })
        rows.sort(key=lambda r: r["created_ts"] or 0, reverse=True)
        return rows[:limit]

    def draft_action(self, draft_id: int, action: str, text: str | None = None) -> dict:
        if WxQueue is None:
            return {"ok": False, "error": "队列模块不可用"}
        inst = WxQueue()
        task = inst.get_outbound(int(draft_id), account=self.account)
        if not task:
            return {"ok": False, "error": "任务不存在"}
        if text is not None:
            text = str(text)
            if not text.strip():
                return {"ok": False, "error": "草稿正文不能为空"}
            if len(text) > 4000:
                return {"ok": False, "error": "草稿正文过长（最多 4000 字）"}
            if task["status"] not in ("hold", "failed", "uncertain", "unverified"):
                return {"ok": False, "error": "该任务已处理或正在发送，不能再改正文"}
            if task.get("kind") in CLARIFY_KINDS and text.strip() != task["text"].strip():
                return {"ok": False, "error": "核对问句须与待确认记录一致，正文不可修改"}
            if task.get("segments_sent") and text.strip() != task["text"]:
                return {"ok": False, "error": "此消息有已发送的分段，不能修改正文"}
            if action != "approve":
                if not inst.update_outbound_text(task["id"], text, self.account):
                    return {"ok": False, "error": "正文未能保存，请刷新后重试"}
                task = inst.get_outbound(task["id"], account=self.account) or task
                log.info("草稿 #%d 正文已人工修改（%d 字）" % (task["id"], len(text)))
                if action == "update":
                    return {"ok": True, "status": task["status"], "text": task["text"]}
        elif action == "update":
            return {"ok": False, "error": "缺少正文"}
        if action == "approve":
            if task.get("kind") in CLARIFY_KINDS:
                from gate import evaluate

                verdict = evaluate(C.load(), task["chat"], paused=inst.is_paused(task["chat"]),
                                   kind=task["kind"], approved=True)
                reason = (verdict["reason"] if verdict["action"] != "ok" else
                          _clarify_send_block_reason(inst, task))
                if reason:
                    return {"ok": False, "error": reason}
            if not inst.approve_outbound(task["id"], self.account, text=text):
                log.warning("人工批准 #%d 失败：状态=%s，失败代码=%s，已发送段数=%s" % (
                    task["id"], task["status"], task.get("failure_code"), task.get("segments_sent"),
                ))
                return {"ok": False, "error": "任务当前无法批准，请核对微信发送结果和任务是否已过期"}
            log.info("已批准出站 #%d，已发送段数=%s" % (task["id"], task.get("segments_sent")))
            # 主动聊天试发期：人工批准过一次，这个会话才转自动发送
            if task.get("kind") == "proactive":
                try:
                    inst.update_proactive_state(self.account, task["chat"], trial_passed=1)
                except Exception as e:
                    log.warning("标记试发期通过失败：%s" % e)
            return {"ok": True, "status": "draft"}
        if task["status"] not in ("hold", "failed", "uncertain", "unverified"):
            return {"ok": False, "error": "该任务已处理或正在发送"}
        if action == "reject":
            result = inst.operator_outbound_result(
                task["id"], self.account, task["status"], "cancelled", "人工拒绝",
            )
            return ({"ok": True, "status": "cancelled"} if result
                    else {"ok": False, "error": "任务状态已变化，请刷新"})
        if action == "mark_sent":
            if task.get("kind") == "relay_clarify":
                return {"ok": False, "error": "代传话核对须经微信回读确认，无法人工标记已发送"}
            if not inst.operator_outbound_result(
                task["id"], self.account, task["status"], "sent", "人工标记已发出",
            ):
                return {"ok": False, "error": "任务状态已变化，请刷新"}
            try:
                inst.record_robot_sent(task["chat"], task["text"])
            except Exception:
                pass
            try:
                if MemoryStore is not None and (C.load().get("memory") or {}).get("enabled", True):
                    MemoryStore().add(task["chat"], "assistant", task["text"],
                                      src_key="manual:%s:%d" % (task["chat"], task["id"]))
            except Exception as e:
                log.warning("记忆写入失败：%s" % e)
            return {"ok": True, "status": "sent"}
        return {"ok": False, "error": "未知动作 %r（approve/reject/mark_sent）" % action}

    # ------------------------------------------------------------ 黑名单
    def blacklist(self) -> dict:
        cfg = C.load()
        bl = list(((cfg.get("agent") or {}).get("reply_blacklist")) or [])
        return {"ok": True, "blacklist": bl, "count": len(bl)}

    @C.synchronized
    def blacklist_action(self, chat: str, action: str) -> dict:
        chat = (chat or "").strip()
        if not chat:
            return {"ok": False, "error": "缺少会话"}
        if action not in ("add", "remove"):
            return {"ok": False, "error": "action 只能是 add / remove"}
        cfg = json.loads(json.dumps(C.load(force=True), ensure_ascii=False))
        bl = list(((cfg.get("agent") or {}).get("reply_blacklist")) or [])
        if action == "add" and chat not in bl:
            bl.append(chat)
        elif action == "remove":
            bl = [x for x in bl if x != chat]
        cfg.setdefault("agent", {})["reply_blacklist"] = bl
        if not C.save(cfg):
            return {"ok": False, "error": "写配置失败"}
        C.load(force=True)
        log.info("黑名单 %s：%s（现有 %d 个）" % (action, chat, len(bl)))
        return {"ok": True, "blacklist": bl, "count": len(bl)}

    def resume(self, chat: str) -> dict:
        if WxQueue is None:
            return {"ok": False, "error": "队列模块不可用"}
        WxQueue().resume_chat(chat)
        log.info("解除接管：[%s]" % chat)
        return {"ok": True, "chat": chat}

    def chats(self) -> list:
        if self.db is None:
            return []
        names = self.db.nickname_map()
        agent = (C.load().get("agent") or {})
        out = []
        # 与主动聊天同一口径（limit=300）：会话页原先 50 条，会话多的账号会
        # 比主动聊天页还少，两边列表对不上。
        for s in self.db.get_sessions(limit=300):
            username = s.get("username")
            if not username:
                continue
            out.append({
                "username": username,
                "display": names.get(username) or username,
                "unread": s.get("unread"),
                "summary": (s.get("summary") or "")[:60],
                "private": gate.is_private_chat(username, agent),
            })
        return out

    # -------------------------------------------------- 主动聊天（单位=联系人）
    @staticmethod
    def _in_hours(hours, now: float) -> bool:
        try:
            lo, hi = int(hours[0]), int(hours[1])
        except (TypeError, ValueError, IndexError):
            return True
        h = time.localtime(now).tm_hour
        if lo == hi:
            return True
        return (lo <= h < hi) if lo < hi else (h >= lo or h < hi)

    @classmethod
    def _proactive_state_text(cls, st, active: bool, now: float, day: str,
                              per_day: int, hours) -> str:
        """把运行态翻译成一句人话，让用户知道「为什么还没开口」。"""
        if not active:
            return "未启用"
        if not st:
            return "待排期"
        if int(st.get("muted") or 0):
            return "已停用（说过别主动找我）"
        ts = float(st.get("cooldown_until") or 0)
        if now < ts:
            return "冷却中 到 " + time.strftime("%m-%d %H:%M", time.localtime(ts))
        ts = float(st.get("quiet_until") or 0)
        if now < ts:
            return "静默中 到 " + time.strftime("%m-%d %H:%M", time.localtime(ts))
        if now < float(st.get("await_reply_until") or 0):
            return "已开口·待回应"
        if not cls._in_hours(hours, now):
            return "非时段"
        used = int(st.get("day_n") or 0) if st.get("day") == day else 0
        if per_day > 0 and used >= per_day:
            return "今日已满"
        return "正常"

    def proactive_contacts(self) -> list[dict]:
        """列出私聊联系人及其主动聊天状态（全局默认 + 该联系人生效参数）。"""
        cfg = C.load()
        pro = cfg.get("proactive") or {}
        wl = set(pro.get("whitelist") or [])
        contacts = cfg.get("contacts") or {}
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        acct = self.account
        inst = WxQueue() if WxQueue is not None else None
        out, seen = [], set()
        sessions = self.db.get_sessions(limit=300) if self.db else []
        for s in sessions:
            u = (s.get("username") or "").strip()
            if not gate.is_private_chat(u, cfg.get("agent")) or u in seen:
                continue
            seen.add(u)
            try:
                disp = self.db.get_nickname(u) or u
            except Exception:
                disp = u
            c = contacts.get(u) or {}
            enabled = bool(c.get("proactive"))
            active = enabled and u in wl
            st = inst.get_proactive_state(acct, u) if inst else None
            used = (int((st or {}).get("day_n") or 0)
                    if (st or {}).get("day") == day else 0)
            own = proactive_rules.params(cfg, u)
            per_day = int(own.get("per_chat_daily", 3))
            hours = own.get("active_hours") or [9, 23]
            out.append({
                "username": u,
                "display": disp,
                "in_whitelist": u in wl,
                "enabled": enabled,
                "active": active,
                "next_at": int((st or {}).get("next_at") or 0),
                "day_n": used,
                "per_chat_daily": per_day,
                "no_reply_streak": int((st or {}).get("no_reply_streak") or 0),
                "state": self._proactive_state_text(st, active, now, day,
                                                    per_day, hours),
                # 该联系人的生效参数 + 他单独设过的项（GUI 弹窗回填用）
                "params": {k: own.get(k) for k in proactive_rules.PARAM_KEYS},
                "override": proactive_rules.overrides(cfg, u),
            })
        return out

    @staticmethod
    def proactive_defaults() -> dict:
        """全局默认参数：联系人弹窗里「跟随全局」要显示这些值。"""
        cfg = C.load()
        pro = cfg.get("proactive") or {}
        return {k: pro.get(k) for k in proactive_rules.PARAM_KEYS}

    @C.synchronized
    def set_proactive_params(self, chat: str, patch) -> dict:
        """改某个联系人的主动聊天参数；传 null/空串表示恢复跟随全局。"""
        chat = (chat or "").strip()
        cfg = json.loads(json.dumps(C.load(force=True), ensure_ascii=False))
        if not gate.is_private_chat(chat, cfg.get("agent")):
            return {"ok": False, "error": "主动聊天只支持私聊（群聊与系统号不支持）"}
        values, error = proactive_rules.normalize_patch(patch)
        if error:
            return {"ok": False, "error": error}
        if not values:
            return {"ok": False, "error": "没有要保存的参数"}
        contacts = cfg.setdefault("contacts", {})
        node = contacts.get(chat)
        if not isinstance(node, dict):
            node = {}                      # 保留该联系人已有的 relation/nickname
        contacts[chat] = proactive_rules.apply_patch(node, values)
        merged = proactive_rules.params(cfg, chat)
        try:
            lo = float(merged.get("idle_min_minutes", 120))
            hi = float(merged.get("idle_max_minutes", 360))
        except (TypeError, ValueError):
            lo, hi = 120.0, 360.0
        if lo > hi:
            return {"ok": False, "error": "最小间隔不能大于最大间隔（现在是 %.0f > %.0f 分钟）"
                                          % (lo, hi)}
        problems = C._validate(cfg)
        if problems:
            return {"ok": False, "error": "配置校验失败: " + "; ".join(problems)}
        if not C.save(cfg):
            return {"ok": False, "error": "写配置失败"}
        C.load(force=True)
        return {"ok": True, "chat": chat,
                "params": {k: merged.get(k) for k in proactive_rules.PARAM_KEYS},
                "override": proactive_rules.overrides(cfg, chat)}

    @C.synchronized
    def set_proactive_contact(self, chat: str, enabled: bool) -> dict:
        """开启/关闭某联系人的主动聊天：白名单与个人开关同时写，避免漏一处。"""
        chat = (chat or "").strip()
        cfg = json.loads(json.dumps(C.load(force=True), ensure_ascii=False))
        # 群聊与系统号不能主动找上门；微信号是自定义 alias 的联系人仍算私聊
        if not gate.is_private_chat(chat, cfg.get("agent")):
            return {"ok": False, "error": "主动聊天只支持私聊（群聊与系统号不支持）"}
        pro = cfg.setdefault("proactive", {})
        wl = [x for x in (pro.get("whitelist") or []) if x != chat]
        if enabled:
            wl.append(chat)
        pro["whitelist"] = wl
        contacts = cfg.setdefault("contacts", {})
        node = contacts.get(chat)
        if not isinstance(node, dict):
            node = {}                      # 保留该联系人已有的 relation/nickname
        node["proactive"] = bool(enabled)
        contacts[chat] = node
        problems = C._validate(cfg)
        if problems:
            return {"ok": False, "error": "配置校验失败: " + "; ".join(problems)}
        if not C.save(cfg):
            return {"ok": False, "error": "写配置失败"}
        C.load(force=True)
        return {"ok": True, "chat": chat, "enabled": bool(enabled)}

    def messages(self, chat: str, limit: int = 20) -> list:
        if self.db is None:
            return []
        out = []
        for m in reversed(self.db.get_messages(chat, limit=limit)):
            out.append({
                "time": time.strftime("%m-%d %H:%M:%S",
                                      time.localtime(m.get("create_time") or 0)),
                "type": m.get("type"),
                "from_me": (m.get("sender_username") or "") == self.me,
                "content": (m.get("content") or "")[:2000],
            })
        return out

    def inbox(self, since: float = 0, limit: int = 30) -> list:
        if WxQueue is None:
            return []
        try:
            import msgtype as MT
        except Exception:
            MT = None
        rows = WxQueue().list_inbound(limit, account=self.account)
        out = []
        for r in rows:
            if since and int(r.get("msg_ts") or 0) <= since:
                continue
            mtype = r.get("msg_type") or ""
            code = r.get("type_code")
            kind, label, text = "unknown", mtype, (r.get("content") or "")
            if MT is not None:
                kind = MT.kind(mtype, code)
                d = MT.describe(mtype, r.get("content") or "", code)
                label = d.get("label") or mtype
                # 非文本消息给人看的是一句描述，不是原始 XML
                text = (r.get("content") or "") if kind == "text" else d.get("text") or ""
            out.append({
                "ts": r.get("msg_ts"),
                "chat": r.get("chat"),
                "type": label,
                "kind": kind,
                "status": r.get("status"),
                "content": text[:400],
            })
        return out

    def usage(self) -> dict:
        out = {}
        try:
            import llm
            out["llm"] = llm.usage_summary(1)
        except Exception as e:
            out["llm"] = {"error": str(e)}
        try:
            import web_tools
            out["web"] = web_tools.stats()
        except Exception as e:
            out["web"] = {"error": str(e)}
        return out

    def log_tail(self, lines: int = 80) -> list[str]:
        try:
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                return f.readlines()[-max(1, min(int(lines), 500)):]
        except OSError:
            return []


SERVICE: Service | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "wxbot/0.2"

    # 只接受回环主机名：端口已经绑 127.0.0.1，但 DNS rebinding 能让外部页面
    # 借浏览器发同源请求，Host 头白名单是这一层唯一的防线。
    ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")

    def log_message(self, fmt, *args):
        pass

    def _host_name(self) -> str:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return ""
        if host.startswith("["):            # [::1]:8765
            end = host.find("]")
            return host[:end + 1] if end > 0 else host
        return host.rsplit(":", 1)[0]

    def _host_ok(self) -> bool:
        name = self._host_name()
        # HTTP/1.0 的本机工具可能不带 Host，放行
        return (not name) or name in self.ALLOWED_HOSTS

    def _auth_ok(self) -> bool:
        tok = getattr(SERVICE, "token", "") or ""
        if not tok:
            return True
        return hmac.compare_digest((self.headers.get("X-Token") or "").encode("utf-8"),
                                   tok.encode("utf-8"))

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ GET
    PUBLIC_PATHS = ("/", "/index.html", "/ui")

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "forbidden host"}, 403)
        u = urlparse(self.path)
        q = parse_qs(u.query)
        # 页面本身不含任何数据，必须能匿名打开，否则浏览器里没地方填令牌
        if u.path not in self.PUBLIC_PATHS and not self._auth_ok():
            return self._json({"error": "unauthorized"}, 401)
        try:
            if u.path in ("/", "/index.html", "/ui"):
                ui = os.path.join(BASE, "ui.html")
                try:
                    with open(ui, "rb") as f:
                        body = f.read()
                except OSError as e:
                    return self._json({"error": "ui.html 读取失败: %s" % e}, 500)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if u.path == "/config":
                return self._json(SERVICE.public_config())
            if u.path == "/status":
                return self._json(SERVICE.status())
            if u.path == "/stats":
                return self._json(SERVICE.stats())
            if u.path == "/drafts":
                return self._json({"drafts": SERVICE.drafts(
                    int((q.get("limit") or ["30"])[0]))})
            if u.path == "/blacklist":
                return self._json(SERVICE.blacklist())
            if u.path == "/usage":
                return self._json(SERVICE.usage())
            if u.path == "/log":
                return self._json({"lines": SERVICE.log_tail(
                    int((q.get("lines") or ["80"])[0]))})
            if u.path == "/chats":
                return self._json({"chats": SERVICE.chats()})
            if u.path == "/messages":
                chat = (q.get("chat") or [""])[0]
                limit = int((q.get("limit") or ["20"])[0])
                if not chat:
                    return self._json({"error": "缺少 chat 参数"}, 400)
                return self._json({"chat": chat,
                                   "messages": SERVICE.messages(chat, limit)})
            if u.path == "/inbox":
                since = float((q.get("since") or ["0"])[0])
                return self._json({"messages": SERVICE.inbox(since)})
            if u.path == "/kb":
                from gui_facilities import kb_request
                return self._json(kb_request())
            if u.path == "/persona":
                from gui_facilities import persona_request
                return self._json(persona_request())
            if u.path == "/reminders":
                if WxQueue is None:
                    return self._json({"ok": False, "error": "队列不可用"}, 500)
                inst = WxQueue()
                status = (q.get("status") or [None])[0] or None
                chat = (q.get("chat") or [None])[0] or None
                items = inst.list_reminders(chat=chat, status=status, limit=100,
                                            account=SERVICE.account)
                for x in items:                      # 提醒的单位是「联系人 + 内容」
                    try:
                        x["display"] = (SERVICE.db.get_nickname(x["chat"])
                                        or x["chat"])
                    except Exception:
                        x["display"] = x["chat"]
                return self._json({
                    "ok": True,
                    "items": items,
                    "occurrences": inst.list_occurrences(limit=20, account=SERVICE.account),
                    "enabled": bool(((C.load().get("proactive") or {})
                                     .get("reminders") or {}).get("enabled")),
                })
            if u.path == "/proactive/contacts":
                return self._json({
                    "ok": True,
                    "items": SERVICE.proactive_contacts(),
                    "defaults": SERVICE.proactive_defaults(),
                    "enabled": bool(((C.load().get("proactive") or {})
                                     .get("enabled"))),
                })
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "forbidden host"}, 403)
        if not self._auth_ok():
            return self._json({"error": "unauthorized"}, 401)
        u = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "invalid length"}, 400)
        if not 0 <= length <= 1024 * 1024:
            return self._json({"error": "请求内容过大或长度无效"}, 400)
        if length and self.headers.get_content_type() != "application/json":
            return self._json({"error": "请求正文须使用 application/json"}, 415)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return self._json({"error": "invalid json"}, 400)
        if not isinstance(data, dict):
            return self._json({"error": "请求正文须为 JSON 对象"}, 400)

        try:
            if u.path == "/config":
                return self._json(SERVICE.update_config(data))
            if u.path == "/send":
                return self._json(SERVICE.send(
                    data.get("chat"), data.get("text"), source=data.get("source", "manual"),
                    account=data.get("account"), outbound_id=data.get("outbound_id"),
                    after_seq=data.get("after_seq"),
                ))
            if u.path == "/kb":
                from gui_facilities import kb_request
                return self._json(kb_request(data))
            if u.path == "/persona":
                from gui_facilities import persona_request
                return self._json(persona_request(data))
            if u.path == "/reminder":
                chat = str(data.get("chat") or "").strip()
                content = str(data.get("content") or data.get("text") or "").strip()
                at = str(data.get("at") or "").strip()
                rule = str(data.get("rule") or "single").strip() or "single"
                if not chat or not content:
                    return self._json({"error": "需要 chat 和 content"}, 400)
                if not at:
                    return self._json({"error": "需要 at（YYYY-MM-DD HH:MM）"}, 400)
                from datetime import datetime as _dt
                try:
                    ts = int(_dt.strptime(at, "%Y-%m-%d %H:%M").timestamp())
                except Exception:
                    return self._json({"error": "at 格式应为 YYYY-MM-DD HH:MM"}, 400)
                if ts < time.time() + 5:
                    return self._json({"error": "时间必须晚于现在"}, 400)
                if WxQueue is None:
                    return self._json({"ok": False, "error": "队列不可用"}, 500)
                target = SERVICE.resolve_target(chat, strict=True)
                if not target["ok"]:
                    return self._json(target)
                inst = WxQueue()
                # 控制台输入的是主人本机时间，调度器却按联系人当地时间推进。
                # 星期必须用**联系人当地**重算：比主机快两小时的联系人，
                # 主机「周一 23:00」在他那边是周二 01:00，用主机星期建单，
                # 第二次触发就会漂回周日。
                rule = local_weekday_rule(inst, SERVICE.account, target["username"],
                                          rule, ts)
                try:
                    zone = inst.get_chat_zone(SERVICE.account, target["username"])
                except Exception:
                    zone = None
                rid = inst.add_reminder(SERVICE.account, target["username"], content[:60],
                                        rule, ts, anchor_ts=ts, tz_zone=zone)
                log.info("手动建提醒 #%d [%s]：%s @ %s" % (
                    rid, chat, redact(content[:30]), at))
                return self._json({"ok": True, "id": rid, "next_ts": ts})
            if u.path == "/proactive/contact":
                chat = str(data.get("chat") or "").strip()
                if not chat:
                    return self._json({"error": "需要 chat"}, 400)
                return self._json(SERVICE.set_proactive_contact(
                    chat, bool(data.get("enabled"))))
            if u.path == "/proactive/params":
                chat = str(data.get("chat") or "").strip()
                if not chat:
                    return self._json({"error": "需要 chat"}, 400)
                return self._json(SERVICE.set_proactive_params(
                    chat, data.get("params")))
            if u.path.startswith("/reminders/"):
                try:
                    rid = int(u.path.rsplit("/", 1)[-1])
                except ValueError:
                    return self._json({"error": "id 无效"}, 400)
                action = str(data.get("action") or "cancel").lower()
                if action not in ("cancel", "pause", "resume"):
                    return self._json(
                        {"error": "action 只能是 cancel / pause / resume"}, 400)
                if WxQueue is None:
                    return self._json({"ok": False, "error": "队列不可用"}, 500)
                inst = WxQueue()
                rem = inst.get_reminder(rid, account=SERVICE.account)
                if not rem:
                    return self._json({"error": "提醒不存在"}, 404)
                status = {"cancel": "cancelled", "pause": "paused",
                          "resume": "active"}[action]
                n = inst.update_reminder(rid, expected_revision=rem["revision"], status=status)
                log.info("提醒 #%d 状态 -> %s（来自控制台）" % (rid, status))
                return self._json({"ok": bool(n), "id": rid, "status": status})
            if u.path == "/open":
                chat = data.get("chat")
                if not chat:
                    return self._json({"error": "需要 chat"}, 400)
                if not SERVICE._account_bound():
                    return self._json({"ok": False, "error": "无法核验选定小号"})
                target = SERVICE.resolve_target(chat)
                if not target["ok"]:
                    return self._json({"ok": False, "error": target["error"]})
                hwnd = SERVICE.engine.open_chat(target["display"], username=target["username"])
                return self._json({"ok": hwnd is not None, "chat": chat,
                                   "resolved": target["display"], "hwnd": hwnd})
            if u.path == "/drafts":
                return self._json(SERVICE.draft_action(
                    int(data.get("id") or 0), str(data.get("action") or ""),
                    text=data.get("text")))
            if u.path == "/resume":
                chat = data.get("chat")
                if not chat:
                    return self._json({"error": "需要 chat"}, 400)
                return self._json(SERVICE.resume(chat))
            if u.path == "/pause":
                from gui_facilities import pause_chat
                return self._json(pause_chat(SERVICE.account, data))
            if u.path == "/blacklist":
                return self._json(SERVICE.blacklist_action(
                    str(data.get("chat") or ""), str(data.get("action") or "")))
            if u.path == "/quit":
                threading.Thread(target=lambda: os._exit(0), daemon=True).start()
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            SERVICE._note_error("%s %s: %s" % (u.path, type(e).__name__, e))
            return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)


def main():
    global SERVICE
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default=os.environ.get("WXBOT_ACCOUNT", ""))
    ap.add_argument("--port", type=int)
    args = ap.parse_args()

    cfg = C.load()
    acc = args.account or cfg.get("account") or ""
    port = args.port or int(os.environ.get("WXBOT_PORT") or cfg.get("port") or DEFAULT_PORT)
    if not acc:
        raise SystemExit("请先选定本机已登录的微信小号，再启动 Orange")

    lock = runtime_lock.acquire(ROOT, "service", gui_child=os.environ.get("WXBOT_GUI_SESSION") == "1")
    if lock is None:
        raise SystemExit("已有 Orange 后台在运行，请先关闭对应的旧链路或 GUI 会话。")
    with lock:
        SERVICE = Service(acc)
        SERVICE.boot()
        with ThreadingHTTPServer(("127.0.0.1", port), Handler) as httpd:
            log.info("服务已启动： http://127.0.0.1:%d（回环监听）" % port)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                SERVICE._stop = True


if __name__ == "__main__":
    main()
