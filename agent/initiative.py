# -*- coding: utf-8 -*-
"""主动行为调度线程。

为什么单独一个线程
------------------
* 提醒要准点：「20 秒后喝水」不能被回复生成的耗时拖住
* 每秒 tick 一次，和 ``reply_worker`` 的 0.5 秒采集循环互不干扰
* 发送仍走现有 sender（串行、有门禁、有回读核验）

P2 只管定时提醒；P3 的主动闲聊也挂在这里。
"""

from __future__ import annotations

import threading
import time

import relay
import reminder
import proactive
from logutil import get_logger

log = get_logger()
_stop = threading.Event()

SYNC_EVERY = 30.0          # 提醒发送结果回写间隔（秒）
CAND_EVERY = 3.0           # 候选判定间隔（秒）
PRO_EVERY = 15.0           # 主动开口检查间隔（秒）
BACK_EVERY = 10.0          # 代传话回传汇总间隔（秒）
TICK = 1.0                 # 到点检查间隔：秒级提醒要靠它

_cand_busy = threading.Event()   # 同一时间只跑一个判定线程，避免并发烧额度
_pro_busy = threading.Event()
_back_busy = threading.Event()


def stop():
    _stop.set()


def tick(q, cfg, now: float | None = None) -> int:
    """跑一轮调度，返回本次排期的提醒条数。测试可直接调。"""
    return len(reminder.fire_due(q, cfg, now=now))


def _run_relay_back(q, cfg):
    try:
        relay.back_tick(q, cfg)
    except Exception as e:
        log.error("代传话回传异常：%s: %s" % (type(e).__name__, e))
    finally:
        _back_busy.clear()


def _run_candidates(q, cfg):
    try:
        reminder.process_candidates(q, cfg, limit=2)
    except Exception as e:
        log.error("候选判定异常：%s: %s" % (type(e).__name__, e))
    finally:
        _cand_busy.clear()


def _run_proactive(q, cfg, brain):
    try:
        proactive.tick(q, cfg, brain)
    except Exception as e:
        log.error("主动开口气异常：%s: %s" % (type(e).__name__, e))
    finally:
        _pro_busy.clear()


def run(q, load_cfg, brain=None):
    """调度线程主体。``load_cfg`` 传 reply_worker.load_cfg，保持热更新。"""
    last_sync = 0.0
    last_cand = 0.0
    last_pro = 0.0
    last_back = 0.0
    log.info("主动调度已启动（提醒 tick=%.0fs，回写=%.0fs）" % (TICK, SYNC_EVERY))
    while not _stop.is_set():
        time.sleep(TICK)
        try:
            cfg = load_cfg()
            tick(q, cfg)
            now = time.time()
            if now - last_sync > SYNC_EVERY:
                last_sync = now
                n = reminder.sync_occurrences(q, account=cfg.get("account"))
                if n:
                    log.info("提醒结果回写 %d 条" % n)
                try:
                    q.expire_clarify()
                    q.expire_relay_clarify()
                    q.expire_hooks()
                except Exception:
                    pass
            # 模型判定要 1~3 秒，丢到独立线程，别拖慢提醒准点
            if not _cand_busy.is_set() and now - last_cand > CAND_EVERY:
                last_cand = now
                _cand_busy.set()
                threading.Thread(target=_run_candidates, args=(q, cfg),
                                 daemon=True).start()
            # 代传话回传：够数/超时后组织一句回给发起人
            if not _back_busy.is_set() and now - last_back > BACK_EVERY:
                last_back = now
                _back_busy.set()
                threading.Thread(target=_run_relay_back, args=(q, cfg),
                                 daemon=True).start()
            # 主动开口同理：生成一句话要几秒，不能卡在 tick 里
            if (brain is not None and not _pro_busy.is_set()
                    and now - last_pro > PRO_EVERY):
                last_pro = now
                _pro_busy.set()
                threading.Thread(target=_run_proactive, args=(q, cfg, brain),
                                 daemon=True).start()
        except Exception as e:
            log.error("主动调度异常：%s: %s" % (type(e).__name__, e))
