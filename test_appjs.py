#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_appjs.py — 首页 `ui/static/app.js` 的**转义/单一出口**守卫（离线，不需要 node）
======================================================================================
为什么单独一套：2026-09-13 review 指出 `alertText()` **内部不转义**（值里含设备自报的
`sn`，来自未验签报文 → 那是攻击者可控内容），靠**调用点**各自 `esc()` 包一层。
当前两个调用点确实都包了，但“下一处忘了包”是个**静默 XSS**：不会报错、只是页面能被打穿。

**做法取“锁住只在一处转义”，而不是“内部再加一层 esc”**（后者会把 `&` 变成 `&amp;lt;`
—— 双重转义在页面上显示成字面量，是真 bug 而不是防御纵深）。于是本套件锁三件事：

1. `esc()` 本身覆盖 `& < > "`（少一个字符类就是一条注入路径，且不会有任何报错）；
2. **`alertText()` 的每一处调用点都用 `esc(...)` 包着**（定义处除外）；
3. **`alertText()` 函数体内不许出现 `esc(`** —— 转义只能发生在“写进 DOM 之前”这一个位置；
   谁要改成“内部转义”，必须同时改调用点与这里，是一次**有意**的改动（而不是悄悄叠一层）。

另：`refresh()` 的防重入是**故意不做**的（实测 `/api/status` 中位 4.2 ms，1 s 间隔余量 200×）
—— 那条口径写在 `app.js` 的注释与 `AGENTS.md` 里，本套件不锁（注释不是可执行契约）。

运行：C:\\Python313\\python.exe test_appjs.py
"""
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
APP_JS = os.path.join(HERE, "ui", "static", "app.js")
PAGES = ("index.html",)     # 只有首页加载 app.js（attack.html 用的是 attack.js，同约定但另一套）


def _line_of(src, pos):
    return src.count("\n", 0, pos) + 1


def check_esc_covers_all(src):
    """`esc()` 必须覆盖 `& < > "` 四个字符。"""
    m = re.search(r"function\s+esc\s*\([^)]*\)\s*\{([^}]*)\}", src, re.S)
    if not m:
        print("  FAIL 找不到 `esc()` 定义（转义唯一实现必须在 app.js 里）")
        return 1
    body = m.group(1)
    need = ["&", "<", ">", '"']
    missing = [c for c in need if f'"{c}"' not in body.replace("'", '"')]
    if missing:
        print(f"  FAIL `esc()` 里少了 {missing} 的映射 —— 少一个字符类就是一条注入路径，"
              "而且不会报错。")
        return 1
    print("  OK   `esc()` 覆盖 & < > \" 四个字符")
    return 0


def check_alerttext_call_sites(src):
    """`alertText(` 的每处调用都必须被 `esc(...)` 包着；函数体内不许自己转义。"""
    bad = 0
    calls = 0
    for m in re.finditer(r"alertText\(", src):
        before = src[max(0, m.start() - 16):m.start()]
        if before.rstrip().endswith("function"):
            continue                       # 定义处
        calls += 1
        if not before.rstrip().endswith("esc("):
            print(f"  FAIL app.js:{_line_of(src, m.start())} `alertText(...)` 没有被 `esc(...)` 包住"
                  " —— alertText 的值里含设备自报字段（攻击者可控），漏包就是静默 XSS")
            bad += 1
    if calls == 0:
        print("  FAIL 一处 `alertText(` 调用都没找到 —— 守卫失效了（函数被改名/删了？）")
        return 1
    if not bad:
        print(f"  OK   {calls} 处 `alertText(...)` 调用全部由 `esc(...)` 包住")
    # 函数体内部不许转义（否则调用点再包一次 = 双重转义，页面上会显示成字面量 &amp;lt;）
    m = re.search(r"function\s+alertText\s*\([^)]*\)\s*\{(.*?)\n\}", src, re.S)
    if not m:
        print("  FAIL 取不到 `alertText()` 函数体（守卫需要跟着改）")
        return bad + 1
    if "esc(" in m.group(1):
        print("  FAIL `alertText()` 函数体里出现了 `esc(` —— 转义只能有一处（写进 DOM 之前）。"
              "要改成内部转义，就把调用点的 `esc()` 去掉并同步改本守卫。")
        return bad + 1
    print("  OK   `alertText()` 体内不转义（转义只发生在写进 DOM 那一步，不会双重转义）")
    return bad


def page_guard():
    """两页都真的加载了 app.js（否则守卫测的是没人用的文件）。"""
    bad = 0
    for page in PAGES:
        p = os.path.join(HERE, "ui", "static", page)
        if not os.path.exists(p):
            continue
        html = open(p, encoding="utf-8").read()
        if 'src="app.js' not in html:
            print(f"  FAIL {page}：没有加载 app.js（本套件守卫的是它的转义口径）")
            bad += 1
    if not bad:
        print("  OK   index.html 加载 app.js（守卫对象确实在用）")
    return bad


def main():
    if not os.path.exists(APP_JS):
        print("  FAIL 找不到 ui/static/app.js")
        print("app.js 转义守卫：有问题（1 处）")
        return 1
    src = open(APP_JS, encoding="utf-8").read()
    print("== 转义唯一实现（esc 覆盖字符） ==")
    fail = check_esc_covers_all(src)
    print("== alertText：调用点必须包 esc、体内不许再包 ==")
    fail += check_alerttext_call_sites(src)
    print("== 页面守卫（app.js 确实被加载） ==")
    fail += page_guard()
    if fail:
        print(f"app.js 转义守卫：有问题（{fail} 处）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
