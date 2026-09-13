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


def check_energy_calib(src):
    """能量卡的**标定出处**（2026-09-13）：三条容易静默失效的接线。

    ① 开页必须主动拉一次 `/api/energy` —— 整表/算式/溯源/错误**只在那里**（1s 轮询只带
       徽标要的几个数）。实测踩过：不拉 → “标定项与出处”展开是**空的**，看着像没做
       （不会报错，也不影响其它任何东西变红）。
    ② `renderCalib()` 必须被 `renderEnergy()` 调到（否则徽标只在刷新时变色、正文不动）。
    ③ 页面**不许写第二份字段表**：字段名清单在后端 `energy_calib.FIELD_IDS`（带 i18n 键、
       单位、小数位、算式），页面只渲染 `rows` —— 页面里出现这些字段名字面量就是拄了第二份。
    """
    import energy_calib as ecal                     # 单一源：字段清单从模块现取
    bad = 0
    m = re.search(r"function\s+renderEnergy\s*\([^)]*\)\s*\{(.*?)\n\}", src, re.S)
    body = m.group(1) if m else ""
    if "renderCalib(" not in body:
        print("  FAIL renderEnergy() 里没调 renderCalib() —— 标定徽标/正文不会随轮询更新")
        bad += 1
    if not re.search(r"function\s+fetchEnergy\s*\(", src):
        print("  FAIL 找不到 `fetchEnergy()` —— 完整视图没人拉（标定表会是空的）")
        bad += 1
    # 初始化处（`connect();` 之后）必须拉一次
    init = src.rfind("connect();")
    if init < 0 or "fetchEnergy()" not in src[init:init + 200]:
        print("  FAIL 开页没有调用 `fetchEnergy()` —— 标定表内容只在用户手点后才出现")
        bad += 1
    if "cb.rows" not in src:
        print("  FAIL 没看到渲染 `cb.rows` —— 字段表是后端给的单一源，页面不能自己拼")
        bad += 1
    dup = [f for f in ecal.FIELD_IDS if f'"{f}"' in src]
    if dup:
        print(f"  FAIL app.js 里出现了标定字段名字面量 {dup} —— 页面拄了第二份字段表")
        bad += 1
    if not bad:
        print("  OK   标定出处接线完整（开页拉一次 / renderCalib 挂在 renderEnergy 上 / 字段表只有后端一份）")
    return bad


def check_energy_cover(src):
    """覆盖（缺口/曲线）的接线守卫（2026-09-13 加）。

    这条也是“静默空白”型的坑：`renderCover()` 没被调，覆盖块就是一片空 —— 不报错、
    不影响其它任何测试；而用户看到的只是“这块没做”。另两条：
    ① 页面输入用**小时**、模型用**秒**，换算只能在提交处做一次（写反了缺口就变 36 倍）；
    ② 覆盖块必须读后端算好的 `state.cover`，不许页面自己算份数（否则两处漂）。
    """
    bad = 0
    m = re.search(r"function\s+renderEnergy\s*\([^)]*\)\s*\{(.*?)\n\}", src, re.S)
    if "renderCover(" not in (m.group(1) if m else ""):
        print("  FAIL renderEnergy() 里没调 renderCover() —— 覆盖块会是空白（看着像没做）")
        bad += 1
    if "st.cover" not in src or "function renderCover" not in src:
        print("  FAIL 覆盖块没读后端的 `state.cover`（页面不许自己算）")
        bad += 1
    if "gap_s: (parseFloat($(\"enGap\").value) || 0) * 3600" not in src:
        print("  FAIL 缺口输入（小时）→ gap_s（秒）的换算不在提交处/写反了")
        bad += 1
    for el in ("#enGap", "#enCover"):
        if el.lstrip("#") not in src:
            print("  FAIL app.js 里没有用到 %s" % el)
            bad += 1
    if not bad:
        print("  OK   覆盖接线完整（renderCover 挂在 renderEnergy 上 / 只读后端算好的 state.cover / 小时→秒）")
    return bad


def check_energy_tier(src):
    """设计常态三档（2026-09-14）的接线守卫。

    “常态 60 s”是个**一等基准**：页面要能一眼分出「在常态 / 已降速 / 跟不住」——
    而它坏起来同样是静默的：没画标记 = 看着一切正常（跟“够用”一模一样）。
    所以钉三件事：
      ① 三档映射只有一份（`EN_TIER`），**不要**在状态格与扫描表各写一套措辞；
      ② 两处都真的用了它（少一处 = 那处根本没有常态信息）；
      ③ “要不要降级换常态”只**陈述**（`to_reach_normal`），页面不许自动降级/替用户选。
    """
    bad = 0
    if src.count("function EN_TIER") != 1:
        print("  FAIL 常态映射 `EN_TIER` 不是恰好一份（多份就会漂）")
        bad += 1
    if src.count("EN_TIER(") < 3:          # 1 处定义 + 状态格 + 扫描表
        print(f"  FAIL `EN_TIER(` 只用 {src.count('EN_TIER(')} 处（状态格与扫描表都要带常态标记）")
        bad += 1
    if "to_reach_normal" not in src:
        print("  FAIL 页面没用到后端的 `to_reach_normal`（降级换常态这条路要摆出来）")
        bad += 1
    with open(INDEX_HTML, encoding="utf-8") as f:
        html = f.read()
    if 'data-i18n="en_th_tier"' not in html:
        print("  FAIL 扫描表缺「常态」表头（en_th_tier）—— 多了列就得有表头")
        bad += 1
    if not bad:
        print("  OK   常态三档接线完整（单一映射 / 状态格+扫描表都带 / 降级备选只陈述）")
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
    print("== 能量标定出处：开页拉整表 / 单一字段表 ==")
    fail += check_energy_calib(src)
    print("== 覆盖（缺口/曲线）：接线不能静默空白 ==")
    fail += check_energy_cover(src)
    print("== 设计常态三档：状态格与扫描表都要带标记 ==")
    fail += check_energy_tier(src)
    print("== 页面守卫（app.js 确实被加载） ==")
    fail += page_guard()
    if fail:
        print(f"app.js 转义守卫：有问题（{fail} 处）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
