#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_energy_calib.py — 能量模型**实测标定入口**的单测（离线，纯计算 + 临时文件）。

为什么单独一个套件：标定入口的价值全在**"这个数是实测的还是演示的"**能不能说清、
以及**坏文件不能半份生效**上 —— 这两类错误都不会让别的东西变红，只会让页面/结论
悄悄失去可信度，所以必须逐个钉住：
  · 未标定（没文件）是**正常状态**，不是错误，也不许显示成"已标定"；
  · 原始实测（µA / mA × mV × ms）换算与直接值两条路都要对，且算式给得出来；
  · **字段部分缺失 = mixed**（逐项标出处），**文件有问题 = 整份不采用**（绝不半份生效）；
  · 越界/类型错/畸形输入一律**可见报错**，不静默接受（写错一个小数点最危险）；
  · 策略阈值不是标定项（写进来只列"已忽略"）；未知键要列出来，不静默丢。

跑法：C:\\Python313\\python.exe test_energy_calib.py
"""
import json
import os
import shutil
import sys
import tempfile

import energy as en
import energy_calib as ecal

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

FAIL = []
TMP = tempfile.mkdtemp(prefix="ecal-")


def ck(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def load_obj(obj, name="c.json"):
    """写一份 JSON（或原始文本）再 load —— 走的是真实文件路径，不是打桩。"""
    p = os.path.join(TMP, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))
    return ecal.load(p)


GOOD = {
    "schema": ecal.SCHEMA,
    "source": {"device": "TX-AH+CH32V203", "who": "tester", "when": "2026-09-13",
               "how": "采样电阻 + 示波器"},
    "v_mv": 3700,
    "sleep": {"sleep_ua": 8},
    "report": {"ES256": {"active_ma": 12, "report_ms": 45},
               "HS256": {"active_ma": 12, "report_ms": 22}},
    "listen": {"listen_ma": 12, "listen_ms": 200},
    "store_mj": 2000, "charge0_mj": 1500,
    "cell": {"empty_mv": 3000, "full_mv": 4200},
}


def keys_of(items):
    return [x["key"] for x in items]


print("== 1. 未标定（没有文件）是正常状态，不是错误 ==")
missing = ecal.load(os.path.join(TMP, "nope.json"))
ck("不存在 → ok=True（读不到 ≠ 出错）", missing.ok and not missing.exists)
ck("source=none（不是 measured）", missing.source == "none")
ck("全部字段出处=demo", set(missing.prov.values()) == {"demo"}, missing.prov)
ck("值与 energy.py 演示值一致（单一源，不是抄来的常量）",
   missing.values["sleep_mw"] == en.SLEEP_MW
   and missing.values["cost_es256"] == en.COST_MJ["ES256"]
   and missing.values["store_mj"] == en.STORE_MJ
   and missing.values["cell_full_mv"] == en.CELL_FULL_MV)
ck("无错误、无提示", not missing.errors and not missing.notes)
ck("summary 说清“未标定”", "未标定" in missing.summary(), missing.summary())
v = missing.view()
ck("view 形状：rows 覆盖全部 7 项、每行都带 i18n 键与出处",
   len(v["rows"]) == len(ecal.FIELD_IDS)
   and all(r["i18n"].startswith("en_cal_f_") and r["prov_i18n"] == "en_cal_prov_demo"
           for r in v["rows"]))
ck("view 里的 source_i18n = en_cal_src_none", v["source_i18n"] == "en_cal_src_none")

print("== 2. 好文件（原始实测形式）：换算 + 算式 + 出处 ==")
c = load_obj(GOOD, "good.json")
ck("ok 且 source=measured", c.ok and c.source == "measured", c.source)
ck("待机换算 8µA@3.7V → 0.0296 mW",
   abs(c.sleep_mw - 0.0296) < 1e-9, c.sleep_mw)
ck("上报换算 12mA/3700mV/45ms → 1.998 mJ",
   abs(c.cost["ES256"] - 1.998) < 1e-9, c.cost["ES256"])
ck("HS256 → 0.9768 mJ", abs(c.cost["HS256"] - 0.9768) < 1e-9, c.cost["HS256"])
ck("听窗口换算（与上报同一公式）12mA/3700mV/200ms → 8.88 mJ",
   abs(c.listen_mj - 8.88) < 1e-9, c.listen_mj)
ck("听窗口也能写直接值 / 少了 listen_ms 要报错（不静默当 0）",
   load_obj({"schema": ecal.SCHEMA, "listen": {"listen_mj": 6.0}}, "ld.json").listen_mj == 6.0
   and keys_of(load_obj({"schema": ecal.SCHEMA, "v_mv": 3700,
                         "listen": {"listen_ma": 12}}, "li.json").errors)
   == ["en_cal_err_raw_incomplete"],
   keys_of(load_obj({"schema": ecal.SCHEMA, "v_mv": 3700,
                     "listen": {"listen_ma": 12}}, "li2.json").errors))
ck("算式字符串给出来了（可拿计算器核对）",
   "µA" in c.calc["sleep_mw"] and "0.0296" in c.calc["sleep_mw"], c.calc.get("sleep_mw"))
ck("算式也进 view.rows", any(r["calc"] for r in c.view()["rows"]))
ck("换算公式与模块里的函数同源（同一处只写一份）",
   abs(ecal.sleep_mw_of(8, 3700) - c.sleep_mw) < 1e-12
   and abs(ecal.cost_mj_of(12, 3700, 45) - c.cost["ES256"]) < 1e-12)
ck("溯源块读出来了（who/when/how/device）",
   c.origin.get("who") == "tester" and c.origin.get("how", "").startswith("采样电阻"))
ck("文件指纹 12 位（对齐“是哪一份标定”，不声称来源可信）",
   c.digest and len(c.digest) == 12, c.digest)
ck("n_measured=8/8", (c.n_measured, c.n_total) == (8, 8))
ck("无错误无提示", not c.errors and not c.notes, keys_of(c.errors) + keys_of(c.notes))
ck("summary 带上路径与实测项数", "8/8" in c.summary(), c.summary())

print("== 3. 两种形式都给：直接值优先；差 >1% 给提示（帮人抓换算错）==")
same = json.loads(json.dumps(GOOD))
same["sleep"] = {"sleep_ua": 8, "sleep_mw": 0.0296}          # 一致 → 不提示
ck("直接值与换算一致 → 不提示", not load_obj(same, "same.json").notes)
diff = json.loads(json.dumps(GOOD))
diff["report"]["ES256"]["cost_mj"] = 3.0                     # 与换算 1.998 差 50%
c2 = load_obj(diff, "diff.json")
ck("冲突 → 采用直接值 3.0（明确写下的值优先）", c2.cost["ES256"] == 3.0)
ck("冲突 → 出一条 form_mismatch 提示（带两个值）",
   "en_cal_note_form_mismatch" in keys_of(c2.notes)
   and c2.notes[0]["args"]["direct"] == "3" and c2.notes[0]["args"]["raw"].startswith("1.99"),
   c2.notes)
ck("冲突仍不算错（值可用，只是两个人算得不一样）", c2.ok and c2.source == "measured")

print("== 4. 只写一部分 = mixed（逐项标出处，不当成错）==")
part = {"schema": ecal.SCHEMA, "sleep": {"sleep_mw": 0.03}}
c3 = load_obj(part, "part.json")
ck("ok 且 source=mixed", c3.ok and c3.source == "mixed", c3.source)
ck("写了的项 = measured", c3.prov["sleep_mw"] == "measured")
ck("没写的项 = demo（**明确标出**，不是默默沿用）",
   c3.prov["cost_es256"] == "demo" and c3.values["cost_es256"] == en.COST_MJ["ES256"])
ck("没写溯源块 → 一条 no_source 提示（不阻断）",
   keys_of(c3.notes) == ["en_cal_note_no_source"], c3.notes)

print("== 5. 坏文件 = 整份不采用（绝不半份生效）==")
broke = json.loads(json.dumps(part))
broke["store_mj"] = 999999                                  # 合法（在区间内）
broke["report"] = {"ES256": {"cost_mj": 999999}}            # 越界 → 整份不采用
c4 = load_obj(broke, "broke.json")
ck("ok=False、source=error", not c4.ok and c4.source == "error")
ck("那一条**合法**的 sleep_mw 也一起不生效（回到演示值）",
   c4.values["sleep_mw"] == en.SLEEP_MW and c4.prov["sleep_mw"] == "demo")
ck("整份回到演示值：值与出处一致（不会出现“值实测、出处演示”这种自相矛盾）",
   all(c4.prov[f] == "demo" for f in ecal.FIELD_IDS)
   and all(abs(c4.values[f] - c4.demo_values()[f]) < 1e-9 for f in ecal.FIELD_IDS))
ck("算式表也清空（不留上一份的算式）", c4.calc == {})
ck("错误里带上 i18n 键与字段键",
   c4.errors[0]["key"] == "en_cal_err_field_range"
   and c4.errors[0]["args"]["field"] == "en_cal_f_cost_es256", c4.errors)
ck("summary 说“仍用演示值”", "仍用演示值" in c4.summary(), c4.summary())

print("== 6. 畸形/越界/类型错：一律可见报错 ==")
ck("JSON 解析失败 → en_cal_err_parse（原文照给）",
   keys_of(load_obj("{oops", "bad.json").errors) == ["en_cal_err_parse"])
ck("顶层不是对象 → en_cal_err_not_object",
   keys_of(load_obj([1, 2], "arr.json").errors) == ["en_cal_err_not_object"])
ck("schema 不对 → en_cal_err_schema（带上 got/want）",
   keys_of(load_obj({"schema": "nope/9"}, "sch.json").errors) == ["en_cal_err_schema"])
ck("缺 schema → 同样报 schema 错",
   keys_of(load_obj({"sleep": {"sleep_mw": 0.03}}, "nosch.json").errors)
   == ["en_cal_err_schema"])
for bad_obj, name, kind, field in (
        ({"sleep": {"sleep_mw": 1e9}}, "r1.json", "field_range", "en_cal_f_sleep_mw"),
        ({"sleep": {"sleep_mw": -1}}, "r2.json", "field_range", "en_cal_f_sleep_mw"),
        ({"sleep": {"sleep_mw": "0.03"}}, "t1.json", "field_type", "en_cal_f_sleep_mw"),
        ({"sleep": {"sleep_mw": True}}, "t2.json", "field_type", "en_cal_f_sleep_mw"),
        ({"store_mj": 0}, "r3.json", "field_range", "en_cal_f_store_mj"),
        ({"cell": {"empty_mv": 4200, "full_mv": 3000}}, "order.json",
         "cell_order", "en_cal_f_cell_full_mv"),
        ({"store_mj": 1000, "charge0_mj": 1500}, "chg.json",
         "charge_gt_store", "en_cal_f_charge0_mj")):
    o = json.loads(json.dumps(bad_obj))
    o["schema"] = ecal.SCHEMA
    cc = load_obj(o, name)
    got = keys_of(cc.errors)
    ck("坏值可见报错：" + name,
       len(got) == 1 and got[0] == "en_cal_err_" + kind
       and cc.errors[0]["args"].get("field") == field,
       got + [e.get("args", {}) for e in cc.errors])

ck("原始实测缺 v_mv → en_cal_err_raw_incomplete",
   keys_of(load_obj({"schema": ecal.SCHEMA, "sleep": {"sleep_ua": 8}},
                    "nov.json").errors) == ["en_cal_err_raw_incomplete"])
ck("原始实测缺 report_ms → en_cal_err_raw_incomplete",
   keys_of(load_obj({"schema": ecal.SCHEMA, "v_mv": 3700,
                     "report": {"ES256": {"active_ma": 12}}},
                    "noms.json").errors) == ["en_cal_err_raw_incomplete"])
ck("source 块类型不对 → en_cal_err_source_type（不静默忽略）",
   keys_of(load_obj({"schema": ecal.SCHEMA, "source": "tester"},
                    "src.json").errors) == ["en_cal_err_source_type"])

print("== 7. 不认识的键 / 策略项：列出来，不静默丢 ==")
c5 = load_obj({"schema": ecal.SCHEMA, "sleep": {"sleep_mw": 0.03},
               "_comment": "注释不算键", "min_interval_s": 5,
               "listen_interval_s": 30,
               "max_useful_interval_s": 600, "magic": 1}, "notes.json")
ck("策略阈值 → policy_ignored（每条一个；**听间隔也是策略不是标定项**）",
   [n["args"]["key"] for n in c5.notes
    if n["key"] == "en_cal_note_policy_ignored"]
   == ["listen_interval_s", "max_useful_interval_s", "min_interval_s"],
   c5.notes)
ck("未知键 → unknown_key", "en_cal_note_unknown_key" in keys_of(c5.notes)
   and [n["args"]["key"] for n in c5.notes
        if n["key"] == "en_cal_note_unknown_key"] == ["magic"])
ck("`_` 开头的键当注释（不出提示）",
   all(n["args"].get("key") != "_comment" for n in c5.notes))
ck("有提示但**仍然是可用的标定**（提示≠错误）",
   c5.ok and c5.source == "mixed" and c5.prov["sleep_mw"] == "measured")
ck("report 里不认识的级别 → level_unknown（不静默忽略）",
   "en_cal_note_level_unknown" in keys_of(load_obj(
       {"schema": ecal.SCHEMA, "report": {"ES384": {"cost_mj": 20}}}, "lv.json").notes))

print("== 8. 标定值真的进了模型（不是只画在页面上）==")
c6 = load_obj(GOOD, "model.json")
p_cal = en.plan(0.08, c6.charge0_mj, c6.store_mj, sleep_mw=c6.sleep_mw, cost=c6.cost,
                listen_mj=c6.listen_mj, listen_interval_s=60)
p_demo = en.plan(0.08, en.CHARGE0_MJ, en.STORE_MJ)
ck("按标定值算出的策略与演示参数**不同**（说明确实生效；0.08 mW 两边都是沉默，所以看 0.5 mW）",
   en.plan(0.5, c6.charge0_mj, c6.store_mj, sleep_mw=c6.sleep_mw, cost=c6.cost,
           listen_mj=c6.listen_mj, listen_interval_s=60)["interval_s"]
   != en.plan(0.5, en.CHARGE0_MJ, en.STORE_MJ)["interval_s"],
   (en.plan(0.5, c6.charge0_mj, c6.store_mj, sleep_mw=c6.sleep_mw, cost=c6.cost,
            listen_mj=c6.listen_mj, listen_interval_s=60)["interval_s"],
    en.plan(0.5, en.CHARGE0_MJ, en.STORE_MJ)["interval_s"]))
ck("★ 标定的**听窗口耗电**也进了模型（固定开销里含监听）",
   abs(p_cal["listen_mw"] - c6.listen_mj / 60.0) < 1e-9
   and abs(p_cal["overhead_mw"] - (c6.sleep_mw + c6.listen_mj / 60.0)) < 1e-9,
   (p_cal["listen_mw"], p_cal["overhead_mw"]))
ck("同一份标定 weight 一致：显式传参 == Calib 暴露的属性",
   abs(p_cal["net_mw"] - round(0.08 - c6.sleep_mw, 4)) < 1e-9, p_cal["net_mw"])
ck("电压映射也吃标定端点（空/满）",
   en.mv_of(0, c6.store_mj, c6.cell_empty_mv, c6.cell_full_mv) == 3000
   and en.mv_of(c6.store_mj * 2, c6.store_mj, c6.cell_empty_mv, c6.cell_full_mv) == 4200)
ck("drain 吃标定的 cost/sleep（逐拍结算）",
   en.drain(1000.0, 0.0, 100.0, en.LEVEL_HS, 100.0, store_mj=c6.store_mj,
            sleep_mw=c6.sleep_mw, cost=c6.cost)
   == 1000.0 - c6.sleep_mw * 100.0 - c6.cost["HS256"])

print("== 9. 路径：env 优先、缺省找仓库根那份 ==")
old = os.environ.get(ecal.ENV_PATH)
try:
    os.environ[ecal.ENV_PATH] = os.path.join(TMP, "env.json")
    ck("path_in_use() 走 env", ecal.path_in_use() == os.path.join(TMP, "env.json"))
    del os.environ[ecal.ENV_PATH]
    ck("没设 env → 缺省是仓库根 energy_calib.json",
       ecal.path_in_use() == ecal.default_path()
       and os.path.basename(ecal.default_path()) == "energy_calib.json")
    ck("示例文件放在同一目录、**不叫**缺省名（不会被自动读进去）",
       os.path.exists(os.path.join(ecal.HERE, "energy_calib.example.json"))
       and ecal.default_path() != os.path.join(ecal.HERE, "energy_calib.example.json"))
finally:
    if old is None:
        os.environ.pop(ecal.ENV_PATH, None)
    else:
        os.environ[ecal.ENV_PATH] = old

print("== 10. 口径守卫：换算常量与文档里写的一致 ==")
ck("µA×mV÷1e6 = mW（8µA × 3700mV = 0.0296 mW）", abs(ecal.sleep_mw_of(8, 3700) - 0.0296) < 1e-12)
ck("mA×mV×ms÷1e6 = mJ（12mA × 3700mV × 45ms = 1.998 mJ）",
   abs(ecal.cost_mj_of(12, 3700, 45) - 1.998) < 1e-12)
ck("策略项不在可标定字段里（阈值是产品选择）",
   not set(ecal.FIELD_IDS) & set(ecal.POLICY_KEYS))
ck("每个错误/提示 kind 都有对应的 i18n 键（页面不用猜）",
   all(k.startswith("en_cal_note_") for k in ecal.NOTE_KEYS.values())
   and len(ecal.NOTE_KEYS) == 5)
ks = ecal.i18n_keys()
ck("下发给页面的键表全是 en_cal_*（i18n 守卫按它逐个核字典）",
   len(ks) >= 35 and all(k.startswith("en_cal_") for k in ks), len(ks))
ck("键表覆盖：字段名 / 出处 / 徽标四态 / 错误 / 提示 / 表格标题",
   all(k in ks for k in ("en_cal_f_sleep_mw", "en_cal_f_listen_mj", "en_cal_prov_measured",
                         "en_cal_src_none", "en_cal_src_error", "en_cal_err_parse",
                         "en_cal_note_no_source", "en_cal_th_value", "en_cal_fold")))
try:
    ecal.Calib("x")._bad(None, "no_such_kind")
    ck("拼错错误 kind → 当场报错（不让它静静显示成键名）", False)
except ValueError:
    ck("拼错错误 kind → 当场报错（不让它静静显示成键名）", True)

shutil.rmtree(TMP, ignore_errors=True)
print()
if FAIL:
    print("标定入口：%d 项失败：%s" % (len(FAIL), "；".join(FAIL)))
    raise SystemExit(1)
print("标定入口：全部通过")
