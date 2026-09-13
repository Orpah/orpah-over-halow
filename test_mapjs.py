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
   `base_src_default` 必须写明**不是现场值**；
9. **野外包（2026-09-13）**：`maps.py`（本地 XYZ 瓦片目录）的列包/数瓦片/上限截断、
   `resolve_tile()` 的**路径穿越与扩展名白名单**（逐条试）、**前端模板 ↔ 服务端解析器同形状**、
   以及「填选项 / 记住选择 / 没有包 / 取不到列表 / 包消失」五种状态都**可见且分得清**。

运行：C:\\Python313\\python.exe test_mapjs.py     （需要 node 在 PATH 上）
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote

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
                "baseLat", "baseLng", "baseInit",
                "PACK_KEY", "PACK_TPL", "TILE_PROVIDERS", "TILE_ORDER",
                "packName", "packTileUrl", "loadPacks", "packNoteRender",
                "packStoreRead", "packStoreWrite", "currentTileUrl", "packInit",
                "addBaseLayer", "baseOfflineNow", "goOffline"];
/* ---- 桩：DOM/存储 ---- */
/* `E(id)` 现在也当得了 <select>：`innerHTML=""` 清选项、`appendChild` 收选项（第一个自动选中，
   与浏览器的默认行为一致）—— 野外包列表要往 select 里填项，不铺这层就没法测。 */
const els = {};
function E(id) {
  if (els[id]) return els[id];
  const o = { id, value: "", textContent: "", files: [], _opts: [],
              set innerHTML(v) { o._opts.length = 0; },
              get innerHTML() { return ""; },
              appendChild(node) { o._opts.push(node); if (o._opts.length === 1) o.value = node.value; },
              addEventListener(type, fn) { (o._ev = o._ev || {}); (o._ev[type] = o._ev[type] || []).push(fn); } };
  els[id] = o;
  return o;
}
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
/* 最小 Leaflet 桩：只够跑「底图图层 + 离线回落」的逻辑（不校验地图渲染）。
   陷阱：`L.control()` 返回的控制对象要能被赋值 onAdd（goOffline 会挂离线提示）。 */
function layerStub() { return { on() { return this; }, addTo() { return this; }, remove() { return this; } }; }
/* 注意：`L.control` 既是函数（`L.control({position})`）又带静态方法（`L.control.scale(...)`）——
   两处都会用到，桩得两个都给，否则 goOffline() 里 `L.control.scale is not a function`。 */
const controlFn = () => ({ addTo() { return this; }, remove() { return this; } });
controlFn.scale = () => layerStub();
global.L = {
  tileLayer: () => layerStub(),
  GridLayer: { extend: () => function () { return layerStub(); } },
  control: controlFn,
  DomUtil: { create: () => ({ style: {}, appendChild() {} }) },
  DomEvent: { disableClickPropagation() {} },
};
/* fetch 桩：`fetchReply = null` 表示“取不到”（网络类错误）—— 野外包那种“取不到 vs 没有”要分开测 */
let fetchReply = { packs: [] };
global.fetch = async () => {
  if (!fetchReply) throw new Error("boom");
  return { json: async () => fetchReply };
};
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

/* ================= 7 · 野外包（本地 XYZ 瓦片目录） =================
   与 maps.py 是一对：**URL 形状必须两边一致**（这里拼、那边解），下面最后一条就是锁这个。 */
ck("野外包在瓦片源列表里（中英两套都有）",
   P.TILE_ORDER.zh.indexOf("pack") >= 0 && P.TILE_ORDER.en.indexOf("pack") >= 0);
ck("野外包放最后（它是本地目录，不是“网上某个源”，默认不选）",
   P.TILE_ORDER.zh[P.TILE_ORDER.zh.length - 1] === "pack");
ck("野外包有条款提示键（合规口径必须在源列表里）",
   P.TILE_PROVIDERS.pack.note === "tile_note_pack"
   && P.TILE_PROVIDERS.pack.i18nName === "tk_tile_pack",
   JSON.stringify(P.TILE_PROVIDERS.pack));
ck("★URL 形状与 maps.resolve_tile() 一致（改一边不改另一边 = 一律 404）",
   P.PACK_TPL === "/maps/{name}/{z}/{x}/{y}.png", P.PACK_TPL);
E("packSel").value = "region";
ck("选中包 → 拼出本地瓦片 URL", P.packTileUrl() === "/maps/region/{z}/{x}/{y}.png", P.packTileUrl());
ck("包名做 URL 编码（现场目录名可能带空格/中文）",
   (function () { E("packSel").value = "区域 A";
      const u = P.packTileUrl(); E("packSel").value = "region";
      return u === "/maps/" + encodeURIComponent("区域 A") + "/{z}/{x}/{y}.png"; })());
E("tileSrc").value = "pack";
ck("currentTileUrl()：tileSrc=pack → 野外包 URL",
   P.currentTileUrl() === "/maps/region/{z}/{x}/{y}.png", P.currentTileUrl());
E("packSel").value = "";
ck("没选到包 → 空 URL（addBaseLayer 直接判离线，不向服务端发一堆无意义 404）",
   P.currentTileUrl() === "");
E("tileSrc").value = "osm";
ck("currentTileUrl()：非 pack 源不受野外包影响",
   P.currentTileUrl().indexOf("tile.openstreetmap.org") >= 0);

/* 三种加载结果都要**分得清**（这正是现场最容易看错的地方）。
   ★必须包在 async IIFE 里：本 harness 是 node 的 CJS 脚本（`require`），**没有顶层 await**。
   结尾打 ASYNC_DONE —— Python 侧检它，防“异步段静默截断却看着全绿”。 */
(async () => {
fetchReply = { packs: [{ name: "region", tiles: 12, zooms: [10, 11], truncated: false }] };
await P.loadPacks();
ck("loadPacks：填选项（包名）",
   E("packSel").value === "region" && P.packName() === "region",
   JSON.stringify([E("packSel").value, E("packSel")._opts.map((o) => o.value)]));
/* 状态行的**代入**要真验：桩 T 只回键名（不含占位符）→ 临时换成带占位符的桩，
   确认 {name}/{zooms}/{n} 全被换掉（否则用户会看到生占位符）。 */
const T0 = global.T;
global.T = (k) => (k === "map_pack_status" ? "包 {name}：{zooms} 级 / {n} 张" : k);
P.packNoteRender();
const statusText = E("packNote").textContent;
global.T = T0;
ck("loadPacks：状态行写了包名/层级/张数（能看出这个包大不大）",
   statusText.indexOf("region") >= 0 && statusText.indexOf("12") >= 0
   && statusText.indexOf("10/11") >= 0 && statusText.indexOf("{") < 0,
   statusText);
ck("loadPacks：状态行带上「包不入库 / 只有跑 ui_server 的这台机器可见」的口径",
   E("packNote").textContent.indexOf("map_pack_hint") >= 0, E("packNote").textContent);
/* 记住选择：下次开页直接回到同一个包 */
P.packStoreWrite("region");
E("packSel").value = "";
await P.loadPacks();
ck("记住的包在列表里 → 自动选回它（现场不用每次重选）",
   E("packSel").value === "region", E("packSel").value);
/* 一个包都没有：空列表选不中任何包，但**不静默** —— 得说清怎么做 */
fetchReply = { packs: [] };
await P.loadPacks();
ck("★没有包 → 占位选项 + 状态行说“放哪里、然后重新扫描”（不是空白）",
   E("packSel").value === "" && E("packSel")._opts.length === 1
   && E("packNote").textContent.indexOf("map_pack_none") === 0,
   JSON.stringify([E("packSel").value, E("packNote").textContent]));
/* 取不到列表（服务端挂了/网络错）**不能**说成“没有包” */
fetchReply = null;
await P.loadPacks();
ck("★取不到包列表 ≠ 没有包：状态行给的是失败原因",
   E("packNote").textContent.indexOf("map_pack_fail") === 0, E("packNote").textContent);
/* 包被删了 / 重新扫描后列表里没了：**不能**与「本来就没放包」混为一谈（处置不同：
   一个是“去看包去哪了”，一个是“去放包”）。先让列表里有东西，才分得出这两种。 */
fetchReply = { packs: [{ name: "other", tiles: 1, zooms: [10] }] };
await P.loadPacks();
E("packSel").value = "gone";
P.packNoteRender();
ck("包被删了/重扫后消失 → 状态行说“服务器上没有了”（仍可回落网格）",
   E("packNote").textContent.indexOf("map_pack_unknown") === 0, E("packNote").textContent);
/* 接线入口：map.js 先于页面脚本加载（那时 `$` 还不存在）→ 顶层不能碰 DOM，必须靠 packInit()
   （踩过：在顶层写 `$("packSel")` 让整页 `$ is not defined`）。
   ★回调必须真的被叫：换包了不重建图层 = 地图上还是旧瓦片（且不报错）。 */
let cbN = 0;
P.packInit(() => { cbN++; });
ck("packInit 不炸，并挂上「重新扫描」", typeof E("btnPackReload").onclick === "function");
E("packSel").value = "region";
E("packSel")._ev.change.forEach((f) => f());
ck("★换包会通知页面重建底图，并把选择记进 localStorage",
   cbN === 1 && P.packStoreRead() === "region", JSON.stringify([cbN, P.packStoreRead()]));
await E("btnPackReload").onclick();
ck("★「重新扫描」扫完也通知一次（否则列表变了地图还是旧瓦片）", cbN === 2, cbN);
/* ★离线回落态必须被**下一次** addBaseLayer 清掉（2026-09-13 实测发现的真 bug）：
   旧代码只重置计数 → 换源/重建地图后 `baseOffline` 仍然为 true：
   ① 状态说谎；② `tileerror` 里的 `if (baseOffline) return;` 让**新的**底图再挂也不回落
   → 地图空白 + 无提示（野外包瓦片不全时最容易碰）。 */
E("tileSrc").value = "pack";
E("packSel").value = "";                    // 没选到包 → 空 URL → 直接判离线
P.addBaseLayer({});
ck("没选到包 → 直接判离线（不发无意义请求）", P.baseOfflineNow() === true);
P.addBaseLayer({});                          // 换了有效的包再来一次（页面重建图层就是这条路）
ck("包没变（仍空 URL）→ 仍离线", P.baseOfflineNow() === true);
E("packSel").value = "region";
P.addBaseLayer({});
ck("★有包了 → 离线标志被清掉（否则地图空白也无提示）", P.baseOfflineNow() === false,
   String(P.baseOfflineNow()));
console.log("  ASYNC_DONE");
})();
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
        if "ASYNC_DONE" not in out:          # 异步段没跑完（顶层 await 被拆、或 promise 挂了）→ 不能算绿
            print("  FAIL node 侧异步段没跑完（没看到 ASYNC_DONE）—— 野外包那几条其实是没测")
            return 1
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


def pack_guard():
    """野外包（本地 XYZ 瓦片目录）：`maps.py` 的逻辑 + 两侧接线。

    用**临时目录**造小包 —— 不碰真实的 `ui/static/maps/`（现场可能真放了包，测试不能依赖它、
    更不能往里写东西）。重点是 `resolve_tile()`：它是**路径穿越的唯一防线**，所以逐条试。
    """
    sys.path.insert(0, HERE)
    import maps                                          # noqa: E402
    bad = 0

    def ck(name, cond, extra=""):
        nonlocal bad
        if cond:
            print(f"  OK   {name}")
        else:
            print(f"  FAIL {name}" + (f"   {extra}" if extra else ""))
            bad += 1

    root = tempfile.mkdtemp(prefix="packtest_")
    try:
        def put(rel):
            p = os.path.join(root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n")            # 内容无所谓，只看路径解析
            return p

        put("packA/10/1/2.png")
        put("packA/11/3/4.png")
        put("区域A/10/1/2.png")                          # 中文包名（现场目录名就是中文）
        put(".hidden/10/0/0.png")                        # 隐藏目录不算包
        os.makedirs(os.path.join(root, "empty"))         # 空目录不算包
        with open(os.path.join(root, "note.txt"), "w") as f:
            f.write("说明")                              # 顶层文件不是包

        packs = maps.list_packs(root)
        names = sorted(p["name"] for p in packs)
        ck("列出包：只算有瓦片的目录（空目录/隐藏目录/顶层文件都不是包）",
           names == sorted(["packA", "区域A"]), names)
        a = [p for p in packs if p["name"] == "packA"][0]
        ck("包信息：瓦片数 + 用到的 zoom 层级",
           a["tiles"] == 2 and a["zooms"] == [10, 11] and a["truncated"] is False, a)
        t = maps.list_packs(root, cap=1)
        ck("数到上限就停并标 truncated（不为一个显示用的数字走完整个包）",
           all(p["truncated"] and p["tiles"] == 1 for p in t), t)
        ck("目录不存在 → 空列表（没放包是正常状态，不是异常）",
           maps.list_packs(os.path.join(root, "nope")) == [])

        # ---- 只允许 <包名>/<z>/<x>/<y>.<白名单扩展名> 一种形状 ----
        ok_p, ok_ct = maps.resolve_tile("packA/10/1/2.png", root)
        ck("合法路径 → 绝对路径 + Content-Type",
           ok_p == os.path.join(root, "packA", "10", "1", "2.png") and ok_ct == "image/png",
           (ok_p, ok_ct))
        ck("中文包名（浏览器会 percent-encode）也能解析",
           maps.resolve_tile("区域A/10/1/2.png", root)[0] is not None
           and maps.resolve_tile(quote("区域A") + "/10/1/2.png", root)[0] is not None)
        ck("大写扩展名不差分（.PNG 当 .png）",
           maps.resolve_tile("packA/10/1/2.PNG", root)[1] == "image/png")
        for label, rel, why in [
            ("穿越：..", "../10/1/2.png", "pack"),
            ("穿越：包里再爬一层", "packA/../../10/1/2.png", "shape"),
            ("穿越：绝对路径", "/etc/passwd.png", "shape"),
            ("隐藏目录（. 开头）", ".hidden/10/0/0.png", "pack"),
            ("层级不够（缺 y）", "packA/10/1", "shape"),
            ("层级多一层", "packA/10/1/2/3.png", "shape"),
            ("扩展名不在白名单（.svg）", "packA/10/1/2.svg", "ext"),
            ("扩展名不在白名单（.txt）", "packA/10/1/2.txt", "ext"),
            ("x 不是数字", "packA/10/x/2.png", "nums"),
            ("z 越界（>24）", "packA/99/1/1.png", "nums"),
            ("x/y 超出该 zoom 的格子范围", "packA/10/2048/1.png", "nums"),
        ]:
            p, got = maps.resolve_tile(rel, root)
            ck(f"拒绝：{label}", p is None and got == why, f"→ {p} / {got}")

        # ---- 与前端同一形状（服务端解析器 vs map.js 的 URL 模板）----
        js = open(MAP_JS, encoding="utf-8").read()
        m = re.search(r'PACK_TPL\s*=\s*"([^"]+)"', js)
        ck("map.js 里有唯一的 URL 模板常量 PACK_TPL", bool(m))
        if m:
            tpl = m.group(1)
            rel = (tpl[len("/maps/"):].replace("{name}", "区域A")
                   .replace("{z}", "10").replace("{x}", "1").replace("{y}", "2"))
            p, why = maps.resolve_tile(rel, root)
            ck("★前端模板拼出的路径，服务端解析器必须认得（改一边不改另一边 = 一律 404）",
               p is not None, f"{tpl} → {why}")

        # ---- 接线守卫 ----
        srv = open(os.path.join(HERE, "ui_server.py"), encoding="utf-8").read()
        ck("ui_server 提供 /api/maps（页面列表源）", '"/api/maps"' in srv)
        ck("ui_server 提供 /maps/… 瓦片路由，且走 maps.resolve_tile()",
           '"/maps/"' in srv and "maps.resolve_tile(" in srv)
        git_ignore = open(os.path.join(HERE, ".gitignore"), encoding="utf-8").read()
        ck("★野外包**不入库**（.gitignore 有 ui/static/maps/）",
           "ui/static/maps/" in git_ignore)
        for page in ("track.html", "replay.html"):
            html = open(os.path.join(HERE, "ui", "static", page), encoding="utf-8").read()
            miss = [k for k in ('id="packSel"', 'id="packNote"', 'id="btnPackReload"')
                    if k not in html]
            ck(f"{page}：野外包选择框/状态行/重新扫描都在", not miss, miss)
            ck(f"{page}：开页拉包列表（loadPacks）", "loadPacks(" in html)
            ck(f"{page}：调 packInit(cb) 接线（map.js 顶层碰不了 DOM；换包要重建图层）",
               bool(re.search(r"packInit\(\s*[^)\s]", html)))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return bad


def main():
    print("== 基点导入/换算/存储（map.js 原文，node 跑） ==")
    fail = run_node()
    print("== 页面守卫（两页一致、入口齐全） ==")
    fail += page_guard()
    print("== 口径守卫（WGS84 / 不做转换 / 本机存储 / 不是现场值） ==")
    fail += i18n_guard()
    print("== 野外包（本地 XYZ 目录）：maps.py 逻辑 + 两侧接线 ==")
    fail += pack_guard()
    if fail:
        print(f"地图共享件：有问题（{fail} 处）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
