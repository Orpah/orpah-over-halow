#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
maps.py — 「野外包」（本地 XYZ 瓦片目录）的**服务端逻辑**（2026-09-13，ROADMAP §二）

野外包是什么：现场没有网络时用的底图 —— 一个**本地 XYZ 瓦片目录**
`<包名>/<z>/<x>/<y>.png`，放在 `ui/static/maps/` 下，由 `ui_server` 当静态文件发出去。
页面「瓦片源」里选 `野外包（本地）` 即用它；瓦片缺了就按既有规则
（`map.js` 的 `addBaseLayer()`，连续 4 张失败且成功 0 张）**回落本地自绘网格底图** + 明确提示。

四条硬约定（都在这里表达，页面/服务端不另写一份）：

1. **不入库**：包是运行时素材（一个城市小区域也可能几十 MB）→ `ui/static/maps/` 已进
   `.gitignore`；仓库里只有**做法**（README「野外包」一节）与这里的读取逻辑。
2. **必须自建/自托管才合规**：`tile.openstreetmap.org` 的政策**明文禁止离线/预取/离线打包**
   （ROADMAP §七）→ 本项**不得**用 OSM 官方瓦片做包；自建瓦片（switch2osm）或明确允许
   离线的供应商才行。页面条款提示里写着这一条（`tile_note_pack`）。
3. **只有「跑 ui_server 的那台机器」上的包可见**：浏览器是去**服务端**要瓦片的 ——
   现场把包放在操作员笔记本上、而 server 跑在另一台机器上时，那台机器上也得有同一份。
   所以提示里写明，包不存在时报「没有找到野外包」而不是留一片空白。
4. **只认一种路径形状**（`resolve_tile()`）：`<包名>/<z>/<x>/<y>.<白名单扩展名>`，
   层级必须是纯数字且在 zoom 范围内 → `..`、绝对路径、别的扩展名**一律 404**。
   这是**路径穿越的唯一防线**，所以放在纯函数里、单测里逐条试（`test_mapjs.py`）。

**PMTiles（单文件 + HTTP Range/206）未做**：用户 2026-09-13 定「选最小方案」——
野外包 = 目录式 XYZ，零新库；PMTiles 的做法与合规前提留在 ROADMAP §二，将来要做时服务端
得先支持 `Range` 响应（现在只整文件发，PMTiles 拉不动）。
"""
import os
import re
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
MAPS_DIR = os.path.join(HERE, "ui", "static", "maps")

# 白名单扩展名 → Content-Type（**不放行**别的类型：这是个“数据目录”，不是第二个静态站）
TILE_EXT = {".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".webp": "image/webp"}

# 列包时最多数多少张瓦片（数完就别再走了：包可能很大，页面只是想知道“大致有多大”）
COUNT_CAP = 20000
# zoom 上限：Leaflet 侧 maxZoom=19，再高的层级这里直接当非法（免得 `z=999` 拼出天文数字路径）
MAX_ZOOM = 24


def list_packs(root=None, cap=None):
    """列出 `root` 下的野外包：[{name, tiles, truncated, zooms}]（按名字排序）。

    - **空目录不算包**（一个瓦片都没有的目录不列出来 —— 免得选了才发现是空的；
      「一个包都没有」由页面译成「没有找到野外包 + 怎么做」，是**可见**的提示）。
    - 隐藏目录（`.` 开头）跳过：放说明/占位用。
    - `tiles` 到 `cap` 就停，并标 `truncated`（页面显示「计数已截断」）——
      这里**不**为了一个显示用的数字去走完几十万个文件。
    - 目录不存在 → 返回 `[]`（而不是抛异常）：**没放包**是正常状态，不是错误。
    """
    root = root or MAPS_DIR
    cap = COUNT_CAP if cap is None else int(cap)
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        if name.startswith("."):
            continue
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        tiles, truncated, zooms = 0, False, []
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames if not x.startswith(".")]
            for f in filenames:
                if os.path.splitext(f)[1].lower() in TILE_EXT:
                    tiles += 1
                    if tiles >= cap:
                        truncated = True
                        break
            rel = os.path.relpath(dirpath, d).replace("\\", "/")
            if rel != "." and rel.isdigit():        # 顶层第 z 层目录 = 这个包里用了哪些层级
                zooms.append(int(rel))
            if truncated:
                break
        if tiles:
            out.append({"name": name, "tiles": tiles, "truncated": truncated,
                        "zooms": sorted(set(zooms))})
    return out


def resolve_tile(rel, root=None):
    """`<包名>/<z>/<x>/<y>.<ext>` → `(绝对路径, Content-Type)`；不合法 → `(None, 原因码)`。

    原因码（给测试与日志用，不直接显示给用户）：`shape`（层级数不对）/
    `pack`（包名不合法）/ `nums`（z/x/y 不是十进制数或越界）/ `ext`（扩展名不在白名单）/
    `outside`（拼出来的路径跑到 maps 目录之外 —— 理论上到不了，留着当兜底）。

    包名允许中文（`unquote` 之后再校验）：现场目录名写 `区域A` 也得能用；
    但**不允许** `/`、`\\`、`..`、以 `.` 开头 —— 这些是穿越与“读隐藏文件”的入口。
    """
    root = os.path.abspath(root or MAPS_DIR)
    parts = [p for p in unquote(str(rel)).replace("\\", "/").split("/") if p not in ("", ".")]
    if len(parts) != 4:
        return None, "shape"
    pack = parts[0]
    if (not pack or pack.startswith(".") or "/" in pack or "\\" in pack
            or len(pack) > 64 or pack in ("..",)):
        return None, "pack"
    m = re.fullmatch(r"([0-9]{1,2})/([0-9]{1,9})/([0-9]{1,9})\.([A-Za-z0-9]{2,5})", "/".join(parts[1:]))
    if not m:
        return None, "nums"
    z, x, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    ext = "." + m.group(4).lower()
    if ext not in TILE_EXT:
        return None, "ext"
    if z > MAX_ZOOM or x >= (1 << z) or y >= (1 << z):     # XYZ 的合法格子范围
        return None, "nums"
    p = os.path.abspath(os.path.join(root, pack, str(z), str(x), str(y) + ext))
    if os.path.commonpath([root, p]) != root:              # 兜底（走到这儿说明上面漏了）
        return None, "outside"
    return p, TILE_EXT[ext]
