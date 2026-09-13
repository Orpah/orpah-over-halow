/* pos.js — ORPAH 定位内核（单一实现，供 track.html 与 replay.html 共用）

为什么单独一个文件：页面里各留一份定位算法必然会漂移（本仓库已有"算法单一源"的约定，
见 damm32.py/luhn32.py/mod97.py 与 tools/ui 的 AT 命令库）。这里只放**纯函数**：
不碰 DOM、不读输入框、不依赖页面状态（三边定位的二义选择改为显式传入 "上一个估计点"）。

包含：
- RSSI ↔ 距离换算（对数距离路径损耗模型）
- 三边定位（≥3 点为线性最小二乘；2 点取两圆交点中离上次估计更近的那个）
- 加权最小二乘（WLS，高斯-牛顿 + 步长折半线搜索）
- 95% 置信椭圆 / 搜索半径（由协方差特征值给）
- 定位质量：残差 RMS、GDOP、站位布局条件数、分级、**补站位建议（几何不行时“往哪儿再放一台”）**
- 观测归集：在某一时刻 t 各站位能看到什么（时间窗 + 不看未来）
- 报文流分析：帧序号缺口 / 回退 / 帧间隔（回放页的「报文流时间轴」用；**页面不许自己算**）

全局函数（classic script，页面直接按名字调用，与重构前一致）。
*/
"use strict";

/* ---------------- RSSI ↔ 距离（对数距离模型） ---------------- */
function rssiFromDist(d, A, n) { return A - 10 * n * Math.log10(Math.max(d, 0.01)); }
function distFromRssi(rssi, A, n) { return Math.pow(10, (A - rssi) / (10 * n)); }

/* ---------------- 三边定位（线性最小二乘） ----------------
   2 个站位时两圆有两个交点（二义）：传 last（上一次估计/真值）取更近的那个，
   不传则取第一个交点（等价于以原点为参考）。 */
function trilaterate(anchors, dists, last) {
  const n = anchors.length;
  if (n === 1) return null;
  if (n === 2) {
    const [a, b] = anchors;
    const d = Math.hypot(b.x - a.x, b.y - a.y);
    if (d > dists[0] + dists[1] || d < Math.abs(dists[0] - dists[1]) || d < 1e-9) return null;
    const aa = (dists[0] * dists[0] - dists[1] * dists[1] + d * d) / (2 * d);
    const h2 = dists[0] * dists[0] - aa * aa;
    if (h2 < 0) return null;
    const h = Math.sqrt(h2);
    const x2 = a.x + aa * (b.x - a.x) / d, y2 = a.y + aa * (b.y - a.y) / d;
    // 二义：取靠近当前估计的那个交点
    const p1 = { x: x2 + h * (b.y - a.y) / d, y: y2 - h * (b.x - a.x) / d };
    const p2 = { x: x2 - h * (b.y - a.y) / d, y: y2 + h * (b.x - a.x) / d };
    const prev = last || { x: 0, y: 0 };
    return (Math.hypot(p1.x - prev.x, p1.y - prev.y)
            <= Math.hypot(p2.x - prev.x, p2.y - prev.y)) ? p1 : p2;
  }
  const [x0, y0] = [anchors[0].x, anchors[0].y];
  const d0 = dists[0];
  let a00 = 0, a01 = 0, a11 = 0, b0 = 0, b1 = 0;
  for (let j = 1; j < n; j++) {
    const [xj, yj] = [anchors[j].x, anchors[j].y];
    const dj = dists[j];
    const ax = 2 * (xj - x0), ay = 2 * (yj - y0);
    const bb = d0 * d0 - dj * dj - (x0 * x0 + y0 * y0) + (xj * xj + yj * yj);
    a00 += ax * ax; a01 += ax * ay; a11 += ay * ay;
    b0 += ax * bb; b1 += ay * bb;
  }
  const det = a00 * a11 - a01 * a01;
  if (Math.abs(det) < 1e-12) return null;
  return { x: (a11 * b0 - a01 * b1) / det, y: (a00 * b1 - a01 * b0) / det };
}

/* ---------------- 加权最小二乘（WLS）+ 误差椭圆 ----------------
   为什么必须加权：RSSI 测距误差随距离增长（路径损耗模型下 σ_d ≈ d·ln10·σ_rssi/(10n)），
   远站位的距离本就不可信；等权最小二乘会被远端噪声拽偏。
   权重 w_i = 1/σ_i²，σ_i = max(SIG_FLOOR, SIG_REL·d_i)（相对误差模型，简单且可解释）。
   协方差 C = (JᵀWJ)⁻¹ · max(1, σ̂0²)，J 行 = 估计点到站位的单位向量（与 GDOP 同一个 J），
   σ̂0² = Σw·r²/(n−2)（后验单位权方差）→ 由 C 的特征值给 95% 置信椭圆（χ²(2,0.95)=5.991）。 */
const SIG_REL = 0.25;                 // 测距相对误差（RSSI 典型 20~30%）
const SIG_FLOOR = 1.0;                // 距离下限（米）：防 d→0 时权重爆炸
const ELLIPSE_K = Math.sqrt(5.991);   // χ²(2, 0.95) → 2.4477

/* 观测的距离噪声 σ（与 wlsLocate 同一模型） */
function sigOf(o) {
  return Math.max(SIG_FLOOR, SIG_REL * Math.max(o.dist || 0, 0));
}

function wlsLocate(obs, init) {
  /* `obs` = **观测对象数组**，形状统一为 `{s:{x,y,sid,…}, dist, …}`（`obsAt()` 返回的就是它，
     三页共用）：站位坐标在 `o.s.x/o.s.y`，测距在 `o.dist`。
     （**不是** `{x,y,dist}` 平铺对象 —— 审查提过一次这个误解，写在这里防复发。） */
  const n = obs.length;
  if (n < 2) return null;
  /* init 请务必传**线性解**（页面即这么用）。只有 2 个站位时若 init 给质心，
     质心正好落在两站连线上 → 两个单位向量共线 → JᵀWJ 奇异 → 直接返回 singular。
     线性解在两圆交点处（离连线有距离），故无此问题。 */
  const sig = obs.map(o => Math.max(SIG_FLOOR, SIG_REL * Math.max(o.dist, 0)));
  const w = sig.map(s => 1 / (s * s));
  const cost = (q) => {
    let s = 0;
    for (let i = 0; i < n; i++) {
      const o = obs[i];
      const r = Math.hypot(q.x - o.s.x, q.y - o.s.y) - o.dist;
      s += w[i] * r * r;
    }
    return s;
  };
  let p = init ? { x: init.x, y: init.y } : {
    x: obs.reduce((s, o) => s + o.s.x, 0) / n,
    y: obs.reduce((s, o) => s + o.s.y, 0) / n,
  };
  let c0 = cost(p);
  let iters = 0, converged = false;
  /* 高斯-牛顿 + **步长折半线搜索**：测量不自洽（距离环不相交）时裸 GN 会来回振荡、
     打满迭代还没有意义地"给出"一个点。线搜索保证代价单调下降，到不动即停。 */
  for (let it = 0; it < 40; it++) {
    let a00 = 0, a01 = 0, a11 = 0, g0 = 0, g1 = 0;
    for (let i = 0; i < n; i++) {
      const o = obs[i];
      const dx = p.x - o.s.x, dy = p.y - o.s.y;
      const r = Math.hypot(dx, dy) || 1e-6;
      const ux = dx / r, uy = dy / r;        // ∂残差/∂p（单位向量）
      const res = r - o.dist;                // 预测 − 测量
      const wi = w[i];
      a00 += wi * ux * ux; a01 += wi * ux * uy; a11 += wi * uy * uy;
      g0 += wi * ux * res; g1 += wi * uy * res;
    }
    const detN = a00 * a11 - a01 * a01;
    if (Math.abs(detN) < 1e-12) {
      return { ok: false, reason: "singular", iters, x: p.x, y: p.y,
               obsCount: n, rms: Math.sqrt(c0 / n), sig };
    }
    const d0 = -(a11 * g0 - a01 * g1) / detN;    // 解 (JᵀWJ)δ = −JᵀWr
    const d1 = -(-a01 * g0 + a00 * g1) / detN;
    let step = 1, moved = false;
    for (let k = 0; k < 8; k++) {                // 折半直到代价下降
      const q = { x: p.x + d0 * step, y: p.y + d1 * step };
      const c1 = cost(q);
      if (c1 < c0) { p = q; c0 = c1; moved = true; break; }
      step /= 2;
    }
    iters = it + 1;
    if (!moved) { converged = true; break; }     // 任何步长都不再下降 → 已到最小点
    if (Math.hypot(d0 * step, d1 * step) < 1e-3) { converged = true; break; }
  }
  /* 在最终解处再算一次：残差 / 权重 / JᵀWJ（协方差要用最终的 J） */
  let a00 = 0, a01 = 0, a11 = 0, sum2 = 0, sum2w = 0;
  const per = {};
  for (let i = 0; i < n; i++) {
    const o = obs[i];
    const dx = p.x - o.s.x, dy = p.y - o.s.y;
    const r = Math.hypot(dx, dy) || 1e-6;
    const ux = dx / r, uy = dy / r;
    const res = r - o.dist;
    per[o.s.sid] = res;
    sum2 += res * res; sum2w += w[i] * res * res;
    a00 += w[i] * ux * ux; a01 += w[i] * ux * uy; a11 += w[i] * uy * uy;
  }
  const det = a00 * a11 - a01 * a01;
  if (Math.abs(det) < 1e-12) {
    return { ok: false, reason: "singular", iters, x: p.x, y: p.y,
             obsCount: n, rms: Math.sqrt(sum2 / n), sig, per };
  }
  const dof = Math.max(1, n - 2);
  const s0 = Math.sqrt(sum2w / dof);              // 后验单位权标准差（≈1 = 残差与先验噪声相符）
  const c00 = a11 / det, c01 = -a01 / det, c11 = a00 / det;
  /* 协方差用**先验**噪声模型 (JᵀWJ)⁻¹（已在 w 里含 σ_i），只有后验明显更差时才放大：
     为什么不能只用 σ0²(JᵀWJ)⁻¹ —— 恒定 RSSI 这类「自洽但错误」的输入残差≈0，
     σ0→0 会给出「零不确定度」的假自信（正是本页一直要防的那类坑）。
     s0 > 1 说明残差比假定的测距噪声还大（有未建模误差）→ 放大椭圆。*/
  const inflate = Math.max(1, s0 * s0);
  const cov = { a: inflate * c00, b: inflate * c01, c: inflate * c11 };
  return { ok: true, x: p.x, y: p.y, iters, converged, obsCount: n, sig, w,
           per, rms: Math.sqrt(sum2 / n), sigma0: s0, inflate, cov,
           ellipse: ellipseOf(cov) };
}

/* 由协方差给 95% 置信椭圆：特征值 → 半轴长，特征向量方位 → 倾角。
   degenerate = 半短轴 ≈0（几何退化，如站位近共线）→ 椭圆退化成一条线段，
   此时「半径」仍可报（沿最长轴），但面积/形状不可信。 */
function ellipseOf(cov) {
  if (!cov) return null;
  const tr = cov.a + cov.c, det = cov.a * cov.c - cov.b * cov.b;
  const disc = Math.max(tr * tr / 4 - det, 0);
  const l1 = tr / 2 + Math.sqrt(disc);
  const l2 = tr / 2 - Math.sqrt(disc);
  if (!(l1 > 0) || !isFinite(l1)) return null;
  const theta = 0.5 * Math.atan2(2 * cov.b, cov.a - cov.c);   // 数学坐标系（y 向上）
  const semiMajor = ELLIPSE_K * Math.sqrt(l1);
  const semiMinor = l2 > 0 ? ELLIPSE_K * Math.sqrt(l2) : 0;
  const deg = d => (d * 180 / Math.PI + 360) % 360;
  return {
    l1, l2, theta, thetaDeg: deg(theta),
    semiMajor, semiMinor,
    radius: semiMajor,                       // 保守覆盖半径（沿最长轴，95%）
    area: Math.PI * semiMajor * semiMinor,
    degenerate: semiMinor < 0.05 * semiMajor, // 半短轴相对半长轴太小
  };
}

/* 椭圆 → 局部坐标点列（画布/地图都能用；n 段足够平滑） */
function ellipsePoints(ell, cx0, cy0, n = 48) {
  if (!ell) return [];
  const ct = Math.cos(ell.theta), st = Math.sin(ell.theta);
  const out = [];
  for (let i = 0; i < n; i++) {
    const t = 2 * Math.PI * i / n;
    const u = ell.semiMajor * Math.cos(t), v = ell.semiMinor * Math.sin(t);
    out.push({ x: cx0 + u * ct - v * st, y: cy0 + u * st + v * ct });
  }
  return out;
}

/* ---------------- 定位质量 ---------------- */
function median(vals) {
  const v = vals.slice().sort((x, y) => x - y);
  if (!v.length) return 0;
  const m = v.length >> 1;
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

/* ---------------- 真值对照：逐帧误差 + CDF（回放页「定位误差 CDF」用） ----------------
   **口径（重要）**：地面真值**只有演示环境有**（`motion.Walk` 的行走模型，经 `/api/truth` 给出）。
   真机部署没有真值 → 真机的定位质量只能看**不需要真值**的那套：RMS 残差 / 95% 椭圆 / 搜索半径
   （`qualityOf` / `ellipseOf`）。所以这组函数只在有 truth 数据时才有意义，页面必须如实标注。
   误差 = 估计点与真值点的欧氏距离（米）；**没有真值就跳过，不编 0**。 */

/* 真值轨迹（[{t,x,y}] 升序）→ t 时刻真值位置（两点间按时间线性插值）。
   真值是**匀速折线**、采样点本身落在折线上（`/api/truth` 自适应步长 ≤0.45s），
   插值误差 ≲ (v·step)²/(8R) < 1cm 量级 —— 与定位误差（米级）不在一个量级。 */
function truthAt(points, t) {
  if (!points || !points.length) return null;
  if (t <= points[0].t) return { x: points[0].x, y: points[0].y };
  const last = points[points.length - 1];
  if (t >= last.t) return { x: last.x, y: last.y };
  let lo = 0, hi = points.length - 1;
  while (hi - lo > 1) {
    const m = (lo + hi) >> 1;
    if (points[m].t <= t) lo = m; else hi = m;
  }
  const a = points[lo], b = points[hi], k = (t - a.t) / ((b.t - a.t) || 1);
  return { x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k };
}

/* 逐帧误差（米）：frames 与真值都有才计一条。 */
function errorsOf(frames, truth) {
  const out = [];
  for (const f of (frames || [])) {
    if (!f || !Number.isFinite(f.x) || !Number.isFinite(f.y)) continue;
    const g = truthAt(truth, f.t);
    if (!g) continue;
    out.push(Math.hypot(f.x - g.x, f.y - g.y));
  }
  return out;
}

/* 分位数（升序数组；p∈[0,1]，线性插值）。空 → null（不给 0 假装量过）。 */
function quantileOf(sorted, p) {
  const v = sorted || [];
  if (!v.length) return null;
  const idx = (v.length - 1) * Math.min(Math.max(p, 0), 1);
  const lo = Math.floor(idx), hi = Math.ceil(idx);
  return v[lo] + (v[hi] - v[lo]) * (idx - lo);
}

/* 误差数组 → {n, pts:[[err,p],…], p50,p90,p95,max,mean}（pts 直接喂 CDF 曲线）。 */
function cdfOf(errs) {
  const v = (errs || []).filter(x => Number.isFinite(x)).sort((a, b) => a - b);
  if (!v.length) {
    return { n: 0, pts: [], p50: null, p90: null, p95: null, max: null, mean: null };
  }
  return {
    n: v.length,
    pts: v.map((x, i) => [x, (i + 1) / v.length]),
    p50: quantileOf(v, 0.5), p90: quantileOf(v, 0.9), p95: quantileOf(v, 0.95),
    max: v[v.length - 1], mean: v.reduce((s, x) => s + x, 0) / v.length,
  };
}

/* GDOP：H 的行 = 估计点到各站位的单位向量，GDOP = sqrt(trace((HᵀH)⁻¹))。
   站位数不足 / 近共线 / 都在同一侧 → HᵀH 降秩 → null（**不可判定**，不给大数糊过去）。
   与某站位完全重合时 `L=0`，该站位的方向无定义 → 按 0 贡献（老行为，不要改）。
   **单一实现**：`qualityOf` 与 `suggestStation` 都走这里，防两处 GDOP 漂移。 */
function gdopOf(est, pts) {
  if (!est || !pts || pts.length < 2) return null;
  let a = 0, b = 0, c = 0;
  for (const p of pts) {
    const dx = p.x - est.x, dy = p.y - est.y;
    const L = Math.hypot(dx, dy) || 1e-6;
    const ux = dx / L, uy = dy / L;
    a += ux * ux; b += ux * uy; c += uy * uy;
  }
  const det = a * c - b * b;
  return Math.abs(det) > 1e-9 ? Math.sqrt((a + c) / det) : null;
}

/* 残差 + 几何强度
   残差 = 测量距离 − 估计点到该站位的距离；RMS 越小越可信。
   GDOP 见 `gdopOf`：站位数不足、近共线、或都在同一侧时 HᵀH 降秩 → GDOP 极大/无解（估计点不可信）。
   没有残差/几何指标时，恒定 RSSI 这类退化输入会给出一个看着很正常、实际无意义的坐标。 */
function qualityOf(obs, est) {
  if (!est || obs.length < 2) return null;
  let sum2 = 0;
  const per = {};
  obs.forEach(o => {
    const d = Math.hypot(est.x - o.s.x, est.y - o.s.y);
    const r = o.dist - d;                       // 测量 − 估计
    per[o.s.sid] = r;
    sum2 += r * r;
  });
  const rms = Math.sqrt(sum2 / obs.length);
  return { rms, per, gdop: gdopOf(est, obs.map(o => o.s)),
           medDist: median(obs.map(o => o.dist)) };
}

/* 站位布局条件数（与估计点无关）：经典最小二乘矩阵 A（行 = 2(xj-x0), 2(yj-y0)）
   的 λmax/λmin；∞ = 完全共线。**解不出来时**用它区分「站位几何不行」还是「距离环不相交」。 */
function layoutCond(obs) {
  if (obs.length < 2) return null;
  const x0 = obs[0].s.x, y0 = obs[0].s.y;
  let a00 = 0, a01 = 0, a11 = 0;
  for (let j = 1; j < obs.length; j++) {
    const ax = 2 * (obs[j].s.x - x0), ay = 2 * (obs[j].s.y - y0);
    a00 += ax * ax; a01 += ax * ay; a11 += ay * ay;
  }
  const tr = a00 + a11, det = a00 * a11 - a01 * a01;
  const disc = Math.max(tr * tr / 4 - det, 0);
  const l1 = tr / 2 + Math.sqrt(disc), l2 = tr / 2 - Math.sqrt(disc);
  if (l2 <= 1e-12) return Infinity;          // 完全共线
  return Math.sqrt(l1 / l2);
}

/* 等级：good / fair / bad（null = 无法判定） */
function gdopGrade(g) {
  if (g === null || g === undefined || !isFinite(g)) return "bad";
  if (g <= 1.5) return "good";
  if (g <= 4) return "fair";
  return "bad";
}
function resGrade(rms, medDist) {
  const base = Math.max(medDist || 0, 1);
  if (rms <= 0.2 * base) return "good";
  if (rms <= 0.6 * base) return "fair";
  return "bad";
}

/* ---------------- 多观测一致性判定（「孤证不立」的落地） ----------------
   为什么需要：`xport`（路由器自报观测）**不在签名预像内** —— 能冒充/控制一台路由器的人就能
   伪造定位，而多路由器粗定位用的恰恰是它。身份类手段（白名单 / 运营商线路绑定）只能回答
   "谁在说"，回答不了"说得对不对"；**冗余**（多个互不依赖的观测互相印证）才是去伪存真的手段。

   判定（改这里必须同步改规格：`Protocol/docs/orpah-over-halow/SPEC.md` §8 原则 P-1 / §10 F-12）：
   - 观测 < 2 → 连位置都解不出 → `none`（如实说"没解"，不编一个点）；
   - 2 台 → 解得出，但**无冗余**：任一台偏了都看不出来 → `single`（不可交叉校验）；
   - ≥3 台 → 用**留一法标准化残差**逐台查：
       · 全都在噪声可解释范围内 → `verified`（**这才是"交叉校验通过"**）；
       · 有台超出 → 取 z 最大者，还要**剔掉它之后其余台真的自洽**才敢定案 →
         剔除后 `verified` + `dropped=[它]`（**离群 ≠ 没事**：它是一条安全线索，页面要显示、要进安全事件）；
       · 定不了案 → `conflict`（观测互相矛盾 → 不输出"看着很确定"的假位置），并把 `suspects`
         作为**参考线索**带出去（最可疑的那些台，**仅线索、不是结论**）。
   判据为什么是留一法 z（三道实测碰出来的墙，别再重踩）：
   ① **不能用"单台残差 > k·σ_i"**：单个离群观测会把**整体拟合带弯**，各台残差被摊平到各自阈值以内
     —— 4 台里 1 台把距离报成 1/4，照样"全部残差合格"（漏检）。
   ② **z 的 σ 必须锚在"其余台推出的距离"上，不能锚在它自己报的距离上**：
     锚在自报距离上时，"报得更远"的那台会把自己的 σ 一起放大（z = 4|f−1|/f，最大只有 4.0）
     → **"偏更远"那一侧永远抓不住**：实测 ×4 偏差在整条路线上 **0/120 全没发现**，
     还照样报"交叉校验通过"。锚在推出距离上则两边对称：z = 4|f−1| → ×4 → 12.0、×0.25 → 3.0。
   ③ **z 的分母不能只算本台测距噪声**：其余台里混着一台偏得多的时，预测点本身就被带偏，
     于是**没偏的那台也会算出很大的 z**（实测 z=9.3 与真偏那台 12.0 几乎分不开）→ 误剔除 11%。
     必须把**预测的不确定度**（`wlsLocate` 协方差沿"预测点→本台"方向的投影）算进去，再用 σ0 放大。
   阈值是**按实测零假设分布定的**，不是拍的：诚实场景 max-z 上界 ≈ **3.8，且与噪声幅度无关**
   （±1/±2/±4 dBm 一样 —— 统计量已对噪声归一）→ 阈值取 5（原拍脑袋的 3 会误报 11%）。

   **判据的性质（务必别越界）**：它只判"这一台与**其余观测 / 与模型**不一致"，**判不了原因**。
   "不一致"的成因**不止一种**：遮挡/多径、天线损坏或接头松、元件性能下降/老化、标定漂移、
   环境与模型不符（我们的 A/n 对数路径损耗模型**只假定空旷无遮挡**），当然也可能是被改装/冒充。
   所以页面上只能说"与其余不一致 / 拟合偏松"，**不得**归因到具体原因，也不得给它贴道德标签；
   剔除之外**必须留人复核**（把不一致当**线索**，不当结论）。规格同步写明（SPEC §8 原则 P-1）。

   **能被抓的偏差下界 = 噪声的可分辨下界（且方向不对称）**：与测距噪声同量级的偏差在原理上与
   噪声不可分（σ0 不超限就不报）；而且**"偏更远"与"偏更近"不对称** —— "偏更远"无上界
   （可靠可检：4 台实测 77% / 5 台 87%），"偏更近"的误差上限就是它自己报的距离 → z 有硬上界
   （≈3.8，与诚实尾部重合）→ **数学上不可分辨**（×0.25/×0.1 实测 864/864 全漏）。
   这是老实话、不是漏检 —— 别为了"提高检出率"调低判据，那只会把正常噪声当攻击（**假警报最害人**）。
   **辨识性的硬边界**：**3 台里有 1 台不一致时，能测出"不一致"，但判不出是哪台** —— 排除一台后
   只剩 2 台，而 2 台永远能拟合得天衣无缝（dof=0），没有冗余可供辨识。要**定到哪一台**需要
   **≥4 台**（排除一台后仍有 ≥3 台可互证）。这不是实现偷懒，是信息论边界（规格同步写明）。

   **移动目标**：各台观测取得于**各自时刻**，把不同时刻的观测算成"同一时刻"会把正常走动判成
   互相矛盾。给了 `opts.vel`（m/s，来自卡尔曼状态）与各观测的 `ts` 后，把测量等价搬到参考时刻
   `tref`：目标在 p(tref) 时于 t_i 测得 d_i ⟺ 站位在 `s_i − v·(t_i−tref)` 处对**静止**目标测得 d_i
   —— 于是只需**平移站位**，残差判定与测量模型一字不改。没给速度/时刻就按静止处理，
   `moved=false` 如实带出来（页面可据此提示"未做运动补偿"）。

   返回 `{trust, est, kept, dropped, obs, rms, maxRes, sigma0, loose, reason, moved, k}`；
   `trust ∈ none|single|verified|conflict`；`est` 与 `wlsLocate` 同形（解不出时 `ok:false`）。
   `sigma0` = 拟合残差；`loose` = 残差明显偏大（**只是事实、不是定罪**，见 CONS_S0_LOOSE）。
   **纯函数**（不读 `Date.now()`、不用随机）→ 实时页与回放页对同一输入给逐位相同的结果。 */
const CONS_MIN = 3;       // 「自洽」至少要几台：3 —— 2 台永远能拟合得天衣无缝，判不出哪台偏了
const CONS_Z = 5;         // 判定阈值（**不是“定罪”**：只判“与其余不一致”，不判原因，见上“判据的性质”）。
                          // 按实测零假设分布定的：诚实场景 max-z 的 p99.9 / 上界 ≈ 3.8，
                          // 且**与噪声幅度无关**（±1/±4 dBm 同样是 3.8 —— 统计量已对噪声归一）。
                          // 所以阈值取 5（在诚实上界之上留余量）；原先拍脑袋的 3 会误报 11%。
const CONS_SEP = 1.5;     // 还要**明显突出**：最大 z ≥ 1.5×次高（其余台里混着一台偏大的时，好台也会偏大）
/* 「拟合残差明显偏大」的事实门限（**不是定罪**：只说明“这组观测拟合不紧”，不指认哪台）。
   为什么要它：**多台同时偏大**（n=4 里 2 台）时各台的 z 都变小 —— 四个圆可能都“看着还行”，
   z 定不了案（这是 SPEC 的信息论边界：n−k ≥ 3 才能定位，4−2=2 不行）。
   但此时**拟合残差 σ0 会明显变大**：实测诚实场景 σ0 上界
   ±1dBm 0.45 / ±2dBm 0.84 / ±4dBm 1.62（均 n=4），而“2 台×4”无论噪声大小中位都 ≈ 2.1。
   故取 1.8：演示噪声（±2dBm）下诚实不误报，多台偏大时报出事实。 */
const CONS_S0_LOOSE = 1.8;
function consensus(obs, opts) {
  const o = opts || {};
  const zMin = o.z !== undefined ? o.z : CONS_Z;
  const vel = o.vel || null;
  const tref = (o.tref !== undefined && o.tref !== null) ? o.tref : null;
  const list = (obs || []).filter(x => x && x.s && isFinite(x.dist));

  /* 折算到参考时刻：等价于把**站位**平移 −v·Δt（目标当静止处理）。 */
  let moved = false;
  const shifted = list.map(x => {
    const ts = (x.ts !== undefined && x.ts !== null) ? x.ts : tref;
    if (!vel || tref === null || ts === null || ts === tref) return x;
    const dt = (ts - tref) / 1000;            // 秒
    moved = true;
    return { ...x, dt,
             s: { ...x.s, x: x.s.x - vel.vx * dt, y: x.s.y - vel.vy * dt } };
  });
  const n = shifted.length;
  const sidOf = (i) => shifted[i].s.sid;

  const resOf = (e, idx) =>
    idx.map(i => shifted[i].dist - Math.hypot(e.x - shifted[i].s.x, e.y - shifted[i].s.y));
  const fit = (idx) => {                      // init 一律给**线性解**（2 台给两圆交点）
    const arr = idx.map(i => shifted[i]);
    return wlsLocate(arr, trilaterate(arr.map(x => x.s), arr.map(x => x.dist), null));
  };
  /* 各台的留一法统计（z 与 ratio）**只算一次**：判定、页面诊断、多帧持续偏差扫描都复用这一份。
     必须在 pack 之前声明：早退路径（观测不够/解不出）也会调 pack，那时还没算。 */
  let zAll = null, ratiosAll = null;
  const pack = (trust, reason, est, idx, dropped, suspects) => {
    const r = est && est.ok ? resOf(est, idx) : [];
    const s0 = (est && est.ok && isFinite(est.sigma0)) ? est.sigma0 : null;
    return { trust, reason, est: est || null, kept: idx.map(sidOf), dropped,
             suspects: (suspects || []).map(sidOf), obs: shifted,
             rms: est && est.ok ? est.rms : null,
             maxRes: r.length ? Math.max(...r.map(Math.abs)) : null,
             zs: (est && est.ok) ? (zAll || []) : null,   // 各台的 z（诊断用，页面可显示）
             /* 各台的「实测/预测」比值（与 z 同一份预测）：留着给**多帧持续偏差扫描**用 ——
                单帧对“偏得更小”是不可分辨的，但持续偏差在多帧上会累计出统计痕迹。 */
             ratios: ratiosAll,
             sigma0: s0,                                  // 拟合残差（诊断/展示；页面直接显示）
             loose: s0 !== null && s0 >= CONS_S0_LOOSE,   // 残差偏大：只陈述事实，不指认谁
             moved, zMin };
  };

  if (n < 2) {                                // 连位置都解不出：如实说"没解"，不编一个点
    return pack("none", n ? "single_obs" : "no_obs", null, [], [], []);
  }
  const all = shifted.map((_, i) => i);
  const est0 = fit(all);
  if (!est0 || !est0.ok) {
    return pack("none", (est0 && est0.reason) || "no_solution", est0, [], [], []);
  }
  /* 留一法：用**其余台**解出位置，再看这一台的实测距离“应该是多少”。
     σ 锚在**推出的**距离上（不是它自报的）—— 理由见函数头注释的两道墙。
     返回 `{z, ratio}`（ratio = 实测/预测，供多帧持续偏差扫描用）；比不出来就回 null。 */
  const looStat = (i, subset) => {
    const rest = (subset || all).filter(j => j !== i);
    if (rest.length < 2) return null;
    const arr = rest.map(j => shifted[j]);
    /* 两台时两圆有两个交点（二义），靠**当前估计**选靠近的那个 —— 不能不给参照：
       `trilaterate` 不传 last 会取“离原点更近的那个”，于是预测点可能跳到另一个分支，
       于是**没偏的那台也会算出很大的 z**（实测：诚实场景 11% 被误剔除、一台偏大时
       六成判不出）。给了参照分支就确定且合理了。 */
    const init = trilaterate(arr.map(x => x.s), arr.map(x => x.dist),
                             (est0 && est0.ok) ? est0 : null);
    const e = wlsLocate(arr, init);
    if (!e || !e.ok) return null;             // 其余台连解都解不出 → 这台无从评价
    const dpred = Math.hypot(e.x - shifted[i].s.x, e.y - shifted[i].s.y);
    /* 分母 = 本台测距噪声 ⊕ **预测的不确定度**：
       · 预测点来自其余台的带噪观测，`wlsLocate` 已经给了它的协方差 `e.cov` ——
         把该不确定度沿“预测点→本台”方向投影出来（`sp`）；
       · 若预测用的那几台**彼此都对不上**（`sigma0` > 1），这个预测本身就更不可信，
         按 `sigma0` 放大。
       为什么必须这么做（实测踩过）：把预测当成准确值，分母就只有本台噪声 ——
       于是“其余台里混着一台偏大的”时，**没偏的那台也会被算出很大的 z**
       （实测 z_S4 = 9.3 与真正偏大那台的 12.0 几乎分不开），而诚实场景里
       11% 被误剔除、一台偏大时六成判不出。 */
    const sig = sigOf({ dist: dpred });
    let sp = 0;
    if (e.cov) {
      const dx = shifted[i].s.x - e.x, dy = shifted[i].s.y - e.y;
      const r = Math.hypot(dx, dy) || 1e-9;
      const ux = dx / r, uy = dy / r;
      sp = Math.sqrt(Math.max(0, ux * ux * e.cov.a + 2 * ux * uy * e.cov.b + uy * uy * e.cov.c));
    }
    const infl = (e.sigma0 && isFinite(e.sigma0)) ? Math.max(1, e.sigma0) : 1;
    return { z: Math.abs(shifted[i].dist - dpred) / (infl * Math.sqrt(sig * sig + sp * sp)),
             ratio: dpred > 0 ? shifted[i].dist / dpred : null };   // 实测/预测：多帧扫描用
  };
  const zOf = (i, subset) => {
    const s = looStat(i, subset);
    return s === null ? null : s.z;
  };
  const consistent = (idx) => {
    if (idx.length < CONS_MIN) return null;
    let ok = true, ev = 0;
    idx.forEach(i => {
      const z = zOf(i, idx);
      if (z === null) return;                 // 比不出来（那两台的圆不相交）→ **跳过，不当矛盾**
      ev++;
      if (z >= zMin) ok = false;
    });
    /* 至少要**两处**真的比过：比不出来 ≠ 矛盾，但也不能把“没验过”当成“验过了” */
    return ok && ev >= 2;
  };
  const loo = all.map(i => looStat(i, all));
  zAll = loo.map(s => (s === null ? null : s.z));
  ratiosAll = loo.map(s => (s === null ? null : s.ratio));
  const suspects = all.filter(i => zAll[i] !== null && zAll[i] >= zMin);

  if (n === 2) {                              // 2 台：解得出，但**无冗余** —— 任一台偏了都看不出来
    return pack("single", "no_redundancy", est0, all, [], []);
  }
  if (consistent(all) === true) {             // 全部自洽 → 这才是"交叉校验通过"
    return pack("verified", "consistent", est0, all, [], []);
  }

  /* **辨识性的硬边界**：只有"排除一台后还剩 ≥3 台"才谈得上**定到哪一台**（即观测数 ≥4）。
     3 台里有 1 台不一致时，排除一台只剩 2 台：要么拟合得天衣无缝（dof=0，无从检验）、
     要么两圆根本不相交 —— 两种都定不了是哪台，硬指着某台就是**误判**（宁可说"不可判定"）。
     所以 3 台一律报 conflict，只把 suspects 当参考线索带出去。 */
  let imax = -1;
  all.forEach(i => { if (zAll[i] !== null && (imax < 0 || zAll[i] > zAll[imax])) imax = i; });
  /* 「明显突出」才定案：最大 z 还要显著高于次高（倍率 CONS_SEP）——
     诚实的噪声里也会有某台碰巧偏大（尾部事件），若只看 z ≥ 阀值就会把好台剔掉
     （实测：不要求突出时，诚实场景 11% 被误剔除）。要定到一台，得同时满足
     “它超阀值” **且** “比第二大的高出一大截”，而不是靠阀值大小。 */
  let z2 = 0;
  all.forEach(i => { if (i !== imax && zAll[i] !== null && zAll[i] > z2) z2 = zAll[i]; });
  const standout = (imax >= 0) && zAll[imax] >= zMin && zAll[imax] >= CONS_SEP * z2;
  if (n >= CONS_MIN + 1 && standout) {
    const idx = all.filter(j => j !== imax);
    if (consistent(idx) === true) {           // 剔掉它之后**其余台真的自洽** → 才敢定案
      return pack("verified", "outlier_dropped", fit(idx), idx, [sidOf(imax)], suspects);
    }
  }
  return pack("conflict", "conflict_unresolved", est0, [], [], suspects);
}

/* ---------------- 补站位建议（几何不行时「往哪儿再放一台」） ----------------
   起因：页面已经能如实报出「站位共线 → 无解」「GDOP 大 → 估计不可信」，但**没说怎么办**。
   现场要的是一句可执行的话：往 (x, y) 再放一台。

   **判据选谁**（关键）：有估计点 → 用**该点的 GDOP**；没有估计点 → 用**布局条件数**。
   - 条件数只看站位坐标（与目标无关）→ 共线这种“根本解不出来、压根没有估计点”的场景也能给建议；
   - 有估计点时 GDOP 才是真正衡量“这个位置解得多准”的量（条件数好、但站位全在目标一侧时 GDOP 仍差）。
   返回里 `scoreBy` 如实标本次用的是哪个判据（页面/测试可核对，不藏着）。

   候选点 = 站位包围盒**各边按布局尺度外扩 `SPOT_PAD`**（尺度 = 两轴较大跨度，
   不是逐轴跨度 —— 共线布局有一轴跨度为 0，逐轴外扩会把候选压成“离直线 0.5 m”，等于没建议）；
   与已有站位距离 < `minSep`（站位间最大跨度 × `SPOT_MIN_SEP`）的候选剔除 ——
   两台贴在一起等于一台，几何上没有贡献。取 score 最小者（并列取网格序最小 → 结果确定）。

   **只估不改**：不改 stations/观测，只给一个建议坐标（0.1 m 取整，便于现场照读）。
   返回 null = 几何够好，无需建议（页面就说“无需补”，而不是无话可说）。
   注：“距离环不相交”型无解（条件数正常）= 测量不自洽，不是站位摆放问题 → 本函数**不给**建议。 */
const SPOT_PAD = 0.5;        // 候选区域：站位包围盒每边外扩（布局较大跨度 × 50%）
const SPOT_N = 13;           // 每轴网格数（169 个候选，够用且即时）
const SPOT_MIN_SEP = 0.25;   // 候选与最近站位的距离下限 = 站位间最大跨度 × 0.25
const COND_OK = 5;           // 条件数 ≤ 5 视为几何够好（与 gdopGrade 的 good 档同量级）
function suggestStation(obs, est) {
  const n = obs ? obs.length : 0;
  if (n === 0) return null;
  if (n < 2) {
    // 一个站位连方向都定不了 → 先把“有观测的站位”凑够，再谈摆哪儿
    return { need: true, reason: "too_few", x: null, y: null, minDist: null,
             condBefore: null, condAfter: null, gdopBefore: null, gdopAfter: null,
             scoreBy: null, have: n };
  }
  const condBefore = layoutCond(obs);
  const gdopBefore = est ? gdopOf(est, obs.map(o => o.s)) : null;
  const weakGdop = gdopBefore !== null && gdopGrade(gdopBefore) !== "good";
  const weakCond = !(condBefore <= COND_OK);      // condBefore=Infinity（共线）→ true
  if (est ? (!weakGdop && !weakCond) : !weakCond) return null;
  const reason = condBefore === Infinity ? "collinear" : "layout";

  const xs = obs.map(o => o.s.x), ys = obs.map(o => o.s.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const spanX = maxX - minX, spanY = maxY - minY;
  // 候选区域用**布局尺度 R**（两轴较大跨度）各向外扩 —— 共线/贴成一条线时有一轴跨度为 0，
  // 逐轴外扩会把候选压成“离直线 0.5 m”，等于没建议（实测踩过：建议 y=-0.5）。
  const R = Math.max(spanX, spanY, 1);
  const pad = R * SPOT_PAD;
  let maxSep = 0;
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) maxSep = Math.max(maxSep, Math.hypot(xs[i] - xs[j], ys[i] - ys[j]));
  }
  const minSep = Math.max(maxSep * SPOT_MIN_SEP, 0.5);
  let best = null;
  for (let gi = 0; gi < SPOT_N; gi++) {
    for (let gj = 0; gj < SPOT_N; gj++) {
      const cx = minX - pad + (spanX + 2 * pad) * gi / (SPOT_N - 1);
      const cy = minY - pad + (spanY + 2 * pad) * gj / (SPOT_N - 1);
      let dmin = Infinity;
      for (let i = 0; i < n; i++) dmin = Math.min(dmin, Math.hypot(cx - xs[i], cy - ys[i]));
      if (dmin < minSep) continue;
      const pts = xs.map((x, i) => ({ x, y: ys[i] })).concat([{ x: cx, y: cy }]);
      const condAfter = layoutCond(pts.map(p => ({ s: p })));
      const gdopAfter = est ? gdopOf(est, pts) : null;
      const score = est ? gdopAfter : condAfter;
      if (score === null || !isFinite(score)) continue;
      if (!best || score < best.score - 1e-12) {
        best = { x: cx, y: cy, score, condAfter, gdopAfter, minDist: dmin };
      }
    }
  }
  if (!best) return null;
  // 取整只用于“照读”，**不能把 null 变成 0**（无值就是无值，这是本仓的约定）
  const r1 = v => (v === null || v === undefined ? null
                   : (isFinite(v) ? Math.round(v * 100) / 100 : v));
  return {
    need: true, reason, have: n,
    x: Math.round(best.x * 10) / 10, y: Math.round(best.y * 10) / 10,
    minDist: r1(best.minDist), condBefore: r1(condBefore), condAfter: r1(best.condAfter),
    gdopBefore: r1(gdopBefore), gdopAfter: r1(best.gdopAfter),
    scoreBy: est ? "gdop" : "cond",
  };
}

/* ---------------- 观测归集（单一实现：实时与回放共用） ----------------
   站位 s 在**时刻 t** 的观测（优先级从高到低）：
     1) 手动绑定 rssi → 直接用（不受时间窗限制）；
     2) 时间窗 [t0, t1) 存在且 t0 <= t → 取窗内样本的 RSSI **中位数**
        （「无人机悬停在某点」的模型，前提是目标静止）；
     3) **路由器序列**：给了该站位自己的样本序列 → 取 t 之前**最近一条**
        （多路由器同时观测；运动目标必须用最近一条，用长窗中位数等于把
         不同时刻的测量混在一起）；
     4) 其它 → 无观测。
   **样本一律限于 ts <= t**：回放时不能看未来（否则会提前知道后面的测量）。
   窗口/绑定保持原语义不变，路由器序列是**新增**的低优先级分支（老数据不受影响）。 */
const ROUTER_MAX_AGE_MS = 30000;   // 「最近一条」的时效：超过就不算当前观测

function latestAt(samples, t) {
  /* 取 t 之前最近一条（含时效判断）→ {rssi, n, age} 或 null。
     samples 需按 t 升序；这里仍按最大值兜底（乱序输入也不会取错）。 */
  let best = null, cnt = 0;
  for (const p of samples || []) {
    if (typeof p.rssi !== "number" || p.t > t) continue;
    if (t - p.t > ROUTER_MAX_AGE_MS) continue;
    cnt++;
    if (!best || p.t > best.t) best = p;
  }
  if (!best) return null;
  return { rssi: best.rssi, n: cnt, age: t - best.t };
}

function obsOfStation(s, samples, t) {
  if (s.rssi !== null && s.rssi !== undefined) {
    return { rssi: s.rssi, n: 1, src: "bind", t: null };   // 绑定：语义就是“当前值”，时刻未知
  }
  if (s.t0 !== null && s.t0 !== undefined && s.t0 <= t) {
    const t1 = (s.t1 === null || s.t1 === undefined) ? Infinity : s.t1;
    const sel = samples.filter(p => p.t >= s.t0 && p.t < t1 && p.t <= t
                                   && typeof p.rssi === "number");
    if (sel.length) {
      const vals = sel.map(p => p.rssi).sort((a, b) => a - b);
      const m = vals.length >> 1;
      const med = vals.length % 2 ? vals[m] : (vals[m - 1] + vals[m]) / 2;
      /* 本观测的**代表时刻**：窗内样本时刻的中位（运动补偿需要它；
         不然一条跨越几十秒的窗口会被当成“同一瞬间”的测量）。 */
      const ts = sel.map(p => p.t).sort((a, b) => a - b);
      return { rssi: med, n: sel.length, src: "window", t: ts[ts.length >> 1] };
    }
    return null;                      // 窗已开但窗内还没有样本
  }
  const last = latestAt(samples, t);
  if (last) return { rssi: last.rssi, n: last.n, src: "router", t: t - last.age };
  return null;
}

/* 「这个站位该用哪份样本」—— **单一判据**，实时页与回放页共用（以前两页各写一份三元表达式）。
   为什么需要它：后端 `router_obs_range()` **会给每台站位一条序列，没数据就是空表**
   （`{S1:[…], S2:[], S3:[]}`），而老数据源（悬停窗口那套）压根没有 per-station 序列，
   观测落在**设备上报流**里。于是“空”有两种含义，必须分开：
     · 有 per-station 序列的数据源 + 该站位空表 → 这台**没测到** → 返回 `[]`（无观测）。
       若回落到设备流，就等于把“**设备自己**到最近那台路由器的强度”当成**该站位**的测量：
       几台站位会拿到同一份相关值，定位给出一个看着正常（RMS/GDOP 都好）的**错位置**。
       —— 2026-09-13 实测复现：`{S1:200 条, S2:[], S3:[]}` → 3 个观测、
       S2/S3 距离都等于设备流那条（30.2 m）→ 估计 (-1.9, 0.0)。
     · **整个** per-station 序列都没有（老数据源 / 旧后端）→ 回落到设备流，保留悬停窗口语义。 */
function samplesFor(routerObs, sid, deviceSamples) {
  const map = routerObs || {};
  if (Object.keys(map).length) return map[sid] || [];   // 有 per-station 数据 → 空表就是“没测到”
  return deviceSamples || [];                           // 老数据源 → 观测在设备流里
}

/* 时刻 t 的全部观测（含距离换算）
   samples 可以是**一个数组**（所有站位共用，如实时流的旧用法），
   也可以是**函数 sid → 该站位的样本序列**（回放/多路由器：每台一份）。 */
function obsAt(stations, samples, t, A, n) {
  const of = (typeof samples === "function") ? samples : (() => samples || []);
  const out = [];
  for (const s of stations) {
    const o = obsOfStation(s, of(s.sid) || [], t);
    if (o) out.push({ s, rssi: o.rssi, n: o.n, src: o.src,
                      ts: (o.t === undefined ? null : o.t),   // 本观测自己的时刻（未知 = null）
                      dist: distFromRssi(o.rssi, A, n) });
  }
  return out;
}

/* ---------------- 判定选项（两页共用同一口径） ----------------
   `consensus()` 要的是“把各观测折算到参考时刻”，而折算要三样东西：参考时刻 `tref`、
   目标速度 `vel`、以及各观测**各自的时刻**（已在 `obs[i].ts`）。三样少一样就不是真补偿。
   为什么把拼装也放这里（而不是两页各写一行）：
   · **漏传 vel** → 正常走动会被当成“观测互相矛盾”（假警报最害人）；
   · **漏传 tref / ts** → 同上，只是更隐蔽。
   → 单一源，两页都走 `consOpts()`；给不出速度（如实时页没有滤波器状态）就如实不补偿，
     `consensus()` 会回 `moved=false`，页面据此标「未做运动补偿」，不准静默当成已补偿。 */
function consOpts(tref, vx, vy) {
  const ok = isFinite(vx) && isFinite(vy) && (vx !== 0 || vy !== 0);
  return { tref: (tref === undefined ? null : tref),
           vel: ok ? { vx: vx, vy: vy } : null };
}

/* ---------------- 判定结果的**文案**（两页共用同一套词） ----------------
   为什么连文案也放这里：判据是 `consensus()`，但“把结果说成哪句话”如果在两个页面各写一份，
   迟早漂移（本仓最忌讳的那种不一致：同一时刻两页给出不同可信度措辞）。
   `T` = 页面的翻译函数（`ui_i18n.js` 的 `T`）→ `pos.js` 不依赖具体字典实现，单测可传桩函数。
   **只有判据能判“不一致”，不能判原因**（遮挡/多径、天线、元件、标定、被改装/冒充都可能），
   所以措辞限定在“一致 / 单一来源 / 冲突 / 剔除离群”，并且带“仅线索”口径 —— 不许归因。 */
function trustText(c, T) {
  const n = (c && c.obs) ? c.obs.length : 0;
  if (!c || !n || c.trust === "none") return T("tk_trust_none");
  if (c.trust === "verified") {
    if (c.dropped && c.dropped.length) {
      return T("tk_trust_ok_dropped")
        .replace("{d}", c.dropped.join(",")).replace("{n}", c.kept.length);
    }
    return T("tk_trust_ok").replace("{n}", c.kept.length);
  }
  if (c.trust === "single") return T("tk_trust_single").replace("{n}", n);
  let s = T("tk_trust_conflict").replace("{n}", n);
  if (c.suspects && c.suspects.length) {
    s += " · " + T("tk_trust_suspect").replace("{s}", c.suspects.join(","));
  }
  return s;
}

/* ---------------- 人级聚合（一个人的多台客户端 → 一个人在哪） ----------------
   背景：走失案件是**以人为单位**的（`cases.py`：标记一个人 → 名下所有设备进丢失态，
   任一台被发现即视为发现该人）；而一个人可能带着/穿着多台客户端（项链、鞋、眼镜…）。
   每台各自上报、各自被多台路由器测到 → 各自能解出一个位置；现场要的是**一个人**在哪。

   ★ **前提假设（必须写在页面上，不许省）**：这些设备**假定与同一个人在一起**。
   现实里它们完全可能分开（手机被拿走、鞋掉一只、备用设备留在家里）——
   而**这一层的观测数据没法验证这个假设**。实测（本演示的 4 台站位 + ±2 dBm 噪声，20000 次模拟）：
     · 单台定位的 95% 椭圆半径**中位就有 19.5 m**（p95 38.7 m、上界 78 m）；
     · 于是「两台设备真相距 100 m / 200 m / 500 m」与「两台设备在一起」的
       δ/√(σ1²+σ2²) 统计量**区分不开**（中位 0.62 / 0.31 / 0.13，**比同点的 0.19 还小** ——
       离得越远，距离误差越大、椭圆越大，反而更像“同一片糊”）。
   ⇒ 所以本函数**不做“设备是否分开”的判定**（要做就是编一个抓不住东西的判据），
     只做两件有实据的事：
       ① **合并成一个人**（1/σ² 加权平均）——多台设备是**同一个人**的多次独立测量，
          观测噪声会按 1/√n 收敛（这是本层唯一的真收益）；
       ② **共模误差提醒**：站位/环境误差（多径、标定漂移）对同一人的几台设备是**共同的**，
          平均**消不掉**它 → 合并半径只代表“观测噪声”，不代表真实位置误差。
     `spread_m`（两两最大间距）只作**陈述**带出去 —— 页面上必须同时说明“它不能证明它们在一起”。

   入参 `fixes` = 每台设备**各自**的结果：`[{sn, est:{x,y,ok}, trust, sigma}]`
     · `trust` 来自各自的 `consensus()`（none|single|verified|conflict）；
     · `sigma` = 该台的定位不确定度（米），页面传 `wls.ellipse.radius`（95% 半长轴）；
       缺省回退 `rms`；两者都没有 → 该台只作参考、不参与加权（不给它编一个权重）。
   出参 `{trust, est, radius, used, dropped, spread_m, why, devices}`。
   纯函数（不读时钟、不用随机）→ 实时页与回放页对同一输入给逐位相同的结果。

   规则（逐级如实降级，任何时候都不硬凑一个点）：
     1. 「参与」= `est && est.ok` 且 `trust !== "conflict"` —— 冲突台**自己的定位**就不可信
        （它的几台路由器互相矛盾），不参与平均，但在 `dropped` 里列出来；
     2. 0 台可参与 → `none`（why 区分 `no_fix` 一台都没解出 / `all_conflict` 全都冲突）；
     3. 1 台可参与 → **single**（单一来源、未交叉校验），位置就是那一台；
     4. ≥2 台 → **1/σ² 加权平均**；人级 trust 取参与台里**最弱**的那个 ——
        多台一致只说明它们**互相对得上**，不等于每台都过了交叉校验，不许吹成 verified。 */
const FUSE_ORDER = { none: 0, conflict: 1, single: 2, verified: 3 };

function fusePerson(fixes) {
  const list = (fixes || []).filter(f => f && f.sn);
  const ok = list.filter(f => f.est && f.est.ok && f.trust !== "conflict" && f.trust !== "none");
  const sigmaOf = (f) => {
    if (f.sigma !== undefined && f.sigma !== null && isFinite(f.sigma) && f.sigma > 0) return f.sigma;
    if (f.rms !== undefined && f.rms !== null && isFinite(f.rms) && f.rms > 0) return f.rms;
    return null;
  };
  /* 两两最大间距：**只作陈述**（不是判据，见头注释） */
  let spread = null;
  for (let i = 0; i < ok.length; i++) {
    for (let j = i + 1; j < ok.length; j++) {
      const d = Math.hypot(ok[i].est.x - ok[j].est.x, ok[i].est.y - ok[j].est.y);
      if (spread === null || d > spread) spread = d;
    }
  }
  const pack = (trust, why, est, radius, used) => ({
    trust, why, est: est || null, radius: (radius === undefined ? null : radius),
    used: (used || []).map(f => f.sn),
    dropped: list.filter(f => !(used || []).includes(f)).map(f => f.sn),
    spread_m: spread, devices: list,
  });
  if (!ok.length) {
    return pack("none", list.some(f => f.est && f.est.ok) ? "all_conflict" : "no_fix", null, null, []);
  }
  if (ok.length === 1) {
    return pack("single", "one_device", ok[0].est, sigmaOf(ok[0]), ok);
  }
  /* 1/σ² 加权；σ 拿不到的台给**中位权重**（不因“没给 σ”就被当成最可信或最不可信） */
  const ws = ok.map(f => { const s = sigmaOf(f); return s === null ? null : 1 / (s * s); });
  const known = ws.filter(x => x !== null).sort((a, b) => a - b);
  const mid = known.length ? known[Math.floor(known.length / 2)] : 1;
  const wt = ws.map(x => (x === null ? mid : x));
  const wSum = wt.reduce((s, x) => s + x, 0);
  const est = {
    ok: true,
    x: ok.reduce((s, f, i) => s + f.est.x * wt[i], 0) / wSum,
    y: ok.reduce((s, f, i) => s + f.est.y * wt[i], 0) / wSum,
  };
  /* 合并半径 = 1/√(Σ1/σ²)（只代表**观测噪声**：共模误差消不掉，见头注释） */
  const radius = known.length ? Math.sqrt(1 / wSum) : null;
  let weakest = "verified";
  ok.forEach(f => { if ((FUSE_ORDER[f.trust] || 0) < (FUSE_ORDER[weakest] || 0)) weakest = f.trust; });
  return pack(weakest === "none" ? "single" : weakest, "agree", est, radius, ok);
}

/* 人级聚合结果的**文案**（单一源：页面不另写一份，否则措辞必漂移）。
   两条不许省：
     · **前提**：这些设备是**假定**在同一人身上的（数据验证不了，见头注释的实测数）；
     · **性质**：合并只压观测噪声，压不掉共模误差（站位/多径/标定）→ 半径别当真实误差。
   不判定“设备是否分开”（数据做不到），所以也不写“一致/不一致”这种暗示判断的话。 */
function fuseText(f, T) {
  if (!f) return T("fuse_none");
  if (f.trust === "none") {
    return f.why === "all_conflict" ? T("fuse_none_conflict") : T("fuse_none_nofix");
  }
  if (f.trust === "single") return T("fuse_single").replace("{n}", f.used.length);
  return T("fuse_agree").replace("{n}", f.used.length)
         .replace("{d}", f.spread_m === null ? "?" : f.spread_m.toFixed(1));
}

/* ---------------- 多帧（时序）持续偏差扫描 ----------------
   为什么需要它：`consensus()` 的单帧留一法对**“偏得更小”那一侧**在数学上不可分辨 ——
   “它报的距离偏小 k 倍”的误差上限就是它自己报的距离，z 有硬上界（实测最大 ≈3.8，与诚实尾部重合）
   → ×0.25/×0.1 实测 864/864 全漏（见 consensus 头注释）。而**持续**的偏差会留下统计痕迹：
   逐帧的「实测/预测」比值在诚实台上围绕 1 散开（中位数趋 1），
   持续偏 k 倍的台，中位比值趋 k —— 帧数一多就分得开。

   入参 `rows`：逐帧一组 `{sid, ratio}`（ratio 来自 `consensus().ratios`，**同一份预测**，不另算）。
   出参：`[{sid, n, med, mad, se, z}]`，`z = |med − 1| / se`，其中
   `se = 1.253 × MAD / √n`（中位数的渐近标准误；用 MAD 而不是标准差 → 不被个别离群帧带跑）。

   **它只说“持续地对不上”，不说原因，也不替代单帧判定**：
   · 阈值按**实测零假设分布**定（`bias_calib*.js` 那套模拟：4 台站位 + 匀速靶标 + ±2 dBm 噪声，
     每档 40 组种子）：诚实台 z 的上界 —— **60 帧 7.4 / 100 帧 5.8 / 300 帧 6.7 / 800 帧 18.5 /
     2000 帧 22.2**（**随帧数缓慢上涨**：比值序列**时间相关**（预测误差在时间上相关），
     所以 `MAD/√n` 低估了不确定度 —— 但上涨是次线性的，2000 帧以内上界 ≤ 22）
     → **阈值取 30**（留 ~1.35× 余量）；超过 2000 帧就**均匀抽稀**到 2000（否则上界会继续爬）。
   · 检出下限（实测，同条件、取最坏种子）：**×0.5 / ×0.25 从 ~100 帧起就可靠**
     （z 最小 31.8 / 93.6）；**×0.75 要 ≳300 帧**（最小 31.4）；60 帧连 ×0.75 都分不开（最小 5.3）。
     即：**帧数越多越灵，帧少时如实不报**（页面要把“用了多少帧”写出来）。
   · **全体台一起偏（如环境标定整体偏了）它看不出来** —— 它比的是“这台 vs 其余台”，
     所以全局性偏差得靠实测标定（F-11）；这句话必须如实写在页面上。 */
const BIAS_MIN_N = 30;        // 太少帧不谈“持续”（中位数标准误还太大）
const BIAS_MAX_N = 2000;      // 超过就均匀抽稀（实测上界在 2000 帧以内 ≤ 22，阈值才稳）
const BIAS_Z = 30;            // 判“持续偏”的阈值（实测诚实上界 ≈22，见上）
function biasScan(rows, opts) {
  const o = opts || {};
  const minN = o.minN === undefined ? BIAS_MIN_N : o.minN;
  const maxN = o.maxN === undefined ? BIAS_MAX_N : o.maxN;
  const zMin = o.z === undefined ? BIAS_Z : o.z;
  const src = rows || [];
  const step = maxN > 0 ? Math.max(1, Math.ceil(src.length / maxN)) : 1;
  const bySid = {};
  src.forEach((row, i) => {
    if (step > 1 && i % step !== 0) return;      // 均匀抽稀（保序，不挑帧）
    (row || []).forEach(cell => {
      if (!cell || cell.sid === undefined || cell.sid === null) return;
      const r = cell.ratio;
      if (r === null || r === undefined || !isFinite(r) || r <= 0) return;
      (bySid[cell.sid] = bySid[cell.sid] || []).push(r);
    });
  });
  const out = [];
  Object.keys(bySid).forEach(sid => {
    const v = bySid[sid].slice().sort((a, b) => a - b);
    const n = v.length;
    const med = median(v);
    const mad = median(v.map(x => Math.abs(x - med))) || 0;
    /* MAD = 0（例如噪声设成 0 → 比值恒为 1）→ 不给 z：那种情况下“分不开”是事实，
       不能拿 0 当分母造出一个巨大的 z（实测踩过：抽稀到 5 个样本时诚实台 z 冲到 107）。 */
    const se = (n && mad > 0) ? 1.253 * mad / Math.sqrt(n) : null;
    const z = se ? Math.abs(med - 1) / se : null;
    out.push({ sid: sid, n: n, med: med, mad: mad, se: se, z: z,
               tooFew: n < minN, flagged: z !== null && n >= minN && z >= zMin });
  });
  out.sort((a, b) => (b.z === null ? -1 : b.z) - (a.z === null ? -1 : a.z));
  return out;
}

/* ---------------- 时序平滑（恒速卡尔曼） ----------------
   为什么**不能**用滑动平均/低通：对**移动目标**，N 点平均的滞后 ≈ (N/2) 个采样周期 ——
   站位 2 s 一采、人走 1.2 m/s 时，5 点平均就滞后 ~5 m，比测量噪声（±2 dBm ≈ 1~2 m 测距）还大，
   结果是"看起来平滑了、其实偏了"。所以用**恒速模型**（状态 x,y,vx,vy）：它用速度外推，
   匀速运动下几乎没有滞后，同时把抖动压下去。

   与「搜索半径」的关系（ROADMAP 挂的开放问题，这里给出答案）：
   **滤波之后的不确定度不再等于测量协方差** —— 必须用滤波器的**后验协方差 P**
   （含过程噪声 Q 与测量噪声 R）给椭圆/半径；否则会把"平滑后的平滑"当成真实精度，
   正是本页一直在防的那种假自信。本函数返回每帧的 P 投影 → 由 `ellipseOf` 出椭圆。

   输入：按时间升序的**位置帧** [{t, x, y, cov?}]（cov={a,b,c} 为该帧测量协方差，缺省用 sigma 估值），
   即"松耦合"做法（对已解出的位置做滤波，而不是对 RSSI 做 EKF）—— 简单、够用；
   严格的非线性 EKF（直接滤测距）留作后续。**跨越长时间缺口会重置**（见 MAX_DT_MS），
   否则滤波会把缺口两侧当成连续运动。 */
const KF_ACC = 1.5;          // 过程噪声：加速度不确定度（m/s²）。人走路急停/转身的量级
const KF_SIG0 = 15;          // 起步先验位置不确定度（m）
const KF_VEL0 = 3;           // 起步先验速度不确定度（m/s）
const KF_MAX_DT_MS = 10000;  // 两帧间隔超过它 = 缺口 → 重置（别把中断当匀速运动）

function kalmanTrack(frames, opts) {
  const o = opts || {};
  const qa = o.acc !== undefined ? o.acc : KF_ACC;
  const dtMax = o.maxDtMs !== undefined ? o.maxDtMs : KF_MAX_DT_MS;
  const sigOf = (f) => {
    if (f.cov) {                       // 测量协方差（WLS 给的 2×2）
      return { a: f.cov.a, b: f.cov.b, c: f.cov.c };
    }
    const s = (f.sigma || 3) * (f.sigma || 3);
    return { a: s, b: 0, c: s };
  };
  let st = null;                       // [x, y, vx, vy]
  let P = null;                        // 4×4
  const out = [];
  const mat4 = () => [[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0]];
  for (const f of frames) {
    const R = sigOf(f);
    if (!st) {                          // 起步：位置取该帧，速度给 0 但方差大
      st = [f.x, f.y, 0, 0];
      P = mat4();
      P[0][0] = P[1][1] = KF_SIG0 * KF_SIG0;
      P[2][2] = P[3][3] = KF_VEL0 * KF_VEL0;
      out.push({ t: f.t, x: f.x, y: f.y, vx: 0, vy: 0, cov: { a: R.a, b: R.b, c: R.c } });
      continue;
    }
    const dt = Math.max(0.001, (f.t - out[out.length - 1].t) / 1000);
    if (dt * 1000 > dtMax) {            // 缺口 → 重置（并且不把两侧连成一条直线）
      st = [f.x, f.y, 0, 0];
      P = mat4();
      P[0][0] = P[1][1] = KF_SIG0 * KF_SIG0;
      P[2][2] = P[3][3] = KF_VEL0 * KF_VEL0;
      // 输出的 cov **故意**给测量协方差 R：重置帧的“位置”就是该帧测量本身
      // （没有滤波历史可言），R 正是它的协方差；后验 P 此时只反映新种子，反而会让人以为“已经融合过了”。
      out.push({ t: f.t, x: f.x, y: f.y, vx: 0, vy: 0, cov: { a: R.a, b: R.b, c: R.c },
                 reset: true });
      continue;
    }
    /* 预测 x = F x，P = F P Fᵀ + Q（恒速模型，Q 用加速度不确定度 qa） */
    const px = st[0] + st[2] * dt, py = st[1] + st[3] * dt;
    const d2 = dt * dt, d3 = d2 * dt, d4 = d2 * d2;
    const q = qa * qa;
    const Q = [
      [q * d4 / 4, 0, q * d3 / 2, 0],
      [0, q * d4 / 4, 0, q * d3 / 2],
      [q * d3 / 2, 0, q * d2, 0],
      [0, q * d3 / 2, 0, q * d2],
    ];
    const F = [[1,0,dt,0],[0,1,0,dt],[0,0,1,0],[0,0,0,1]];
    const PFt = mat4();
    for (let i = 0; i < 4; i++)
      for (let j = 0; j < 4; j++) {
        let s = 0;
        for (let k = 0; k < 4; k++) s += P[i][k] * F[j][k];   // (P Fᵀ)_ij
        PFt[i][j] = s;
      }
    const Pp = mat4();
    for (let i = 0; i < 4; i++)
      for (let j = 0; j < 4; j++) {
        let s = Q[i][j];
        for (let k = 0; k < 4; k++) s += F[i][k] * PFt[k][j];
        Pp[i][j] = s;
      }
    /* 更新：只观测位置（H = [I2 0]） */
    const S00 = Pp[0][0] + R.a, S01 = Pp[0][1] + R.b, S11 = Pp[1][1] + R.c;
    const det = S00 * S11 - S01 * S01;
    if (!(Math.abs(det) > 1e-12)) {     // 数值退化 → 直接采信测量
      out.push({ t: f.t, x: f.x, y: f.y, vx: st[2], vy: st[3],
                 cov: { a: R.a, b: R.b, c: R.c }, degenerate: true });
      st = [f.x, f.y, st[2], st[3]];
      P = Pp;
      continue;
    }
    const i00 = S11 / det, i01 = -S01 / det, i11 = S00 / det;
    const ry = f.y - py, rx = f.x - px;
    const K = mat4();                   // K = Pp Hᵀ S⁻¹（H 只取前两行）
    for (let i = 0; i < 4; i++) {
      K[i][0] = Pp[i][0] * i00 + Pp[i][1] * i01;
      K[i][1] = Pp[i][0] * i01 + Pp[i][1] * i11;
    }
    const nx = px + K[0][0] * rx + K[0][1] * ry;
    const ny = py + K[1][0] * rx + K[1][1] * ry;
    const nvx = st[2] + K[2][0] * rx + K[2][1] * ry;
    const nvy = st[3] + K[3][0] * rx + K[3][1] * ry;
    /* P = (I−KH) Pp (I−KH)ᵀ + K R Kᵀ —— **Joseph 形式**。
       为什么不能用更短的 P = (I−KH)Pp：那个形式在浮点下会丢掉对称正定，跑几十帧后
       创新协方差 S 近奇异 → 增益暴冲 → 状态发散（实测：真实数据跑到第 84 帧从 -11 m
       直接跳到 12000 m）。Joseph 形式数值上保正定，代价只是多两次矩阵乘。 */
    const A = mat4();                     // A = I − K H（H 只观测位置）
    for (let i = 0; i < 4; i++)
      for (let j = 0; j < 4; j++) {
        const kh = (j === 0) ? K[i][0] : (j === 1 ? K[i][1] : 0);
        A[i][j] = (i === j ? 1 : 0) - kh;
      }
    const AP = mat4();
    for (let i = 0; i < 4; i++)
      for (let j = 0; j < 4; j++) {
        let s = 0;
        for (let k = 0; k < 4; k++) s += A[i][k] * Pp[k][j];
        AP[i][j] = s;
      }
    let Pn = mat4();
    for (let i = 0; i < 4; i++)
      for (let j = 0; j < 4; j++) {
        let s = 0;
        for (let k = 0; k < 4; k++) s += AP[i][k] * A[j][k];
        s += K[i][0] * (R.a * K[j][0] + R.b * K[j][1])
           + K[i][1] * (R.b * K[j][0] + R.c * K[j][1]);
        Pn[i][j] = s;
      }
    for (let i = 0; i < 4; i++)             // 消掉浮点残差带来的不对称
      for (let j = i + 1; j < 4; j++) {
        const m = (Pn[i][j] + Pn[j][i]) / 2;
        Pn[i][j] = m; Pn[j][i] = m;
      }
    if (!isFinite(nx) || !isFinite(ny) || !(Pn[0][0] > 0) || !(Pn[1][1] > 0)) {
      /* 兜底：宁可重置并采信测量，也不要把 NaN/发散值写进轨迹（静默垃圾最糟） */
      st = [f.x, f.y, 0, 0];
      Pn = mat4();
      Pn[0][0] = Pn[1][1] = KF_SIG0 * KF_SIG0;
      Pn[2][2] = Pn[3][3] = KF_VEL0 * KF_VEL0;
      out.push({ t: f.t, x: f.x, y: f.y, vx: 0, vy: 0,
                 cov: { a: R.a, b: R.b, c: R.c }, reset: true, diverged: true });
      P = Pn;
      continue;
    }
    st = [nx, ny, nvx, nvy];
    P = Pn;
    out.push({ t: f.t, x: nx, y: ny, vx: nvx, vy: nvy,
               cov: { a: Pn[0][0], b: Pn[0][1], c: Pn[1][1] } });
  }
  return out;
}

/* 轨迹抖动度：相邻点距离的均值（m）。平滑效果的可量化指标 —— 原始 vs 平滑。 */
function pathJitter(pts) {
  const v = (pts || []).filter(Boolean);
  if (v.length < 2) return 0;
  let s = 0;
  for (let i = 1; i < v.length; i++) s += Math.hypot(v[i].x - v[i - 1].x, v[i].y - v[i - 1].y);
  return s / (v.length - 1);
}

/* ---------------- 有效时段分段（回放「标注缺口 / 跳过无效段」用） ----------------
   把 [t0, t1] 切成最多 maxSeg 段，逐段给出「有几台站位有观测」：
     0   = 无观测（解不出位置，也没有距离环）
     1   = 只有 1 台（有距离环，但解不出位置 —— 信息不足）
     >=2 = 可定位
   **语义直接调用 obsOfStation**（不另写一套判定），只是把时间分批以免逐毫秒扫描。
   跳过/标注只认「>=2」为有效：1 台观测给不出坐标，对找人没有可操作信息。

   性能：时间轴最长 12 h（回放上限）。对**走路由器序列**的站位，只把 t 附近
   2×ROUTER_MAX_AGE 内的样本喂进去 —— obsOfStation 在该分支只看「最近一条」，
   裁剪不改变结果；有 t0 悬停窗口的站位**不裁剪**（窗口分支要窗内全部样本算中位数）。
   返回 { step(ms), n(段数), t0, counts(Uint8Array) }。 */
function obsSegments(stations, samplesOf, t0, t1, maxSeg = 1200) {
  const span = Math.max(1, t1 - t0);
  const step = Math.max(1000, Math.ceil(span / maxSeg));
  const n = Math.floor(span / step) + 1;
  const counts = new Uint8Array(n);
  const state = {};
  for (const s of stations) {
    state[s.sid] = {
      arr: samplesOf(s.sid) || [],
      lo: 0,
      // 手动绑定不受时间影响；有 t0 的走窗口分支（要全量样本）→ 这两种不裁剪
      sliding: (s.rssi === null || s.rssi === undefined)
               && (s.t0 === null || s.t0 === undefined),
    };
  }
  for (let i = 0; i < n; i++) {
    const t = t0 + i * step;
    let c = 0;
    for (const s of stations) {
      const st = state[s.sid];
      let smp = st.arr;
      if (st.sliding) {
        while (st.lo < st.arr.length && st.arr[st.lo].t < t - 2 * ROUTER_MAX_AGE_MS) st.lo++;
        smp = st.arr.slice(st.lo);
      }
      if (obsOfStation(s, smp, t)) c++;
    }
    counts[i] = Math.min(255, c);
  }
  return { step, n, t0, counts };
}

/* 时刻 → 段号（越界返回 -1） */
function segIndexAt(seg, t) {
  if (!seg) return -1;
  const i = Math.floor((t - seg.t0) / seg.step);
  return (i < 0 || i >= seg.n) ? -1 : i;
}

/* 从 t 起（含）第一个「可定位」时刻；没有则返回 null。
   minCount 默认 2 —— 1 台观测解不出位置，不算有效。 */
function nextValidAt(seg, t, minCount = 2) {
  if (!seg) return null;
  let i = Math.max(0, Math.ceil((t - seg.t0) / seg.step));
  for (; i < seg.n; i++) {
    if (seg.counts[i] >= minCount) return seg.t0 + i * seg.step;
  }
  return null;
}

/* ---------------- 报文流（原始上报帧）分析 ----------------

输入 = 回放窗内的设备上报点 `[{t(ms), rssi, seq, router_id}]`（升序，见 `tsdb.query_report_range`），
输出 = 逐帧标注 + 汇总。**为什么单独成函数、且放在这里**：页面里再写一份“算缺口”的循环
迟早与这里漂移，而序号判定有两个**真陷阱**（下面两条），必须有单测（`test_posjs.py` 用 node 跑本文件原文）：

1. **序号回退 ≠ 缺口**：`seq` 是**设备**给的计数，设备重启会从 1 重来（`seq` 也可能因乱序到达
   而变小）。只写 `seq - prev - 1` 会得出**负数**缺口（看着像丢了几千条），把真问题淹掉。
   所以：`d > 1` → 缺口 d-1；`d === 0` → 重复；`d < 0` → 回退（重启/乱序），三者分开计。
2. **没有序号就不判**：`seq` 缺失/非数字 → 该帧的 `gap/dup/reset` 全为 false，**不猜**
   （本仓“无值不给 0”的约定）。

返回 `{rows, n, gaps, gap_total, dups, resets, dt:{n, med, p95, max}}`；
`rows[i] = {t, seq, rssi, router_id, dt, gap, dup, reset}`，`dt` = 与上一帧的间隔（ms，首帧 null）。
*/
function frameGaps(points) {
  const rows = [];
  const dts = [];
  let gaps = 0, gapTotal = 0, dups = 0, resets = 0, prev = null;
  (points || []).forEach(p => {
    const r = { t: p.t, seq: p.seq, rssi: p.rssi, router_id: p.router_id || "",
                dt: null, gap: 0, dup: false, reset: false };
    if (prev && prev.t != null && p.t != null && isFinite(p.t) && isFinite(prev.t)) {
      r.dt = p.t - prev.t;
      if (r.dt >= 0) dts.push(r.dt);
    }
    // 没有序号就不判（**`null` 不能当 0**：`Number(null)===0` 会让“没值”变成一片 seq 回退，
    // 这正是本仓 `isFinite(null)` 踩过的同一类坑 —— 单测锁住了这一条）
    const hasSeq = v => v !== null && v !== undefined && v !== "" && isFinite(Number(v));
    if (prev && hasSeq(prev.seq) && hasSeq(p.seq)) {
      const d = Number(p.seq) - Number(prev.seq);
      if (d > 1) { r.gap = d - 1; gaps++; gapTotal += d - 1; }
      else if (d === 0) { r.dup = true; dups++; }
      else if (d < 0) { r.reset = true; resets++; }
    }
    rows.push(r);
    prev = p;
  });
  dts.sort((x, y) => x - y);
  const q = k => dts.length ? dts[Math.min(dts.length - 1, Math.floor(k * (dts.length - 1)))] : null;
  return { rows, n: rows.length, gaps, gap_total: gapTotal, dups, resets,
           dt: { n: dts.length, med: q(0.5), p95: q(0.95), max: dts.length ? dts[dts.length - 1] : null } };
}
