# -*- coding: utf-8 -*-
"""一次性星期词 + 周期时区推进。

两条实测踩坑（2026-11-21 审查）：
* 周六说「下周一早上八点提醒我交材料」，排到了周日——星期词根本没被解析，
  退化成「下一个八点」。
* 对方比主机快两小时时，「每周一凌晨一点」第一次是周一，第二次滑到周二——
  周期推进拿主机日期的星期去匹配。
"""
import sys
from datetime import datetime

import support
from support import check, section

import reminder_parse

SAT = int(datetime(2026, 11, 21, 15, 0).timestamp())      # 周六 15:00


def at(ts):
    return datetime.fromtimestamp(ts)


section("一次性星期词：下周一不是周日")
r = reminder_parse.detect("下周一早上八点提醒我交材料", SAT, SAT)
check("解析出来了", r is not None, str(r))
check("落在下周一 08:00", r and at(r["next_ts"]) == datetime(2026, 11, 23, 8, 0),
      at(r["next_ts"]).strftime("%m-%d %H:%M") if r else "")
check("内容只剩「交材料」", r and r["content"] == "交材料", str(r))

r = reminder_parse.detect("周一早上八点提醒我开会", SAT, SAT)
check("不写「下」也指未来最近的周一", r and at(r["next_ts"]) == datetime(2026, 11, 23, 8, 0),
      at(r["next_ts"]).strftime("%m-%d %H:%M") if r else "")

r = reminder_parse.detect("这周三下午三点提醒我体检", SAT, SAT)
check("这周三 = 本周三", r and at(r["next_ts"]) == datetime(2026, 11, 25, 15, 0),
      at(r["next_ts"]).strftime("%m-%d %H:%M") if r else "")

r = reminder_parse.detect("星期天晚上八点提醒我值日", SAT, SAT)
check("星期天也能认", r and at(r["next_ts"]) == datetime(2026, 11, 22, 20, 0),
      at(r["next_ts"]).strftime("%m-%d %H:%M") if r else "")

# 周一十点说「周一八点」——今天那个钟点已经过了，指下周一
MON_10 = int(datetime(2026, 11, 23, 10, 0).timestamp())
r = reminder_parse.detect("周一早上八点提醒我打卡", MON_10, MON_10)
check("钟点已过顺延一周", r and at(r["next_ts"]) == datetime(2026, 11, 30, 8, 0),
      at(r["next_ts"]).strftime("%m-%d %H:%M") if r else "")

r = reminder_parse.detect("下周一提醒我交材料", SAT, SAT)
check("只给星期不给钟点 → 要澄清", r and r.get("need_clarify") is True, str(r))

r = reminder_parse.detect("上周一早上八点提醒我补交", SAT, SAT)
check("「上周一」是过去，不排提醒", r is None or at(r["next_ts"]) > datetime(2026, 11, 21),
      str(r))

section("正文里的星期词不是触发日期")
r = reminder_parse.detect("晚上八点提醒我准备下周一的面试", SAT, SAT)
check("按「晚上八点」排今天，不是下周一", r and
      at(r["next_ts"]) == datetime(2026, 11, 21, 20, 0),
      at(r["next_ts"]).strftime("%m-%d %H:%M") if r else "")
check("正文保留「下周一的面试」",
      r and "下周一的面试" in r["content"], r and r["content"])

r = reminder_parse.detect("上周一早上八点提醒我补交", SAT, SAT)
check("「上周一」这种过期时间词不按八点乱排", r is None, str(r))

section("周期推进：按收件人当地星期")
TZ = 7200                       # 对方比主机快两小时
# 对方本地周一 01:00 = 主机周日 23:00
first = int(datetime(2026, 11, 23, 1, 0).timestamp()) - TZ
nxt = reminder_parse.advance("weekly:1", first, first, TZ)
check("第二次仍是对方周一 01:00", nxt == first + 7 * 86400,
      at(nxt + TZ).strftime("%m-%d %H:%M") if nxt else "None")
check("按本地看是周一", nxt and at(nxt + TZ).isoweekday() == 1)

a = int(datetime(2026, 11, 23, 8, 0).timestamp())
check("每日提醒不受时区影响", reminder_parse.advance("daily", a, a, TZ) == a + 86400)
check("每 N 天不受时区影响",
      reminder_parse.advance("interval:3", a, a, TZ) == a + 3 * 86400)

# 回归：不带时差时行为不变
check("默认 tz=0 与旧行为一致",
      reminder_parse.advance("weekly:1", a, a) == a + 7 * 86400)

section("时差下的「每周一凌晨一点」首次也落在周一")
r = reminder_parse.detect("每周一凌晨一点提醒我睡觉", SAT, SAT, tz=TZ)
check("首次是对方周一 01:00", r is not None and
      at(r["next_ts"] + TZ).isoweekday() == 1 and at(r["next_ts"] + TZ).hour == 1,
      at(r["next_ts"] + TZ).strftime("%m-%d %H:%M") if r else "")
if r:
    n2 = reminder_parse.advance(r["rule"], r["anchor_ts"], r["next_ts"], TZ)
    check("第二次还是周一 01:00", n2 and at(n2 + TZ).isoweekday() == 1
          and at(n2 + TZ).hour == 1,
          at(n2 + TZ).strftime("%m-%d %H:%M") if n2 else "None")

section("跨夏令时：按目标那一刻的偏移换算")
tz_winter = reminder_parse.tz_from_zone("Europe/London")
tz_summer = reminder_parse.tz_from_zone(
    "Europe/London", int(datetime(2027, 7, 1).timestamp()))
check("伦敦冬令时 -8 小时", tz_winter == -8 * 3600, str(tz_winter))
check("伦敦夏令时 -7 小时", tz_summer == -7 * 3600, str(tz_summer))
# 伦敦每周一 08:00：2027-03-22 还是冬令时，下一次 03-29 已经转夏令时
local_a = datetime(2027, 3, 22, 8, 0)
anchor = int(local_a.timestamp()) - tz_winter          # 换算成主机时间戳
nxt = reminder_parse.advance("weekly:1", anchor, anchor, tz_winter, "Europe/London")
expect = int(local_a.timestamp() + 7 * 86400) - tz_summer
check("跨夏令时按目标时刻算（差一小时也能对上）", nxt == expect,
      "%s vs %s" % (nxt, expect))

section("夏令时切换当天：相对时间按真实秒数算")
# 伦敦 2027-03-28 00:30（切换发生在 01:00 UTC），90 分钟后应是真实 90 分钟
local_mid = datetime(2027, 3, 28, 0, 30)
base_mid = int(local_mid.timestamp()) + 8 * 3600          # 主机（冬令时 -8h）
r = reminder_parse.detect("90分钟后提醒我喝水", base_mid, base_mid,
                          tz=-8 * 3600, tz_zone="Europe/London")
check("等的是 90 分钟，不是 30 分钟",
      r and r["next_ts"] == base_mid + 90 * 60,
      str(r and (r["next_ts"] - base_mid) // 60))

section("每日提醒跨夏令时仍停在当地 09:00")
a = int(datetime(2027, 3, 27, 9, 0).timestamp()) + 8 * 3600
n = reminder_parse.advance("daily", a, a, -8 * 3600, "Europe/London")
check("切换后第一次仍是当地 09:00",
      datetime.fromtimestamp(n + (reminder_parse.tz_from_zone("Europe/London", n) or 0)).hour == 9,
      str(datetime.fromtimestamp(n + (reminder_parse.tz_from_zone("Europe/London", n) or 0))))
# 再推进一次时，联系人时差已经变成夏令时 -7h（chat_tz 会被更新）
n2 = reminder_parse.advance("daily", a, n, -7 * 3600, "Europe/London")
check("第二次也不漂到 10:00",
      datetime.fromtimestamp(n2 + (reminder_parse.tz_from_zone("Europe/London", n2) or 0)).hour == 9,
      str(datetime.fromtimestamp(n2 + (reminder_parse.tz_from_zone("Europe/London", n2) or 0))))

sys.exit(support.finish())
