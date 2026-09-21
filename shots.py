# -*- coding: utf-8 -*-
"""把 UI 的**真实页面**截成 README 用的图（可重出、带自检）。

为什么要有这个脚本：README 要「图文并茂」，界面图必须是**真实页面**（不是手绘 UI）；
手截的图会过时、也没人知道怎么重出 —— 所以页面/区域/等待全写成下面那张 `SHOTS` 表。

    # 1) 另开一个终端起 UI（--every 2 = 演示加速；设计常态是 60 s）
    python ui_server.py --every 2 --no-browser
    # 2) 出图（默认写到 docs/images/）
    python shots.py
    python shots.py --only ui-track        # 只重出某一两张（名字子串匹配）
    python shots.py --base http://127.0.0.1:8901

**自检**：每张图都要**真有内容** —— 用 PIL 量「亮像素占比」，低于 `MIN_INK` 直接报错退出（退出码 1）。
踩过的坑：渲染面太小 / 页面还没画完时，截图会变成「左上角一小块 + 其余全黑」，
那种图是垃圾却不会自己报错，所以必须在这里拦住（判据 = 亮像素占比 + 文件尺寸）。

**不动服务端状态**：只截静态内容，或跑页面内的本地模拟（track 的「开始」）；
**不要**在这里点「跑全部攻击」—— 攻击清单里的 `revoked` 用例会改密钥库状态（见 AGENTS §0）。
"""
import sys
import pathlib
import argparse

from PIL import Image
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DEFAULT = "http://127.0.0.1:8901"
OUT_DIR = pathlib.Path(__file__).resolve().parent / "docs" / "images"
VIEW = {"width": 1280, "height": 900}
SVG_DSF = 1                    # 1 = 图就是 CSS 像素；README 里更轻
SETTLE_MS = 4500               # 打开页面后等首绘 + 首批数据
ACTION_WAIT_MS = 2500          # 点了按钮之后再等一会儿
MIN_INK = 0.008                # 亮像素占比下限（0.8%）—— 见文件头「自检」
BRIGHT = 24                    # 灰度 > 此值算「有内容」（页面是深色主题）

# 页面, 目标选择器（None = 整屏）, 输出文件名, 截图前动作, 动作后额外等待, 需要的能力, 说明
SHOTS = [
    ("/", "#topology", "ui-index-topology.png", None, 0, None,
     "首页 · 三层拓扑（客户端→空口→路由器→UDP→服务器）与按内容分色的计数"),
    ("/", "#rlsec", "ui-index-rl.png", None, 0, None,
     "首页 · 限频三段：Server（省 CPU）/ Router（省带宽）/ 设备侧（自愿延后，不是防线）"),
    ("/track.html", "#canvas", "ui-track.png", ("click", "#btnRun"), 9000, None,
     "定位与轨迹 · 页面内本地模拟跑一遍（蓝方块=站位、绿线=真实轨迹、红线=估计轨迹、黄虚线=95% 椭圆）"),
    ("/replay.html", "#canvasReplay", "ui-replay.png", None, 6000, "tsdb",
     "回放 · 轨迹与搜索半径（读 IoTDB 时间窗；没有 IoTDB 时这张跳过）"),
    ("/attack.html", "#atTable", "ui-attack.png", None, 0, None,
     "攻击流量面板 · 用例清单（只截静态；不注入，避免改了密钥库状态）"),
]


def ink_ratio(path):
    """亮像素占比 + 「边缘不能是死的」—— 后者防的是「渲染面太小 → 图右下角全是黑」。"""
    im = Image.open(path).convert("L")
    w, h = im.size
    px = im.load()
    bright = sum(1 for y in range(h) for x in range(w) if px[x, y] > BRIGHT)
    edge = any(px[x, y] > BRIGHT for x in range(w - 4, w) for y in range(h)) or \
           any(px[x, y] > BRIGHT for y in range(h - 4, h) for x in range(w))
    return bright / (w * h), edge


def status(base):
    import json
    import urllib.request
    with urllib.request.urlopen(base + "/api/status", timeout=5) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE_DEFAULT)
    ap.add_argument("--only", default="", help="只出名字含该子串的图")
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--browser", default="msedge",
                    help="msedge / chrome（用系统自带浏览器，免下载）或 chromium（需 playwright install chromium）")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        st = status(args.base)
    except Exception as e:                                    # noqa: BLE001
        raise SystemExit(f"!! UI 没在跑（{args.base}）：{e}\n"
                         f"   先另开终端跑：python ui_server.py --every 2 --no-browser")
    tsdb = bool(st.get("tsdb"))
    print(f"UI: {args.base}   IoTDB: {'可用' if tsdb else '不可用（回放那张会跳过）'}\n")

    todo = [s for s in SHOTS if args.only in s[2]]
    bad, made = [], []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel=args.browser)
        except Exception as e:                                # noqa: BLE001
            raise SystemExit(f"!! 启不起浏览器（{args.browser}）：{e}\n"
                             f"   用 --browser chrome 换系统 Chrome，"
                             f"或先跑：python -m playwright install chromium")
        page = browser.new_page(viewport=VIEW, device_scale_factor=SVG_DSF)
        for url, sel, name, action, extra, need, desc in todo:
            if need == "tsdb" and not tsdb:
                print(f"{name:26s} 跳过 —— 需要 IoTDB（{desc}）")
                continue
            page.goto(args.base + url, wait_until="load")
            page.wait_for_timeout(SETTLE_MS)
            if action and action[0] == "click":
                page.locator(action[1]).click()
                page.wait_for_timeout(max(extra, ACTION_WAIT_MS))
            elif extra:
                page.wait_for_timeout(extra)
            target = page.locator(sel) if sel else page
            if sel:
                target.scroll_into_view_if_needed()
                page.wait_for_timeout(500)
            path = out_dir / name
            target.screenshot(path=str(path))
            ratio, edge = ink_ratio(path)
            size = Image.open(path).size
            ok = ratio >= MIN_INK and edge
            if not ok:
                bad.append(name)
            else:
                made.append(name)
            note = "" if ok else ("（亮像素太少）" if ratio < MIN_INK else "（右/下边缘全黑 = 渲染面被截）")
            print(f"{name:26s} {'OK  ' if ok else 'FAIL'} {size[0]}×{size[1]}  "
                  f"亮像素 {ratio * 100:.1f}%   {desc}{note}")
        browser.close()

    print(f"\n出图 {len(made)} 张 → {out_dir}" + (f"，失败 {len(bad)} 张：{', '.join(bad)}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
