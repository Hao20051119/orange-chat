# -*- coding: utf-8 -*-
"""带图的提醒：粗筛命中后把「文字 + 图」一起给模型，信息不全就问。

来历：一句「以后每次化学前沿课前十分钟都提醒我」+ 一张课表图。只看文字永远
不知道那门课是周几几点；只看图又不知道要提前多少。所以两者一起判，判完：

* 信息齐全、也没图 → 直接建单
* 缺信息 / 带图 → 先问一句核对（「是不是要在每周二 13:50 提醒你…？」）
* 他回「对」→ 按提案建单；回「不对，周三」→ 按新说法重建；一直答非所问 → 放弃
"""
import json
import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import support
from support import check, section

os.environ["WXBOT_MEDIA_CACHE"] = os.path.join(support.TMP, "media_cache")

import llm          # noqa: E402
import media        # noqa: E402
import reminder     # noqa: E402
import reply_worker # noqa: E402

ACCOUNT = "wxid_test"
CHAT = "wxid_contact"
TMP = support.TMP

PNG = os.path.join(TMP, "timetable.png")
from PIL import Image                                    # noqa: E402
Image.new("RGB", (320, 240), (30, 60, 90)).save(PNG, "PNG")

IMGS = [{"p": PNG, "m": "0123456789abcdef0123456789abcdef", "l": "图片"}]
NOW = int(time.time())


def make_cfg(**rem):
    base = {"enabled": True, "llm_text": True, "llm_timeout": 10,
            "confirm_with_images": True, "clarify_max_asks": 3}
    base.update(rem)
    return support.base_cfg(proactive={"enabled": True, "reminders": base})


def ts_of(rule_weekday, hour, minute):
    """下个周 X 的 HH:MM 的时间戳（离现在够远，测试不会踩到过去）。"""
    now = datetime.now()
    delta = (rule_weekday - now.isoweekday()) % 7
    if delta == 0 and (now.hour, now.minute) >= (hour, minute):
        delta = 7
    dt = (now + timedelta(days=delta)).replace(hour=hour, minute=minute,
                                               second=0, microsecond=0)
    return int(dt.timestamp())


def inbox(q, text, mid=1, base_ts=None):
    q.enqueue_inbound(ACCOUNT, CHAT, mid, base_ts or NOW, "文本", text, sender=CHAT)
    return q.newest_pending(CHAT, account=ACCOUNT)[-1]


def verdict(**kw):
    base = {"is_reminder": True, "confidence": 0.9, "content": "化学前沿课前准备",
            "rule": "weekly:2", "next_ts": ts_of(2, 13, 50), "need_time": False,
            "ask": "", "tz": "", "next_local": ""}
    base.update(kw)
    return base


# ---------------------------------------------------------------- 判定阶段
section("粗筛命中后：文字 + 图 一起交给模型")
q = support.make_queue("rv1.db")
msg = inbox(q, "以后每次化学前沿课前十分钟都记得提醒我一下")
seen = {}


def fake_judge(cfg, text, base_ts, tz=None, tz_zone=None, images=None):
    seen["text"] = text
    seen["images"] = images
    return verdict()


with patch.object(reminder, "judge_with_llm", side_effect=fake_judge):
    ids = reminder.maybe_capture(make_cfg(), q, CHAT, [msg], ACCOUNT, images=IMGS)
check("图传到了判定层", seen.get("images") == IMGS, str(seen.get("images")))
check("带图时先进待确认（默认要核对）", ids == [] and bool(q.get_clarify(CHAT, "pending", account=ACCOUNT)), str(ids))
plan = reminder.plan_decision(verdict(), {"confirm_with_images": False}, False)
check("无图+信息全 → create", plan["action"] == "create", str(plan))
plan = reminder.plan_decision(verdict(), {"confirm_with_images": True}, True)
check("带图 → 先核对（clarify）", plan["action"] == "clarify", str(plan))
check("核对句带时间且是问句",
      "每周二" in plan["ask"] and plan["ask"].endswith("？"), plan["ask"])
check("提案时间随问句一起存", plan["proposed_ts"] == verdict()["next_ts"], str(plan))
plan = reminder.plan_decision(verdict(need_time=True, next_ts=None,
                                      ask="化学前沿是周几几点？"),
                              {"confirm_with_images": True}, True)
check("缺时间 → clarify", plan["action"] == "clarify", str(plan))
check("缺时间时用模型的问法", plan["ask"] == "化学前沿是周几几点？", plan["ask"])
plan = reminder.plan_decision(verdict(need_time=True, next_ts=ts_of(1, 7, 50),
                                      ask="化学前沿课是周几几点上？"),
                              {"confirm_with_images": False}, False)
check("缺时间时模型瞎填的时间不进提案",
      plan["proposed_ts"] is None and plan["rule"] == "", str(plan))
plan = reminder.plan_decision(verdict(confidence=0.3),
                              {"confirm_with_images": False}, False)
check("把握不足 → skip", plan["action"] == "skip", str(plan))

section("judge_with_llm：真的把图发给模型")
captured = {}


class CapLlm:
    def __call__(self, msgs, cfg=None, **kw):
        captured["msgs"] = msgs
        payload = dict(verdict())
        payload["next"] = datetime.fromtimestamp(
            payload.pop("next_ts")).strftime("%Y-%m-%d %H:%M")
        return {"ok": True, "text": json.dumps(payload, ensure_ascii=False)}


with patch.object(llm, "chat", CapLlm()):
    r = reminder.judge_with_llm(make_cfg(), "以后每次化学前沿课前十分钟提醒我",
                                NOW, images=IMGS)
content = captured["msgs"][0]["content"]
check("多模态 parts", isinstance(content, list), str(type(content)))
check("带了一张图",
      sum(1 for p in content if p.get("type") == "image_url") == 1, "")
prompt = content[0]["text"]
check("提示里点明看图找时间", "先看图" in prompt and "课表" in prompt, "")
check("提示要求核对句", "核对用" in prompt, "")
check("解析出规则与时间", r and r["rule"] == "weekly:2" and r["next_ts"], str(r))
check("记下「这次真看了图」", r and r.get("used_images") is True, str(r))

section("图读不出来时：不当作「带图判定」，别拿来要核对")
captured.clear()
with patch.object(llm, "chat", CapLlm()):
    r_bad = reminder.judge_with_llm(make_cfg(), "以后每次课前十分钟提醒我", NOW,
                                   images=[{"p": os.path.join(TMP, "没有这张.png")}])
check("没图可看就是 False", r_bad and r_bad.get("used_images") is False, str(r_bad))
plan_bad = reminder.plan_decision(r_bad, {"confirm_with_images": True},
                                  bool(r_bad.get("used_images")))
check("图没解出来时请求补时间", plan_bad["action"] == "clarify"
      and plan_bad["proposed_ts"] is None, str(plan_bad))
with patch.object(llm, "chat", CapLlm()):
    r_bad = reminder.judge_with_llm(make_cfg(), "明天早上八点提醒我上课", NOW,
                                   images=[{"p": os.path.join(TMP, "没有这张.png")}])
check("坏图不影响文字已说明的钟点", r_bad and not r_bad["used_images"] and not r_bad["need_time"])

section("模型没给图：还是老样子（纯文本）")
captured.clear()
with patch.object(llm, "chat", CapLlm()):
    reminder.judge_with_llm(make_cfg(), "明天八点提醒我跑步", NOW)
check("纯文本请求", isinstance(captured["msgs"][0]["content"], str), "")

# ---------------------------------------------------------------- 待确认
def clarify_of(q):
    return q.get_clarify(CHAT, "asked", account=ACCOUNT) or q.get_clarify(
        CHAT, "pending", account=ACCOUNT)


def send_prompt(q):
    task = q.next_outbound(account=ACCOUNT)
    assert task and task["kind"] == "reminder_clarify", task
    q.finish_outbound(task["id"], "sent", verified=1, expected="sending")
    return task


section("带图判定：先问一句，不急着建单")
q2 = support.make_queue("rv2.db")
msg2 = inbox(q2, "以后每次化学前沿课前十分钟都记得提醒我一下")
with patch.object(reminder, "judge_with_llm",
                  side_effect=lambda *a, **kw: verdict(
                      ask="是不是要在每周二 14:00 提醒你化学前沿课前准备？")):
    ids2 = reminder.maybe_capture(make_cfg(), q2, CHAT, [msg2], ACCOUNT, images=IMGS)
rec = clarify_of(q2)
check("只记了待确认（不当成已建提醒返回）", ids2 == [] and not q2.list_reminders(account=ACCOUNT),
      str(ids2))
check("待确认里有提案规则", rec and rec["rule"] == "weekly:2", str(rec and rec["rule"]))
check("待确认里有提案时间",
      rec and int(rec["proposed_ts"] or 0) == ts_of(2, 13, 50),
      str(rec and rec["proposed_ts"]))
check("待确认里留了图", rec and json.loads(rec["images"] or "[]") == IMGS,
      str(rec and rec["images"]))
check("核对文字从实际时间生成", "每周二 13:50" in rec["ask"] and "14:00" not in rec["ask"],
      rec["ask"])
check("核对未送达前不能用「对」建单",
      reminder.maybe_answer_clarify(make_cfg(), q2, CHAT, "对", ACCOUNT) is None)

sender = reply_worker.Worker.__new__(reply_worker.Worker)
sender.q, sender.acc = q2, ACCOUNT
sender.cfg = make_cfg()
sender.cfg["agent"]["auto_reply"] = False
sender.brain = SimpleNamespace(mem=support.make_memory("confirm_send.db"))
sender.db = SimpleNamespace(get_messages=lambda *_a, **_kw: [])
with patch.object(reply_worker, "post", return_value={
        "ok": True, "verified": True, "verified_seq": 101}) as post:
    sent = sender.send_task(q2.next_outbound(account=ACCOUNT), sleep_delay=False, reload_cfg=False)
check("只开启定时提醒也会发送核对", sent["status"] == "sent" and post.call_count == 1, str(sent))
check("实际核对的是数据库提案", post.call_args.args[1]["text"] == rec["ask"], str(post.call_args))

section("回一句「对」：按提案建单")
check("不能用发送前同秒的消息确认", reminder.maybe_answer_clarify(
    make_cfg(), q2, CHAT, "对", ACCOUNT, msg_seq=100) is None)
rid = reminder.maybe_answer_clarify(make_cfg(), q2, CHAT, "对", ACCOUNT, msg_seq=102)
row = q2.get_reminder(int(rid)) if rid else None
check("建了单", bool(row), str(rid))
check("规则照提案", row and row["rule"] == "weekly:2", str(row and row["rule"]))
check("时间照提案", row and int(row["next_ts"]) == ts_of(2, 13, 50),
      str(row and row["next_ts"]))
check("待确认已结案", q2.get_clarify(CHAT, "pending", account=ACCOUNT) is None
      and q2.get_clarify(CHAT, "asked", account=ACCOUNT) is None, "")

section("同一会话里较新的代传核对不会一并确认旧提醒")
cross_queue = support.make_queue("rv_cross_prompt.db")
cross_queue.add_clarify(ACCOUNT, CHAT, "准备例会", NOW - 120, "确认这个提醒吗？",
                        rule="single", proposed_ts=NOW + 3600)
earlier_prompt = reminder.enqueue_clarify_prompt(cross_queue, ACCOUNT, CHAT)
cross_queue.next_outbound(kinds=("reminder_clarify",), account=ACCOUNT)
cross_queue.bump_segments(earlier_prompt, 1, complete=True, sent_after_seq=201)
other_clarify = cross_queue.add_relay_clarify(
    ACCOUNT, CHAT, "晚上好", ["同事"], "你要发给同事吗？", confirm_chat="wxid_other")
later_prompt = cross_queue.enqueue_relay_clarify_prompt(ACCOUNT, CHAT, "你要发给同事吗？")
cross_queue.next_outbound(kinds=("relay_clarify",), account=ACCOUNT)
cross_queue.bump_segments(later_prompt, 1, complete=True, sent_after_seq=203)
cross_queue.set_relay_clarify(other_clarify, "done")
same_second = int(time.time())
with cross_queue.tx() as conn:
    conn.execute("UPDATE outbound SET sent_ts=? WHERE id IN (?,?)",
                 (same_second, earlier_prompt, later_prompt))
check("确认较新代传任务后，该条简短答复不能再建旧提醒",
      reminder.maybe_answer_clarify(make_cfg(), cross_queue, CHAT, "对", ACCOUNT,
                                    msg_ts=same_second, msg_seq=204) is None
      and not cross_queue.list_reminders(account=ACCOUNT))
check("同秒缺少入站序号时保守地拒绝简短确认",
      reminder.maybe_answer_clarify(make_cfg(), cross_queue, CHAT, "对", ACCOUNT,
                                    msg_ts=same_second) is None)
check("早于较新的问句的入站仍能确认此前已送达的提醒",
      reminder.maybe_answer_clarify(make_cfg(), cross_queue, CHAT, "对", ACCOUNT,
                                    msg_ts=same_second, msg_seq=202) is not None)

section("回一句「不对，周三 13:50」：按新说法重建")
q3 = support.make_queue("rv3.db")
msg3 = inbox(q3, "每周三下午一点五十提醒我化学前沿")
with patch.object(reminder, "judge_with_llm",
                  side_effect=lambda *a, **kw: verdict(
                      ask="是不是要在每周二 13:50 提醒你化学前沿课前准备？")):
    reminder.maybe_capture(make_cfg(), q3, CHAT, [msg3], ACCOUNT, images=IMGS)
send_prompt(q3)
rid3 = reminder.maybe_answer_clarify(make_cfg(), q3, CHAT, "不对，周三 13:50", ACCOUNT)
row3 = q3.get_reminder(int(rid3)) if rid3 else None
check("按纠正建了单", bool(row3), str(rid3))
check("规则改成每周三", row3 and row3["rule"] == "weekly:3", str(row3 and row3["rule"]))
check("钟点按纠正的 13:50",
      row3 and datetime.fromtimestamp(int(row3["next_ts"])).strftime("%H:%M") == "13:50",
      str(row3 and row3["next_ts"]))

section("等确认期间又来一单新委托：不能套旧内容建单")
q10 = support.make_queue("rv10.db")
msg10 = inbox(q10, "以后每次化学前沿课前十分钟都记得提醒我一下")
with patch.object(reminder, "judge_with_llm",
                  side_effect=lambda *a, **kw: verdict(
                      ask="是不是要在每周二 13:50 提醒你化学前沿课前准备？")):
    reminder.maybe_capture(make_cfg(), q10, CHAT, [msg10], ACCOUNT, images=IMGS)
send_prompt(q10)
rid10 = reminder.maybe_answer_clarify(make_cfg(), q10, CHAT,
                                      "明天八点提醒我拿快递", ACCOUNT)
check("不当成澄清答案（不建单）", rid10 is None and not q10.list_reminders(account=ACCOUNT),
      str(rid10))
check("待确认还挂着，等他真回答", clarify_of(q10) is not None, str(clarify_of(q10)))
section("老是答非所问：问满次数就放弃")
q4 = support.make_queue("rv4.db")
msg4 = inbox(q4, "以后每次化学前沿课前十分钟都记得提醒我一下")
with patch.object(reminder, "judge_with_llm",
                  side_effect=lambda *a, **kw: verdict(
                      ask="是不是要在每周二 13:50 提醒你化学前沿课前准备？")):
    reminder.maybe_capture(make_cfg(), q4, CHAT, [msg4], ACCOUNT, images=IMGS)
send_prompt(q4)
for i, junk in enumerate(("今天好热", "在干嘛", "哈哈哈"), 1):
    rid4 = reminder.maybe_answer_clarify(make_cfg(), q4, CHAT, junk, ACCOUNT)
    rec4 = clarify_of(q4)
    queued4 = reminder.enqueue_clarify_prompt(q4, ACCOUNT, CHAT)
    if i < 3:
        check("第 %d 次答非所问后还会再问" % i, bool(queued4), str(i))
        send_prompt(q4)
    else:
        check("问满 3 次就不再问了", queued4 is None, str(queued4))
    check("第 %d 次答非所问不建单" % i, rid4 is None and not q4.list_reminders(account=ACCOUNT),
          str(rid4))
check("问满 3 次后结案",
      clarify_of(q4) is None, str(clarify_of(q4)))

section("没图的老路径：直接建单，不问")
q5 = support.make_queue("rv5.db")
msg5 = inbox(q5, "明天早上八点提醒我交材料")
with patch.object(reminder, "judge_with_llm",
                  side_effect=lambda *a, **kw: verdict(rule="single",
                                                       next_ts=NOW + 3600,
                                                       content="交材料")):
    ids5 = reminder.maybe_capture(make_cfg(), q5, CHAT, [msg5], ACCOUNT)
check("直接建单", len(ids5) == 1 and bool(q5.get_reminder(int(ids5[0]))), str(ids5))
check("没有待确认", q5.get_clarify(CHAT, "pending", account=ACCOUNT) is None, "")

# ---------------------------------------------------------------- 候选队列
section("模型不可用：候选连图一起排队，恢复后判定也带图")
q6 = support.make_queue("rv6.db")
msg6 = inbox(q6, "以后每次化学前沿课前十分钟都记得提醒我一下")


def boom(*a, **kw):
    raise RuntimeError("模型挂了")


with patch.object(reminder, "judge_with_llm", side_effect=boom):
    reminder.maybe_capture(make_cfg(), q6, CHAT, [msg6], ACCOUNT, images=IMGS)
cands = q6.pending_candidates(5, account=ACCOUNT)
check("进了候选队列", len(cands) == 1, str(len(cands)))
check("候选里存了图", cands and json.loads(cands[0]["images"] or "[]") == IMGS,
      str(cands and cands[0]["images"]))
seen6 = {}


def judge6(cfg, text, base_ts, tz=None, tz_zone=None, images=None):
    seen6["images"] = images
    return verdict()


with patch.object(reminder, "judge_with_llm", side_effect=boom):
    reminder.process_candidates(q6, make_cfg())
check("模型仍未恢复时不会立即重试", not q6.pending_candidates(5, account=ACCOUNT))
with (patch.object(reminder.time, "time", return_value=time.time() + 6),
      patch.object(reminder, "judge_with_llm", side_effect=judge6)):
    reminder.process_candidates(q6, make_cfg())
check("候选判定时也带了图", seen6.get("images") == IMGS, str(seen6.get("images")))
check("候选已结案（转成待确认）", not q6.pending_candidates(5, account=ACCOUNT), "")
check("候选核对主动进入发送队列", send_prompt(q6)["text"] == q6.get_clarify(
    CHAT, "asked", account=ACCOUNT)["ask"])

section("入站处理与候选重试不会各自建同一条提醒")
duplicated = support.make_queue("rv_duplicate.db")
same = inbox(duplicated, "明天八点提醒我交材料")
with patch.object(reminder, "judge_with_llm", return_value=None):
    reminder.maybe_capture(make_cfg(), duplicated, CHAT, [same], ACCOUNT)
with patch.object(reminder, "judge_with_llm", return_value=verdict(rule="single")):
    reminder.maybe_capture(make_cfg(), duplicated, CHAT, [same], ACCOUNT)
    reminder.process_candidates(duplicated, make_cfg())
check("只保留一次建单", len(duplicated.list_reminders(account=ACCOUNT)) == 1)

section("没图又不知道时间：回「对」不能把瞎猜的时间坐实")
q8 = support.make_queue("rv8.db")
msg8 = inbox(q8, "以后每次化学前沿课前十分钟都记得提醒我一下")
with patch.object(reminder, "judge_with_llm",
                  side_effect=lambda *a, **kw: verdict(
                      need_time=True, next_ts=ts_of(1, 7, 50), rule="weekly:1",
                      ask="化学前沿课是周几几点上？")):
    ids8 = reminder.maybe_capture(make_cfg(), q8, CHAT, [msg8], ACCOUNT)
rec8 = clarify_of(q8)
check("只问不建", ids8 == [] and not q8.list_reminders(account=ACCOUNT), str(ids8))
check("提案是空的（没有可坐实的时间）",
      rec8 and not rec8["proposed_ts"] and not (rec8["rule"] or ""), str(rec8 and
                                                                      (rec8["rule"], rec8["proposed_ts"])))
rid8 = reminder.maybe_answer_clarify(make_cfg(), q8, CHAT, "对", ACCOUNT)
check("回「对」不会凭空建单",
      rid8 is None and not q8.list_reminders(account=ACCOUNT), str(rid8))
rid8b = reminder.maybe_answer_clarify(make_cfg(), q8, CHAT, "每周三 13:50", ACCOUNT)
row8 = q8.get_reminder(int(rid8b)) if rid8b else None
check("补上时间才建单", bool(row8) and row8["rule"] == "weekly:3",
      str(row8 and (row8["rule"], row8["next_ts"])))

section("澄清保留原委托的周期，也接受新的准确日期")
for i, (original, answer, wanted) in enumerate((
        ("每天提醒我拉伸", "上午八点", "daily"),
        ("明天提醒我交材料", "后天下午三点", "single"),
        ("后天提醒我交材料", "下午三点", "single"))):
    clarified = support.make_queue("rv_dates_%s.db" % i)
    message = inbox(clarified, original)
    with patch.object(reminder, "judge_with_llm", return_value=verdict(
            need_time=True, rule=wanted, next_ts=None)):
        reminder.maybe_capture(make_cfg(), clarified, CHAT, [message], ACCOUNT)
    rid = reminder.maybe_answer_clarify(make_cfg(), clarified, CHAT, answer, ACCOUNT)
    row = clarified.get_reminder(rid) if rid else None
    check("保留或改为正确周期", row and row["rule"] == wanted, str(row))
    if wanted == "single":
        dt = datetime.fromtimestamp(row["next_ts"]) if row else None
        wanted_date = (datetime.fromtimestamp(NOW) + timedelta(days=2)).date()
        check("约定时间是后天 15:00", dt and dt.date() == wanted_date and dt.hour == 15, str(dt))

section("已撤销的核对不能因随后回复而建单")
cancelled = support.make_queue("rv_cancelled.db")
with patch.object(reminder, "judge_with_llm", return_value=verdict(used_images=True)):
    reminder.maybe_capture(make_cfg(), cancelled, CHAT, [inbox(cancelled, "提醒我上课")], ACCOUNT)
send_prompt(cancelled)
check("可以撤销待确认", reminder.maybe_cancel(cancelled, CHAT, "不用提醒了", account=ACCOUNT) == (True, 1))
check("撤销后回复「好」不会建提醒", reminder.maybe_answer_clarify(
    make_cfg(), cancelled, CHAT, "好", ACCOUNT) is None and not cancelled.list_reminders(account=ACCOUNT))

section("模型不支持图片时用文字判断，图片中的未知时间要求澄清")
fallback = verdict()
fallback["next"] = datetime.fromtimestamp(ts_of(2, 13, 50)).strftime("%Y-%m-%d %H:%M")
with patch.object(llm, "chat", side_effect=[
        {"ok": False, "status": 400}, {"ok": True, "text": json.dumps(fallback)}]) as model:
    read = reminder.judge_with_llm(make_cfg(), "按照课表提醒我", NOW, images=IMGS)
check("先发图后退回文本", isinstance(model.call_args_list[0].args[0][0]["content"], list)
      and isinstance(model.call_args_list[1].args[0][0]["content"], str))
check("没有读到图就保留缺时间状态", read and not read["used_images"] and read["need_time"])

section("精确秒数与联系人的未来夏令时")
short = verdict(rule="single")
short["next"] = datetime.fromtimestamp(NOW + 20).strftime("%Y-%m-%d %H:%M:%S")
with patch.object(llm, "chat", return_value={"ok": True, "text": json.dumps(short)}) as model:
    read = reminder.judge_with_llm(make_cfg(), "20秒后提醒我喝水", NOW)
check("模型拿到带秒的消息时间", datetime.fromtimestamp(NOW).strftime("%H:%M:%S")
      in model.call_args.args[0][0]["content"])
check("短倒计时不损失秒数", read and read["next_ts"] == NOW + 20 and read["short_delay"], str(read))

london = ZoneInfo("Europe/London")
winter = int(datetime(2026, 11, 22, 12).timestamp())
summer = datetime(2027, 4, 23, 14, tzinfo=london)
future = verdict(rule="single", next="2027-04-23 14:00", tz="")
with (patch.object(reminder.time, "time", return_value=winter),
      patch.object(llm, "chat", return_value={"ok": True, "text": json.dumps(future)})):
    london_read = reminder.judge_with_llm(make_cfg(), "4月23日14点提醒我", winter,
                                          tz=-8 * 3600, tz_zone=london.key)
check("模型不重复给时区也按夏令时换算", london_read and london_read["next_ts"]
      == int(summer.timestamp()), str(london_read))

section("确认关卡可以关掉")
cfg_off = make_cfg(confirm_with_images=False)
q7 = support.make_queue("rv7.db")
msg7 = inbox(q7, "以后每次化学前沿课前十分钟都记得提醒我一下")
with patch.object(reminder, "judge_with_llm", side_effect=lambda *a, **kw: verdict()):
    ids7 = reminder.maybe_capture(cfg_off, q7, CHAT, [msg7], ACCOUNT, images=IMGS)
check("关掉核对就直接建单",
      len(ids7) == 1 and bool(q7.get_reminder(int(ids7[0]))), str(ids7))

section("待确认的编号不会冒充别人的提醒")
q9 = support.make_queue("rv9.db")
other = "wxid_other"
# 别的会话先占住提醒 #1
rid9 = q9.add_reminder(ACCOUNT, other, "买咖啡", "single", NOW + 600, anchor_ts=NOW + 600)
cid9 = q9.add_clarify(ACCOUNT, CHAT, "化学前沿课前准备", NOW, "是不是每周三 13:50？",
                      rule="weekly:3", proposed_ts=NOW + 3600)
check("两张表各自从 1 开始（编号会撞）", rid9 == cid9, "%s / %s" % (rid9, cid9))
check("不会把别人的提醒说成「你已经记下了」",
      reminder.accepted_hint(q9, [cid9], ACCOUNT, CHAT) == "",
      reminder.accepted_hint(q9, [cid9], ACCOUNT, CHAT))
check("本会话的提醒照常提示",
      "买咖啡" in reminder.accepted_hint(q9, [rid9], ACCOUNT, other), "")
section("排期说法")
check("每周二 13:50",
      reminder.describe_schedule("weekly:2", ts_of(2, 13, 50)) == "每周二 13:50",
      reminder.describe_schedule("weekly:2", ts_of(2, 13, 50)))
check("每天 07:30",
      reminder.describe_schedule("daily", NOW) is not None, "")

raise SystemExit(support.finish())
