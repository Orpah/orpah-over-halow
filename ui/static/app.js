/* ORPAH L1 demo UI — 前端逻辑：SSE 报文流 + 拓扑点亮 + 计数 + 控制
 * 文案统一走 OrpahI18n（ui_i18n.js，zh/en 单一源）；URL ?lang=en 可切英文预览。
 */
"use strict";

const $ = id => document.getElementById(id);
const T = key => OrpahI18n.t(key);          // 取文案
let paused = false;
let idRevoked = false;

const STAGE_NODE = {
  client: "nodeClient", router: "nodeRouter", server: "nodeServer",
};
const STAGE_CLS = { client: "flash", router: "flash-r", server: "flash" };

/* 连接状态中文映射：机器值英文（后端 /api/status），仅展示层按字典翻译 */
const connZh = v => {
  const s = OrpahI18n.t("conn_" + v);
  return s === "conn_" + v ? (v || "?") : s;   // 字典无此键 → 回退机器值
};
/* 报文流「节点」列：后端 stage 是机器值 client/router/server → 字典 node_*（拓扑图同一套词条） */
const nodeLabel = v => {
  const s = OrpahI18n.t("node_" + v);
  return s === "node_" + v ? (v || "-") : s;   // 字典无此键 → 回退机器值
};

/* ---------- 语言应用（静态 data-i18n / title / placeholder / 文档标题） ---------- */
function applyI18n() {
  OrpahI18n.apply();
  // 按钮文案可能被状态切换，统一由 data-i18n 管理即可；这里再补 pause 态
  $("btnPause").textContent = T(paused ? "btn_resume" : "btn_pause");
}
document.addEventListener("DOMContentLoaded", applyI18n);

/* ---------- SSE 事件流 ---------- */
function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => setSrv(true);
  es.onerror = () => setSrv(false);
  es.onmessage = ev => {
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (d.type === "report") onReport(d);
    else if (d.type === "alert") alertToast(d);   // 告警**边沿**事件（服务端只在变化那一刻发）
  };
}
function setSrv(ok) {
  const b = $("srvstatus");
  b.textContent = ok ? T("srv_connected") : T("srv_disconnected");
  b.className = "badge " + (ok ? "on" : "off");
}

/* ---------- 报文事件（SSE：只做闪烁 + 触发全量刷新） ---------- */
function onReport(d) {
  const stage = d.stage;                 // client / router / server
  // 拓扑点亮对应节点
  const nid = STAGE_NODE[stage];
  const cls = STAGE_CLS[stage];
  if (nid) {
    const n = $(nid);
    n.classList.remove("flash", "flash-r");
    void n.offsetWidth;                  // 重触发动画
    n.classList.add(cls);
    setTimeout(() => n.classList.remove(cls), 900);
  }
  // 链路流动动画（方向箭头高亮：上行 client→router→server）
  // 页面上是**两段**链路（`.link-flow`：空口段 + UDP 段）→ 一次上报同时点亮两段，
  // 这是**有意**的（同一条上报确实穿过两段）；只点亮一段反而会让人以为另一段没通。
  const linkFlow = document.querySelectorAll(".link-flow");
  linkFlow.forEach(f => {
    f.classList.add("active");
    setTimeout(() => f.classList.remove("active"), 600);
  });
  // 表格以 /api/status 全量 reports 为准，拉一次即时刷新
  refresh();
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ---------- 全量状态轮询（计数 / 连接 / 表格真相） ---------- */
const seenRows = {};   // seq -> row element
const MAX_ROWS = 50;
let lostSns = new Set();   // 当前丢失态设备 SN（用于报文流标红）

function renderRows(rows) {
  const tbody = $("msgList");
  // rows 已按最新在前（后端 reversed）
  const active = new Set();
  rows.forEach(r => {
    const seq = r.seq;
    active.add(seq);
    let tr = seenRows[seq];
    if (!tr) {
      tr = document.createElement("tr");
      tr.innerHTML =
        `<td class="tm"></td><td class="sq"></td><td class="sn"></td>` +
        `<td class="rs"></td>` +
        `<td class="c">·</td><td class="r">·</td><td class="s">·</td>`;
      tbody.appendChild(tr);             // 追加（顺序由顺序数组保证）
      seenRows[seq] = tr;
    }
    tr.querySelector(".tm").textContent = r.tm || "";
    tr.querySelector(".tm").title = r.ts_src === "server" ? T("reports_ts_server") : "";
    tr.querySelector(".sq").textContent = seq;
    const snCell = tr.querySelector(".sn");
    snCell.textContent = "";
    if (r.sn) {
      const a = document.createElement("a");
      a.href = "registry.html?sn=" + encodeURIComponent(r.sn);
      a.textContent = r.sn;
      a.title = T("sn_link_title");
      a.className = lostSns.has(r.sn) ? "snlink lost" : "snlink";
      snCell.appendChild(a);
    } else {
      snCell.textContent = "-";
    }
    tr.querySelector(".rs").textContent = r.rssi != null ? r.rssi : "-";
    setCell(tr, ".c", r.client);
    setCell(tr, ".r", r.router);
    setCell(tr, ".s", r.server);
  });
  // 移除已滑出窗口的行
  Object.keys(seenRows).forEach(seq => {
    if (!active.has(Number(seq))) {
      const tr = seenRows[seq];
      if (tr) tr.remove();
      delete seenRows[seq];
    }
  });
  // 最新在顶部：reports 最新在前 → DOM 里按需把新行插到最前更直观，
  // 这里统一重排一次（保持 tbody 顺序 = rows 顺序）
  // —— 这段排序是**唯一**保证「DOM 顺序 = 数据顺序」的地方：新行是 append 到**末尾**的，
  //    而数据是最新在前，所以不排的话新行会沉到底下（清屏后重连、seq 跳变时也会错位）。
  //    实测（2026-09-13，50 行满）：0.10 ms/次轮询 —— 不值得为它改成 insertBefore 的
  //    “只插新行”写法：那会把 DOM 顺序正确性绑死在插入路径上，省下的是 0.1 ms/秒。
  [...tbody.children].sort((a, b) => {
    const sa = Number(a.querySelector(".sq").textContent);
    const sb = Number(b.querySelector(".sq").textContent);
    return sb - sa;                       // 大 seq 在前
  }).forEach(n => tbody.appendChild(n));
  while (tbody.children.length > MAX_ROWS) tbody.lastChild.remove();
}

function setCell(tr, sel, on) {
  const td = tr.querySelector(sel);
  if (!td) return;
  td.textContent = on ? "✓" : "·";
  td.className = on ? "yes" : "no";
}

/* ---------- L2：消息流面板 + 走失表 ---------- */
const MAX_FLOW = 60;
const FLOW_ZH = { "up": "↑", "down": "↓" };

function renderFlow(flow) {
  const tbody = $("flowList");
  tbody.innerHTML = "";
  flow.slice(0, MAX_FLOW).forEach(e => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${esc(e.t)}</td><td>${FLOW_ZH[e.dir] || esc(e.dir)}</td>` +
      `<td>${esc(e.type)}</td><td>${esc(e.sn)}</td>` +
      `<td>${esc(e.status === undefined ? "-" : e.status)}</td>` +
      `<td>${esc(nodeLabel(e.stage))}</td>`;
    tbody.appendChild(tr);
  });
}

function renderLost(lost) {
  const state = $("lostState");
  const sn = $("lostSn").value || $("ctlSn").value || "CN-WH01-9AF3C1D2";
  const rec = lost[sn];
  const tracked = rec && rec.tracked;
  state.textContent = tracked ? T("lost_yes") : T("lost_no");
  state.className = "hint" + (tracked ? " lost-yes" : "");
}

/* 服务器发布走失表记录（走失数据是服务器主动“下发”的，单独列出） */
const MAX_PUB = 10;

function renderFounds(founds) {
  const ul = $("foundList");
  if (!ul) return;
  ul.innerHTML = "";
  const list = founds || [];
  if (!list.length) {
    const li = document.createElement("li");
    li.className = "pub-empty";
    li.textContent = T("found_empty");
    ul.appendChild(li);
    return;
  }
  const parts = T("found_line").split("{sn}");   // [前, 后]
  list.slice(0, MAX_PUB).forEach(f => {
    const li = document.createElement("li");
    li.className = "pub-meta";
    li.innerHTML =
      `<span class="tm">${esc(f.t)}</span> ` +
      `<span class="pub-act">${esc(parts[0])}` +
      `<b class="lost-yes">${esc(f.sn || "-")}</b>${esc(parts[1] || "")}</span>`;
    ul.appendChild(li);
  });
}

function renderPublishes(publishes) {
  const ul = $("publishList");
  if (!ul) return;
  ul.innerHTML = "";
  const list = publishes || [];
  if (!list.length) {
    const li = document.createElement("li");
    li.className = "pub-empty";
    li.textContent = T("pub_empty");
    ul.appendChild(li);
    return;
  }
  list.slice(0, MAX_PUB).forEach(p => {
    const li = document.createElement("li");
    const action = T("pub_action")
      .replace("{n}", p.n).replace("{k}", p.targets);
    li.innerHTML =
      `<div class="pub-meta"><span class="tm">${esc(p.t)}</span> ` +
      `<span class="pub-act">${esc(action)}</span></div>` +
      `<ul class="pub-ents">` +
      p.entries.map(en =>
        `<li>${esc(en.sn)} = ` +
        `<b class="${en.tracked ? "lost-yes" : ""}">` +
        `${esc(en.tracked ? T("lost_yes") : T("lost_no"))}</b></li>`
      ).join("") + `</ul>`;
    ul.appendChild(li);
  });
}

/* ---------- 事件历史（IoTDB 持久化，重启后仍在） ---------- */
const evtLabel = v => {
  const s = OrpahI18n.t("evt_type_" + v);
  return s === "evt_type_" + v ? (v || "?") : s;   // 字典无此键 → 回退机器值
};

function renderEvents(rows, ok, meta) {
  const ul = $("evtList");
  if (!ul) return;
  const state = $("evtState");
  const list = rows || [];
  state.textContent = ok ? T("evt_n").replace("{n}", list.length)
                         : T("evt_offline");
  state.className = ok ? "hint" : "hint lost-yes";
  const ret = $("evtRetention");
  if (ret && meta) {      // 保留期限：由后端 tsdb.EVENT_RETENTION_DAYS 决定
    const d = meta.retention_days;
    ret.textContent = d ? T("evt_retention").replace("{n}", d)
                        : T("evt_retention_all");
  }
  ul.innerHTML = "";
  if (!list.length) {
    const li = document.createElement("li");
    li.className = "pub-empty";
    li.textContent = T("evt_empty");
    ul.appendChild(li);
    return;
  }
  list.forEach(e => {
    const li = document.createElement("li");
    li.className = "pub-meta";
    const tm = new Date(e.t || 0).toTimeString().slice(0, 8);
    // 操作者：只标人为操作（自动事件记 system，不显）
    const who = (e.actor && e.actor !== "system")
      ? ` <span class="evt-actor">${esc(e.actor)}</span>` : "";
    li.innerHTML =
      `<span class="tm">${esc(tm)}</span> ` +
      `<span class="pub-act"><b>${esc(evtLabel(e.etype))}</b>` +
      (e.sn ? ` <b class="lost-yes">${esc(e.sn)}</b>` : "") +
      (e.detail ? ` ${esc(e.detail)}` : "") + who + `</span>`;
    ul.appendChild(li);
  });
}

let evtBusy = false;

async function refreshEvents() {
  const ul = $("evtList");
  if (!ul || evtBusy) return;      // 防重入：IoTDB 慢时不堆请求
  evtBusy = true;
  const limit = Math.min(200, Math.max(5, parseInt($("evtLimit").value) || 30));
  const etype = $("evtType").value;
  try {
    const r = await fetch(`/api/ts/events?limit=${limit}` +
      (etype ? `&etype=${encodeURIComponent(etype)}` : "")).then(x => x.json());
    renderEvents(r.rows, !!r.ok, r);
  } catch (e) {
    renderEvents([], false);
  } finally {
    evtBusy = false;
  }
}

function renderId(d) {
  if (!d || !d.sn) return;
  $("idSn").textContent = d.sn;
  $("idAlg").textContent = d.alg || "-";
  $("idLevel").textContent = d.level != null ? d.level : "-";
  const t = $("idTrust");
  const ok = d.accepted;
  const trustTxt = d.trust && d.trust !== "-" ? " · " + T("trust_" + d.trust) : "";
  // 设备无时钟（ts=0）：标一句，别让人以为设备报了 1970（库里/审计用的是服务器接收时刻）
  const noClock = d.ts_src === "server" ? " · " + T("id_no_clock") : "";
  // §8.3：L2 = SE 不可用（仍更新定位）/ L3 = 只做覆盖发现（不当人员出现）—— 卡片上说清
  const deg = d.degraded ? " · " + T("id_degraded") : "";
  const cov = d.coverage_only ? " · " + T("id_coverage_only") : "";
  t.textContent = ok
    ? T("id_ok") + trustTxt + deg + cov + noClock
    : T("id_bad") + (d.error ? " · " + d.error : "") + noClock;
  t.className = ok ? "ok" : "bad";
  $("idSig").textContent = d.sig || "-";
  $("idNonce").textContent = d.nonce || "-";
}

function renderIdReports(list) {
  const tbody = $("idReportList");
  if (!tbody) return;
  tbody.innerHTML = "";
  const arr = list || [];
  if (!arr.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="6" class="pub-empty">${esc(T("id_stream_empty"))}</td>`;
    tbody.appendChild(tr);
    return;
  }
  arr.slice(0, 10).forEach(r => {
    const tr = document.createElement("tr");
    const trustTxt = r.trust && r.trust !== "-" ? T("trust_" + r.trust) : "-";
    tr.innerHTML =
      `<td>${esc(r.t)}</td><td>${esc(r.sn)}</td>` +
      `<td>${esc(r.alg)}</td><td>${esc(r.level)}</td>` +
      `<td>${esc(trustTxt)}</td>` +
      `<td class="${r.accepted ? "yes" : "no"}">${r.accepted ? "✓" : "✗ " + esc(r.error || "")}</td>`;
    tbody.appendChild(tr);
  });
}

/* ---------------- 限频（§5.8）：计数 + 刷量演示 ----------------
   参数一律读服务端（`/api/status.ratelimit.params`）——页面**不写死**限频参数，
   否则改了环境变量会出现“页面说的与服务器做的不一致”。*/
let rlBusy = false;
let rlP = {};                     // 最近一次 /api/status 里的限频参数（刷量条数按它算）
function renderRatelimit(rl, rtr, sl) {
  if (!rl) return;
  const p = rl.params || {};
  rlP = p;
  $("rlState").textContent = rl.on
    ? T("rl_on")
    : T("rl_off");
  $("rlState").className = rl.on ? "ok" : "";
  $("rlParams").textContent = T("rl_params_fmt")
    .replace("{a}", `${p.sn_burst}/${p.sn_rate}`)
    .replace("{b}", `${p.router_burst}/${p.router_rate}`);
  $("rlAllowed").textContent = rl.allowed;
  const d = rl.dropped || {};
  $("rlDropped").textContent = T("rl_drop_fmt")
    .replace("{t}", d.total || 0).replace("{s}", d.sn || 0)
    .replace("{r}", d.router || 0);
  $("rlDropped").className = (d.total || 0) > 0 ? "bad" : "ok";
  // Router 侧（§5.8 的行 1/2）：与 Server 侧**分开显示** —— 参数不同、丢的后果也不同
  // （砍带宽 vs 砍 CPU），合成一个数就说不清“报文死在哪一段”了。
  const rp = (rtr && rtr.params) || {};
  $("rlRtrParams").textContent = T("rl_params_fmt_rtr")
    .replace("{a}", `${rp.sn_burst}/${rp.sn_rate}`)
    .replace("{b}", `${rp.router_burst}/${rp.router_rate}`);
  const rd = (rtr && rtr.dropped) || {};
  $("rlRtrDropped").textContent = T("rl_drop_fmt").replace("{t}", rd.total || 0)
    .replace("{s}", rd.sn || 0).replace("{r}", rd.router || 0);
  $("rlRtrDropped").className = (rd.total || 0) > 0 ? "bad" : "ok";
  // 设备侧（§5.8 设备那一环）：**自愿**自限频，不是防线。文案必须用「延后」——
  // 写「丢弃」会让人以为漏报了（延后的那些下一拍还会发）。
  const sp = (sl && sl.params) || {};
  $("slState").textContent = sl && sl.on ? T("sl_on") : T("sl_off");
  $("slState").className = sl && sl.on ? "ok" : "";
  $("slParams").textContent = T("sl_params_fmt")
    .replace("{a}", sp.burst).replace("{b}", sp.min_interval);
  $("slHeld").textContent = T("sl_held_fmt").replace("{n}", (sl && sl.held) || 0);
  $("slHeld").className = (sl && sl.held) > 0 ? "no" : "ok";
  const tbody = $("rlRecent");
  if (!tbody) return;
  tbody.innerHTML = "";
  // 两侧的最近丢弃合到一张表（按侧着色文字），否则“到底是谁丢的”就看不见了。
  // 合并后要**按时间重排**：两边各只留最近 5 条，直接拼起来会把另一侧较新的挤掉
  // （表头写的是“最新在前”，就得真的是最新在前）。t 是 HH:MM:SS，同日可直接比字符串。
  const rows = (rl.recent || []).map(r => Object.assign({ side: "server" }, r))
    .concat((rtr && rtr.recent ? rtr.recent : []).map(r => Object.assign({}, r)))
    .concat(((sl && sl.recent) || []).map(r => Object.assign({}, r)))
    .sort((a, b) => String(b.t || "").localeCompare(String(a.t || "")))
    .slice(0, 8);
  if (!rows.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="7" class="pub-empty">${esc(T("rl_recent_empty"))}</td>`;
    tbody.appendChild(tr);
    return;
  }
  rows.forEach(r => {
    const tr = document.createElement("tr");
    const which = r.which === "router" ? T("rl_which_router")
      : (r.which === "interval" ? T("sl_which_interval") : T("rl_which_sn"));
    const side = r.side === "router" ? T("rl_side_router")
      : (r.side === "client" ? T("sl_side") : T("rl_side_server"));
    tr.innerHTML =
      `<td>${esc(r.t || "")}</td><td class="no">${esc(side)}</td>` +
      `<td>${esc(r.kind || r.mtype || "-")}</td>` +
      `<td>${esc(r.sn || "-")}</td><td>${esc(r.router || "-")}</td>` +
      `<td class="no">${esc(which)}</td>` +
      `<td>${r.retry_after == null ? "-" : esc(String(r.retry_after))}</td>`;
    tbody.appendChild(tr);
  });
}

function rlMsg(txt, cls) {
  const el = $("rlMsg");
  if (!el) return;
  el.textContent = txt;
  el.className = cls ? "hint " + cls : "hint";
}

async function rlFlood(btn, n, rotate) {
  if (rlBusy) return;
  rlBusy = true;
  const all = ["btnRlFlood", "btnRlRotate", "btnRlReset"];
  all.forEach(id => { if ($(id)) $(id).disabled = true; });
  rlMsg(T(rotate ? "rl_running_rotate" : "rl_running").replace("{n}", n));
  try {
    const r = await fetch("/api/ctl", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "flood", n: n, rotate: rotate }),
    }).then(x => x.json());
    if (!r || r.ok === false && r.sent === undefined) {
      rlMsg(T("rl_fail") + (r && r.err ? r.err : ""), "err");
    } else {
      // 数字含同一时刻的周期报文（同一 SN 的 L2 REPORT 也会被限）；
      // Router 侧丢的不会到 Server → 两边分开报（否则加起来对不上“发了多少条”）。
      rlMsg(T(rotate ? "rl_done_rotate" : "rl_done")
        .replace("{n}", r.sent).replace("{a}", r.accepted).replace("{d}", r.dropped)
        .replace("{dr}", r.dropped_router == null ? 0 : r.dropped_router)
        + (r.ok ? "" : " " + T("rl_timeout")), r.dropped > 0 ? "" : "err");
    }
  } catch (e) {
    rlMsg(T("rl_fail") + e, "err");
  } finally {
    all.forEach(id => { if ($(id)) $(id).disabled = false; });
    rlBusy = false;
  }
}

async function refresh() {
  try {
    const r = await fetch("/api/status");
    const s = await r.json();
    /* 数据新鲜度（UI ④，2026-09-13）：先记“本轮轮询成功”，再拿服务端的 `fresh` 渲染。
       顺序不能反 —— 否则渲染出来的“本页轮询”还悬着上一轮的失败计数。 */
    freshPollOk();
    freshRender($("freshBox"), s.fresh, T);
    $("snClient").textContent = s.sn;
    // 计数行（字典 fmt，数字高亮）。**按内容分色**：同一颜色 = 同一类内容，
    // 蓝 = L2（REQ-CONNECT/REPORT）、紫 = Orpah ID 签名上报、黄 = 发现（ORPAH-FOUND）。
    // 口径（为什么会看到不同数字，全部在此说明，避免"看起来不一致"）：
    //   · 客户端「上行注入」= client_sent = REQ-CONNECT + REPORT（每周期 2 条）
    //   · 客户端「ID 上报」= id_sent = 本机注入的 ID-REPORT（含页面注入的重放/超窗/伪造报文）
    //   · 空口帧 = tx_sta = 上面两者之和（STA 发出的**数据**帧；不含信标/关联帧）
    //   · 转发 REPORT / 转发 ID = 路由器真正发给 Server 的两种内容（REQ-CONNECT 本机应答不过 UDP）
    //   · 服务器「收到」只计 REPORT；ID 走验签通道单独计（含被拒，供防 spoof 演示）
    $("rowClientCnt").innerHTML =
      T("lbl_sent").replace("{n}", `<b class="cnt">${s.client_sent}</b>`);
    $("rowClientCnt").title = T("lbl_sent_tip");
    $("rowClientId").innerHTML =
      T("lbl_id_up").replace("{n}", `<b class="cnt-id">${s.id_sent || 0}</b>`);
    $("rowRouterCnt").innerHTML =
      T("lbl_up").replace("{n}", `<b class="cnt">${s.router_up}</b>`);
    $("rowRouterId").innerHTML =
      T("lbl_id_fwd").replace("{n}", `<b class="cnt-id">${s.router_id_up || 0}</b>`);
    $("rowRouterLost").innerHTML =
      T("lbl_lost_recv").replace("{n}", `<b class="cnt">${s.router_lost_recv || 0}</b>`);
    // 下行来源校验丢掉的数量（A 方案）：非 0 = 有东西不是从 Server 地址发包 → 显式可见
    $("rowRouterRej").innerHTML =
      T("lbl_down_rej").replace("{n}",
        `<b class="${(s.router_down_rejected || 0) > 0 ? "cnt-bad" : "cnt"}">${s.router_down_rejected || 0}</b>`);
    $("rowRouterRej").title = T("lbl_down_rej_tip");
    // 下行**真实性**（B 方案）：配了公钥才真在验；没配就如实写「未启用」，
    // 不把“只做了来源校验（A）”读成“已防住”。拒收条数非 0 时标红。
    const ds = (s.downlink && s.downlink.router) || {};
    $("rowRouterSig").innerHTML = ds.on
      ? T("lbl_down_sig").replace("{n}", `<b class="cnt">${ds.verified || 0}</b>`) +
        ((ds.failed || 0) > 0
          ? " " + T("lbl_down_sig_fail").replace("{n}", `<b class="cnt-bad">${ds.failed}</b>`)
          : "")
      : T("lbl_down_sig_off");
    $("rowRouterSig").title = ds.on ? T("lbl_down_sig_tip")
                                    : T("lbl_down_sig_off_tip");
    $("rowRouterFound").innerHTML =
      T("lbl_found").replace("{n}", `<b class="cnt-found">${s.found_total || 0}</b>`);
    $("rowServerCnt").innerHTML =
      T("lbl_recv").replace("{n}", `<b class="cnt">${s.server_recv}</b>`);
    $("rowServerId").innerHTML =
      T("lbl_id_recv").replace("{n}", `<b class="cnt-id">${s.id_report_total || 0}</b>`);
    $("rowServerPub").innerHTML =
      T("lbl_pub").replace("{n}", `<b class="cnt">${s.publish_total || 0}</b>`);
    $("rowServerFound").innerHTML =
      T("lbl_found_recv").replace("{n}", `<b class="cnt-found">${s.found_recv || 0}</b>`);
    // 两段链路：总数的口径见下 + 「按内容」拆行（颜色与节点行一致）
    //   空口帧 = L2 注入 + ID 注入（= STA 发出的数据帧总数，所以两项相加等于总数）
    //   UDP 帧 = 转发 REPORT + 转发 ID + 发现（= 路由器真正发给 Server 的帧总数；
    //            不含控制面：REQ-CONNECT 本机应答、LOST-TABLE 拉表/推送）
    const found = s.found_total || 0;
    $("airFrames").textContent = s.tx_sta;
    $("airSplit").innerHTML =
      `<span class="s-l2">L2 <b>${s.client_sent}</b></span> · ` +
      `<span class="s-id">ID <b>${s.id_sent || 0}</b></span>`;
    $("udpFrames").textContent = s.router_up + (s.router_id_up || 0) + found;
    $("udpSplit").innerHTML =
      `<span class="s-l2">REPORT <b>${s.router_up}</b></span> · ` +
      `<span class="s-id">ID <b>${s.router_id_up || 0}</b></span> · ` +
      `<span class="s-found">${T("sp_found")} <b>${found}</b></span>`;
    // 连接 / 徽标（展示层按字典中文化）
    const setConn = (elId, val) => {
      const el = $(elId);
      el.textContent = connZh(val);
      el.className = el.className.split(" ")[0] + " " +
        (val === "CONNECTED" ? "ok" : "");
    };
    setConn("chipClient", s.conn_b);
    setConn("chipRouter", s.conn_a);
    $("chipServer").textContent = s.server_recv > 0 ? T("chip_run") : T("chip_listen");
    $("chipServer").className = "chip" + (s.server_recv > 0 ? " ok" : "");
    // 报文流表格（全量真相）
    lostSns = new Set(s.lost_sns || []);
    // 时间列：设备无时钟（ts=0/缺失，见 orpah_proto.effective_ts）→ 显示**服务器接收时刻**并加 *，
    // 与库里真正用的时间一致（否则界面显示 1970、库里却是现在，对不上）
    const hhmmss = v => new Date((v || 0) * 1000).toTimeString().slice(0, 8);
    const rows = (s.reports || []).map(x => ({
      ...x,
      tm: x.ts ? hhmmss(x.ts) : (x.ts_eff ? hhmmss(x.ts_eff) + "*" : "--"),
    }));
    renderRows(rows);
    // L2：消息流面板 + 走失表状态 + 服务器发布记录 + 发现记录
    renderFlow(s.flow || []);
    renderLost(s.lost || {});
    renderPublishes(s.publishes || []);
    renderFounds(s.founds || []);
    renderId(s.id_demo || {});
    renderIdReports(s.id_reports || []);
    renderRatelimit(s.ratelimit || {}, s.ratelimit_rtr || {}, s.selflimit || {});
    fillSpoofKinds(s.spoof_kinds || []);
    spoofKinds = s.spoof_kinds || spoofKinds;
    // §8.2 降级演示下拉：选项来自 /api/status（单一源），选中值回显当前模式
    fillIdLevelModes(s.id_level_modes || []);
    const selLv = $("idLevelMode");
    if (selLv && document.activeElement !== selLv && s.id_level) selLv.value = s.id_level;
    lastIdNonce = (s.id_demo && s.id_demo.nonce) || lastIdNonce;
    spoofCompare(s.id_demo || {});
    idRevoked = !!s.id_revoked;
    const btnRev = $("btnIdRevoke");
    if (btnRev) btnRev.textContent = T(idRevoked ? "btn_unrevoke" : "btn_revoke");
    // 控制面板回显
    if (!document.activeElement || document.activeElement.id !== "ctlSn")
      $("ctlSn").value = s.sn;
    if (!document.activeElement || document.activeElement.id !== "lostSn")
      $("lostSn").value = s.sn;
    // 设备时钟偏移/漂移估计（clock.py）：只**估计**，不改记录时间语义（§5.5）
    if (!document.activeElement || document.activeElement.id !== "ctlClock")
      $("ctlClock").value = s.clock_off || 0;
    // 设备能力声明（2026-09-13）：null=未声明 / true=有 RTC / false=无 RTC
    const selCap = $("ctlCap");
    if (selCap && document.activeElement !== selCap)
      selCap.value = s.id_cap_rtc === true ? "yes" : (s.id_cap_rtc === false ? "no" : "none");
    const cbTs = $("ctlTsBroken");
    if (cbTs && document.activeElement !== cbTs) cbTs.checked = !!s.id_ts_broken;
    const ci = $("clockInfo");
    if (ci) {
      const est = (s.clock || {})[s.sn];
      const off = s.clock_off || 0;
      const why = (est && est.drift_ppm == null && est.drift_why)
        ? T("clock_why_" + est.drift_why) : "";
      const idr = (s.id_demo || {});
      // 上报卡上先说清“时间是谁给的”：无 RTC / ts 置 0 时设备没有可用时钟（§5.5）
      const capTxt = s.id_cap_rtc === false ? T("cap_rtc_no")
        : (s.id_cap_rtc === true ? T("cap_rtc_yes") : T("cap_none"));
      const tsTxt = (s.id_ts_broken || idr.ts_src === "server")
        ? " · " + T("cap_ts_server").replace("{cap}", capTxt) : " · " + capTxt;
      ci.textContent = (est && est.ok)
        ? T("clock_info")
            .replace("{off}", (est.offset >= 0 ? "+" : "") + est.offset.toFixed(1))
            .replace("{drift}", est.drift_ppm == null ? "—" : (est.drift_ppm >= 0 ? "+" : "") + est.drift_ppm.toFixed(0))
            .replace("{why}", why)
            .replace("{n}", est.n)
          + (off ? "  ·  " + T("clock_demo_on").replace("{v}", off) : "")
          + tsTxt
        : T("clock_none") + tsTxt;
    }
    renderEnergy(s.energy, s.energy_axis);
  } catch (e) {
    /* 服务器未就绪 / 网络断：**这里必须把失败显现出来**（UI ④）——
       以前是彻底静默：页面上的数字保持最后一次的值，看着“稳定”，
       但读者没有任何线索知道它已经不再更新了。 */
    freshPollFail();
    freshRender($("freshBox"), null, T);
  }
}

/* ---------- 控制按钮 ---------- */
function postCtl(body) {
  return fetch("/api/ctl", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).catch(e => console.error("postCtl 请求失败:", e));
}

$("btnPause").onclick = async () => {
  paused = !paused;
  await postCtl({ action: paused ? "pause" : "resume" });
  $("btnPause").textContent = T(paused ? "btn_resume" : "btn_pause");
};
$("btnReset").onclick = () => {
  Object.keys(seenRows).forEach(k => delete seenRows[k]);
  $("msgList").innerHTML = "";
};
$("btnApply").onclick = async () => {
  await postCtl({
    action: "every", every: parseFloat($("ctlEvery").value) || 2,
  });
  await postCtl({ action: "set_sn", sn: $("ctlSn").value || "CN-WH01-9AF3C1D2" });
  await postCtl({ action: "clock_off", sec: parseFloat($("ctlClock").value) || 0 });
  // 能力声明：none → 不声明（null），yes/no → true/false
  const cap = $("ctlCap").value;
  await postCtl({ action: "cap", rtc: cap === "yes" ? true : (cap === "no" ? false : null) });
  await postCtl({ action: "ts_broken", on: !!$("ctlTsBroken").checked });
};
$("btnMark").onclick = async () => {
  const sn = $("lostSn").value || $("ctlSn").value || "CN-WH01-9AF3C1D2";
  await postCtl({ action: "mark", sn });
};
$("btnUntrack").onclick = async () => {
  const sn = $("lostSn").value || $("ctlSn").value || "CN-WH01-9AF3C1D2";
  await postCtl({ action: "untrack", sn });
};
$("btnIdRevoke").onclick = async () => {
  await postCtl({ action: idRevoked ? "unrevoke" : "revoke" });
};
$("btnIdReplay").onclick = async () => {
  await postCtl({ action: "replay" });
};
$("btnIdStale").onclick = async () => {
  await postCtl({ action: "stale" });
};

/* ---------- 能量轴（免电池客户端）----------

   模型在 energy.py（三参数：采集 P / 储能 C / 每次上报代价 cost），这里只做呈现：
   · 状态格：电量 mV、级别、间隔、还能撑多久、净余、原因（`why` 是机器值，字典翻译）；
   · 扫描表：横轴 = 采集功率 → 纵轴 = 可持续的上报间隔（这就是「能量轴」的名字由来）；
   · 头条：`min_harvest_mw` = 少于此功率就跟不住人（要≥0.1mW 才每 300s 报一次）。

   页面操作**不改模型**：参数只是喂给 `energy.plan()` 的输入，判定恒由后端算（单一源）。 */

let enBusy = false;

const EN_WHY = w => {
  const s = T("en_why_" + w);
  return s === "en_why_" + w ? (w || "-") : s;
};

function enNum(v, digits) {
  return (v == null || !isFinite(v)) ? "—" : Number(v).toFixed(digits == null ? 2 : digits);
}

function fmtInterval(v) {
  return v == null ? T("en_silent") : enNum(v, 1) + " s";
}

function fmtSilence(v) {
  if (v == null) return "—";
  if (v >= 86400) return enNum(v / 86400, 1) + " " + T("en_day");
  if (v >= 3600) return enNum(v / 3600, 1) + " " + T("en_hour");
  return enNum(v, 0) + " s";
}

function renderEnergy(e, ax) {
  const box = $("enState");
  if (!box) return;
  const on = !!(e && e.on);
  const st = (e && e.state) || {};
  const mv = st.mv;
  const cell = (k, v, cls) =>
    `<div class="id-row"><span>${esc(T(k))}</span><b class="${cls || ""}">${v}</b></div>`;
  if (!on) {
    box.innerHTML = cell("en_st_state", esc(T("en_state_off"))) +
      cell("en_st_mv", "—") + cell("en_st_level", "—") +
      cell("en_st_interval", "—") + cell("en_st_silence", "—");
  } else {
    // 电站「还有多少」= 储能 × 电压映射；沉默倒计时是**演示加速后**的直观秒数
    const lv = mv == null ? "—" : mv + " mV";
    const lvCls = (st.mv != null && st.why === "empty") ? "lost-yes" : "";
    let sTxt = "—";
    if (st.silent) sTxt = T("en_silent_now");
    else if (st.hard) sTxt = T("en_hard_now") + " (" + fmtSilence(st.silence_eta_s) + ")";
    else if (st.silence_in_s != null) sTxt = fmtSilence(st.silence_eta_s);
    box.innerHTML =
      cell("en_st_state", esc(T("en_state_on"))) +
      cell("en_st_mv", lv, lvCls) +
      cell("en_st_level", esc(st.level || "—") +
           (st.degraded ? ` <span class="hint">${esc(T("en_degraded"))}</span>` : "")) +
      cell("en_st_interval", esc(fmtInterval(st.interval_s))) +
      cell("en_st_silence", esc(sTxt)) +
      cell("en_st_net", enNum(st.net_mw, 3) + " mW") +
      cell("en_st_why", esc(EN_WHY(st.why)));
  }
  // 输入框回显（不动正在编辑的那个）
  const ae = document.activeElement && document.activeElement.id;
  const p = (e && e.params) || {};
  if (ae !== "enHarvest" && p.harvest_mw != null) $("enHarvest").value = p.harvest_mw;
  if (ae !== "enCharge" && p.charge_mj != null) $("enCharge").value = p.charge_mj;
  if (ae !== "enStore" && p.store_mj != null) $("enStore").value = p.store_mj;
  if (ae !== "enSpeedup" && p.speedup != null) $("enSpeedup").value = p.speedup;
  const cbOn = $("enOn");
  if (cbOn && document.activeElement !== cbOn) cbOn.checked = on;
  const cbPush = $("enPush");
  if (cbPush && document.activeElement !== cbPush) cbPush.checked = !!p.push;

  // 扫描表 + 头条
  const msg = $("enAxisMsg");
  const tb = $("enAxisList");
  if (!tb) return;
  const rows = (ax && ax.rows) || [];
  tb.innerHTML = "";
  rows.forEach(r => {
    const tr = document.createElement("tr");
    const cur = (e && e.params && Math.abs(r.harvest_mw - e.params.harvest_mw) < 1e-9);
    if (cur) tr.className = "en-cur";
    const usable = !!r.usable;
    tr.innerHTML =
      `<td>${enNum(r.harvest_mw, 3)}${cur ? " ◀" : ""}</td>` +
      `<td>${enNum(r.net_mw, 3)}</td>` +
      `<td class="${usable ? "yes" : "no"}">${esc(r.level || "—")}` +
      `${r.degraded ? ` <span class="hint">${esc(T("en_degraded"))}</span>` : ""}</td>` +
      `<td>${esc(fmtInterval(r.interval_s))}` +
      `${r.every_s != null ? ` <span class="hint">(${enNum(r.every_s, 1)}s)</span>` : ""}</td>` +
      `<td>${esc(fmtSilence(r.silence_in_s))}</td>` +
      `<td>${esc(EN_WHY(r.why))}</td>`;
    tb.appendChild(tr);
  });
  if (msg) {
    msg.textContent = (ax && ax.min_harvest_mw != null)
      ? T("en_axis_head").replace("{n}", enNum(ax.min_harvest_mw, 2))
          .replace("{n2}", enNum(ax.n, 0))
      : T("en_axis_none");
  }
}

async function postEnergy(body) {
  if (enBusy) return;                     // 防重入：连点不堆请求
  enBusy = true;
  try {
    const r = await fetch("/api/energy", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(x => x.json());
    if (r && r.axis) renderEnergy(r, r.axis);   // 立刻回显，不等下一次轮询
  } catch (err) {
    console.error("postEnergy 失败:", err);
  } finally {
    enBusy = false;
  }
}

if ($("btnEnApply")) {
  $("btnEnApply").onclick = () => postEnergy({
    action: "set",
    on: !!$("enOn").checked,
    harvest_mw: parseFloat($("enHarvest").value) || 0,
    charge_mj: parseFloat($("enCharge").value) || 0,
    store_mj: parseFloat($("enStore").value) || 0,
    push: !!$("enPush").checked,
    speedup: parseFloat($("enSpeedup").value) || 1,
  });
  $("btnEnReset").onclick = () => postEnergy({ action: "reset" });
  $("enOn").onchange = () => postEnergy({ action: $("enOn").checked ? "on" : "off" });
}

/* ---------- §8.2 降级演示：切换「哪个环节坏了」---------- */
function fillIdLevelModes(list) {
  const sel = $("idLevelMode");
  if (!sel || !list || !list.length) return;
  const sig = list.join("|");
  if (sel.dataset.sig === sig && OrpahI18n.lang === sel.dataset.lang) return;
  const keep = sel.value;
  sel.innerHTML = list.map(k =>
    `<option value="${esc(k)}">${esc(T("id_lv_" + k))}</option>`).join("");
  sel.dataset.sig = sig;
  sel.dataset.lang = OrpahI18n.lang;
  if (keep && list.includes(keep)) sel.value = keep;
}
if ($("idLevelMode")) {
  $("idLevelMode").onchange = async () => {
    await postCtl({ action: "id_level", level: $("idLevelMode").value });
  };
}

/* ---------- 防 spoof：把攻击报文真的注入空口，看服务器怎么拦 ---------- */
let spoofKinds = [];        // /api/status.spoof_kinds（脚本/UI 同一份，见 spoof.py）
let spoofRunning = false;
let lastIdNonce = "";       // 最近一条验签结果的 nonce（用来等"这条攻击的结果到了"）

function spoofOptText(o) {
  const name = OrpahI18n.lang === "en" ? (o.en || o.kind) : (o.zh || o.kind);
  return name + "  →  " + (o.expect || T("spoof_accept"));
}

function fillSpoofKinds(list) {
  const sel = $("idSpoofKind");
  if (!sel || !list || !list.length) return;
  const sig = list.map(o => o.kind).join("|");
  if (sel.dataset.sig === sig && OrpahI18n.lang === sel.dataset.lang) return;
  const keep = sel.value;
  sel.innerHTML = list.map(o =>
    `<option value="${esc(o.kind)}">${esc(spoofOptText(o))}</option>`).join("");
  sel.dataset.sig = sig;
  sel.dataset.lang = OrpahI18n.lang;
  if (keep && list.some(o => o.kind === keep)) sel.value = keep;
}

function spoofMsg(html, cls) {
  const el = $("spoofMsg");
  if (!el) return;
  el.innerHTML = html;
  el.className = "hint" + (cls ? " " + cls : "");
}

async function spoofOne(kind) {
  const r = await postCtl({ action: "spoof", kind });
  let info = {};
  try { info = await r.json(); } catch (e) { /* ignore */ }
  if (!info.ok) { spoofMsg(T("spoof_fail") + (info.err || ""), "err"); return null; }
  const o = spoofKinds.find(x => x.kind === kind) || info;
  const name = OrpahI18n.lang === "en" ? (o.en || kind) : (o.zh || kind);
  spoofMsg(T("spoof_sent").replace("{k}", esc(name))
    .replace("{e}", esc(info.expect || T("spoof_accept"))) + " …");
  // 验签结果由 server 异步回（经真链路），等下一轮 id_demo 更新后再比对
  return { kind, name, expect: info.expect, note: info.note };
}

/* 把"期望 vs 实际"写进结果行；由 refresh() 在 id_demo 更新时调 */
let spoofPending = null;
function spoofCompare(d) {
  if (!spoofPending || !d || d.sn === undefined) return;
  const got = d.accepted ? null : (d.error || "?");
  const ok = got === spoofPending.expect;
  const mark = ok ? "✓" : "✗";
  const tn = t => (t == null ? T("spoof_accept") : t);
  spoofMsg(T("spoof_res").replace("{k}", esc(spoofPending.name))
    .replace("{e}", esc(tn(spoofPending.expect)))
    .replace("{g}", esc(tn(got)))
    .replace("{m}", mark), ok ? "" : "err");
  spoofPending = null;
}

$("btnIdSpoof").onclick = async () => {
  const kind = $("idSpoofKind").value;
  if (!kind) return;
  const p = await spoofOne(kind);
  if (p) spoofPending = p;
};

$("btnIdSpoofAll").onclick = async () => {
  if (spoofRunning) return;
  spoofRunning = true;
  $("btnIdSpoofAll").disabled = true;
  $("btnIdSpoofStop").style.display = "";
  const list = spoofKinds.slice();
  const hits = [];
  for (let i = 0; i < list.length && spoofRunning; i++) {
    const p = await spoofOne(list[i].kind);
    if (!p) continue;
    hits.push(p);
    // 等该条的验签结果（id_demo 的 nonce 变化 = 新结果到了）
    const t0 = Date.now();
    let last = lastIdNonce;
    while (Date.now() - t0 < 3000 && lastIdNonce === last) {
      await new Promise(res => setTimeout(res, 120));
    }
    spoofPending = null;
    await new Promise(res => setTimeout(res, 150));
  }
  spoofRunning = false;
  $("btnIdSpoofAll").disabled = false;
  $("btnIdSpoofStop").style.display = "none";
  const n = hits.length;
  spoofMsg(T("spoof_all_done").replace("{n}", n)
    + " " + T("spoof_all_hint"), "");
};
$("btnIdSpoofStop").onclick = () => { spoofRunning = false; };

/* 限频：刷量演示（走真链路；请求会阻塞到发完+服务端处理完）。
   条数**按服务端参数算**：同一 SN 取 3×桶容量（保证明显超限），轮换取
   per-Router 桶容量 +20（一定要超它，否则一条都不丢、什么也证明不了）。 */
if ($("btnRlFlood")) {
  $("btnRlFlood").onclick = () => rlFlood($("btnRlFlood"),
    Math.max(60, Math.round((rlP.sn_burst || 20) * 3)), false);
  $("btnRlRotate").onclick = () => rlFlood($("btnRlRotate"),
    Math.round((rlP.router_burst || 60) + 20), true);
  $("btnRlReset").onclick = async () => {
    await fetch("/api/ctl", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "rl_reset" }),
    });
    rlMsg(T("rl_reset_done"));
  };
}

applyI18n();               // 本文件在 </body> 前加载，DOM 已就绪，直接应用
connect();
/* refresh() 由两处触发：1s 轮询 + SSE 每来一条上报（onReport 末尾拉一次即时刷新）
   → 峰值约 2.5 次/秒。**故意没加防重入**（`refreshBusy`）：实测 `/api/status`
   中位 4.2 ms、最大 5.5 ms（本机 25 次），而 `ui_server.status()` 只读内存快照
   （**不做 IoTDB 查询**），离 1 s 间隔有 200 倍余量 —— 加标志只会带来“被跳过的那一刷新
   要不要补”的新问题（漏刷新 = 页面数字停住不更新，比多几次请求更难发现）。
   ⇒ **将来若给 status() 加了 IO/慢查询，必须在这里补防重入**（忙时置 pending、忙完补一次），
   否则请求会堆积且旧响应可能后到（数字瞬时回退）。 */
/* 本页轮询节拍 = 1s（上面的 setInterval）——「数据新鲜度」按 2.5×/6× 这个节拍判滞后/停摆，
   所以节拍必须在这里声明（写死成别的值会让状态判断与真实轮询对不上）。 */
freshPollArm(1);
setInterval(refresh, 1000);
refresh();

/* 事件历史：初始化拉一次 + 每 5s 刷新（IoTDB 查询很轻，不跟 1s 轮询） */
if ($("btnEvtRefresh")) {
  $("btnEvtRefresh").onclick = refreshEvents;
  $("evtType").onchange = refreshEvents;
  $("evtLimit").onchange = refreshEvents;
  setInterval(refreshEvents, 5000);
  refreshEvents();
}

/* ---------- 告警（规则引擎在 alerts.py；本页只轮询 + 渲染） ----------
   后端只回 {kind, level, key, msg(i18n 键), since, ...数据} —— 文案由本页本地化，
   免得又变成"后端文案不跟语言走"。
   红点用轮询（3s）而不是 SSE：比 1s 的 /api/status 慢一档，规则评估很轻。 */
let alertKeys = new Set();     // 上一轮的告警 key：用来判断"新告警"

/* 持续时长：中文用 天/小时/分钟/秒，英文用 d/h/m/s（单位走字典，不写死文案）。
   最多两段（有更大单位就截到下一级），与常见「只在需要时才细分」习惯一致。 */
function fmtGap(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600),
        m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const vals = { d: d, h: h, m: m, s: s };
  let out = T(d ? "dur_dh" : (h ? "dur_hm" : (m ? "dur_ms" : "dur_s")));
  Object.keys(vals).forEach(k => { out = out.split("{" + k + "}").join(String(vals[k])); });
  return out;
}

/* 用告警自带字段填 msg 模板 */
function alertText(a) {
  const data = Object.assign({}, a);
  delete data.msg;
  if (data.gap !== undefined) data.gap = fmtGap(data.gap);
  let s = T(a.msg);
  Object.keys(data).forEach(k => { s = s.split("{" + k + "}").join(String(data[k])); });
  return s;
}

function renderAlerts(r) {
  const badge = $("alertBadge"), card = $("alertCard"), ul = $("alertList");
  if (!badge || !card || !ul) return;
  const list = r.alerts || [], counts = r.counts || {}, n = counts.total || 0;
  badge.hidden = !n;
  badge.textContent = n ? "⚠ " + n : "";
  badge.className = "badge" + (counts.crit ? " crit" : (n ? " warn" : ""));
  card.hidden = !n;
  ul.innerHTML = "";
  list.forEach(a => {
    const li = document.createElement("li");
    li.className = a.level;
    const age = fmtGap(Date.now() / 1000 - a.since);
    li.innerHTML = esc(alertText(a)) +
      ' <span class="since">· ' + esc(T("alert_age").replace("{v}", age)) + "</span>";
    ul.appendChild(li);
  });
  // 出现"上一轮没有的告警"→ 徽标闪一下（比弹窗轻，不打断演示）
  const keys = new Set(list.map(a => a.key));
  const isNew = n > 0 && [...keys].some(k => !alertKeys.has(k));
  alertKeys = keys;
  if (isNew) {
    badge.style.boxShadow = "0 0 10px 2px currentColor";
    setTimeout(() => { badge.style.boxShadow = ""; }, 1500);
  }
  renderNotify(r);            // 通知卡 + 阈值（同一份 /api/alerts 响应，不额外请求）
}

/* ---------- 告警通知卡（配置 + 计数 + 最近投递）与当前阈值 ----------
   阈值快照来自服务端（alerts.thresholds()）—— 这里**只显示**，数值一个都不写死；
   连“该显示哪几条”也来自服务端（THRESHOLDS 元信息），页面不维护第二份清单。 */
/* 字典里没这个键 → 回退到机器值（源码中其它地方同写法，见 connZh/nodeLabel） */
const i18nOr = (key, machine) => {
  const s = OrpahI18n.t(key);
  return s === key ? (machine || "") : s;
};

function notifyStateText(rec) {
  if (rec.err === "off") return T("notify_st_off");
  /* 重试过的投递要看得出来是第几次 —— 否则「成功了」看不出它其实是第三次才成功 */
  const nth = (rec.tries > 1) ? "　" + T("notify_attempts").replace("{n}", rec.tries) : "";
  if (rec.ok === true) return T("notify_st_ok") + nth;
  if (rec.ok === false) return T("notify_st_fail") +
    (rec.status ? " " + rec.status : "") + nth;
  return "—";
}

function renderNotify(r) {
  const nf = r.notify, th = r.thresholds;
  if (!nf || !$("notifyState")) return;
  // 输入框只在用户没在编辑时回填（否则 1s 轮询会把正在输入的内容冲掉）
  const url = $("notifyUrl");
  if (url && document.activeElement !== url) url.value = nf.url || "";
  if ($("notifyLevel")) $("notifyLevel").value = nf.min_level || "warn";
  const st = $("notifyState");
  st.textContent = nf.on ? T("notify_on") : T("notify_off");
  st.className = nf.on ? "ok" : "";
  $("notifySent").textContent = nf.sent;
  $("notifyFailed").textContent = nf.failed;
  $("notifySkipped").textContent = nf.skipped;
  $("notifyWatch").textContent = nf.watching;
  /* 重试态（至少一次语义，见 notify.py）：队列里几条 / 还要等多久 / 已重试几次 / 主动丢了几条。
     「下次重试」跟着服务端算好的 next_in（不在页面自己推时间），没有队列就是 — */
  const rt = nf.retry || {};
  if ($("notifyQueued")) {
    $("notifyQueued").textContent = rt.attempts != null
      ? `${rt.queued || 0} / 最多再试 ${rt.attempts} 次` : (rt.queued || 0);
    $("notifyNext").textContent = (rt.next_in == null) ? "—"
      : rt.next_in + " s" + (rt.next_key ? " · " + rt.next_key : "");
    $("notifyRetried").textContent = rt.retried || 0;
    $("notifyDropped").textContent = rt.dropped || 0;
  }
  $("notifyErr").textContent = nf.last_error || "—";
  $("notifyRecent").innerHTML = (nf.recent || []).map(x => {
    const ev = i18nOr("notify_ev_" + x.event, x.event);
    const err = x.err === "off" ? T("notify_st_off") : (x.err || "—");
    return `<tr><td>${esc(x.t || "—")}</td><td>${esc(ev)}</td>` +
      `<td>${esc(x.kind || "—")}</td><td>${esc(x.level || "—")}</td>` +
      `<td>${esc(notifyStateText(x))}</td><td>${esc(err)}</td></tr>`;
  }).join("") || `<tr><td colspan="6">${esc(T("notify_none"))}</td></tr>`;
  if (th) {
    $("alertTh").innerHTML = th.map(x =>
      `<div class="id-row"><span>${esc(T(x.i18n))}</span>` +
      `<b>${esc(String(x.value))}${x.unit ? " " + esc(x.unit) : ""}</b></div>`).join("");
  }
}

async function notifyCtl(body) {
  const r = await postCtl(body);
  let info = {};
  try { info = await r.json(); } catch (e) { /* ignore */ }
  if (info && info.notify) renderNotify({ notify: info.notify, thresholds: null });
  return info;
}

if ($("btnNotifySave")) {
  $("btnNotifySave").onclick = async () => {
    const info = await notifyCtl({ action: "notify_set", url: $("notifyUrl").value,
                                   level: $("notifyLevel").value });
    $("notifyMsg").textContent = (info.notify && info.notify.on)
      ? T("notify_saved_on") : T("notify_saved_off");
    $("notifyMsg").className = "hint";
    refreshAlerts();
  };
  $("btnNotifyTest").onclick = async () => {
    const info = await notifyCtl({ action: "notify_test" });
    const rec = (info && info.test) || {};
    const ok = rec.ok === true;
    $("notifyMsg").textContent = rec.err === "off" ? T("notify_test_off")
      : ok ? T("notify_test_ok") : T("notify_test_fail") + (rec.err || "?");
    $("notifyMsg").className = "hint" + (ok ? "" : " err");
    refreshAlerts();
  };
}

/* ---------- 告警弹窗（SSE 边沿事件） ----------
   只在新告警 / 级别升高 / 告警消失时收到（服务端 `_emit_alert` 只在边沿发）——
   所以这里**不需要**自己判重，也不会每 3 秒弹一遍。弹窗说的是“通知发出去了没有”，
   与上方红点（当前状态）是两件事：状态持续存在，通知只在变化的那一刻发一次。

   **「看到」之后要能「去看」**：弹窗是唯一的瞬时提示，点它应当直接跳到看该问题的地方
   （否则读者得自己猜去哪一页、哪一条）。URL 是 UI 的事，所以映射表放这里；
   服务端只给机器可读字段（kind/sn/key）—— 见 ALERT_LINK 的注释。 */

/* 告警 kind → 点弹窗去哪儿看。
   ★ 这张表**必须覆盖 `alerts.py` 里所有 kind**（`test_appjs.py` 会从 alerts.py 抽 kind 逐个核；
   漏一个 = 那条告警点了没反应 = 静默失效）。`{sn}` / `{key}` 会被代入并 URL 编码。
   为什么不是每个 kind 都指向“最精确”的页面：有的告警没有更精确的落点（如限频只有首页那张卡片），
   就指向它所在的卡片锚点 —— 比跳首页顶部强。 */
const ALERT_LINK = {
  no_report: "track.html?sn={sn}",              // 长未上报 → 看这台设备的观测/最后定位（真实上报模式）
  no_report_energy: "track.html?sn={sn}",       // 没电导致的沉默 → 同上（归因不同，落点相同）
  rssi_jump: "track.html?sn={sn}",              // RSSI 突变 → 看它的 RSSI 曲线
  id_cap_mismatch: "index.html#idsec",           // 能力与声明不符 → Orpah ID 签名上报卡片
  id_clock: "index.html#ctlsec",                 // 时钟偏移 → 上报控制卡片
  id_energy: "index.html#ensec",                 // 电量低/耗尽 → 能量轴卡片
  id_degraded: "metrics.html?sn={sn}",           // 签名降级 → 算法分布/验签统计
  ratelimit: "index.html#rlsec",                 // 被限频丢包 → 限频卡片
  badcheck_streak: "metrics.html",               // SN 校验连败 → 验签/被拒统计
  sig_fail_rate: "metrics.html",                 // 签名失败率超阈 → 同上
  case_overtime: "case.html",                    // 走失超时没人接手 → 案件页
  case_handled_overtime: "case.html",            // 已接手但超时效 → 案件页
};
function alertLink(a) {
  const tpl = ALERT_LINK[(a && a.kind) || ""];
  if (!tpl) return null;                         // 没登记的 kind → 弹窗不跳（别瞎指）
  return tpl.replace("{sn}", encodeURIComponent(a.sn || ""))
            .replace("{key}", encodeURIComponent(a.key || ""));
}

function alertToast(ev) {
  const box = $("alertToast");
  if (!box) return;
  const a = ev.alert || {};
  const div = document.createElement("div");
  div.className = "alert-toast" + (a.level === "crit" ? " crit" : "");
  const title = i18nOr("notify_ev_" + ev.event, ev.event);
  const body = a.msg ? esc(alertText(a)) : esc(a.kind || ev.key || "");
  const d = ev.delivery || {};
  const sent = d.err === "off" ? T("notify_toast_off")
    : d.ok === true ? T("notify_toast_sent")
    : T("notify_toast_fail") + (d.err || "?");
  const url = alertLink(a);
  if (url) div.classList.add("link");
  div.innerHTML = `<span class="x" title="${esc(T("btn_cancel"))}">✕</span>` +
    `<div class="tt">${esc(title)}</div>${body}` +
    `<div class="tb${d.ok === true || d.err === "off" ? "" : " bad"}">${esc(sent)}</div>` +
    (url ? `<div class="go">${esc(T("notify_toast_jump"))}</div>` : "");
  div.querySelector(".x").onclick = (e) => { e.stopPropagation(); div.remove(); };
  if (url) {
    div.onclick = () => { location.href = url; };
    div.tabIndex = 0;                              // 键盘可达：可聚焦 + Enter/Space 跳转
    div.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); location.href = url; }
    };
  }
  box.appendChild(div);
  while (box.children.length > 4) box.firstChild.remove();
  setTimeout(() => div.remove(), 12000);        // 不永久占屏；红点仍是长期状态
  beep(ev.event === "alert_resolved" ? "resolved" : a.level);   // 声音提醒（默认关）
}

/* ---------- 声音提醒（默认关；勾选才响） ----------
   现场用 WebAudio 合成，不带音频文件。三条如实说明（页面卡片里也写了）：
   ① 它只是**本机提示音**，不是“通知已送达”的证明 —— 真正的送达靠 Webhook，
      关掉页面/换台电脑就没有声音；
   ② 浏览器自动播放策略：没有跟页面交互过时 AudioContext 是 suspended → 这里**静默跳过**，
      不假装响过（首次点击页面后自动解锁）；
   ③ 默认关，开关存在 localStorage（只有“要不要响”是本机偏好，与 Webhook 地址无关）。 */
const SOUND_KEY = "orpah_ui_notify_sound";
let audioCtx = null;
function soundOn() { const c = $("notifySound"); return !!(c && c.checked); }
function audioReady() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;                          // 老浏览器：没有就没声，别抛
    if (!audioCtx) audioCtx = new AC();
    if (audioCtx.state === "suspended") audioCtx.resume();
    return audioCtx.state === "running" ? audioCtx : null;
  } catch (e) { return null; }
}
function beep(level) {
  if (!soundOn()) return false;
  const ctx = audioReady();
  if (!ctx) return false;
  const tone = (f, t0, dur) => {
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = "sine"; o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, ctx.currentTime + t0);
    g.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + t0 + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + t0 + dur);
    o.connect(g); g.connect(ctx.destination);
    o.start(ctx.currentTime + t0); o.stop(ctx.currentTime + t0 + dur + 0.02);
  };
  if (level === "crit") { tone(880, 0, 0.16); tone(880, 0.22, 0.16); }   // crit = 两声
  else if (level === "resolved") tone(520, 0, 0.18);                     // 消警 = 低声一声
  else tone(880, 0, 0.16);
  return true;
}
if ($("notifySound")) {
  const c = $("notifySound");
  c.checked = localStorage.getItem(SOUND_KEY) === "1";        // 默认关（读不到就是关）
  c.onchange = () => {
    localStorage.setItem(SOUND_KEY, c.checked ? "1" : "0");
    if (c.checked) beep("warn");                              // 勾上先响一声（试听 + 解锁）
  };
  /* 首次交互解锁：**只在开着声音时才建 AudioContext**（默认关就不该建对象）。
     挂着不解锁就一直留着 —— 浏览器策略下 suspended 需要一次真实手势，而用户可能先点别处。 */
  const unlock = () => {
    if (soundOn() && audioReady()) document.removeEventListener("click", unlock);
  };
  document.addEventListener("click", unlock);
}

async function refreshAlerts() {
  try {
    const r = await fetch("/api/alerts").then(x => x.json());
    if (r && r.ok) renderAlerts(r);
  } catch (e) { /* 服务器未就绪 */ }
}
if ($("alertBadge")) {
  setInterval(refreshAlerts, 3000);
  refreshAlerts();
}

/* ---------- 顶部「工具」下拉菜单 ----------
   已由 `nav.js`（头部导航单一源）接管：这里不再接线（两处接线 = 点一下开又关）。
   菜单元素名叫 `navToolsBtn`/`navToolsMenu`。 */
