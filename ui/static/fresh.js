/* fresh.js — 「这是不是老数据」的共享件（单一源：index / track / replay 共用）
   --------------------------------------------------------------------------
   现场最容易看错的一件事：页面上的数字**看起来一样**，含义却完全不同 ——
   「刚更新的」「30 秒没动了」「其实早停了」。而且页面自己的 1s 轮询照旧在画，
   看着一直在动（**轮询正常 ≠ 后端在推进**，这是两件独立的事，必须分开说）。

   本文件只做“把事实翻译成人话”，两类事实分得很清：
     · **后端侧**（服务端给的 `status.fresh`）：每条周期流「最近一次真的动了」+ 状态。
       状态与阈值由服务端算（`freshness.py`，按各流自己的节拍推），页面**不写死阈值**。
     · **本页侧**（`freshPollOk/Fail`）：本页轮询还通不通、在不在后台（浏览器会限流）。
       这一条服务端不知道，只能页面自己观察。

   状态与标记（形状 + 颜色**双通道**，且文字永远在 —— 不辨色也能读）：
     live ● 绿（正常） · stale ◐ 橙（滞后） · stopped ✕ 红（停摆） · unknown ○ 灰（还没见过）
   诚实边界（页面上要写出来）：
     · 它只回答「**我们这侧**这类数据还在不在推进」，**不代表对端设备在线**
       （设备不发报也可能是它没电了、被屏蔽了、或者本来就不需要这么勤）；
     · `unknown`（本进程还没见过这类数据）**既不是正常也不是故障**，不许显示成绿色；
     · `tsdb_write` 行的「未启用」与「本该写却停了」是两件事（处置完全不同），文案分开。

   底线（与 pos.js 的 trustText 同一个教训）：**所有文案都要把 `T` 传进来** ——
   本文件不依赖字典实现（单测传桩函数）。*/

/* 状态标记：符号 + 颜色类（形状是第二通道） */
const FRESH_MARK = {
  live:    { sym: "●", cls: "f-live" },
  stale:   { sym: "◐", cls: "f-stale" },
  stopped: { sym: "✕", cls: "f-stop" },
  unknown: { sym: "○", cls: "f-unknown" },
  /* `fact` = **陈述性事实**（不是“状态”）：中性圆点，不参与好坏判断。
     用在“数据截止时刻”这类东西上 —— 设备多久报一次由设备决定（低功耗设备可能 30 分钟一条），
     拿“截止多久以前”判故障会把正常省电误报成停摆。 */
  fact:    { sym: "·", cls: "f-fact" },
};
/* 最坏态排序：unknown 排在 live 之后 —— 没数据不等于坏，但也不该被读成“一切正常” */
const FRESH_ORDER = { unknown: 0, live: 1, stale: 2, stopped: 3 };

/* 阈值倍数（**必须与 freshness.py 的 LIVE_MULT / STALE_MULT 一致**，
   `test_fresh.py` 会把两个文件里的数逐字比一遍 —— 漂了就是页面上说“正常”、
   服务端说“滞后”这种自相矛盾）。本页轮询也复用同一组倍数（见 freshPollState）。 */
const FRESH_LIVE_MULT = 2.5;
const FRESH_STALE_MULT = 6.0;

/* 每条流的人话名字（键 → i18n key；页面/服务端新增一条流时，这里加一行即可） */
const FRESH_LABEL = {
  report_cycle: "fresh_l_report_cycle",
  id_report:    "fresh_l_id_report",
  alert_scan:   "fresh_l_alert_scan",
  tsdb_write:   "fresh_l_tsdb_write",
  page_poll:    "fresh_l_page_poll",
};

/* 未知键 → 原样显示机器名（同 connZh/nodeLabel 的回退约定，不静默消失） */
function freshLabel(key, T) {
  const k = FRESH_LABEL[key];
  if (!k) return String(key || "?");
  const s = T(k);
  return (s === k || s == null) ? String(key) : s;
}

/* 年龄 → 人话（秒/分/时；**不用 toLocaleString** —— 那个会把数字变成带千分位/本地写法，
   在“几秒前”这种短量级上没有意义，且受页面语言影响）。 */
function freshAgeText(ageS, T) {
  if (ageS === null || ageS === undefined || isNaN(ageS)) return T("fresh_never");
  const a = Number(ageS);
  if (a < 60) return T("fresh_age_s").replace("{n}", a.toFixed(a < 10 ? 1 : 0));
  if (a < 3600) return T("fresh_age_m").replace("{n}", String(Math.floor(a / 60)));
  return T("fresh_age_h").replace("{n}", (a / 3600).toFixed(1));
}

/* 状态 → 文案（带年龄）。`state` 是服务端给的机器值；认不出来的状态**不当正常**。 */
function freshStateText(state, ageS, T) {
  const key = "fresh_s_" + String(state || "unknown");
  const s = T(key);
  if (s === key || s == null) return T("fresh_s_unknown").replace("{age}", freshAgeText(ageS, T));
  return s.replace("{age}", freshAgeText(ageS, T));
}

/* 年龄 → “N 前”的附加词（只用于事实行：时刻 + （N 前）） */
function freshAgo(ageS, T) {
  return T("fresh_ago").replace("{age}", freshAgeText(ageS, T));
}

/* 后端侧一行的人话（含 tsdb 的两个特例）。返回纯文本，不碰 DOM。 */
function freshRowText(row, T) {
  if (!row) return "";
  if (row.key === "tsdb_write" && row.on === false) return T("fresh_s_disabled");
  return freshStateText(row.state, row.age_s, T);
}

/* 一行行的悬停解释：节拍几秒、上次动是什么时候、落库错误摘要。
   写清楚“这条流的正常节拍”很重要 —— 否则 30s 的节拍会被当成“太慢”。 */
function freshRowTitle(row, T) {
  if (!row) return "";
  const parts = [];
  if (row.period_s) {
    parts.push(T("fresh_tip_period").replace("{n}", String(row.period_s)));
  }
  if (row.last) {
    parts.push(T("fresh_tip_last").replace("{t}", new Date(row.last * 1000).toLocaleTimeString()));
  }
  if (row.key === "tsdb_write") {
    parts.push(row.on === false ? T("fresh_tip_tsdb_off") : T("fresh_tip_tsdb_on"));
    if (row.err) parts.push(T("fresh_tip_err").replace("{e}", String(row.err)));
  }
  parts.push(T("fresh_tip_caveat"));
  return parts.join(" ");
}

/* 最坏态（整条横幅用哪个颜色）。空集合 → unknown（没有数据 ≠ 正常）。 */
function freshWorst(rows) {
  let w = "unknown";
  (rows || []).forEach(r => {
    if (!r) return;
    const s = (r.key === "tsdb_write" && r.on === false) ? "unknown" : String(r.state || "unknown");
    if ((FRESH_ORDER[s] || 0) > (FRESH_ORDER[w] || 0)) w = s;
  });
  return w;
}

/* ---------- 本页轮询（页面自己观察得到的事实） ----------
   为什么要它：后端停摆时页面只看得到“数字不动”，但**数字不动也可能是没变化**；
   反过来，页面网络断了/标签页在后台被浏览器限流时，又会让人以为“数据停了”。
   两者必须分开说（见 freshPollState 的 hidden 分支）。 */
let _pollPeriodS = 1;
let _pollLastOk = null;      // 最近一次轮询成功（**页面本地时钟**，epoch 秒）
let _pollLastTry = null;
let _pollFails = 0;

function freshPollArm(periodS) { _pollPeriodS = Number(periodS) > 0 ? Number(periodS) : 1; }
function freshPollOk(nowS) {
  _pollLastOk = (nowS === undefined) ? Date.now() / 1000 : nowS;
  _pollLastTry = _pollLastOk;
  _pollFails = 0;
}
function freshPollFail(nowS) {
  _pollLastTry = (nowS === undefined) ? Date.now() / 1000 : nowS;
  _pollFails += 1;
}
function freshPollReset() { _pollLastOk = _pollLastTry = null; _pollFails = 0; }

/* 本页轮询的状态：拿到过答复就按“距上次成功多久”判；一次都没成功过 → stopped
   （页面根本没连上，这时说“正常”是最坏的谎）。
   `document.hidden` = 本页在后台：**浏览器会限制定时器**，所以“没刷新”是我们自己这边
   的原因，不是后端停了 → 单独一个状态文案，不参与故障判断（但也不说成绿色正常）。 */
function freshPollState(nowS) {
  const now = (nowS === undefined) ? Date.now() / 1000 : nowS;
  if (typeof document !== "undefined" && document && document.hidden) return "hidden";
  if (_pollLastOk === null) return "stopped";
  const age = now - _pollLastOk;
  if (age <= FRESH_LIVE_MULT * _pollPeriodS) return "live";
  if (age <= FRESH_STALE_MULT * _pollPeriodS) return "stale";
  return "stopped";
}

function freshPollText(nowS, T) {
  const st = freshPollState(nowS);
  const now = (nowS === undefined) ? Date.now() / 1000 : nowS;
  if (st === "hidden") return T("fresh_s_page_hidden");
  if (_pollLastOk === null) {
    return T("fresh_s_page_down").replace("{n}", String(_pollFails));
  }
  const s = T("fresh_s_page_" + st);
  const txt = (s && s.indexOf("fresh_s_page_") !== 0)
    ? s.replace("{age}", freshAgeText(now - _pollLastOk, T))
    : freshStateText(st, now - _pollLastOk, T);
  /* 失败计数直接接在后面（不加空格）：中文用全角括号不需要空格，英文的词条自己以空格开头 */
  return (_pollFails > 0) ? (txt + T("fresh_page_fails").replace("{n}", String(_pollFails))) : txt;
}

/* ---------- 数据集这一侧的事实（track / replay 用） ----------
   `info` 全部用 **epoch 秒**（页面自己换算，别在这儿猜单位）：
     · `fetchAt` 本页最后一次成功取数的时刻；
     · `cutoff`  这批数据里最新的样本时刻（无数据传 null）；
     · `n`       本次取回的样本条数；
     · `error`   取数失败时的错误文本（有它就不显示上面那些 —— 没数据却说截止时刻是骗人）。
   **不判 live/stale/stopped**：设备多久报一次由设备决定，见上面 `fact` 的说明。 */
function freshDataset(box, info, T) {
  if (!box) return;
  box.textContent = "";
  const now = Date.now() / 1000;
  if (info && info.sim) {
    /* 模拟模式：本地合成，根本没有外部数据源 —— 如实说清，别让人找“数据老不老” */
    freshLine(box, T("fresh_l_dataset"), "fact", T("fresh_s_sim"), T("fresh_tip_sim"));
    _freshPollLine(box, T, info.poll);
    return;
  }
  if (info && info.error) {
    freshLine(box, T("fresh_l_dataset"), "stopped", String(info.error), T("fresh_tip_dataset_err"));
    _freshPollLine(box, T, info.poll);
    return;
  }
  const tm = s => new Date(s * 1000).toLocaleTimeString();
  if (!info || !info.fetchAt) {
    freshLine(box, T("fresh_l_dataset"), "unknown", T("fresh_never"), T("fresh_tip_fetch_at"));
    _freshPollLine(box, T, info && info.poll);
    return;
  }
  freshLine(box, T("fresh_l_fetch_at"), "fact",
            freshAgo(now - info.fetchAt, T), T("fresh_tip_fetch_at"));
  freshLine(box, T("fresh_l_cutoff"), "fact",
            info.cutoff ? (tm(info.cutoff) + " · " + freshAgo(now - info.cutoff, T)) : T("fresh_no_data"),
            T("fresh_tip_cutoff"));
  if (info.n !== undefined && info.n !== null) {
    freshLine(box, T("fresh_l_samples"), "fact", String(info.n), T("fresh_tip_samples"));
  }
  _freshPollLine(box, T, info.poll);
}

/* ---------- 渲染（**只在这里碰 DOM**） ----------
   用 textContent 逐块填，不拼 HTML —— 从根上不给注入留口子（app.js 的 esc 那套在这里不需要）。*/
function _freshEl(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined && text !== null) el.textContent = text;
  return el;
}

/* 一行：● 周期上报 · 1.2 秒前（title 里是节拍/上次时刻/边界说明） */
function freshLine(box, label, state, text, title) {
  const item = _freshEl("span", "fitem");
  const mk = FRESH_MARK[state] || FRESH_MARK.unknown;
  item.appendChild(_freshEl("span", "fdot " + mk.cls, mk.sym));
  item.appendChild(_freshEl("span", "flab", label));
  item.appendChild(_freshEl("b", "fage", text));
  if (title) item.title = title;
  box.appendChild(item);
  return item;
}

/* 本页轮询那一行（`freshRender` 与 `freshDataset` 共用）——
   两个页面自己都不重写一份，否则“本页到底还通不通”会出现两种说法。 */
function _freshPollLine(box, T, show) {
  if (show === false) return;
  const st = freshPollState();
  freshLine(box, freshLabel("page_poll", T),
            (st === "hidden") ? "fact" : st, freshPollText(undefined, T),
            T("fresh_tip_poll").replace("{n}", String(_pollPeriodS)));
}

/* 把服务端 `status.fresh` 渲染进容器（`fresh` 传 null = 这一轮没拿到服务端状态）。 */
function freshRender(box, fresh, T) {
  if (!box) return;
  box.textContent = "";
  const rows = (fresh && fresh.rows) || [];
  /* 整条横幅的“最坏态”：**只覆盖服务端那几行**（本页轮询行自带状态，页面上看那一个点）。
     服务端会带 `worst`（单一源），但它缺失时自己算一份
     （`freshWorst` 与 `freshness.py` 的 _ORDER 同序：unknown < live < stale < stopped）。 */
  box.dataset.worst = (fresh && fresh.worst) || freshWorst(rows);
  rows.forEach(r => {
    freshLine(box, freshLabel(r.key, T),
              (r.key === "tsdb_write" && r.on === false) ? "fact" : (r.state || "unknown"),
              freshRowText(r, T), freshRowTitle(r, T));
  });
  /* 本页轮询始终显示：它是“这页看到的数字有多可信”的前提，缺了这块，
     上面几行就是无源之水（拿不到服务端状态时更是只有它能说明问题）。 */
  _freshPollLine(box, T, true);
}
