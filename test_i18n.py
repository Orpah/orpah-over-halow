#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_i18n.py — 文案字典自检（`ui/static/ui_i18n.js` ↔ 页面引用）

为什么单独一个套件：字典与页面是**两份东西**，改一边不改另一边时——
  · 页面写了 `data-i18n="xxx"` / `T("xxx")` 而字典里没有 → 页面上直接显示成 `xxx`（英文界面尤其明显）；
  · 字典里 zh 有、en 没有（或反之）→ 切到英文时漏成 key 名；
  · 同一个 key 写两遍（本仓历史真出现过 `th_status` 重复两次）→ 后面那份静默覆盖前面那份。
三种都不会报错、只会"看着有点怪"，所以用这个套件把它们钉住。

口径：
  1. `ui_i18n.js` 里 zh / en 两块的 key 集合**完全一致**，且每个 key 恰好出现 2 次（zh 一次 + en 一次）；
  2. `ui/static/` 下所有页面/脚本引用到的 key（`data-i18n*` 属性 + `T("k")`/`t("k")` 调用 +
     **动态前缀家族**，如 `T("trust_" + v)` / `tOr("pow_", v)`）都必须在字典里；
  3. 用 node 做一次语法检查（字典是经典脚本，语法错了整页文案全失效）。

2026-09-12：本字典原是 halow-demo 与 orpah 共用的一份，拆分后各持一份 —— 这个套件的第 2 条
正是"拆完别少 key"的保险。
"""
import os
import re
import shutil
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "ui", "static")
DICT = os.path.join(STATIC, "ui_i18n.js")

FAILS = []


def check(name, cond, extra=""):
    if cond:
        print("PASS  " + name)
    else:
        print("FAIL  " + name + ("   " + str(extra) if extra else ""))
        FAILS.append(name)


# key 字符集要放宽：字典里还有 AT 帮助键（`qt:AT+RST` / `at:def:AT+MODE=` / `at:v2:AT+PING=`）
RE_KEY = re.compile(r'^\s*"([^"]+)"\s*:', re.M)


def load_dict():
    txt = open(DICT, encoding="utf-8").read()
    i_zh = next(i for i, l in enumerate(txt.split("\n")) if l.strip().startswith("zh: {"))
    i_en = next(i for i, l in enumerate(txt.split("\n")) if l.strip().startswith("en: {"))
    lines = txt.split("\n")
    zh = [RE_KEY.match(l).group(1) for l in lines[i_zh + 1:i_en] if RE_KEY.match(l)]
    en = [RE_KEY.match(l).group(1) for l in lines[i_en + 1:] if RE_KEY.match(l)]
    return txt, zh, en


RE_STR = re.compile(r'''(["'])((?:\\.|(?!\1)[^\\\n])*)\1''')


def collect_refs():
    """页面/脚本引用到的 key（含动态前缀家族）。"""
    refs, prefs, files = set(), set(), 0
    for dirpath, dirnames, filenames in os.walk(STATIC):
        dirnames[:] = [d for d in dirnames if d not in ("vendor", "__pycache__", "uploads")]
        for fn in filenames:
            if not fn.endswith((".html", ".js")) or fn == "ui_i18n.js":
                continue
            files += 1
            blob = open(os.path.join(dirpath, fn), encoding="utf-8").read()
            refs |= set(re.findall(r'data-i18n(?:-title|-ph|-doc-title)?\s*=\s*"([^"]+)"', blob))
            refs |= set(re.findall(r'(?:\bT|\bt|\bfmt)\(\s*"([^"]+)"', blob))
            # 前缀候选：**正确配对**的字符串字面量（用反向引用）。
            # 踩过：写成 `["']([^"'\n]+)["']` 会跨越引号配对（从 `" · "` 的右引号匹配到
            # `"trust_"` 的左引号），于是 `trust_` 这种前缀永远抽不出来。
            prefs |= {m.group(2) for m in RE_STR.finditer(blob)}
    return refs, prefs, files


def main():
    txt, zh, en = load_dict()
    keys = set(zh) | set(en)
    check("字典 zh/en key 集合一致（%d / %d）" % (len(set(zh)), len(set(en))),
          set(zh) == set(en), sorted(set(zh) ^ set(en)))
    check("每个 key 恰好 2 次（zh + en）", len(zh) + len(en) == 2 * len(keys),
          "zh %d + en %d vs %d" % (len(zh), len(en), 2 * len(keys)))
    check("字典非空且含中文与英文条目（翻译真的两套）",
          "conn_CONNECTED" in keys and len(keys) > 100, len(keys))

    refs, prefs, nfiles = collect_refs()
    # 动态前缀：任何 >=2 个 key 以其开头的字面量都展开成整族（页面用 "trust_" + v 拼 key）
    fams = set()
    for p in prefs:
        if p in keys:
            continue
        # 宁洒毋漏：前缀门槛放宽只会“多留几个 key”（无害），收窄会导致“页面引用的 key 不在字典里”。
        ok = ("_" in p and len(p) >= 4) or (":" in p and len(p) >= 3)
        if not ok:
            continue
        f = {k for k in keys if k.startswith(p)}
        if len(f) >= 2:
            fams.add(p)
            refs |= f
    missing = sorted(refs - keys - fams)
    check("扫描 %d 个页面/脚本，引用 %d 个 key（含动态前缀家族 %d 个）"
          % (nfiles, len(refs), len(fams)), nfiles > 0)
    check("页面引用的 key 全部在字典里", not missing, missing)

    # 页面把 i18n 文案当**纯文本**渲染（不是 markdown）→ 值里写 `**加粗**` 会原样显示成星号
    # （2026-09-13 实测踩过：中文句子里露出 `**本系统不做密钥轮换**`）。源码注释/文档里仍可用
    # Markdown，这条只管**文案**。同样检查 HTML 里 data-i18n 的兜底文案（它是字典值的副本）。
    bad_val = [l.strip()[:70] for l in txt.split("\n") if RE_KEY.match(l) and "**" in l]
    check("i18n 文案值里不含 Markdown 标记（**）", not bad_val, bad_val[:5])
    bad_html = []
    for fn in sorted(os.listdir(STATIC)):
        if not fn.endswith(".html"):
            continue
        with open(os.path.join(STATIC, fn), encoding="utf-8") as f:
            for l in f:
                if "data-i18n" in l and "**" in l:
                    bad_html.append(fn + ": " + l.strip()[:70])
    check("页面上 data-i18n 的兜底文案也不含（**）", not bad_html, bad_html[:5])

    # ★ 服务端**下发**的文案键也要查（2026-09-13 新增）：
    # `/api/alerts` 的告警 `msg` 与 `/api/*.thresholds` 的 `i18n` 都是**服务端给的键名**，
    # 页面按数据里的键去查字典 —— 页面源码里没有字面量 → 上面那套「页面引用扫描」**查不出来**。
    # 拼错一个字母就会在页面上原样显示成 `alert_th_rssi_jump`（看着像功能没做）。
    # 来源两条（都是单一源）：① `alerts.THRESHOLDS` 的 i18n 字段；
    # ② `alerts.py` 里每个 `_alert(...)` 调用的第 4 个实参（文案键）。
    srv_keys = set()
    try:
        import alerts as alr                                    # noqa: E402
        srv_keys |= {x[1] for x in alr.THRESHOLDS}    # (key, i18n, unit) 三元组
        with open(os.path.join(HERE, "alerts.py"), encoding="utf-8") as f:
            src = f.read()
        i = 0
        while True:
            i = src.find("_alert(", i)
            if i < 0:
                break
            m = re.search(r'"(alert_[a-z_]+)"', src[i:i + 600])   # 第 4 个实参在附近
            if m:
                srv_keys.add(m.group(1))
            i += 7
    except Exception as e:                     # 拿不到就**明确跳过**，不当通过
        print("SKIP  服务端文案键检查（%s: %s）" % (type(e).__name__, e))
    if srv_keys:
        miss = sorted(srv_keys - keys)
        check("服务端下发的 %d 个文案键都在字典里（告警 msg + 阈值标签）"
              % len(srv_keys), not miss, miss)

    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "--check", DICT], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        check("node 语法检查通过", r.returncode == 0, (r.stderr or "")[:200])
    else:
        print("SKIP  node 不在 PATH 上，跳过语法检查")

    print()
    if FAILS:
        print("文案字典：%d 项失败：%s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("文案字典：全部通过（%d 个 key，引用 %d 个）" % (len(keys), len(refs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
