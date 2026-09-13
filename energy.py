#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""energy.py — 免电池/低功耗客户端的**能量轴**（纯计算，2026-09-13）

## 为什么有这个东西（用户 2026-09-13 定方向）

ORPAH 的终端设定是**免电池可穿戴**（项链/鞋/钮扣，靠运动/光/射频取能）。对免电池设备，
真正的约束不是信号距离，而是**能量预算**：每次上报都要花电（唤醒 → 关联/发帧 → 签名），
而采集功率是**波动的**（进屋、被衣服盖住、站着不动 → 采集骤降）。于是「多久报一次」
「还签不签得起 ES256」不再是常数，而是**能量的函数**（ROADMAP §一 那句话）。

最要命的一点：**沉默有两种相反的成因** —— 设备**没电了**（等它取能）还是**被藏/被干扰**
（立刻搜）。演示里两者原来都只触发「长未上报」，找人场景下处置正好相反。本模块给出
**可算的判据**（当前电量能撑多久），让服务端能把两种沉默分开（`alerts.py` 里做）。

## 模型（**三个参数**，2026-09-13 用户选定「A. 三参数单源模型」）

| 参数 | 含义 |
|---|---|
| `harvest_mw` | 平均**采集**功率（mW）——可调，是能量轴的横轴 |
| `store_mj`   | **储能**容量（mJ；电容/电池），当前电量 `charge_mj` |
| `cost_mj`    | **单次上报**耗电（mJ；唤醒+发帧+签名），按级别不同（ES256 比 HS256 贵） |
| `listen_mj` / `listen_interval_s` | **每次听窗口**耗电（mJ）与**多久听一次**（s）——见下「监听」 |

派生量：净功率 `net = harvest - sleep`（`sleep_mw` = 待机功耗，否则设备永不掉电）；
固定开销 `overhead = sleep + listen_mw`；**真正能拿去上报的功率** `usable = harvest - overhead`；
可维持间隔 `cost / usable`；**还能撑多久**（`silence_in_s`）= 在吃储能时 `charge / 缺口`。

## 监听（下行是免电池终端的**固定开销**，2026-09-13 加）

下行（走失表更新 / 回执 / ERROR）**不会自己送到**：客户端必须**周期性醒来听**。
这一块在 2026-09-13 之前的模型里**是 0** —— 而真机上它常常**比待机还贵**
（听窗口 = 唤醒 + RF 收 + 解码；以“200ms @12mA @3.7V”为量级 = **8.9 mJ/次**，
每 60s 听一次就是 **0.15 mW**，而待机才 0.05 mW）。三条口径：

1. **按平均功率折算**：`listen_mw = listen_mj / listen_interval_s`，进**固定开销**那一侧
   （与上报间隔无关）。模型**不**建窗口内的时序（醒多久、什么时候收）。
2. **听间隔是产品选择，不是标定项**：`listen_interval_s` 调长 = 下行变慢、发现更慢 ——
   那是取舍，**模型不替调用方拍板**。采集供不住时，模型**如实报“没有可维持间隔”**
   （`interval_s=None`）并用 `short_of` 指哪一层缺钱，**绝不自作主张把听间隔拉长**。
3. **沉默倒计时按“继续按听间隔监听”算**（它是唯一还能收到下行的方式）。
   固件若选择“干脆不听了去保命”，能撑更久 —— 那是另一种产品选择，本模型**不建**。
   另：听失败后的重试/重关联、信标跟踪的差别（DTIM/唤醒源）也没建。

## ⚠ 参数是**演示标定值，不是实测**

`COST_MJ` / `SLEEP_MW` / `LISTEN_MJ` / `LISTEN_INTERVAL_S` / `CELL_*_MV` / `STORE_MJ`
全是**凑出来能演示的量级**，**没有**真机实测依据（真机标定要上机测唤醒/发帧/签名/听窗口
的能耗，属阶段二）。所以：
- 任何文档/页面上引用这些数都要标「演示参数」；
- 结论只用于演示**趋势**（采集越低 → 间隔越长 → 级别越低 → 最终沉默），不能当时长承诺。

## 级别与间隔策略（用户 2026-09-13 选「A. 允许降级，但必须显式标注」）

先说清楚 **`P = max(usable, 0)` = 真正能拿去上报的功率**（采集 − 待机 − 监听）。规则表：

| `P` | 条件 | 级别 | 间隔 | `why` |
|---|---|---|---|---|
| `> 0` | ES256 维持的间隔 ≤ `MAX_USEFUL_INTERVAL_S` | ES256 | `cost_es/P` | `ok` |
| `> 0` | ES256 太长、HS256 够用 | **HS256（降级）** | `cost_hs/P` | `degraded_saves` |
| `> 0` | 两个级别都超过可用上限 | HS256 | `cost_hs/P` | `too_slow` |
| `= 0` | 还有点采集（`harvest > 0`） | HS256 | **`None`** | `deficit` |
| `= 0` | 完全没采集 | HS256 | **`None`** | `no_energy` |

1. **降级要"为了还能跟住人"，不是"为了省电而省电"**：省电本身不是目的，间隔长到
   `> MAX_USEFUL_INTERVAL_S` 就等于跟不住人（追踪失去意义），这时用便宜算法把间隔拉回可用区，
   是拿**一点信任度**换**还能找到人**。所以只有第 2 行叫 `degraded_saves`，且
   `degraded_reason="energy"` 明确标出 —— **不是偷偷降**（服务端据此出告警，页面写清"因省电而降级"）。
2. **不主动降到 L3（无签名）**：L3 按 §8.3 不能用于人员确认 —— 那是「找不到人时反而把人丢掉」
   的坏交易。能量再紧也只到 HS256；真到发不动，就**如实沉默**并让服务端看见
   （`silence_in_s` 告诉它"我预计什么时候没电"），而不是发一条不可信的裸报。
3. **`P = 0` 是一个独立的"没有可维持间隔"状态**（`interval_s=None`），
   不是"慢慢报"：采集连**待机**都不够时，任何固定间隔都在吃储能，"慢慢报"与"快点报"
   只是"把储能拿去发报"还是"把储能拿去待机"的分别 —— 模型不该替调用方拍这个板，
   而是**给事实**（`silence_in_s` = 还能撑多久）。想"宁可吃储能也要被听见"的调用方用
   `EMERGENCY_INTERVAL_S` + `survive_s()` 硬撑，并把截止时间告诉服务端。
   这样也让这条曲线**单调**（采集↑ → 间隔↓），不会在 `net ≈ 0` 处跳变。
4. **已知且有意**：采集增加到足以用 ES256 时会**升级算法**，而 ES256 比 HS256 贵 3 倍
   → 那一下间隔会**跳大一次**（实测：0.08 mW 时 HS256/167s → 0.10 mW 时 ES256/300s）。
   所以"间隔随采集单调下降"只在**同一级别内部**成立；跨级是"便宜的算法换密度"与
   "强的算法换安全"的取舍，本模型按**安全优先（只要还能跟得住人）**选。
   `test_energy.py` 把这个跳跃锁成已知行为，避免下次被当成 bug。
5. 覆盖规则：电量连**一次**上报都不够（`charge < cost`）→ `why="empty"`（现在发不动；
   长期收支可能仍然平衡，两者不矛盾）。
6. **`why="deficit"` 另给归因**（`short_of`）：`"sleep"` = 采集连待机都不够；
   `"listen"` = 供得住待机、供不住监听（**听不成了**，页面/告警据此说清差在哪）。
   没有单个 why 能描述“哪一层缺钱”—— 所以不在 `why` 里堆码，而是另给一个字段（同理于
   服务端从（级别+电量）**推导** `degraded_reason` 而不是新增报文字段）。

## 覆盖（不断线）—— 取能波动 ≠ 可以夜间停机（2026-09-13 用户定口径）

取能功率会随环境波动（光/运动/温差）—— 那是**换能器的事（物理）**。但 ORPAH 是**找人**：
**夜间/取能低谷恰恰是必须覆盖的时段**（黄金时间含夜，夜里更危险），
“天黑就不工作”**既不是需求、也不许被画成正常循环**。所以模型要回答的是：

1. **缺口里能撑多久** `cover_s` = 储能 ÷（固定开销 + 上报功率 − 缺口期采集）；
2. **要覆盖这个缺口，储能至少要多少** `need_store_mj`；
3. **不够时怎么办 → 降级换覆盖**：先把算法降到 HS256（最便宜）、并把间隔拉到
   `MAX_USEFUL_INTERVAL_S`（再长就跟不住人）→ 给出**降级后能多撑多久**与**还差多少**。
   **绝不为了省电沉默**（沉默 = 丢人）—— 与本模块“下限 L1、永不 L3”是同一条逻辑。
4. 断线是**设计不足**（该出警的），不是“正常作息”：`coverage()` 把 `cover_s` / `gap_short_s` /
   `verdict` 给出去，让服务端与页面能在**断线之前**预警。

取能波动有两种给法（两种都收，见 `coverage()`）：

- **最坏缺口**（`gap_s` + `gap_harvest_mw`，缺省）—— 保守：不假装知道中间过程；
- **实测曲线**（`curve` = `[[t_s, mW], …]` 分段常数，通常 24h）—— 逐段积分，更准；
  曲线是**实测数据**，所以归标定文件（`energy_calib.py` 的 `harvest_curve`），不归策略输入。

## 只算不改

本模块**不碰**客户端/服务端状态，也不改任何报文语义（能量状态沿用 Orpah ID §5 的
`payload.battery_mv`，用户 2026-09-13 选「A. 沿用该字段」）。`drain()` 只做电量推进，
由调用方（模拟器）决定何时用。
"""

# ---- 演示参数（**未按真机实测标定**；见模块头「⚠ 参数」） ----
SLEEP_MW = 0.05                      # 待机功耗（mW）：MCU 深睡眠 + 采集器静态损耗
COST_MJ = {"ES256": 15.0, "HS256": 5.0}   # 单次上报耗电（mJ）：ES256 比 HS256 贵约 3 倍
LISTEN_MJ = 6.0                      # **每次听窗口**耗电（mJ，演示量级：唤醒 + RF 收 + 解码）
LISTEN_INTERVAL_S = 60.0             # **多久听一次**（s）—— 产品选择（不是标定项）：
#                                      听间隔↑ = 下行变慢/发现更慢；模型不替你拍这个板（见模块文档）
GAP_S_DEMO = 12 * 3600.0             # **演示场景**：最长无取能时长（12h）—— 只为了让 demo 一上来就有个
#                                      缺口可看（不是实测值：现场按天气/树木/通风/穿戴遮挡标定）
MIN_INTERVAL_S = 2.0                 # 间隔下限（再快服务器/空口也撑不住，且无意义）
MAX_USEFUL_INTERVAL_S = 300.0        # 间隔上限：超过就"跟不住人"（追踪语义上限）
EMERGENCY_INTERVAL_S = 60.0          # `interval_s=None`（采不敷出）时演示用的"硬撑"间隔：
#                                      调用方想"宁可吃储能也要被听见"时用它 + survive_s() 算能撑多久
STORE_MJ = 2000.0                    # 默认储能容量（mJ）
CHARGE0_MJ = 1500.0                  # 默认初始电量（mJ）
CELL_EMPTY_MV = 3000                 # 电压↔电量映射：空
CELL_FULL_MV = 4200                  # 电压↔电量映射：满（单节锂电/超级电容的演示近似）

LEVEL_ES = "ES256"
LEVEL_HS = "HS256"

# 浮点容差：“恰好等于上限”必须算够用 —— 除法常在边界上给出 300.00000000000006 这种值，
# 没容差的话门槛会随浮点噪声跳档（用户看到的是假象：多一点点采集反而不够用）。
_EPS = 1e-9


def mv_of(charge_mj, store_mj=STORE_MJ, empty_mv=CELL_EMPTY_MV, full_mv=CELL_FULL_MV):
    """电量（mJ）→ 电压（mV，整数）。线性近似，只为让报文里的 `battery_mv` 有意义。

    **反过来说明**：服务端**不**从电压反推"还剩多少电"（电压曲线与电池类型强相关），
    它只用电压做低/临界两档判断（阈值可配）；精确的"还能撑多久"由设备侧模型给
    （页面/审计看 `silence_in_s`）。报文里沿用 `payload.battery_mv`，不新增字段。
    """
    if store_mj <= 0:
        return int(empty_mv)
    frac = max(0.0, min(1.0, float(charge_mj) / float(store_mj)))
    return int(round(empty_mv + (full_mv - empty_mv) * frac))


def plan(harvest_mw, charge_mj, store_mj=STORE_MJ, sleep_mw=SLEEP_MW, cost=None,
         min_interval_s=MIN_INTERVAL_S, max_useful_s=MAX_USEFUL_INTERVAL_S,
         listen_mj=LISTEN_MJ, listen_interval_s=LISTEN_INTERVAL_S):
    """能量 → 可执行策略：用哪个级别、多久报一次、还能撑多久、会不会沉默。

    `listen_interval_s=0` = **不建模监听**（拿来做对照实验：加监听之前的行为可以逐位复现）。

    返回（字段都有明确含义，**无值给 `None` 不给 0**）：

    | 字段 | 含义 |
    |---|---|
    | `level` | 选定的算法级别（`ES256` / `HS256`） |
    | `degraded` / `degraded_reason` | 是否因能量降级 / 成因（`"energy"` 或 `None`） |
    | `interval_s` | 选定策略的可维持上报间隔（秒） |
    | `interval_es256_s` / `interval_hs256_s` | 两个候选级别的间隔（解释"为什么降级"用；`None`=可上报功率不足算不出） |
    | `net_mw` | 净功率（采集 − 待机）—— **含意未变**（兼容）；要解释间隔请看 `usable_mw` |
    | `listen_mw` / `listen_interval_s` / `listen_modeled` | 监听的**平均开销** / 听间隔（回显）/ 是否建模了监听 |
    | `overhead_mw` | 固定开销 = 待机 + 监听（与上报间隔无关） |
    | `usable_mw` | **真正能拿去上报的功率** = 采集 − 固定开销（负值钳到 0） |
    | `report_mw` | 按选定间隔跑时，上报的**平均功率**（`None` = 没有可维持间隔） |
    | `budget_ok` | 当前策略是否收支平衡（`False` = 在吃储能，会走向沉默） |
    | `silence_in_s` | 还能撑多久（秒）；`None` = 收支平衡，不会因没电沉默 |
    | `short_of` | 缺钱在哪一层（仅 `budget_ok=False`）：`"sleep"` / `"listen"` / `None` |
    | `usable` | 是否持续“跟得住人”（间隔 ≤ `max_useful_s` 且收支平衡且电量够发下一条） |
    | `why` | 机器码：`ok` / `degraded_saves` / `too_slow` / `deficit` / `no_energy` / `empty` |

    `interval_s=None` = **没有可维持的间隔**（采集连固定开销都供不住）：调用方应让它**如实沉默**，
    或者（如果选择"宁可吃储能也要被听见"）用 `EMERGENCY_INTERVAL_S` + `survive_s()` 硬撑，
    并把硬撑的截止时间告诉服务端。**听间隔不会被自动拉长**（见模块文档「监听」第 2 条）。
    """
    cost = cost or COST_MJ
    harvest_mw = float(harvest_mw)
    charge_mj = float(charge_mj)
    sleep_mw = float(sleep_mw)
    listen_mj, listen_interval_s = float(listen_mj), float(listen_interval_s)
    if listen_mj < 0 or listen_interval_s < 0:      # 写错就报，不静默当成 0
        raise ValueError("listen_mj / listen_interval_s 不能为负")
    listen_modeled = listen_interval_s > 0
    listen_mw = (listen_mj / listen_interval_s) if listen_modeled else 0.0
    overhead = sleep_mw + listen_mw
    net = harvest_mw - sleep_mw                   # 含意不变（采集 − 待机）
    usable_mw = max(harvest_mw - overhead, 0.0)   # 真正能拿去上报的功率
    c_es, c_hs = float(cost[LEVEL_ES]), float(cost[LEVEL_HS])

    if usable_mw > 0:
        iv_es = max(c_es / usable_mw, min_interval_s)
        iv_hs = max(c_hs / usable_mw, min_interval_s)
        if iv_es <= max_useful_s + _EPS:
            level, interval, why, degraded = LEVEL_ES, iv_es, "ok", False
        elif iv_hs <= max_useful_s + _EPS:
            level, interval, why, degraded = LEVEL_HS, iv_hs, "degraded_saves", True
        else:
            level, interval, why, degraded = LEVEL_HS, iv_hs, "too_slow", True
    else:
        # 采不敷出 → **没有可维持的间隔**（保留 HS256：省一点是一点；不装出能持续的样子）
        level, interval, degraded = LEVEL_HS, None, True
        iv_es = iv_hs = None
        why = "deficit" if harvest_mw > 0 else "no_energy"

    # 缺钱在哪一层（仅采不敷出时有值）：连待机都不够 vs 供得住待机、供不住监听
    short_of = None
    if harvest_mw < sleep_mw:
        short_of = "sleep"
    elif listen_modeled and harvest_mw < overhead:
        short_of = "listen"

    # 收支：按选定间隔跑，缺口多大；有缺口就吃储能 → 还能撑多久
    spend_mw = 0.0 if interval is None else (c_hs if level == LEVEL_HS else c_es) / interval
    deficit = overhead + spend_mw - harvest_mw
    if deficit > 1e-9:
        budget_ok = False
        silence_in_s = (charge_mj / deficit) if charge_mj > 0 else 0.0
    else:
        budget_ok, silence_in_s = True, None

    # 电量连一次上报都不够 → 该沉默了（why 优先级最高：它决定“还能不能说上话”）
    can_afford_next = charge_mj >= (c_hs if level == LEVEL_HS else c_es)
    if not can_afford_next:
        why = "empty"
    usable = bool(interval is not None and interval <= max_useful_s + _EPS
                  and budget_ok and can_afford_next)
    return {
        "level": level,
        "degraded": bool(degraded),
        "degraded_reason": "energy" if degraded else None,
        "interval_s": None if interval is None else round(interval, 2),
        "interval_es256_s": None if iv_es is None else round(max(iv_es, min_interval_s), 2),
        "interval_hs256_s": None if iv_hs is None else round(max(iv_hs, min_interval_s), 2),
        "net_mw": round(net, 4),
        "listen_mw": round(listen_mw, 4),
        "listen_interval_s": (listen_interval_s if listen_modeled else None),
        "listen_modeled": bool(listen_modeled),
        "overhead_mw": round(overhead, 4),
        "usable_mw": round(usable_mw, 4),
        "report_mw": round(spend_mw, 4),
        "budget_ok": bool(budget_ok),
        "silence_in_s": None if silence_in_s is None else round(silence_in_s, 1),
        "short_of": short_of,
        "usable": usable,
        "why": why,
    }


def survive_s(charge_mj, harvest_mw, interval_s, level, store_mj=STORE_MJ,
              sleep_mw=SLEEP_MW, cost=None, listen_mw=0.0):
    """**硬撑**：明知采不敷出，仍然按 `interval_s` 报 —— 还能撑多久（秒）？

    这是“宁可吃储能也要被听见”策略的代价计算（演示里 `plan()` 返回 `interval_s=None` 时用它）。
    返回 `None` = 收支平衡（不会因没电停）；返回 `0.0` = 已经没电。
    收支 = 待机 + **监听** + 上报 − 采集（缺口 > 0 就在吃储能）。

    `listen_mw` 由调用方从 `plan()["listen_mw"]` 拿（或自己算 `listen_mj/listen_interval_s`）——
    **硬撑也得继续听**，否则“还能撑多久”会比实际乐观。
    """
    cost = cost or COST_MJ
    spend_mw = float(cost.get(level, 0.0)) / float(interval_s) if interval_s else 0.0
    deficit = float(sleep_mw) + float(listen_mw) + spend_mw - float(harvest_mw)
    if deficit <= 1e-9:
        return None
    return float(charge_mj) / deficit if charge_mj > 0 else 0.0


def drain(charge_mj, harvest_mw, interval_s, level, dt_s, store_mj=STORE_MJ,
          sleep_mw=SLEEP_MW, cost=None, listen_mw=0.0):
    """推进一拍：返回新的电量（mJ，钳在 [0, store_mj]）。

    收支 = 采集×dt − 待机×dt − **监听×dt** − 上报耗电×（dt/间隔）——**逐拍结算**，
    所以间隔比 dt 长时平均下来才是“每次上报摊一次”。间隔 ≤ 0 视为只付固定开销（不发报）。

    ★ `listen_mw` 必须传（与 `plan()` 同源）：漏了会让**电量推进**比“监听开销”那一步
    算得乐观 —— 而这条电量又会被写成已签的 `battery_mv`，就成了“模型说一套、报文说另一套”。
    """
    cost = cost or COST_MJ
    charge = (float(charge_mj) + float(harvest_mw) * dt_s
              - float(sleep_mw) * dt_s - float(listen_mw) * dt_s)
    if interval_s and interval_s > 0:
        charge -= float(cost.get(level, 0.0)) * (dt_s / float(interval_s))
    return max(0.0, min(float(store_mj), charge))


def _sim_curve(curve, charge_mj, overhead_mw, report_mw):
    """按一条**分段常数**取能曲线推进电量（一个周期）——不断线吗、什么时候断、要多少储能。

    `curve` = `[[t_s, mW], …]`（**时间非递减**、功率 ≥ 0），段内取**左端点**的功率；
    相邻两点时间相同 = 一个**阶跃**（零长度段，不积分）—— 手写与实测曲线都长这样，
    所以允许；时间**倒退**才报错。

    返回：

    | 字段 | 含义 |
    |---|---|
    | `min_charge_mj` / `dead_at_s` | 周期内最低电量 / **断线时刻**（`None` = 没断） |
    | `longest_gap_s` | 最长的一段"净缺口"（采集 < 开销）时长 |
    | `max_drawdown_mj` | **要不断线所需的最小储能** = 累计净收支的**最大回撤**（运行峰值 − 之后的最低点）。不是简单的"最长缺口 × 开销"：中间能回一点的地方会自动减掉 |
    | `cycle_net_mj` / `sustainable` | 一个周期的**净收支** / 是否**可永续**（净收支 ≥ 0） |

    ★ `sustainable=False`（采集不够自给）时，`max_drawdown_mj` 的含义只是"**撑过一个周期**"要的储能 ——
    **再大的储能也只是拖时间**（每个周期都净亏）。这条必须如实传给页面/结论。
    """
    spend = float(overhead_mw) + float(report_mw)
    gain = 0.0            # 累计净收支（>0 = 在充）
    run_max = dd = 0.0    # 运行峰值 / 最大回撤（= 所需储能）
    charge = float(charge_mj)
    dead_at, min_charge = None, charge
    gap = longest = 0.0
    net_mj = 0.0                                     # 一个周期的净收支（>0 = 能永续）
    for i in range(len(curve) - 1):
        dt = float(curve[i + 1][0]) - float(curve[i][0])
        if dt < 0:
            raise ValueError("曲线时间不能倒退（相等 = 阶跃，允许）")
        if dt == 0:
            continue                                  # 阶跃点：不积分，下一段直接用新值
        net = float(curve[i][1]) - spend              # >0 = 这一段在充电
        net_mj += net * dt
        gain += net * dt
        run_max = max(run_max, gain)
        dd = max(dd, run_max - gain)                  # 最大回撤 = 要撑过的最深一次亏空
        if net < 0:
            gap += dt
            longest = max(longest, gap)
        else:
            gap = 0.0
        if dead_at is None:
            before = charge                           # 这一段开始前的电量
            charge += net * dt
            if charge <= 0:                           # 断线：在段内线性推到 0 的时刻
                min_charge = 0.0
                dead_at = (float(curve[i][0]) + before / (-net)) if net < 0 else float(curve[i][0])
            else:
                min_charge = min(min_charge, charge)
    return {"min_charge_mj": round(min_charge, 2), "dead_at_s": (None if dead_at is None else round(dead_at, 1)),
            "max_drawdown_mj": round(dd, 2), "longest_gap_s": round(longest, 1),
            # ★ 周期净收支 < 0 时，“需要的储能”这个说法不成立（再大也只是拖时间）→ 如实给出去
            "cycle_net_mj": round(net_mj, 1), "sustainable": net_mj >= -_EPS}


def coverage(p, charge_mj, gap_s=None, gap_harvest_mw=0.0, curve=None, cost=None,
             max_useful_s=MAX_USEFUL_INTERVAL_S):
    """缺口（取能掉落/夜间）里**会不会断线** + 不够时**降级换覆盖**能多撑多久。

    `p` = `plan()` 的结果（常态策略：级别/间隔/固定开销/上报功率都从它来，单一源）。
    两种给法：**最坏缺口** `gap_s`（秒）+ `gap_harvest_mw`（缺口期采集，通常 0）；
    或者 **实测曲线** `curve`（`[[t_s, mW], …]`）。两个都不给 / `gap_s<=0` → `verdict="none"`
    （不建模，也**不编数**）。`gap_s<0` 抛 `ValueError`（写错就报）。

    返回：

    | 字段 | 含义 |
    |---|---|
    | `verdict` | `ok`（常态策略就够）/ `degrade`（降级+拉长间隔后够）/ `short`（怎么着都不够 → 设计不足）/ `none`（没建模） |
    | `gap_deficit_mw` | 缺口期净开销（固定开销 + 上报功率 − 缺口期采集） |
    | `cover_s` | 按常态策略能撑多久（缺口期收支平衡 → `None`，不是 0） |
    | `covers` / `gap_short_s` | 够不够 / 还差多少秒 |
    | `need_store_mj` | **要覆盖这个缺口所需的最小储能**（可直接拿去选型/采购） |
    | `need_harvest_mw` | 不靠储能就得有的采集功率（自给自足的线） |
    | `degraded` | 降级换覆盖（HS256 + 间隔拉到上限）：`level/interval_s/deficit_mw/cover_s/covers/gap_short_s/extra_s/need_store_mj`；
      ★ `extra_s`（降级多撑的时间）**可能为负** —— 常态本来就不报（`interval_s=None`）时，
      “降级”等于把原本不花的报账加上去，反而更早断线。那种情形**不该按“降级换覆盖”理解**，
      要的是加大储能/取能（页面得把这个区分写出来）。 |
    | `curve` | 有曲线时的积分结果：`min_charge_mj/dead_at_s/max_drawdown_mj/longest_gap_s` |

    `verdict` 只描述**能不能不断线**，不描述“能不能维持高质量定位”—— 降级换覆盖是拿
    **信任度与更新频率**换**不断线**（HS256 弱一级、间隔拉到 300s 是最慢档），页面要把这个取舍写出来。
    """
    cost = cost or COST_MJ
    charge_mj = float(charge_mj)
    overhead = float(p["overhead_mw"])
    report = float(p.get("report_mw") or 0.0)          # 常态就沉默时 = 0（缺口里只付固定开销）
    gap_harvest_mw = float(gap_harvest_mw or 0.0)
    deg_interval = float(max_useful_s)
    deg_report = float(cost[LEVEL_HS]) / deg_interval

    def _leg(rep):
        """一段策略在缺口里的命（收支平衡 → cover_s=None 而不是 0）。"""
        deficit = overhead + rep - gap_harvest_mw
        if deficit <= _EPS:
            return deficit, None
        return deficit, (charge_mj / deficit if charge_mj > 0 else 0.0)

    out = {"verdict": "none", "gap_s": None, "gap_harvest_mw": gap_harvest_mw,
           "gap_deficit_mw": None, "cover_s": None, "covers": None, "gap_short_s": None,
           "need_store_mj": None, "need_harvest_mw": round(overhead + report, 4),
           "degraded": None, "curve": None}

    if curve is not None:
        pts = [[float(t), float(mw)] for t, mw in curve]
        if len(pts) < 2:
            raise ValueError("取能曲线至少要 2 个点")
        if any(mw < 0 for _, mw in pts):
            raise ValueError("取能曲线的功率不能为负")
        base = _sim_curve(pts, charge_mj, overhead, report)
        degb = _sim_curve(pts, charge_mj, overhead, deg_report)
        ok = base["dead_at_s"] is None
        deg_ok = degb["dead_at_s"] is None
        out.update({
            "verdict": "ok" if ok else ("degrade" if deg_ok else "short"),
            "gap_s": round(pts[-1][0] - pts[0][0], 1),
            "cover_s": None,            # 曲线路径的答案是“什么时候断”（curve.dead_at_s），不是一把秒数
            "covers": ok,
            "curve": {"n": len(pts), "period_s": round(pts[-1][0] - pts[0][0], 1), **base},
            "need_store_mj": base["max_drawdown_mj"],
            "degraded": {"level": LEVEL_HS, "interval_s": deg_interval,
                         "deficit_mw": round(overhead + deg_report - gap_harvest_mw, 4),
                         "cover_s": None, "covers": deg_ok, "gap_short_s": None,
                         "extra_s": None,
                         "need_store_mj": degb["max_drawdown_mj"],
                         "curve": {"n": len(pts), "period_s": round(pts[-1][0] - pts[0][0], 1),
                                   **degb}},
        })
        return out

    gap_s = None if gap_s is None else float(gap_s)
    if gap_s is not None and gap_s < 0:
        raise ValueError("gap_s 不能为负（0 = 不建模缺口）")
    if not gap_s:
        return out                                   # 不建模：全 None，不编数

    d, cover_s = _leg(report)
    dd, deg_cover_s = _leg(deg_report)
    # `cover_s is None` = 缺口期收支平衡（不会因缺口没电）→ **算撑得过**，不当 False
    covers = (cover_s is None) or (cover_s >= gap_s - _EPS)
    deg_covers = (deg_cover_s is None) or (deg_cover_s >= gap_s - _EPS)
    out.update({
        "verdict": "ok" if covers else ("degrade" if deg_covers else "short"),
        "gap_s": round(gap_s, 1),
        "gap_deficit_mw": round(d, 4),
        "cover_s": None if cover_s is None else round(cover_s, 1),
        "covers": covers,
        "gap_short_s": None if cover_s is None else round(max(0.0, gap_s - cover_s), 1),
        "need_store_mj": round(d * gap_s, 1),
        "degraded": {"level": LEVEL_HS, "interval_s": deg_interval,
                     "deficit_mw": round(dd, 4),
                     "cover_s": None if deg_cover_s is None else round(deg_cover_s, 1),
                     "covers": deg_covers,
                     "gap_short_s": None if deg_cover_s is None else round(max(0.0, gap_s - deg_cover_s), 1),
                     "extra_s": (None if (cover_s is None or deg_cover_s is None)
                                 else round(deg_cover_s - cover_s, 1)),
                     "need_store_mj": round(dd * gap_s, 1)},
    })
    return out


def sweep(harvests, charge_mj=CHARGE0_MJ, store_mj=STORE_MJ, **kw):
    """扫采集功率（能量轴的横轴）→ 每个点的策略表 + **头条数字**。

    返回 `{"rows": [...], "min_harvest_mw": X|None, "n": len(rows)}`；
    `min_harvest_mw` = 最小"够用"的采集功率（第一行 `usable=True`）——
    这是这一项真正想回答的问题：**要多少采集功率才持续跟得住人**。
    全都不够用 → `None`（不编一个数）。
    """
    rows = []
    for h in harvests:
        p = plan(h, charge_mj, store_mj, **kw)
        rows.append({"harvest_mw": round(float(h), 3), **p})
    first = next((r["harvest_mw"] for r in rows if r["usable"]), None)
    return {"rows": rows, "min_harvest_mw": first, "n": len(rows)}


def axis(n=13, h_max=12.0, charge_mj=CHARGE0_MJ, store_mj=STORE_MJ, **kw):
    """默认能量轴：0…h_max 均匀 n 点（含 0）。页面直接画这张表。"""
    n = max(2, int(n))
    return sweep([h_max * i / (n - 1) for i in range(n)],
                 charge_mj=charge_mj, store_mj=store_mj, **kw)
