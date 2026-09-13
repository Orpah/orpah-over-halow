#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_mapjs.py — 地图共享件 `ui/static/map.js` 的离线自检（用 node 跑同一份源码）
==============================================================================
为什么单独测：`map.js` 是**底图源列表 / 条款说明 / 基点 ↔ 经纬度换算 / 离线回落** 的单一源
（`track.html` 与 `replay.html` 共用），但它的错**不会报错** —— 基点错就是“整条轨迹平移”，
两张图看起来都正常。这里把纯函数部分直接塞进 node 跑一遍（不复制实现）：

覆盖：
1. **基点导入解析**：本项目导出格式 / `lat,lng` / `latitude,longitude` / `lat0,lng0` /
   `origin|base|center|point` 子对象 / **GeoJSON Point（顺序是 [经度, 纬度]！）** /
   `Feature` / `FeatureCollection` 的第一个点；
2. **校验与错误码**：非法 JSON、认不出字段、NaN/空串/null/∞、超出 ±90/±180 —— 每种都给
   明确错误码（**不静默回落成默认值**，否则使用者以为导入成功了）；范围边界（±90/±180 合法）；
3. **往返**：`baseExportText()` 的输出能被 `baseImport()` 原样读回（同一个数）；
4. **换算语义**：`toLatLng` 按基点 + 米/度换算 —— 基点整体平移 Δ ⇒ 所有点整体平移 Δ
   （这条锁住“基点错 = 平移而不是缩放”）；纬度 1° = 110540 m、经度 1° 按基点纬度压缩；
5. **存储**：写→读→清；存坏了的写回被忽略（**不当默认值用**）；localStorage 抛异常时不崩；
6. **来源显示**：default / manual / imported 三态（避免把演示默认值误读成现场值）；
7. **页面守卫**：两页的 `value=` 必须与 `BASE_DEFAULT` 一致（单一源）、两页都必须调
   `baseApplyStored()` + `baseInit()`，且两页都要有那几个元素（否则只有一页能用导入）；
8. **口径守卫**：`base_hint` 双语都要写明「只接受 WGS84、不做坐标系转换、存在本机浏览器」，
   `base_src_default` 必须写明**不是现场值**。

运行：C:\\Python313\\python.exe test_mapjs.py     （需要 node 在 PATH 上）
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

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_JS = os.path.join(HERE, "ui", "static", "map.js")
I18N_JS = os.path.join(HERE, "ui", "static", "ui_i18n.js")

# node 侧：给 map.js 铺最小桩（$ / T / OrpahI18n / localStorage / document / L / window），
# 然后把要测的纯函数导出。**map.js 原文不改一个字**。
HARNESS = r"""
const fs = require("fs");
const EXPORT = ["BASE_DEFAULT", "basePick", "baseCheck", "baseParse", "baseImport",
                "baseExportText", "baseSource", "baseApplyStored", "baseStoreRead",
                "baseStoreWrite", "baseStoreClear", "baseErrText", "toLatLng",
                "baseLat", "baseLng", "baseInit"];
/* ---- 桩：DOM/存储 ---- */
const els = {};
function E(id) { return els[id] || (els[id] = { id, value: "", textContent: "", files: [] }); }
global.$ = (id) => E(id);
global.T = (k) => k;                       // 文案只需存在性，不求内容
global.OrpahI18n = { lang: "zh" };
let store = {};
global.window = {
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  },
};
global.localStorage = global.window.localStorage;
global.document = { createElement: () => ({ style: {}, click() {} }),
                    body: { appendChild() {}, removeChild() {} } };
global.Blob = function () {}; global.URL = { createObjectURL: () => "blob:x", revokeObjectURL() {} };
global.FileReader = function () {}; global.setTimeout = (f) => f();
const src = fs.readFileSync(process.argv[2], "utf8")
          + "\nmodule.exports = {" + EXPORT.join(",") + "};\n";
const m = { exports: {} };
new Function("module", "exports", src)(m, m.exports);
const P = m.exports;

let fails = [];
function ck(name, cond, extra) {
  if (cond) { console.log("  OK   " + name); }
  else { console.log("  FAIL " + name + (extra ? "   " + extra : "")); fails.push(name); }
}
function near(a, b, tol) { return Math.abs(a - b) <= (tol === undefined ? 1e-9 : tol); }
function setBase(lat, lng) { E("baseLat").value = String(lat); E("baseLng").value = String(lng); }

/* ================= 1 · 导入解析：认哪些写法 ================= */
const D = P.BASE_DEFAULT;
ck("默认基点是**一个**常量（两页共用，不在页面里各写一份）",
   typeof D.lat === "number" && typeof D.lng === "number");

const forms = [
  ["本项目导出格式", JSON.stringify({ orpah_base: 1, lat: 31.2, lng: 121.5 })],
  ["lat/lng", JSON.stringify({ lat: 31.2, lng: 121.5 })],
  ["latitude/longitude", JSON.stringify({ latitude: 31.2, longitude: 121.5 })],
  ["lat0/lng0", JSON.stringify({ lat0: 31.2, lng0: 121.5 })],
  ["baseLat/baseLng", JSON.stringify({ baseLat: 31.2, baseLng: 121.5 })],
  ["origin 子对象", JSON.stringify({ origin: { lat: 31.2, lng: 121.5 } })],
  ["base 子对象", JSON.stringify({ base: { lat: 31.2, lng: 121.5 } })],
  ["字符串数字（现场手抄常见）", JSON.stringify({ lat: "31.2", lng: "121.5" })],
  ["GeoJSON Point（顺序 [经度,纬度]）",
   JSON.stringify({ type: "Point", coordinates: [121.5, 31.2] })],
  ["GeoJSON Feature(Point)", JSON.stringify({ type: "Feature", properties: {},
     geometry: { type: "Point", coordinates: [121.5, 31.2] } })],
  ["GeoJSON FeatureCollection（取第一个点）", JSON.stringify({ type: "FeatureCollection",
     features: [{ type: "Feature", properties: {},
                  geometry: { type: "Point", coordinates: [121.5, 31.2] } }] })],
];
for (const [label, txt] of forms) {
  const r = P.baseParse(txt);
  ck("能读：" + label, r.ok && near(r.base.lat, 31.2, 1e-9) && near(r.base.lng, 121.5, 1e-9),
     JSON.stringify(r));
}
/* ★GeoJSON 顺序弄反会落到地球另一边（且范围校验拦不住）—— 用一个**纬度不合法**的坐标
   来证明顺序真的按 GeoJSON 规则读：[lng=121.5, lat=31.2] 合法；[31.2, 121.5] 应报越界。 */
const swapped = P.baseParse(JSON.stringify({ type: "Point", coordinates: [31.2, 121.5] }));
ck("★GeoJSON 顺序按 [经度, 纬度] 读（反了会得到 lat=121.5 → 越界报错，而不是默默错）",
   !swapped.ok && swapped.err === "base_off_globe", JSON.stringify(swapped));
/* 畸形的 GeoJSON 不能炸：首要素没有 geometry / features 为空 → 一律归到“认不出字段”这个
   可见错误（**不是** 抛异常、也**不是**默默用默认值）—— 这也是 review 提到的那一处。 */
const noGeom = P.baseParse(JSON.stringify({ type: "FeatureCollection",
  features: [{ type: "Feature", properties: { lat: 31.2, lng: 121.5 } }] }));
ck("FeatureCollection 首要素无 geometry → 可见错误（不抛、不静默用默认值）",
   !noGeom.ok && noGeom.err === "base_need_point", JSON.stringify(noGeom));
const emptyFc = P.baseParse(JSON.stringify({ type: "FeatureCollection", features: [] }));
ck("FeatureCollection 空 features → 同样是可见错误",
   !emptyFc.ok && emptyFc.err === "base_need_point", JSON.stringify(emptyFc));
ck("float 数组/对象里夹着非数字也不炸（不抛，给错误码）",
   !P.baseParse(JSON.stringify({ lat: 31, lng: [1, 2] })).ok);

/* ================= 2 · 校验与错误码（不静默回落） ================= */
const bads = [
  ["非法 JSON", "{ lat: 31.2 }", "base_bad_json"],
  ["空文本", "", "base_bad_json"],
  ["认不出纬经度字段", JSON.stringify({ x: 1, y: 2 }), "base_need_point"],
  ["顶层是数组（不是点）", JSON.stringify([1, 2]), "base_need_point"],
  ["NaN（数字字面量写不出来 → 用字符串）", JSON.stringify({ lat: "abc", lng: 1 }), "base_need_point"],
  ["只有一半字段", JSON.stringify({ lat: 31.2 }), "base_need_point"],
  ["纬度为 null", JSON.stringify({ lat: null, lng: 121.5 }), "base_need_point"],
  ["纬度越界（91）", JSON.stringify({ lat: 91, lng: 121.5 }), "base_off_globe"],
  ["经度越界（181）", JSON.stringify({ lat: 31.2, lng: 181 }), "base_off_globe"],
  ["纬度负向越界（-90.5）", JSON.stringify({ lat: -90.5, lng: 0 }), "base_off_globe"],
];
for (const [label, txt, code] of bads) {
  const r = P.baseParse(txt);
  ck("拒绝并给出错误码：" + label, !r.ok && r.err === code, JSON.stringify(r));
}
ck("越界时**带上实际值**（页面能原样显示给人看，不用去猜）",
   P.baseParse(JSON.stringify({ lat: 91, lng: 121.5 })).lat === 91);
/* 边界值必须合法（板端/极地也可能用到） */
for (const [la, ln] of [[90, 180], [-90, -180], [0, 0]]) {
  ck(`边界合法：lat=${la} lng=${ln}`,
     P.baseParse(JSON.stringify({ lat: la, lng: ln })).ok);
}
ck("∞ 不是数字（JSON.parse 接受 1e999=∞，必须拦下）",
   !P.baseParse('{"lat": 1e999, "lng": 0}').ok);
ck("baseCheck(null) → base_need_point", P.baseCheck(null).err === "base_need_point");

/* ================= 3 · 往返（导出 → 导入） ================= */
setBase(31.2345678, 121.9876543);
const exported = P.baseExportText();
const back = P.baseParse(exported);
ck("导出→导入 往返（同一个基点，1e-12）",
   back.ok && near(back.base.lat, 31.2345678, 1e-12) && near(back.base.lng, 121.9876543, 1e-12),
   JSON.stringify(back));
ck("导出文本里带 `orpah_base` 版本标记（将来改格式能认出来）",
   JSON.parse(exported).orpah_base === 1);
ck("导出文本带来源（default/manual/imported 之一）",
   ["default", "manual", "imported"].indexOf(JSON.parse(exported).source) >= 0);

/* ================= 4 · 换算语义（基点错 = 平移） ================= */
setBase(31.2, 121.5);
const p0 = P.toLatLng(0, 0);
ck("toLatLng(0,0) = 基点本身", near(p0[0], 31.2) && near(p0[1], 121.5));
const pN = P.toLatLng(0, 110540);
ck("向北 110540 m = 纬度 +1°", near(pN[0], 32.2, 1e-9), JSON.stringify(pN));
const mPerLngDeg = 111320 * Math.cos(31.2 * Math.PI / 180);
const pE = P.toLatLng(mPerLngDeg, 0);
ck("向东 111320·cos(基点纬度) m = 经度 +1°（经度按基点纬度压缩）",
   near(pE[1], 122.5, 1e-9), JSON.stringify(pE));
/* ★这条锁住“基点错 = 整条轨迹平移”：同一个点在新旧基点下的增量 = 基点之差 */
setBase(31.3, 121.7);                       // 基点整体 +0.1 / +0.2
const p0b = P.toLatLng(0, 0);
ck("★改基点 ⇒ 该点整体平移（纬度增量 = 基点纬度之差）",
   near(p0b[0] - p0[0], 0.1, 1e-9), JSON.stringify([p0, p0b]));
/* 经度方向**不是**严格相加：米/度换算按基点纬度取 cos 缩放 —— 换基点会同时换比例，
   所以经度增量与基点经度之差有一个 ~3e-5 的相对差（演示场景里 < 0.01 m，可忽略但要如实写）。 */
ck("★经度增量 ≈ 基点经度之差（米/度按基点纬度缩放，不是严格相加）",
   Math.abs((p0b[1] - p0[1]) - 0.2) < 1e-4,
   `实际增量 ${(p0b[1] - p0[1]).toFixed(6)}，期望 ≈0.2`);
/* ★真正的“不是缩放”判据：纬度方向是**纯平移**（与点位置无关，逐位相同）。
   经度方向除平移外还有一个与 x 有关的极小比例项（米/度按基点纬度取 cos：基点纬度变 0.1°
   → 500 m 东向点的经度增量再差 ~4e-6°，约 0.4 m）—— 这是**等距圆柱近似**本身的性质，
   所以基点要取在场地附近（几公里内），别拿它去跨纬度用。这条锁住“别把差异量级搞大”。 */
setBase(31.3, 121.7);
const a1 = P.toLatLng(0, 0), b1 = P.toLatLng(500, 300);
setBase(31.2, 121.5);
const a2 = P.toLatLng(0, 0), b2 = P.toLatLng(500, 300);
ck("★纬度方向是纯平移（远近两点位移逐位相同）",
   near(a1[0] - a2[0], b1[0] - b2[0], 1e-12), JSON.stringify([a1[0] - a2[0], b1[0] - b2[0]]));
ck("★经度方向：平移为主，极小比例项如实限住（500 m 内差 < 1e-5°，约 1 m）",
   Math.abs((a1[1] - a2[1]) - 0.2) < 1e-9
   && Math.abs((b1[1] - b2[1]) - 0.2) < 1e-5,
   JSON.stringify([a1[1] - a2[1], b1[1] - b2[1]]));
setBase(31.2, 121.5);
ck("0.001° 纬度 ≈ 110.5 m（提示文案里那句数量级是对的）",
   Math.abs(110540 * 0.001 - 110.54) < 0.01);

/* ================= 5 · 存储（本机 localStorage） ================= */
P.baseStoreClear();
ck("清空后 baseStoreRead() = null（不是默认值）", P.baseStoreRead() === null);
setBase(31.4, 121.6);
ck("写成功返回 true", P.baseStoreWrite({ lat: 31.4, lng: 121.6 }) === true);
const rd = P.baseStoreRead();
ck("读回来的就是写进去的", rd && near(rd.lat, 31.4) && near(rd.lng, 121.6), JSON.stringify(rd));
store["orpah.base.v1"] = "{ 坏掉的 }";
ck("存储里是坏数据 → 当没有（null），**不把它当默认值用**", P.baseStoreRead() === null);
store["orpah.base.v1"] = JSON.stringify({ lat: 999, lng: 0 });
ck("存储里是越界值 → 当没有（不静默采信坏坐标）", P.baseStoreRead() === null);
/* localStorage 抛异常（隐私模式/配额满）不能让页面崩 */
const realLS = global.window.localStorage;
global.window.localStorage = { getItem() { throw new Error("denied"); },
                              setItem() { throw new Error("denied"); },
                              removeItem() { throw new Error("denied"); } };
ck("localStorage 拒绝访问时：读给 null、写/清给 false，**不抛**",
   P.baseStoreRead() === null && P.baseStoreWrite({ lat: 1, lng: 2 }) === false
   && P.baseStoreClear() === false);
global.window.localStorage = realLS;

/* ================= 6 · 来源三态 + 开页应用 ================= */
P.baseStoreClear();
setBase(D.lat, D.lng);
ck("来源：默认值 → default", P.baseSource() === "default");
setBase(31.9, 121.9);
ck("来源：手改输入框 → manual", P.baseSource() === "manual");
const imp = P.baseImport(JSON.stringify({ lat: 30.5, lng: 120.5, name: "现场A" }));
ck("导入成功（ok + 已存 + 写进输入框）",
   imp.ok && imp.stored === true
   && E("baseLat").value === "30.5" && E("baseLng").value === "120.5",
   JSON.stringify([imp, E("baseLat").value, E("baseLng").value]));
ck("来源：导入后 → imported", P.baseSource() === "imported");
const impBad = P.baseImport("nope");
ck("导入失败：ok=false + 错误码，且**不改**当前基点（不静默回落）",
   !impBad.ok && impBad.err === "base_bad_json"
   && P.baseSource() === "imported" && E("baseLat").value === "30.5");
E("baseLat").value = "1"; E("baseLng").value = "2";
const applied = P.baseApplyStored();
ck("开页应用：把存储里的基点写回输入框（在建地图之前调）",
   applied && near(applied.lat, 30.5) && E("baseLat").value === "30.5");
ck("baseErrText 把错误码转成可读文案，并带上 detail/实际值",
   P.baseErrText({ err: "base_bad_json", detail: "boom" }).indexOf("boom") >= 0
   && P.baseErrText({ err: "base_off_globe", lat: 91, lng: 0 }).indexOf("91") >= 0);
ck("baseInit 不炸（元素齐全时挂上三个入口）", (P.baseInit(() => {}) , true));

/* 页面守卫用的记号（Python 侧读这行） */
console.log("  PAGE_GUARD_READY");
"""


def run_node():
    node = shutil.which("node")
    if not node:
        print("  FAIL 找不到 node（本套件直接跑 map.js 原文，需要 node 在 PATH 上）")
        return 1
    fd, path = tempfile.mkstemp(suffix=".js", prefix="mapjstest_")
    os.close(fd)
    fail = 0
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(HARNESS)
        r = subprocess.run([node, path, MAP_JS], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
        print(out.rstrip())
        n_ok, n_bad = out.count("  OK "), out.count("  FAIL ")
        print(f"  （map.js 检查 {n_ok} 条通过 / {n_bad} 条失败）")
        if r.returncode != 0 or "FAIL" in out:
            fail = 1
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return fail


def page_guard():
    """两页守卫：默认值单一源、导入入口齐全（否则只有一页能用导入）。"""
    bad = 0
    src = open(MAP_JS, encoding="utf-8").read()
    m = re.search(r"BASE_DEFAULT\s*=\s*\{\s*lat:\s*([-\d.]+),\s*lng:\s*([-\d.]+)", src)
    if not m:
        print("  FAIL map.js 里找不到 BASE_DEFAULT（默认基点必须在这一个地方）")
        return 1
    lat, lng = m.group(1), m.group(2)
    for page in ("track.html", "replay.html"):
        html = open(os.path.join(HERE, "ui", "static", page), encoding="utf-8").read()
        if f'id="baseLat" type="number" step="any" value="{lat}"' not in html \
           or f'id="baseLng" type="number" step="any" value="{lng}"' not in html:
            print(f"  FAIL {page}：基点输入框的 value 与 BASE_DEFAULT（{lat}/{lng}）不一致 —— "
                  "页面里的 value 只是**离线兜底**，必须与服务端/单一源同值")
            bad += 1
        else:
            print(f"  OK   {page}：基点默认值与 BASE_DEFAULT 一致")
        for need, why in (('id="baseFile"', "导入用的文件框"),
                          ('id="btnBaseImport"', "导入按钮"),
                          ('id="btnBaseExport"', "导出按钮"),
                          ('id="btnBaseReset"', "复位按钮"),
                          ('id="baseNote2"', "状态行（导入失败必须可见）")):
            if need not in html:
                print(f"  FAIL {page}：缺 {need} —— {why}")
                bad += 1
        if "baseApplyStored()" not in html or "baseInit(" not in html:
            print(f"  FAIL {page}：没调 baseApplyStored()/baseInit() —— "
                  "基点导入/记忆在这一页不会生效")
            bad += 1
        else:
            print(f"  OK   {page}：开页套用存储基点 + 挂上导入/导出/复位")
    if bad:
        return 1
    return 0


def i18n_guard():
    """口径守卫：基点口径必须写明（这是**最容易被人当成“准的”**的那种输入）。"""
    src = open(I18N_JS, encoding="utf-8").read()
    bad = 0
    checks = [("base_hint", ["WGS84", "GCJ-02", "本机浏览器", "等距圆柱"]),
              ("base_hint", ["WGS84", "GCJ-02", "this browser", "equirectangular"]),
              ("base_src_default", ["演示默认值", "不是现场值"]),
              ("base_src_manual", ["未存"]),
              ("base_export_ok", ["再导回来"]),
              ("base_import_fail", ["{why}"])]
    for key, words in checks:
        # 同一个键在字典里出现两次（zh/en）—— 任一行满足即可（不按行号猜语言）
        lines = [ln for ln in src.splitlines() if f'"{key}"' in ln]
        if not lines:
            print(f"  FAIL 文案字典缺少 {key}")
            bad += 1
            continue
        hit = next((ln for ln in lines if all(w in ln for w in words)), None)
        if hit is None:
            print(f"  FAIL {key} 里少了 {words} 之一（基点口径必须写明，别让人以为是准的）")
            bad += 1
        else:
            print(f"  OK   {key} 写明：{'/'.join(words)}")
    return bad


def main():
    print("== 基点导入/换算/存储（map.js 原文，node 跑） ==")
    fail = run_node()
    print("== 页面守卫（两页一致、入口齐全） ==")
    fail += page_guard()
    print("== 口径守卫（WGS84 / 不做转换 / 本机存储 / 不是现场值） ==")
    fail += i18n_guard()
    if fail:
        print(f"地图共享件：有问题（{fail} 处）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
