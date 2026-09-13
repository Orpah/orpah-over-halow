#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_fresh.py — 「数据新鲜度」共享件与状态机的离线自检（UI ④，2026-09-13）
======================================================================
为什么单独测：这一层的作用就是**替现场判断“这页的数字是不是老的”**，它自己错了比不做还糟 ——
判成“正常”会让人继续看一个早就停了的数（最坏的谎）；判成“停摆”又会让人去查一个没坏的系统。
所以边界、缺失值、后台标签页这几条都逐条钉住。

覆盖：
1. **状态机边界**（`freshness.state_of`）：None → unknown（**不是** 0/不是 live —— 与
   JS 里 `Number(null) === 0` 同一类坑）；2.5× / 6× 节拍的边界含等号；节拍缺失/非正 → unknown；
2. **Tracker**：touch 更新 last 与 period、`set_period` 只改节拍不动 last、age 由服务端算、
   `worst` 排序（unknown < live < stale < stopped，空集合 = unknown）；
3. **IoTDB 行**（`freshness.tsdb_row`）：未启用 → unknown（不是 stopped）、从没成功过 → unknown、
   启用且很久没写 → stopped、错误摘要带出来；
4. **页面共享件 fresh.js**（node 跑原文）：年龄文案/状态文案（认不出的状态**回退到 unknown 文案**）、
   行文案的 tsdb 特例、动态键标签、本页轮询四态（live/stale/stopped/hidden）、数据集事实行
   （取数/截止/条数/失败/模拟）、渲染行数、`freshWorst` 回退；
5. **单一源守卫**：fresh.js 的 `FRESH_LIVE_MULT`/`FRESH_STALE_MULT` 必须与 `freshness.py` 的
   `LIVE_MULT`/`STALE_MULT` 数值一致（否则页面说“正常”、服务端说“滞后”）；
6. **传 T 守卫**（与 `pos.js` 的 trustText 同一个坑）：`fresh.js` 与各页面里每一处
   `freshXxx(...)` 调用都必须把 `T` 传进去（漏传的运行期表现是 `T is not a function`，
   会把调用它的整块逻辑打断，而且**不报到明处**）；
7. **页面守卫**：index/track/replay 都引入 fresh.js，且各自的容器被真正喂到
   （index `#freshBox` ← `freshRender`；track `#dsFresh` / replay `#rpFresh` ← `freshDataset`）；
   页面里不许出现自己写的状态字面量（"stale"/"stopped" 判定只能来自共享件）。

运行：C:\\Python313\\python.exe test_fresh.py     （需要 node 在 PATH 上，同 test_posjs/test_mapjs）
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import freshness as fr                                  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FRESH_JS = os.path.join(HERE, "ui", "static", "fresh.js")
STATIC = os.path.join(HERE, "ui", "static")
DICT = os.path.join(STATIC, "ui_i18n.js")            # 桩 T 用真实字典（见 HARNESS）

FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}" + (f"   {extra}" if extra else ""))
        FAILS.append(name)
    return cond


# ---------------------------------------------------------------- 服务端侧
def check_python():
    print("== freshness.py：状态机 / 跟踪器 / IoTDB 行 ==")
    S = fr.state_of
    # 1) 缺失时间戳 **不许** 当成“刚刚更新过”
    check("age=None → unknown（不是 live、不是 0）", S(None, 2.0) == fr.UNKNOWN, S(None, 2.0))
    check("节拍缺失/0/负 → unknown（拿不到节拍不下判断）",
          S(1.0, None) == fr.UNKNOWN and S(1.0, 0) == fr.UNKNOWN and S(1.0, -3) == fr.UNKNOWN)
    # 2) 边界含等号（<= 才算“还在节拍内”）
    p = 2.0
    check("age = 2.5×节拍 → live（含等号）", S(fr.LIVE_MULT * p, p) == fr.LIVE)
    check("age 略超 2.5× → stale", S(fr.LIVE_MULT * p + 0.01, p) == fr.STALE)
    check("age = 6×节拍 → stale（含等号）", S(fr.STALE_MULT * p, p) == fr.STALE)
    check("age 略超 6× → stopped", S(fr.STALE_MULT * p + 0.01, p) == fr.STOPPED)

    t = fr.Tracker()
    check("没打过点的流 → 0 行（页面显示“从未”而不是绿色）", t.rows() == [])
    t.touch("a", period=2.0, ts=1000.0)
    r = t.rows(now=1001.0)[0]
    check("touch 后 age 由服务端算（1.0s）", r["age_s"] == 1.0 and r["state"] == fr.LIVE, r)
    t.set_period("a", 30.0)
    r = t.rows(now=1001.0)[0]
    check("set_period 只改节拍、不动 last（节拍变了 ≠ 刚有数据）",
          r["period_s"] == 30.0 and r["last"] == 1000.0, r)
    r = t.rows(now=1001.0)[0]
    t.touch("b", period=2.0, ts=900.0)          # 100s 前 → stopped
    w = t.worst(t.rows(now=1001.0))
    check("worst = 最坏那条（stopped）", w == fr.STOPPED, w)
    check("worst 空集合 = unknown（没有数据 ≠ 正常）", t.worst([]) == fr.UNKNOWN)
    check("排序：unknown 不算比 live 好", fr._ORDER[fr.UNKNOWN] < fr._ORDER[fr.LIVE])

    # 3) IoTDB 行：三个特例
    off = fr.tsdb_row({"enabled": False, "available": False, "last_ok": None,
                       "last_err": None, "err": ""}, 2.0, now=1000.0)
    check("未启用 IoTDB → unknown（不是 stopped：没打算落库不是故障）",
          off["state"] == fr.UNKNOWN and off["on"] is False, off)
    never = fr.tsdb_row({"enabled": True, "available": True, "last_ok": None,
                         "last_err": None, "err": ""}, 2.0, now=1000.0)
    check("启用了但从没成功过 → unknown（还没有基准）", never["state"] == fr.UNKNOWN, never)
    stale = fr.tsdb_row({"enabled": True, "available": False, "last_ok": 900.0,
                         "last_err": 950.0, "err": "Timeout: boom"}, 2.0, now=1000.0)
    check("启用且 100s 没写成功 → stopped + 带错误摘要",
          stale["state"] == fr.STOPPED and "Timeout" in stale["err"], stale)
    check("age 为 None 时不给 0（否则会显示成“刚刚”）",
          off["age_s"] is None and never["age_s"] is None)


# ---------------------------------------------------------------- fresh.js（node）
HARNESS = r"""
const fs = require("fs");
const EXPORT = ["FRESH_MARK", "FRESH_ORDER", "FRESH_LABEL", "FRESH_LIVE_MULT",
                "FRESH_STALE_MULT", "freshLabel", "freshAgeText", "freshStateText",
                "freshAgo", "freshRowText", "freshRowTitle", "freshWorst",
                "freshLine", "freshRender", "freshDataset",
                "freshPollArm", "freshPollOk", "freshPollFail", "freshPollReset",
                "freshPollState", "freshPollText"];

/* ---- 最小 DOM 桩（只够 fresh.js 用：createElement / textContent / appendChild / dataset）---- */
function el(tag) {
  return {
    tagName: tag, className: "", textContent: "", children: [], dataset: {}, title: "",
    appendChild(n) { this.children.push(n); return n; },
    setAttribute(k, v) { this.dataset[k] = v; },
  };
}
global.document = { hidden: false, createElement: el };
/* `T` 用**真实字典**（从 ui_i18n.js 的 zh 块里抽键值）—— 桩自己造文案的话，
   “占位符有没有替换”这类事情根本测不到（拿一个没有 {n} 的假模板去测 = 白测）。
   缺失键的行为也要与 `OrpahI18n.t` 一致：回退到 key 本身（fresh.js 靠这个回退判断）。 */
const i18nSrc = fs.readFileSync(process.argv[3], "utf8");
const zhStart = i18nSrc.indexOf("zh: {");
const enStart = i18nSrc.indexOf("en: {");
const zhBlock = i18nSrc.slice(zhStart, enStart);
const dict = {};
const reKV = /^\s*"([^"]+)"\s*:\s*("(?:[^"\\]|\\.)*")/gm;
let mm;
while ((mm = reKV.exec(zhBlock))) { try { dict[mm[1]] = JSON.parse(mm[2]); } catch (e) {} }
ck_dict = Object.keys(dict).length;
global.T = (k) => (k in dict ? dict[k] : k);
global.window = {};
const box = () => el("div");
global.__box = box;

const src = fs.readFileSync(process.argv[2], "utf8")
          + "\nmodule.exports = {" + EXPORT.join(",") + "};\n";
const m = { exports: {} };
new Function("module", "exports", src)(m, m.exports);
const F = m.exports;

let fails = [];
function ck(name, cond, extra) {
  if (cond) { console.log("  OK   " + name); }
  else { console.log("  FAIL " + name + (extra !== undefined ? "   " + JSON.stringify(extra) : "")); fails.push(name); }
}
/* 取一行的三段（符号 / 标签 / 值）—— 渲染结果只在这里拆，测试里不重复 DOM 细节 */
const parts = (b, i) => {
  const it = b.children[i];
  return it ? [it.children[0].textContent, it.children[2].textContent] : null;
};
/* 把 {age} 代入后的期望文案（模板里带占位符，直接比会永远不等） */
const sub = (k, age) => global.T(k).replace("{age}", F.freshAgeText(age, global.T));

/* 1) 文案（全部要求把 T 传进去） */
ck("真字典可用（否则下面的断言都是在测桩）", ck_dict > 500, ck_dict);
ck("状态文案：live", F.freshStateText("live", 3, global.T) === sub("fresh_s_live", 3));
ck("状态文案：认不出的状态回退到 unknown（不返回空、不返回原始 key）",
   F.freshStateText("weird", 3, global.T) === global.T("fresh_s_unknown"),
   F.freshStateText("weird", 3, global.T));
ck("状态文案：state 为 null/undefined 也回退 unknown",
   F.freshStateText(null, 1, global.T) === global.T("fresh_s_unknown"));
ck("状态文案：占位符 {age} 真的被替换（留下花括号就是没替换）",
   global.T("fresh_s_live").indexOf("{age}") >= 0 &&
   F.freshStateText("live", 3.24, global.T).indexOf("{age}") < 0,
   F.freshStateText("live", 3.24, global.T));
ck("年龄：秒级保留一位（<10s）", F.freshAgeText(3.24, global.T).indexOf("3.2") >= 0,
   F.freshAgeText(3.24, global.T));
ck("年龄：10s 以上取整", F.freshAgeText(42.6, global.T).indexOf("43") >= 0,
   F.freshAgeText(42.6, global.T));
ck("年龄：分钟", F.freshAgeText(75, global.T).indexOf("1") >= 0, F.freshAgeText(75, global.T));
ck("年龄：小时", F.freshAgeText(5400, global.T).indexOf("1.5") >= 0, F.freshAgeText(5400, global.T));
ck("年龄：null/NaN → “从未”（不是 0）",
   F.freshAgeText(null, global.T) === global.T("fresh_never") &&
   F.freshAgeText(NaN, global.T) === global.T("fresh_never"));
ck("动态键 → 文案；字典没有的键回退机器名（不静默消失）",
   F.freshLabel("report_cycle", global.T) === global.T("fresh_l_report_cycle") &&
   F.freshLabel("brand_new_thing", global.T) === "brand_new_thing");

/* 2) 行文案：tsdb 的两个特例 */
ck("落库行：未启用 → 未启用文案（不是“停摆”）",
   F.freshRowText({ key: "tsdb_write", on: false, state: "unknown" }, global.T) ===
     global.T("fresh_s_disabled"));
ck("落库行：启用 → 走状态文案",
   F.freshRowText({ key: "tsdb_write", on: true, state: "stopped", age_s: 30 }, global.T) ===
     sub("fresh_s_stopped", 30));
ck("悬停解释里带节拍与上次时刻",
   F.freshRowTitle({ key: "report_cycle", period_s: 2, last: 1000, state: "live" }, global.T)
     .indexOf(global.T("fresh_tip_period").split("{n}")[0]) >= 0);

/* 3) 最坏态（unknown < live < stale < stopped；未启用的落库行不算坏） */
ck("freshWorst：混合取最坏", F.freshWorst([{ state: "live" }, { state: "stale" }]) === "stale");
ck("freshWorst：空 = unknown", F.freshWorst([]) === "unknown");
ck("freshWorst：未启用的落库行按 unknown（不把“没启用”算成故障）",
   F.freshWorst([{ state: "stopped", key: "tsdb_write", on: false }]) === "unknown");

/* 4) 渲染：服务端行 + 本页轮询行 */
F.freshPollArm(1);
F.freshPollOk(1000);
const b1 = global.__box();
F.freshRender(b1, { worst: "stopped", rows: [
  { key: "report_cycle", period_s: 2, last: 999, age_s: 1, state: "live" },
  { key: "tsdb_write", period_s: 2, last: null, age_s: null, state: "unknown", on: false }] }, global.T);
ck("渲染：服务端 2 行 + 本页轮询 1 行 = 3 行", b1.children.length === 3, b1.children.length);
ck("渲染：第一行带符号与文案", parts(b1, 0)[0] === "●" &&
   parts(b1, 0)[1] === sub("fresh_s_live", 1), parts(b1, 0));
ck("渲染：落库行显示“未启用”", parts(b1, 1)[1] === global.T("fresh_s_disabled"), parts(b1, 1));
ck("渲染：整条横幅的 worst 记在 dataset（服务端给的 worst 优先）", b1.dataset.worst === "stopped", b1.dataset);
const b1b = global.__box();
F.freshRender(b1b, { rows: [{ key: "report_cycle", state: "stale" }] }, global.T);
ck("渲染：服务端没给 worst 时自己算（live+stale → stale）", b1b.dataset.worst === "stale", b1b.dataset);

/* 5) 本页轮询四态 */
F.freshPollArm(1);
F.freshPollOk(1000);
ck("轮询：1s 内 = live", F.freshPollState(1001) === "live", F.freshPollState(1001));
ck("轮询：超过 2.5×节拍 = stale", F.freshPollState(1004) === "stale", F.freshPollState(1004));
ck("轮询：超过 6×节拍 = stopped", F.freshPollState(1010) === "stopped", F.freshPollState(1010));
F.freshPollReset();
ck("轮询：从没成功过 = stopped（打开页面就没连上，说“正常”是最坏的谎）",
   F.freshPollState(1010) === "stopped");
ck("轮询：从没成功过的文案带失败次数",
   F.freshPollText(1010, global.T).indexOf(global.T("fresh_s_page_down").split("{n}")[0]) >= 0,
   F.freshPollText(1010, global.T));
F.freshPollOk(2000);
document.hidden = true;
ck("轮询：本页在后台 → hidden（不判成后端停了）", F.freshPollState(3000) === "hidden",
   F.freshPollState(3000));
ck("轮询：后台文案如实说“不是后端停了”",
   F.freshPollText(3000, global.T) === global.T("fresh_s_page_hidden"));
document.hidden = false;
F.freshPollFail(2001);
ck("轮询：失败计数拼进文案",
   F.freshPollText(2001, global.T).indexOf(global.T("fresh_page_fails").split("{n}")[0]) >= 0,
   F.freshPollText(2001, global.T));

/* 6) 数据集事实行（track / replay） */
const b2 = global.__box();
F.freshDataset(b2, { fetchAt: Date.now() / 1000, cutoff: Date.now() / 1000 - 30, n: 12 }, global.T);
ck("数据集：取数/截止/条数 = 3 行（+ 轮询 1 行 = 4）", b2.children.length === 4, b2.children.length);
ck("数据集：截止行是**事实**标记（中性点，不判好坏）", parts(b2, 1)[0] === "·", parts(b2, 1));
const b3 = global.__box();
F.freshDataset(b3, { sim: true, poll: false }, global.T);
ck("数据集：模拟模式只有 1 行且如实说“本地合成”", b3.children.length === 1 &&
   parts(b3, 0)[1] === global.T("fresh_s_sim"), b3.children.length);
const b4 = global.__box();
F.freshDataset(b4, { error: "boom", poll: false }, global.T);
ck("数据集：取数失败 → stopped + 错误文本", parts(b4, 0)[0] === "✕" &&
   parts(b4, 0)[1] === "boom", parts(b4, 0));
const b5 = global.__box();
F.freshDataset(b5, { poll: false }, global.T);
ck("数据集：还没取过 → unknown（不是绿色正常）", parts(b5, 0)[0] === "○", parts(b5, 0));
const b6 = global.__box();
F.freshDataset(b6, { fetchAt: 1, cutoff: null, n: 0, poll: false }, global.T);
ck("数据集：窗内没样本 → 明写“窗内没有样本”",
   parts(b6, 1)[1] === global.T("fresh_no_data"), parts(b6, 1));

/* 7) 阈值常量（与 freshness.py 一致性由 Python 侧比，这里只导出） */
ck("导出的倍数常量存在", F.FRESH_LIVE_MULT > 0 && F.FRESH_STALE_MULT > F.FRESH_LIVE_MULT);

if (fails.length) { console.log("FAILED " + fails.length); process.exit(1); }
console.log("ALL OK");
"""


def check_js():
    print("== fresh.js（node 跑原文，带最小 DOM 桩） ==")
    node = shutil.which("node")
    if not node:
        print("  SKIP 本机没有 node —— **不算通过**（fresh.js 的纯函数没被验证）")
        return False, None
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(HARNESS)
        path = fh.name
    try:
        p = subprocess.run([node, path, FRESH_JS, DICT], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=60)
    finally:
        os.unlink(path)
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines():
        if line.strip():
            print(line if line.startswith("  ") else "    " + line)
    check("fresh.js 全部用例通过", p.returncode == 0 and "ALL OK" in out, f"exit={p.returncode}")
    return True, out


def js_const(name):
    src = open(FRESH_JS, encoding="utf-8").read()
    m = re.search(re.escape(name) + r"\s*=\s*([0-9.]+)", src)
    return float(m.group(1)) if m else None


def check_single_source():
    print("== 单一源：阈值常量两边必须一致 ==")
    check("FRESH_LIVE_MULT == freshness.LIVE_MULT",
          js_const("FRESH_LIVE_MULT") == fr.LIVE_MULT,
          f"js={js_const('FRESH_LIVE_MULT')} py={fr.LIVE_MULT}")
    check("FRESH_STALE_MULT == freshness.STALE_MULT",
          js_const("FRESH_STALE_MULT") == fr.STALE_MULT,
          f"js={js_const('FRESH_STALE_MULT')} py={fr.STALE_MULT}")
    # 状态字面量：页面按这些字符串查文案，两边必须同一套
    js = open(FRESH_JS, encoding="utf-8").read()
    for st in (fr.LIVE, fr.STALE, fr.STOPPED, fr.UNKNOWN):
        check(f"fresh.js 认识状态 “{st}”", f'{st}:' in js or f'"{st}"' in js, st)


# 只守**产文案**的函数：轮询状态机（freshPollState 等）不产文案、freshWorst 也不
# 吃 T，把它们列进来只会产生假失败（守卫自己得先对）。
CALL = re.compile(r"\b(fresh(?:Render|Dataset|RowText|RowTitle|StateText|AgeText|Label|Line|"
                  r"Ago|PollText))\s*\(")


def check_T_passed():
    """每一处 `freshXxx(...)` 调用都必须把 T 传进去。

    与 `pos.js` 的 `trustText(c, T)` 同一个坑：漏传的运行期表现是 `T is not a function`，
    会把**调用它的整块逻辑**打断（页面看起来只是“这块不更新了”），不报到明处。
    """
    print("== 传 T 守卫（漏传 = 静默断掉整块逻辑） ==")
    files = [FRESH_JS] + [os.path.join(STATIC, n) for n in
                          ("index.html", "track.html", "replay.html", "app.js")]
    bad, n = [], 0
    for p in files:
        if not os.path.exists(p):
            continue
        src = open(p, encoding="utf-8").read()
        # 定义处（function freshXxx( / const freshXxx = ）不算调用点
        src = re.sub(r"function\s+fresh\w+\s*\(", "function _def(", src)
        for m in CALL.finditer(src):
            # 取到匹配的右括号（支持嵌套一层括号），检查里面有没有 T
            i, depth = m.end() - 1, 0
            while i < len(src):
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            args = src[m.end():i]
            n += 1
            # `T` 作为参数（`..., T)`）或直接调用（`T("key")`）都算把 T 传进来了。
            # 注意：args **不含右括号**（它是分界符）—— 所以末尾的 T 后面什么都没有，
            # 必须允许行尾，否则 `freshAgeText(ageS, T)` 这种会被误报。
            if not re.search(r"(^|[,\s(])T\s*(\(|\)|,|$)", args):
                bad.append(f"{os.path.basename(p)}: {m.group(1)}({args.strip()[:40]})")
    check(f"共 {n} 处调用都带了 T", not bad, bad[:4])


def check_pages():
    print("== 页面守卫：三个页面都真正接了共享件 ==")
    idx = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
    trk = open(os.path.join(STATIC, "track.html"), encoding="utf-8").read()
    rpl = open(os.path.join(STATIC, "replay.html"), encoding="utf-8").read()
    app = open(os.path.join(STATIC, "app.js"), encoding="utf-8").read()
    check("index/track/replay 都加载 fresh.js",
          all('src="fresh.js' in h for h in (idx, trk, rpl)))
    check("index 有容器 #freshBox 且 app.js 喂它",
          'id="freshBox"' in idx and 'freshRender($("freshBox")' in app)
    check("index 把“拿不到服务端状态”也显现出来（不是静默保持旧值）",
          "freshPollFail()" in app and "freshRender($(\"freshBox\"), null, T)" in app)
    check("track 有容器 #dsFresh 且真实模式喂它",
          'id="dsFresh"' in trk and 'freshDataset($("dsFresh")' in trk)
    check("track 模拟模式如实说明“本地合成”（不留空白让人以为是取数失败）",
          "sim: true" in trk)
    check("replay 有容器 #rpFresh 且取数后喂它",
          'id="rpFresh"' in rpl and 'freshDataset($("rpFresh")' in rpl)
    check("replay 取数失败也显现（没数据却说截止时刻是骗人）",
          rpl.count('freshDataset($("rpFresh"), { error:') >= 2)
    # 页面不许自己写状态判定（判定只能来自共享件/服务端）—— 只拦“比较/分支”，
    # 不拦普通字符串（例：app.js 的 `{action:"stale"}` 是“超窗重放”演示动作，与此无关）。
    dec = re.compile(r"(===|!==|==|!=)\s*[\"'](live|stale|stopped|unknown)[\"']"
                     r"|[\"'](live|stale|stopped|unknown)[\"']\s*(===|!==|==|!=)")
    hard = []
    for name, h in (("index", idx), ("track", trk), ("replay", rpl), ("app.js", app)):
        for m in dec.finditer(h):
            hard.append((name, m.group(0).strip()))
    check("页面里没有自己做新鲜度状态判定（判定单一源）", not hard, hard)
    # 卡片顺序：新鲜度在拓扑之前（先回答“数字是不是老的”，再看数字）
    check("index 的新鲜度卡在拓扑之前",
          idx.index('id="freshCard"') < idx.index('id="topology"'))


def main():
    check_python()
    ok, out = check_js()
    if out is None:
        FAILS.append("node 缺失：fresh.js 未验证")
    check_single_source()
    check_T_passed()
    check_pages()
    if FAILS:
        print(f"数据新鲜度：有问题（{len(FAILS)} 处）")
        return 1
    print("数据新鲜度：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
