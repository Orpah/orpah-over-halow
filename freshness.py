#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""freshness.py — 「这是不是老数据」的单一源（UI ④，2026-09-13）

问题（现场最容易看错的地方）：页面上的数字**看起来一样**，但含义可能完全不同 ——
「刚更新的」「30 秒没动了」「其实已经停了」。光靠肉眼盯计数不动，人是分不清
“这轮没变化”和“后端早停了”的（尤其首页 1s 轮询照旧在画，看着还在动）。

所以做两件事：
1. 每条流**最近一次真的动了**的时刻（`touch`）；
2. 按**这条流自己的节拍**判断状态：`live` / `stale` / `stopped` / `unknown`。

为什么阈值按节拍推（不是拍的数）：节拍是代码里真实存在的量（周期上报的 `every`、
告警巡视图的 `ORPAH_NOTIFY_SEC`）—— 2.5× 节拍内算正常、6× 内算滞后、再久算停摆。
节拍还会随能量轴变化（省电时 `every` 会涨），所以 `touch` 允许**同时更新节拍**。

诚实边界（必须写在页面上，不许包装）：
  · 它只回答「**我们这侧**这类数据还在不在推进」，**不代表对端设备在线**
    （设备不发报 = 没人上报，也可能是它没电了/被屏蔽了）；
  · `unknown`（本进程还没见过这类数据）**既不是正常也不是故障** ——
    比如刚启动、或这次演示根本没触发过该流，不许显示成绿色“正常”。

运行自测：C:\\Python313\\python.exe test_fresh.py
"""
import time

# 阈值倍数（唯一一份；页面不写死，服务端算好状态下发）
LIVE_MULT = 2.5      # ≤ 2.5× 节拍 = 正常
STALE_MULT = 6.0     # ≤ 6× 节拍 = 滞后；再久 = 停摆

# 状态机取值（页面按这些字符串查文案，字典单一源在 ui_i18n.js）
LIVE, STALE, STOPPED, UNKNOWN = "live", "stale", "stopped", "unknown"

# 最坏状态排序：页面用 `worst` 决定整条横幅的颜色（unknown 排在 live 之后 ——
# 没数据不等于坏，但也不该被读成“一切正常”）
_ORDER = {UNKNOWN: 0, LIVE: 1, STALE: 2, STOPPED: 3}


def state_of(age_s, period_s):
    """按节拍判定状态。纯函数（不读时钟），便于单测。

    `age_s is None` → UNKNOWN（**不是** 0、不是 LIVE）：
        “没有时间戳”与“刚刚更新过”是两件事，混起来正是现场看错数据的根因。
    `period_s` 缺失/非正 → UNKNOWN：拿不到节拍就不该下判断。
    """
    if age_s is None:
        return UNKNOWN
    if not period_s or period_s <= 0:
        return UNKNOWN
    if age_s <= LIVE_MULT * period_s:
        return LIVE
    if age_s <= STALE_MULT * period_s:
        return STALE
    return STOPPED


def tsdb_row(ts_state, period_s, now=None):
    """IoTDB 落库那一行的构造（纯函数，便于单测）。

    `ts_state` = `tsdb.Tsdb.write_state()`。两个特例都在这里定死：
      · `enabled=False`（没装 iotdb 包或显式关掉）→ UNKNOWN，**不是 STOPPED** ——
        “没打算落库”与“本该落库却停了”处置完全不同，不允许合成一句；
      · `enabled=True` 但从未成功过（`last_ok is None`）→ UNKNOWN
        （还没有基准可比，不该一上来就红）。
    """
    now = time.time() if now is None else now
    last = ts_state.get("last_ok")
    age = None if last is None else max(0.0, now - last)
    st = UNKNOWN if not ts_state.get("enabled") else state_of(age, period_s)
    return {"key": "tsdb_write", "last": last,
            "age_s": None if age is None else round(age, 1),
            "period_s": period_s, "state": st,
            "on": bool(ts_state.get("enabled")),
            "available": bool(ts_state.get("available")),
            "err": ts_state.get("err", ""),
            "last_err": ts_state.get("last_err")}


class Tracker:
    """各条流的“最近一次动过”记录。线程安全（dict 单键读写是原子的，够用）。"""

    def __init__(self):
        self._t = {}          # name -> {"last": epoch|None, "period": s}

    def touch(self, name, period=None, ts=None):
        """记一次“动了”。`period` 传了就把这条流的节拍更新掉（能量轴会改周期）。"""
        rec = self._t.setdefault(name, {"last": None, "period": None})
        rec["last"] = time.time() if ts is None else ts
        if period is not None:
            rec["period"] = float(period)
        return rec["last"]

    def set_period(self, name, period):
        """只更新节拍（不动 last）—— 节拍会变，但不代表刚有数据。"""
        rec = self._t.setdefault(name, {"last": None, "period": None})
        rec["period"] = None if period is None else float(period)

    def rows(self, now=None):
        """给页面用的行：`age_s` 在**服务端**算（避免浏览器与本机时钟不一致时的错判）。"""
        now = time.time() if now is None else now
        out = []
        for name, rec in sorted(self._t.items()):
            last = rec["last"]
            age = None if last is None else max(0.0, now - last)
            st = state_of(age, rec["period"])
            out.append({"key": name, "last": last,
                        "age_s": None if age is None else round(age, 1),
                        "period_s": rec["period"], "state": st})
        return out

    def worst(self, rows=None):
        rows = self.rows() if rows is None else rows
        w = UNKNOWN
        for r in rows:
            if _ORDER[r["state"]] > _ORDER[w]:
                w = r["state"]
        return w
