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
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "ui", "static")
CSS = os.path.join(STATIC, "style.css")

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


def main():
    check_css()
    check_pages()
    if FAILS:
        print(f"UI 样式守卫：有问题（{len(FAILS)} 处）")
        return 1
    print("UI 样式守卫：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
