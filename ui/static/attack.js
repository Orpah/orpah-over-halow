/* attack.js — 攻击流量独立面板（2026-09-13）
 * ==========================================
 * 数据源（全部来自服务端，**本页不另写一份**）：
 *   · `/api/status.spoof_kinds` — 攻击清单（名称/期望裁决/防线/说明），单一源 = `spoof.py` 的 CASES
 *   · `/api/status.spoof`       — 攻击流量状态：注入数、每道防线拦下数、最近流量、未等到结果的条数
 *   · `/api/ctl {action:"spoof"}`      — 按类型注入一条（走真链路）
 *   · `/api/ctl {action:"spoof_reset"}`— 清空面板（**不**动密钥库/限频桶）
 *
 * 为什么单独一页（ROADMAP §四「攻击流量的 UI 独立面板」）：以前攻击结果只能混在首页的
 * 「签名上报流 + 事件历史」里看，正常周期上报与攻击报文排在一起，读者得自己猜哪条是哪条。
 * 现在服务端按 **nonce** 认领攻击报文（`ui_server._spoof_claim`），攻击流量是**一条独立的流**。
 *
 * 口径（照实说，别包装）：结果只有三种 —— 与期望一致 / 与期望不一致 / 未等到结果。
 * 「被拒」不等于「防住了」；没等到结果既不算被拒也不算通过（可能死在限频那道防线之前）。
 */
"use strict";

const $ = id => document.getElementById(id);
const T = k => OrpahI18n.t(k);
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));

let KINDS = [];      // /api/status.spoof_kinds（清单，来自 spoof.py）
let SP = {};         // /api/status.spoof（攻击流量状态）
let running = false; // 正在「跑全部」
let pollMs = 1000;   // 轮询间隔（跑全部时收紧，好让逐条结果快点上屏）

const isEn = () => OrpahI18n.lang === "en";
const nameOf = o => (o ? (isEn() ? (o.en || o.kind) : (o.zh || o.kind)) : "?");
const noteOf = o => (o ? (isEn() ? (o.note_en || "") : (o.note_zh || "")) : "");
const verdictText = v => (v == null ? T("spoof_accept") : v);

/* 防线 id → 名字：**取自服务端 `spoof.DEFENSES`**（页面不写死清单）。
 * "wait" = 没等到结果（不是第 10 道防线，是"根本没走到验签"），单列一档。 */
function lineName(id) {
  if (id === "wait") return T("at_lost");
  const d = (SP.defenses || []).find(x => x.id === id);
  if (!d) return id || "—";
  return isEn() ? d.en : d.zh;
}

function msg(html, cls) {
  const el = $("atMsg");
  el.innerHTML = html;
  el.className = "hint" + (cls ? " " + cls : "");
}

function setRunning(on) {
  running = on;
  pollMs = on ? 300 : 1000;
  $("atAll").disabled = on;
  $("atStop").style.display = on ? "" : "none";
}

/* ---------- 渲染 ---------- */
function render() {
  const res = SP.results || {};
  const waiting = {};
  (SP.waiting || []).forEach(w => { waiting[w.kind] = w; });

  // 概览：三种结果分开数（不合并成一个"通过率" —— 未等到结果不是"不一致"）
  let ok = 0, bad = 0, lost = 0, cover = 0;
  KINDS.forEach(k => {
    const r = res[k.kind];
    if (!r) return;
    cover++;
    if (r.state === "lost") lost++;
    else if (r.ok) ok++;
    else bad++;
  });
  $("atInj").textContent = SP.injected || 0;
  $("atOk").textContent = ok;
  $("atBad").textContent = bad;
  $("atLost").textContent = lost;
  $("atCover").textContent = cover + " / " + KINDS.length;

  // 防线分布：条长 = 条数（单色 —— 颜色不承载分类，不辨色也能读）
  const cnt = {};
  Object.keys(res).forEach(k => {
    const lid = res[k].line;
    cnt[lid] = (cnt[lid] || 0) + 1;
  });
  const expectCnt = {};
  KINDS.forEach(k => { expectCnt[k.line] = (expectCnt[k.line] || 0) + 1; });
  const bars = (SP.defenses || []).map(d => ({
    id: d.id, name: isEn() ? d.en : d.zh, n: cnt[d.id] || 0,
    // 本清单里**没有**用例期望落在这一道 → 显示 0 是"没这条用例"，不是"没跑"
    none: !expectCnt[d.id],
  }));
  if (cnt.wait) bars.push({ id: "wait", name: T("at_lost"), n: cnt.wait, none: false });
  const max = Math.max(1, ...bars.map(b => b.n));
  $("atBars").innerHTML = bars.map(b =>
    `<div class="brow${b.n ? "" : " zero"}">` +
    `<span class="bname">${esc(b.name)}` +
    (b.none ? ` <span class="bnote">· ${T("at_line_none")}</span>` : "") + `</span>` +
    `<span class="bnum">${b.n}</span>` +
    `<span class="btrack"><span class="bfill" style="width:${(b.n / max * 100).toFixed(1)}%"></span></span>` +
    `</div>`).join("");

  // 清单表：每行 = 一条用例（期望 / 实际 / 哪道防线 / 最近结果）
  $("atList").innerHTML = KINDS.map(o => {
    const r = res[o.kind] || null;
    const w = waiting[o.kind] || null;
    let mark = `<span class="mark none">—</span>`;
    if (w) mark = `<span class="mark wait">${T("at_waiting")}</span>`;
    else if (r) {
      mark = r.state === "lost" ? `<span class="mark wait">${T("at_lost")}</span>`
        : r.ok ? `<span class="mark ok">✓ ${T("at_match")}</span>`
               : `<span class="mark bad">✗ ${T("at_mismatch")}</span>`;
    }
    const got = (r && r.state !== "lost")
      ? `<code class="c">${esc(verdictText(r.got))}</code>` : "—";
    const line = (r && r.state !== "lost") ? esc(lineName(r.line)) : "—";
    return `<tr>
      <td class="case"><div class="nm">${esc(nameOf(o))}</div>` +
      `<div class="kid">${esc(o.kind)}</div>` +
      `<div class="nt">${esc(noteOf(o))}</div></td>` +
      `<td>${esc(verdictText(o.expect))}` +
      `<div class="nt">${esc(lineName(o.line))}</div></td>` +
      `<td>${got}</td><td>${line}</td><td>${mark}</td>` +
      `<td class="mono">${r ? esc(r.t) : "—"}</td>` +
      `<td><button class="btn" data-inject="${esc(o.kind)}">${T("at_inject")}</button></td>` +
      `</tr>`;
  }).join("");

  // 攻击流量流（只含攻击报文；正常周期上报不在这里）
  const rec = SP.recent || [];
  $("atRecent").innerHTML = rec.length ? rec.map(r => {
    const o = KINDS.find(x => x.kind === r.kind) || { kind: r.kind };
    const lv = (r.level == null) ? "—" : "L" + r.level;
    const got = r.state === "lost" ? T("at_lost")
      : `<code class="c">${esc(verdictText(r.got))}</code>`;
    const line = r.state === "lost" ? "—" : esc(lineName(r.line));
    const sn = r.sn ? `<a class="snlink" href="registry.html?sn=${encodeURIComponent(r.sn)}">${esc(r.sn)}</a>` : "—";
    return `<tr><td class="mono">${esc(r.t)}</td><td>${esc(nameOf(o))}</td>` +
      `<td class="mono">${sn}</td><td>${lv}</td><td>${got}</td><td>${line}</td></tr>`;
  }).join("") : `<tr><td colspan="6">${T("at_no_traffic")}</td></tr>`;
}

/* ---------- 注入 ---------- */
/* fetch 失败（UI 服务器被停/网络断）时与 app.js 的 postCtl 同一写法：吃掉异常、
 * 返回 undefined —— 调用方 `injectOne` 已经按 `!info` 处理，不会冒出 unhandled rejection
 * （那种情况下「跑全部」会停在半路，且页面上什么也不说）。 */
async function postCtl(body) {
  const r = await fetch("/api/ctl", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).catch(e => {
    console.error("postCtl 请求失败:", e);
    return null;
  });
  if (!r) return null;
  return r.json();
}

/** 等这一条（按 nonce 认领）的结果上屏；超时返回 null（服务端随后会把它标成「未等到结果」）。 */
function waitResult(kind, nonce, timeoutMs) {
  const t0 = Date.now();
  return new Promise(res => {
    const iv = setInterval(() => {
      const row = (SP.results || {})[kind];
      if (row && row.nonce === nonce) { clearInterval(iv); res(row); return; }
      if (Date.now() - t0 > timeoutMs) { clearInterval(iv); res(null); }
    }, 150);
  });
}

async function injectOne(kind) {
  const o = KINDS.find(x => x.kind === kind);
  const info = await postCtl({ action: "spoof", kind });
  if (!info || !info.ok) {
    // 连不上服务器（postCtl 返回 null）与「服务端拒绝了这个 kind」要分开说：
    // 前者是环境问题（ui_server 没跑），后者是调用参数问题 —— 显示成同一个 "?" 看不出该改哪边。
    msg(T("at_fail") + esc((info && info.err) || T("at_net_err")), "err");
    return null;
  }
  msg(T("at_sent").replace("{k}", esc(nameOf(o || info))));
  // 等待上限 = 服务端的判定阈值 + 一点余量（**不写死**：阈值由 /api/status 给）
  const wait = Math.round(((SP.wait || 15) + 3) * 1000);
  const row = await waitResult(kind, info.nonce, wait);
  return row || { kind, state: "lost", timeout: true };
}

async function runAllSeq() {
  if (running) return;
  setRunning(true);
  const list = KINDS.slice();
  let n = 0, ok = 0, bad = 0, lost = 0;
  for (let i = 0; i < list.length && running; i++) {
    msg(T("at_running").replace("{i}", i + 1).replace("{n}", list.length) +
        " · " + esc(nameOf(list[i])));
    const r = await injectOne(list[i].kind);
    if (r) {
      n++;
      if (r.state === "lost") lost++;
      else if (r.ok) ok++;
      else bad++;
    }
    await sleep(120);
  }
  setRunning(false);
  msg(T("at_done").replace("{n}", n).replace("{ok}", ok)
    .replace("{bad}", bad).replace("{lost}", lost));
  await load();
}

/* ---------- 轮询 ---------- */
async function load() {
  try {
    const d = await (await fetch("/api/status")).json();
    if (d.spoof_kinds && d.spoof_kinds.length) KINDS = d.spoof_kinds;
    SP = d.spoof || {};
    render();
  } catch (e) {
    /* 服务器没起：保留上一屏（清空会让人以为"面板本来就没东西"） */
  }
}

async function loop() {
  await load();
  setTimeout(loop, pollMs);
}

$("atList").onclick = ev => {
  const b = ev.target.closest("button[data-inject]");
  if (b) injectOne(b.getAttribute("data-inject"));
};
$("atAll").onclick = () => runAllSeq();
$("atStop").onclick = () => { running = false; setRunning(false); };
$("atReset").onclick = async () => {
  await postCtl({ action: "spoof_reset" });
  await load();
  msg(T("at_reset_done"));
};

OrpahI18n.apply();
loop();
