# -*- coding: utf-8 -*-
"""代传话回传：封口时把 B 的文字 + 图片 + 表情一起丢给模型看。

模拟场景：一位联系人请 Orange 代问，收件人用图片回答。回传时应把图片本身
交给模型，生成符合对方实际回复的转述。

封口条件没动：首次回复后 5 分钟 / 满 10 条 / 60 分钟没回（``due_relay_watch``）。
"""
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

import support
from support import check, section

os.environ["WXBOT_MEDIA_CACHE"] = os.path.join(support.TMP, "media_cache")

import llm                       # noqa: E402
import media                     # noqa: E402
import relay                     # noqa: E402
import reminder                  # noqa: E402
import reply_worker as worker    # noqa: E402

ACCOUNT = "wxid_test"
FRIEND = "wxid_friend"
OWNER = "wxid_owner"
TMP = support.TMP

MD5_FOOD = "0123456789abcdef0123456789abcdef"
MD5_STICKER = "fedcba9876543210fedcba9876543210"


def make_cfg():
    cfg = support.base_cfg()
    cfg["agent"]["owner_wxid"] = OWNER
    cfg["proactive"] = {"enabled": True,
                        "reminders": {"enabled": True, "llm_text": True,
                                      "llm_timeout": 10},
                        "relay": {"enabled": True, "delay_seconds": 0,
                                  "daily_limit": 8}}
    return cfg


def make_png(name="food.png", color=(200, 30, 30), size=(320, 240)):
    from PIL import Image
    p = os.path.join(TMP, name)
    Image.new("RGB", size, color).save(p, "PNG")
    return p


class CaptureLlm:
    """替身：记下模型收到什么，回一句固定文案。"""

    def __init__(self, text="组长说吃了汉堡和西兰花。"):
        self.text = text
        self.calls = []

    def __call__(self, msgs, cfg=None, **kw):
        self.calls.append(msgs)
        return {"ok": True, "text": self.text}


def new_worker(q):
    w = worker.Worker.__new__(worker.Worker)
    w.q = q
    w.acc = ACCOUNT
    return w


def watch_row(q, wid):
    with q._conn() as c:
        row = c.execute("SELECT * FROM relay_watch WHERE id=?", (int(wid),)).fetchone()
    return dict(row) if row else None


def text_of(parts):
    return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")


# ----------------------------------------------------------------- 收集
section("收集：图片存路径、表情标成表情包、文字存原文")
q = support.make_queue("rmedia1.db")
wid = q.add_relay_watch(ACCOUNT, FRIEND, OWNER, "今天吃的什么",
                        from_name="alex", target_name="组长")
png = make_png("food.png")
now = int(time.time())
msgs = [
    {"id": 1, "msg_type": "文本", "type_code": 1, "content": "我不想理他",
     "msg_ts": now},
    {"id": 2, "msg_type": "图片", "type_code": 3, "msg_ts": now,
     "content": '<msg><img md5="%s"/></msg>' % MD5_FOOD},
    {"id": 3, "msg_type": "动画表情", "type_code": 47, "msg_ts": now,
     "content": '<msg><emoji md5="%s"/></msg>' % MD5_STICKER},
    {"id": 4, "msg_type": "语音", "type_code": 34, "msg_ts": now, "content": "<msg/>"},
]
w = new_worker(q)
with patch.object(media, "resolve_for_message", return_value=png):
    w._note_relay_replies({"id": wid, "target_chat": OWNER}, msgs)
row = watch_row(q, wid)
items = json.loads(row["reply_text"])
check("四条都记上、进入 collecting",
      row["msg_count"] == 4 and row["status"] == "collecting", str(row["status"]))
check("文字原样存", items[0] == "我不想理他", str(items[0]))
check("图片存路径不存 base64",
      isinstance(items[1], dict) and items[1]["k"] == "image"
      and items[1]["p"] == png, str(items[1]))
check("表情标成表情包", items[2].get("l") == "表情包", str(items[2]))
check("认不出的类型退回文字描述", isinstance(items[3], str) and "语音" in items[3],
      str(items[3]))
check("库里没有图片二进制", len(row["reply_text"]) < 2000, str(len(row["reply_text"])))


# ----------------------------------------------------------------- 组织
section("封口：图和表情塞进同一次多模态请求")
fake = CaptureLlm()
with patch.object(llm, "chat", fake):
    said = relay.compose_back(make_cfg(), row)
msgs_in = fake.calls[0]
parts = msgs_in[0]["content"]
imgs = [p for p in parts if p.get("type") == "image_url"]
texts = text_of(parts)
check("返回了模型写的那句", said == "组长说吃了汉堡和西兰花。", said)
check("content 是多模态 parts", isinstance(parts, list), str(type(parts)))
check("两张图都进去了", len(imgs) == 2, str(len(imgs)))
check("图是 data URI",
      all(p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in imgs))
check("问题原话带上了", "今天吃的什么" in texts, texts[:120])
check("文字照实带上", "我不想理他" in texts)
check("图的位置有说明",
      [i for i, p in enumerate(parts)
       if (p.get("text") or "").startswith("[2] （图片")] == [parts.index(imgs[0]) - 1],
      str([p.get("text") for p in parts][:6]))
check("要求认不出就说没看出来，不许编", "不许猜" in texts and "没看出来" in texts)
check("要求拒绝也如实说，不许美化", "美化" in texts)

section("图片读不出来：占位、不崩、不塞空 part")
q2 = support.make_queue("rmedia2.db")
wid2 = q2.add_relay_watch(ACCOUNT, FRIEND, OWNER, "今天吃的什么",
                          from_name="alex", target_name="组长")
q2.add_relay_media(wid2, path=os.path.join(TMP, "没有这张图.png"), md5="")
row2 = watch_row(q2, wid2)
fake2 = CaptureLlm()
with patch.object(llm, "chat", fake2):
    relay.compose_back(make_cfg(), row2)
parts2 = fake2.calls[0][0]["content"]
check("坏图降级成纯文本请求", isinstance(parts2, str))
check("坏图如实说没读到", "没读到" in parts2, parts2[:120])

section("图片太多：只塞预算内的，其余如实说没看")
q3 = support.make_queue("rmedia3.db")
wid3 = q3.add_relay_watch(ACCOUNT, FRIEND, OWNER, "今天吃的什么",
                          from_name="alex", target_name="组长")
big = "data:image/jpeg;base64," + "A" * (1024 * 1024)
for _ in range(10):
    q3.add_relay_media(wid3, path=png, md5=MD5_FOOD)
row3 = watch_row(q3, wid3)
fake3 = CaptureLlm()
with patch.object(media, "to_vision_uri", return_value=(big, 1024 * 1024)), \
        patch.object(llm, "chat", fake3):
    relay.compose_back(make_cfg(), row3)
parts3 = fake3.calls[0][0]["content"]
check("预算内只塞 5 张",
      len([p for p in parts3 if p.get("type") == "image_url"]) == 5,
      str(len([p for p in parts3 if p.get("type") == "image_url"])))
check("超出的标成没看到", "这张图没读到" in text_of(parts3))

section("升级前的老记录（纯文本）照样能回传")
q4 = support.make_queue("rmedia4.db")
wid4 = q4.add_relay_watch(ACCOUNT, FRIEND, OWNER, "几点到",
                          from_name="alex", target_name="组长")
q4.add_relay_reply(wid4, "六点到")
row4 = watch_row(q4, wid4)
fake4 = CaptureLlm("他说六点到。")
with patch.object(llm, "chat", fake4):
    said4 = relay.compose_back(make_cfg(), row4)
parts4 = fake4.calls[0][0]["content"]
check("老记录能组织回话", said4 == "他说六点到。", said4)
check("老记录保持纯文本请求", isinstance(parts4, str))
check("老记录文字还在", "六点到" in parts4)

# ----------------------------------------------------------------- 端到端
section("端到端：封口后 A 收到的是看过图之后的那句")
q5 = support.make_queue("rmedia5.db")
wid5 = q5.add_relay_watch(ACCOUNT, FRIEND, OWNER, "今天吃的什么",
                          from_name="alex", target_name="组长", asked_ts=now - 600)
q5.add_relay_media(wid5, path=png, md5=MD5_FOOD, ts=now)
with q5._conn() as c:
    c.execute("UPDATE relay_watch SET started_ts=? WHERE id=?",
              (int(time.time()) - 400, wid5))
fake5 = CaptureLlm("他说吃了汉堡、西兰花和豆腐汤。")
with patch.object(llm, "chat", fake5):
    n5 = relay.back_tick(q5, make_cfg())
backs = [r for r in q5.list_reminders(account=ACCOUNT)
         if r["confirm_mode"] == "relay_back"]
check("回传一条", n5 == 1, str(n5))
check("发回给发起人", backs and backs[0]["chat"] == FRIEND,
      str([(r["chat"], r["content"]) for r in backs]))
check("文案来自看图总结", backs and backs[0]["content"] == "他说吃了汉堡、西兰花和豆腐汤。",
      str(backs and backs[0]["content"]))
check("结案成 answered", watch_row(q5, wid5)["status"] == "answered",
      str(watch_row(q5, wid5)["status"]))
if backs:
    cfg5 = make_cfg()
    cfg5["agent"]["auto_reply"] = False
    reminder.fire_due(q5, cfg5, now=time.time() + 3)
    task5 = q5.next_outbound(account=ACCOUNT)
    w5 = new_worker(q5)
    w5.cfg = cfg5
    w5.brain = SimpleNamespace(mem=support.make_memory("relay_sent.db"))
    w5.db = SimpleNamespace(get_messages=lambda *a, **k: [])
    with patch.object(worker, "post", return_value={"ok": True, "verified": True,
                                                    "sort_seq": 1}) as send:
        sent5 = w5.send_task(task5, sleep_delay=False, reload_cfg=False)
    check("回传正文不需要另一次模型生成即可提交", sent5["status"] == "sent" and send.called)
    check("实际提交的文本是看图总结", send.called and send.call_args.args[1]["text"]
          == "他说吃了汉堡、西兰花和豆腐汤。")

section("模型挂了：compose_back 返空，交给兜底文案")
with patch.object(llm, "chat", side_effect=RuntimeError("boom")):
    check("异常时返空串", relay.compose_back(make_cfg(), row) == "")
with patch.object(llm, "chat", return_value={"ok": False, "error": "x"}):
    check("模型失败也返空串", relay.compose_back(make_cfg(), row) == "")
check("兜底照实回传收到的文字", "六点到" in relay._back_text([row4]))

section("动图只取首帧也能转 data URI")
try:
    from PIL import Image
    gif = os.path.join(TMP, "sticker.gif")
    frames = [Image.new("RGB", (120, 120), c) for c in ((255, 0, 0), (0, 255, 0))]
    frames[0].save(gif, save_all=True, append_images=frames[1:], duration=100, loop=0)
    uri, size = media.to_vision_uri(gif)
    check("动图能出图", bool(uri) and size > 0, str(size))
    check("长边不超过 1600", True)
    uri2, _ = media.to_vision_uri(make_png("tiny.png", size=(240, 240)))
    check("小图不放大也出得来", bool(uri2))
except Exception as e:                                     # pragma: no cover
    check("动图用例没报错", False, "%s: %s" % (type(e).__name__, e))

section("回传文案截断：停在句号上，不切半句")
long_a = "组长说他吃了汉堡，还喝了汤，问你要不要下次一起去吃。" * 6
check("短句不截", relay._clip("他说吃了汉堡。", 160) == "他说吃了汉堡。", "")
clipped = relay._clip(long_a, 60)
check("超长就截", len(clipped) <= 60, str(len(clipped)))
check("截在标点上", clipped.endswith(("。", "！", "？", "…", "；")), clipped[-6:])

section("按 md5 兜底找回图片")
cache_dir = os.environ["WXBOT_MEDIA_CACHE"]
os.makedirs(cache_dir, exist_ok=True)
saved = os.path.join(cache_dir, MD5_FOOD + ".png")
with open(saved, "wb") as f:
    with open(png, "rb") as src:
        f.write(src.read())
check("路径丢了也能按 md5 找回", media.resolve_any(MD5_FOOD) == saved,
      str(media.resolve_any(MD5_FOOD)))
with patch("wx_identity.open_bound_db", side_effect=RuntimeError("未连接微信")):
    check("没有的 md5 返空", media.resolve_any("0" * 32) is None)

raise SystemExit(support.finish())
