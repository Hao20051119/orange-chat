# -*- coding: utf-8 -*-
"""回复节奏：对方分几条把一句话说完时，只回一条。

同一句话被拆成两条发、或者说完又补一个表情包时，两条消息应作为同一轮处理。

这里覆盖：
* 三档静默（问句 2s / 普通 3.5s / 半句 5s / 表情 1.5s）
* 「只起语气作用」的补充（表情、附和）并进已有一轮，不另回
* 回复还没发出去就收到补充 → 旧草稿作废，两轮并成一条
* 发送线程提交第一段之前再复检一次
"""
import json
import sys
import threading
import time
from pathlib import Path

import support
from support import check, section

import reply_worker as RW
import persona
import style_samples
from split import split_reply
from msgtype import kind as mkind

style_samples.FILE = str(Path(support.TMP) / "style_samples.json")

ACC = "wxid_test"
CHAT = "wxid_bro"
CFG = support.base_cfg(merge={"quiet_seconds": 3.5, "quiet_complete_seconds": 2.0,
                              "quiet_incomplete_seconds": 5.0,
                              "quiet_media_seconds": 1.5,
                              "absorb_after_seconds": 30,
                              "draft_merge_seconds": 300,
                              "max_wait_seconds": 12.0})


def msg(text="", mtype="文本", type_code=None):
    return {"content": text, "msg_type": mtype, "type_code": type_code}


section("三档静默")
from reply_worker import (quiet_seconds_for, absorbable_supplement,
                          is_trivial_text, batch_hint)

mc = CFG["merge"]
check("问句 → 2 秒", quiet_seconds_for([msg("你到家了吗？")], mc) == 2.0,
      str(quiet_seconds_for([msg("你到家了吗？")], mc)))
check("普通消息 → 3.5 秒", quiet_seconds_for([msg("我刚下班")], mc) == 3.5,
      str(quiet_seconds_for([msg("我刚下班")], mc)))
check("明显半句 → 5 秒", quiet_seconds_for([msg("要不我们")], mc) == 5.0,
      str(quiet_seconds_for([msg("要不我们")], mc)))
check("逗号结尾 → 5 秒", quiet_seconds_for([msg("我刚想说，")], mc) == 5.0,
      str(quiet_seconds_for([msg("我刚想说，")], mc)))
check("省略号结尾 → 5 秒", quiet_seconds_for([msg("那个…")], mc) == 5.0,
      str(quiet_seconds_for([msg("那个…")], mc)))
check("表情 → 1.5 秒", quiet_seconds_for([msg("", "动画表情")], mc) == 1.5,
      str(quiet_seconds_for([msg("", "动画表情")], mc)))
check("以最后一条为准",
      quiet_seconds_for([msg("你到家了吗？"), msg("我刚想说")], mc) == 5.0)
check("问句不因为没有问号就漏判",
      quiet_seconds_for([msg("在吗")], mc) == 2.0,
      str(quiet_seconds_for([msg("在吗")], mc)))
check("配置缺失走默认", quiet_seconds_for([msg("我刚下班")], {}) == 3.5)

section("「只起语气作用」的补充")
check("单个表情算", absorbable_supplement([msg("", "动画表情")]))
check("「嗯」算", absorbable_supplement([msg("嗯")]))
check("「哈哈哈」算", absorbable_supplement([msg("哈哈哈")]))
check("文字+表情算", absorbable_supplement([msg("好"), msg("", "动画表情")]))
check("有真话不算", not absorbable_supplement([msg("明天再看好了")]))
check("图片不算（可能带问题）", not absorbable_supplement([msg("", "图片")]))
check("语音不算", not absorbable_supplement([msg("", "语音")]))
check("「晚安」不算附和", not is_trivial_text("晚安"))
check("「好的吧」不算附和", not is_trivial_text("好的吧"))
check("空批不算", not absorbable_supplement([]))

check("连着两条才提示",
      batch_hint([msg("一"), msg("二")]).count("只回一次") == 1)
check("单条不提示", batch_hint([msg("一")]) == "")


# ------------------------------------------------------------------ 假对象
class FakeDB:
    def get_messages(self, chat, limit=30):
        return []


class FakeBrain:
    def __init__(self, replies=None):
        self.cfg = CFG
        self.mem = support.make_memory("pacing_mem_%d.db" % id(self))
        self.calls = []
        self._replies = list(replies or ["在呀，怎么啦"])

    def reload(self, cfg):
        self.cfg = cfg

    def should_reply(self, chat, text, **kw):
        return True, "ok"

    def compose(self, chat, text, extra="", **kw):
        self.calls.append((text, extra))
        i = min(len(self.calls) - 1, len(self._replies) - 1)
        return {"ok": True, "reply": self._replies[i]}

    def sensitive_label(self, reply):
        return None

    def contact_cfg(self, chat):
        return {}

    def human_delay(self, chat=None):
        return 0

    def maybe_summarize(self, chat):
        return {"ok": False}

    def maybe_adapt(self, chat):
        return {"ok": False}


def make_worker(q, brain=None, cfg=None):
    w = RW.Worker.__new__(RW.Worker)          # 跳过 WeChatDB 初始化
    w.cfg = cfg or dict(CFG)
    w.q = q
    w.brain = brain or FakeBrain()
    w.me = "me"
    w.db = FakeDB()
    w.acc = ACC
    w._watermark = {}
    w._HOOK_LAST = {CHAT: time.time()}        # 跳过钩子抽取（会调模型）
    w._inflight = set()
    w._inflight_lock = threading.Lock()
    return w


def setup():
    q = support.make_queue("p%d.db" % setup.n)
    setup.n += 1
    RW.load_cfg = lambda: dict(CFG)           # 不让测试读生产 config.json
    return q


setup.n = 0

section("文字 + 表情包：只回一条")
q = setup()
brain = FakeBrain(["那早点睡，别又刷到一点"])
w = make_worker(q, brain)
q.enqueue_inbound(ACC, CHAT, "l1", int(time.time()) - 6, "文本", "今天累死了")
w.process(CHAT)
check("先回了一条", len(q.list_outbound("draft")) == 1,
      str([(x["id"], x["text"]) for x in q.list_outbound("draft")]))
draft = q.list_outbound("draft")[0]
check("草稿记下了它答过哪条入站",
      json.loads(draft.get("covered") or "[]") == [1], str(draft.get("covered")))

q.enqueue_inbound(ACC, CHAT, "l2", int(time.time()), "动画表情",
                  "<msg><emoji md5='x'/></msg>")
w.process(CHAT)
check("表情没有再生成回复", len(brain.calls) == 1, str(len(brain.calls)))
check("仍然只有一条草稿", len(q.list_outbound("draft")) == 1)
check("表情被标记为已处理",
      [x["status"] for x in q.list_inbound(5) if x["content"].startswith("<msg>")]
      == ["done"], str([x["status"] for x in q.list_inbound(5)]))

section("刚回过一句，对方只补个「嗯」→ 不再回")
q = setup()
brain = FakeBrain(["你先睡吧"])
w = make_worker(q, brain)
tid = q.create_outbound(ACC, CHAT, "你先睡吧")
q.finish_outbound(tid, "sent", verified=1)
q.enqueue_inbound(ACC, CHAT, "l1", int(time.time()) - 1, "文本", "嗯")
w.process(CHAT)
check("没有新草稿", len(q.list_outbound("draft")) == 0,
      str([(x["id"], x["text"]) for x in q.list_outbound("draft")]))
check("没再叫模型", brain.calls == [])

section("回复正在发（还在拟人延迟里），对方补个表情 → 算进这条")
q = setup()
brain = FakeBrain(["那早点睡"])
w = make_worker(q, brain)
q.enqueue_inbound(ACC, CHAT, "l1", int(time.time()) - 6, "文本", "今天累死了")
w.process(CHAT)
task = q.next_outbound()
check("草稿被发送线程领走", task is not None and q.get_outbound(task["id"])["status"]
      == "sending", str(q.get_outbound(task["id"])["status"]))
q.enqueue_inbound(ACC, CHAT, "l2", int(time.time()), "动画表情",
                  "<msg><emoji md5='x'/></msg>")
w.process(CHAT)
check("没有再生成一条", len(brain.calls) == 1, str(len(brain.calls)))
check("表情标记已处理",
      [x["status"] for x in q.list_inbound(5) if x["content"].startswith("<msg>")]
      == ["done"])

section("回复还没发出就补充：旧稿作废，合成一条")
q = setup()
brain = FakeBrain(["周六也行呀", "周五也行呀"])
w = make_worker(q, brain)
q.enqueue_inbound(ACC, CHAT, "l1", int(time.time()) - 6, "文本", "周六去吗？")
w.process(CHAT)
first = q.list_outbound("draft")[0]
check("第一版草稿产生", first["text"] == "周六也行呀", first["text"])

q.enqueue_inbound(ACC, CHAT, "l2", int(time.time()), "文本", "说错了，周五")
w.process(CHAT)
rows = q.list_outbound(limit=10)
by_id = {r["id"]: r for r in rows}
check("旧草稿被作废", by_id[first["id"]]["status"] == "superseded",
      by_id[first["id"]]["status"])
check("只剩一条能发的草稿",
      [r["text"] for r in rows if r["status"] == "draft"] == ["周五也行呀"],
      str([(r["id"], r["status"], r["text"]) for r in rows]))
check("旧稿的文字当成本轮参考", "周六也行呀" in brain.calls[-1][1],
      brain.calls[-1][1][:120])
check("提示只回一次", "只回一次" in brain.calls[-1][1])
check("合并后仍是 2 次生成（不是 3 次）", len(brain.calls) == 2, str(len(brain.calls)))

section("发送线程：提交前发现补充 → 作废，一个字都不发")
q = setup()
w = make_worker(q)
sent = []
RW.post = lambda path, obj, timeout=90: (sent.append(obj["text"]),
                                         {"ok": True, "verified": True})[1]
q.enqueue_inbound(ACC, CHAT, "l1", int(time.time()) - 8, "文本", "周六去吗？")
tid = q.create_outbound(ACC, CHAT, "周六也行呀", src_msg_id=1, covered=[1],
                        segments=["周六也行呀"])
q.enqueue_inbound(ACC, CHAT, "l2", int(time.time()), "文本", "说错了，周五")
task = q.next_outbound()
res = w.send_task(task, sleep_delay=False, reload_cfg=False)
check("返回 superseded", res["status"] == "superseded", str(res))
check("真的没发出去", sent == [], str(sent))
check("库里 superseded", q.get_outbound(tid)["status"] == "superseded")

q = setup()
w = make_worker(q)
sent = []
RW.post = lambda path, obj, timeout=90: (sent.append(obj["text"]),
                                         {"ok": True, "verified": True})[1]
q.enqueue_inbound(ACC, CHAT, "l1", int(time.time()) - 8, "文本", "今天累死了")
tid = q.create_outbound(ACC, CHAT, "那早点睡", src_msg_id=1, covered=[1],
                        segments=["那早点睡"])
q.enqueue_inbound(ACC, CHAT, "l2", int(time.time()), "动画表情",
                  "<msg><emoji md5='x'/></msg>")
task = q.next_outbound()
res = w.send_task(task, sleep_delay=False, reload_cfg=False)
check("表情补充不妨碍发送", res["status"] == "sent", str(res))
check("只发一条", sent == ["那早点睡"], str(sent))
check("表情计入这条回复",
      [x["status"] for x in q.list_inbound(5) if x["content"].startswith("<msg>")]
      == ["done"])

section("端到端：真人设提示词 + 真分段，文字→补表情→只发一条一个气泡")
import brain as brain_mod
from brain import Brain

q = setup()
cfg_e2e = dict(CFG)
cfg_e2e["llm"] = {"api_key": "sk-test", "model": "deepseek-chat"}
real_brain = Brain(cfg_e2e)
brain_mod.llm_chat = lambda msgs, c=None, **kw: {
    "ok": True, "text": "那早点睡，别又刷到一点"}
w = make_worker(q, real_brain)
w.cfg = cfg_e2e
q.enqueue_inbound(ACC, CHAT, "l1", int(time.time()) - 6, "文本", "今天累死了")
w.process(CHAT)
q.enqueue_inbound(ACC, CHAT, "l2", int(time.time()), "动画表情",
                  "<msg><emoji md5='x'/></msg>")
w.process(CHAT)
posts = []
RW.post = lambda path, obj, timeout=90: (posts.append(obj["text"]),
                                         {"ok": True, "verified": True})[1]
task = q.next_outbound()
res = w.send_task(task, sleep_delay=False, reload_cfg=False)
check("发出去了", res["status"] == "sent", str(res))
check("一共只发 1 条", len(posts) == 1, str(posts))
check("这条里只有 1 个气泡", len(split_reply(posts[0], max_chars=60,
                                             max_bubbles=2)) == 1, str(posts))
rows = q.list_outbound(limit=10)
check("库里只有 1 条回复任务", len(rows) == 1,
      str([(r["id"], r["status"], r["text"]) for r in rows]))
check("入站都结案了", not q.pending_inbound_count(), str(q.pending_inbound_count()))

section("一条回复只出一个气泡")
check("默认一条", len(split_reply("在呀，怎么啦", max_chars=60, max_bubbles=2)) == 1)
check("惊叹句连说也在一条",
      len(split_reply("真的假的！请客请客，别装没听见 (≧▽≦)",
                      max_chars=60, max_bubbles=2)) == 1)
check("模型主动换行才分条",
      len(split_reply("在呀\n怎么啦", max_chars=60, max_bubbles=2)) == 2)

section("提示词与样例")
p = persona.build_system_prompt("【当前情况】测试", dict(CFG))
check("有【回复节奏】段", "【回复节奏】" in p)
check("写明拆几条也只回一次", "合并成一次回" in p)
check("写明表情包不是新话题", "表情包，那是语气不是新话题" in p)
check("写明默认一条气泡", "默认一条气泡" in p)
preferred = "走呀，一起吃饭 (≧▽≦)"
style_samples.add("吃饭", "去吃饭吗", "好\n马上来", preferred, approved=True)
style_samples.add("吃饭", "去吃饭吗", "好", "还在审核的样例")
items = style_samples.pick("吃饭")
check("只选已批准的样例", len(items) == 1 and items[0]["approved"])
check("单气泡样例及颜文字原样注入", preferred in style_samples.build_prompt("吃饭"))

sys.exit(support.finish())
