#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_notify.py — 告警通知（出站 Webhook）单测：纯内存，不碰网络

为什么单独一个套件：`notify.py` 做的事只有一件「**边沿触发** + 投递 + 记账」，
但错法都很安静：
  · 少了差分 → 每 3 秒把同一条告警推一遍（接收端被刷爆，日志里看不出来）；
  · 少了「升级再推」→ warn 变 crit 时没通知，最该叫人起来的时刻静默；
  · 失败被吞 → 页面显示“已推 N 条”而实际一条没到（最坏的一种：以为通知在工作）。
投递函数注入假实现 → 下面每条都能精确断言「推了几次、推了什么」。

运行：python test_notify.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 本套件断言默认值（如 URL 默认空、最低级别 warn），先清掉环境里的 ORPAH_NOTIFY_*
for _k in [k for k in os.environ if k.startswith("ORPAH_NOTIFY_")]:
    os.environ.pop(_k)

import notify as nt                              # noqa: E402

FAILS = []
NOW = 1_800_000_000


def ck(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("   " + str(extra)) if not cond and extra else ""))
    if not cond:
        FAILS.append(name)


def fake_post(ok=True, status=200, err=""):
    """假投递：记录 (url, payload, timeout)。"""
    calls = []

    def f(url, payload, timeout):
        calls.append((url, payload, timeout))
        return ok, status, err
    f.calls = calls
    return f


def alert(kind, obj, level="warn", **data):
    a = {"kind": kind, "level": level, "key": f"{kind}:{obj}", "msg": "alert_x",
         "since": NOW}
    a.update(data)
    return a


# ---- 1. 没配地址 = 通知关闭 -------------------------------------------------
p = fake_post()
n = nt.Notifier(url="", post=p)
ck("默认：没配 ORPAH_NOTIFY_URL → 关闭（on=False）",
   n.snapshot()["on"] is False and n.url == "")
ck("关闭时 step 不投递、不产生投递记录",
   n.step([alert("no_report", "A")], now=NOW) == [] and not p.calls)
ck("关闭时也不把活跃告警当“新告警”攒着（seen 跟着更新，开启后不会重推一堆旧告警）",
   n.seen == {"no_report:A": "warn"})
ck("关闭时 max 也不涨计数", n.sent == 0 and n.failed == 0)

# ---- 2. 边沿触发：只在出现/升级时推 -----------------------------------------
p = fake_post()
n = nt.Notifier(url="http://127.0.0.1:9/hook", post=p)
r = n.step([alert("no_report", "A")], now=NOW)
ck("首次出现 → 推一次（event=alert）",
   len(r) == 1 and r[0]["event"] == "alert" and r[0]["ok"] is True
   and len(p.calls) == 1)
for _ in range(5):                                    # 同一状态重复评估 5 轮
    rr = n.step([alert("no_report", "A")], now=NOW + 3)
ck("★ 同一告警持续存在 → 后续评估**一次都不推**（否则每 3 秒刷一遍）",
   rr == [] and len(p.calls) == 1)
r = n.step([alert("no_report", "A", level="crit")], now=NOW + 6)
ck("★ 级别升高（warn→crit）→ 再推一次（event=alert_upgraded，带 prev_level）",
   len(r) == 1 and r[0]["event"] == "alert_upgraded" and r[0]["prev_level"] == "warn"
   and len(p.calls) == 2)
r = n.step([alert("no_report", "A", level="warn")], now=NOW + 9)
ck("级别回落（crit→warn）不推（降级不是“该叫人起来”的时刻，页面看得到即可）",
   r == [] and len(p.calls) == 2)
r = n.step([], now=NOW + 12)
ck("告警消失 → 默认不推 resolved（resolve 默认关）",
   r == [] and len(p.calls) == 2 and n.seen == {})
ck("消失后再出现 → 当成新告警再推一次（key 从 seen 里清掉了）",
   len(n.step([alert("no_report", "A")], now=NOW + 15)) == 1 and len(p.calls) == 3)

# ---- 3. resolve 开关（可选）------------------------------------------------
p = fake_post()
n = nt.Notifier(url="http://x/hook", post=p, resolve=True)
n.step([alert("no_report", "A")], now=NOW)
r = n.step([], now=NOW + 3)
ck("resolve=True → 告警消失时补推一条 alert_resolved",
   len(r) == 1 and r[0]["event"] == "alert_resolved" and n.resolved_total == 1)
p2 = fake_post()
n2 = nt.Notifier(url="http://x/hook", post=p2, resolve=True, min_level="crit")
n2.step([alert("no_report", "A", level="warn")], now=NOW)
r = n2.step([], now=NOW + 3)
ck("resolve 只对“推过的级别”补推（低于 min_level 的 warn 不推 resolved）",
   r == [] and not p2.calls)

# ---- 4. min_level：只看严重的 ----------------------------------------------
p = fake_post()
n = nt.Notifier(url="http://x/hook", post=p, min_level="crit")
r = n.step([alert("no_report", "A", level="warn")], now=NOW)
ck("min_level=crit → warn 不推（但记进 seen，skipped 计数 +1）",
   r == [] and not p.calls and n.skipped == 1 and n.seen == {"no_report:A": "warn"})
r = n.step([alert("no_report", "A", level="crit")], now=NOW + 3)
ck("同一个 key 升到 crit → 推（低级别挡不住升级通知）",
   len(r) == 1 and len(p.calls) == 1)
r = n.step([alert("no_report", "A", level="crit")], now=NOW + 6)
ck("升级后同级反复 → 不重复推", r == [] and len(p.calls) == 1)

# ---- 5. 失败必须可见 + **有界重试**（2026-09-13：语义从“不重试”改成“至少一次”）------
# 语义变了要一起改的地方：失败**不再立刻计 failed**（那变成“尝试失败次数”），
# failed = **重试用尽才计**；新增 retried（重试次数）/retry.queued（待重试）/dropped（丢弃）。
p = fake_post(ok=False, status=500, err="HTTP 500")
n = nt.Notifier(url="http://x/hook", post=p)
r = n.step([alert("no_report", "A")], now=NOW)
ck("投递失败：记录 ok=False/status/err + last_error，并**进重试队列**（failed 还不计）",
   r[0]["ok"] is False and r[0]["status"] == 500 and r[0]["err"] == "HTTP 500"
   and n.last_error == "HTTP 500" and n.sent == 0 and n.failed == 0
   and len(n.queue) == 1 and n.snapshot()["retry"]["queued"] == 1)
ck("退避：第 1 次失败后按 backoff[0] 再试（默认 1s，没到点不试）",
   n.snapshot()["retry"]["next_in"] == 1.0 and len(p.calls) == 1)
n.step([alert("no_report", "A")], now=NOW + 0.5)          # 还没到期
ck("没到退避时间不试（不空转、不提前）", len(p.calls) == 1 and len(n.queue) == 1)
n.step([alert("no_report", "A")], now=NOW + 1.0)          # 到期 → 第 2 次尝试
ck("到期重试（第 2 次尝试，tries 记进投递记录）",
   len(p.calls) == 2 and r[0]["tries"] == 1
   and n.deliveries[0]["tries"] == 2 and n.retried == 1 and n.failed == 0)
n.step([alert("no_report", "A")], now=NOW + 1.0 + 5.0)    # 第 3 次（backoff[1]=5s）→ 试完
ck("★ 重试用尽（默认 2 次重试 = 3 次尝试）→ 计一次「已失败（放弃）」、队列清空",
   len(p.calls) == 3 and n.failed == 1 and len(n.queue) == 0 and n.retried == 2)
ck("★ 没启用重试计数不虚高：failed 是“告警条数”（=1）、retried 是“多试了几次”（=2）",
   n.failed == 1 and n.snapshot()["retry"]["retried"] == 2)
n.step([alert("no_report", "A")], now=NOW + 1.0 + 5.0 + 30.0)
ck("放弃后不再重试（队列已空，后续 tick 也不补发）", len(p.calls) == 3)

# 重试成功：sent 只 +1（一条告警算一条），且不再排下一次
calls = []
def flaky(url, payload, timeout):
    calls.append(payload)
    if len(calls) >= 2:
        return True, 200, ""
    return False, 503, "HTTP 503"
n = nt.Notifier(url="http://x/hook", post=flaky)
n.step([alert("rssi_jump", "A")], now=NOW)
n.step([alert("rssi_jump", "A")], now=NOW + 1.0)
ck("★ 重试成功：sent +1（一条告警计一条）、retried +1、队列空、failed 仍 0",
   len(calls) == 2 and n.sent == 1 and n.failed == 0 and n.retried == 1
   and len(n.queue) == 0 and n.deliveries[0]["ok"] is True)

# retry=0：等价于“不重试”（旧行为），当作开关的回归锁
p = fake_post(ok=False, status=500, err="HTTP 500")
n = nt.Notifier(url="http://x/hook", post=p, retry=0)
n.step([alert("no_report", "A")], now=NOW)
ck("retry=0（不重试）→ 一次尝试即计失败（开关有效）",
   len(p.calls) == 1 and n.failed == 1 and len(n.queue) == 0)
n.step([alert("no_report", "A")], now=NOW + 100)
ck("不重试时后续 tick 也不会补发（同级反复仍不推）", len(p.calls) == 1)

# 队列上限：端点久挂时保新弃旧，且丢弃**可见**
p = fake_post(ok=False, status=500, err="HTTP 500")
n = nt.Notifier(url="http://x/hook", post=p, retry=5, queue_max=2,
                backoff=(100.0,))
for i, k in enumerate(("A", "B", "C")):
    n.step([alert("no_report", k)], now=NOW + i)         # 三条新告警都失败 → 队列只有 2
ck("队列满 → 丢最旧的并计数（dropped 可见，不静默）",
   len(n.queue) == 2 and n.snapshot()["retry"]["dropped"] == 1
   and n.queue[0]["alert"]["key"] == "no_report:B")

# 每 tick 有额度上限：端点挂掉时不能把后台线程卡住（20 条 × 3s 超时会拖死巡视）
p = fake_post(ok=False, status=500, err="HTTP 500")
n = nt.Notifier(url="http://x/hook", post=p, retry=5, backoff=(0.0,), queue_max=50)
for i in range(6):
    n.step([alert("no_report", "K%d" % i)], now=NOW + i)
before = len(p.calls)
n.step([alert("no_report", "Z")], now=NOW + 100)          # 一堆到期的重试 + 一条新的
ck("每 tick 最多试 per_step 条（默认 3）—— 防止端点挂掉时把巡视线程卡住",
   len(p.calls) - before <= 3, f"本 tick 试了 {len(p.calls) - before} 条")

# 换地址 / 关通知：丢掉待重试（那些属于旧端点），并计数
p = fake_post(ok=False, status=500, err="HTTP 500")
n = nt.Notifier(url="http://x/hook", post=p)
n.step([alert("no_report", "A")], now=NOW)
n.set_url("http://y/hook2")
ck("换地址 → 丢掉待重试（不往新端点重发旧通知）并计数",
   len(n.queue) == 0 and n.snapshot()["retry"]["dropped"] == 1)
n.step([alert("no_report", "B")], now=NOW + 1)
n.set_url("")
ck("关通知（地址清空）→ 同样丢掉待重试", len(n.queue) == 0 and n.snapshot()["retry"]["dropped"] == 2)

# 同 key 在队列里时又升级 → **合并**（用最新载荷），不给自己造重复
p = fake_post(ok=False, status=500, err="HTTP 500")
n = nt.Notifier(url="http://x/hook", post=p, backoff=(10.0,))
n.step([alert("no_report", "A", level="warn")], now=NOW)
n.step([alert("no_report", "A", level="crit")], now=NOW + 0.1)   # 升级（队列里那条还没到期）
ck("同 key 又升级 → 队列里合并成一条（用最新载荷），不排队两条",
   len(n.queue) == 1 and n.queue[0]["alert"]["level"] == "crit",
   f"queue={len(n.queue)}")

p2 = fake_post(ok=False, status=0, err="URLError: refused")
n2 = nt.Notifier(url="http://x/hook", post=p2)
n2.step([alert("x", "A")], now=NOW)
ck("连不上（status=0）也算失败，不是成功",
   n2.sent == 0 and n2.last_error == "URLError: refused" and len(n2.queue) == 1)

# ---- 6. 投递内容与记账 -----------------------------------------------------
p = fake_post()
n = nt.Notifier(url="http://127.0.0.1:9/hook", post=p, timeout=1.5)
n.step([alert("rssi_jump", "A", jump_db=40.0)], now=NOW)
url, payload, timeout = p.calls[0]
ck("投递体：source/event/ts/key/alert 都在（接收端不必猜）",
   payload["source"] == "orpah-over-halow" and payload["event"] == "alert"
   and payload["ts"] == NOW and payload["key"] == "rssi_jump:A"
   and payload["alert"]["jump_db"] == 40.0)
ck("投递体只给**机器可读**字段（msg 是 i18n 键，不做后端文案）",
   payload["alert"]["msg"] == "alert_x")
ck("超时按配置传下去（默认 3s，可配）", timeout == 1.5)
s = n.snapshot()
ck("快照：开关/地址/最低级别/计数/最近投递/正在跟踪几条都在",
   s["on"] and s["url"] == "http://127.0.0.1:9/hook" and s["sent"] == 1
   and s["watching"] == 1 and s["recent"][0]["key"] == "rssi_jump:A")
ck("投递记录带时间戳与级别（页面能直接显示）",
   s["recent"][0]["t"] and s["recent"][0]["level"] == "warn")
n.keep = 3
for i in range(6):
    n.set_url("http://x/hook")
    n.step([alert("k%d" % i, "A")], now=NOW + i)
ck("最近投递有上限（不攒着，页面只显示最近几条）", len(n.deliveries) <= 3)

# ---- 7. 测试发送 / 运行时配置 ----------------------------------------------
p = fake_post()
n = nt.Notifier(url="", post=p)
r = n.test(now=NOW)
ck("测试发送：没配地址时如实回 err=off（不是“发成功”）",
   r["event"] == "test" and r["ok"] is None and r["err"] == "off" and not p.calls)
n.set_url("http://127.0.0.1:9/hook")
r = n.test(now=NOW)
ck("测试发送：配好之后真推一条（event=test，且不动 seen）",
   r["ok"] is True and p.calls[0][1]["event"] == "test" and n.seen == {})
ck("测试发送不计入“告警已推”计数（sent 只统计真告警）", n.sent == 0)
ck("运行时改 URL / 最低级别立即生效，并做白名单校验",
   n.set_min_level("crit") == "crit" and n.set_min_level("bogus") == "crit"
   and n.set_url("  ") == "" and n.snapshot()["on"] is False)
ck("改地址不清 seen（换接收端不重推已推过的告警）",
   nt.Notifier(url="a", post=fake_post()).set_url("b") == "b")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
    raise SystemExit(1)
print("all notify tests passed")
