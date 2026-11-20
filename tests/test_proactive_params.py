# -*- coding: utf-8 -*-
"""主动聊天的人级参数：全局+覆盖合并、提交校验、门禁时段、配置校验。"""
import time

import support
from support import check, section

import config as C
import gate
import proactive


def _cfg(**contacts):
    cfg = support.base_cfg()
    cfg["proactive"] = {
        "enabled": True, "whitelist": list(contacts),
        "per_chat_daily": 3, "idle_min_minutes": 120, "idle_max_minutes": 360,
        "quiet_minutes_after_user": 20, "min_gap_after_self": 90,
        "active_hours": [9, 23],
    }
    cfg["contacts"] = {chat: dict(node) for chat, node in contacts.items()}
    return cfg


section("params：没设过的项跟随全局")
cfg = _cfg(wxid_a={"proactive": True})
own = proactive.params(cfg, "wxid_a")
check("每天上限取全局", own["per_chat_daily"] == 3, own)
check("间隔取全局", (own["idle_min_minutes"], own["idle_max_minutes"]) == (120, 360), own)
check("时段取全局", own["active_hours"] == [9, 23], own)
check("没配置的联系人也给全局值", proactive.params(cfg, "wxid_unknown")["per_chat_daily"] == 3)
check("连全局段都没有就返回空表（调用方各自带默认）",
      proactive.params({}, "wxid_a") == {})

section("params：人级覆盖只影响自己")
cfg = _cfg(wxid_a={"proactive": True, "proactive_daily": 6,
                   "proactive_idle_min": 30, "proactive_idle_max": 90,
                   "proactive_quiet_minutes": 5, "proactive_gap_after_self": 10,
                   "proactive_hours": [10, 12]},
           wxid_b={"proactive": True})
a = proactive.params(cfg, "wxid_a")
b = proactive.params(cfg, "wxid_b")
check("上限覆盖", a["per_chat_daily"] == 6, a)
check("间隔覆盖", (a["idle_min_minutes"], a["idle_max_minutes"]) == (30, 90), a)
check("静默覆盖", (a["quiet_minutes_after_user"], a["min_gap_after_self"]) == (5, 10), a)
check("时段覆盖", a["active_hours"] == [10, 12], a)
check("别人不受影响", (b["per_chat_daily"], b["active_hours"]) == (3, [9, 23]), b)

section("overrides：只回显显式设过的项")
check("覆盖项齐全", proactive.overrides(cfg, "wxid_a") == {
    "per_chat_daily": 6, "idle_min_minutes": 30, "idle_max_minutes": 90,
    "quiet_minutes_after_user": 5, "min_gap_after_self": 10,
    "active_hours": [10, 12]}, proactive.overrides(cfg, "wxid_a"))
check("没设过就是空", proactive.overrides(cfg, "wxid_b") == {}, proactive.overrides(cfg, "wxid_b"))

section("normalize_patch：提交值收敛与校验")
values, error = proactive.normalize_patch({"per_chat_daily": "5", "idle_min_minutes": 45.0,
                                           "proactive_unknown": 1})
check("字符串/浮点转整数", not error and values == {"per_chat_daily": 5,
                                                    "idle_min_minutes": 45}, (values, error))
values, error = proactive.normalize_patch({"per_chat_daily": "", "idle_max_minutes": None})
check("空串/None = 恢复跟随全局", not error
      and values == {"per_chat_daily": None, "idle_max_minutes": None}, (values, error))
values, error = proactive.normalize_patch({"per_chat_daily": 99})
check("越界被拒", bool(error) and "每天上限" in error, error)
values, error = proactive.normalize_patch({"idle_min_minutes": "abc"})
check("非数字被拒", bool(error), error)
values, error = proactive.normalize_patch({"active_hours": [9, 23]})
check("时段正常", not error and values.get("active_hours") == [9, 23], (values, error))
values, error = proactive.normalize_patch({"active_hours": [23]})
check("时段缺一半被拒", bool(error), error)
values, error = proactive.normalize_patch({"active_hours": [25, 3]})
check("时段越界被拒", bool(error), error)
values, error = proactive.normalize_patch({"active_hours": []})
check("清空时段", not error and values.get("active_hours") is None, (values, error))
values, error = proactive.normalize_patch(None)
check("非对象被拒", bool(error), error)

section("apply_patch：写进 contacts 节点的字段名")
node = proactive.apply_patch({"proactive": True, "relation": "哥哥"},
                             {"per_chat_daily": 2, "idle_min_minutes": 60,
                              "active_hours": [8, 20]})
check("人级键名正确", node == {"proactive": True, "relation": "哥哥",
                               "proactive_daily": 2, "proactive_idle_min": 60,
                               "proactive_hours": [8, 20]}, node)
check("原有字段保留", node.get("proactive") is True and node.get("relation") == "哥哥")
restored = proactive.apply_patch(node, {"per_chat_daily": None, "active_hours": None})
check("传 None 恢复跟随全局",
      "proactive_daily" not in restored and "proactive_hours" not in restored, restored)
check("不改原对象", "proactive_daily" in node)

section("maybe_open：配额真的按人级上限走")


class FakeQueue:
    """只实现 maybe_open 用到的那几个方法。"""

    def __init__(self):
        self.state = {"next_at": 0, "quiet_until": 0, "cooldown_until": 0,
                      "await_reply_until": 0, "no_reply_streak": 0, "muted": 0,
                      "activity_version": 0, "last_self_ts": 0, "day_n": 0,
                      "day": proactive.today()}
        self.claimed = []
        self.updated = []

    def ensure_proactive_state(self, account, chat, trial):
        return dict(self.state)

    def get_proactive_state(self, account, chat):
        return dict(self.state)

    def update_proactive_state(self, account, chat, **fields):
        self.updated.append(fields)
        self.state.update(fields)

    def is_paused(self, chat):
        return False

    def last_inbound_ts(self, chat, account=None):
        return 0

    def newest_pending(self, chat, limit=1, account=None):
        return []

    def list_outbound(self, status, limit, account=None):
        return []

    def pick_hook(self, chat, now, account=None):
        return None

    def claim_proactive_slot(self, account, chat, day, per_day, next_at):
        self.claimed.append((day, per_day, int(next_at)))
        return True

    def create_outbound(self, account, chat, text, **kw):
        return 7

    def mark_hook(self, *a, **kw):
        pass


class FakeBrain:
    def __init__(self):
        self.calls = 0

    def compose_proactive(self, chat, seed="", kind="idle"):
        self.calls += 1
        return {"ok": True, "reply": "忙完了吗"}

    def sensitive_label(self, reply):
        return None


cfg = _cfg(wxid_a={"proactive": True, "proactive_daily": 6,
                   "proactive_idle_min": 30, "proactive_idle_max": 90,
                   "proactive_hours": [0, 24]})
q = FakeQueue()
now = time.time()
opened = proactive.maybe_open(q, cfg, FakeBrain(), cfg["account"], "wxid_a", now)
check("人级参数下能开口", opened)
check("配额用该联系人的上限", q.claimed and q.claimed[0][1] == 6, q.claimed)
check("next_at 按人级间隔排", q.claimed
      and now + 30 * 60 - 2 <= q.claimed[0][2] <= now + 90 * 60 + 2, q.claimed)

q = FakeQueue()
quota_time = time.mktime((2026, 1, 15, 12, 0, 0, 0, 0, -1))
q.state.update(day=proactive.today(quota_time), day_n=6)
brain = FakeBrain()
for elapsed in (0, 15, 30):
    proactive.maybe_open(q, cfg, brain, cfg["account"], "wxid_a", quota_time + elapsed)
check("当日达到个人上限后，多次轮询也不会调用模型", brain.calls == 0 and not q.claimed)
check("次日恢复生成", proactive.maybe_open(q, cfg, brain, cfg["account"], "wxid_a", quota_time + 86400)
      and brain.calls == 1)

cfg = _cfg(wxid_a={"proactive": True, "proactive_daily": 0,
                   "proactive_idle_min": 30, "proactive_idle_max": 30,
                   "proactive_hours": [0, 24]})
q = FakeQueue()
q.state.update(day=proactive.today(quota_time), day_n=30)
check("0 = 不限也照传", proactive.maybe_open(q, cfg, FakeBrain(), cfg["account"],
                                             "wxid_a", quota_time)
      and q.claimed[0][1] == 0, q.claimed)

q = support.make_queue("proactive-quota.db")
acct, chat = cfg["account"], "wxid_a"
q.ensure_proactive_state(acct, chat)
day = proactive.today(quota_time)
tomorrow = proactive.today(quota_time + 86400)
check("SQLite 保留第一份占额并拒绝同一天超过上限的申请",
      q.claim_proactive_slot(acct, chat, day, 1, 100)
      and not q.claim_proactive_slot(acct, chat, day, 1, 200)
      and q.get_proactive_state(acct, chat)["next_at"] == 100)
check("SQLite 次日从 1 开始计数",
      q.claim_proactive_slot(acct, chat, tomorrow, 1, 300)
      and q.get_proactive_state(acct, chat)["day_n"] == 1)

section("plan_next：按人级间隔排下一次")
cfg = _cfg(wxid_a={"proactive": True, "proactive_idle_min": 30,
                   "proactive_idle_max": 90, "proactive_hours": [0, 24]})
now = time.time()
ts = proactive.plan_next(now, proactive.params(cfg, "wxid_a"))
check("落在 30~90 分钟内", now + 30 * 60 - 2 <= ts <= now + 90 * 60 + 2, ts - now)

section("gate：人级主动时段覆盖全局")
night = time.mktime((2026, 1, 15, 3, 0, 0, 0, 0, -1))     # 凌晨 3 点
cfg = _cfg(wxid_a={"proactive": True})
check("全局时段外 → 不开口",
      gate.evaluate_proactive(cfg, "wxid_a", now=night)["action"] == "cancel")
cfg = _cfg(wxid_a={"proactive": True, "proactive_hours": [0, 24]})
check("人级全时段 → 通过",
      gate.evaluate_proactive(cfg, "wxid_a", now=night)["action"] == "ok")
cfg = _cfg(wxid_a={"proactive": True, "proactive_hours": [9, 12]})
verdict = gate.evaluate_proactive(cfg, "wxid_a", now=night)
check("人级时段外 → 不开口", verdict["action"] == "cancel"
      and "主动时段" in verdict["reason"], verdict)

section("config._validate：人级参数")
cfg = support.base_cfg(contacts={"wxid_a": {"proactive": True, "proactive_daily": 5,
                                           "proactive_idle_min": 30,
                                           "proactive_idle_max": 90,
                                           "proactive_hours": [9, 23]}})
check("合法人级参数通过", not C._validate(cfg), C._validate(cfg))
for key, bad in (("proactive_daily", -1), ("proactive_idle_min", "x"),
                 ("proactive_quiet_minutes", -5)):
    cfg = support.base_cfg(contacts={"wxid_a": {key: bad}})
    check("%s=%r 被拒" % (key, bad), any(key in p for p in C._validate(cfg)),
          C._validate(cfg))
cfg = support.base_cfg(contacts={"wxid_a": {"proactive_hours": [25, 3]}})
check("时段越界被拒", any("proactive_hours" in p for p in C._validate(cfg)))

raise SystemExit(support.finish())
