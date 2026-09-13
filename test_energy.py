#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_energy.py — 能量轴（免电池客户端能量模型）单测，离线、纯计算。

覆盖：模型的收支/间隔/级别选择；**"降级只为跟得住人"**（不省电也就不用降）；
**监听（下行）是固定开销**（2026-09-13 加：加监听前后的数各自锁住）；
采不敷出时只付固定开销 + 还能撑多久、并归因到"哪一层缺钱"（`short_of`）；
**没采集就如实沉默**（`interval=None`，不编一个数）；不主动降 L3；
能量轴扫描与头条数字（`min_harvest_mw`）；电量推进 `drain()` 的边界。

跑法：C:\\Python313\\python.exe test_energy.py
"""
import sys

import energy as en

# 控制台是 GBK：打印非 GBK 字符（µ/✓/✗/−…）会 UnicodeEncodeError 崩 → 统一兜住
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

FAIL = []


def ck(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


# 本套件的两套参数：演示默认（**含监听**）与"不建模监听"（= 加监听之前的行为，逐位可复现）
NO_L = {"listen_interval_s": 0}
OVERHEAD = en.SLEEP_MW + en.LISTEN_MJ / en.LISTEN_INTERVAL_S      # 0.05 + 0.1 = 0.15 mW


print("== 1. 电压映射（电量 → battery_mv）==")
ck("满电 → 上限电压", en.mv_of(en.STORE_MJ) == en.CELL_FULL_MV)
ck("空电 → 下限电压", en.mv_of(0) == en.CELL_EMPTY_MV)
ck("半电 → 中间值（线性）",
   en.mv_of(en.STORE_MJ / 2) == (en.CELL_EMPTY_MV + en.CELL_FULL_MV) // 2)
ck("越界钳位（超出容量也不超上限、负值不跌破下限）",
   en.mv_of(en.STORE_MJ * 3) == en.CELL_FULL_MV and en.mv_of(-5) == en.CELL_EMPTY_MV)
ck("store=0 不炸（返回下限）", en.mv_of(10, store_mj=0) == en.CELL_EMPTY_MV)

print("== 2. 采集充足：ES256 + 够用的间隔（其中监听是固定开销）==")
p = en.plan(1.0, en.CHARGE0_MJ)
ck("监听平均开销 = listen_mj / 听间隔（6mJ / 60s = 0.1 mW）",
   abs(p["listen_mw"] - 0.1) < 1e-9 and p["listen_modeled"], str(p.get("listen_mw")))
ck("固定开销 = 待机 + 监听（0.05 + 0.1 = 0.15 mW）——**间隔由扣完它之后的可上报功率决定**",
   abs(p["overhead_mw"] - OVERHEAD) < 1e-9 and abs(p["usable_mw"] - 0.85) < 1e-9,
   str((p["overhead_mw"], p["usable_mw"])))
ck("净余 net_mw 含意未变（采集 − 待机 = 0.95；只是它不再决定间隔）",
   abs(p["net_mw"] - 0.95) < 1e-9)
ck("采集 1mW → 用 ES256（不降级）", p["level"] == "ES256" and not p["degraded"], str(p))
ck("间隔 = cost_es / 可上报功率", abs(p["interval_s"] - 15.0 / 0.85) < 0.01,
   str(p["interval_s"]))
ck("上报的平均功率回显（= 可上报功率，收支恰好平衡）",
   abs(p["report_mw"] - 0.85) < 1e-9)
ck("收支平衡 → silence_in_s 为 null（不会因没电沉默）",
   p["budget_ok"] and p["silence_in_s"] is None)
ck("usable=True（跟得住人）", p["usable"] and p["why"] == "ok")

print("== 3. 采集偏低：降级**只为把间隔拉回可用区** ==")
p = en.plan(0.18, en.CHARGE0_MJ)                    # 可上报 = 0.18 − 0.15 = 0.03 mW
ck("ES256 撑不住（500s）、HS256 够用（166.7s）→ 降级且标明成因",
   p["level"] == "HS256" and p["degraded"] and p["degraded_reason"] == "energy", str(p))
ck("why=degraded_saves（是「为了还能跟住人」，不是「为省电而省电」）",
   p["why"] == "degraded_saves")
ck("两个候选间隔都给出（页面才能解释为什么降）",
   p["interval_es256_s"] == 500.0 and abs(p["interval_hs256_s"] - 166.67) < 0.01, str(p))
ck("降级后 usable=True", p["usable"])
ck("**降级不跨 L3**：级别只可能是 ES256/HS256（能量再紧也不发裸报）",
   p["level"] in ("ES256", "HS256"))

print("== 4. 采集太低：慢到跟不住人（但仍不降 L3） ==")
p = en.plan(0.16, en.CHARGE0_MJ)                    # 可上报 0.01 → HS256 要 500s
ck("两个级别都超上限 → why=too_slow 且 usable=False",
   p["why"] == "too_slow" and not p["usable"], str(p))
ck("仍然用 HS256（能量紧时没理由挑更贵的算法）", p["level"] == "HS256")
ck("收支其实平衡（只是太慢）→ silence_in_s 仍为 null",
   p["budget_ok"] and p["silence_in_s"] is None, str(p))

print("== 5. 采不敷出：没有可维持间隔 + **缺钱在哪一层**（short_of） ==")
p = en.plan(0.03, en.CHARGE0_MJ)                    # 0.03 < 待机 0.05
ck("interval_s 为 None（没有可维持的间隔）", p["interval_s"] is None, str(p))
ck("why=deficit（有采集但不够）", p["why"] == "deficit")
ck("short_of=sleep（连待机都不够）", p["short_of"] == "sleep", str(p.get("short_of")))
ck("budget_ok=False（在吃储能）", not p["budget_ok"])
ck("silence_in_s = 储能 /(固定开销 − 采集) = 1500/0.12 = 12500s",
   abs(p["silence_in_s"] - 12500.0) < 1.0, str(p["silence_in_s"]))
ck("degraded_reason=energy（标出成因=能量）", p["degraded_reason"] == "energy")
ck("usable=False（跟不住人）", not p["usable"])

p = en.plan(0.08, en.CHARGE0_MJ)                    # 0.05 ≤ 0.08 < 0.15
ck("★ short_of=listen：供得住待机、**供不住监听**（听不成了）——不再笼统说“采不敷出”",
   p["short_of"] == "listen" and p["why"] == "deficit", str(p.get("short_of")))
ck("★ **不偷偷帮它拉长听间隔**：听间隔原样回显，只是如实报“没有可维持间隔”",
   p["interval_s"] is None and p["listen_interval_s"] == en.LISTEN_INTERVAL_S, str(p))
ck("silence_in_s = 1500/(0.15 − 0.08) = 21428.6s",
   abs(p["silence_in_s"] - 21428.6) < 1.0, str(p["silence_in_s"]))
ck("★ 硬撑（宁可吃储能也要被听见）：survive_s 的缺口里**含监听**",
   abs(en.survive_s(en.CHARGE0_MJ, 0.0, en.EMERGENCY_INTERVAL_S, "HS256",
                    listen_mw=p["listen_mw"])
       - 1500.0 / (0.05 + 0.1 + 5.0 / 60.0)) < 0.5,
   str(en.survive_s(en.CHARGE0_MJ, 0.0, en.EMERGENCY_INTERVAL_S, "HS256",
                    listen_mw=p["listen_mw"])))
ck("survive_s：收支平衡 → None（不会因没电停，不给 0）",
   en.survive_s(100, 1.0, 60, "HS256") is None)
ck("survive_s：已没电 → 0.0", en.survive_s(0.0, 0.0, 60, "HS256") == 0.0)

print("== 6. 完全没采集：**如实沉默**，不编间隔 ==")
p = en.plan(0.0, en.CHARGE0_MJ)
ck("interval_s 为 None（没有可维持的间隔）", p["interval_s"] is None, str(p))
ck("why=no_energy", p["why"] == "no_energy")
ck("silence_in_s = 储能/固定开销 = 1500/0.15 = 10000s（监听也要吃电，所以比以前短）",
   abs(p["silence_in_s"] - 10000.0) < 1.0, str(p["silence_in_s"]))
ck("interval_es256_s / interval_hs256_s 都为 None（不编候选）",
   p["interval_es256_s"] is None and p["interval_hs256_s"] is None)
ck("usable=False（不能假装能定位）", not p["usable"])

print("== 7. 电量不够一次上报：why=empty（现在发不动） ==")
p = en.plan(1.0, 1.0)                               # 电量 1 mJ < cost_es 15 mJ
ck("电量 < 单次耗电 → why=empty", p["why"] == "empty", str(p))
ck("usable=False", not p["usable"])
ck("长期收支仍然平衡 → silence_in_s=None（「现在发不动」与「会不会没电」是两件事）",
   p["silence_in_s"] is None, str(p))
p = en.plan(0.0, 0.0)
ck("没采集且电量耗尽 → silence_in_s=0（确实停了，不是 null）", p["silence_in_s"] == 0.0)

print("== 8. 单调性与可用阈值（能量轴的趋势必须单调）==")
hs = [i * 0.02 for i in range(0, 41)]               # 0 … 0.8 mW
rows = en.sweep(hs, en.CHARGE0_MJ)["rows"]          # sweep 的行带 harvest_mw（plan 本身不带）
ck("每个级别的间隔都随采集增加而**不增**（两条曲线各自单调；None=没有可维持间隔，跳过）",
   all(b["interval_hs256_s"] <= a["interval_hs256_s"] + 1e-9
       for a, b in zip([r for r in rows if r["interval_hs256_s"]],
                       [r for r in rows if r["interval_hs256_s"]][1:]))
   and all(b["interval_es256_s"] <= a["interval_es256_s"] + 1e-9
           for a, b in zip([r for r in rows if r["interval_es256_s"]],
                           [r for r in rows if r["interval_es256_s"]][1:])),
   f"{[(r['harvest_mw'], r['interval_es256_s'], r['interval_hs256_s']) for r in rows[:3]]}…")
ck("级别随采集增加而**升级**（HS256 → ES256，不会倒退）",
   [r["level"] for r in rows] == sorted([r["level"] for r in rows], reverse=True))
ck("同一个级别内部，间隔随采集增加而下降",
   all(b[1] <= a[1] + 1e-9 for a, b in
       zip([(r["level"], r["interval_s"]) for r in rows if r["interval_s"]],
           [(r["level"], r["interval_s"]) for r in rows if r["interval_s"]][1:])
       if a[0] == b[0]))
ck("存在一个「够用」的采集门槛（低采集不可用、高采集可用）",
   not rows[0]["usable"] and rows[-1]["usable"])
first_ok = next(r["harvest_mw"] for r in rows if r["usable"])
ck("★ 门槛 = 固定开销 + HS256 恰好跑在 300s 所需 = 0.15 + 5/300 ≈ 0.1667 mW"
   "（**加了监听之后**才这么高；见第 11 节对比）——本网格 0.02 步长只能落到 0.18，"
   "解析值用第 9 节的 1/12 网格钉住",
   OVERHEAD + 5.0 / en.MAX_USEFUL_INTERVAL_S <= first_ok <= 0.18
   and not en.plan(0.16, en.CHARGE0_MJ)["usable"], f"h={first_ok}")
ck("跨过门槛的第一行是 **HS256 降级**（先用便宜算法达成「跟得住人」），"
   "再到更高采集才升级为 ES256（不降级）",
   next(r["level"] for r in rows if r["usable"]) == "HS256"
   and next(r["level"] for r in rows if r["usable"] and not r["degraded"]) == "ES256"
   and next(r["harvest_mw"] for r in rows if r["usable"] and not r["degraded"]) > first_ok,
   f"第一个可用={first_ok} mW/{next(r['level'] for r in rows if r['usable'])}；"
   f"第一个不降级={next(r['harvest_mw'] for r in rows if r['usable'] and not r['degraded'])} mW")
# 已知且**有意**的特性：升级算法那一下会让间隔跳大一次（“安全优先，只要还能跟得住人”）——
# 把它写下来，避免下次当成 bug（实测：0.16 mW 时 HS256/500s → 0.18 时 HS256/167s → 0.20 时 ES256/300s）
_up = [(a["harvest_mw"], a["level"], a["interval_s"], b["harvest_mw"], b["level"], b["interval_s"])
       for a, b in zip(rows, rows[1:])
       if a["interval_s"] and b["interval_s"] and b["interval_s"] > a["interval_s"] + 1e-9]
ck("升级那一下间隔会跳大一次（有意：ES256 比 HS256 贵 3 倍），且只发生在级别变化处",
   all(u[1] != u[4] for u in _up) and len(_up) == 1, str(_up))

print("== 9. 能量轴扫描：头条数字 = 持续跟踪所需的最小采集功率 ==")
ax = en.axis(n=13, h_max=1.0)
ck("点数正确（含 0 与 h_max）",
   ax["n"] == 13 and ax["rows"][0]["harvest_mw"] == 0.0
   and ax["rows"][-1]["harvest_mw"] == 1.0)
ck("min_harvest_mw = 第一个 usable 的采集功率",
   ax["min_harvest_mw"] == next(r["harvest_mw"] for r in ax["rows"] if r["usable"]))
ck("★ 浮点容差：恰好等于上限（300s）要算**够用** —— 否则门槛会随浮点噪声跳一档"
   "（除法在边界上会给出 300.00000000000006）",
   ax["min_harvest_mw"] == 0.167, str(ax["min_harvest_mw"]))
ck("无解时不编数：h_max 太小 → min_harvest_mw 为 None",
   en.axis(n=5, h_max=0.001)["min_harvest_mw"] is None)
ck("自定义 cost（真机标定后替换演示值）也能算",
   en.plan(0.5, 1000, cost={"ES256": 30.0, "HS256": 10.0})["interval_s"] > 0)
ck("n<2 被钳到 2（不崩）", en.axis(n=1)["n"] == 2)

print("== 10. drain()：电量推进与边界（含监听的固定开销）==")
c = en.drain(1000, 0.0, 100, "ES256", 100)          # 采集 0、待机 0.05W·100s=5mJ、上报 15mJ
ck("不建模监听时：只掉待机 + 一次上报（5 + 15 = 20 mJ）", abs(c - 980.0) < 1e-6, str(c))
ck("★ 监听也要逐拍扣（0.1mW × 100s = 10 mJ）→ 970 mJ"
   "（漏掉监听会让电量比模型说得乐观，而它会被写成已签的 battery_mv）",
   abs(en.drain(1000, 0.0, 100, "ES256", 100, listen_mw=0.1) - 970.0) < 1e-6)
c = en.drain(1000, 1.0, 10, "ES256", 100)           # 采集 100mJ 进来，每 10s 一次 → 10 次上报 = 150mJ
ck("采集足够时电量上升（100 − 5 − 150 = 945）", abs(c - 945.0) < 1e-6, str(c))
ck("interval_s=None（沉默）→ 只付固定开销（待机 + 监听）",
   abs(en.drain(1000, 0.0, None, "HS256", 100, listen_mw=0.1) - 985.0) < 1e-6)
ck("钳在 [0, store]（不出现负电量、不超容量）",
   en.drain(1, 0.0, 2, "ES256", 100) == 0.0
   and en.drain(en.STORE_MJ, 99.0, 2, "HS256", 100) == en.STORE_MJ)

print("== 11. 监听（2026-09-13 新增）：口径与「加之前 / 之后」对照 ==")
ck("监听默认**建模**（要显式给 listen_interval_s=0 才关）",
   en.plan(1.0, en.CHARGE0_MJ)["listen_modeled"]
   and not en.plan(1.0, en.CHARGE0_MJ, **NO_L)["listen_modeled"])
old = en.plan(1.0, en.CHARGE0_MJ, **NO_L)
ck("★ 回归锁：关掉监听 → 与“加监听之前”的数**逐位一致**（间隔 = cost/净余；"
   "`interval_s` 本身是 2 位四舍五入的展示值）",
   abs(old["interval_s"] - 15.0 / 0.95) < 0.01 and old["overhead_mw"] == en.SLEEP_MW
   and old["usable_mw"] == old["net_mw"] and old["listen_mw"] == 0.0, str(old))
old2 = en.plan(0.08, en.CHARGE0_MJ, **NO_L)
ck("★ 同一把锁的老口径：0.08 mW → HS256/166.67s（正是加监听之前的演示数字）",
   old2["why"] == "degraded_saves" and abs(old2["interval_hs256_s"] - 166.67) < 0.01,
   str(old2))
ck("★ 加监听之后同一个采集点**变成沉默**（0.08 mW 原来能降级撑着，现在连听都听不成）",
   en.plan(0.08, en.CHARGE0_MJ)["interval_s"] is None
   and en.plan(0.08, en.CHARGE0_MJ)["short_of"] == "listen")
ck("★ 门槛对比（写死在用例里当事实锁）：0.083 → 0.167 mW，**约 2 倍**",
   abs(en.axis(n=13, h_max=1.0, **NO_L)["min_harvest_mw"] - 0.083) < 1e-9
   and abs(en.axis(n=13, h_max=1.0)["min_harvest_mw"] - 0.167) < 1e-9,
   str((en.axis(n=13, h_max=1.0, **NO_L)["min_harvest_mw"],
        en.axis(n=13, h_max=1.0)["min_harvest_mw"])))
ck("听间隔是**策略**：拉长 → 监听开销变小 → 同一个采集点又够用了（这是设计取舍，不是 bug）",
   en.plan(0.16, en.CHARGE0_MJ)["why"] == "too_slow"
   and en.plan(0.16, en.CHARGE0_MJ, listen_interval_s=600)["usable"]
   and abs(en.plan(0.16, en.CHARGE0_MJ, listen_interval_s=600)["listen_mw"] - 0.01) < 1e-9)
ck("听间隔越短 → 监听开销越大（单调）",
   en.plan(1.0, en.CHARGE0_MJ, listen_interval_s=10)["listen_mw"]
   > en.plan(1.0, en.CHARGE0_MJ, listen_interval_s=60)["listen_mw"]
   > en.plan(1.0, en.CHARGE0_MJ, listen_interval_s=600)["listen_mw"])
ck("每次听窗口**耗电为 0** = 合法（有的固件听窗口极小）→ 等价于只付待机",
   abs(en.plan(1.0, en.CHARGE0_MJ, listen_mj=0.0)["overhead_mw"] - en.SLEEP_MW) < 1e-9)
try:
    en.plan(1.0, en.CHARGE0_MJ, listen_mj=-1)
    ck("写错（负的听窗口耗电）→ 当场报错，不静默当成 0", False)
except ValueError:
    ck("写错（负的听窗口耗电）→ 当场报错，不静默当成 0", True)
try:
    en.plan(1.0, en.CHARGE0_MJ, listen_interval_s=-5)
    ck("写错（负的听间隔）→ 当场报错", False)
except ValueError:
    ck("写错（负的听间隔）→ 当场报错", True)
ck("扫描表的每一行都带监听字段（页面/接口不必自己算）",
   all("listen_mw" in r and "usable_mw" in r and "overhead_mw" in r
       for r in en.axis(n=5)["rows"]))
ck("不建模监听时 listen_interval_s=None（**不撒谎说 0s**）",
   en.plan(1.0, en.CHARGE0_MJ, **NO_L)["listen_interval_s"] is None)

print("== 12. 覆盖（不断线）：缺口里能撑多久 + 降级换覆盖 ==")
# 口径（2026-09-13 用户定）：取能波动 ≠ 可以夜间停机。夜间/取能低谷**必须覆盖**；
# 不够时先降级（HS256）再把间隔拉到上限，**绝不沉默**；还差多少就如实说。
P = en.plan(0.5, en.CHARGE0_MJ)                     # 常态：ES256 / 42.86s / 开销 0.15 / 上报 0.35
ck("不建模缺口（不给 gap_s）→ verdict=none 且**所有数都是 None**（不编）",
   en.coverage(P, en.CHARGE0_MJ)["verdict"] == "none"
   and en.coverage(P, en.CHARGE0_MJ)["cover_s"] is None
   and en.coverage(P, en.CHARGE0_MJ)["degraded"] is None)
ck("gap_s=0 同样视为不建模（0 ≠ 一个零长度的缺口）",
   en.coverage(P, en.CHARGE0_MJ, gap_s=0)["verdict"] == "none")
try:
    en.coverage(P, en.CHARGE0_MJ, gap_s=-1)
    ck("写错（负的缺口时长）→ 当场报错", False)
except ValueError:
    ck("写错（负的缺口时长）→ 当场报错", True)

c1 = en.coverage(P, 1500.0, gap_s=600.0)            # 缺口 10 分钟
ck("缺口短 → verdict=ok（常态策略就覆盖得住）",
   c1["verdict"] == "ok" and c1["covers"], str(c1["verdict"]))
ck("缺口期净开销 = 固定开销 + 上报功率（0.15 + 0.35 = 0.5 mW）",
   abs(c1["gap_deficit_mw"] - 0.5) < 1e-9, str(c1["gap_deficit_mw"]))
ck("能撑 = 储能 / 缺口开销（1500/0.5 = 3000s）", abs(c1["cover_s"] - 3000.0) < 0.1,
   str(c1["cover_s"]))
ck("要覆盖该缺口所需的最小储能 = 开销 × 缺口时长（0.5mW × 600s = 300 mJ）",
   abs(c1["need_store_mj"] - 300.0) < 0.1, str(c1["need_store_mj"]))
ck("自给自足线 need_harvest_mw = 固定开销 + 上报功率",
   abs(c1["need_harvest_mw"] - 0.5) < 1e-9, str(c1["need_harvest_mw"]))

c2 = en.coverage(P, 1500.0, gap_s=3600.0)           # 缺口 1 小时：常态 3000s 不够，降级后 9000s 够
ck("★ 常态不够、**降级换覆盖**够 → verdict=degrade",
   c2["verdict"] == "degrade" and not c2["covers"] and c2["degraded"]["covers"],
   str(c2["verdict"]))
ck("★ 降级口径：HS256（**永不 L3**）+ 间隔拉到可用上限（不再往上）",
   c2["degraded"]["level"] == "HS256"
   and c2["degraded"]["interval_s"] == en.MAX_USEFUL_INTERVAL_S,
   str((c2["degraded"]["level"], c2["degraded"]["interval_s"])))
ck("★ 降级多撑的时间 extra_s = 9000 − 3000 = 6000s",
   abs(c2["degraded"]["extra_s"] - 6000.0) < 0.1, str(c2["degraded"]["extra_s"]))

c3 = en.coverage(P, 1500.0, gap_s=12 * 3600.0)      # 缺口 12 小时：降级也不够 → 设计不足
ck("★ 降级也不够 → verdict=short（**设计不足**，不是“正常作息”）",
   c3["verdict"] == "short", str(c3["verdict"]))
ck("如实给出还差多少（降级后仍差 34200s）",
   abs(c3["degraded"]["gap_short_s"] - 34200.0) < 0.1, str(c3["degraded"]["gap_short_s"]))
ck("★ 可执行结论：覆盖 12h 缺口，常态要 21.6 J、降级后只要 7.2 J（降级把储能需求降下来）",
   abs(c3["need_store_mj"] - 21600.0) < 1.0
   and abs(c3["degraded"]["need_store_mj"] - 7200.0) < 1.0,
   str((c3["need_store_mj"], c3["degraded"]["need_store_mj"])))
ck("缺口期还有采集 → 净开销变小、能撑更久（0.5→0.4mW 时 3750s）",
   abs(en.coverage(P, 1500.0, gap_s=12 * 3600.0, gap_harvest_mw=0.1)["cover_s"]
       - 3750.0) < 0.1)
ck("缺口期收支平衡（采集 ≥ 开销）→ cover_s=None + covers=True（**平衡不是 0 秒**）",
   en.coverage(P, 1500.0, gap_s=3600.0, gap_harvest_mw=0.5)["cover_s"] is None
   and en.coverage(P, 1500.0, gap_s=3600.0, gap_harvest_mw=0.5)["covers"])
P_sil = en.plan(0.10, en.CHARGE0_MJ)                # 常态就沉默（report=0）
ck("常态就沉默时：缺口里只付固定开销（report_mw=0 → 净开销 0.15）",
   P_sil["report_mw"] == 0.0
   and abs(en.coverage(P_sil, 1500.0, gap_s=12 * 3600.0)["gap_deficit_mw"] - 0.15) < 1e-9)
ck("★ 常态本来就不报时，“降级换覆盖”**无益**（extra_s<0：加回报反而更早断线）——"
   "这种情形该走“加大储能/取能”，页面得区分",
   en.coverage(P_sil, 1500.0, gap_s=12 * 3600.0)["degraded"]["extra_s"] < 0,
   str(en.coverage(P_sil, 1500.0, gap_s=12 * 3600.0)["degraded"]["extra_s"]))

print("== 13. 覆盖：按**实测取能曲线**积分（可选路径）==")
NIGHT_DAY = [[0, 0.0], [6 * 3600, 0.0], [6 * 3600, 1.2],      # 00:00-06:00 无取能
             [18 * 3600, 1.2], [18 * 3600, 0.0],              # 06:00-18:00 有光
             [24 * 3600, 0.0]]                                # 18:00-24:00 无取能
cc = en.coverage(P, 1500.0, curve=NIGHT_DAY)
ck("最长缺口 = 6 小时（夜）", abs(cc["curve"]["longest_gap_s"] - 21600.0) < 0.1,
   str(cc["curve"]["longest_gap_s"]))
ck("★ 要撑过这一夜所需储能 = 0.5mW × 6h = 10800 mJ（**不是**“最长缺口×开销”以外的东西："
   "中间能回电的地方会自动减掉）",
   abs(cc["need_store_mj"] - 10800.0) < 0.5, str(cc["need_store_mj"]))
ck("这条曲线**可永续**（白天净充电 > 夜里净亏）",
   cc["curve"]["sustainable"] and abs(cc["curve"]["cycle_net_mj"] - 8640.0) < 1.0,
   str(cc["curve"]["cycle_net_mj"]))
ck("储能只有 1500 mJ → 夜里就断（50 分钟，0.5mW 下 1500/0.5=3000s）",
   cc["verdict"] == "short" and abs(cc["curve"]["dead_at_s"] - 3000.0) < 1.0
   and cc["curve"]["min_charge_mj"] == 0.0, str(cc["curve"]))
ck("★ 降级换覆盖把“撑过这一夜”的储能需求从 10.8 J 降到 3.6 J",
   abs(cc["degraded"]["need_store_mj"] - 3600.0) < 0.5,
   str(cc["degraded"]["need_store_mj"]))
cc_ok = en.coverage(P, 12000.0, curve=NIGHT_DAY)    # 给足储能（> 10800）
ck("给足储能 → 不断线（dead_at=None）", cc_ok["curve"]["dead_at_s"] is None
   and cc_ok["covers"] and cc_ok["verdict"] == "ok", str(cc_ok["curve"]))
CC_WEAK = [[0, 0.0], [6 * 3600, 0.0], [6 * 3600, 0.6],
           [18 * 3600, 0.6], [18 * 3600, 0.0], [24 * 3600, 0.0]]
cw = en.coverage(P, 12000.0, curve=CC_WEAK)
ck("★ 采集不够自给（日均 0.3mW < 支出 0.5mW）→ sustainable=False + 周期净亏 −17.28 J："
   "此时 need_store 只是“撑过一个周期”，**再大的储能也只是拖时间**",
   not cw["curve"]["sustainable"] and abs(cw["curve"]["cycle_net_mj"] + 17280.0) < 1.0
   and cw["verdict"] != "ok", str(cw["curve"]))
ck("曲线路径不给单值“能撑多久”（不编不适用的秒数）",
   cc["cover_s"] is None and cc["gap_deficit_mw"] is None)
ck("阶跃写法（相邻同刻点）合法；时间**倒退**才报错",
   en.coverage(P, 1500.0, curve=[[0, 0.0], [10, 0.0], [10, 1.0], [20, 1.0]])["gap_s"] == 20.0)
for bad, why in (([[0, 0.0], [10, 0.5], [5, 0.0]], "时间倒退"),
                 ([[0, 0.0]], "点数<2"),
                 ([[0, -1.0], [10, 0.0]], "功率为负")):
    try:
        en.coverage(P, 1500.0, curve=bad)
        ck("曲线畸形（%s）→ 报错，不静默算出差数" % why, False)
    except ValueError:
        ck("曲线畸形（%s）→ 报错，不静默算出差数" % why, True)
ck("纯函数：同一输入两次结果完全一样（不藏状态）",
   en.coverage(P, 1500.0, gap_s=3600.0) == en.coverage(P, 1500.0, gap_s=3600.0)
   and en.coverage(P, 1500.0, curve=NIGHT_DAY) == en.coverage(P, 1500.0, curve=NIGHT_DAY))

print()
if FAIL:
    print(f"{len(FAIL)} 项失败: " + "; ".join(FAIL))
    sys.exit(1)
print("能量轴测试全部通过")
