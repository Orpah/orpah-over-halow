#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""energy_calib.py — 能量模型的**实测标定入口**（2026-09-13）

## 为什么有这个东西

`energy.py` 里那套参数（待机 0.05 mW、上报 ES256 15 mJ / HS256 5 mJ、储能 2000 mJ、
电压 3000↔4200 mV）全是**演示标定值**，没有真机实测依据。上机之后要把实测数灌进来，
而"灌进来"必须能回答一个问题：**这个结论里的这个数，是实测的还是演示的？**
否则页面/文档上写着"能量模型"，读者无法判断它是不是编的（本仓最忌讳的静默失真）。

所以本模块只做三件事：**读文件 → 换算 → 标出处**。判定（级别 / 间隔 / 沉默）依旧全在
`energy.py` —— 模型只有一份，这里**绝不重算**。

## 文件怎么写

JSON。路径由 **`ORPAH_ENERGY_CALIB`** 指定；没设就找仓库根目录的 `energy_calib.json`
（示例见 `energy_calib.example.json`）。

真上机测出来的通常是**电流 × 电压 × 时长**，而不是 mW / mJ，所以**两种写法都收**：

```json
{
  "schema": "orpah-energy-calib/1",
  "source": {"device": "TX-AH + CH32V203", "who": "shi.jinghai",
             "when": "2026-09-13", "how": "采样电阻 + 示波器，n=100 次取均值"},
  "v_mv": 3700,
  "sleep":  {"sleep_ua": 8},                              // 8 µA @3.7V → 0.0296 mW
  "report": {"ES256": {"active_ma": 12, "report_ms": 45},  // → 2.00 mJ
             "HS256": {"active_ma": 12, "report_ms": 22}}, // → 0.98 mJ
  "listen": {"listen_ma": 12, "listen_ms": 200},          // 听窗口 200ms @12mA → 8.88 mJ/次
  "harvest_curve": {"period_s": 86400, "points": [[0, 0], [21600, 0], [21600, 1.2],
                                                  [64800, 1.2], [64800, 0], [86400, 0]]},
  "store_mj": 2000, "charge0_mj": 1500,
  "cell": {"empty_mv": 3000, "full_mv": 4200}
}
```

**听窗口（`listen`）与上报用同一套换算**（都是“活跃电流 × 电压 × 时长”）。
注意 **`listen_interval_s`（多久听一次）不是标定项** —— 它是产品选择（听间隔变大 =
下行变慢、发现更慢），写进文件只会被列进「已忽略」。

**取能曲线（`harvest_curve`，可选）** = **实测**的“取能功率随时间变化”（分段常数，
秒为单位，`points` 时间非递减；相邻同刻点 = 阶跃）。它用于算**覆盖**（能不能不断线、
要多少储能）—— 归标定文件，因为它是测出来的数据，不是策略选择。不给就只用“最坏缺口”
参数（保守：不假装知道中间过程）。⚠ 它**不**计入那 8 项标定的计数（它是时间序列，不是单值），
页面单独一行显示。

等价写法（已有换算结果时）：`"sleep": {"sleep_mw": 0.03}`、
`"report": {"ES256": {"cost_mj": 2.0}}`、`"listen": {"listen_mj": 6.0}`；`v_mv`
也可写在各自的块里（块内优先）。

**换算公式（只写在这里一份）**：

- 功耗：`mW = µA × mV ÷ 1e6`（µA × mV = nW）
- 单次耗电（上报 / 听窗口都一样）：`mJ = mA × mV × ms ÷ 1e6`（mA × mV = µW，× ms = nJ）

每一行的**生效值**都带算式（`calc`，如 `8 µA × 3700 mV ÷ 1e6 = 0.0296 mW`），标定的人可以
拿计算器核对；**两种写法都给且差 >1%** 时另出一条提示（帮人抓自己的换算错），不是悄悄取一个。

## 三条硬规则

1. **文件不存在 ≠ 出错**：那就是「未标定」，一切按 `energy.py` 的演示值走，页面如实显示。
2. **文件存在但有问题 → 整份不采用**（`ok=False`，值与出处全回到演示值），**绝不半份生效**：
   半份 = 一半实测一半演示，而页面上看着"已标定" —— 这比没有标定更坏。
   但**部分字段没写**（schema 合法、只给了 2 项）**不算错**，那叫 `mixed`，逐字段标出处。
3. **每一项都要能说出出处**（`measured` / `demo`），整体 `source ∈ {none, mixed, measured, error}`，
   页面必须显示；**策略阈值不是标定项**（`min_interval_s` / `max_useful_interval_s` /
   `emergency_interval_s` / **`listen_interval_s`（多久听一次下行）** 是产品选择，
   写进文件只会被列进「已忽略」）。

另外：JSON 没有注释语法 → **`_` 开头的键当注释**（忽略、不出提示），例如 `"_comment"`；
其余不认识的键会**列进「已忽略」**（不静默丢，也不接受）。

## 诚实边界（页面 `en_cal_foot` 同口径）

- 标定只改**物理量**，不改模型形状（仍是"单点平均功率收支"，**没有**时变取能与储能缓冲 —— 那是另一项）。
- 电压 ↔ 电量仍是**线性近似**（`CELL_*` 只是两个端点），实测放电曲线没建。
- 一份文件 = **一次标定**（某台样机、某个温度）；温度 / 老化 / 批次差异没建，别当全生命周期承诺。
- 标定值来自**本地文件**，不是设备上报（设备不自报自己的能耗参数）。

## 给页面的形状（`Calib.view()`）

`rows` 是**画表格用的成品**（字段 i18n 键 / 生效值 / 演示值 / 出处 / 算式 / 单位 / 小数位），
页面只管渲染，**不写第二份字段表**。错误与提示都是 `{key, args}`：`args.field` 是 **i18n 键**、
其余是**字面值**，页面按同一个规则替换占位符即可（见 `ui/static/app.js` 的 `calLine()`）。
"""

import hashlib
import json
import math
import os

import energy as en  # noqa: E402  模型本身（本模块只读它的**演示参数**作对照）

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = "ORPAH_ENERGY_CALIB"
DEFAULT_NAME = "energy_calib.json"
SCHEMA = "orpah-energy-calib/1"

# 可标定项 = **物理量**（顺序 = 页面表格顺序）。策略阈值不在其中，见 POLICY_KEYS。
FIELD_IDS = ("sleep_mw", "cost_es256", "cost_hs256", "listen_mj", "store_mj",
             "charge0_mj", "cell_empty_mv", "cell_full_mv")
FIELD_I18N = {f: "en_cal_f_" + f for f in FIELD_IDS}
FIELD_UNIT = {"sleep_mw": "mW", "cost_es256": "mJ", "cost_hs256": "mJ",
              "listen_mj": "mJ", "store_mj": "mJ", "charge0_mj": "mJ",
              "cell_empty_mv": "mV", "cell_full_mv": "mV"}
FIELD_DIGITS = {"sleep_mw": 4, "cost_es256": 3, "cost_hs256": 3, "listen_mj": 3,
                "store_mj": 1, "charge0_mj": 1, "cell_empty_mv": 0, "cell_full_mv": 0}

# 合理区间（超出 → 报错，**不静默接受**：写错一个小数点的 mW 值会把所有结论带偏）
LIMITS = {
    "sleep_mw": (0.0, 100.0),
    "cost_es256": (0.0, 1000.0),
    "cost_hs256": (0.0, 1000.0),
    "listen_mj": (0.0, 1000.0),
    "store_mj": (1.0, 1.0e7),
    "charge0_mj": (0.0, 1.0e7),
    "cell_empty_mv": (1.0, 6000.0),
    "cell_full_mv": (1.0, 6000.0),
}

# **不是**标定项：产品选择（听间隔 / 间隔阈值）。“多久听一次下行”是设计取舍
# （听间隔↑ = 下行变慢、发现更慢），写进文件只会被列进「已忽略」。
POLICY_KEYS = ("min_interval_s", "max_useful_interval_s", "emergency_interval_s",
               "listen_interval_s")
KNOWN_TOP = {"schema", "source", "v_mv", "sleep", "report", "listen", "harvest_curve",
             "store_mj", "charge0_mj", "cell"}

NOTE_KEYS = {
    "no_source": "en_cal_note_no_source",
    "unknown_key": "en_cal_note_unknown_key",
    "policy_ignored": "en_cal_note_policy_ignored",
    "form_mismatch": "en_cal_note_form_mismatch",
    "level_unknown": "en_cal_note_level_unknown",
}

# 错误种类（i18n 键 = `en_cal_err_<kind>`）：写成常量表 + `_bad()` 校验，
# 拼错一个 kind 当场报错（否则页面上只会显示成 `en_cal_err_xx`，什么都不报）。
ERROR_KINDS = ("read", "parse", "not_object", "schema", "field_type",
               "field_range", "raw_incomplete", "source_type",
               "charge_gt_store", "cell_order",
               "curve_shape", "curve_point", "curve_negative", "curve_order")


def i18n_keys():
    """本模块会下发给页面的**全部 i18n 键**（`test_i18n.py` 据此逐个核字典 —— 单一源）。

    页面是按数据里的键名去查字典的（`T(o.key)` / `T(r.i18n)`），拼错一个字母就会在页面上
    原样显示成 `en_cal_err_xxx` —— 那种错误不会让任何测试变红，所以把键表**列在这里**。
    """
    ks = {"en_cal_fold", "en_cal_foot", "en_cal_reload", "en_cal_origin",
          "en_cal_count", "en_cal_digest", "en_cal_path_none",
          "en_cal_th_field", "en_cal_th_value", "en_cal_th_demo", "en_cal_th_prov"}
    ks |= set(FIELD_I18N.values())
    ks |= set(NOTE_KEYS.values())
    ks |= {"en_cal_err_" + k for k in ERROR_KINDS}
    ks |= {"en_cal_prov_" + p for p in ("measured", "demo")}
    ks |= {"en_cal_src_" + s for s in ("none", "mixed", "measured", "error")}
    return ks


def default_path():
    """未设 `ORPAH_ENERGY_CALIB` 时找的那份（仓库根同一目录，便于随仓库入库）。"""
    return os.path.join(HERE, DEFAULT_NAME)


def path_in_use():
    """当前会读的那份标定文件路径（env 优先）—— 页面/日志都用它，避免两处各写一遍。"""
    return os.environ.get(ENV_PATH) or default_path()


# ---------------- 换算（**只此一份**：算式与代码同源，页面直接显示字符串） ----------------

def sleep_mw_of(sleep_ua, v_mv):
    """待机功耗：µA × mV ÷ 1e6 = mW（µA × mV = nW）。"""
    return float(sleep_ua) * float(v_mv) / 1e6


def cost_mj_of(active_ma, v_mv, report_ms):
    """单次上报耗电：mA × mV × ms ÷ 1e6 = mJ（mA × mV = µW，× ms = nJ）。"""
    return float(active_ma) * float(v_mv) * float(report_ms) / 1e6


def _sleep_calc(sleep_ua, v_mv, out):
    return "%g µA × %g mV ÷ 1e6 = %g mW" % (float(sleep_ua), float(v_mv), out)


def _cost_calc(active_ma, v_mv, report_ms, out):
    return "%g mA × %g mV × %g ms ÷ 1e6 = %g mJ" % (
        float(active_ma), float(v_mv), float(report_ms), out)


class Calib(object):
    """一次标定的结果：**生效值 + 逐项出处 + 溯源 + 错误/提示**。

    值语义（`self.values[fid]`）：
    - 未标定 / 该项没写 → `energy.py` 的演示值，`prov[fid] = "demo"`；
    - 实测项 → 文件里的值（直接值优先，原始实测换算而来），`prov[fid] = "measured"`；
    - 文件不可用（`ok=False`）→ **整份回到演示值**，错误列在 `errors`。
    """

    def __init__(self, path=None):
        self.path = path or path_in_use()
        self.exists = False
        self.ok = True
        self.source = "none"                  # none | mixed | measured | error
        self.digest = None                    # 文件指纹（对齐"是哪一份标定"，不保证来源）
        self.origin = {}                      # {device, who, when, how}
        self.errors = []                      # [{key, args}]
        self.notes = []                       # [{key, args}]
        self.calc = {}                        # fid -> 算式字符串（仅实测项）
        self.curve = None                     # 实测取能曲线 [[t_s, mW], …]（可选，不计入 8 项计数）
        self.values = self.demo_values()
        self.prov = {f: "demo" for f in FIELD_IDS}

    # ---- 演示值：**引用 energy.py 的属性**（不复制常量，改一处就够） ----
    @staticmethod
    def demo_values():
        return {
            "sleep_mw": float(en.SLEEP_MW),
            "cost_es256": float(en.COST_MJ[en.LEVEL_ES]),
            "cost_hs256": float(en.COST_MJ[en.LEVEL_HS]),
            "listen_mj": float(en.LISTEN_MJ),
            "store_mj": float(en.STORE_MJ),
            "charge0_mj": float(en.CHARGE0_MJ),
            "cell_empty_mv": float(en.CELL_EMPTY_MV),
            "cell_full_mv": float(en.CELL_FULL_MV),
        }

    # ---- 模型直接吃的那几个（ui_server 按这些传参，不再自己拼） ----
    @property
    def sleep_mw(self):
        return self.values["sleep_mw"]

    @property
    def cost(self):
        return {en.LEVEL_ES: self.values["cost_es256"],
                en.LEVEL_HS: self.values["cost_hs256"]}

    @property
    def listen_mj(self):
        return self.values["listen_mj"]

    @property
    def store_mj(self):
        return self.values["store_mj"]

    @property
    def charge0_mj(self):
        return self.values["charge0_mj"]

    @property
    def cell_empty_mv(self):
        return self.values["cell_empty_mv"]

    @property
    def cell_full_mv(self):
        return self.values["cell_full_mv"]

    @property
    def n_measured(self):
        return sum(1 for f in FIELD_IDS if self.prov[f] == "measured")

    @property
    def n_total(self):
        return len(FIELD_IDS)

    # ---- 错误/提示：`args.field` 是 **i18n 键**，其余是字面值 ----
    def _bad(self, fid, kind, **args):
        if kind not in ERROR_KINDS:                 # 拼错 kind 当场报（页面会静静显示成键名）
            raise ValueError("unknown error kind: %r" % (kind,))
        if fid:
            args.setdefault("field", FIELD_I18N[fid])
        self.errors.append({"key": "en_cal_err_" + kind, "args": args})

    def _note(self, kind, **args):
        self.notes.append({"key": NOTE_KEYS[kind], "args": args})

    def summary(self):
        """一行给启动日志/自检看的话（中英无关，纯事实）。"""
        extra = "，含取能曲线 %d 点" % len(self.curve) if self.curve else ""
        if not self.exists:
            return "未标定 → 用 energy.py 演示值（可设 %s 或放 %s）" % (
                ENV_PATH, os.path.basename(default_path()))
        if not self.ok:
            return "标定文件不可用 → 仍用演示值：%s（%s）" % (
                self.errors[0]["key"] if self.errors else "?", self.path)
        return "已加载 %s：实测 %d/%d 项%s%s%s" % (
            self.path, self.n_measured, self.n_total, extra,
            "，指纹 " + self.digest if self.digest else "",
            "" if not self.notes else "，%d 条提示" % len(self.notes))

    # ---- 给页面的成品形状 ----
    def rows(self):
        demo = self.demo_values()
        out = []
        for f in FIELD_IDS:
            out.append({
                "field": f,
                "i18n": FIELD_I18N[f],
                "unit": FIELD_UNIT[f],
                "digits": FIELD_DIGITS[f],
                "value": round(self.values[f], FIELD_DIGITS[f]),
                "demo": round(demo[f], FIELD_DIGITS[f]),
                "prov": self.prov[f],
                "prov_i18n": "en_cal_prov_" + self.prov[f],
                "calc": self.calc.get(f),
            })
        return out

    def view(self):
        cv = None
        if self.curve:
            cv = {"present": True, "n": len(self.curve),
                  "period_s": round(self.curve[-1][0] - self.curve[0][0], 1)}
        return {
            "schema": SCHEMA,
            "exists": self.exists,
            "ok": self.ok,
            "source": self.source,                    # none|mixed|measured|error
            "source_i18n": "en_cal_src_" + self.source,
            "path": self.path,
            "env": ENV_PATH,
            "digest": self.digest,
            "origin": dict(self.origin),
            "n_measured": self.n_measured,
            "n_total": self.n_total,
            "curve": cv or {"present": False},       # 曲线单独一行显示（不计入 8 项计数）
            "values": {f: round(self.values[f], FIELD_DIGITS[f]) for f in FIELD_IDS},
            "prov": dict(self.prov),
            "rows": self.rows(),
            "errors": list(self.errors),
            "notes": list(self.notes),
        }


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def load(path=None):
    """读一份标定。**任何情况下都返回 Calib**（不抛异常）：坏文件 = `ok=False` + 演示值。

    调用方（`ui_server`）只做两件事：把 `values` 传给 `energy.plan/drain/mv_of`，
    把 `view()` 交给页面。**不要**在这里 try/except 吞掉错误 —— 错误是给页面看的。
    """
    c = Calib(path)
    try:
        with open(c.path, encoding="utf-8") as fh:
            txt = fh.read()
    except FileNotFoundError:
        return c                                    # 未标定：不是错误，也不是"已标定"
    except OSError as e:
        c.exists = True
        c.ok, c.source = False, "error"
        c._bad(None, "read", detail="%s: %s" % (type(e).__name__, e))
        return c

    c.exists = True
    c.digest = hashlib.sha256(txt.encode("utf-8")).hexdigest()[:12]
    try:
        doc = json.loads(txt)
    except Exception as e:                          # JSONDecodeError 等：原文照给
        c.ok, c.source = False, "error"
        c._bad(None, "parse", detail=str(e))
        return c
    if not isinstance(doc, dict):
        c.ok, c.source = False, "error"
        c._bad(None, "not_object", detail=type(doc).__name__)
        return c
    if doc.get("schema") != SCHEMA:
        c.ok, c.source = False, "error"
        c._bad(None, "schema", got=json.dumps(doc.get("schema"), ensure_ascii=False),
               want=SCHEMA)
        return c

    vals, prov, calc = {}, {}, {}
    curve = None
    root_v = doc.get("v_mv")
    if "v_mv" in doc and not _is_num(root_v):
        c._bad(None, "field_type", key="v_mv")

    # --- 溯源块（缺失只提示，不阻断：标定值本身仍然是可用的） ---
    src = doc.get("source")
    if src is None:
        c._note("no_source")
    elif not isinstance(src, dict):
        c._bad(None, "source_type", detail=type(src).__name__)
    else:
        for k in ("device", "who", "when", "how"):
            v = src.get(k)
            if isinstance(v, str) and v.strip():
                c.origin[k] = v.strip()
        if not c.origin:
            c._note("no_source")

    # --- 未知键 / 策略项：**列出来**，不静默丢（`_` 开头 = 注释，见模块文档） ---
    for k in sorted(doc):
        if k.startswith("_"):
            continue
        if k in POLICY_KEYS:
            c._note("policy_ignored", key=k)
        elif k not in KNOWN_TOP:
            c._note("unknown_key", key=k)

    def num_of(block, key, fid):
        if key not in block:
            return None
        if not _is_num(block[key]):
            c._bad(fid, "field_type", key=key, detail=type(block[key]).__name__)
            return None
        return float(block[key])

    def set_field(fid, value, text=None):
        if value is None:
            return
        lo, hi = LIMITS[fid]
        if not (lo <= value <= hi):
            c._bad(fid, "field_range", value="%g" % value,
                   range="%g ~ %g %s" % (lo, hi, FIELD_UNIT[fid]))
            return
        vals[fid], prov[fid] = value, "measured"
        if text:
            calc[fid] = text

    def v_mv_of(block, fid):
        v = block.get("v_mv", root_v)
        if not _is_num(v):
            c._bad(fid, "raw_incomplete", key="v_mv")
            return None
        return float(v)

    def both_forms(fid, direct, raw, text=None):
        """直接值 & 原始实测都给 → 采用直接值，差 >1% 另给提示（帮人抓换算错）。"""
        set_field(fid, direct if direct is not None else raw, text)
        if direct is not None and raw is not None:
            rel = abs(direct - raw) / max(abs(direct), abs(raw), 1e-12)
            if rel > 0.01:
                c._note("form_mismatch", field=FIELD_I18N[fid],
                        direct="%g" % direct, raw="%g" % raw,
                        diff="%.1f" % (rel * 100.0))

    # --- 待机功耗 ---
    sl = doc.get("sleep")
    if sl is not None:
        if not isinstance(sl, dict):
            c._bad("sleep_mw", "field_type", key="sleep", detail=type(sl).__name__)
        else:
            direct = num_of(sl, "sleep_mw", "sleep_mw")
            raw = None
            if "sleep_ua" in sl:
                ua = num_of(sl, "sleep_ua", "sleep_mw")
                v = v_mv_of(sl, "sleep_mw")
                if ua is not None and v is not None:
                    raw = sleep_mw_of(ua, v)
            both_forms("sleep_mw", direct, raw,
                       _sleep_calc(sl["sleep_ua"], sl.get("v_mv", root_v), raw)
                       if raw is not None else None)

    # --- 上报耗电（按级别） ---
    rep = doc.get("report")
    if rep is not None:
        if not isinstance(rep, dict):
            c._bad("cost_es256", "field_type", key="report", detail=type(rep).__name__)
        else:
            for lv, fid in ((en.LEVEL_ES, "cost_es256"), (en.LEVEL_HS, "cost_hs256")):
                blk = rep.get(lv)
                if blk is None:
                    continue
                if not isinstance(blk, dict):
                    c._bad(fid, "field_type", key=lv, detail=type(blk).__name__)
                    continue
                direct = num_of(blk, "cost_mj", fid)
                raw = None
                if "active_ma" in blk or "report_ms" in blk:
                    if "active_ma" not in blk or "report_ms" not in blk:
                        c._bad(fid, "raw_incomplete", key=lv)
                    else:
                        ma = num_of(blk, "active_ma", fid)
                        ms = num_of(blk, "report_ms", fid)
                        v = v_mv_of(blk, fid)
                        if ma is not None and ms is not None and v is not None:
                            raw = cost_mj_of(ma, v, ms)
                both_forms(fid, direct, raw,
                           _cost_calc(blk["active_ma"], blk.get("v_mv", root_v),
                                      blk["report_ms"], raw)
                           if raw is not None else None)
            for lv in sorted(rep):
                if lv not in (en.LEVEL_ES, en.LEVEL_HS):
                    c._note("level_unknown", key=lv)

    # --- 每次听窗口耗电（与上报同一套换算：mA × mV × ms ÷ 1e6 = mJ）---
    ls = doc.get("listen")
    if ls is not None:
        if not isinstance(ls, dict):
            c._bad("listen_mj", "field_type", key="listen", detail=type(ls).__name__)
        else:
            direct = num_of(ls, "listen_mj", "listen_mj")
            raw = None
            if "listen_ma" in ls or "listen_ms" in ls:
                if "listen_ma" not in ls or "listen_ms" not in ls:
                    c._bad("listen_mj", "raw_incomplete", key="listen")
                else:
                    ma = num_of(ls, "listen_ma", "listen_mj")
                    ms = num_of(ls, "listen_ms", "listen_mj")
                    v = v_mv_of(ls, "listen_mj")
                    if ma is not None and ms is not None and v is not None:
                        raw = cost_mj_of(ma, v, ms)
            both_forms("listen_mj", direct, raw,
                       _cost_calc(ls["listen_ma"], ls.get("v_mv", root_v),
                                  ls["listen_ms"], raw)
                       if raw is not None else None)

    # --- 储能容量 / 初始电量 / 电压端点 ---
    set_field("store_mj", num_of(doc, "store_mj", "store_mj"))
    set_field("charge0_mj", num_of(doc, "charge0_mj", "charge0_mj"))
    cell = doc.get("cell")
    if cell is not None:
        if not isinstance(cell, dict):
            c._bad("cell_empty_mv", "field_type", key="cell", detail=type(cell).__name__)
        else:
            set_field("cell_empty_mv", num_of(cell, "empty_mv", "cell_empty_mv"))
            set_field("cell_full_mv", num_of(cell, "full_mv", "cell_full_mv"))

    # --- 跨项一致性（用**生效值**判：实测 + 演示混着也要自洽） ---
    eff = dict(c.demo_values())
    eff.update(vals)
    if eff["charge0_mj"] > eff["store_mj"]:
        c._bad("charge0_mj", "charge_gt_store",
               value="%g" % eff["charge0_mj"], store="%g" % eff["store_mj"])
    if eff["cell_empty_mv"] >= eff["cell_full_mv"]:
        c._bad("cell_full_mv", "cell_order",
               lo="%g" % eff["cell_empty_mv"], hi="%g" % eff["cell_full_mv"])

    # --- 实测取能曲线（可选；形状/时序/功率全查，坏了**整份不采用**）---
    hc = doc.get("harvest_curve")
    if hc is not None:
        pts = hc.get("points") if isinstance(hc, dict) else hc
        if not isinstance(pts, list):
            c._bad(None, "curve_shape", detail=type(pts).__name__)
        elif len(pts) < 2:
            c._bad(None, "curve_shape", n=len(pts))
        else:
            out = []
            for i, pt in enumerate(pts):
                if (not isinstance(pt, (list, tuple)) or len(pt) != 2
                        or not _is_num(pt[0]) or not _is_num(pt[1])):
                    c._bad(None, "curve_point", i=i)
                    continue
                t, mw = float(pt[0]), float(pt[1])
                if mw < 0:
                    c._bad(None, "curve_negative", i=i, mw="%g" % mw)
                    continue
                if out and t < out[-1][0]:
                    c._bad(None, "curve_order", i=i, t="%g" % t, prev="%g" % out[-1][0])
                    continue
                out.append([t, mw])
            if len(out) == len(pts) and len(out) >= 2:
                curve = out

    # --- 采用 / 整份不采用（硬规则 2） ---
    if not c.ok or c.errors:
        c.ok, c.source = False, "error"
        c.values = c.demo_values()                  # 整份回到演示值（值 + 出处一致）
        c.prov = {f: "demo" for f in FIELD_IDS}
        c.calc = {}
        c.curve = None
        return c
    c.values.update(vals)
    c.prov.update(prov)
    c.calc = calc
    c.curve = curve
    n = sum(1 for f in FIELD_IDS if c.prov[f] == "measured")
    c.source = "measured" if n == c.n_total else ("mixed" if n else "none")
    return c


def main():
    """自检：打印当前这份标定的结论（`python energy_calib.py`，只读不改）。"""
    import sys
    for _s in (sys.stdout, sys.stderr):          # 控制台 GBK：µ 等字符兜住
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    c = load()
    print(c.summary())
    for r in c.rows():
        print("  %-14s %-10s %s%s" % (
            r["field"], "实测" if r["prov"] == "measured" else "演示",
            r["value"], " " + r["unit"] + ("  [" + r["calc"] + "]" if r["calc"] else "")))
    for e in c.errors:
        print("  !! " + e["key"] + " " + json.dumps(e["args"], ensure_ascii=False))
    if c.curve:
        print("  取能曲线：%d 点，周期 %.0f s" % (len(c.curve), c.curve[-1][0] - c.curve[0][0]))
    return 0 if c.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
