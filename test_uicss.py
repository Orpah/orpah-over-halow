#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_uicss.py — UI 样式/窄屏守卫（离线，纯文本检查，不需要浏览器）
======================================================================
为什么单独一个套件：**布局坏掉是"看不见的回归"** —— 排版塌了不会让任何测试变红，
只会让现场用手机的人（这套系统真正的使用场景）根本用不了。而这类回归往往就来自
一两行 CSS/内联样式，所以用最笨的办法把它们钉住。

背景（2026-09-13 实测）：`style.css` 里原来写着 `body { min-width: 1180px }`
（当年为了让首页拓扑单行不折行）→ 实测 390px 宽的手机上页面宽 1180px，**要横向拖 3 屏**；
控件高度普遍 28~31px（触屏点不准，通行下限 ~44px）；还有若干内联 `style="min-width:320px"`
（内联样式**会赢过样式表**，窄屏必撑宽）。现在改成"谁的宽谁自己滚"+900px 断点，
本套件守住这几条。

锁住的事（都是客观事实，不是审美）：

1. **不许给 body 设固定最小宽**（≥1024px）—— 那就是"整站横向拖"的根因；
2. **必须有窄屏断点**（`@media (max-width: …)`），且断点里要把控件抬到可点高度（≥40px）；
3. **页面里不许出现会撑宽的内联宽度**（`style="…min-width:≥200px"` 或 `width:≥320px"`）——
   要宽控件用 `.w-sm/.w-md/.w-lg`（`style.css` 里带 `max-width:100%`）；内联样式优先级更高，
   这里不给它留口子；
4. **每页三件套**：`<meta name="viewport">`（缺了手机按 980px 视口渲染 = 白做响应式）、
   `<title>`、`style.css` + `ui_i18n.js`（文案与样式都从共享件来，不各写一份）；
5. **按钮不换行**（`.btn { white-space: nowrap }`）—— 窄屏上把「回放」挤成两行，踩过。

运行：C:\\Python313\\python.exe test_uicss.py
"""
import glob
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
STATIC = os.path.join(HERE, "ui", "static")
CSS = os.path.join(STATIC, "style.css")
NAV = os.path.join(STATIC, "nav.js")

FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}" + (f"   {extra}" if extra else ""))
        FAILS.append(name)
    return cond


def body_block(css):
    """取 `body { ... }` 那一段（只看最外层，不追嵌套）。
    注意：调用方已经把注释剥掉了 —— 否则注释里写“以前是 min-width: 1180px”会被当成违规
    （本套件第一版就栽在这：报了一个实际已经不存在的值）。"""
    m = re.search(r"^body\s*\{(.*?)^\}", css, re.S | re.M)
    return m.group(1) if m else ""


def strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def check_css():
    css = strip_comments(open(CSS, encoding="utf-8").read())
    print("== style.css：页面不再被写死宽度、窄屏档位齐全 ==")

    # 1) body 不许有 ≥1024px 的 min-width（那是整站横向滚的根因）
    bad = [int(x) for x in re.findall(r"min-width:\s*(\d+)px", body_block(css))
           if int(x) >= 1024]
    check("body 没有固定最小宽（≥1024px）—— 否则窄屏整站横向拖", not bad, bad)

    # 2) 必须有窄屏断点
    bps = [int(x) for x in re.findall(r"@media\s*\(max-width:\s*(\d+)px", css)]
    check("有窄屏断点（@media max-width）", bool(bps), bps)

    # 3) 断点里要把控件抬到可点高度
    mob = ""
    for m in re.finditer(r"@media\s*\(max-width:\s*\d+px\)\s*\{(.*?)\n\}", css, re.S):
        mob += m.group(1)
    heights = [int(x) for x in re.findall(r"min-height:\s*(\d+)px", mob)]
    check("窄屏断点里控件最小高度 ≥40px（触屏可点）",
          bool(heights) and min(heights) >= 40, heights[:6])
    check("窄屏断点覆盖 .btn（按钮也要可点）", ".btn" in mob)

    # 4) 按钮不换行（窄屏挤成两行踩过）
    m = re.search(r"\.btn\s*\{(.*?)\}", css, re.S)
    check("`.btn` 设了 white-space: nowrap（按钮文字不换行）",
          bool(m) and "nowrap" in m.group(1))

    # 5) 宽控件类必须带 max-width（否则窄屏照样撑宽）
    for cls in (".w-md", ".w-lg"):
        m = re.search(re.escape(cls) + r"\s*\{([^}]*)\}", css)
        check(f"{cls} 带 max-width: 100%", bool(m) and "max-width: 100%" in m.group(1))

    # 6) 常态三档的颜色必须对**状态格**也生效（2026-09-14 实测踩过）：
    #    `.id-row span { color: var(--mut) }`（0,1,1）会赢过单个类选择器（0,1,0）→
    #    徽标静默变成灰色，“在常态/已降速/跟不住”三档看不出区别（表格里却是对的）。
    missing = [c for c in (".tier-ok", ".tier-slower", ".tier-too-slow", ".tier-silent")
               if (".id-row " + c) not in css]
    check("常态三档颜色对状态格也生效（`.id-row .tier-*`，否则徽标静默变灰）",
          not missing, missing)


def check_hierarchy():
    """信息层级（UI ③）的守卫：长口径必须收进折叠、折叠标签成对、长列表要限高。

    为什么用文本检查：这几条坏掉同样**不会让任何测试变红**，但现场体验直接崩 ——
    卡片里塞 2000 字说明，看的人得滚三屏才见到表；`<details>` 少一个闭合标签则整段
    后续排版串位（本次实测就漏过一个 `</details>`，页面上看不出来，只能靠数标签）。
    """
    print("== 信息层级：长口径折叠 + 标签成对 + 长列表限高 ==")
    css = strip_comments(open(CSS, encoding="utf-8").read())
    check("style.css 有 `details.fold` 样式（折叠块）", "details.fold" in css)
    check("style.css 有 `.scroll-y`（长列表限高滚动）", ".scroll-y" in css)

    # 折叠块里的关键字（都是 >300 字的“口径/图例”类说明；现场先看数据，要抠再展开）
    folded = {
        "index.html": ["sl_hint", "rl_hint", "notify_why", "en_cal_foot", "en_cov_foot"],
        "track.html": ["tk_devmul_hint", "tk_act_hint"],
        "replay.html": ["rp_legend", "rp_legend_map", "rp_bias_hint", "rp_trust_hint"],
    }
    bad_fold, bad_nest, dup = [], [], []
    for name, keys in folded.items():
        html = open(os.path.join(STATIC, name), encoding="utf-8").read()
        # 标签成对（多/少一个都会静默串位）
        if html.count("<details") != html.count("</details>"):
            bad_nest.append((name, html.count("<details"), html.count("</details>")))
        for k in keys:
            needle = f'data-i18n="{k}"'
            if html.count(needle) != 1:
                dup.append((name, k, html.count(needle)))
                continue
            i = html.index(needle)
            # 最近一个 <details 出现在最近一个 </details> 之后 → 这段文字在折叠块里面
            if html.rfind("<details", 0, i) < html.rfind("</details>", 0, i):
                bad_fold.append((name, k))
    check("长口径说明都在 <details class=\"fold\"> 里面（每处只一份）",
          not bad_fold and not dup, bad_fold + dup)
    check("每页 <details> 与 </details> 数量相等（漏一个会静默串位）", not bad_nest, bad_nest)

    # 长列表限高：replay 的两个列表（时间轴 / 报文流）用页面内 `ul.tl`，
    # 首页两张长表用 style.css 的 `.scroll-y`。
    rp = open(os.path.join(STATIC, "replay.html"), encoding="utf-8").read()
    tl = css_rule(rp.replace("\n", " "), "ul.tl")
    check("replay 两个长列表（时间轴 / 报文流）都限高滚动",
          rp.count('class="tl"') >= 2 and "max-height" in tl, tl[:60])
    idx = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
    check("首页长表用了 .scroll-y", idx.count('class="scroll-y"') >= 2, idx.count('class="scroll-y"'))


def check_escaping():
    """服务端来的字符串**不许拼进 innerHTML**（用 textContent）。

    起因（2026-09-13 审查）：人级聚合的表格一开始把 `d.sn` 直接拼进 `tr.innerHTML`。
    SN 是**服务端数据**（清册里可改/可从上报来），而那次审查只核了“已有代码都 esc 过”，
    没看出新加的这一格没转义 —— 这类“新代码自己开了个口子”正是要防的。
    这里只钉**这一格**（`textContent = d.sn`），不搞全页启发式（那会一堆误报）。
    """
    print("== 转义口径：服务端字符串不拼进 innerHTML ==")
    p = os.path.join(STATIC, "track.html")
    src = open(p, encoding="utf-8").read()
    check("人级聚合表的 SN 用 textContent 填（不拼 innerHTML）",
          "tdSn.textContent = d.sn" in src and "+ d.sn +" not in src)
    # 精确到“有没有被拼进 HTML”，**不**数出现次数、也不禁 `${d.sn}` 本身 ——
    # `o.textContent = \`${d.sn} · …\`` 是安全的（textContent 不解析 HTML），
    # 那种计数/关键词式守卫只会制造误报（本节第一版就误报了一次）。
    bad = [l.strip()[:70] for l in src.splitlines()
           if "innerHTML" in l and "d.sn" in l]
    check("SN 没有被拼进 innerHTML（用 textContent 是允许的）", not bad, bad)


def check_inline_js():
    """每页**内联脚本**的语法必须能过 `node --check`（2026-09-13 真踩过）。

    为什么必须有一条：内联 `<script>` 里一个语法错（本次是复制代码时多出一行
    `function renderPerson(f) {`）会让**整块脚本一个字都不执行** —— 页面照样渲染、
    不报错到明处，只是所有动态内容都是空的、点按钮没反应（最容易被当成“后端挂了”）。
    静态文本检查看不出来，所以这里把内联脚本抠出来交给 node 解析。
    本机没有 node → 明确跳过（打印 SKIP，不当通过）。
    """
    print("== 页面内联脚本：node --check ==")
    node = shutil.which("node")
    if not node:
        print("  SKIP 本机没有 node —— 内联脚本语法未验证（**不算通过**）")
        return False
    bad = []
    for p in sorted(glob.glob(os.path.join(STATIC, "*.html"))):
        html = open(p, encoding="utf-8").read()
        for i, body in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
            if not body.strip():
                continue
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                             encoding="utf-8") as fh:
                fh.write(body)
                tmp = fh.name
            try:
                r = subprocess.run([node, "--check", tmp], capture_output=True,
                                   encoding="utf-8", errors="replace")
            finally:
                os.unlink(tmp)
            if r.returncode != 0:
                msg = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
                bad.append((os.path.basename(p), i, msg[-1] if msg else "?"))
    check("每页内联脚本语法正确（错一处 = 整页 JS 全不执行）", not bad, bad[:3])
    return True


def check_narrow_grids():
    """窄屏：页面内 `<style>` 里定义的**多列网格**必须被收成单列（UI ⑤）。

    为什么这条容易漏：这些网格写在**各页自己的 `<style>`** 里，而 `<style>` 在文档流里
    **晚于** `style.css` → 同优先级下它会赢过样式表（有没有媒体查询都一样）。
    所以 `style.css` 的窄屏档必须用 `body ` 前缀提高一级优先级才盖得住 ——
    这一条不看的话，手机上两列各 ~150px，数字/单位会拆行、textarea 只剩几十 px。
    """
    print("== 窄屏：页面局部多列网格要收成单列 ==")
    css = strip_comments(open(CSS, encoding="utf-8").read())
    narrow = ""
    for m in re.finditer(r"@media\s*\(max-width:\s*(\d+)px\)\s*\{(.*?)\n\}", css, re.S):
        if int(m.group(1)) == 900:                 # 档位固定 900px（见 check_css）
            narrow += m.group(2)
    # 窄屏档里的规则拆成 (selector, body)，才能处理「一条规则盖多个类」的写法
    narrow_rules = [(m.group(1).strip(), m.group(2))
                    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", narrow)]
    miss = []
    for p in sorted(glob.glob(os.path.join(STATIC, "*.html"))):
        name = os.path.basename(p)
        html = open(p, encoding="utf-8").read()
        for m in re.finditer(r"([^{}]+)\{([^}]*)\}", html):
            body = m.group(2)
            g = re.search(r"grid-template-columns:\s*([^;]+)", body)
            if not g:
                continue
            val = g.group(1).strip()
            # 单列（`1fr` / `100%` / 单个值）不算多列；`repeat(auto-fit…)` 算
            if val in ("1fr", "100%") or ("repeat" not in val and len(val.split()) < 2):
                continue
            for cls in set(re.findall(r"\.([A-Za-z0-9_-]+)", m.group(1))):
                ok = any(re.search(r"\.%s\b" % re.escape(cls), sel)
                         and "grid-template-columns: 1fr" in rbody
                         and "body " in sel      # 必须有 `body ` 前缀：否则盖不住页面内的 `<style>`
                         for sel, rbody in narrow_rules)
                if not ok:
                    miss.append((name, cls, val[:40]))
    check("每个页面局部多列网格，在 style.css 的 900px 档里都有单列覆盖（且带 `body ` 前缀）",
          not miss, miss)


def css_rule(css, sel):
    m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", css)
    return m.group(1) if m else ""


def check_pages():
    print("== 页面：内联宽度、viewport、共享件 ==")
    pages = sorted(glob.glob(os.path.join(STATIC, "*.html")))
    check("找到页面文件（≥10 个）", len(pages) >= 10, len(pages))
    inline_wide, no_vp, no_title, no_shared = [], [], [], []
    for p in pages:
        name = os.path.basename(p)
        html = open(p, encoding="utf-8").read()
        # 内联宽度：min-width ≥200px 或 width ≥320px（内联会赢过样式表 → 窄屏撑宽）
        for m in re.finditer(r'style="([^"]*)"', html):
            s = m.group(1)
            bad_min = [int(x) for x in re.findall(r"min-width:\s*(\d+)px", s) if int(x) >= 200]
            bad_w = [int(x) for x in re.findall(r"(?<!max-)width:\s*(\d+)px", s) if int(x) >= 320]
            if bad_min or bad_w:
                inline_wide.append((name, s.strip()[:48]))
        if 'name="viewport"' not in html:
            no_vp.append(name)
        if "<title>" not in html:
            no_title.append(name)
        if "style.css" not in html or "ui_i18n.js" not in html:
            no_shared.append(name)
    check("没有会把卡片撑宽的内联宽度（改用 .w-sm/.w-md/.w-lg）", not inline_wide, inline_wide[:4])
    check("每页都有 viewport meta（手机按真实宽度渲染）", not no_vp, no_vp)
    check("每页都有 <title>", not no_title, no_title)
    check("每页都加载共享 style.css + ui_i18n.js", not no_shared, no_shared)


def check_nav():
    """头部导航单一源（nav.js）的守卫。

    踩过的：导航以前**每页手写一份** → 有的页到不了（工具页只有首页能进）、
    同一链接在不同页顺序/文案不一致、新增页面漏改某页就是「点了回不来」。
    """
    print("== 头部导航：单一源（nav.js）+ 没有孤岛页面 ==")
    nav = open(NAV, encoding="utf-8").read()
    pages = sorted(glob.glob(os.path.join(STATIC, "*.html")))
    no_nav, no_script, no_init, hand_written = [], [], [], []
    for p in pages:
        name = os.path.basename(p)
        html = open(p, encoding="utf-8").read()
        if 'id="nav"' not in html:
            no_nav.append(name)
        if "nav.js" not in html:
            no_script.append(name)
        if "navInit(" not in html and not name.startswith("attack"):
            # attack 页的调用在 attack.js 里（页面逻辑不在 app.js，同约定）
            no_init.append(name)
        # 手写导航链接：data-i18n="nav_*" 出现在页面里 → 又变成两份了
        if re.search(r'data-i18n="nav_', html):
            hand_written.append(name)
    check("每页都有导航容器 <div id=\"nav\">", not no_nav, no_nav)
    check("每页都加载 nav.js", not no_script, no_script)
    check("每页都调 navInit()（attack 页在 attack.js 里）", not no_init, no_init)
    check("页面里没有手写的导航链接（导航只能来自 nav.js）", not hand_written, hand_written)

    # 孤岛页面：每个 html 都必须在 nav.js 里登记（否则从界面上到不了）
    listed = set(re.findall(r'href:\s*"([A-Za-z0-9_]+\.html)"', nav))
    have = {os.path.basename(p) for p in pages}
    missing = sorted(have - listed)
    extra = sorted(listed - have)
    check("每个页面都在 nav.js 里登记了（无孤岛页面）", not missing, missing)
    check("nav.js 里没有指向不存在页面的链接", not extra, extra)
    check("nav.js 标出当前页（aria-current=page）", 'aria-current="page"' in nav)
    check("style.css 给当前页做了样式（.nav-link.cur）", ".nav-link.cur" in open(CSS, encoding="utf-8").read())


def main():
    check_css()
    check_pages()
    check_nav()
    check_hierarchy()
    check_narrow_grids()
    check_escaping()
    check_inline_js()
    if FAILS:
        print(f"UI 样式守卫：有问题（{len(FAILS)} 处）")
        return 1
    print("UI 样式守卫：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
