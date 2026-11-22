# -*- coding: utf-8 -*-
"""迁移：旧库（缺新列）打开后自动补列，数据不丢。"""
import hashlib
import json
import os
import sqlite3
import sys
import time
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
from support import check, section

section("旧队列库 → 新列")
qpath = os.path.join(support.TMP, "old_queue.db")
conn = sqlite3.connect(qpath)
conn.executescript("""
CREATE TABLE inbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, chat TEXT NOT NULL,
    local_id TEXT, msg_ts INTEGER NOT NULL, msg_type TEXT, content TEXT,
    status TEXT NOT NULL DEFAULT 'pending', created_ts INTEGER NOT NULL,
    UNIQUE(account, chat, local_id, msg_ts));
CREATE TABLE outbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, chat TEXT NOT NULL,
    text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
    attempts INTEGER NOT NULL DEFAULT 0, created_ts INTEGER NOT NULL,
    sent_ts INTEGER, error TEXT, src_msg_id INTEGER);
CREATE TABLE cursor (account TEXT PRIMARY KEY, last_ts INTEGER NOT NULL DEFAULT 0,
    updated_ts INTEGER);
CREATE TABLE lock_chat (chat TEXT PRIMARY KEY, until_ts INTEGER NOT NULL);
CREATE TABLE pause (chat TEXT PRIMARY KEY, until_ts INTEGER NOT NULL, reason TEXT);
CREATE TABLE robot_sent (id INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT NOT NULL,
    text TEXT, ts INTEGER NOT NULL);
""")
conn.execute("INSERT INTO inbound(account, chat, local_id, msg_ts, msg_type, content, "
             "status, created_ts) VALUES('acc','chatA','7',?, '文本','旧消息','done',?)",
             (int(time.time()), int(time.time())))
conn.execute("INSERT INTO outbound(account, chat, text, status, created_ts) "
             "VALUES('acc','chatA','旧回复','sent',?)", (int(time.time()),))
conn.commit()
conn.close()

from wxqueue import Queue

q = Queue(qpath)
with q._conn() as c:
    cols = {r["name"] for r in c.execute("PRAGMA table_info(inbound)")}
    ocols = {r["name"] for r in c.execute("PRAGMA table_info(outbound)")}
    rcols = {r["name"] for r in c.execute("PRAGMA table_info(robot_sent)")}
for need in ("shard", "sort_seq", "type_code", "retry_count", "not_before"):
    check("inbound 补列 %s" % need, need in cols, str(sorted(cols)))
for need in ("updated_ts", "not_before", "segments", "segments_sent", "verified", "gate"):
    check("outbound 补列 %s" % need, need in ocols, str(sorted(ocols)))
for need in ("outbound_id", "stage"):
    check("robot_sent 补列 %s" % need, need in rcols, str(sorted(rcols)))

with q._conn() as c:
    row = c.execute("SELECT content, status FROM inbound WHERE local_id='7'").fetchone()
check("旧数据仍在", row and row["content"] == "旧消息", str(dict(row) if row else None))
check("旧数据状态保留", row and row["status"] == "done")
check("旧出站数据仍在", q.get_outbound(1)["text"] == "旧回复")

check("迁移后可正常入队",
      q.enqueue_inbound("acc", "chatA", "8", int(time.time()), "文本", "新消息",
                        shard="msg0.db", sort_seq=100))
check("迁移后可正常取任务", q.create_outbound("acc", "chatB", "新回复") > 0)

section("迁移幂等（再开一次不报错）")
q2 = Queue(qpath)
check("二次打开正常", q2.stats()["inbound_pending"] >= 1)

section("旧记忆库 → src_key")
mpath = os.path.join(support.TMP, "old_memory.db")
conn = sqlite3.connect(mpath)
conn.executescript("""
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT NOT NULL,
    role TEXT NOT NULL, content TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE profiles (chat TEXT PRIMARY KEY, summary TEXT, portrait TEXT,
    turns INTEGER DEFAULT 0, updated_at INTEGER);
CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT NOT NULL,
    fact TEXT NOT NULL, ts INTEGER NOT NULL);
""")
for i in range(3):
    conn.execute("INSERT INTO messages(chat, role, content, ts) VALUES('c','user',?,?)",
                 ("旧%d" % i, int(time.time())))
conn.commit()
conn.close()

from store import MemoryStore

m = MemoryStore(mpath)
with m._conn() as c:
    mcols = {r["name"] for r in c.execute("PRAGMA table_info(messages)")}
check("messages 补 src_key", "src_key" in mcols, str(sorted(mcols)))
check("messages 补 sender", "sender" in mcols, str(sorted(mcols)))
check("旧消息还在", m.count("c") == 3, str(m.count("c")))
m.add("c", "user", "新消息", src_key="in:99")
m.add("c", "user", "新消息", src_key="in:99")
check("迁移后 src_key 去重生效", m.count("c") == 4, str(m.count("c")))
m.add("c", "user", "群里的话", src_key="in:100", sender="张三")
check("sender 能写能读",
      any(r.get("sender") == "张三" for r in m.recent("c")), str(m.recent("c")[-1]))
check("media_desc 表已建", m.media_desc("nope") is None)

section("跨设备迁移包清除凭证")
sys.path.insert(0, str(Path(support.ROOT) / "tools"))
import migrate_device as device

old_root = Path(support.TMP) / "old installation"
new_root = Path(support.TMP) / "new installation"
old_account, new_account = "wxid_migration_aaaa", "wxid_migration_bbbb"
other_account = "wxid_unrelated_cccc"
model = {"base_url": "https://example.invalid", "api_key": "old-fake-key", "model": "demo"}
old_config = {"account": old_account, "token": "old-fake-token", "llm": model}
archive = Path(support.TMP) / "transfer.zip"
now = int(time.time())
chat = "chatA"
digest = "a" * 32
image_data = b"pending fixture image"
portable_digest = hashlib.md5(image_data).hexdigest()
with patch.multiple(device, ROOT=old_root, RUNTIME=old_root / "GUI" / ".runtime",
                    BACKUP=old_root / "backup"):
    old_session = device.session_dir_for(old_account)
    device.write_json(old_root / "config.json", old_config)
    device.write_json(old_session / "config.json", old_config)
    device.write_json(old_root / "config.example.json", old_config)
    for file in (old_root / "GUI" / "app.py", old_root / "keys.json",
                 old_root / "personal.md", old_session / "runtime.log"):
        file.write_text("fixture", encoding="utf-8")
    for name in ("agent/relay.py", "docs/changes.md"):
        file = old_root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("working copy fixture", encoding="utf-8")
    source_queue = Queue(str(old_session / "queue.db"))
    task_id = source_queue.create_outbound(old_account, chat, "要迁移的内容")
    source_queue.next_outbound(account=old_account)
    source_queue.begin_outbound_attempt(task_id, 0, 10000)
    source_queue.dispatch_outbound_attempt(task_id, old_account, 10000)
    source_queue.enqueue_inbound(old_account, chat, 31, now - 600, "文本", "旧消息", sort_seq=9999)
    source_queue.set_collect_watermark(old_account, chat, 10000)
    source_queue.enqueue_inbound(other_account, chat, 31, now - 600, "文本", "其他账号的消息", sort_seq=20000)
    source_queue.set_collect_watermark(other_account, chat, 20000)
    source_queue.set_chat_tz(old_account, chat, -28800, "Europe/London")
    source_queue.add_relay_clarify(old_account, chat, "你知道吗", ["对方"], "问谁？")
    cache = old_session / "media_cache"
    cache.mkdir()
    cached = cache / f"{digest}.png"
    cached.write_bytes(b"image with a cache key")
    path_only = cache / "attachment.png"
    path_only.write_bytes(image_data)
    (cache / "unrelated.png").write_bytes(b"must not be copied")
    source_queue.add_candidate(old_account, chat, 1, "课前提醒", now,
                               images=[{"p": str(cached), "m": digest,
                                        "local_id": 42, "shard": "message_0.db"}])
    foreign_ref = {"p": str(cache / "unrelated.png"), "local_id": 91}
    source_queue.add_candidate(other_account, chat, 1, "其他账号的委托", now, images=[foreign_ref])
    source_queue.add_clarify(old_account, chat, "开会", now, "几点？",
                             images=[{"p": str(path_only), "local_id": 42,
                                      "shard": "message_0.db"}])
    watch_id = source_queue.add_relay_watch(old_account, "chatB", chat, "吃的什么？")
    source_queue.add_relay_media(watch_id, str(path_only), label="表情包",
                                 local_id=42, shard="message_0.db")
    with source_queue._conn() as conn:
        conn.execute("UPDATE relay_watch SET reply_after_seq=10000 WHERE id=?", (watch_id,))
    for with_code in (False, True):
        with redirect_stdout(StringIO()):
            device.cmd_export(SimpleNamespace(out=archive, with_code=with_code))
        with zipfile.ZipFile(archive) as package:
            for name in ("data/config.json", "data/runtime/config.json"):
                exported = json.loads(package.read(name))
                check("迁移配置清除凭证 %s / %s" % (with_code, name),
                      exported["token"] == exported["llm"]["api_key"] == ""
                      and exported["llm"]["model"] == "demo")
            names = package.namelist()
            check("代码开关决定是否带 GUI 源码", ("code/GUI/app.py" in names) == with_code)
            if with_code:
                check("代码区排除本机文件", {name for name in names if name.startswith("code/")}
                      == {"code/config.example.json", "code/GUI/app.py", "code/agent/relay.py",
                          "code/docs/changes.md"}, str(names))
                example = json.loads(package.read("code/config.example.json"))
                check("模板也不带凭证", example["token"] == example["llm"]["api_key"] == "")
            check("只导出在途事项引用的图", {name for name in names if "/media_cache/" in name}
                  == {f"data/runtime/media_cache/{digest}.png",
                      f"data/runtime/media_cache/{portable_digest}.png"}, str(names))
    check("导出不改原配置", device.read_json(old_root / "config.json") == old_config
          and device.read_json(old_session / "config.json") == old_config)

section("兼容旧迁移包并保留目标机器的凭证")
legacy = archive.with_name("legacy.zip")
old_files = {"data/config.json", "data/runtime/config.json"}
with zipfile.ZipFile(archive) as current, zipfile.ZipFile(legacy, "w") as previous:
    for name in current.namelist():
        previous.writestr(name, json.dumps(old_config) if name in old_files else current.read(name))
with patch.multiple(device, ROOT=new_root, RUNTIME=new_root / "GUI" / ".runtime",
                    BACKUP=new_root / "backup"):
    device.write_json(new_root / "config.json", {
        "token": "new-local-token", "llm": {**model, "api_key": "new-local-key"},
    })
    report = StringIO()
    with redirect_stdout(report):
        result = device.cmd_import(SimpleNamespace(package=legacy, account=new_account, apply=True,
                                                   allow_other_wxid=False))
    check("提示从原始迁移包解压代码", result == 0 and str(legacy.resolve()) in report.getvalue())
    local = device.read_json(new_root / "config.json")
    check("保留目标机器的令牌和同服务商密钥",
          local["token"] == "new-local-token" and local["llm"]["api_key"] == "new-local-key")
    new_session = device.session_dir_for(new_account)
    imported = device.read_json(new_session / "config.json")
    check("旧会话的凭证没有带入", imported["token"] == imported["llm"]["api_key"] == "")
    imported_queue = Queue(str(new_session / "queue.db"))
    draft = imported_queue.get_outbound(1)
    check("出站记录和新账号正确导入", draft["account"] == new_account and draft["text"] == "要迁移的内容")
    check("迁移前已经提交的任务只留人工核对", draft["status"] == "unverified"
          and draft["attempt_after_seq"] is None and not draft["retryable"])
    check("采集不会读回旧机 sort_seq", imported_queue.get_collect_watermark(new_account, chat) is None
          and imported_queue.max_seq(chat, account=new_account) == 0
          and bool(imported_queue.list_inbound(account=new_account)))
    check("去重键保留，新机较低序号的新消息可入队",
          not imported_queue.enqueue_inbound(new_account, chat, 31, now - 600, "文本", "旧消息", sort_seq=1)
          and imported_queue.enqueue_inbound(new_account, chat, 32, now, "文本", "新消息", sort_seq=2))
    clarify = imported_queue.get_clarify(chat, account=new_account)
    relay_clarify = imported_queue.get_relay_clarify(new_account, chat)
    watch = imported_queue.relay_watch_for(new_account, chat)
    check("提醒确认、代传话澄清、回传与时区都能按新账号读取", clarify and relay_clarify and watch
          and imported_queue.get_chat_zone(new_account, chat) == "Europe/London"
          and watch["reply_after_seq"] == 0)
    candidate = imported_queue.pending_candidates(account=new_account)[0]
    check("三个媒体引用都使用新机的缓存键，免用旧机本地消息 ID 或分片",
          json.loads(candidate["images"]) == [{"m": digest}]
          and json.loads(clarify["images"]) == [{"m": portable_digest}]
          and json.loads(watch["reply_text"]) == [{"k": "image", "l": "表情包", "m": portable_digest}]
          and (new_session / "media_cache" / f"{portable_digest}.png").read_bytes() == image_data)
    other_candidate = imported_queue.pending_candidates(account=other_account)[0]
    check("不改绑其他账号，不清其水位或重写它的缓存引用",
          imported_queue.max_seq(chat, account=other_account) == 20000
          and imported_queue.get_collect_watermark(other_account, chat) == 20000
          and json.loads(other_candidate["images"]) == [foreign_ref]
          and len(imported_queue.pending_candidates(account=new_account)) == 1)
    device.update_accounts(new_session / "queue.db", "", "wxid_unknown_dddd")
    check("源账号标识缺失时保留已有归属与水位",
          imported_queue.pending_candidates(account=new_account)[0]["id"] == candidate["id"]
          and imported_queue.get_collect_watermark(other_account, chat) == 20000)

section("旧知识库合并：仅隔离实际导入的旧自动入库记录")
import kb
from migrate_gui_data import merge_knowledge

legacy_kb = Path(support.TMP) / "legacy_knowledge.db"
current_kb = Path(support.TMP) / "current_knowledge.db"
with sqlite3.connect(legacy_kb) as conn:
    kb.ensure_schema(conn)
    conn.execute("ALTER TABLE kb_documents DROP COLUMN quarantined")
    conn.execute("PRAGMA user_version=2")
    conn.executemany(
        "INSERT INTO kb_documents(id,account,scope,kind,source_key) VALUES(?,?,'public','file',?)",
        [(1, new_account, "old collision"), (2, new_account, "old auto"),
         (3, new_account, "old manual"), (4, new_account, "old other account")])
    conn.executemany(
        "INSERT INTO kb_jobs(account,event_key,input_ref,doc_id,state) VALUES(?,?,?,?,'done')",
        [(new_account, "old collision event", "wxfile:1|same.pdf", 1),
         (new_account, "old auto event", "wxfile:1|same.pdf", 2),
         (new_account, "old manual event", "D:/manual.pdf", 3),
         (old_account, "other account event", "wxfile:1|same.pdf", 4)])
with sqlite3.connect(current_kb) as conn:
    kb.ensure_schema(conn)
    conn.execute("INSERT INTO kb_documents(id,account,scope,kind,source_key) "
                 "VALUES(1,?,'public','file','new existing')", (new_account,))
merge_knowledge(legacy_kb, current_kb, apply=False)
with sqlite3.connect(current_kb) as conn:
    check("预演不修改目标库", conn.execute("SELECT count(*) FROM kb_documents").fetchone()[0] == 1)
merge_knowledge(legacy_kb, current_kb, apply=True)
with sqlite3.connect(current_kb) as conn:
    rows = conn.execute("SELECT source_key,quarantined FROM kb_documents ORDER BY id").fetchall()
    check("本次导入的旧自动记录被隔离，已有文档和手工记录保留",
          rows == [("new existing", 0), ("old auto", 1), ("old manual", 0),
                   ("old other account", 0)], rows)

sys.exit(support.finish())
