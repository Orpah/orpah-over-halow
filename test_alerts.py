#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_alerts.py — 告警规则单测（纯内存，不依赖 IoTDB / 网页）

运行：python test_alerts.py
"""
import os
from collections import deque
from types import SimpleNamespace

# 本文件要断言「默认阈值」（如 B 的 24h），所以**先清掉环境里可能存在的 ORPAH_ALERT_***
# 再 import alerts —— 否则本机 shell 里设过阈值（例如验证 B 方案时留下的
# ORPAH_ALERT_CASE_HANDLED_SEC=5）会让一堆默认值断言假失败（2026-09-12 实际踩到）。
for _k in [k for k in os.environ if k.startswith("ORPAH_ALERT_")]:
    os.environ.pop(_k)

import alerts as alr       # noqa: E402
import cases as cs         # noqa: E402
import energy as enmod   # 注意：下面第 327 行有个测试助手也叫 `en`，别把模块名占上
import registry as reg     # noqa: E402

# 长未上报的时间尺度：**跟着设计常态周期走**（2026-09-14）——
# 那时它不是拍的 30 s，而是 2.5×60 s = 150 s（改周期就跟着变）。
GAP = int(2.5 * enmod.NORMAL_INTERVAL_S) + 50      # 落在 warn 带里的一个间隔（200s）

NOW = 1_800_000_000     # 固定"现在"，测试可复现
FAILS = []


def dev(sn, status=reg.STATUS_ACTIVE, last_seen=None):
    return SimpleNamespace(sn=sn, status=status, last_seen=last_seen)


def regis(*devs):
    return SimpleNamespace(devices={d.sn: d for d in devs})


def case(cid, status=cs.CASE_OPEN, created=NOW, person_id="P001", handler="",
         handled_at=None):
    # handler/handled_at 是「处置态」维度（2026-09-12 新增）：真 Case 有，假 Case 也必须给 ——
    # 假对象**少**给属性会让测试直接崩（多给属性则会掩盖真实的 AttributeError，见 case_mgr 注释）
    return SimpleNamespace(case_id=cid, person_id=person_id,
                           status=status, created=created, handler=handler,
                           handled_at=handled_at)


def case_mgr(*cs_):
    """假的 CaseManager：**只**提供 open_cases()。
    刻意不提供 CASE_OPEN —— 真 CaseManager 也没有这个属性（常量在 cases 模块上），
    假对象多给一个属性会把真实的 AttributeError 掩盖掉（这次就踩了）。"""
    return SimpleNamespace(
        open_cases=lambda: [c for c in cs_ if c.status in (cs.CASE_OPEN, cs.CASE_FOUND)])


def rep(accepted, t=NOW):
    return {"accepted": accepted, "t": t}


def kinds(alerts):
    return [a["kind"] for a in alerts]


def check(name, cond, detail=None):
    """`detail` 只在**失败**时打印（把实测值/样本一起打出来，省得再跑一遍去猜）——
    与 `test_energy.ck()` 同约定。"""
    if cond:
        print("PASS  " + name)
    else:
        print("FAIL  " + name + ("  " + str(detail) if detail is not None else ""))
        FAILS.append(name)


# ---- 规则 1：长未上报 ------------------------------------------------------
# 口径（2026-09-12 复核修正）：工作态 = 启用 **或 走失**，且**曾经上报过**。
# 走失者的追踪器最该盯（掉线往往就是找不到人的原因）；停用/报废不盯。
r = regis(dev("A", last_seen=NOW - GAP),                        # 启用+超时 → 应告警
          dev("B", last_seen=NOW - 10),                         # 启用+正常 → 不告警
          dev("C", last_seen=None),                             # 从未上报 → 不告警
          dev("D", status=reg.STATUS_LOST, last_seen=NOW - GAP),   # 走失中+超时 → **应告警**
          dev("E", status=reg.STATUS_DISABLED, last_seen=NOW - GAP),  # 停用 → 不告警
          dev("F", status=reg.STATUS_SCRAPPED, last_seen=NOW - GAP))  # 报废 → 不告警
a = alr.evaluate(r, case_mgr(), deque(), now=NOW)
check("长未上报：启用/走失中的都报，停用/报废不报",
      [x["sn"] for x in a] == ["A", "D"])
check("长未上报：带 gap 且等级 warn", a[0]["gap"] == GAP and a[0]["level"] == "warn")

# ---- C 方案：按持续时长分级（2026-09-12）----------------------------------
# 起步 > 2.5×设计常态周期（150s）是 warn；沉默超过 NO_REPORT_CRIT_SEC（默认 5×=300s）升 crit。
NRC = alr.NO_REPORT_CRIT_SEC
check("★ 两档阈值都**随设计常态周期成比例**（2.5×/5× 60s = 150/300s），"
      "不是拍的数字（2026-09-14）",
      (alr.NO_REPORT_SEC, alr.NO_REPORT_CRIT_SEC)
      == (int(2.5 * enmod.NORMAL_INTERVAL_S), int(5 * enmod.NORMAL_INTERVAL_S)))
check("C：默认 no_report 升级阈值 300s（ORPAH_ALERT_NO_REPORT_CRIT_SEC）", NRC == 300)
check("C：默认 case_handled 升级阈值 48h（ORPAH_ALERT_CASE_HANDLED_CRIT_SEC；"
      "注意与起步阈值 CASE_HANDLED_SEC=24h 是两个不同的量）",
      alr.CASE_HANDLED_CRIT_SEC == 172800 and alr.CASE_HANDLED_SEC == 86400)


def lv(alerts, sn):
    return [x["level"] for x in alerts if x.get("sn") == sn]


a = alr.evaluate(regis(dev("A", last_seen=NOW - (NRC - 1))), case_mgr(), deque(), now=NOW)
check("C：刚过起步但未到升级点 → warn", lv(a, "A") == ["warn"])
a = alr.evaluate(regis(dev("A", last_seen=NOW - (NRC + 1))), case_mgr(), deque(), now=NOW)
check("C：超过升级点 → crit", lv(a, "A") == ["crit"])
a = alr.evaluate(regis(dev("A", last_seen=NOW - NRC)), case_mgr(), deque(), now=NOW)
check("C：刚好等于升级点 → 仍 warn（用 > 比较）", lv(a, "A") == ["warn"])
# 停用/报废即使沉默很久也不报（分级不改变“哪些设备该盯”）
a = alr.evaluate(regis(dev("E", status=reg.STATUS_DISABLED, last_seen=NOW - 99999),
                       dev("F", status=reg.STATUS_SCRAPPED, last_seen=NOW - 99999)),
                 case_mgr(), deque(), now=NOW)
check("C：分级不影响“盯谁”（停用/报废沉默很久也不报）", a == [])

# ---- B + C 合用：接手后 24h warn，48h 升 crit ----
HC = alr.CASE_HANDLED_SEC
HCC = alr.CASE_HANDLED_CRIT_SEC

def case_lv(alerts):
    return [(x["kind"], x["level"]) for x in alerts]


a = alr.evaluate(regis(), case_mgr(case("C001", handler="张警官",
                                        handled_at=NOW - HC - 10)), deque(), now=NOW)
check("B+C：接手后刚超 24h → warn", case_lv(a) == [("case_handled_overtime", "warn")])
a = alr.evaluate(regis(), case_mgr(case("C001", handler="张警官",
                                        handled_at=NOW - HCC - 10)), deque(), now=NOW)
check("B+C：接手后超 48h → crit", case_lv(a) == [("case_handled_overtime", "crit")])

# ---- 不该分级的两条：定性问题，一发生就是 crit（守住 A 方案的语义）----
check("C：case_overtime（无人接手）仍是恒 crit",
      [x["level"] for x in alr.evaluate(regis(), case_mgr(case("C001", created=NOW - 200)),
                                         deque(), now=NOW)] == ["crit"])
check("C：sig_fail_rate 仍是恒 crit",
      [x["level"] for x in alr.evaluate(regis(), case_mgr(),
                                         deque([rep(False)] * 5), now=NOW)] == ["crit"])

# 分级与排序互动：crit 的 no_report 应排在 warn 的 no_report 之前
a = alr.evaluate(regis(dev("A", last_seen=NOW - (NRC - 1)),        # warn
                       dev("B", last_seen=NOW - (NRC + 1))),      # crit
                 case_mgr(), deque(), now=NOW)
check("C：排序 crit 在前（同 kind 不同等级也遵守）",
      [(x["sn"], x["level"]) for x in a] == [("B", "crit"), ("A", "warn")])

# 走失设备的告警单独确认（避免以后又被“非启用就不报”改回去）
a_lost = alr.evaluate(regis(dev("D", status=reg.STATUS_LOST, last_seen=NOW - GAP)),
                      case_mgr(), deque(), now=NOW)
check("长未上报：走失中的跟踪器掉线要报（2026-09-12 修正）",
      [x["sn"] for x in a_lost] == ["D"])
check("长未上报：停用的设备不报",
      alr.evaluate(regis(dev("E", status=reg.STATUS_DISABLED, last_seen=NOW - GAP)),
                   case_mgr(), deque(), now=NOW) == [])

# 边界：刚好等于阈值不算超时（用 > 比较）
a = alr.evaluate(regis(dev("A", last_seen=NOW - alr.NO_REPORT_SEC)), case_mgr(), deque(), now=NOW)
check("长未上报：等于阈值不告警", a == [])

# ---- 规则 2：走失超时 ------------------------------------------------------
a = alr.evaluate(regis(), case_mgr(case("C001", created=NOW - 300),                    # 超时
                                   case("C002", status=cs.CASE_FOUND, created=NOW - 300)),  # 已发现
                 deque(), now=NOW)
check("走失超时：只报 open 的案件", [x["case_id"] for x in a] == ["C001"])
check("走失超时：等级 crit", a[0]["level"] == "crit")
check("走失超时：case_key 可去重", a[0]["key"] == "case_overtime:C001")

# 处置态（2026-09-12 用户定 A 方案）：超时 **且无人接手** 才告警 ——
# 否则只要案件还开着就永远 crit，红点恒亮被淹没（分不出“刚超时没人管”与“已在找人”）
a = alr.evaluate(regis(),
                 case_mgr(case("C001", created=NOW - 300),                       # 无人接手 → 告警
                          case("C002", created=NOW - 300, handler="张警官")),  # 已接手 → 不告警
                 deque(), now=NOW)
check("处置态：已接手的超时案件不告警", [x["case_id"] for x in a] == ["C001"])

# 接手 → 红点消失（同一案件前后对比）
a = alr.evaluate(regis(), case_mgr(case("C001", created=NOW - 300, handler="张警官")),
                 deque(), now=NOW)
check("处置态：接手后该条告警消失", kinds(a) == [])
# 取消接手 → 告警回来（handler 清空即回到未处置）
a = alr.evaluate(regis(), case_mgr(case("C001", created=NOW - 300, handler="")),
                 deque(), now=NOW)
check("处置态：取消接手后告警回来", kinds(a) == ["case_overtime"])

# ---- 规则 2b（B 方案）：接手后仍超时 → 再提醒 -------------------------------
# 语义：从**接手时刻**起算。默认 86400s（真实 24h），演示把阈值调小看效果。
HOV = alr.CASE_HANDLED_SEC
check("B：默认接手阈值是 24h", HOV == 86400)

a = alr.evaluate(regis(),
                 case_mgr(case("C001", created=NOW - 99999, handler="张警官",
                               handled_at=NOW - HOV - 10)),
                 deque(), now=NOW)
check("B：接手超阈值 → 报 case_handled_overtime", kinds(a) == ["case_handled_overtime"])
check("B：级别 warn（不盖过“没人接”的 crit）", a[0]["level"] == "warn")
check("B：since = 接手时刻（不是立案时刻）", a[0]["since"] == NOW - HOV - 10)
check("B：带 handler / case_id / gap（前端模板要用）",
      a[0]["handler"] == "张警官" and a[0]["case_id"] == "C001"
      and a[0]["gap"] == HOV + 10)
check("B：key 可与 A 区分", a[0]["key"] == "case_handled_overtime:C001")

# 刚好等于阈值不报（用 > 比较）；没超过接手阈值也不报（A 方案的“不扰”行为保留）
a = alr.evaluate(regis(), case_mgr(case("C001", handler="张警官",
                                        handled_at=NOW - HOV)), deque(), now=NOW)
check("B：等于接手阈值不报", kinds(a) == [])
a = alr.evaluate(regis(), case_mgr(case("C001", created=NOW - 99999,
                                        handler="张警官", handled_at=NOW - 10)),
                 deque(), now=NOW)
check("B：刚接手（未超阈值）不报，即使立案已很久", kinds(a) == [])

# 已发现或已结案的案件不算（open_cases 已过滤 found，这里守一下别回归）
a = alr.evaluate(regis(), case_mgr(case("C001", status=cs.CASE_FOUND,
                                        handler="张警官", handled_at=NOW - HOV - 10)),
                 deque(), now=NOW)
check("B：已发现的案件不报", kinds(a) == [])

# 老库/手工对象没有 handled_at → 回落到 created（不能因为缺属性就崩或默默不报）
a = alr.evaluate(regis(), case_mgr(case("C001", created=NOW - HOV - 10,
                                        handler="张警官", handled_at=None)),
                 deque(), now=NOW)
check("B：handled_at 缺失时回落到 created", kinds(a) == ["case_handled_overtime"]
      and a[0]["since"] == NOW - HOV - 10)

# A 与 B 同时存在：无人接手的 crit 应排在已接手的 warn 之前
a = alr.evaluate(regis(),
                 case_mgr(case("C001", created=NOW - 300),                       # A: crit
                          case("C002", created=NOW - 300, handler="张警官",
                               handled_at=NOW - HOV - 10)),              # B: warn
                 deque(), now=NOW)
check("A+B：crit 在前、warn 在后", [x["level"] for x in a] == ["crit", "warn"])
check("A+B：两种 kind 各自独立", sorted(kinds(a)) == ["case_handled_overtime", "case_overtime"])

# ---- 规则 3：签名失败率 ----------------------------------------------------
a = alr.evaluate(regis(), case_mgr(), deque([rep(False)] * 3 + [rep(True)] * 2), now=NOW)
check("签名失败率：3/5 被拒 → 告警", "sig_fail_rate" in kinds(a))
check("签名失败率：附 n/total", a and a[0]["n"] == 3 and a[0]["total"] == 5)

a = alr.evaluate(regis(), case_mgr(), deque([rep(False)] * 4), now=NOW)
check("签名失败率：样本不足窗口 → 不告警", "sig_fail_rate" not in kinds(a))

a = alr.evaluate(regis(), case_mgr(), deque([rep(False)] * 2 + [rep(True)] * 3), now=NOW)
check("签名失败率：2/5 未超阈 → 不告警", "sig_fail_rate" not in kinds(a))

# ---- 规则 4：设备时钟偏移/漂移（§5.5 深化）--------------------------------
def clock_est(off=None, drift=None, n=10, ok=True, since=None):
    return {"offset": off, "spread": 0.0, "drift_ppm": drift, "n": n, "span": 60.0,
            "ok": ok, "breach_since": since}


a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, clock={"A": clock_est(off=90)})
check("时钟：偏移 90s > 30s → id_clock 告警", kinds(a) == ["id_clock"])
check("时钟：级别 warn（时间可信度问题，不是走失）", a and a[0]["level"] == "warn")
check("时钟：带 offset_sec / n 供页面显示",
      a and a[0]["offset_sec"] == 90.0 and a[0]["n"] == 10)
check("时钟：无越界起点 → since=now（不报 1970）", a and a[0]["since"] == NOW)

a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, clock={"A": clock_est(off=90, since=NOW - 500)})
check("时钟：有 breach_since → since 用它（页面“持续 X”）", a and a[0]["since"] == NOW - 500)

a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, clock={"A": clock_est(off=29.9)})
check("时钟：偏移未超阈 → 不告警", a == [])

a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, clock={"A": clock_est(off=0, drift=500)})
check("时钟：仅漂移超阈（500ppm > 200）也告警，且 offset_sec 如实给 0",
      kinds(a) == ["id_clock"] and a[0]["drift_ppm"] == 500.0 and a[0]["offset_sec"] == 0.0)

a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, clock={"A": clock_est(off=0, drift=None)})
check("时钟：样本跨度不够（drift=None）+ 偏移正常 → 不告警（宁可不报）", a == [])

a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, clock={"A": clock_est(off=999, ok=False, n=2)})
check("时钟：样本不足（ok=False）→ 即使 offset 很大也不告警", a == [])

a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW)
check("时钟：不传 clock → 完全不出时钟告警", a == [])

a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW,
                 clock={"A": clock_est(off=90), "B": clock_est(off=-120, since=NOW - 9)})
check("时钟：多设备各自成条（分别挂在各自 SN 上）",
      sorted(x["sn"] for x in a) == ["A", "B"] and all(x["kind"] == "id_clock" for x in a))
check("时钟：负偏移取绝对值（-120s 也超阈，offset_sec 保留符号）",
      any(x["sn"] == "B" and x["offset_sec"] == -120.0 for x in a))

# ---- 规则 6：能力声明不一致（cap.rtc=true 却给了不可用的 ts）----------------
# 口径（2026-09-13）：只吃**已签**声明（`cap_rtc is True`）；「声明无 RTC + ts=0」是正常，
# 不告警。状态型问题 → 同一台只出一条、窗口内没再犯自动消警。
def crep(cap_rtc, ts_ok, accepted=True, t=NOW, src="server", sn=None, ts_raw=0):
    d = {"accepted": accepted, "t": t, "cap_rtc": cap_rtc, "ts_ok": ts_ok,
         "ts_src": src, "ts": ts_raw, "ts_eff": t}
    if sn:
        d["sn"] = sn
    return d


a = alr.evaluate(regis(), case_mgr(), deque([crep(True, False, sn="A")]), now=NOW)
check("能力：声明有 RTC 却送 ts=0（服务器代填时间）→ id_cap_mismatch warn",
      kinds(a) == ["id_cap_mismatch"] and a[0]["level"] == "warn" and a[0]["sn"] == "A")
check("能力：告警带 ts_src（页面能解释“时间是谁填的”）",
      a[0]["ts_src"] == "server" and a[0]["ts_raw"] == 0)

a = alr.evaluate(regis(), case_mgr(), deque([crep(True, True, sn="A", src="device")]), now=NOW)
check("能力：声明有 RTC 且 ts 可用 → 不告警（正常）", a == [])

a = alr.evaluate(regis(), case_mgr(), deque([crep(False, False, sn="A")]), now=NOW)
check("能力：声明**无 RTC** + ts=0 → **不**告警（免电池终端的预期行为）", a == [])

a = alr.evaluate(regis(), case_mgr(), deque([crep(None, False, sn="A")]), now=NOW)
check("能力：**未声明** + ts=0 → 不告警（老设备/老固件，沿用老行为）", a == [])

a = alr.evaluate(regis(), case_mgr(), deque([crep(True, False, accepted=False, sn="A")]), now=NOW)
check("能力：验签被拒的上报不算（声明不可信，不能凭它报设备故障）", a == [])

a = alr.evaluate(regis(), case_mgr(),
                 deque([crep(True, False, t=NOW - 400, sn="A")]), now=NOW)
check(f"能力：超出消警窗口（> {alr.CAP_MISMATCH_SEC}s 没再犯）→ 自动消警", a == [])

a = alr.evaluate(regis(), case_mgr(),
                 deque([crep(True, False, t=NOW - 5, sn="B"), crep(True, False, t=NOW, sn="A")]),
                 now=NOW)
check("能力：同一台只出一条（最近一次），多台各自成条",
      sorted(x["sn"] for x in a) == ["A", "B"] and len(a) == 2)
check("能力：同一台取**最近**一次的 since（deque 最新在前）",
      [x["since"] for x in a if x["sn"] == "A"] == [NOW])
check("能力：阈值可配（cap_mismatch_sec=0 → 1 秒前的记录即过期不报）",
      alr.evaluate(regis(), case_mgr(), deque([crep(True, False, t=NOW - 1, sn="A")]),
                   now=NOW, cap_mismatch_sec=0) == [])
check("能力：窗口边界含（age=100 且 cap_mismatch_sec=100 → 仍报，与 id_degraded 同口径）",
      kinds(alr.evaluate(regis(), case_mgr(), deque([crep(True, False, t=NOW - 100, sn="A")]),
                         now=NOW, cap_mismatch_sec=100)) == ["id_cap_mismatch"])

# ---- 规则 7：能量（低电 warn / 耗尽 crit）+ 沉默归因分流（2026-09-13）----
def en(mv, sil=None):
    return {"mv": mv, "silence_in_s": sil, "level": "HS256", "degraded_reason": "energy"}


a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, energy={"A": en(3600)})
check("能量：电量正常 → 不告警", a == [])
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, energy={"A": en(alr.ENERGY_LOW_MV)})
check("能量：低电边界（=3300mV）→ id_energy warn",
      kinds(a) == ["id_energy"] and a[0]["level"] == "warn")
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, energy={"A": en(alr.ENERGY_OUT_MV)})
check("能量：耗尽边界（=3100mV）→ id_energy crit",
      kinds(a) == ["id_energy"] and a[0]["level"] == "crit")
check("能量：告警带 mv 与 silence_in_s（页面可直接显示“还能报 X 秒”）",
      a[0]["mv"] == alr.ENERGY_OUT_MV and a[0]["silence_in_s"] is None)
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, energy={"A": en(None)})
check("能量：没报过电量（mv=None）→ 不猜、不告警", a == [])
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, energy={"A": en(3200, sil=120.0)})
check("能量：低电区间 → warn 且带上倒计时",
      kinds(a) == ["id_energy"] and a[0]["silence_in_s"] == 120.0)
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW)
check("能量：不传 energy → 不评估（与 clock 同约定）", a == [])
check("能量：阈值可配（3400mV 默认不报；energy_low_mv=3500 时报）",
      kinds(alr.evaluate(regis(), case_mgr(), deque(), now=NOW,
                         energy={"A": en(3400)})) == []
      and kinds(alr.evaluate(regis(), case_mgr(), deque(), now=NOW,
                             energy={"A": en(3400)}, energy_low_mv=3500)) == ["id_energy"])

# 分流：沉默 + 低电 → no_report_energy（warn，**不**升 crit）；电量充足突然沉默 → no_report（crit）
# 注意：低电/耗尽时规则 7 总会另出一条 `id_energy`（设备自报电量低是独立事实），
#       所以这里断言的是「**不再出 `no_report`**」——那才是分流要防的误判（把没电当被藏）。
r_sil = regis(dev("A", last_seen=NOW - 1000))
a = alr.evaluate(r_sil, case_mgr(), deque(), now=NOW, energy={"A": en(3200, sil=60.0)})
check("分流：沉默 + 低电（未耗尽）→ 出 no_report_energy、**不**出 no_report",
      "no_report_energy" in kinds(a) and "no_report" not in kinds(a))
check("分流：该条是 warn（不升级 crit，等它取能）",
      [x for x in a if x["kind"] == "no_report_energy"][0]["level"] == "warn")
check("分流：该条带 gap/mv/silence_in_s（页面能解释“疑似没电”）",
      [(x["gap"], x["mv"], x["silence_in_s"]) for x in a
       if x["kind"] == "no_report_energy"] == [(1000, 3200, 60.0)])
a = alr.evaluate(r_sil, case_mgr(), deque(), now=NOW, energy={"A": en(4000)})
check("分流：沉默 + 电量充足 → 仍是 no_report（且超 crit 阈值 → crit，这才是最该出警的）",
      kinds(a) == ["no_report"] and a[0]["level"] == "crit")
a = alr.evaluate(r_sil, case_mgr(), deque(), now=NOW)
check("分流：不传 energy → 行为与以前完全一致（no_report）", kinds(a) == ["no_report"])
# 分流只看“最后一签上报的电量”，不看设备自称的降级级别（避免“没电”被用在无能量信息的设备上）
a = alr.evaluate(r_sil, case_mgr(), deque(), now=NOW, energy={"B": en(4000)})
check("分流：能量快照里没有该设备 → 不当作没电（不报 no_report_energy）",
      kinds(a) == ["no_report"])
# 沉默且已耗尽时，两条同时出（描述两件事，数据不同，不合并）：
# 一条是「它自己说没电了」（设备视角），一条是「它不再吭声且最后信息是低电」（运维视角）。
a = alr.evaluate(r_sil, case_mgr(), deque(), now=NOW, energy={"A": en(3000, sil=60.0)})
check("分流：沉默 + 已经耗尽（低于 3100mV）→ no_report_energy + id_energy 两条",
      sorted(kinds(a)) == ["id_energy", "no_report_energy"])
check("分流：两条的严重度不同（沉默那条反而更轻）",
      {x["kind"]: x["level"] for x in a} == {"no_report_energy": "warn", "id_energy": "crit"})

# ---- 规则 7b：覆盖（不断线）不足（2026-09-13，SPEC §5.2 E4③）----------------
# 口径：取能波动 ≠ 允许夜间停机；覆盖不了 = **设计不足**（要处置的告警），不是作息。
# 判据直接取模型算好的 verdict（**本模块不重算**）：degrade → warn / short → crit /
# ok · none → 不报（没算过的事不报）。
def cov(verdict, **kw):
    d = {"mv": 4000, "since": NOW - 5,
         "cover": {"verdict": verdict, "gap_s": 43200.0, "cover_s": 3000.0,
                   "gap_short_s": 40200.0, "need_store_mj": 21600.0,
                   "degraded": {"covers": False, "cover_s": 9000.0,
                                "need_store_mj": 7200.0}, "curve": None}}
    d["cover"].update(kw.pop("cover", {}))
    d.update(kw)
    return d


a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, energy={"A": cov("ok")})
check("覆盖：verdict=ok（够）→ 不告警", a == [])
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, energy={"A": cov("none")})
check("覆盖：verdict=none（没建模缺口）→ 不报（**没算过的事不报**）", a == [])
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, energy={"A": cov("degrade")})
check("覆盖：degrade（常态不够、降级换覆盖够）→ warn",
      kinds(a) == ["id_cover_short"] and a[0]["level"] == "warn")
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, energy={"A": cov("short")})
check("覆盖：short（降级也不够、会在缺口里断线）→ crit",
      kinds(a) == ["id_cover_short"] and a[0]["level"] == "crit")
check("覆盖：告警带可执行数据（缺口/能撑/还差/需要储能/降级那组）",
      (a[0]["gap_s"], a[0]["cover_s"], a[0]["gap_short_s"], a[0]["need_store_mj"],
       a[0]["deg_covers"], a[0]["deg_cover_s"], a[0]["deg_need_store_mj"])
      == (43200.0, 3000.0, 40200.0, 21600.0, False, 9000.0, 7200.0))
check("覆盖：key 带对象（前端据此判断“是不是新告警”）", a[0]["key"] == "id_cover_short:A")
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW,
                 energy={"A": cov("short", cover={"curve": {"longest_gap_s": 21600.0,
                                                           "dead_at_s": 3000.0,
                                                           "sustainable": True}})})
check("覆盖：有实测曲线时把「最长缺口 / 预计断线时刻 / 是否可永续」一并带出去",
      (a[0]["curve_gap_s"], a[0]["dead_at_s"], a[0]["sustainable"])
      == (21600.0, 3000.0, True))
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, energy={"A": {"mv": 4000}})
check("覆盖：快照里没有 cover（老调用方/未开模型）→ 不评估本条", a == [])
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, energy={"A": cov("short", mv=None)})
check("覆盖：只吃**已签**上报里的电量 —— 没报过电量时不报（无值就是无值）", a == [])


# ---- 规则 8：限频丢弃（2026-09-13，§5.8）------------------------------------
# 只陈述事实、**不归因**（大流量 ≠ 有人在攻击）；窗口内有丢弃就报，窗口外自动消警。
def rl(which="sn", t=None, total=7, by_sn=None, by_router=None):
    return {"on": True,
            "dropped": {"sn": by_sn if by_sn is not None else total,
                        "router": by_router or 0, "total": total},
            "last_drop": {"t": t if t is not None else NOW, "which": which,
                          "sn": "A", "router": "127.0.0.1:40001"}}


a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, ratelimit=rl())
check("限频：窗口内有丢弃 → ratelimit warn（带防线与计数）",
      kinds(a) == ["ratelimit"] and a[0]["level"] == "warn"
      and a[0]["which"] == "sn" and a[0]["dropped_sn"] == 7)
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW,
                 ratelimit=rl(which="router", by_sn=0, by_router=9, total=9))
check("限频：per-Router 防线也如实报（which=router）",
      kinds(a) == ["ratelimit"] and a[0]["which"] == "router"
      and a[0]["dropped_router"] == 9)
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW,
                 ratelimit=rl(t=NOW - alr.RL_SEC - 1))
check("限频：超出窗口 → 自动消警（不再视为当前问题）", kinds(a) == [])
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW,
                 ratelimit={"on": True, "dropped": {"sn": 0, "router": 0, "total": 0},
                            "last_drop": None})
check("限频：从未丢弃过 → 不报", kinds(a) == [])
a = alr.evaluate(regis(), case_mgr(), deque(), now=NOW, ratelimit=rl(total=0, by_sn=0))
check("限频：有 last_drop 但计数为 0（异常组合）→ 不报（宁可漏报不误报）", kinds(a) == [])
check("限频：不传 ratelimit → 不评估（与 clock/energy 同约定）",
      kinds(alr.evaluate(regis(), case_mgr(), deque(), now=NOW)) == [])
check("限频：阈值可配（rl_sec=1 时 5 秒前的丢弃不再报）",
      kinds(alr.evaluate(regis(), case_mgr(), deque(), now=NOW,
                         ratelimit=rl(t=NOW - 5), rl_sec=1)) == [])


# ---- 规则 4：降级上报（含“没有可用 ts_eff”时不漏报）-------------------------
# 外部评审（2026-09-13）读 `ts = int(r.get("ts_eff") or 0)` + `if ts and ...` 后断言
# “缺 ts_eff 的记录会跳过 → 漏报”。**实际不会**：ts=0 只是**不参与窗口判定**，
# 告警照样出，`since` 回退到 `now`（下一行 `worst[sn] = (lv, ts or now)`）。
# 这三条把它锁住 —— 免得以后有人按那个误读把 `ts or now` 删成 `ts`。
for lv, mark in ((2, "warn"), (3, "crit")):
    for label, ts in (("缺失", None), ("=0", 0), ("=None", None)):
        rec = {"accepted": True, "level": lv, "sn": "A"}
        if ts is not None:
            rec["ts_eff"] = ts
        a = alr.evaluate(regis(), case_mgr(), deque([rec]), now=NOW)
        got = [x for x in a if x["kind"] == "id_degraded"]
        check("降级：ts_eff %s 也不漏报（L%d → %s）" % (label, lv, mark),
              len(got) == 1 and got[0]["level"] == mark and got[0]["since"] == NOW)
# 反过来：确实超出窗口只是“不再视为当前问题”，这是**有意**的（自动消警）
a = alr.evaluate(regis(), case_mgr(), deque(
    [{"accepted": True, "level": 2, "sn": "A", "ts_eff": NOW - 10 ** 6}]), now=NOW)
check("降级：超窗不再报（自动消警，有意如此）", kinds(a) == [])
a = alr.evaluate(regis(), case_mgr(), deque(
    [{"accepted": False, "level": 2, "sn": "A", "ts_eff": NOW}]), now=NOW)
check("降级：被拒的记录不算降级上报（§8.3 只看已签接受的）", kinds(a) == [])


# ---- 规则 9：RSSI 突变（2026-09-13 加，2026-09-14 按设计常态重标定）----------------
# 阈值与窗口都按**实测零假设分布**定（见 alerts.py 模块头「RSSI 突变阈值与窗口怎么定的」）：
# 窗口 = 0.5~450 s（= 1.5 × 最长合法上报间隔），阈值仍 **25 dB**。
# 下面这些用例把「不许误报」与「该报要报」两头都锁住。
def rssi(sn, rssi, ts):
    return {"sn": sn, "rssi": rssi, "ts": ts}


def jumps(series, now=None):
    return [x for x in alr.evaluate(regis(), case_mgr(), deque(),
                                    now=int(now if now is not None else NOW),
                                    rssi_series=series)
            if x["kind"] == "rssi_jump"]


# ★ 回归锁：诚实序列零误报。诚实序列 = 演示行走模型里设备自报的 rssi
#   （`motion.Walk.nearest`，含 ±2 dBm 确定性噪声）。
#   ★ 站位取 **`ui_server.DEMO_STATIONS` 单一源**（不再在测试里抄一份坐标）——
#   实测口径跟演示场景绑在一起，几何改了这条锁就会变红，逼人重新量。
import motion                                    # noqa: E402
import ui_server                                 # noqa: E402
_STATIONS = list(ui_server.OrpahApp.DEMO_STATIONS)   # [(名字, x, y), …]
_walk = motion.Walk()
_T0MS = 1_789_000_000_000
_false = []
_pairs = 0
_peak = {}


def _honest(every, hours):
    """按 `every` 秒采样诚实序列（返回近 7 对以下的全部相邻对，外加该 dt 下的 max）。"""
    global _pairs
    ser = []
    for _i in range(int(hours * 3600 / every)):
        _tms = _T0MS + _i * every * 1000
        _sid, _r = _walk.nearest(_tms, _STATIONS)
        ser.append(rssi("A", _r, _tms / 1000.0))
    ser.reverse()                                # 最新在前
    mx = 0.0
    for _k in range(len(ser) - 1):
        _pairs += 1
        mx = max(mx, abs(ser[_k]["rssi"] - ser[_k + 1]["rssi"]))
        if jumps(ser[_k:_k + 2]):
            _false.append((every, _k))
    _peak[every] = mx


# 密集采样（旧标定用的那四种）+ 设计常态 60 s + 窗口两端（0.5~450 s 内）
for _every in (1, 2, 5, 10):
    _honest(_every, 2)
for _every in (15, 30, 60, 90, 300, 450):
    _honest(_every, 168)                         # 一周：极端值统计要够长
check("★ RSSI：诚实序列 %d 对样本（1/2/5/10s×2h + 15/30/60/90/300/450s×168h）零误报"
      % _pairs, not _false, _false[:3])
check("★ RSSI：实测诚实上界 %.0f dB ≤ 22 dB（离阈值 25 还有余量；大到接近就得重标）"
      % max(_peak.values()), max(_peak.values()) <= 22, _peak)
check("★ RSSI：新建模的 60 s 常态**真的在窗口里**（自己对自己不再“不可比”）",
      alr.RSSI_PAIR_MIN_SEC <= 60 <= alr.RSSI_PAIR_MAX_SEC)
check("★ RSSI：窗口按最长合法上报间隔定（450 = 1.5 × max_useful 300）",
      alr.RSSI_PAIR_MAX_SEC == int(1.5 * enmod.MAX_USEFUL_INTERVAL_S) == 450)

_a = jumps([rssi("A", -40, NOW), rssi("A", -80, NOW - 2)])
check("RSSI：2 秒内跳 40 dB → rssi_jump warn，且带上一/当前/间隔三个数（页面能解释）",
      len(_a) == 1 and _a[0]["level"] == "warn" and _a[0]["sn"] == "A"
      and (_a[0]["rssi_from"], _a[0]["rssi_to"], _a[0]["jump_db"], _a[0]["dt_sec"])
      == (-80.0, -40.0, 40.0, 2.0))
check("RSSI：刚好等于阈值（25 dB）不报（严格大于才报）",
      jumps([rssi("A", -40, NOW), rssi("A", -65, NOW - 2)]) == [])
check("RSSI：差一点点超阈（25.1→26）也照样报（没有额外的“凑整”门槛）",
      len(jumps([rssi("A", -40, NOW), rssi("A", -66, NOW - 2)])) == 1)
check("RSSI：间隔 >450 s 不比 —— 超过最长合法上报间隔的 1.5 倍（没法归因就不报）",
      jumps([rssi("A", -40, NOW), rssi("A", -80, NOW - 451)]) == [])
check("★ RSSI：**60 s 间隔（设计常态）要比** —— 2 s 演示周期的旧窗口曾把它挡在外面",
      len(jumps([rssi("A", -40, NOW), rssi("A", -80, NOW - 60)])) == 1)
check("★ RSSI：**降级上报（300 s）也要比**（最长合法间隔）",
      len(jumps([rssi("A", -40, NOW), rssi("A", -80, NOW - 300)])) == 1)
check("RSSI：间隔 <0.5 s 不比（同一瞬间的重复采样，噪声能随便跳）",
      jumps([rssi("A", -40, NOW), rssi("A", -80, NOW - 0.4)]) == [])
check("RSSI：只有一条样本 → 不报（没有可比的“前一次”）",
      jumps([rssi("A", -40, NOW)]) == [])
check("RSSI：缺 rssi / 缺 ts 的样本跳过，不崩",
      jumps([{"sn": "A", "rssi": None, "ts": NOW}, {"sn": "A", "rssi": -40, "ts": None}]) == [])
_a = jumps([rssi("A", -40, NOW), rssi("A", -80, NOW - 2),
            rssi("B", -50, NOW), rssi("B", -52, NOW - 2)])
check("RSSI：每台设备各算各的（A 跳了、B 没跳 → 只报 A）",
      [x["sn"] for x in _a] == ["A"])
check("RSSI：取的是**最新**那一对（老的跳变不再报 —— 消警靠样本自然滚出去）",
      jumps([rssi("A", -40, NOW), rssi("A", -41, NOW - 2),
             rssi("A", -80, NOW - 4)]) == [])
check("RSSI：阈值可配（rssi_jump_db=5 时 10 dB 也算突变）",
      len(jumps([rssi("A", -40, NOW), rssi("A", -50, NOW - 2)])) == 0
      and len(alr.evaluate(regis(), case_mgr(), deque(), now=NOW, rssi_jump_db=5,
                           rssi_series=[rssi("A", -40, NOW), rssi("A", -50, NOW - 2)])) == 1)
check("RSSI：不传 rssi_series → 不评估（与 clock/energy 同约定）",
      kinds(alr.evaluate(regis(), case_mgr(), deque(), now=NOW)) == [])

# ---- 规则 10：校验位连续失败（2026-09-13）------------------------------------
# 「连续」= 字面意思：从最新一条往回数 bad_check，中间夹了**任何**其它结果就停。
def brec(err, t=NOW, sn="CN-WH01-9AF3C1D2"):
    return {"accepted": False, "error": err, "ts_eff": t, "sn": sn}


def bc(recs):
    return [x for x in alr.evaluate(regis(), case_mgr(), deque(recs), now=NOW)
            if x["kind"] == "badcheck_streak"]


_a = bc([brec("bad_check")] * 3)
check("校验位：连续 3 条 → warn，且带条数与 SN（仅线索口径）",
      len(_a) == 1 and _a[0]["level"] == "warn" and _a[0]["n"] == 3
      and _a[0]["sn"] == "CN-WH01-9AF3C1D2")
check("校验位：只有 2 条 → 不报（默认阈值 3）", bc([brec("bad_check")] * 2) == [])
check("校验位：中间夹一条**通过** → 连续断了，不报（那台设备明明能正常上报）",
      bc([brec("bad_check"), {"accepted": True, "ts_eff": NOW},
          brec("bad_check"), brec("bad_check")]) == [])
check("校验位：中间夹别的拒绝码（signature_invalid）→ 也断",
      bc([brec("bad_check"), brec("signature_invalid"),
          brec("bad_check"), brec("bad_check")]) == [])
_a = bc([brec("bad_check", t=NOW)] + [brec("bad_check", t=NOW - 10)] * 3)
check("校验位：4 条连败 → n=4，since 取**最早**那条（页面「持续 X」才对）",
      len(_a) == 1 and _a[0]["n"] == 4 and _a[0]["since"] == NOW - 10)
check("校验位：阈值可配（badcheck_streak=2）",
      len(alr.evaluate(regis(), case_mgr(), deque([brec("bad_check")] * 2), now=NOW,
                       badcheck_streak=2)) == 1)
check("校验位：空序列 → 不报", bc([]) == [])
# 阈值 0 = 关掉这条。注意 5 条连败**同时**会触发 sig_fail_rate（5 条全被拒，比例 1.0）
# —— 两条规则从不同角度描述同一串报文，都出是对的；这里只看 badcheck_streak 那条。
check("校验位：阈值设为 0 = 关掉这条（连败也不报该条，其它规则照旧）",
      [x for x in alr.evaluate(regis(), case_mgr(), deque([brec("bad_check")] * 5),
                               now=NOW, badcheck_streak=0)
       if x["kind"] == "badcheck_streak"] == [])

# ---- 阈值快照（页面展示用；单一源 = alerts.THRESHOLDS）-----------------------
_t = alr.thresholds()
check("阈值快照：条数与 THRESHOLDS 元信息一致、且 key 不重复",
      len(_t) == len(alr.THRESHOLDS)
      and len({x["key"] for x in _t}) == len(_t))
check("阈值快照：值是模块常量本身（不是另抄一份）",
      all(x["value"] == getattr(alr, x["key"].upper()) for x in _t))
check("阈值快照：每条都带 i18n 键（页面按它取文案）与单位字段",
      all(x["i18n"].startswith("alert_th_") and "unit" in x for x in _t))
check("阈值快照：本轮新增的两条也在里面（RSSI 突变 / 校验位连败）",
      {x["key"] for x in _t} >= {"rssi_jump_db", "badcheck_streak"})
check("阈值快照：覆盖值生效（th 注入优先）",
      [x for x in alr.thresholds(rssi_jump_db=99) if x["key"] == "rssi_jump_db"][0]
      ["value"] == 99)


# ---- 排序 / 计数 / 空态 ----------------------------------------------------


a = alr.evaluate(regis(dev("A", last_seen=NOW - GAP)),
                 case_mgr(case("C001", created=NOW - 300)),
                 deque([rep(False)] * 5), now=NOW)
check("排序：crit 在 warn 之前", [x["level"] for x in a] == ["crit", "crit", "warn"])
check("summary：计数正确", alr.summary(a) == {"crit": 2, "warn": 1, "total": 3})
check("空态：无设备/无案件/无上报 → 空列表",
      alr.evaluate(regis(), case_mgr(), deque(), now=NOW) == [])

# ---- 用**真实** Registry / CaseManager（内存库）跑一遭 ----------------------
# 假对象能过不代表真对象能过（上一版就因为假对象多给了 CASE_OPEN 而漏掉 AttributeError）
r2 = reg.Registry(":memory:")
pid = r2.add_person("测试")
r2.register("CN-WH01-AAAAAAAA", person_id=pid)     # 会被立案 → 转 lost
r2.register("CN-WH02-BBBBBBBB")                     # 另一台，不入案
r2.touch("CN-WH02-BBBBBBBB", ts=NOW - GAP)         # 超时
c2 = cs.CaseManager(":memory:")
c2.mark(pid, r2, ts=NOW - 300)                      # 立案 300s 前
real = alr.evaluate(r2, c2, deque(), now=NOW)
check("真实对象：长未上报 + 走失超时都能报",
      sorted(kinds(real)) == ["case_overtime", "no_report"])
check("真实对象：长未上报报的是没入案的那台",
      [x["sn"] for x in real if x["kind"] == "no_report"] == ["CN-WH02-BBBBBBBB"])
# 真实对象上验证处置态：真的调 cases.assign（不是假装改字段）
# ⚠ assign 不传 ts 会用**真实** time.time()，而本测试的 NOW 是固定值（1.8e9，比真实现在早 ~126 天）
#   → 那会让 B 方案（接手后超 24h）当场成立、把这条"接手后不再报"的断言弄假。
#   测试要控制时钟：显式传 ts（这也是 B 方案引入后暴露出来的夹具问题）。
cid_real = list(c2.cases)[0]
real_take = c2.assign(cid_real, "王警官", ts=NOW - 10)
check("真实对象：assign 成功", real_take[1] is None and c2.cases[cid_real].handler == "王警官")
real2 = alr.evaluate(r2, c2, deque(), now=NOW)
check("真实对象：接手后 case_overtime 消失（只剩长未上报）",
      kinds(real2) == ["no_report"])
# 真实对象上把接手时间往前推（真调 assign 带 ts）→ B 方案应报出来
c2.assign(cid_real, "王警官", ts=NOW - alr.CASE_HANDLED_SEC - 10)
real3 = alr.evaluate(r2, c2, deque(), now=NOW)
check("真实对象：接手超阈值 → 出现 case_handled_overtime（B 方案）",
      sorted(kinds(real3)) == ["case_handled_overtime", "no_report"])
check("真实对象：B 告警带真实接手人与接手时刻",
      [(x["handler"], x["since"]) for x in real3
       if x["kind"] == "case_handled_overtime"] == [("王警官", NOW - alr.CASE_HANDLED_SEC - 10)])
c2.assign(cid_real, "", ts=NOW)        # 还原（取消接手）

# ---- 阈值环境变量（进程内改了要重启才生效，这里用 reload 模拟重启）----------
import importlib                  # noqa: E402


def reload_with(**env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(alr)


a = reload_with(ORPAH_ALERT_CASE_OVERTIME_SEC="600",
                ORPAH_ALERT_NO_REPORT_SEC=None, ORPAH_ALERT_SIG_WINDOW=None,
                ORPAH_ALERT_SIG_FAIL_RATIO=None)
check("env：未设的项保持默认（no_report 默认随设计常态周期）",
      (a.NO_REPORT_SEC, a.SIG_WINDOW, a.SIG_FAIL_RATIO)
      == (int(2.5 * enmod.NORMAL_INTERVAL_S), 5, 0.5))
check("env：超时阈值被覆盖", a.CASE_OVERTIME_SEC == 600)
check("env：未设时 B 的接手阈值保持默认 24h", a.CASE_HANDLED_SEC == 86400)

# B 的阈值也要能用环境变量压小（演示里 24h 看不到效果）
b = reload_with(ORPAH_ALERT_CASE_HANDLED_SEC="120")
check("env：B 接手阈值可覆盖", b.CASE_HANDLED_SEC == 120)
check("env：B 覆盖值切实用于评估",
      [x["kind"] for x in b.evaluate(
          regis(), case_mgr(case("C001", handler="张警官",
                                 handled_at=NOW - 130)), deque(), now=NOW)]
      == ["case_handled_overtime"])
check("env：B 未超新阈值则不报",
      b.evaluate(regis(), case_mgr(case("C001", handler="张警官",
                                        handled_at=NOW - 110)), deque(), now=NOW) == [])
a = reload_with(ORPAH_ALERT_CASE_HANDLED_SEC=None)

# C 的两个升级阈值也要能用环境变量改（并把“关闭分级”的途径测到：crit <= warn → 直接 crit）
# 注：起步阈值现已随常态周期（150s），所以下面把两个都显式压小 —— 只压 CRIT 会得到
# “没有 warn 带”（crit 比 150 还小，一超就直接 crit），那是另一条用例。
c = reload_with(ORPAH_ALERT_NO_REPORT_SEC="100", ORPAH_ALERT_NO_REPORT_CRIT_SEC="120")
check("env：C 的 no_report 升级阈值可覆盖", c.NO_REPORT_CRIT_SEC == 120)
check("env：C 覆盖值切实生效（130s → crit）",
      [x["level"] for x in c.evaluate(regis(dev("A", last_seen=NOW - 130)), case_mgr(),
                                       deque(), now=NOW)] == ["crit"])
check("env：C 覆盖值切实生效（110s → warn）",
      [x["level"] for x in c.evaluate(regis(dev("A", last_seen=NOW - 110)), case_mgr(),
                                       deque(), now=NOW)] == ["warn"])
c = reload_with(ORPAH_ALERT_NO_REPORT_SEC="30",
                ORPAH_ALERT_NO_REPORT_CRIT_SEC="10")     # crit 比起步阈值 30 还小
check("env：crit 阈值 <= 起步阈值 → 直接 crit（相当于关掉分级）",
      [x["level"] for x in c.evaluate(regis(dev("A", last_seen=NOW - 40)), case_mgr(),
                                       deque(), now=NOW)] == ["crit"])
check("env：段内没超起步阈值就什么都不报",
      c.evaluate(regis(dev("A", last_seen=NOW - 20)), case_mgr(), deque(), now=NOW) == [])
a = reload_with(ORPAH_ALERT_NO_REPORT_SEC=None, ORPAH_ALERT_NO_REPORT_CRIT_SEC=None)
# 立案 300s 前：默认 180 会告警，改成 600 后不该告警
check("env：覆盖值切实用于评估",
      a.evaluate(regis(), case_mgr(case("C001", created=NOW - 300)),
                 deque(), now=NOW) == [])

a = reload_with(ORPAH_ALERT_CASE_OVERTIME_SEC="abc", ORPAH_ALERT_SIG_WINDOW="",
                ORPAH_ALERT_SIG_FAIL_RATIO="0.9")
check("env：非法值/空串回退默认", (a.CASE_OVERTIME_SEC, a.SIG_WINDOW) == (180, 5))
check("env：浮点阈值生效", a.SIG_FAIL_RATIO == 0.9)
# 4/5 = 0.8：默认 0.5 会告警，0.9 不该告警
a2 = a.evaluate(regis(), case_mgr(), deque([rep(False)] * 4 + [rep(True)]), now=NOW)
check("env：浮点阈值切实用于评估", a2 == [])

a = reload_with(ORPAH_ALERT_CASE_OVERTIME_SEC=None, ORPAH_ALERT_SIG_FAIL_RATIO=None)
check("env：清后恢复默认",
      (a.CASE_OVERTIME_SEC, a.SIG_FAIL_RATIO, a.CASE_HANDLED_SEC,
       a.NO_REPORT_CRIT_SEC, a.CASE_HANDLED_CRIT_SEC) == (180, 0.5, 86400, 300, 172800))

# 时钟阈值（§5.5 深化）也要能用环境变量改
a = reload_with(ORPAH_ALERT_CLOCK_OFFSET_SEC="300")
check("env：时钟偏移阈值可覆盖", a.CLOCK_OFFSET_SEC == 300)
check("env：偏移 90s 在新阈值下不告警",
      a.evaluate(regis(), case_mgr(), deque(), now=NOW, clock={"A": clock_est(off=90)}) == [])
check("env：偏移 301s 在新阈值下告警",
      kinds(a.evaluate(regis(), case_mgr(), deque(), now=NOW,
                       clock={"A": clock_est(off=301)})) == ["id_clock"])
b = reload_with(ORPAH_ALERT_CLOCK_DRIFT_PPM="2000")
check("env：时钟漂移阈值可覆盖", b.CLOCK_DRIFT_PPM == 2000)
check("env：500ppm 在新阈值下不告警",
      b.evaluate(regis(), case_mgr(), deque(), now=NOW,
                 clock={"A": clock_est(off=0, drift=500)}) == [])
a = reload_with(ORPAH_ALERT_CLOCK_OFFSET_SEC="abc", ORPAH_ALERT_CLOCK_DRIFT_PPM="")
check("env：时钟阈值非法值/空串回退默认", (a.CLOCK_OFFSET_SEC, a.CLOCK_DRIFT_PPM) == (30, 200))

# 能力声明消警窗口也可配（演示里 300s 太长，压小才看得到"自动消警"）
a = reload_with(ORPAH_ALERT_CAP_MISMATCH_SEC="60")
check("env：能力消警窗口可覆盖", a.CAP_MISMATCH_SEC == 60)
check("env：窗口覆盖值切实用于评估（age=120 > 60 → 不报）",
      a.evaluate(regis(), case_mgr(), deque([crep(True, False, t=NOW - 120, sn="A")]),
                 now=NOW) == [])
check("env：窗口内仍报（age=30 < 60）",
      kinds(a.evaluate(regis(), case_mgr(), deque([crep(True, False, t=NOW - 30, sn="A")]),
                       now=NOW)) == ["id_cap_mismatch"])
a = reload_with(ORPAH_ALERT_CAP_MISMATCH_SEC=None)
check("env：能力窗口清后回默认 300", a.CAP_MISMATCH_SEC == 300)
a = reload_with(ORPAH_ALERT_CLOCK_OFFSET_SEC=None, ORPAH_ALERT_CLOCK_DRIFT_PPM=None)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
    raise SystemExit(1)
print("all alert tests passed")
