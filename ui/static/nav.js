/* nav.js — 头部导航的**单一源**（2026-09-13）
================================================================================
为什么抽出来（用户 2026-09-13 定的 UI 优化第一项）：以前**每个页面手写自己的头部链接**，
于是必然出现三件事 ——
  ① **有的页到不了**：工具页（签名/校验码/Damm32/指标/RSSI）只有从首页的下拉菜单能进，
     进别的子页后就没路回去了（实测 12 个页面的头部链接各不相同）；
  ② **同一链接在不同页文案/顺序不一致**（"← 返回主页" 有的在第二位有的在最后）；
  ③ **新增页面要改 12 个文件**，漏一个就是"点了回不来"，而且没人会发现。

现在：**页面只放 `<div id="nav" data-sub="<本页副标题 i18n 键>"></div>`**，其余由这里生成 ——
主导航、工具下拉、语言按钮、当前页高亮（`aria-current="page"` + 视觉）、以及"我在哪一页"。

**页面上自己那块不在这里**：告警徽标、连接状态、暂停/清空、刷新、操作者输入框（审计"谁"）——
那些是页面功能，不是导航，留在各页的 `<div class="tools" id="hdrTools">` 里。

`data-sub` 的键由各页给（`brand_sub`/`tk_sub`/`case_sub`…）＝ 本页副标题；
**页面名**（"走失案件"）不用各页写，由这里按当前文件名从下面的列表里查 —— 那是"我在哪"，
正是最该单一源的东西（写两处必然漂移）。

⚠ 新增页面时**必须**在下面 MAIN 或 TOOLS 里登记（否则 `test_uicss.py` 会拦：
"页面没登记进导航 = 从界面上到不了"）。 */
(function () {
  "use strict";

  /* 主导航 = 现场找人时会走的顺序：首页 → 人/设备 → 案件 → 定位 → 回放 → 攻击面 */
  var MAIN = [
    { href: "index.html", key: "nav_home", t: "nav_home_title" },
    { href: "registry.html", key: "nav_registry", t: "nav_registry_t" },
    { href: "case.html", key: "nav_case", t: "nav_case_t" },
    { href: "track.html", key: "nav_track", t: "nav_track_t" },
    { href: "replay.html", key: "nav_replay", t: "nav_replay_t" },
    { href: "attack.html", key: "nav_attack", t: "nav_attack_t" }
  ];
  /* 工具页收进下拉：结构/算法/密钥/编解码这些不是"每次现场都要点"的入口 */
  var TOOLS = [
    { href: "rssi.html", key: "nav_rssi", t: "nav_rssi_t" },
    { href: "metrics.html", key: "nav_metrics", t: "nav_metrics_t" },
    { href: "sig.html", key: "nav_sig", t: "nav_sig_t" },
    { href: "keys.html", key: "nav_keys", t: "nav_keys_t" },
    { href: "checksum.html", key: "nav_checksum", t: "nav_checksum_t" },
    { href: "damm32.html", key: "nav_damm32", t: "nav_damm32_t" }
  ];

  function T(k) {
    return (window.OrpahI18n && OrpahI18n.t) ? OrpahI18n.t(k) : k;
  }
  /* 自带 esc：nav.js 在页面内联脚本**之前**加载，那时页面里的 esc() 还不存在
     （同 map.js「顶层别碰页面函数」那条教训）。 */
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function curPage() {
    var p = location.pathname.split("/").pop();
    return p || "index.html";
  }
  function isCur(href) { return href === curPage(); }

  function link(p, cls) {
    var c = cls + (isCur(p.href) ? " cur" : "");
    var aria = isCur(p.href) ? ' aria-current="page"' : "";
    return '<a class="' + c + '" href="' + esc(p.href) + '"' + aria +
           ' title="' + esc(T(p.t)) + '">' + esc(T(p.key)) + "</a>";
  }

  /* 页面名：按当前文件名查列表（本页写自己的名字 = 迟早漂移）。
     首页不给标签：品牌名本身就是首页的身份，而且 `nav_home` 的文案是「← 返回主页」
     （它在导航里是对的、当页面名就很怪）。 */
  function pageName() {
    var all = MAIN.concat(TOOLS);
    for (var i = 0; i < all.length; i++) {
      if (all[i].href === curPage()) {
        return all[i].href === "index.html" ? "" : T(all[i].key);
      }
    }
    return "";
  }

  function navInit() {
    var box = document.getElementById("nav");
    if (!box) return false;
    var sub = box.getAttribute("data-sub") || "brand_sub";
    var name = pageName();
    box.innerHTML =
      '<div class="brand">' +
        '<a href="index.html" title="' + esc(T("nav_home_title")) + '">' +
          '<img class="brand-logo" src="orpah-logo.png" alt="orpah"></a>' +
        '<span class="brand-name">ORPAH-over-HaLow</span>' +
        (name ? '<span class="page-now">' + esc(name) + "</span>" : "") +
        '<span class="sub">' + esc(T(sub)) + "</span>" +
      "</div>" +
      '<div class="tools">' +
        MAIN.map(function (p) { return link(p, "btn nav-link"); }).join("") +
        '<div class="menu" id="navToolsMenu">' +
          '<button class="btn" id="navToolsBtn">' + esc(T("nav_tools")) + "</button>" +
          '<div class="menu-list">' +
            TOOLS.map(function (p) { return link(p, "nav-tool"); }).join("") +
          "</div>" +
        "</div>" +
        '<button class="btn" id="langBtn" title="中文 / English"></button>' +
      "</div>";

    var btn = document.getElementById("navToolsBtn");
    var menu = document.getElementById("navToolsMenu");
    if (btn && menu) {
      btn.onclick = function (e) { e.stopPropagation(); menu.classList.toggle("open"); };
      document.addEventListener("click", function () { menu.classList.remove("open"); });
    }
    /* 让 i18n 接管这一块（语言按钮的内容由 ui_i18n 的 bootLangBtn 填）。
       主动调一次：有些页把 OrpahI18n.apply() 放在 navInit() 之前，否则按钮会是空的。 */
    if (window.OrpahI18n && OrpahI18n.apply) OrpahI18n.apply(box);
    return true;
  }

  window.navInit = navInit;
  window.OrpahNav = { MAIN: MAIN, TOOLS: TOOLS, pageName: pageName };
})();
