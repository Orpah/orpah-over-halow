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

2026-09-13 追加两条（弹窗跳转 / 声音提醒）：

4. **`ALERT_LINK` 必须覆盖 `alerts.py` 里的全部 kind**：弹窗能“点进去看”靠的是这张表，
   漏一个 kind = 那条告警弹出来**点了没反应**（不报错、不提示，纯静默失效）。
   kind 清单**从 `alerts.py` 现抽**（`_alert("<kind>"`），不在这里另拄一份 ——
   否则加了新告警类型，测试仍然绿（这正是本条要防的）。
5. **声音提醒必须默认关**：`index.html` 的 `#notifySound` 不得带 `checked`（浏览器会把
   它当默认勾选）；`localStorage` 读的是 `=== "1"`（读不到 = 关）。
   需求就是“默认关，勾选开启”—— 悄悄改成默认开是范态度默认值变更，比功能坏更难受。

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
ALERTS_PY = os.path.join(HERE, "alerts.py")
INDEX_HTML = os.path.join(HERE, "ui", "static", "index.html")
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


def check_alert_links(src):
    """`ALERT_LINK` 必须覆盖 `alerts.py` 里出现的每一个 kind（kind 从 alerts.py 现抽）。"""
    if not os.path.exists(ALERTS_PY):
        print("  FAIL 找不到 alerts.py —— 无法校验跳转表覆盖（守卫对象变了要同步改）")
        return 1
    kinds = sorted(set(re.findall(r'_alert\(\s*"([a-z_]+)"',
                                  open(ALERTS_PY, encoding="utf-8").read())))
    if not kinds:
        print("  FAIL 从 alerts.py 抽不到任何 kind —— 取值方式变了（守卫失效，会假绿）")
        return 1
    m = re.search(r"const\s+ALERT_LINK\s*=\s*\{(.*?)\n\};", src, re.S)
    if not m:
        print("  FAIL 找不到 `ALERT_LINK` 表 —— 弹窗跳转的单一源没了")
        return 1
    table = m.group(1)
    have = set(re.findall(r"^\s*([a-z_]+)\s*:", table, re.M))
    missing = [k for k in kinds if k not in have]
    extra = sorted(have - set(kinds))
    bad = 0
    if missing:
        print(f"  FAIL ALERT_LINK 漏了 {len(missing)} 个 kind {missing} —— "
              "这些告警弹出来点了没反应（静默失效）")
        bad += 1
    if extra:
        print(f"  FAIL ALERT_LINK 里有多余/写错的 kind {extra} —— "
              "大概率是拼错（拼错的 key 永远匹配不上，等于白写）")
        bad += 1
    if not bad:
        print(f"  OK   ALERT_LINK 覆盖 alerts.py 全部 {len(kinds)} 个 kind，无多余项")
    # 跳转 URL 一律用 `alertLink()` 取（页面自己拼 URL 就是第二份映射）
    if src.count("alertLink(") < 1:
        print("  FAIL 找不到 `alertLink(` —— 表在但没被用来生成 URL")
        bad += 1
    return bad


def check_sound_default_off(src):
    """声音提醒必须**默认关**：HTML 不带 checked，读 localStorage 用 `=== \"1\"`。"""
    bad = 0
    if not os.path.exists(INDEX_HTML):
        print("  FAIL 找不到 index.html")
        return 1
    html = open(INDEX_HTML, encoding="utf-8").read()
    m = re.search(r"<input[^>]*id=\"notifySound\"[^>]*>", html)
    if not m:
        print("  FAIL index.html 里找不到 `#notifySound` 复选框")
        return 1
    if re.search(r"\bchecked\b", m.group(0)):
        print("  FAIL `#notifySound` 带 `checked` —— 声音就会默认开（需求是默认关、勾选才开）")
        bad += 1
    if 'localStorage.getItem(SOUND_KEY) === "1"' not in src:
        print("  FAIL app.js 里没有 `localStorage.getItem(SOUND_KEY) === \"1\"` —— "
              "默认关的口径没了（改写成“非空即开”会把 old 值当开）")
        bad += 1
    if not re.search(r"function\s+beep\s*\(", src) or "soundOn()" not in src:
        print("  FAIL 找不到 `beep()` / `soundOn()` —— 声音开关没接到弹窗路径上")
        bad += 1
    if not bad:
        print("  OK   声音提醒默认关（HTML 无 checked + localStorage 严格等于 1 才开）")
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
    print("== 告警弹窗跳转：ALERT_LINK 必须覆盖 alerts.py 全部 kind ==")
    fail += check_alert_links(src)
    print("== 声音提醒：默认关 ==")
    fail += check_sound_default_off(src)
    print("== 页面守卫（app.js 确实被加载） ==")
    fail += page_guard()
    if fail:
        print(f"app.js 转义守卫：有问题（{fail} 处）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
