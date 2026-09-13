#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_posjs.py — 定位内核 `ui/static/pos.js` 的离线自检（用 node 跑同一份源码）

为什么单独测 JS：`pos.js` 是 `track.html` / `rssi.html` / `replay.html` **共用的唯一实现**
（定位/质量/平滑/CDF 全在里面），但此前只有“浏览器里手工看一眼”这种覆盖 —— 改坏了前端
不报错、只是位置静默偏。这里把它当纯函数库跑一遍：**不复制算法**，直接把 `pos.js`
原文塞进 node 执行（`new Function` 的独立作用域，`"use strict"` 下也能取到导出）。

覆盖：RSSI↔距离往返、三边定位复原、WLS（含远端差站权重）、95% 椭圆、观测归集（不看未来）、
卡尔曼因果性、轨迹抖动、**误差 CDF/分位数/真值插值**（回放页那张误差 CDF 就靠它们）、
**多观测一致性判定 `consensus`（「孤证不立」）**：一致→verified、单台偏差大→剔除、无冗余→如实降级、
互相矛盾→conflict、**移动目标不做补偿就会把诚实观测当成离群（假警报）**。

运行：C:\\Python313\\python.exe test_posjs.py     （需要 node 在 PATH 上）
"""
import json
import os
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
POS_JS = os.path.join(HERE, "ui", "static", "pos.js")

HARNESS = r"""
const fs = require("fs");
const EXPORT = ["rssiFromDist", "distFromRssi", "trilaterate", "wlsLocate", "ellipseOf",
                "ellipsePoints", "median", "qualityOf", "layoutCond", "gdopGrade",
                "resGrade", "latestAt", "obsOfStation", "obsAt", "kalmanTrack",
                "pathJitter", "obsSegments", "segIndexAt", "nextValidAt",
                "truthAt", "errorsOf", "quantileOf", "cdfOf",
                "gdopOf", "suggestStation", "samplesFor", "frameGaps", "consensus"];
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
function near(a, b, tol) { return Math.abs(a - b) <= (tol === undefined ? 1e-6 : tol); }

/* ---- RSSI ↔ 距离往返 ---- */
ck("RSSI→距离→RSSI 往返（A=-40 n=2.5, -70dBm）",
   near(P.rssiFromDist(P.distFromRssi(-70, -40, 2.5), -40, 2.5), -70, 1e-6));

/* ---- 三边定位：三个精确圆 → 精确解 ---- */
const anchors = [{ x: 0, y: 0 }, { x: 30, y: 0 }, { x: 0, y: 26 }];
const d = anchors.map(a => Math.hypot(5 - a.x, 5 - a.y));
const lin = P.trilaterate(anchors, d, null);
ck("三边定位（精确距离）复原 (5,5)", lin && near(lin.x, 5, 1e-6) && near(lin.y, 5, 1e-6),
   JSON.stringify(lin));

/* ---- WLS：远端差站被降权（与**等权线性解**对比 —— 只有 3 台站位时谁也拒不了坏测量，
       所以这里只能断言“更准”，不能断言“准”） ---- */
const dBad = d.slice();
dBad[2] += 40;                                            // 第三台站测距差 +40m
const linBad = P.trilaterate(anchors, dBad, null);
const obs = anchors.map((a, i) => ({ s: a, dist: dBad[i] }));
const w = P.wlsLocate(obs, linBad);
const eEq = linBad ? Math.hypot(linBad.x - 5, linBad.y - 5) : NaN;
const eW = (w && w.ok) ? Math.hypot(w.x - 5, w.y - 5) : NaN;
ck("WLS：远端差站（+40m）比等权线性解明显更接近真值（误差比 < 0.5）",
   eW < eEq * 0.5, `等权=${eEq.toFixed(2)}m 加权=${eW.toFixed(2)}m`);
/* 入参形状就是 `{s:{x,y}, dist}`（页面 obsAt 返回的形状）—— 用精确距离证明它被正确读取：
   若真读成 `o.x`（undefined）会得到 NaN/垃圾，而非 1e-9 级复原。 */
const exact = anchors.map(a => ({ s: a, dist: Math.hypot(5 - a.x, 5 - a.y) }));
const we = P.wlsLocate(exact, { x: 0, y: 0 });
ck("WLS：标准入参（{s,dist}）在精确距离下复原 (5,5)（证明读的是 o.s.x/o.s.y）",
   we && we.ok && Math.hypot(we.x - 5, we.y - 5) < 1e-6,
   JSON.stringify(we && { x: we.x, y: we.y }));

/* ---- 95% 椭圆（注意入参是 {a,b,c}，不是矩阵） ---- */
const ell = P.ellipseOf({ a: 4, b: 0, c: 1 });
ck("椭圆：特征值→半轴（√(5.991·4) / √(5.991·1)，长短轴比 2）",
   ell && near(ell.semiMajor / ell.semiMinor, 2, 1e-9)
   && near(ell.semiMajor, Math.sqrt(5.991) * 2, 1e-9)
   && near(ell.radius, ell.semiMajor, 1e-12) && ell.degenerate === false,
   JSON.stringify(ell));
ck("椭圆：退化协方差（近乎共线）→ degenerate=true（半径仍可给，形状不可信）",
   P.ellipseOf({ a: 4, b: 0, c: 1e-6 }).degenerate === true
   && P.ellipseOf(null) === null);

/* ---- 观测归集：**不看未来** ---- */
const st = [{ sid: "A", name: "A", x: 0, y: 0, rssi: null, t0: null, t1: null }];
const smp = { A: [{ t: 1000, rssi: -60 }, { t: 9000, rssi: -50 }] };
const o1 = P.obsOfStation(st[0], smp.A, 5000, -40, 2.5);
const o2 = P.obsOfStation(st[0], smp.A, 9000, -40, 2.5);
ck("观测归集：t=5000 只看得到 1000 那条（不看未来）", o1 && o1.rssi === -60);
ck("观测归集：t=9000 用最新那条", o2 && o2.rssi === -50);
ck("观测归集：超过时效（30s）的旧样本不算当前观测",
   P.obsOfStation(st[0], smp.A, 60000, -40, 2.5) === null);
ck("obsAt：无站位 → 空数组", P.obsAt([], {}, 0, -40, 2.5).length === 0);

/* ---- 卡尔曼：因果（不吃未来）+ 平滑更接近真值 ---- */
const frames = [];
for (let i = 0; i <= 40; i++) {
  const t = 1000 + i * 1000;
  frames.push({ t, x: Math.sin(i / 4) * 10 + ((i % 3) - 1) * 0.8, y: (i % 5) - 2, cov: [[4, 0], [0, 4]] });
}
const sm = P.kalmanTrack(frames);
ck("卡尔曼：非有限/负方差不会写进输出",
   sm.every(p => Number.isFinite(p.x) && Number.isFinite(p.y)));
const j0 = P.pathJitter(frames), j1 = P.pathJitter(sm);
ck("卡尔曼：平滑后轨迹抖动不高于原始", j1 <= j0 * 1.05, `raw=${j0.toFixed(3)} sm=${j1.toFixed(3)}`);
const causal = P.kalmanTrack(frames.slice(0, 20));
ck("卡尔曼：**因果**（前 20 帧的结果与全长跑出来的前 20 帧逐位一致）",
   causal.every((p, i) => p.x === sm[i].x && p.y === sm[i].y));

/* ---- 真值插值 + 误差 + CDF（回放页「定位误差 CDF」的算法） ---- */
const truth = [{ t: 0, x: 0, y: 0 }, { t: 10, x: 10, y: 0 }, { t: 20, x: 10, y: 20 }];
ck("truthAt：段内按时间线性插值", near(P.truthAt(truth, 5).x, 5, 1e-9));
ck("truthAt：第二段（拐角后）", near(P.truthAt(truth, 15).y, 10, 1e-9));
ck("truthAt：区间外夹到端点（不外推）",
   P.truthAt(truth, -5).x === 0 && P.truthAt(truth, 999).y === 20);
ck("truthAt：无真值 → null（不编 0）", P.truthAt([], 5) === null);
const errs = P.errorsOf([{ t: 0, x: 0, y: 0 }, { t: 10, x: 13, y: 4 }, { t: 20, x: 10, y: 20 }], truth);
ck("errorsOf：逐帧欧氏距离（0 / 5 / 0）", errs.length === 3 && near(errs[0], 0) && near(errs[1], 5),
   JSON.stringify(errs));
ck("errorsOf：无真值 → 空（调用方显示“不适用”）",
   P.errorsOf([{ t: 0, x: 1, y: 1 }], []).length === 0);
ck("分位数：中位数/极值/空集", near(P.quantileOf([1, 2, 3, 4], 0.5), 2.5)
   && near(P.quantileOf([1, 2, 3, 4], 1), 4) && P.quantileOf([], 0.9) === null);
const c = P.cdfOf([3, 1, 2, 10]);
ck("cdfOf：n/p50/p90/max/mean 正确",
   c.n === 4 && near(c.p50, 2.5) && near(c.p90, 7.9) && near(c.max, 10) && near(c.mean, 4),
   JSON.stringify(c));
ck("cdfOf：pts 升序且累积到 1（可直接画 CDF 曲线）",
   c.pts.length === 4 && near(c.pts[0][0], 1) && near(c.pts[3][1], 1));
ck("cdfOf：空输入 → 各项 null（不许给 0 假装量过）",
   P.cdfOf([]).n === 0 && P.cdfOf([]).p50 === null && P.cdfOf([NaN]).max === null);

/* ---- GDOP 单一实现 + 补站位建议（页面“几何不行”时给一句可执行的） ---- */
const xy = (arr) => arr.map(p => ({ s: p, dist: 1 }));
ck("gdopOf 与 qualityOf 同源（对同一组点算出同一个 GDOP）",
   near(P.gdopOf({ x: 5, y: 5 }, anchors), P.qualityOf(anchors.map(a => ({ s: a, dist: 1 })), { x: 5, y: 5 }).gdop, 1e-12));
ck("gdopOf：站位 < 2 / 与站位重合 → null（不可判定，不给大数）",
   P.gdopOf({ x: 0, y: 0 }, [{ x: 0, y: 0 }]) === null);

/* 三站完全共线 → 解不出来；建议点必须**离开那条直线**，且加入后条件数变有限 */
const col = [{ x: -20, y: 0 }, { x: 0, y: 0 }, { x: 20, y: 0 }];
const advCol = P.suggestStation(xy(col), null);
ck("补站位：三站共线（无估计点）→ 给出建议", advCol && advCol.need, JSON.stringify(advCol));
ck("补站位：共线场景判据用条件数（scoreBy=cond）、reason=collinear",
   advCol.scoreBy === "cond" && advCol.reason === "collinear");
ck("补站位：共线前条件数 ∞ → 建议点让条件数变有限",
   advCol.condBefore === Infinity && isFinite(advCol.condAfter), JSON.stringify(advCol));
ck("补站位：共线时建议点**离开直线**（|y| > 5 m）",
   Math.abs(advCol.y) > 5, `建议 y=${advCol.y}`);
ck("补站位：建议点不贴在已有站位上（minDist ≥ 跨度×0.25）",
   advCol.minDist >= 40 * 0.25 - 1e-9, `minDist=${advCol.minDist}`);

/* 有估计点时改用 GDOP 判据，并且必须真的降下来 */
const advG = P.suggestStation(xy(col), { x: 0, y: 12 });
ck("补站位：有估计点 → 判据换成 gdop（scoreBy=gdop）",
   advG && advG.scoreBy === "gdop", JSON.stringify(advG));
ck("补站位：建议点让该点 GDOP 下降",
   advG.gdopAfter < advG.gdopBefore, `${advG.gdopBefore} → ${advG.gdopAfter}`);

/* 几何已经够好 → 不给建议（而不是无话可说/乱指一处） */
const good = [{ x: 0, y: 0 }, { x: 30, y: 0 }, { x: 0, y: 26 }];
ck("补站位：几何够好（好三角 + 估计点在中心）→ null（无需补）",
   P.suggestStation(xy(good), { x: 10, y: 9 }) === null);

/* 两点：两台本来就只能定一条线（布局矩阵秩 1）→ 条件数 ∞ = 几何退化，
   建议必须离开两点连线（不能用“只有两台”当理由就去指一个在连线上的点） */
const two = [{ x: -20, y: 0 }, { x: 20, y: 0 }];
const adv2 = P.suggestStation(xy(two), null);
ck("补站位：只有两点 → reason=collinear（秩退化）且建议离开连线",
   adv2 && adv2.reason === "collinear" && Math.abs(adv2.y) > 5, JSON.stringify(adv2));
ck("补站位：只有两点时条件数 = ∞（不是 0、也不是“算不出来就跳过”）",
   adv2.condBefore === Infinity && isFinite(adv2.condAfter),
   `before=${adv2.condBefore} after=${adv2.condAfter}`);
ck("补站位：无估计点时 gdopBefore/After 如实给 null（**不是 0**——0 会被误读为“完美几何”）",
   adv2.gdopBefore === null && adv2.gdopAfter === null);

/* 一个站位：连方向都定不了 → 先说“凑够有观测的站位”，不编坐标 */
const adv1 = P.suggestStation(xy([{ x: 0, y: 0 }]), null);
ck("补站位：只有一个站位 → reason=too_few 且**不给坐标**（不编）",
   adv1 && adv1.need && adv1.reason === "too_few" && adv1.x === null && adv1.y === null);
ck("补站位：无站位 → null", P.suggestStation([], null) === null);

/* 无解但几何不弱（距离环不相交）= 测量不自洽，不是摆位问题 → 不给建议 */
const wide = [{ x: 0, y: 0 }, { x: 40, y: 0 }, { x: 20, y: 35 }];
ck("补站位：无解但条件数正常（距离环不相交）→ 不给摆位建议（别误导去搬站位）",
   P.suggestStation(xy(wide), null) === null);

/* 确定性：纯几何搜索，同样输入必须给出同样结果（页面每 2s 重算，不能抖） */
const r1 = P.suggestStation(xy(col), { x: 0, y: 12 });
const r2 = P.suggestStation(xy(col), { x: 0, y: 12 });
ck("补站位：确定性（两次调用逐位相同）", JSON.stringify(r1) === JSON.stringify(r2));
ck("补站位：坐标取到 0.1 m（现场可照读）",
   Math.abs(r1.x * 10 - Math.round(r1.x * 10)) < 1e-9);

/* ---- 样本源判据 samplesFor（空表 ≠ 没这个数据源） ---- */
const s1 = [{ t: 3000, rssi: -70 }, { t: 2000, rssi: -72 }, { t: 1000, rssi: -75 }];
const devSamples = [{ t: 3000, rssi: -60 }, { t: 2000, rssi: -61 }];
ck("samplesFor：该站位有序列 → 用它",
   P.samplesFor({ S1: s1, S2: [] }, "S1", devSamples) === s1);
ck("samplesFor：★该站位**空表** → 返回空（**不能**回落到设备流：那是别的测量）",
   P.samplesFor({ S1: s1, S2: [], S3: [] }, "S2", devSamples).length === 0);
ck("samplesFor：序列里没这个 sid → 也是空（不是设备流）",
   P.samplesFor({ S1: s1 }, "S9", devSamples).length === 0);
ck("samplesFor：整个 per-station 序列都缺席（老数据源）→ 回落到设备流",
   P.samplesFor({}, "S1", devSamples) === devSamples
   && P.samplesFor(undefined, "S1", devSamples) === devSamples);
ck("samplesFor：没有设备流也不炸（给空表）", P.samplesFor({}, "S1", undefined).length === 0);

/* 端到端：空表站位不能凭空多出一个观测（修复前实测：3 个观测、S2/S3 拿设备流那条） */
const st3 = [{ sid: "S1", x: 30, y: 0 }, { sid: "S2", x: -15, y: 26 }, { sid: "S3", x: -15, y: -26 }];
const sOf = (obsMap) => (sid) => P.samplesFor(obsMap, sid, devSamples);
const withEmpty = P.obsAt(st3, sOf({ S1: s1, S2: [], S3: [] }), 3000, -40, 2.5);
ck("obsAt：只有 S1 有序列 → 只出 1 个观测（不再拿设备流充数）",
   withEmpty.length === 1 && withEmpty[0].s.sid === "S1", JSON.stringify(withEmpty.map(o => o.s.sid)));
const legacy2 = P.obsAt(st3, sOf({}), 3000, -40, 2.5);
ck("obsAt：老数据源（per-station 全缺席）→ 3 个观测都来自设备流（悬停窗口语义保留）",
   legacy2.length === 3 && legacy2.every(o => o.src === "router"));

/* ---- 顺序无关（后端保证升序，但 latestAt 自己也不能靠它） ---- */
const asc = [{ t: 1000, rssi: -75 }, { t: 2000, rssi: -72 }, { t: 3000, rssi: -70 }];
const desc = asc.slice().reverse();
const mixed = [asc[1], asc[2], asc[0]];
ck("latestAt：升序/降序/乱序给出同一条（取 t 之前最近一条，不依赖输入顺序）",
   P.latestAt(asc, 2500).rssi === P.latestAt(desc, 2500).rssi
   && P.latestAt(asc, 2500).rssi === P.latestAt(mixed, 2500).rssi);
ck("latestAt：时效（>30s 不算；时效常量是 **ms**）与“不看未来”仍在",
   P.latestAt(asc, 3000 + 40000) === null && P.latestAt(asc, 1500).rssi === -75);

/* ---- 近共线（cond 有限但极大）也要给建议，且分类如实 ---- */
const nearCol = [{ x: -20, y: 0 }, { x: 0, y: 0.05 }, { x: 20, y: 0 }];
const advNear = P.suggestStation(xy(nearCol), null);
ck("补站位：近共线（cond 有限但巨大）→ 仍给建议、仍要求离开直线",
   advNear && advNear.reason === "layout" && isFinite(advNear.condBefore)
   && advNear.condBefore > 500 && Math.abs(advNear.y) > 5,
   JSON.stringify(advNear));

/* ---- 报文流分析：缺口 / 重复 / 回退 / 帧间隔（回放页「报文流时间轴」靠它）----
   两条真陷阱必须在位：序号回退（设备重启）**不是**丢包（不能算出负数缺口）；没有 seq 就不判。 */
const pts = (arr) => arr.map(([t, seq, rssi, rid]) =>
  ({ t, seq, rssi, router_id: rid === undefined ? "" : rid }));
let fg = P.frameGaps(pts([[1000, 1], [2000, 2], [3000, 4], [4000, 5]]));
ck("报文流：连续帧不报缺口，跳号那条报缺 1",
   fg.n === 4 && fg.gaps === 1 && fg.gap_total === 1
   && fg.rows[1].gap === 0 && fg.rows[2].gap === 1 && fg.rows[3].gap === 0);
ck("报文流：缺口累计（跳 5 条）",
   P.frameGaps(pts([[1, 1], [2, 7]])).gap_total === 5);
fg = P.frameGaps(pts([[1000, 5], [2000, 5], [3000, 4]]));
ck("报文流：seq 相同 = 重复（不算缺口）",
   fg.dups === 1 && fg.gaps === 0 && fg.rows[1].dup && !fg.rows[1].gap);
ck("报文流：seq 变小 = 回退（设备重启/乱序，不算缺口、不出现负数）",
   fg.resets === 1 && fg.gaps === 0 && fg.gap_total === 0 && fg.rows[2].reset);
fg = P.frameGaps(pts([[1000, 1], [2000, null], [3000, 9], [4000, undefined]]));
ck("报文流：seq 缺失 → 该帧不判（不猜），也不影响后一条的判定",
   fg.rows[1].gap === 0 && !fg.rows[1].dup && !fg.rows[1].reset
   && fg.rows[2].gap === 0 && fg.rows[3].gap === 0 && fg.gaps === 0);
fg = P.frameGaps(pts([[1000, 1, -55], [3000, 2, -60], [4000, 3, -61]]));
ck("报文流：帧间隔逐条给（首帧 null）",
   fg.rows[0].dt === null && fg.rows[1].dt === 2000 && fg.rows[2].dt === 1000);
ck("报文流：间隔统计（中位/P95/最大，秒级用得到）",
   fg.dt.n === 2 && fg.dt.med === 1000 && fg.dt.p95 === 1000 && fg.dt.max === 2000);
ck("报文流：router_id 空串原样保留（真机阶段才非空，页面如实显示“（空）”）",
   P.frameGaps(pts([[1, 1]])).rows[0].router_id === ""
   && P.frameGaps(pts([[1, 1, -50, "R1"]])).rows[0].router_id === "R1");
ck("报文流：空表/undefined 不炸（n=0，统计给 null 不给 0）",
   P.frameGaps([]).n === 0 && P.frameGaps(undefined).n === 0
   && P.frameGaps([]).dt.med === null && P.frameGaps([]).gap_total === 0);
ck("报文流：单帧不给 delta（不能凭空造一条间隔）",
   P.frameGaps(pts([[1, 1]])).dt.n === 0);

/* ---- 多观测一致性判定 consensus（「孤证不立」：位置可信度不依赖单台路由器）----
   场景：站位 S1..S4，目标静止在 (12,9) 或沿 +x 以 1.2 m/s 行走；观测由真值距离经
   「距离→RSSI→距离」往返生成（与页面同一条链路，不手工塞 dist）。
   偏差 = 某台把测得的距离按倍数改大/改小（现实里成因不止一种：遮挡/多径、天线、元件、标定，
   也不排除被改装/冒充 —— 本用例只用它做**“某台与其余台对不上”**的可复现输入，不预设原因）。 */
const A0 = -40, N0 = 2.5, TR = { x: 12, y: 9 };
const ST = [{ sid: "S1", x: 0, y: 0 }, { sid: "S2", x: 30, y: 0 },
            { sid: "S3", x: 0, y: 26 }, { sid: "S4", x: 30, y: 26 }];
function mk(at, ids) {
  return ids.map(sid => {
    const s = ST.find(z => z.sid === sid);
    const rssi = P.rssiFromDist(Math.hypot(at.x - s.x, at.y - s.y), A0, N0);
    return { s, rssi, dist: P.distFromRssi(rssi, A0, N0) };
  });
}
const spoil = (arr, sid, f) => arr.map(o => o.s.sid === sid ? { ...o, dist: o.dist * f } : o);
const ids3 = ["S1", "S2", "S3"], ids4 = ["S1", "S2", "S3", "S4"];

const c3 = P.consensus(mk(TR, ids3), {});
ck("consensus：3 台一致 → verified（交叉校验通过），全留、零剔除",
   c3.trust === "verified" && c3.kept.length === 3 && c3.dropped.length === 0
   && c3.reason === "consistent", JSON.stringify([c3.trust, c3.kept, c3.dropped]));
ck("consensus：verified 时估计回真值 (12,9)",
   near(c3.est.x, TR.x, 0.05) && near(c3.est.y, TR.y, 0.05), JSON.stringify(c3.est));
ck("consensus：无速度/无时刻 → moved=false（如实标“未做运动补偿”）", c3.moved === false);

const c3l = P.consensus(spoil(mk(TR, ids3), "S2", 0.25), {});
ck("consensus：★3 台里 1 台偏大 → 测得出“不一致”，但**定不了是哪台**（排除一台只剩 2 台无冗余）→ conflict",
   c3l.trust === "conflict" && c3l.reason === "conflict_unresolved",
   JSON.stringify([c3l.trust, c3l.reason, c3l.dropped]));

const c4l = P.consensus(spoil(mk(TR, ids4), "S3", 4.0), {});
ck("consensus：4 台里 1 台偏大（×4，测得“更远”）→ 排除它后其余 3 台自洽 → verified + dropped=[S3]",
   c4l.trust === "verified" && c4l.dropped.join() === "S3" && c4l.kept.length === 3
   && c4l.reason === "outlier_dropped", JSON.stringify([c4l.trust, c4l.dropped, c4l.kept]));
ck("consensus：剔除后估计回到真值附近（没被那台偏大的带跑）",
   Math.hypot(c4l.est.x - TR.x, c4l.est.y - TR.y) < 1, JSON.stringify(c4l.est));
ck("consensus：小偏差（与测距噪声同量级，0.9 倍）→ **不报**（原理上不可分辨，如实漏检）",
   P.consensus(spoil(mk(TR, ids4), "S3", 0.9), {}).trust === "verified"
   && P.consensus(spoil(mk(TR, ids4), "S3", 0.9), {}).dropped.length === 0);

/* ★回归锁（2026-09-13 实测踩过）：**“偏得更远”（×4）必须也抓得住**。
   把 σ 锚在**它自报的**距离上时，偏远那一侧会把自己的 σ 一起放大（z = 4|f−1|/f 封顶 4.0）
   → 整条路线 0/120 全没发现，还照样报“交叉校验通过”。σ 锚在**推出的**距离上才两边对称。 */
const farDev = P.consensus(spoil(mk(TR, ids4), "S1", 4.0), {});
ck("consensus：★“偏得更远”（×4）也抓得住（σ 必须锚在推出距离上）",
   farDev.trust === "verified" && farDev.dropped.join() === "S1",
   JSON.stringify([farDev.trust, farDev.dropped, (farDev.zs || []).map(z => z && z.toFixed(1))]));
ck("consensus：★“偏得更近”（×0.25）**数学上不可分辨** → 如实不报（与噪声同一结论，不调阈值凑检出）",
   P.consensus(spoil(mk(TR, ids4), "S4", 0.25), {}).dropped.length === 0);
ck("consensus：小偏差（×0.5 → z=2）**不报** —— 与 25% 测距噪声原理上不可分（如实漏检）",
   P.consensus(spoil(mk(TR, ids4), "S1", 0.5), {}).trust === "verified"
   && P.consensus(spoil(mk(TR, ids4), "S1", 0.5), {}).dropped.length === 0);

/* ★「零误报」回归锁（阀值是按**实测零假设分布**定的：诚实场景 max-z 上界 ≈ 3.8，与噪声幅度无关，
   阀值取 5 —— 原先拍脑袋的 3 会误报 3%）。这里批量跑诚实样本，一旦阈值/σ 改松就红。 */
let falseAcc = 0, honest = 0, looseHonest = 0;
for (let gx = -25; gx <= 30; gx += 5) {
  for (let gy = -25; gy <= 30; gy += 5) {
    for (let seed = 0; seed < 6; seed++) {
      const at = { x: gx, y: gy };
      const obs = ids4.map((sid, i) => {
        const s = ST.find(z => z.sid === sid);
        const d = Math.hypot(at.x - s.x, at.y - s.y);
        const r = (Math.sin(seed * 12.9898 + i * 78.233) * 43758.5453) % 1;
        const rssi = P.rssiFromDist(d, A0, N0) + r * 2.0;
        return { s, rssi, dist: P.distFromRssi(rssi, A0, N0) };
      });
      honest++;
      const ch = P.consensus(obs, {});
      if (ch.dropped.length) falseAcc++;
      if (ch.loose) looseHonest++;          // 「残差偏大」也不能误报（实测诚实 σ0 ≤ 0.84 @±2dBm）
    }
  }
}
ck("consensus：★诚实的 " + honest + " 个样本里**一台也不许被误剔除**（零误报）",
   falseAcc === 0, "误剔除 " + falseAcc + " 例");
ck("consensus：★诚实样本里**也不许误报“残差偏大”**（loose 阀 1.8 是按实测诚实上界定的）",
   looseHonest === 0, "误报 " + looseHonest + " 例");

/* ★诚实的**能力边界**（不是“抓得住”）：4 台里 2 台同时偏大 → n−k=2<3，信息论上定不了是哪台
   （SPEC §8 P-1）；但拟合残差 σ0 会明显变大（实测跨噪声中位 ≈2.1，诚实 ≤0.84）→
   页面只能如实说“残差偏大、定不了是哪台”，不能指到具体一台。 */
const twoDev = ids4.map((sid, i) => {
  const o = mk(TR, ids4)[i];
  return (i === 1 || i === 3) ? { ...o, dist: o.dist * 4 } : o;
});
const c2l = P.consensus(twoDev, {});
ck("consensus：★2 台同时偏大 → **定不了是哪台**（只能报残差偏大）—— n−k≥3 才够",
   c2l.dropped.length === 0 && c2l.loose === true,
   JSON.stringify([c2l.trust, c2l.dropped, c2l.sigma0]));

const c2 = P.consensus(mk(TR, ["S1", "S2"]), {});
ck("consensus：2 台 → single（无冗余：任一台偏了都看不出来）",
   c2.trust === "single" && c2.reason === "no_redundancy" && c2.dropped.length === 0
   && isFinite(c2.est.x), JSON.stringify([c2.trust, c2.reason]));
ck("consensus：1 台 → none(single_obs)；0 台 → none(no_obs)",
   P.consensus(mk(TR, ["S1"]), {}).trust === "none"
   && P.consensus(mk(TR, ["S1"]), {}).reason === "single_obs"
   && P.consensus([], {}).trust === "none" && P.consensus([], {}).reason === "no_obs");
ck("consensus：空/undefined 不炸", P.consensus(undefined, undefined).trust === "none");

const cBad = P.consensus([{ s: ST[0], dist: 5 }, { s: ST[1], dist: 5 }, { s: ST[2], dist: 5 }], {});
ck("consensus：三台互相矛盾（距离环不相交）→ conflict，不硬给位置",
   cBad.trust === "conflict" && cBad.reason === "conflict_unresolved",
   JSON.stringify([cBad.trust, cBad.reason]));

/* 移动目标：t=0/30/60 s，目标 1.2 m/s 沿 +x（12 → 48 → 84），参考时刻 = 最后一个观测 */
const MOV = [{ t: 0, at: { x: 12, y: 9 } }, { t: 30000, at: { x: 48, y: 9 } },
             { t: 60000, at: { x: 84, y: 9 } }];
const mObs = [].concat(...MOV.map(m => mk(m.at, ids3).map(o => ({ ...o, ts: m.t }))));
const cNo = P.consensus(mObs, {});
ck("consensus：运动目标**不做**补偿 → 估计明显偏（>50 m）—— 所以必须补偿；不报假冲突",
   cNo.moved === false && Math.hypot(cNo.est.x - 84, cNo.est.y - 9) > 50,
   JSON.stringify([cNo.trust, Math.round(cNo.est.x), Math.round(cNo.est.y)]));
const cYes = P.consensus(mObs, { vel: { vx: 1.2, vy: 0 }, tref: 60000 });
ck("consensus：给了速度 → 补偿后 verified、零剔除，且估计回参考时刻真值 (84,9)",
   cYes.moved === true && cYes.trust === "verified" && cYes.dropped.length === 0
   && near(cYes.est.x, 84, 0.05) && near(cYes.est.y, 9, 0.05), JSON.stringify(cYes.est));
ck("consensus：纯函数（同一输入两次调用逐位相同）",
   JSON.stringify(P.consensus(mObs, { vel: { vx: 1.2, vy: 0 }, tref: 60000 }))
   === JSON.stringify(cYes));

console.log("");
if (fails.length) {
  console.log("定位内核：失败 " + fails.length + " 项：" + fails.join("；"));
  process.exit(1);
}
console.log("定位内核（pos.js）全部通过");
"""


def page_guard():
    """两页必须调用 `samplesFor`（**单一判据**），不许再各写一份 `(own && own.length) ? …` 三元。

    为什么要在 Python 侧查：判据在 pos.js，但**调用点在两个 HTML 页面**里；
    以前两页各写一份同样的三元表达式，其中「空表 ≠ 没有该数据源」的语义一旦漂移，
    两页会对同一时刻给出不同位置（本仓最忌讳的那种不一致）。
    """
    bad = []
    for page, needle in (("track.html", "samplesFor(realObs"),
                         ("replay.html", "samplesFor(S.obs")):
        p = os.path.join(HERE, "ui", "static", page)
        with open(p, encoding="utf-8") as f:
            src = f.read()
        if needle not in src:
            bad.append(page)
    if bad:
        print(f"  FAIL 这两页没有走 samplesFor（单一判据）：{', '.join(bad)}")
        return 1
    print("  OK   两页都走 samplesFor（样本源判据单一源）")
    # 报文流：判定只能走 pos.js 的 frameGaps（页面里再写一份“算缺口”的循环迟早漂移，
    # 而且 seq 回退（设备重启）那条陷阱很容易被写成负数缺口）
    with open(os.path.join(HERE, "ui", "static", "replay.html"), encoding="utf-8") as f:
        rp = f.read()
    if "frameGaps(S.points)" not in rp:
        print("  FAIL replay.html 没有调用 frameGaps（报文流判定必须走单一实现）")
        return 1
    if "seq - " in rp or "seq-" in rp:
        print("  FAIL replay.html 里在页面侧自算 seq 差值（应改用 pos.js 的 frameGaps）")
        return 1
    print("  OK   replay.html 走 frameGaps（报文流判定单一源）")
    # 三幕演示（量化）：判定必须**复用** pos.js 的 consensus()，不许在页面里另传阈值/门限
    # （演示里一旦自己写一套 z 阈值，就会出现"演示的"与"测的"两套口径 —— 本仓最忌讳的那种漂移）
    with open(os.path.join(HERE, "ui", "static", "track.html"), encoding="utf-8") as f:
        tk = f.read()
    for needle, why in (("function runActs", "缺三幕演示（runActs）"),
                        ("function actRun", "缺 actRun（逐幕统计）"),
                        ("consensus(obsSim, {})", "判定没走 consensus(obsSim, {}) —— 不许另传阈值")):
        if needle not in tk:
            print(f"  FAIL track.html：{why}")
            return 1
    print("  OK   track.html 三幕演示走 consensus()（判定单一实现，不另传阈值）")
    return 0


def main():
    node = shutil.which("node")
    if not node:
        print("  FAIL 找不到 node（本套件直接跑 pos.js 原文，需要 node 在 PATH 上）")
        print("定位内核：1 项失败：node 缺失")
        return 1
    fd, path = tempfile.mkstemp(suffix=".js", prefix="posjstest_")
    os.close(fd)
    fail = 0
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(HARNESS)
        r = subprocess.run([node, path, POS_JS], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
        print(out.rstrip())
        n_ok = out.count("  OK ")
        n_bad = out.count("  FAIL ")
        print(f"  （pos.js 检查 {n_ok} 条通过 / {n_bad} 条失败）")
        if r.returncode != 0 or "FAIL" in out:
            fail = 1
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    fail += page_guard()
    if fail:
        print("定位内核：有问题（见上）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
