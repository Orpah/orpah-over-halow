#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ui_server.py — ORPAH-over-HaLow L1 demo Web UI（纯 PC，无硬件）
================================================================
一个进程内装好整条 L1 链路的可视化 demo：

    [Client 终端] --注入--> [STA 模块] ==HaLow 虚拟空口== [AP 模块]
        --host口--> [Router 桥] --真实UDP--> [Server]

- 内嵌 2 台 PC 模拟器（host/sim.py Core）：A=AP(Router 模块)、B=STA(Client 模块)
- 启动 OrpahServer（UDP 19447）+ RouterBridge + ClientHost（周期自动上报）
- 浏览器打开即看：三层拓扑 + ORPAH-REPORT 实时报文流 + 三端计数

运行：python ui_server.py [--port 8901] [--every 2] [--sn CN-WH01-9AF3C1D2] [--host 0.0.0.0]
零第三方依赖（仅标准库）。启动后自动打开浏览器 http://127.0.0.1:8901/

`--host`（2026-09-13）：**只改 HTTP 监听地址**，默认 `127.0.0.1`（只本机可达）。
想让手机/平板在同网段打开看，传 `--host 0.0.0.0`；组件端口（模拟器 console/link/host、
UDP server、Router）**仍然只在本机**，不随它暴露。
⚠ **这个页面没有任何认证**（demo 性质）：谁能访问到端口，谁就能驱动演示（标记走失、
下发/作废、注入攻击报文、刷量……）。所以**只在可信局域网、临时用**，用完就关。
⚠ 本链路 Phase 1 只做 IPv4：`--host` 给 IPv6 地址（含 `:`）会直接报错退出，
而不是让人误以为“绑上了”。
"""
import argparse
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque

# Windows 控制台默认代码页 GBK/cp936：强制 stdout/stderr 用 UTF-8 编码
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
# 空口仿真：用**本仓副本**（vendor/halow）而不是上游目录 —— 本项目要求零硬件、单进程即可跑 demo
# （不用先启动 halow-demo；副本来源与同步约定见 vendor/halow/VENDOR.md）
HOST_DIR = os.path.join(HERE, "vendor", "halow")
if HOST_DIR not in sys.path:
    sys.path.insert(0, HOST_DIR)

import sim                                # noqa: E402
from server import OrpahServer            # noqa: E402
from router import RouterBridge           # noqa: E402
from client import ClientHost             # noqa: E402
import orpah_id as oid                    # noqa: E402  Orpah ID 身份/真实性层
import orpah_proto as P                   # noqa: E402  报文常量/编解码 + 时钟归一化 effective_ts
import keystore as kst                    # noqa: E402  密钥库持久化（多代/轮换/撤销）
import registry as reg                    # noqa: E402  设备清册（SN↔走失者）
import cases                              # noqa: E402  走失案件闭环（以人为单位）
import stations as sta                     # noqa: E402  定位站位（无人机悬停测点 / 路由器坐标）
import motion                              # noqa: E402  演示用「移动的人」运动模型 + RSSI 换算
                                           #           （标定 A/n/噪声为唯一源 → /api/config）
import spoof                               # noqa: E402  防 spoof：攻击报文构造（脚本/UI 共用）
import alerts as alr                      # noqa: E402  告警规则引擎（页面红点）
import notify as ntf                      # noqa: E402  告警通知（出站 Webhook，边沿触发）
import downlink as down                   # noqa: E402  下行真实性（F-14 B）：Server 签名 / Router 验签
import clock as clk                       # noqa: E402  设备时钟偏移/漂移估计（纯计算）
import metrics                            # noqa: E402  指标面板纯计算（/api/metrics）
import maps                               # noqa: E402  野外包（本地 XYZ 瓦片目录，/api/maps + /maps/…）
import energy as en                       # noqa: E402  能量轴（免电池客户端模型，纯计算）
import energy_calib as ecal              # noqa: E402  能量模型的实测标定入口（同一模型、更实的参数）
import ratelimit as rl_mod                # noqa: E402  限频（§5.8）：令牌桶 + 计数 + 快照
import tsdb                               # noqa: E402  Apache IoTDB 时序库
import freshness as fr                    # noqa: E402  「这是不是老数据」的单一源（UI ④）
from waiting import wait_until            # noqa: E402  按截止时间等待（只这一份实现）
import damm32 as d32                      # noqa: E402  校验算法单一源
import luhn32 as l32                      # noqa: E402
import mod97 as m97                       # noqa: E402


def _check_of(algo, org_unique):
    """算校验位（ORG-UNIQUE，不含 CC）——统一走 Python 参考实现。"""
    if algo == "damm32":
        return d32.damm32_check(org_unique)
    if algo == "luhn32":
        return l32.luhn32_check(org_unique)
    if algo == "mod97":
        return m97.mod97_check(org_unique)
    raise ValueError(f"未知算法: {algo}")


def _verify_of(algo, body):
    """校验 ORG-UNIQUE-CHECK（不含 CC）。"""
    if algo == "damm32":
        return d32.damm32_verify(body)
    if algo == "luhn32":
        return l32.luhn32_verify(body)
    if algo == "mod97":
        return m97.mod97_verify(body)
    raise ValueError(f"未知算法: {algo}")


def _int_arg(s, default=None):
    """查询参数 → int（拿不到合法整数就返回 `default`）——**不要用 `str.isdigit()` 当校验**。

    2026-09-13 实测踩到：`isdigit()` 问的是“字符是不是数字类”，**不是**“`int()` 能不能解析”：
      · `"--123".lstrip("-").isdigit()` → True，但 `int("--123")` 抛 ValueError；
      · `"²".isdigit()` → True（上标二），但 `int("²")` 同样抛。
    异常发生在 `do_GET` 里（那里**没有**外层 try）→ 连接线程直接崩、客户端只看到
    “Failed to fetch”（不是 400）。实测复现：`/api/truth?from=--123`、`/api/truth?from=²`、
    `/api/truth?step=²`、`/api/replay?from=²` 四条全部把连接打断。
    故统一用 try/except int()（唯一入口），垃圾输入→回退默认值（与 `abc` 同待遇）。
    """
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    """环境变量 → float（拿不到合法数就返回 `default`）。只有**策略值**才走 env，
    物理量走标定文件（否则“这个数从哪来”会分不清）。"""
    try:
        return float(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


# ---- 端口分配（默认，可 --port 改 HTTP；组件端口固定避免冲突） ----
HTTP_PORT = 8901
CONSOLE_A, LINK_A, HOST_A = 9401, 9411, 9421    # AP（Router 模块）
CONSOLE_B, LINK_B, HOST_B = 9402, 9412, 9422    # STA（Client 模块）
UDP_SRV = 19447

EVENTS = queue.Queue(maxsize=1000)   # SSE 事件（满丢最旧）

# 演示用的降级级别模式（§8.2）：模式 → 喂给 `Device.report()` 的「哪个环节坏了」。
# 注意不是直接写死 level —— 交给 `orpah_id.pick_level()` 算出来，走的是规格里那条路径。
ID_LEVEL_MODES = {
    "auto":      {},                                  # 全正常 → pick_level → L0
    "sign_fail": {"sign_ok": False},                  # Step2 Slot0 签名失败 → L1
    "se_fail":   {"se_ok": False},                    # Step1 SE 不可用（有 HMAC）→ L2
    "no_key":    {"se_ok": False, "hmac_ok": False},   # Step3 无可用密钥 → L3
}


class OrpahApp:
    """装配整条 L1 链路 + 状态/计数（供 UI 轮询）。"""

    def __init__(self, every=2.0, sn="CN-WH01-9AF3C1D2", rssi=-55, walk=True):
        self.every = every
        self.sn = sn
        self.rssi = rssi
        # 演示数据：让被保护对象沿路线走动，并按「多台路由器各自测到它」写观测
        # （walk=False 则回到旧的恒定 RSSI 行为，便于对照/回归）
        self.walk = motion.default() if walk else None
        self.paused = False
        self.stop = threading.Event()
        # 三端计数
        self.client_sent = 0               # L2 上行注入：REQ-CONNECT + REPORT
        self.router_up = 0                 # 路由器转发给 Server 的 REPORT
        self.server_recv = 0               # Server 收到的 REPORT
        # 按内容分开的两个计数（UI 分色显示）：Orpah ID 签名上报不走 L2 计数，
        # 但它**真的走空口和 UDP** —— 空口帧 / UDP 帧的总额里含它。
        self.id_sent = 0                   # Client 注入的 ID-REPORT（含页面注入的攻击报文）
        self.router_id_up = 0              # 路由器透传给 Server 的 ID-REPORT
        # 设备时钟偏移/漂移估计（2026-09-12）：由「设备自报 ts vs 服务器接收时刻」反推，
        # **只估计不改数据**（§5.5 的时间语义仍由 effective_ts 负责）。
        self.clock = clk.ClockTracker(offset_warn=alr.CLOCK_OFFSET_SEC)
        # 最近报文（按 seq 记，stage 点亮；最多 50 条）
        self.reports = {}
        self.order = []
        # L2 消息流日志（环形，dir=up/down；供前端面板展示）
        self.flow = []
        # Server 权威走失表快照（前端展示 + mark/untrack 控制）
        self.lost = {}
        # 服务器发布走失表记录（每次下发 LOST-TABLE 记一条，最新在前）
        self.publishes = []
        self.publish_total = 0             # 服务器发布走失表总次数（单调累加）
        # 发现记录（Router 命中走失表 → ORPAH-FOUND，最新在前）
        self.founds = []
        # 持久化库（清册/案件/站位/密钥库共用同一 orpah.db）
        db_path = os.path.join(HERE, "orpah.db")
        # Orpah ID 层演示：设备 + 密钥库 + nonce 缓存（设备按 Client SN 创建，懒初始化）
        # 密钥库落 SQLite（与清册同库）：重启不丢，代次/宽限/撤销都在 —— 见 keystore.py
        self.id_ks = kst.KeyStoreDB(db_path)
        self.id_ks.sweep()                 # 启动收敛一次：宽限已过的代次 → retired
        self.id_dev = None
        self.id_used = oid.NonceCache()
        self.id_demo = {}
        self.id_level = "auto"             # 演示降级模式（§8.2，见 ID_LEVEL_MODES）
        self.id_cap_rtc = None             # 设备声明的「有无 RTC」（None=未声明 / True / False）
        self.id_ts_broken = False          # 演示：ID 上报的 ts 置 0（无时钟，2026-09-13）
        # ---- 能量轴（2026-09-13）：免电池客户端能量模型驱动间隔与级别 ----
        # **标定（2026-09-13）**：物理参数（待机 / 上报代价 / 储能 / 电压端点）先看实测标定文件
        # （`ORPAH_ENERGY_CALIB` 或仓库根 `energy_calib.json`；没有就用 energy.py 演示值）。
        # `self.cal` 是**唯一来源** —— 下面所有 plan/drain/mv_of 都从它取值，页面也显示它的出处；
        # 没标定时它是“未标定”状态（值=演示值、出处=demo），**不是**错误。
        self.cal = ecal.load()
        self.en_on = False                 # 是否让能量接管间隔/级别（默认关：间隔仍手工填）
        self.en_harvest = 0.5              # 平均采集功率（mW，演示参数）
        self.en_listen = _env_float("ORPAH_LISTEN_INTERVAL_S", en.LISTEN_INTERVAL_S)
        #                                   多久听一次下行（s）——**策略项**（不是标定项）：
        #                                   听间隔↑ = 下行变慢/发现更慢；0 = 不建模监听（对照用）
        self.en_charge = self.cal.charge0_mj   # 当前电量（mJ；标定给的初值）
        self.en_store = self.cal.store_mj      # 储能容量（mJ；标定给的容量）
        self.en_push = False               # 采不敷出时是否“硬撑”（吃储能也要被听见）
        self.en_speedup = 10.0             # 演示加速倍数：真实间隔 ÷ 它 = 页面看到的周期
        self.en_state = {}                 # 最近一次能量状态（/api/status 用）
        self.id_battery_mv = en.mv_of(self.cal.charge0_mj, self.cal.store_mj,
                                      self.cal.cell_empty_mv, self.cal.cell_full_mv)
        self.id_level_energy = None        # 能量决定的级别覆盖（None = 不覆盖）
        self.id_reports = deque(maxlen=20)     # 环形（appendleft 自动截断，线程安全）
        self.id_report_total = 0
        self._last_id_report = None        # 最近一条已签上报（供“重放”演示）
        self.id_attacker = None            # 防 spoof 演示：攻击者设备（自造钥匙，未登记）
        # ---- 攻击流量面板（2026-09-13）：攻击报文与**正常**签名上报流分开记 ----
        # 为什么要分开：`id_reports` 是两者混在一起的，旧做法只能让页面从公用的签名上报流里
        # 「猜」哪条是攻击（ROADMAP §四「攻击流量的 UI 独立面板」未做项的由来）。
        # 认领靠 **nonce**：注入时记下 nonce → 验签结果回来时对上，就是这一条。
        self.spoof_pending = {}            # nonce → {"kind","t"(epoch),"expect","sn"}
        self.spoof_results = {}            # kind → 最近一次结果行（刷新页面不丢）
        self.spoof_events = deque(maxlen=60)   # 攻击流量流（最新在前）
        self.spoof_injected = 0            # 累计注入条数（页面的分母）
        self.spoof_lost = 0                # 注入了但没等到结果的条数（见 _spoof_sweep）
        # ---- 告警通知（2026-09-13）：告警是**状态**（页面红点），通知是**事件** ----
        # 没人在看页面时也必须能被通知到，所以巡视 + 投递放在**后台线程**里
        # （不在 HTTP 请求里：投递要发网络请求，会把页面轮询卡住）。
        self.notifier = ntf.Notifier()
        self.alert_cache = {"t": "", "counts": alr.summary([]), "alerts": [],
                            "thresholds": alr.thresholds()}
        # ---- 下行真实性（F-14 B 方案，2026-09-13）：Server 持私钥签名 / Router 只持公钥 ----
        # 演示里两个角色在**同一进程**里 → 公钥直接传对象；同时把公钥**写盘**
        # （`ORPAH_DOWN_PUB`，缺省系统临时目录下的固定名），这样另起的 Router 进程
        # （demo 脚本 / 真机）能读到同一把。公钥不是秘密 —— 私钥才不能这么干。
        self.down_priv = down.load_priv()          # ORPAH_DOWN_KEY（没配就现场生成一把）
        self.down_key_src = "ORPAH_DOWN_KEY" if self.down_priv else "generated"
        if self.down_priv is None:
            self.down_priv, self.down_pub = down.demo_pair()
        else:
            self.down_pub = self.down_priv.public_key()
        self.down_pub_path = down.default_pub_path()
        try:
            down.write_pub(self.down_pub, self.down_pub_path)
        except OSError as e:                        # 写不了盘也不该拦着 demo 起来
            self.down_pub_path = ""
            print(f"[downlink] 公钥写盘失败（{type(e).__name__}: {e}）—— 同进程两个角色不受影响；"
                  f"另起的 Router 进程拿不到公钥（会走“未校验”那条路，页面会显示出来）")
        # ---- 限频（§5.8，2026-09-13）：与 server 共用同一实例（页面要读它的计数） ----
        self.rl = rl_mod.RateLimiter.from_env()
        self.rl_events = []                # 最近被丢弃的（最新在前，供卡片展示）
        # 数字签名工具：临时密钥对（供 /api/sig 演示 ES256/HS256）
        self.sig_dev = None
        # 设备清册 + 走失案件（SQLite 持久化；首次启动播种）
        self.registry = reg.Registry(db_path)
        self.cases = cases.CaseManager(db_path)
        self.stations = sta.StationTable(db_path)   # 定位站位（全局一张）
        if not self.registry.persons:
            self._seed_registry()
        self._ensure_demo_stations()
        # IoTDB 时序库（上报流 + 业务事件；未启动时优雅降级）
        self.tsdb = tsdb.Tsdb()
        # 数据新鲜度跟踪（UI ④，2026-09-13）：各条周期流的“最近一次真的动了”+ 各自节拍。
        # 打点位置就是这几条流**真正推进的地方**（下面 start()/_report_loop 等），
        # 不在页面侧猜 —— 页面只能看到结果，看不到“后端其实停了”。
        self.fresh = fr.Tracker()
        self.cores = []
        self.srv = None
        self.router = None
        self.client = None
        self.threads = []

    def _seed_registry(self):
        """演示种子：一个走失者绑多台客户端（项链/鞋），另一走失者一台已丢失。"""
        p1 = self.registry.add_person("小明", "演示：被保护对象（项链 + 鞋）",
                                      gender="男", age="7", height="120",
                                      build="偏瘦", features="左臂小胎记")
        self.registry.register("CN-WH01-9AF3C1D2", person_id=p1, org="WH01", cc="CN")
        self.registry.register("CN-WH01-8K3M2P7Q", person_id=p1, org="WH01", cc="CN")
        p2 = self.registry.add_person("小红", "演示：走失中（手表）",
                                      gender="女", age="6", height="115",
                                      health="轻度智力障碍", mental="神志清楚",
                                      communicate="能说出自己姓名")
        self.registry.register("CN-WH02-5T9V1B4C", person_id=p2, org="WH02", cc="CN",
                               status=reg.STATUS_LOST)
        # 演示：小红已立案走失（open 案件，含走失信息）
        self.cases.mark(p2, self.registry,
                        missing_at="2026-09-10 14:30 左右",
                        missing_place="XX市XX公园北门",
                        clothing="粉色连衣裙、白色凉鞋",
                        contact_phone="13800001234")

    # 演示用四台路由器（= 四个站位；坐标为各自安装位置，单位米）。
    # **为什么是四台**：定位需要同一时刻 ≥2 个观察者；而“某台的观测与其余台不一致时，
    # 能不能**定到是哪一台**”需要 ≥4 台（排除一台后还剩 ≥3 台可互证）——
    # 三台只能报“观测冲突、定不了是哪台”。
    # 这条是 `pos.js` 的 `consensus()` 实测出来的（见 SPEC §8 威胁 2 / §10 F-12）。
    # 第 4 台的位置不是随手放的：按"剔掉任意一台后剩下 3 台的几何"（最坏 3 台子集 GDOP，
    # 在整条演示路线上取值）在候选里选的 —— 西侧 (-20,0) 为 1.89；北侧 (0,40) 只能到 4.31。
    DEMO_STATIONS = (("路由器A", 30.0, 0.0),
                     ("路由器B", -15.0, 26.0),
                     ("路由器C", -15.0, -26.0),
                     ("路由器D", -20.0, 0.0))

    # 攻击流量面板：注入后等多久算「没等到结果」（秒）。
    # 这条链路上一条报文往返是毫秒级，本机上 15 s 足够宽松；超了就说明它根本没走到验签
    # （最可能是被 §5.8 限频挡在验签之前）→ 面板照实标「未等到结果」，不当成「被拒」。
    SPOOF_WAIT = 15.0

    def _ensure_demo_stations(self):
        """确保演示站位都在（**幂等，且不覆盖用户改动**）。

        为什么不是"空表才播种"（原先的写法）：老库（`orpah.db`）里已经有 3 台，
        新增第 4 台时"空表才播种"永远不生效 —— 而删库重来会连清册/案件/密钥一起清掉。
        这里只补**缺的**：按名字或坐标（±0.5 m）判定已存在，已存在就原样不动
        （在页面上改过坐标的以用户为准）；补完打印一行说明补了什么，**不静默**。
        """
        have = self.stations.list()
        added = []
        for name, x, y in self.DEMO_STATIONS:
            if any(s.name == name or (abs(s.x - x) < 0.5 and abs(s.y - y) < 0.5)
                   for s in have):
                continue
            s = self.stations.add(x=x, y=y, name=name)
            have.append(s)
            added.append(f"{s.sid}({name})")
        if added:
            print(f"[stations] 补齐演示站位：{', '.join(added)}")

    # ---------------- 消息流日志 ----------------
    def _push_flow(self, dirn, msg, stage=""):
        """记一条 L2 消息流日志（最多 200 条，新的在前）。"""
        ent = {
            "dir": dirn,                 # "up" 上行 / "down" 下行
            "stage": stage,              # client/router/server（可选）
            "type": msg.get("type", "?"),
            "sn": msg.get("sn", "-"),
            "status": msg.get("status", msg.get("code", msg.get("tracked", "-"))),
            "t": time.strftime("%H:%M:%S"),
        }
        self.flow.insert(0, ent)
        del self.flow[200:]

    # ---------------- 事件（三端回调 → SSE） ----------------
    def _emit(self, stage, msg, **kw):
        ev = {"type": "report", "stage": stage, "msg": msg,
              "t": time.strftime("%H:%M:%S"), **kw}
        try:
            EVENTS.put_nowait(ev)
        except queue.Full:
            try:
                EVENTS.get_nowait()
            except queue.Empty:
                pass
            try:
                EVENTS.put_nowait(ev)
            except queue.Full:
                pass

    def _on_sent(self, msg, eth_len):
        """Client 注入上行（REQ-CONNECT / REPORT）。"""
        self.client_sent += 1
        self._remember(msg, "client")
        self._push_flow("up", msg, "client")
        self._emit("client", msg, eth_len=eth_len)

    def _on_recv(self, msg):
        """Client 收到下行（ACCESS-INFO / TRACKING-STATUS / ERROR）。"""
        self._push_flow("down", msg, "client")
        # 表无 seq，不点亮三列；发 SSE 供控制台类日志
        self._emit("log", msg, dir="rx")

    def _on_up(self, msg):
        """Router 上行转发（REPORT → Server）。"""
        self.router_up += 1
        self._remember(msg, "router")
        self._push_flow("up", msg, "router")
        self._emit("router", msg)

    def _on_up_id(self, msg):
        """Router 透传 ID-REPORT → Server（不占 L2 上行计数，单独计）。

        ID 上报不进「L2 协议消息流」面板（那不是 L2 报文），只计数 + 发 SSE
        （页面/日志能看到），与 `_on_id_report`（Server 验签结果）分开。
        """
        self.router_id_up += 1
        self._emit("log", msg, dir="tx-id")

    def _on_down(self, msg):
        """Router 注入下行（回 Client：ACCESS-INFO/TRACKING-STATUS/ERROR）。"""
        self._push_flow("down", msg, "router")
        self._emit("log", msg, dir="tx")

    def _on_report(self, msg):
        """Server 收到 REPORT。"""
        self.server_recv += 1
        self._remember(msg, "server")
        # 时钟可信（§5.5）：**一次**算出本条上报的有效时刻，然后所有落库/审计/展示都用它 ——
        # 以前是 write_report / _write_router_obs / registry.touch 各自 time.time() 兑底，
        # 既会有毫秒级偏差，也会出现「设备流存服务器时间、报文流还显示 0（1970）」的不一致。
        rx = time.time()
        # RSSI 突变规则（2026-09-13）要拿**服务器接收时刻**算相邻样本的间隔：免电池设备
        # 没有时钟（ts=0），拿设备自报的 ts 算不出“间隔多久跳了多少 dB”。
        msg["rx"] = rx
        rtc = P.rtc_of(msg)                    # 设备能力声明（业务报文未签名 → 只能当提示）
        ts_s, ts_src = P.effective_ts(msg.get("ts"), rx, rtc=rtc)
        msg["ts_src"] = ts_src                 # 只改服务器侧这份副本，供前端标注；报文原文不动
        if ts_src == P.TS_SRC_SERVER:
            msg["ts_eff"] = ts_s
        self._push_flow("up", msg, "server")
        self._emit("server", msg)
        # 设备清册：更新最近见时间；IoTDB 写上报点；走失案件：被看到即触发「发现」
        if msg.get("sn"):
            sn = msg["sn"]
            # 时钟估计：只有“设备自己的时钟”可用时才是一条有效观测（ts_src=device），
            # 且设备**声明无 RTC** 时永不喂 —— 它没有参考时钟，拿它估偏移/漂移是纯噪声。
            if ts_src == P.TS_SRC_DEVICE and rtc is not False:
                self.clock.add(sn, msg.get("ts"), rx)
            self.registry.touch(sn, ts=ts_s)
            self.tsdb.write_report(sn, ts=ts_s, rssi=msg.get("rssi"),
                                   seq=msg.get("seq"))
            self._write_router_obs(sn, msg, ts_s)
            _, newly = self.cases.on_found(sn, self.registry, detail="report")
            if newly:
                self.tsdb.write_event("case_found", sn=sn, detail="report")

    def _write_router_obs(self, sn, msg, ts_s):
        """演示：把「各台路由器各自测到该设备」的观测写库（多路由器定位的数据源）。

        位置由上报时刻决定（`motion.Walk.pos`，确定性）—— 同一份报文无论何时回放，
        得到的位置一致。写进 root.orpah.routers.<sid>.<sn>（每台一条序列，互不覆盖；
        若挤在 root.orpah.devices.<sn> 同一时间戳下会 last-write-wins 互相覆盖）。
        ts_s 由调用方给出（= `effective_ts` 的结果）：**必须与设备流用同一个时刻**，
        否则定位/回放按时间对齐时两边对不上。

        ★ **人级聚合的演示数据（2026-09-13）**：本 demo 只有**一台**设备真的在周期上报，
        而「人级聚合」要的是同一个人的**多台**客户端（项链 + 鞋）。所以这里顺带按
        **同一个 motion 位置模型**给该人名下的其它设备也各写一份观测，噪声用
        `"<sid>|<sn>"` 当盐 → 各设备**独立**（真实独立性来自各自的晶振/天线/遮挡）。
        ⇒ 这些是**模拟数据**（页面上明说），真机是各设备自己上报、各自带自己的噪声；
          本函数不改变链路（空口/验签那条路上的报文仍然只有真实设备那一份）。
        """
        if not self.walk:
            return
        t_s = float(ts_s)                           # 注意：tsdb 写入接口的 ts 是 epoch **秒**
        t_ms = int(t_s * 1000)                     # 位置按毫秒算（motion 用 ms）
        dev = self.registry.get(sn)
        others = []
        if dev is not None and getattr(dev, "person_id", None):
            others = [d.sn for d in self.registry.devices_of(dev.person_id)
                      if d.sn != sn]
        for s in self.stations.list():
            for one in [sn] + others:
                salt = s.sid if one == sn else f"{s.sid}|{one}"
                rssi = self.walk.rssi_to(t_ms, s.x, s.y, sid=salt)
                self.tsdb.write_router_obs(s.sid, one, ts=t_s, rssi=rssi,
                                           seq=msg.get("seq") if one == sn else 0)

    def router_obs_range(self, sn, t0_ms, t1_ms, limit=20000):
        """{sid: [{t,rssi,seq}]}：各路由器在某时间窗内对该设备的测量（升序）。

        每台一条序列（含没数据的空表）→ 页面按 sid 取自己那份，不用再按 router_id 筛。
        """
        out = {}
        for s in self.stations.list():
            rows = self.tsdb.query_router_range(s.sid, sn, t0_ms, t1_ms, limit)
            out[s.sid] = rows or []
        return out

    def router_obs_recent(self, sn, limit=500):
        """{sid: [{t,rssi,seq}]}：各路由器对某设备的**最近** limit 条（升序）。"""
        out = {}
        for s in self.stations.list():
            rows = self.tsdb.query_router_recent(s.sid, sn, limit)
            out[s.sid] = rows or []
        return out

    def _on_lost(self, snap):
        """Server 走失表变更 → 存快照（前端展示）。"""
        self.lost = snap

    def _on_publish(self, entries, targets):
        """Server 发布/更新 LOST-TABLE（下发给 Router）→ 记发布记录（供前端展示）。"""
        self.publish_total += 1
        rec = {"t": time.strftime("%H:%M:%S"), "n": len(entries),
               "targets": targets, "entries": list(entries)}
        self.publishes.insert(0, rec)
        del self.publishes[20:]
        self._emit("publish", {"n": len(entries), "targets": targets})
        # 落库（内存环形只留 20 条，历史看「事件历史」区）
        ent = ",".join(f"{e['sn']}={'1' if e['tracked'] else '0'}"
                       for e in entries)
        self.tsdb.write_event(
            "publish", sn="",
            detail=f"n={len(entries)} targets={targets} {ent}".strip())

    def _on_found_router(self, msg):
        """Router 发送 ORPAH-FOUND（发现走失）→ 消息流记 Router 段。"""
        self._push_flow("up", msg, "router")

    def _on_found_server(self, msg, addr):
        """Server 收到 ORPAH-FOUND → 消息流记 Server 段 + 发现记录列表 + 案件联动。"""
        self._push_flow("up", msg, "server")
        rec = {"t": time.strftime("%H:%M:%S"), "sn": msg.get("sn", "-")}
        self.founds.insert(0, rec)
        del self.founds[20:]
        self._emit("found", rec)
        sn = msg.get("sn", "-")
        # 落库（内存环形只留 20 条）
        self.tsdb.write_event("found", sn=sn,
                              detail=f"router {addr[0]}:{addr[1]}")
        # 走失案件：任一台设备被 Router 发现 → 该人案件进入「已发现」
        c, newly = self.cases.on_found(sn, self.registry,
                                       detail=f"router {addr[0]}:{addr[1]}")
        if newly:
            self.tsdb.write_event("case_found", sn=sn,
                                  detail=f"router {addr[0]}:{addr[1]}")
        if c is not None:
            self._emit("case", {"case_id": c.case_id, "status": c.status,
                                "sn": sn})

    def _remember(self, msg, stage):
        seq = msg.get("seq")
        if seq is None:
            return
        if seq not in self.reports:
            self.reports[seq] = {"msg": msg, "seen": set()}
            self.order.append(seq)
            if len(self.order) > 50:
                old = self.order.pop(0)
                self.reports.pop(old, None)
        self.reports[seq]["seen"].add(stage)

    # ---------------- 装配 ----------------
    def start(self):
        # 1) 两台 PC 模拟器
        coreA = sim.Core("Router-AP", "AP", CONSOLE_A, LINK_A, None,
                         host_port=HOST_A)
        coreB = sim.Core("Client-STA", "STA", CONSOLE_B, LINK_B,
                         ("127.0.0.1", LINK_A), host_port=HOST_B)
        self.cores = [coreA, coreB]
        for c in self.cores:
            th = threading.Thread(target=self._loop, args=(c,), daemon=True)
            th.start()
            self.threads.append(th)

        # 2) Server（真实 UDP，权威走失库）
        self.srv = OrpahServer(port=UDP_SRV, on_report=self._on_report,
                               on_lost=self._on_lost, on_push=self._on_publish,
                               on_found=self._on_found_server,
                               keystore=self.id_ks, id_nonces=self.id_used,
                               down_key=self.down_priv,
                               on_id_report=self._on_id_report,
                               rl=self.rl, on_ratelimit=self._on_ratelimit)
        self.srv.start()

        # 2.5) 已立案案件名下设备同步进权威走失表（种子案件等）
        for c in self.cases.open_cases():
            for d in self.registry.devices_of(c.person_id):
                self.srv.mark_tracked(d.sn, note=f"case:{c.case_id}")

        # 3) Router 桥（AP host 口 ⇄ UDP ⇄ Server；双向）
        self.router = RouterBridge(ap_port=HOST_A, server_port=UDP_SRV,
                                   down_pub=self.down_pub,
                                   on_up=self._on_up, on_down=self._on_down,
                                   on_found=self._on_found_router,
                                   on_up_id=self._on_up_id,
                                   on_ratelimit=self._on_ratelimit)
        if not self.router.start():
            print("[ui] Router 连不上 AP host 口，退出")
            return False

        # 4) Client host（双向会话：注入上行 + 收下行）
        #    设备侧**自愿**自限频开着（§5.8 设备那一环）—— 页面的“周期上报”走它，
        #    而演示注入（攻击/刷量/重放）由调用方 `force=True` 绕过（那是“别的设备”的行为）。
        self.client = ClientHost(sta_port=HOST_B, sn=self.sn or "CN-WH01-9AF3C1D2",
                                 rssi=self.rssi, on_sent=self._on_sent,
                                 on_recv=self._on_recv, self_limit=True)
        if not self.client.connect():
            print("[ui] Client 连不上 STA host 口，退出")
            return False

        # 5) 等 STA 关联 AP（关联前注入会被模块丢弃 → 先等连上再开始上报）
        #    UI 场景与验收脚本不同：**连不上也要把界面起起来**（能看到 conn=OFFLINE、可手动排查），
        #    所以这里只告警不退出（以前是猜 100×0.1s，超时了也当“就绪”往下走，说不清状态）。
        if wait_until(lambda: coreB.wifi.conn == sim.CONN_CONNECTED,
                      timeout=15, interval=0.1):
            print(f"[ui] STA conn = {coreB.wifi.conn_str()}  链路就绪")
        else:
            print(f"[ui] 警告：15s 内 STA 未关联 AP（conn={coreB.wifi.conn_str()}）——"
                  "界面上报会被丢弃；可先在页面上检查空口/配置")

        # 6) 会话线程（L2：REQ-CONNECT → REPORT 自动周期）
        th = threading.Thread(target=self._report_loop, daemon=True)
        th.start()
        self.threads.append(th)
        self._id_tick()          # 立即填充一次，不必等首个周期

        # 7) 告警巡视线程（2026-09-13）：评估 + 边沿触发投递。
        #    必须独立于页面/HTTP：没人开页面时也得能通知到（见 `_alert_watch` 说明）。
        th = threading.Thread(target=self._alert_watch, daemon=True)
        th.start()
        self.threads.append(th)
        return True

    def _loop(self, core):
        while not self.stop.is_set():
            core.wifi.poll()
            core.link.poll()
            time.sleep(0.005)

    def _report_loop(self):
        while not self.stop.is_set():
            if not self.paused:
                self._energy_step()          # 能量轴：先算「这次该不该报/多久报一次/什么级别」
                if self.en_state.get("silent"):
                    # 采不敷出且选择了“不如实沉默”（或没开硬撑）→ 本轮不发报，只推进电量
                    time.sleep(self.every)
                    continue
                self._aim_link_rssi()
                self.client.send_req_connect()
                time.sleep(0.25)
                self.client.report_once()
                self._id_tick()
                # 数据新鲜度（UI ④）：这一轮**真的发出去了**才算“周期上报在动”。
                # 节拍随能量轴变（2s → 30s）→ 一起更新，否则省电模式会被自己判成“停摆”
                # （一条假告警比没有告警更坏）。人工 `pause` **不打点** ——
                # 那是“我知道它停了”，打点会把暂停伪装成正常。
                self.fresh.touch("report_cycle", period=self.every)
            time.sleep(self.every)

    def _energy_step(self, drain=True):
        """能量轴推进一拍（2026-09-13）：

        1. 用当前采集/电量算策略（`energy.plan`）—— 级别与间隔**都由能量决定**；
        2. 间隔写回 `self.every`（客户端据此周期上报）；级别写回 `id_level_energy`；
        3. 电量按 `energy.drain()` 推进，并把 `energy.mv_of()` 的电压写进下一份**已签**报告
           （`payload.battery_mv`，在签名预像内 → 服务端不能抵赖我“快没电了”）；
        4. `interval_s is None`（采不敷出）：默认就**如实沉默**（不发报），
           只有演示开关 `en_push` 打开时才“硬撑”（用 `EMERGENCY_INTERVAL_S` 继续发，
           并让服务端能看见 `silence_in_s` 倒计时）。
        未开启能量模式时什么也不做（`every` 仍由页面手填控制）。
        `drain=False`：只重算策略、**不动电量**（页面改参数时用，见 POST /api/energy）。
        """
        if not self.en_on:
            self.en_state = {}
            return
        # 参数来自标定（唯一源）：没标定就是 energy.py 的演示值，出处标在 `calib` 里
        # 监听开销也交给模型（`listen_mj` 来自标定、`listen_interval_s` 是页面/env 的策略项）
        p = en.plan(self.en_harvest, self.en_charge, self.en_store,
                    sleep_mw=self.cal.sleep_mw, cost=self.cal.cost,
                    listen_mj=self.cal.listen_mj, listen_interval_s=self.en_listen)
        no_interval = p["interval_s"] is None
        if not no_interval:
            interval = float(p["interval_s"])
        else:
            interval = en.EMERGENCY_INTERVAL_S if self.en_push else None
        hard = bool(no_interval and self.en_push)
        if interval is not None:
            # 演示加速：真实 300s 的间隔按 `en_speedup` 倍压缩，否则现场看不到变化
            self.every = max(0.5, interval / self.en_speedup)
        # 硬撑时 `plan()` 给的 `silence_in_s` 是“不发报”的缺口（乐观）—— 硬撑明明在发报，
        # 得按**实际硬撑间隔**重算一次（含监听），否则页面上的倒计时比实情长。
        silence_in_s = p["silence_in_s"]
        if hard:
            silence_in_s = en.survive_s(self.en_charge, self.en_harvest, interval, p["level"],
                                        store_mj=self.en_store, sleep_mw=self.cal.sleep_mw,
                                        cost=self.cal.cost, listen_mw=p["listen_mw"])
        self.id_battery_mv = en.mv_of(self.en_charge, self.en_store,
                                      self.cal.cell_empty_mv, self.cal.cell_full_mv)
        self.id_level_energy = 1 if p["degraded"] else None   # 1 = HS256（§8.2 的 L1 算法）
        self.en_state = {
            "on": True, "harvest_mw": self.en_harvest, "charge_mj": round(self.en_charge, 2),
            "store_mj": self.en_store, "mv": self.id_battery_mv, "level": p["level"],
            "degraded": p["degraded"], "degraded_reason": p["degraded_reason"],
            "interval_s": p["interval_s"], "every_s": round(self.every, 2),
            "net_mw": p["net_mw"], "budget_ok": p["budget_ok"],
            # 监听（固定开销，2026-09-13）：模型算出来的平均值 + 听间隔回显 + 缺钱在哪一层
            "listen_mw": p["listen_mw"], "listen_interval_s": p["listen_interval_s"],
            "listen_modeled": p["listen_modeled"], "overhead_mw": p["overhead_mw"],
            "usable_mw": p["usable_mw"], "report_mw": p["report_mw"],
            "short_of": p["short_of"],
            "silence_in_s": silence_in_s, "usable": p["usable"], "why": p["why"],
            "silent": bool(no_interval and not hard), "hard": hard,
            "silence_eta_s": (round(silence_in_s / self.en_speedup, 1)
                              if silence_in_s is not None else None),
        }
        if not drain:
            return
        self.en_charge = en.drain(self.en_charge, self.en_harvest, interval, p["level"],
                                  float(self.every) * self.en_speedup,
                                  store_mj=self.en_store, sleep_mw=self.cal.sleep_mw,
                                  cost=self.cal.cost, listen_mw=p["listen_mw"])

    def _aim_link_rssi(self):
        """设备自身的 REPORT 里的 rssi = **当前与之关联的那台路由器**测到的强度。

        人走到哪台路由器附近，链路就强（报文里那个 rssi 是有物理含义的：
        STA↔AP 的链路强度），而不是一个写死的常量。
        """
        if not self.walk or not self.client:
            return
        sid, rssi = self.walk.nearest(int(time.time() * 1000), self.stations.list())
        if rssi is not None:
            self.client.rssi = rssi

    def _ensure_id_device(self):
        """让 Orpah ID 演示设备与当前 Client SN 一致（SN 变更时重建+重注册）。

        密钥由 (sn, gen) 确定派生（`oid.derive_demo_privkey`）→ 重启后公钥仍对得上；
        gen 取密钥库里该 SN 的 **active 代次**，轮换后签的是新代。
        """
        sn = self.client.sn if self.client else self.sn
        if self.id_dev is not None and self.id_dev.sn == sn:
            return
        rec = self.id_ks.active_of(sn)
        gen = rec["gen"] if rec else 1
        self.id_dev = oid.Device(sn=sn, se_sn="ATECC608B-DEMO", gen=gen)
        self.id_ks.register(self.id_dev, model="CH32V203+TX-AH+ATECC608B",
                            firmware="1.0.3")

    def _id_tick(self):
        """周期生成真实签名的 orpah-id-report 并经既有链路上行（Server 验签）。"""
        self._spoof_sweep()      # 顺手把「注入后一直没等到结果」的攻击条目标出来
        try:
            self._send_id_report()
        except Exception as e:  # 无 cryptography 时退化为错误展示
            self.id_demo = {"t": time.strftime("%H:%M:%S"), "err": str(e)}

    def _legit_id_report(self, ts=None):
        """一条**合法签名**上报表（周期上报与 spoof 演示的攻击基准都用它）。

        级别由 `self.id_level`（演示模式）经 **§8.2 的 `pick_level`** 自动选出：
        默认 `auto` → L0；`sign_fail` → L1；`se_fail` → L2；`no_key` → L3。
        传的是"哪个环节坏了"而不是直接写死 level —— 走的才是规格里那条路径。
        """
        self._ensure_id_device()
        cap = None if self.id_cap_rtc is None else {"rtc": bool(self.id_cap_rtc)}
        kw = dict(ID_LEVEL_MODES.get(self.id_level, ID_LEVEL_MODES["auto"]))
        if self.id_level_energy is not None:
            # 能量轴：能量决定的级别（HS256）。**不用 pick_level 的故障路径**（那是“哪个环节坏了”），
            # 否则会把“因为没电而省电”错报成“Slot0 签名失败”。
            kw = {"level": self.id_level_energy}
        return self.id_dev.report(
            ts=0 if self.id_ts_broken else ts,
            seen_routers=[{"bssid": "AA:BB:CC:DD:EE:FF",
                           "ssid": "ORPAHID_ZONE_A", "rssi": -42}],
            battery_mv=self.id_battery_mv, firmware="1.0.3", cap=cap, **kw)

    def _send_id_report(self, ts=None, force=False):
        """生成一条已签 orpah-id-report 并注入上行；ts 可指定（演示超窗）。

        `force=True` 用于**演示注入**（刷量/重放/超窗）——那是“另一台设备/一台失控设备”
        的行为，不是本机自己的业务上报 → 明确绕开设备侧自限频（它本来就是自愿的）。
        """
        r = self._legit_id_report(ts=ts)
        self._last_id_report = r
        self._inject_id(r, force=force)
        return r

    def _inject_id(self, report, force=False):
        """注入一条 ID-REPORT 并计数（周期上报 / 重放 / 超窗 / 伪造都经此）。

        计数放这里而不是 `client.send_id_report` 的各调用点：空口帧总额
        (tx_sta) = L2 注入 + ID 注入，四处各计一次迟早漏一个 → 页面上对不上。
        注：被设备侧自限频**延后**的条（返回 0）**不计**入 `id_sent` —— 它没上空口，
        计了就会让空口帧总额对不上（页面 `client_held` 单独报这个数）。
        """
        n = self.client.send_id_report(report, force=force)
        if n > 0:
            self.id_sent += 1
        return n

    def spoof_attack(self, kind):
        """防 spoof 演示：造一条攻击报文并经**既有空口链路**上行 → (ok, info)。

        走的就是真实链路（client→STA→AP→router→server），验签结果由 server 回
        `_on_id_report` → 页面/`id_reject` 事件都能看到，不是“离线自说自话”。
        攻击构造与 `demo_spoof.py` 共用 `spoof.py`，两边不会漂移。

        返回值里带 `nonce`：**攻击流量面板**靠它把「注入的这一条」与「回来的那条验签结果」
        对上（`id_reports` 里正常上报与攻击是混着排队的，不能靠顺序认）。
        """
        if kind not in spoof.UI_KINDS:
            return False, {"err": "bad_kind", "kinds": spoof.UI_KINDS}
        if self.id_attacker is None:
            # 攻击者自造一对钥匙（SN 未登记）—— 冒充只能靠“用别人的 SN + 自己的签名”
            self.id_attacker = oid.Device(cc="CN", org="WHOFF")
        self._ensure_id_device()
        used_nonce = None
        if kind == "replay" and self._last_id_report:
            used_nonce = (self._last_id_report.get("payload") or {}).get("nonce")
        try:
            report, expect, note = spoof.build_case(
                kind, self.id_dev, int(time.time()),
                attacker=self.id_attacker, used_nonce=used_nonce)
        except Exception as e:
            return False, {"err": str(e)}
        nonce = (report.get("payload") or {}).get("nonce") or ""
        sn = (report.get("payload") or {}).get("sn") or ""
        self._spoof_track(nonce, kind, expect, sn)
        self.spoof_injected += 1
        # 攻击注入 = “别的设备在说话”，不是本机业务上报 → 明确绕过设备侧自限频
        # （它本来就是**自愿**的：真被改的设备不会做，这里只是别把演示自己拦了）
        self._inject_id(report, force=True)
        zh, en, _, _ = spoof.case_info(kind)
        return True, {"kind": kind, "zh": zh, "en": en, "nonce": nonce, "sn": sn,
                      "expect": expect, "note": note,
                      "note_en": spoof.case_note(kind, "en")}

    def _spoof_track(self, nonce, kind, expect, sn=""):
        """记下「这条攻击报文的 nonce」，等它的验签结果回来认领（见 `_spoof_claim`）。

        上限 64 条：键是攻击者可选的 nonce，只增不减会攒着；丢掉最老的（那条本来也
        等不到结果，`_spoof_sweep` 已把它记进 `spoof_lost`）。
        """
        self.spoof_pending[nonce] = {"kind": kind, "t": time.time(),
                                     "expect": expect, "sn": sn}
        while len(self.spoof_pending) > 64:
            self.spoof_pending.pop(next(iter(self.spoof_pending)))

    def _spoof_row(self, kind, nonce, epoch, expect, state, rec=None, sn=""):
        """构造一行**攻击流量**记录（面板与 `spoof_events` 共用同一种形状）。

        state：`blocked`（被某道防线拒）/ `accepted`（没被拦）/ `lost`（没等到结果）。
        `ok` = 实际裁决与期望是否一致；没等到结果时**不为真**（`ok=None`，如实留空）。
        """
        got = None if state == "accepted" else (rec or {}).get("error")
        if state == "lost":
            got = None
        return {
            "kind": kind, "nonce": nonce,
            "t": time.strftime("%H:%M:%S", time.localtime(epoch)),
            "epoch": int(epoch), "expect": expect, "got": got, "state": state,
            "ok": None if state == "lost" else (got == expect),
            "line": "wait" if state == "lost" else spoof.defense_of(got),
            "sn": (rec or {}).get("sn", sn),
            "alg": (rec or {}).get("alg"), "level": (rec or {}).get("level"),
            "trust": (rec or {}).get("trust"),
            "accepted": (rec or {}).get("accepted"),
        }

    def _spoof_claim(self, rec):
        """验签结果回来 → 认领它是不是**我们注入的攻击流量**（按 nonce）。

        只有认得下的（nonce 在 pending 里）才进攻击流量流/结果表 ——
        正常周期上报、页面上的重放/超窗演示都不受影响（它们照旧只进 `id_reports`）。
        """
        nonce = rec.get("nonce") or ""
        p = self.spoof_pending.pop(nonce, None)
        if p is None:
            return
        state = "accepted" if rec.get("accepted") else "blocked"
        row = self._spoof_row(p["kind"], nonce, time.time(), p["expect"], state,
                              rec=rec, sn=p.get("sn", ""))
        self.spoof_results[p["kind"]] = row
        self.spoof_events.appendleft(row)

    def _spoof_sweep(self):
        """把**注入了却一直没等到结果**的攻击条目标出来（由 `_id_tick` 每秒调一次）。

        为什么会有这种条目：攻击报文走的是**真**链路，也可能死在验签**之前** ——
        §5.8 限频（Server 侧 per-SN/per-Router、Router 侧转发）就在验签之前。
        那种条永远不会出现在 `_on_id_report` 里。**如实标「未等到结果」**：
        既不写成「被拒」（会让人以为防线抓住了），也不写成「通过」（会让人以为拦不住）。
        """
        if not self.spoof_pending:
            return
        now = time.time()
        for nonce, p in list(self.spoof_pending.items()):
            if now - p["t"] <= self.SPOOF_WAIT:
                continue
            # 用 pop 而不是 del：`_spoof_claim` 在 **server 的线程**里跑，
            # 正好在“拷一份待查列表”与“删它”之间把它认领走时，del 会抛 KeyError。
            # （本方法与 HTTP 请求线程共用一个 dict，只靠 GIL 的原子性不够。）
            if self.spoof_pending.pop(nonce, None) is None:
                continue
            self.spoof_lost += 1
            row = self._spoof_row(p["kind"], nonce, p["t"], p["expect"], "lost",
                                  sn=p.get("sn", ""))
            self.spoof_results[p["kind"]] = row
            self.spoof_events.appendleft(row)

    def spoof_view(self):
        """攻击流量面板的视图（`/api/status.spoof`）。

        `defenses` 一并给出，页面**不写死**防线清单（单一源 = `spoof.DEFENSES`）——
        与 `spoof_kinds` 同一个做法：后端把中英文都给出来，页面按当前语言挑。
        """
        return {
            "injected": self.spoof_injected,
            "lost": self.spoof_lost,
            "wait": self.SPOOF_WAIT,
            "waiting": [dict(kind=p["kind"], nonce=n, sn=p.get("sn", ""),
                             expect=p["expect"], epoch=int(p["t"]))
                        for n, p in list(self.spoof_pending.items())],
            "results": dict(self.spoof_results),
            "recent": list(self.spoof_events),
            "defenses": [{"id": lid, "zh": zh, "en": en}
                         for lid, zh, en in spoof.DEFENSES],
        }

    def spoof_reset(self):
        """清空攻击流量面板的计数与结果（演示用；**不**动密钥库/限频桶，见 `rl_reset`）。"""
        self.spoof_pending.clear()
        self.spoof_results.clear()
        self.spoof_events.clear()
        self.spoof_injected = 0
        self.spoof_lost = 0

    def rssi_series(self, limit=12):
        """设备**自报**的链路强度序列（**最新在前**）→ 给 `alerts` 的 RSSI 突变规则用。

        - 时刻用**服务器接收时刻**（`msg["rx"]`）而不是设备自报的 `ts`：免电池设备没有时钟
          （ts=0），用它算不出“相邻样本间隔多久”；而这条规则判的正是“多久跳了多少 dB”。
        - 只取最近 limit 条：规则本身也只看**最新那一对**（`alerts` 里已注明为何不比更早的）。
        - `ORPAH-REPORT` **未签名** → 这条只能当线索（规则文案已写死这个口径）。
        """
        out = []
        for seq in reversed(list(self.order)[-limit:]):
            rec = self.reports.get(seq)
            if not rec:
                continue
            m = rec["msg"]
            if not m.get("sn") or m.get("rssi") is None or m.get("rx") is None:
                continue
            out.append({"sn": m["sn"], "rssi": m["rssi"], "ts": m["rx"]})
        return out

    def alert_view(self):
        """告警视图（`/api/alerts` 与后台巡视线程**共用同一份**）—— 无状态，每次重算。

        阈值快照一并给出（页面显示用，单一源 = `alerts.thresholds()`）；
        `notify` 也带一份（页面不必再多一个请求）。
        """
        al = alr.evaluate(self.registry, self.cases, self.id_reports,
                          clock=self.clock.snapshot(),
                          energy=self.energy_snapshot(),
                          ratelimit=self.rl_alert_view(),
                          rssi_series=self.rssi_series())
        return {"ok": True, "counts": alr.summary(al), "alerts": al,
                "thresholds": alr.thresholds(), "notify": self.notifier.snapshot()}

    def _emit_alert(self, rec, alert):
        """把「投递出去的通知」也发一份到 SSE —— 页面据此弹一次 toast。

        只在**边沿**上发（新告警/升级/消警），所以页面不会每 3 秒弹一遍同一个告警。
        """
        ev = {"type": "alert", "t": time.strftime("%H:%M:%S"),
              "event": rec.get("event"), "key": rec.get("key"),
              "level": alert.get("level"), "alert": alert,
              "delivery": {"ok": rec.get("ok"), "status": rec.get("status"),
                           "err": rec.get("err")}}
        try:
            EVENTS.put_nowait(ev)
        except queue.Full:
            pass

    def _notify_step(self, alerts):
        """一次「评估 → 差分 → 投递」（只跑在后台巡视线程里）。

        投递结果都**留痕**：审计写 `notify` 事件（含 ok/status/err/tries），页面看 `/api/status.notify`。
        **失败会有界重试**（`notify.py` 头注释：退避 1s/5s/30s、试完才计「放弃投递」）；
        这里能看到的失败是**当前这一次尝试**的失败 —— 它后面还会被重试，`tries` 就是第几次。
        不管哪一次，都**不静默吞**。
        """
        by_key = {a.get("key"): a for a in alerts or []}
        for rec in self.notifier.step(alerts):
            a = by_key.get(rec.get("key")) or {"kind": rec.get("kind"),
                                               "level": rec.get("level"),
                                               "key": rec.get("key")}
            self._emit_alert(rec, a)
            sn = a.get("sn") or ""
            if rec.get("err") == "off":
                continue      # 没配地址，不算投递（只在事件里不写噪声）
            self.tsdb.write_event(
                "notify", sn=sn,
                detail=(f"event={rec.get('event')} kind={rec.get('kind')} "
                        f"level={rec.get('level')} key={rec.get('key')} "
                        f"ok={rec.get('ok')} status={rec.get('status')} "
                        f"tries={rec.get('tries')} "
                        f"err={rec.get('err') or '-'}"))

    def _alert_watch(self):
        """后台告警巡视图：每 `ORPAH_NOTIFY_SEC`（默认 3 s）评估一次并投递。

        为什么必须有这个线程（而不是只在页面轮询时投递）：**没人打开页面时也得能通知到** ——
        那正是通知存在的意义。用 `self.stop.wait()` 等下一轮（不猜次数、不空转）。
        """
        interval = max(1.0, float(ntf._env_int("ORPAH_NOTIFY_SEC", 3)))
        while not self.stop.is_set():
            try:
                v = self.alert_view()
                self.alert_cache = dict(v, t=time.strftime("%H:%M:%S"))
                self._notify_step(v["alerts"])
                # 数据新鲜度：巡视图每转一圈就算“告警扫描在动”（节拍 = 本函数的 interval）
                self.fresh.touch("alert_scan", period=interval)
            except Exception as e:        # 巡视图不能死（死了通知会静默停摆）
                print(f"[notify] 巡视出错：{type(e).__name__}: {e}")
            self.stop.wait(interval)

    def rl_alert_view(self):
        """给告警引擎的限频视图（`alerts.evaluate(ratelimit=…)`）。

        只给形状需要的最小集：累计计数 + 最近一次丢弃的时刻/防线（不搬整张桶表）。
        **两侧合并**：对“有人在刷”这件事而言，是 Server 还是 Router 拦下的不重要；
        页面/告警上再分说是哪一侧。
        """
        snap = self.rl.snapshot()
        rtr = self.router.rl.snapshot() if self.router else None
        drop = dict(snap["dropped"])
        last = self.rl.last_drop
        if rtr:
            drop = {"sn": drop["sn"] + rtr["dropped"]["sn"],
                    "router": drop["router"] + rtr["dropped"]["router"],
                    "total": drop["total"] + rtr["dropped"]["total"]}
            # 取两侧更晚的那次（两侧的 t 都是 epoch 秒）
            lt = (self.router.rl.last_drop or {}).get("t") or 0
            if lt > ((last or {}).get("t") or 0):
                last = dict(self.router.rl.last_drop, side="router")
        return {"on": snap["on"], "dropped": drop, "last_drop": last}

    def flood(self, n=200, rotate=False, timeout=30.0):
        """演示限频（§5.8）：连发 n 条 ID-REPORT，看有多少被丢。

        **走真实链路**（client→STA→AP→router→UDP→server），不是"直接调限频器" ——
        否则演示的与代码里跑的不是一回事。
          · `rotate=False`：同一 SN 连发 → 命中 **per-SN** 防线（页面 `which=sn`）；
          · `rotate=True`：每条换一个 SN（源地址不变）→ per-SN 桶每台都拿满桶、
            **根本拦不住**（这正是"必须两条防线"的证据）→ 由 **per-Router** 桶拦。
            轮换用的报文是**最简骨架**（未签名）：限频层本来就不该信任报文内容，
            这里要看的只是"这条线怎么反应"。

        判据用 `wait_until` 等**服务端真的处理完**（按计数增量）；超时**可见地报**，
        不静默返回一个好看的数字。
        """
        n = max(1, min(int(n or 200), 2000))          # 上限防手滑（2000 条 ≈ 几秒）
        before_a = self.srv.id_report_total
        before_d = self.srv.rl_dropped
        rtr0 = self.router.rl_dropped if self.router else 0
        sn0 = self.client.sn if self.client else self.sn
        for i in range(n):
            if rotate:
                self._inject_id(P.build_id_report(
                    {"payload": {"sn": f"CN-WH01-RL{i:04d}"}}), force=True)
            else:
                # 真签名（含 ECDSA 开销：这才叫刷量）
                # `force=True`：刷量演示模拟的就是“一台失控/被改的设备” —— 那正是不做
                # 自限频的情形（设备侧自限频是自愿的，这里用 force 把它诚实地摆出来）。
                self._send_id_report(force=True)
        # 判据：**两侧合计**（Router 侧限的是带宽，它丢的报文根本到不了 Server ——
        # 只看 server 侧计数会永远等不到；2026-09-13 实测踩过）。
        done = wait_until(
            lambda: (self.srv.id_report_total - before_a
                     + self.srv.rl_dropped - before_d
                     + (self.router.rl_dropped - rtr0 if self.router else 0)) >= n,
            timeout=timeout, interval=0.02)
        acc = self.srv.id_report_total - before_a
        drop = self.srv.rl_dropped - before_d
        drop_rtr = (self.router.rl_dropped - rtr0) if self.router else 0
        if not done:
            print(f"[ui] 警告：刷量 {n} 条在 {timeout}s 内未被服务端处理完"
                  f"（已处理 {acc + drop + drop_rtr} 条）")
        return {"ok": bool(done), "sn": sn0, "rotate": bool(rotate), "sent": n,
                # 注意：这几个数含**同一时刻的周期报文**（同一 SN 的 L2 REPORT 也会被限）
                "accepted": acc, "dropped": drop, "dropped_router": drop_rtr}

    def _on_ratelimit(self, rec):
        """被限频丢弃（**Server 侧与 Router 侧共用**这个回调）：留痕 + 落库。

        两侧的区别只在 `rec["side"]`（`server` / `router`）与 `which` 的含义：
          · Server 侧：`sn` = per-SN 桶（省验签 CPU）、`router` = per-Router 桶（源地址）；
          · Router 侧：`sn` = 转发按 SN 限（省带宽）、`router` = per-源MAC（未签名的 REQ-CONNECT）。

        **与 `id_reject` 分开**：那些是过了限频但**验签链**判不合格（伪造/重放/吊销）；
        这里是**根本没让进验签**（省 CPU / 省带宽）。混在一起会让“被哪道防线拒”失真，
        也会污染签名失败率告警（`sig_fail_rate`）。
        """
        self.rl_events.insert(0, rec)
        del self.rl_events[20:]
        self.tsdb.write_event(
            "ratelimit", sn=rec.get("sn", "") if rec.get("sn") != "-" else "",
            detail=(f"side={rec.get('side', 'server')} which={rec.get('which')} "
                    f"mtype={rec.get('mtype')} router={rec.get('router')} "
                    f"retry_after={rec.get('retry_after')}"))

    def _on_id_report(self, rec):
        """Server 验签结果回调：更新卡片 + 签名上报流（带 trust）。"""
        self.id_demo = {
            "t": rec["t"], "sn": rec["sn"], "alg": rec["alg"],
            "gen": rec.get("gen"), "kid": rec.get("kid"),
            "level": rec["level"], "trust": rec["trust"],
            "accepted": rec["accepted"], "sig": rec.get("sig", ""),
            "nonce": rec.get("nonce", ""), "error": rec.get("error"),
            # §8.3：L2 = SE 不可用（仍更新定位，标 degraded）/ L3 = 无可用密钥（只做覆盖发现）
            "degraded": rec.get("degraded"),
            "coverage_only": rec.get("coverage_only"),
            # 时钟可信：设备无时钟（ts=0）→ 卡片上标一句，别让人以为设备报了 1970
            "ts_src": rec.get("ts_src"), "ts_eff": rec.get("ts_eff"),
            # 能力声明（2026-09-13）：None=未声明 / True=有 RTC / False=无 RTC
            "cap_rtc": rec.get("cap_rtc"), "ts_ok": rec.get("ts_ok"),
        }
        self.id_report_total += 1
        # 数据新鲜度：**服务端真的收到并走完验签**才算“ID 上报流在动”——
        # 在本机注入处打点会把“链路断了（帧没到）”也标成新鲜。
        self.fresh.touch("id_report", period=self.every)
        self.id_reports.appendleft(rec)
        self._emit("id_report", rec)
        # 攻击流量面板：这条验签结果是不是我们注入的攻击流量（按 nonce 认领）——
        # 认得下就进**攻击流量流**，正常周期上报不受影响（两条流分开，页面不用再猜）。
        self._spoof_claim(rec)
        # 「最近见」只由**能当人员出现**的结果刷新（§8.3）：`orpah_id.counts_as_presence`
        # = accepted 且非 coverage_only → L0/L1/L2 算，**被拒的（含伪造）与 L3 不算**。
        # 2026-09-12 实测踩过：原来无条件 touch，一条验签失败的伪造上报就把 last_seen 从
        # 1789199499 推到 1789199504 → 伪造报文能“报平安”，把「长未上报」告警永远抑住。
        if rec.get("sn") and oid.counts_as_presence(rec):
            self.registry.touch(rec["sn"])
        # 落库（内存 deque 有上限，历史看「事件历史」区）
        # 验签不通过 → 单记 id_reject，才能在事件历史里按类型筛出「被拒上报」
        # gen = 命中的密钥代次（审计“用的是哪一代钥匙”，与 /api/keys 对得上）
        self.tsdb.write_event(
            "id_report" if rec.get("accepted") else "id_reject",
            sn=rec.get("sn", ""),
            detail=(f"alg={rec.get('alg')} level={rec.get('level')} "
                    f"trust={rec.get('trust')} "
                    f"accepted={rec.get('accepted')}" +
                    # §8.3 降级标记（只在成立时写，L0/L1 的审计行保持与原来逐字一致）
                    (" degraded=1" if rec.get("degraded") else "") +
                    (" coverage_only=1" if rec.get("coverage_only") else "") +
                    (f" gen={rec.get('gen')}" if rec.get("gen") is not None else "") +
                    # 设备无时钟（ts=0）时如实标出：审计要能区分“设备说的时间”与“服务器看到的时间”
                    (" ts_src=server" if rec.get("ts_src") == P.TS_SRC_SERVER else "") +
                    # 能力声明（2026-09-13）：1=声明有 RTC、0=声明无 RTC、缺省=未声明
                    (" cap_rtc=%d" % int(bool(rec.get("cap_rtc")))
                     if rec.get("cap_rtc") is not None else "") +
                    (f" err={rec.get('error')}" if rec.get("error") else "")))

    # ---------------- 密钥库（P1 密钥/证书生命周期） ----------------
    def keys_view(self):
        """密钥库全量视图 → {ok, now, grace_sec, counts, items:[{sn,device,revoked,keys}]}。

        除已有密钥的 SN，也把「已入清册但还没发钥」的 SN 列进去（keys=[]），
        方便在页面上直接给它签发第一代密钥（产线先发钥、设备后入网也合理）。
        顺手 sweep 一次（宽限已过的 grace → retired）；只在真有转移时写库。
        """
        now = int(time.time())
        self.id_ks.sweep(now)
        sns = set(self.id_ks.sns()) | set(self.registry.devices.keys())
        items = [self.id_ks.to_dict(sn, registry=self.registry, now=now)
                 for sn in sorted(sns)]
        states = [k["state"] for it in items for k in it["keys"]]
        return {"ok": True, "now": now, "grace_sec": self.id_ks.grace_sec,
                # 演示终端（Orpah ID 设备）：页面据此标出「当前用哪一代」并给更新按钮
                "demo": {"sn": (self.client.sn if self.client else self.sn),
                         "gen": (self.id_dev.gen
                                 if self.id_dev is not None else None)},
                "counts": {
                    "sn": len(items),
                    "active": states.count(oid.KEY_ACTIVE),
                    "grace": states.count(oid.KEY_GRACE),
                    "retired": states.count(oid.KEY_RETIRED),
                    "revoked_sn": sum(1 for it in items if it["revoked"]),
                },
                "items": items}

    # ---------------- 控制（前端按钮） ----------------
    def _energy_params(self):
        """能量模型当前参数（GET /api/energy 与 /api/status 共用一份形状）。"""
        return {"harvest_mw": self.en_harvest, "charge_mj": round(self.en_charge, 2),
                "store_mj": self.en_store, "push": self.en_push,
                "speedup": self.en_speedup, "listen_interval_s": self.en_listen}

    def energy_axis(self):
        """能量轴：扫采集功率 → 每点策略 + 头条数字（「要多少 mW 才持续跟得住人」）。

        横轴上限取 `max(2×当前采集, 0.5 mW)`（保证当前工作点一定在图上），13 个点。
        **扫的是模型**（与演示加速倍数无关）：页面另外乘 `en_speedup` 显示“页面周期”。
        """
        h_max = max(self.en_harvest * 2.0, 0.5)
        ax = en.axis(n=13, h_max=h_max, charge_mj=self.en_charge, store_mj=self.en_store,
                     sleep_mw=self.cal.sleep_mw, cost=self.cal.cost,
                     listen_mj=self.cal.listen_mj, listen_interval_s=self.en_listen)
        for r in ax["rows"]:                       # 附带“页面周期”（演示加速后）
            r["every_s"] = (None if r["interval_s"] is None
                            else round(max(0.5, r["interval_s"] / self.en_speedup), 2))
        ax["speedup"] = self.en_speedup
        ax["h_max"] = round(h_max, 3)
        return ax

    def energy_view(self):
        """能量轴的完整视图（GET /api/energy）：参数 + 当前状态 + 扫描表。"""
        return {
            "ok": True,
            "on": self.en_on,
            "params": self._energy_params(),
            "defaults": {"cost_mj": en.COST_MJ, "sleep_mw": en.SLEEP_MW,
                         "listen_mj": en.LISTEN_MJ,
                         "listen_interval_s": en.LISTEN_INTERVAL_S,
                         "min_interval_s": en.MIN_INTERVAL_S,
                         "max_useful_interval_s": en.MAX_USEFUL_INTERVAL_S,
                         "charge0_mj": en.CHARGE0_MJ, "store_mj": en.STORE_MJ,
                         "emergency_interval_s": en.EMERGENCY_INTERVAL_S,
                         "cell_empty_mv": en.CELL_EMPTY_MV, "cell_full_mv": en.CELL_FULL_MV},
            "state": self.en_state,
            "axis": self.energy_axis(),
            # 标定出处（2026-09-13）：生效值/演示值/逐项来源/算式/错误与提示，都在 `calib` 里。
            # 页面**不写第二份字段表**，直接画 `calib.rows`。
            "calib": self.cal.view(),
            "note": "parameters are DEMO values unless measured calibration is loaded",
        }

    def energy_snapshot(self):
        """告警用的能量快照 `{sn: {...}}`（最后一条**已签**上报里的电量 + 模型推算）。

        只吃已签上报里的 `battery_mv`（在签名预像内 → 设备不能抵赖"我快没电了"），
        逐设备取最新一条；`silence_in_s` 只有演示设备（当前能量模型）才有，其它设备为 None。
        """
        out = {}
        for rec in list(self.id_reports):          # 最新在前
            sn = rec.get("sn")
            if not sn or sn in out:
                continue
            st = self.en_state or {}
            out[sn] = {"mv": rec.get("battery_mv"),
                       "silence_in_s": st.get("silence_in_s") if self.en_on else None,
                       "level": rec.get("alg"), "degraded_reason": rec.get("degraded_reason"),
                       "since": rec.get("ts_eff")}
            if len(out) >= 20:
                break
        return out

    def status(self):
        conn_a = self.cores[0].wifi.conn_str() if self.cores else "-"
        conn_b = self.cores[1].wifi.conn_str() if len(self.cores) > 1 else "-"
        # 模块空口数据帧计数：wifi.tx_pkts/rx_pkts 只计 DATA 帧（不含 beacon/关联帧），
        # 反映真实数据面。coreA=AP(Router 侧)、coreB=STA(Client 侧)；单向上行 → STA 发/
        # AP 收增长，反向恒 0（真实）。
        tx_ap = self.cores[0].wifi.tx_pkts if self.cores else 0
        rx_ap = self.cores[0].wifi.rx_pkts if self.cores else 0
        tx_sta = self.cores[1].wifi.tx_pkts if len(self.cores) > 1 else 0
        rx_sta = self.cores[1].wifi.rx_pkts if len(self.cores) > 1 else 0
        rows = []
        # 只读快照：本函数被 HTTP 线程调用，而 `_remember`（上报线程）会 append 到
        # `order` 并在超过 50 条时 `pop(0)` + `reports.pop()`。若正好卡在这两条语句之间，
        # 我们手上的 seq 已从 reports 里消失 → 直接索引会抛 KeyError（HTTP 500，
        # 下一轮轮询自愈，但没必要让它发生）。用 .get() 跳过即可，**不必加锁**：
        # 每个结构只有一个写者（order/reports 同属 _remember），列表读迭代最多跳一条，
        # 下轮就补齐；其余环形缓冲在返回前都已 list() 拷贝。
        for seq in list(self.order):
            r = self.reports.get(seq)
            if r is None:
                continue
            m = r["msg"]
            rows.append({
                "seq": seq, "sn": m.get("sn"), "ts": m.get("ts"),
                # 时钟可信：设备无时钟（ts=0/缺失）时，把「实际用于记录的时刻 + 来源」带给前端
                # （前端据此把时间列显示成服务器接收时刻并加 *，否则界面显示 1970 而库里是现在）
                "ts_src": m.get("ts_src"), "ts_eff": m.get("ts_eff"),
                "rssi": m.get("rssi"), "client": "client" in r["seen"],
                "router": "router" in r["seen"], "server": "server" in r["seen"],
            })
        return {
            "client_sent": self.client_sent,
            "router_up": self.router_up,
            "server_recv": self.server_recv,
            # 按内容分色显示的第二个维度（见 index.html 拓扑）
            "id_sent": self.id_sent,
            "router_id_up": self.router_id_up,
            "tx_sta": tx_sta, "rx_sta": rx_sta,
            "tx_ap": tx_ap, "rx_ap": rx_ap,
            "conn_a": conn_a, "conn_b": conn_b,
            "sn": self.client.sn if self.client else "-",
            "router_lost_recv": self.router.lost_push_recv if self.router else 0,
            # 下行来源校验（A 方案，2026-09-13）：被丢掉的下行报文数 —— 非 0 就意味着
            # “有东西在往 Router 的 UDP 端口发包但不是 Server”，页面/日志上都应该看得到。
            "router_down_rejected": self.router.down_rejected if self.router else 0,
            "every": self.every, "paused": self.paused,
            "reports": list(reversed(rows)),
            "flow": list(self.flow),
            "lost": self.lost,
            "lost_sns": self.registry.lost_sns(),
            "tsdb": self.tsdb.available,
            # 数据新鲜度（UI ④，2026-09-13）：每条流「最近一次真的动了」+ 按各自节拍算的状态。
            # `age_s` 在**服务端**算 —— 浏览器与本机时钟不一致时，年龄不该由前端自己减。
            # 诚实边界：它只回答「我们这侧还在不在推进」，**不代表对端设备在线**。
            "fresh": self.fresh_view(),
            "publishes": list(self.publishes),
            "publish_total": self.publish_total,
            "founds": list(self.founds),
            "found_total": self.router.found_count if self.router else 0,
            "found_recv": self.srv.found_count if self.srv else 0,
            "id_demo": self.id_demo,
            "id_reports": list(self.id_reports),
            "id_report_total": self.id_report_total,
            "id_level": self.id_level,               # 当前降级模式（§8.2 演示）
            "id_level_modes": list(ID_LEVEL_MODES),  # 页面下拉选项（单一源）
            "clock": self.clock.snapshot(),          # 设备时钟偏移/漂移估计（估计，不改数据）
            "clock_off": getattr(self.client, "ts_off", 0) if self.client else 0,
            # 设备能力声明（2026-09-13）：None=未声明 / True / False；ts_broken = 自报 ts 置 0
            "id_cap_rtc": self.id_cap_rtc,
            "id_ts_broken": self.id_ts_broken,
            # 能量轴（2026-09-13）：开启后由能量模型驱动间隔/级别（见 _energy_step）
            # 形状与 GET /api/energy 的 `on/params/state` 一致（页面同一份渲染代码吃两种来源）；
            # 但**不含**扫描表 —— 那个只在 /api/energy 与这里的 `energy_axis` 出，别塞进 1s 轮询。
            "energy": {"on": self.en_on, "params": self._energy_params(),
                       "state": self.en_state,
                       # 标定出处（1s 轮询只带**徽标要的**那几个数）：整表/算式/错误在 /api/energy
                       "calib": {"ok": self.cal.ok, "source": self.cal.source,
                                 "source_i18n": "en_cal_src_" + self.cal.source,
                                 "n_measured": self.cal.n_measured,
                                 "n_total": self.cal.n_total}},
            "energy_axis": self.energy_axis(),
            # 限频（§5.8，2026-09-13）：形状 `on/params/counters/...`（单一源，页面不写死参数）。
            # `recent` 用 ui_server 自己那份（server 的环形也可，但这里要保证与审计事件同序）。
            "ratelimit": dict(self.rl.snapshot(), recent=self.rl_events[:5]),
            # Router 侧（§5.8 的两行：转发按 SN / 未签名 REQ-CONNECT 按源 MAC）——
            # 与 Server 侧**分开报**：两边参数不同、丢的后果也不同（砍带宽 vs 砍 CPU），
            # 合成一个数就说不清“报文死在哪一段”了。
            "ratelimit_rtr": dict(
                self.router.rl.snapshot() if self.router else {},
                dropped_total=self.router.rl_dropped if self.router else 0,
                recent=list(self.router.rl_drops)[:5] if self.router else []),
            # 设备侧（§5.8 里“设备自己那一环”，2026-09-13）：**自愿**自限频，**不是防线**。
            # `held` 是**延后**（下一拍还会发）—— 页面不得把它写成“丢弃”（会让人以为漏报了）。
            "selflimit": (self.client.limiter.snapshot() if self.client else {}),
            # 防 spoof 演示的攻击清单（脚本/UI 同一份，见 spoof.py；页面按语言取 zh/en）
            # `line` = 它**期望**被哪道防线拦（`spoof.DEFENSES` 单一源；None=应被接受）
            "spoof_kinds": [{"kind": k, "zh": spoof.case_info(k)[0],
                             "en": spoof.case_info(k)[1],
                             "expect": spoof.case_info(k)[2],
                             "line": spoof.defense_of(spoof.case_info(k)[2]),
                             "note_zh": spoof.case_note(k, "zh"),
                             "note_en": spoof.case_note(k, "en")}
                            for k in spoof.UI_KINDS],
            # 攻击流量（2026-09-13，独立面板 `attack.html`）：**只含攻击报文**——
            # 与上面混排的 `id_reports` 分开，页面不用再从签名上报流里猜哪条是攻击。
            "spoof": self.spoof_view(),
            # 下行真实性（F-14 B，2026-09-13）：Server 签了多少 + Router 验了多少/拒了多少。
            # Router 侧 `on=false` 或 `unverified>0` 必须看得见 —— 那意味着这段路
            # 现在**只有来源校验（A）**，不得读成“已经防住了”。
            "downlink": {
                "server": (self.srv.downlink_view() if self.srv else {}),
                "router": (self.router.downlink_view() if self.router else {}),
                "pub_path": self.down_pub_path, "key_src": self.down_key_src,
            },
            "id_revoked": (self.id_ks.is_revoked(self.client.sn)
                           if self.client else False),
        }

    def fresh_view(self):
        """数据新鲜度视图（UI ④，2026-09-13）—— 4 条流的单一源。

        只列**周期性**的东西（各自有真实节拍）；事件型（发现/走失表）不入列：
        它们本来就是“几天才动一次”的，列出来只会一直红了（那是噪声不是信号）。

        IoTDB 落库行多给一个 `on`：**没启用**与**本该写却停了**是两回事，
        页面文案也不同（一个中性、一个告警），不许合成一句。
        """
        rows = self.fresh.rows()
        rows.append(fr.tsdb_row(self.tsdb.write_state(), self.every))
        return {"now": round(time.time(), 3), "worst": self.fresh.worst(rows),
                "rows": rows}

    def cmd(self, action, sn=None, every=None, note="", kind=None, level=None, sec=None,
            rtc="__missing__", on=None, n=None, rotate=None, url=None, resolve=None):
        if action == "pause":
            self.paused = True
        elif action == "resume":
            self.paused = False
        elif action == "set_sn" and sn:
            if self.client:
                self.client.sn = sn
                self._ensure_id_device()   # 按新 SN 重建 Orpah ID 设备并重注册密钥
        elif action == "every" and every and every > 0:
            self.every = float(every)
        elif action == "clock_off":
            # 演示设备时钟偏移（秒）：让设备自报的 ts = now + N，服务器侧靠估计器看出来。
            # 只影响**设备自报 ts**，不改服务器记录的时间语义（§5.5）。
            if self.client is not None and isinstance(sec, (int, float)):
                self.client.ts_off = int(sec)
            return {"ok": True, "clock_off": getattr(self.client, "ts_off", 0)}
        elif action == "cap":
            # 设备能力声明（2026-09-13）：rtc=true/false/null（null = 不声明，回老行为）。
            # 同时应用到**业务报文**（client.cap_rtc，未签名 → 只能当提示）与
            # **已签 ID 报告**（在 JCS 预像里 → 权威、篡改即验签失败）。
            if rtc != "__missing__":
                val = None if rtc is None else bool(rtc)
                self.id_cap_rtc = val
                if self.client is not None:
                    self.client.cap_rtc = val
            return {"ok": True, "id_cap_rtc": self.id_cap_rtc}
        elif action == "ts_broken":
            # 演示“设备没有可用时钟”：ID 上报与业务报文的 ts 一律置 0
            # （§5.5：ts=0 → 验签跳过时间窗，服务端用接收时刻记账）。
            if on is not None:
                self.id_ts_broken = bool(on)
                if self.client is not None:
                    self.client.ts_broken = bool(on)
            return {"ok": True, "id_ts_broken": self.id_ts_broken}
        elif action == "id_level":
            # 演示降级策略（§8.2）：切换"哪个环节坏了"，下一条 ID 上报就走对应的降级路径。
            # 非法值不改（返回实际生效值，前端据此回弹）
            if level in ID_LEVEL_MODES:
                self.id_level = level
            return {"ok": True, "id_level": self.id_level}
        elif action == "mark" and sn and self.srv:
            self.srv.mark_tracked(sn, note=note or "ui")
        elif action == "untrack" and sn and self.srv:
            self.srv.untrack(sn)
        elif action == "revoke" and self.client:
            self.id_ks.revoke(self.client.sn)
        elif action == "unrevoke" and self.client:
            self.id_ks.unrevoke(self.client.sn)
        elif action == "replay" and self._last_id_report:
            # 重放演示：注入的是“刚才那一条”（别的设备/攻击者行为）→ 绕过自限频
            self._inject_id(self._last_id_report, force=True)
        elif action == "stale":
            self._send_id_report(ts=int(time.time()) - 3600, force=True)
        elif action == "spoof":
            # 响应**始终带 ok**（2026-09-12 review）：以前失败路径只回 {"err":…}，
            # 调用方得靠“没有 ok”推断失败；现在显式 false。前端 `if (!info.ok)` 两种都能工作。
            ok, info = self.spoof_attack(kind or "legit")
            return dict({"ok": bool(ok)}, **info)
        elif action == "flood":
            return self.flood(n=n, rotate=bool(rotate))
        elif action == "spoof_reset":
            # 攻击流量面板：清计数与结果（**不**动密钥库、不动限频桶 —— 与 rl_reset 同一取舍：
            # 只清"面板上的数"，不清"防线本身"，否则就把演示变成假的了）
            self.spoof_reset()
            return {"ok": True}
        elif action == "notify_set":
            # 告警通知配置（运行时改，不重启）：url（空=关闭）/ level（warn|crit）/ resolve
            if url is not None:
                self.notifier.set_url(url)
            if level is not None:
                self.notifier.set_min_level(level)
            if resolve is not None:
                self.notifier.set_resolve(resolve)
            return {"ok": True, "notify": self.notifier.snapshot()}
        elif action == "notify_test":
            # 「测试发送」：立刻推一条，返回投递记录（ok/status/err 供页面显示）。
            # 写审计（人为操作）—— 与其它人为动作一样要留痕。
            rec = self.notifier.test()
            self.tsdb.write_event(
                "notify", sn="",
                detail=(f"event=test url={self.notifier.url or '-'} "
                        f"ok={rec.get('ok')} status={rec.get('status')} "
                        f"err={rec.get('err') or '-'}"))
            return {"ok": True, "test": rec, "notify": self.notifier.snapshot()}
        elif action == "rl_reset":
            # 演示用：清零计数后重新刷量，页面上的数字才对得上（**不**清桶本身 ——
            # 清了就等于“把限频关一下”，会把演示变成假的）
            self.rl.reset_counters()
            if self.client is not None:      # 设备侧计数一并清（三侧同拍，页面对得上）
                self.client.limiter.reset_counters()
            self.rl_events = []
            return {"ok": True}
        return {"ok": True}

    def stop_all(self):
        self.stop.set()
        if self.client:
            self.client.close()
        if self.router:
            self.router.stop()
        if self.srv:
            self.srv.stop()
        self.tsdb.close()


APP = None


# ---------------------------------------------------------------------------
# HTTP / SSE
# ---------------------------------------------------------------------------
import http.server                                       # noqa: E402
import socketserver                                      # noqa: E402

STATIC_DIR = os.path.join(HERE, "ui", "static")
# 回放单次返回的上报点上限（防一把抱走整库；超了页面提示 truncated）
REPLAY_MAX_POINTS = 20000


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _pack_tile(self):
        """野外包瓦片：`/maps/<包名>/<z>/<x>/<y>.png`（形状与路径安全见 `maps.resolve_tile()`）。

        只允许那一种形状 → 路径穿越 / 隐藏目录 / 奇怪扩展名一律 **404**；
        包或瓦片不在也是 404 —— 页面据此走「连续失败 → 回落网格底图」（`map.js`），
        所以**不留白、也不静默**。
        **不整文件之外的任何东西**：不支持 `Range`（那是 PMTiles 方案的前提，未做，见 maps.py 头）。
        """
        rel = self.path.split("?", 1)[0].lstrip("/")[len("maps/"):]
        p, _why = maps.resolve_tile(rel.strip("/"))
        if not p or not os.path.isfile(p):
            self._send(404, b"tile not found", "text/plain")
            return
        ctype = maps.TILE_EXT[os.path.splitext(p)[1].lower()]
        with open(p, "rb") as f:
            self._send(200, f.read(), ctype, {"Cache-Control": "no-store"})

    def do_GET(self):
        global APP
        if self.path == "/api/status":
            self._send(200, json.dumps(APP.status()).encode())
            return
        if self.path == "/api/registry":
            self._send(200, json.dumps(APP.registry.to_dict()).encode())
            return
        if self.path == "/api/cases":
            self._send(200, json.dumps(APP.cases.to_dict(APP.registry)).encode())
            return
        if self.path == "/api/stations":
            self._send(200, json.dumps(APP.stations.to_dict()).encode())
            return
        if self.path == "/api/keys":
            self._send(200, json.dumps(APP.keys_view()).encode())
            return
        if self.path == "/api/config":
            # 标定参数（A/n/噪声…）：以 motion.py 为唯一源，页面开页取默认值用。
            # 见 motion.calibration() 与 test_motion.py 的「单源守卫」。
            self._send(200, json.dumps({"ok": True, **motion.calibration()}).encode())
            return
        if self.path == "/api/alerts":
            # 无状态评估：每次用当前快照重算活跃告警（规则见 alerts.py）。
            # 与后台巡视线程（`_alert_watch`）**共用同一个视图函数** —— 页面看到的与
            # 通知推的必须是同一份判定，否则会出现「页面红点亮了但没通知」这类对不上。
            self._send(200, json.dumps(APP.alert_view()).encode())
            return
        if self.path == "/api/maps":
            # 野外包（本地 XYZ 瓦片目录）列表：页面「瓦片源 = 野外包」的选项来源。
            # 列表为空是**正常状态**（仓库不带包，见 maps.py 头注释）——页面译成
            # 「没有找到野外包 + 怎么做」，不是错误。
            self._send(200, json.dumps({"ok": True, "dir": maps.MAPS_DIR,
                                        "packs": maps.list_packs()}).encode())
            return
        if self.path.startswith("/api/energy"):
            self._send(200, json.dumps(APP.energy_view()).encode())
            return
        if self.path.startswith("/api/ts/query"):
            self._api_ts_query()
            return
        if self.path.startswith("/api/ts/events"):
            self._api_ts_events()
            return
        if self.path.startswith("/api/metrics"):
            self._api_metrics()
            return
        if self.path.startswith("/api/replay"):
            self._api_replay()
            return
        if self.path.startswith("/api/truth"):
            self._api_truth()
            return
        if self.path.startswith("/api/checksum"):
            self._api_checksum()
            return
        if self.path == "/api/events":
            self._sse()
            return
        if self.path.startswith("/maps/"):
            self._pack_tile()
            return
        rel = self.path.lstrip("/")
        if "?" in rel:
            rel = rel.split("?", 1)[0]
        if rel == "":
            rel = "index.html"
        p = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(p):
            self._send(404, b"not found", "text/plain")
            return
        ctype = {"html": "text/html", "js": "application/javascript",
                 "css": "text/css", "png": "image/png", "jpg": "image/jpeg",
                 "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
                 "svg": "image/svg+xml"}.get(p.rsplit(".", 1)[-1], "text/plain")
        # uploads/（照片，uuid 命名）与 vendor/（第三方库，引用处带 ?v= 版本号）内容按 URL 不变
        # → 长缓存；其余（自己写的 html/js/css）一律 no-store，改完刷新即可见，不让"改了不生效"。
        if rel.startswith("uploads/") or rel.startswith("vendor/"):
            cache = "public, max-age=86400, immutable"
        else:
            cache = "no-store"
        with open(p, "rb") as f:
            self._send(200, f.read(), ctype,
                       headers={"Cache-Control": cache})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        last_hb = time.time()
        try:
            while not APP.stop.is_set():
                try:
                    item = EVENTS.get(timeout=1)
                except queue.Empty:
                    item = None
                if item is None:
                    if time.time() - last_hb > 15:
                        last_hb = time.time()
                        self.wfile.write(b": hb\n\n")
                        self.wfile.flush()
                    continue
                self.wfile.write(("data: " + json.dumps(item) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass

    def _api_registry(self):
        """设备清册增删改：add_person / add_device / remove_device /
        update_person / remove_person / set_status / set_status_many。"""
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "err": str(e)}).encode())
            return
        action = req.get("action")
        try:
            if action == "add_person":
                pid = APP.registry.add_person(
                    req.get("name", ""), req.get("note", ""),
                    req.get("gender", ""), req.get("age", ""),
                    req.get("photo", ""), req.get("height", ""),
                    req.get("build", ""), req.get("features", ""),
                    req.get("health", ""), req.get("mental", ""),
                    req.get("communicate", ""))
                self._send(200, json.dumps({"ok": True, "pid": pid}).encode())
            elif action == "add_device":
                sn = req.get("sn", "").strip().upper()
                if not oid.sn_ok(sn):
                    self._send(200, json.dumps({"ok": False,
                        "err": f"SN 格式非法（{oid.sn_err(sn)}）"}).encode())
                    return
                if not oid.verify_check(sn):
                    self._send(200, json.dumps({"ok": False,
                        "err": "SN 校验位错误"}).encode())
                    return
                pid = req.get("person_id") or None
                if pid is not None and APP.registry.get_person(pid) is None:
                    self._send(200, json.dumps({"ok": False,
                        "err": "绑定的走失者不存在"}).encode())
                    return
                # org/cc 一律由服务端从 SN 解析（契约见 API.md §1），不信任前端传值
                APP.registry.register(sn, person_id=pid, org=None, cc=None)
                self._send(200, json.dumps({"ok": True}).encode())
            elif action == "remove_device":
                APP.registry.remove_device(req.get("sn", ""))
                self._send(200, json.dumps({"ok": True}).encode())
            elif action == "update_person":
                APP.registry.update_person(req.get("pid", ""),
                                           req.get("name"), req.get("note"),
                                           req.get("gender"), req.get("age"),
                                           req.get("photo"), req.get("height"),
                                           req.get("build"), req.get("features"),
                                           req.get("health"), req.get("mental"),
                                           req.get("communicate"))
                self._send(200, json.dumps({"ok": True}).encode())
            elif action == "remove_person":
                pid = req.get("pid", "")
                if APP.cases.active_case(pid) is not None:
                    self._send(200, json.dumps({"ok": False,
                        "err": "该走失者有未结案件，请先在「走失案件」页结案"}).encode())
                else:
                    APP.registry.remove_person(pid)
                    self._send(200, json.dumps({"ok": True}).encode())
            elif action == "set_status":
                APP.registry.set_status(req.get("sn", ""), req.get("status", ""))
                self._send(200, json.dumps({"ok": True}).encode())
            elif action == "set_status_many":
                ok, missing = APP.registry.set_statuses(req.get("sns", []),
                                                        req.get("status", ""))
                self._send(200, json.dumps({"ok": True, "updated": ok,
                                            "missing": missing}).encode())
            else:
                self._send(200, json.dumps({"ok": False,
                                            "err": f"unknown action: {action}"}).encode())
        except Exception as e:
            self._send(200, json.dumps({"ok": False, "err": str(e)}).encode())

    def _api_upload(self):
        """照片上传：POST /api/upload?filename=<name>，body 为原始图片字节。

        校验扩展名 + 5MB 上限；uuid 重命名防注入/重名；存 static/uploads/。
        """
        from urllib.parse import unquote
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        name = ""
        for kv in qs.split("&"):
            k, _, v = kv.partition("=")
            if k == "filename":
                name = unquote(v)
        ext = os.path.basename(name or "").rsplit(".", 1)[-1].lower()
        ALLOWED = {"jpg", "jpeg", "png", "webp", "gif"}
        if ext not in ALLOWED:
            self._send(400, json.dumps({"ok": False,
                                        "err": "仅支持 jpg/png/webp/gif"}).encode())
            return
        MAX = 5 * 1024 * 1024
        cl = self.headers.get("Content-Length")
        try:
            n = int(cl) if cl is not None else -1
        except (TypeError, ValueError):
            n = -1
        if n > MAX:
            self._send(400, json.dumps({"ok": False, "err": "图片超过 5MB 限制"}).encode())
            return
        # 无/非法 Content-Length（chunked 等）→ 最多读 MAX+1 字节。
        # 必须带套接字超时：keep-alive 连接上等不到 EOF 会永久挂住该连接线程。
        # 超时是**每次 recv 各算**（持续有数据就一直不超时）→ 文案如实写“10 秒内没有收到数据”，
        # 别只说“客户端未关流”（那会让人以为只要在传就不会超时；2026-09-13 按评审意见改准）。
        if n < 0:
            try:
                self.connection.settimeout(10.0)
                data = self.rfile.read(MAX + 1)
            except (TimeoutError, OSError):
                self._send(400, json.dumps({"ok": False, "err": (
                    "读取超时（无 Content-Length：需客户端关流结束上传；"
                    "10 秒内没有收到数据即超时）")}).encode())
                return
            finally:
                # 连接可能已在上面失败/被对端关掉 → `settimeout(None)` 会抛 OSError，
                # 必须吞掉（否则 finally 里的异常会盖掉上面的 400 响应）。
                try:
                    self.connection.settimeout(None)
                except OSError:
                    pass
        else:
            data = self.rfile.read(n)
        if not data:
            self._send(400, json.dumps({"ok": False, "err": "空文件"}).encode())
            return
        if len(data) > MAX:
            self._send(400, json.dumps({"ok": False, "err": "图片超过 5MB 限制"}).encode())
            return
        up = os.path.join(STATIC_DIR, "uploads")
        os.makedirs(up, exist_ok=True)
        fname = f"{uuid.uuid4().hex}.{ext}"
        with open(os.path.join(up, fname), "wb") as f:
            f.write(data)
        self._send(200, json.dumps({"ok": True, "url": "/uploads/" + fname}).encode())

    def _api_cases(self):
        """走失案件：mark 立案 / close 结案（找回或撤销）。"""
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "err": str(e)}).encode())
            return
        action = req.get("action")
        try:
            if action == "mark":
                c, sns, dup = APP.cases.mark(
                    req.get("person_id", ""), APP.registry,
                    missing_at=req.get("missing_at", ""),
                    missing_place=req.get("missing_place", ""),
                    possible_to=req.get("possible_to", ""),
                    clothing=req.get("clothing", ""),
                    contact_phone=req.get("contact_phone", ""),
                    police=req.get("police", ""),
                    police_case_no=req.get("police_case_no", ""),
                    police_station=req.get("police_station", ""),
                    belongings=req.get("belongings", ""),
                    vehicle=req.get("vehicle", ""))
                if c is None:
                    self._send(200, json.dumps({"ok": False,
                                                "err": "该走失者名下无设备"}).encode())
                else:
                    if not dup:
                        for sn in sns:
                            if APP.srv:
                                APP.srv.mark_tracked(sn, note=f"case:{c.case_id}")
                        APP.tsdb.write_event("case_mark", sn=",".join(sns),
                                             detail=c.case_id,
                                             actor=req.get("actor", ""))
                    resp = {"ok": True, "dup": dup, "case_id": c.case_id}
                    if dup:
                        resp["existing_case_id"] = c.case_id
                    self._send(200, json.dumps(resp).encode())
            elif action == "assign":
                # 处置态（2026-09-12 用户定 A 方案）：接手人只用自由文本（复用审计 actor），
                # 不建 operators 表、不做登录；handler 为空 = 取消接手（误点可撤回）。
                c, err = APP.cases.assign(req.get("case_id", ""),
                                          req.get("handler", ""))
                if err:
                    self._send(200, json.dumps({"ok": False, "err_code": err}).encode())
                else:
                    actor = req.get("actor", "") or c.handler
                    APP.tsdb.write_event("case_assign",
                                         detail=f"{c.case_id}:{c.handler}",
                                         actor=actor)
                    self._send(200, json.dumps({"ok": True, "case_id": c.case_id,
                                                "handler": c.handler}).encode())
            elif action == "close":
                outcome_val = req.get("outcome", "")
                if outcome_val not in ("closed", "revoked"):
                    self._send(200, json.dumps({"ok": False,
                                                "err": "非法结案方式"}).encode())
                    return
                c, sns = APP.cases.close(req.get("case_id", ""), APP.registry,
                                         outcome_val)
                if c is None:
                    self._send(200, json.dumps({"ok": False,
                                                "err": "案件不存在"}).encode())
                else:
                    for sn in sns:
                        if APP.srv:
                            APP.srv.untrack(sn)
                    APP.tsdb.write_event("case_close",
                                         detail=f"{c.case_id}:{outcome_val}",
                                         actor=req.get("actor", ""))
                    self._send(200, json.dumps({"ok": True}).encode())
            else:
                self._send(200, json.dumps({"ok": False,
                                            "err": f"unknown action: {action}"}).encode())
        except Exception as e:
            self._send(200, json.dumps({"ok": False, "err": str(e)}).encode())

    def _api_keys(self):
        """密钥库操作（P1 密钥/证书生命周期）：issue / rotate / adopt / retire /
        revoke / unrevoke。每个变更都写审计事件（etype=key_*，带操作者 actor）。

        - issue     给某 SN 签发新一代（已有 active 时幂等，回 note_code=already_active）
        - rotate    轮换：旧 active → grace（now+grace_sec），新钥 → active
        - adopt     让演示终端改用当前 active 代（演示“设备侧完成更新”）
        - retire    强制退役（带 kid 只退该代；不带则退所有非 active）——也可当作
                    “让宽限期立即到期”的按钮
        - revoke    整机作废（所有代立即不验签；不可逆*）
        - unrevoke  撤销的逆操作（*仅演示：代次一律转 retired，要恢复需重新签发）

        **返回机器码**（code / note_code），文案由页面按语言本地化 —— 后端不拼中文，
        免得又变成“后端文案不跟语言走”（tools/ui 那次的教训）。
        """
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "code": "bad_json",
                                        "err": str(e)}).encode())
            return
        action = (req.get("action") or "").strip()
        sn = (req.get("sn") or "").strip()
        kid = (req.get("kid") or "").strip()
        actor = req.get("actor", "")
        ks = APP.id_ks

        def err(code, msg=""):
            self._send(200, json.dumps({"ok": False, "code": code,
                                        "err": msg or code}).encode())

        def done(etype=None, detail="", **extra):
            """写审计 + 回全量密钥视图（前端直接重渲染，避免状态漂移）。"""
            if etype:
                APP.tsdb.write_event(etype, sn=sn, detail=detail, actor=actor)
            resp = {"ok": True, "code": action}
            resp.update(extra)
            resp["view"] = APP.keys_view()
            self._send(200, json.dumps(resp).encode())

        if not sn:
            return err("no_sn")
        # 只允许对「已在密钥库或已在设备清册」的 SN 操作（防手滑打错 SN 造出野钥）
        if sn not in set(ks.sns()) | set(APP.registry.devices.keys()):
            return err("unknown_sn", sn)
        try:
            if action in ("issue", "rotate") and ks.is_revoked(sn):
                return err("revoked_sn")     # 撤销不可逆：需先 unrevoke 再签发

            if action == "issue":
                cur = ks.active_of(sn)
                if cur is not None:
                    return done(note_code="already_active", kid=cur["kid"])
                gen = max([k["gen"] for k in ks.list_keys(sn)] or [0]) + 1
                kid = ks.register(oid.Device(sn=sn, gen=gen),
                                  model=req.get("model") or None,
                                  firmware=req.get("firmware") or None)
                return done("key_issue", kid, kid=kid)

            if action == "rotate":
                cur = ks.active_of(sn)
                gen = (cur["gen"] if cur else 0) + 1
                r = ks.rotate(sn, oid.Device(sn=sn, gen=gen))
                return done("key_rotate",
                            f"{r['prev_kid'] or '-'} -> {r['kid']} "
                            f"grace={r['grace_sec']}s", **r)

            if action == "adopt":
                cur = ks.active_of(sn)
                if cur is None:
                    return err("no_active")
                if APP.id_dev is not None and APP.id_dev.sn == sn:
                    APP.id_dev = None          # 强制重建 → 采用 active 代
                    APP._ensure_id_device()
                    return done(kid=cur["kid"])
                # 不是当前演示终端：只有终端自己的 SN 会真的换钥（页面上不给这个按钮）
                return done(note_code="not_demo_sn", kid=cur["kid"])

            if action == "retire":
                kids = ks.retire(sn, kid=kid or None)
                if not kids:
                    return err("nothing_retire")
                return done("key_retire", ",".join(kids), kids=kids)

            if action == "revoke":
                reason = (req.get("reason") or "").strip()
                rec = ks.revoke(sn, reason=reason, actor=actor)
                return done("key_revoke", reason or "-", revoked=rec)

            if action == "unrevoke":
                ks.unrevoke(sn)
                return done("key_unrevoke", "unrevoke (demo only)")

            return err("unknown_action", action)
        except ValueError as e:
            # 协议层兜底（撤销不可逆）：仍翻译成机器码给页面本地化
            err("revoked_sn" if ks.is_revoked(sn) else "exception", str(e))
        except Exception as e:
            err("exception", str(e))

    def _api_stations(self):
        """定位站位（无人机悬停测点）：增删改 + 打点 + 手动绑定 + 导入悬停计划。

        POST /api/stations
          {action:"add",    x, y, name?}            → 新增站位（返回 sid）
          {action:"update", sid, x?, y?, name?, t0?, t1?}
          {action:"remove", sid}
          {action:"clear"}                          → 清空
          {action:"import", stations:[{sid?,name?,x,y,t0?,t1?}]} → 覆盖式导入悬停计划
          {action:"punch",  sid, t?}                → 打点：此刻在该站位（自动收尾上一个）
          {action:"bind",   sid, rssi, t?}          → 手动绑定一条观测（优先于时间窗）
          {action:"unbind", sid}                    → 取消手动绑定 → 回落到时间窗

        每个 action 都回全量站位表，前端直接重渲染，避免两边状态漂移。
        """
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "err": str(e)}).encode())
            return
        action = req.get("action")
        try:
            if action == "add":
                st = APP.stations.add(req.get("x", 0), req.get("y", 0),
                                      req.get("name", ""))
                resp = {"ok": True, "sid": st.sid}
            elif action == "update":
                fields = {k: req[k] for k in ("name", "x", "y", "t0", "t1", "rssi")
                          if k in req}
                st = APP.stations.update(req.get("sid"), **fields)
                if st is None:
                    self._send(200, json.dumps(
                        {"ok": False, "err": "站位不存在"}).encode())
                    return
                resp = {"ok": True}
            elif action == "remove":
                resp = {"ok": APP.stations.remove(req.get("sid"))}
            elif action == "clear":
                resp = {"ok": True, "removed": APP.stations.clear()}
            elif action == "import":
                resp = {"ok": True, "imported": APP.stations.import_config(req)}
            elif action == "punch":
                st = APP.stations.punch(req.get("sid"), req.get("t"))
                if st is None:
                    self._send(200, json.dumps(
                        {"ok": False, "err": "站位不存在"}).encode())
                    return
                resp = {"ok": True, "t0": st.t0}
            elif action == "bind":
                if req.get("rssi") is None:
                    self._send(200, json.dumps(
                        {"ok": False, "err": "缺少 rssi"}).encode())
                    return
                st = APP.stations.bind(req.get("sid"), req.get("rssi"), req.get("t"))
                if st is None:
                    self._send(200, json.dumps(
                        {"ok": False, "err": "站位不存在"}).encode())
                    return
                resp = {"ok": True, "rssi": st.rssi}
            elif action == "unbind":
                st = APP.stations.unbind(req.get("sid"))
                if st is None:
                    self._send(200, json.dumps(
                        {"ok": False, "err": "站位不存在"}).encode())
                    return
                resp = {"ok": True}
            else:
                self._send(200, json.dumps({"ok": False,
                    "err": f"unknown action: {action}"}).encode())
                return
            resp["stations"] = [s.to_dict() for s in APP.stations.list()]
            self._send(200, json.dumps(resp).encode())
        except Exception as e:
            self._send(200, json.dumps({"ok": False, "err": str(e)}).encode())

    def _api_replay(self):
        """GET /api/replay?sn=<SN>[&from=<ms>&to=<ms>&minutes=N][&evlimit=N]

        回放数据源：**IoTDB 时间窗**（重启不丢、按时间升序） ——
        `points` = 该设备在该窗内的上报点；`events` = 该窗内的业务事件（发布/发现/验签/立案/密钥…）。
        定位计算**不在服务端**：页面拿 points + 站位表用 pos.js 自己算（与实时页同一内核）。

        窗口上限（防一把抱走整库）：`REPLAY_MAX_MIN`（默认 720 分 = 12h）。
        点数/事件数也有上限，超了置 `truncated`。
        """
        from urllib.parse import parse_qs
        qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        sn = (qs.get("sn") or [""])[0].strip()
        if not sn:
            self._send(200, json.dumps({"ok": False, "code": "no_sn",
                                        "err": "no_sn"}).encode())
            return
        now_ms = int(time.time() * 1000)
        try:
            to_ms = int((qs.get("to") or [now_ms])[0])
            minutes = int((qs.get("minutes") or [30])[0])
            ev_limit = int((qs.get("evlimit") or [2000])[0])
        except ValueError:
            self._send(200, json.dumps({"ok": False, "code": "bad_param",
                                        "err": "bad_param"}).encode())
            return
        minutes = min(max(minutes, 1), 720)          # 1 分 ~ 12 小时
        from_q = (qs.get("from") or [""])[0].strip()
        from_ms = _int_arg(from_q, to_ms - minutes * 60_000)
        if from_ms > to_ms:                          # 容错：反了就换过来
            from_ms, to_ms = to_ms, from_ms
        if to_ms - from_ms > 720 * 60_000:
            from_ms = to_ms - 720 * 60_000

        points = APP.tsdb.query_report_range(sn, from_ms, to_ms,
                                             limit=REPLAY_MAX_POINTS)
        events = APP.tsdb.query_events_range(from_ms, to_ms, limit=ev_limit)
        obs = APP.router_obs_range(sn, from_ms, to_ms, limit=REPLAY_MAX_POINTS)
        ts_ok = points is not None
        ev_ok = events is not None
        out = {
            "ok": ts_ok,                # points 拿不到 = IoTDB 未就绪（页面按此提示）
            "sn": sn,
            "from": from_ms, "to": to_ms,
            "points": points or [],
            "obs": obs,                 # {sid: [{t,rssi,seq}]} 各路由器本窗内的测量
            "events": (events or []) if ev_ok else [],
            "counts": {"points": len(points or []),
                       "events": len(events or []),
                       "obs": sum(len(v) for v in obs.values())},
            "truncated": bool(points and len(points) >= REPLAY_MAX_POINTS),
            "events_ok": ev_ok,
        }
        self._send(200, json.dumps(out).encode())

    def _api_truth(self, body=None):
        """模拟器**地面真值**轨迹（`motion.Walk` 的行走模型）—— 只用于演示里量“定位误差”。

        GET  /api/truth?from=<epoch ms>&to=<epoch ms>[&step=<ms>] → **等间隔采样**（画真值路线）
        POST /api/truth   body `{"times": [t1, t2, …]}`           → **指定时刻精确取点**

        两者都在一起返回：`{ok, src:"motion.Walk", step, n, points:[{t,x,y}], speed,
        loop_sec, total_m}`；`--no-walk`（无行走模型）→ `{ok:false, code:"no_walk"}`。

        **为什么既有网格又有逐时刻**：网格点之间要靠页面线性插值，而插值误差随步长**平方**增长
        （实测 250ms 步长 → 2.9cm；12h 窗被点数上限逼到 10.8s/点 → 米级）。算误差 CDF 时
        真值本身不能成为误差源，所以按帧时刻 POST 精确取点；网格那份只用来画路线。

        口径（重要，别在真机上报这个指标）：**地面真值只有演示环境有**（真机部署没有真值）。
        所以页面必须如实标「仅模拟环境」；真机的定位质量看**不需要真值**的 RMS 残差 /
        95% 椭圆 / 搜索半径（`pos.js` 里那套）。

        采样/取点都在 `motion.truth_samples()` / `motion.truth_at()`（单一源，
        `test_motion.py` 锁住“与 walk.pos 逐点一致”）。
        """
        if not APP.walk:
            self._send(200, json.dumps({"ok": False, "code": "no_walk",
                                        "err": "no_walk"}).encode())
            return
        walk = APP.walk
        meta = {"speed": walk.speed, "loop_sec": round(walk.loop_sec, 3),
                "total_m": round(walk.total, 3)}
        if body is not None:
            times = body.get("times") or []
            pts = motion.truth_at(APP.walk, times)
            if not pts:
                self._send(200, json.dumps({"ok": False, "code": "no_times",
                                            "err": "no_times"}).encode())
                return
            self._send(200, json.dumps({
                "ok": True, "src": "motion.Walk", "step": None, "n": len(pts),
                "from": pts[0]["t"], "to": pts[-1]["t"], "points": pts, **meta,
            }).encode())
            return
        from urllib.parse import parse_qs
        qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        now_ms = int(time.time() * 1000)
        try:
            to_ms = int((qs.get("to") or [now_ms])[0])
        except ValueError:
            self._send(200, json.dumps({"ok": False, "code": "bad_param",
                                        "err": "bad_param"}).encode())
            return
        from_q = (qs.get("from") or [""])[0].strip()
        from_ms = _int_arg(from_q, to_ms - 30 * 60_000)
        span = min(abs(to_ms - from_ms), 720 * 60_000)      # 与回放同一上限（12h）
        to_ms = max(to_ms, from_ms)
        from_ms = to_ms - span
        step_q = (qs.get("step") or [""])[0].strip()
        pts, step = motion.truth_samples(
            APP.walk, from_ms, to_ms,
            step=_int_arg(step_q))          # 非法 step → None → 按窗口自适应
        self._send(200, json.dumps({
            "ok": True, "src": "motion.Walk",
            "from": from_ms, "to": to_ms, "step": step, "n": len(pts),
            "points": pts, **meta,
        }).encode())

    def _api_checksum(self):
        """校验码工具：算法单一源（damm32.py / luhn32.py / mod97.py）。

        GET /api/checksum?action=compute&algo=damm32&org_unique=WH01-9AF3C1D2
            action=verify&algo=luhn32&body=WH01-9AF3C1D2-E
            action=table&algo=damm32            → 32×32 拟群表 + 性质
            action=brute&algo=damm32&n=32&maxlen=3 → 穷举验证
        """
        from urllib.parse import parse_qs
        qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")

        def g(k, d=""):
            return (qs.get(k) or [d])[0]

        action = g("action", "compute")
        algo = g("algo", "damm32").lower()
        try:
            if action == "compute":
                org = g("org_unique").strip().upper()
                check = _check_of(algo, org)
                self._send(200, json.dumps({"ok": True, "algo": algo,
                    "org_unique": org, "check": check,
                    "valid": _verify_of(algo, org + "-" + check)}).encode())
            elif action == "verify":
                body = g("body").strip().upper()
                self._send(200, json.dumps({"ok": True, "algo": algo,
                    "valid": _verify_of(algo, body)}).encode())
            elif action == "table":
                if algo != "damm32":
                    self._send(200, json.dumps({"ok": False,
                        "err": f"{algo} 没有拟群表（仅 damm32）"}).encode())
                    return
                T = d32._TABLE
                okv, checks = d32.verify_table(T)
                self._send(200, json.dumps({"ok": True, "algo": "damm32",
                    "crockford": d32.CROCKFORD, "quasigroup": T,
                    "valid": okv, "checks": checks}).encode())
            elif action == "brute":
                n = int(g("n", "32") or 32)
                maxlen = int(g("maxlen", "3") or 3)
                t0 = time.time()
                miss = d32.brute_verify(d32._TABLE, n, maxlen)
                ms = int((time.time() - t0) * 1000)
                self._send(200, json.dumps({"ok": True,
                    "miss": (list(miss) if miss else None), "ms": ms}).encode())
            else:
                self._send(200, json.dumps({"ok": False,
                    "err": f"unknown action: {action}"}).encode())
        except Exception as e:
            self._send(200, json.dumps({"ok": False, "err": str(e)}).encode())

    def _api_ts_query(self):
        """GET /api/ts/query?sn=...&limit=N → 设备上报流 + 各路由器的观测序列。

        `rows` = 设备自己报的链路值（root.orpah.devices.<sn>，时间倒序）；
        `obs`  = {sid: [{t,rssi,seq}]} 各路由器对**该设备**的测量（时间升序，最多 limit 条）。
        定位用的是 obs（同一时刻多台各自的测量）；rows 只用于画链路 RSSI 曲线。
        """
        from urllib.parse import parse_qs
        qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        sn = (qs.get("sn") or [""])[0]
        try:
            limit = int((qs.get("limit") or ["100"])[0])
        except ValueError:
            limit = 100
        rows = APP.tsdb.query_report(sn, limit) if sn else None
        obs = APP.router_obs_recent(sn, limit) if sn else {}
        self._send(200, json.dumps({"ok": rows is not None, "sn": sn,
                                    "rows": rows or [], "obs": obs}).encode())

    def _api_ts_events(self):
        """GET /api/ts/events?limit=N[&etype=][&sn=] → IoTDB 里的业务事件历史。

        事件由本进程写入（publish/found/id_report/id_reject/case_*），**重启后仍在**。
        每行带 actor（操作者，审计「谁」；自动事件 = system）。
        retention_days = 事件保留期限（天，0 = 不清理；见 tsdb.EVENT_RETENTION_DAYS）。
        """
        from urllib.parse import parse_qs
        qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        try:
            limit = int((qs.get("limit") or ["50"])[0])
        except ValueError:
            limit = 50
        limit = min(max(limit, 1), 500)
        etype = (qs.get("etype") or [""])[0].strip()
        sn = (qs.get("sn") or [""])[0].strip()
        rows = APP.tsdb.query_events(limit=limit, etype=etype, sn=sn)
        self._send(200, json.dumps({"ok": rows is not None,
                                    "etype": etype, "sn": sn,
                                    "retention_days": APP.tsdb.event_retention_days,
                                    "purged_upto": APP.tsdb.purged_upto,
                                    "rows": rows or []}).encode())

    def _api_metrics(self):
        """GET /api/metrics?minutes=N[&sn=] → 指标面板数据（ROADMAP §五）。

        窗口：最近 N 分钟（默认 60，上限 1440）。取数：
        - 事件流 `root.orpah.events`（**时间窗**，原生索引）→ 签名失败率 / 算法分布
        - 上报流 `root.orpah.devices.<sn>`（时间窗）→ 平均 RSSI
        - 案件表（SQLite）→ 走失处置时长：**不按窗口切**（一个案子跨小时，
          窗口化会把案子切两半、时长算成错的；口径说明见 metrics.py 头注释）
        计算全在 `metrics.py`（纯函数，`test_metrics.py` 覆盖）；这里只取数与组装。
        """
        from urllib.parse import parse_qs
        qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        try:
            minutes = int((qs.get("minutes") or ["60"])[0])
        except ValueError:
            minutes = 60
        minutes = min(max(minutes, 1), 1440)
        sn = (qs.get("sn") or [""])[0].strip() or (APP.client.sn if APP.client
                                                  else "")
        now_s = int(time.time())
        t1 = now_s * 1000
        t0 = t1 - minutes * 60 * 1000
        events = APP.tsdb.query_events_range(t0, t1, limit=5000)
        reports = APP.tsdb.query_report_range(sn, t0, t1) if sn else []
        tsdb_ok = events is not None and reports is not None
        out = metrics.summarize(events or [], reports or [],
                                list(APP.cases.cases.values()), now=now_s,
                                window={"minutes": minutes, "sn": sn,
                                        "t0": t0, "t1": t1},
                                tsdb=tsdb_ok)
        self._send(200, json.dumps(out).encode())

    def _api_sig(self):
        """数字签名工具：newkey 生成临时密钥对；sign 算预像+签名；verify 验签。"""
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "err": str(e)}).encode())
            return
        action = req.get("action")
        if action == "newkey":
            APP.sig_dev = oid.Device(cc="CN", org="WH01", check="mod97")
            dev = APP.sig_dev
            self._send(200, json.dumps({
                "ok": True,
                "sn": dev.sn,
                "pubkey_pem": oid.pubkey_to_pem(dev.pubkey) if dev.pubkey else None,
                "hmac_hex": dev.hmac_key.hex(),
            }).encode())
            return
        dev = APP.sig_dev
        if dev is None:
            self._send(200, json.dumps({"ok": False, "err": "请先生成密钥"}).encode())
            return
        if action in ("sign", "verify"):
            alg = req.get("alg", "ES256")
            hdr, payload = req.get("hdr"), req.get("payload")
            if not isinstance(hdr, dict) or not isinstance(payload, dict):
                self._send(200, json.dumps({"ok": False,
                                            "err": "hdr/payload 必须是 JSON 对象"}).encode())
                return
            preimage = oid.preimage_of({"hdr": hdr, "payload": payload})
            if action == "sign":
                sig = oid.sign_preimage(alg, preimage, dev)
                self._send(200, json.dumps({
                    "ok": True,
                    "preimage": preimage.decode("utf-8"),
                    "sig": oid.b64url_encode(sig) if sig is not None else None,
                }).encode())
                return
            sig = req.get("sig")
            note = None
            if alg == "ES256":
                ok = bool(sig) and dev.pubkey is not None and oid._verify_es256(dev.pubkey, preimage, sig)
            elif alg == "HS256":
                ok = bool(sig) and oid._verify_hs256(dev.hmac_key, preimage, sig)
            elif alg == "none":
                ok = False
                note = "none 算法无签名，不视为已验证"
            else:
                ok = False
            resp = {"ok": True, "verified": ok}
            if note:
                resp["note"] = note
            self._send(200, json.dumps(resp).encode())
            return
        self._send(200, json.dumps({"ok": False, "err": f"unknown action: {action}"}).encode())

    def do_POST(self):
        global APP
        if self.path.startswith("/api/upload"):
            self._api_upload()
            return
        if self.path == "/api/registry":
            self._api_registry()
            return
        if self.path == "/api/cases":
            self._api_cases()
            return
        if self.path == "/api/stations":
            self._api_stations()
            return
        if self.path == "/api/keys":
            self._api_keys()
            return
        if self.path == "/api/sig":
            self._api_sig()
            return
        if self.path.startswith("/api/energy"):
            # 能量轴参数（2026-09-13）：on/off/set/reset。**每步都回全量视图**，
            # 页面直接重渲染（与 /api/stations 同一习惯，避免状态漂移）。
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "err": str(e)}).encode())
                return
            a = req.get("action")
            if a == "on":
                APP.en_on = True
                APP.en_charge = APP.en_store      # 开启时充满：演示从“能撑”开始看
            elif a == "off":
                APP.en_on = False
                APP.en_state = {}
                APP.id_level_energy = None
            elif a == "reset":
                APP.en_charge = req.get("charge_mj", APP.en_store)
            elif a == "reload":
                # 重新读标定文件（改完文件不用重启 demo）。**会把储能/初始电量按文件重置** ——
                # 与按钮上说的一致（那不是“悄悄改参数”，是“按新标定重新装载”）。
                APP.cal = ecal.load()
                APP.en_store = max(1.0, APP.cal.store_mj)
                APP.en_charge = min(APP.cal.charge0_mj, APP.en_store)
                print("[ui] 能量标定重载：" + APP.cal.summary())
            elif a == "set":
                # `on` 也接受（页面一次提交里同时“启用 + 改参数”）：
                # 明确写进来的参数优先级更高 → 先处理“首次开启充满”，再套参数。
                if "on" in req:
                    if req["on"]:
                        if not APP.en_on:
                            APP.en_on = True
                            APP.en_charge = APP.en_store      # 首次开启：充满
                    else:
                        APP.en_on = False
                        APP.en_state = {}
                        APP.id_level_energy = None
                if "harvest_mw" in req:
                    APP.en_harvest = max(0.0, float(req["harvest_mw"]))
                if "charge_mj" in req:
                    APP.en_charge = max(0.0, min(float(req["charge_mj"]), APP.en_store))
                if "store_mj" in req:
                    APP.en_store = max(1.0, float(req["store_mj"]))
                if "push" in req:
                    APP.en_push = bool(req["push"])
                if "listen_interval_s" in req:
                    # 策略项：0 = 不建模监听（对照实验）；负值当 0（与页面 min=0 一致）
                    APP.en_listen = max(0.0, float(req["listen_interval_s"]))
                if "speedup" in req and float(req["speedup"]) > 0:
                    APP.en_speedup = float(req["speedup"])
            elif a is not None:
                self._send(200, json.dumps(
                    {"ok": False, "err": "bad_action"}).encode())
                return
            # `drain=False`：这一拍只**重算策略**（级别/间隔/状态格立刻反映新参数），
            # 不推进电量 —— 否则页面上改一次参数就白白丢掉一拍储能（且与真实计时不符）。
            APP._energy_step(drain=False)
            self._send(200, json.dumps(APP.energy_view()).encode())
            return
        if self.path.startswith("/api/truth"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "err": str(e)}).encode())
                return
            self._api_truth(body=req)
            return
        if self.path != "/api/ctl":
            self._send(404, b"not found", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "err": str(e)}).encode())
            return
        r = APP.cmd(req.get("action"), sn=req.get("sn"), every=req.get("every"),
                    note=req.get("note") or "", kind=req.get("kind"),
                    level=req.get("level"), sec=req.get("sec"),
                    rtc=req.get("rtc", "__missing__"), on=req.get("on"),
                    n=req.get("n"), rotate=req.get("rotate"),
                    url=req.get("url"), resolve=req.get("resolve"))
        self._send(200, json.dumps(r).encode())


class Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def lan_urls(port, host=""):
    """列出本机可用的**局域网**访问地址（供手机/平板输入）。

    只返回 IPv4（与 Phase 1 的地址族约定一致）且**排除回环**（手机上打 127.0.0.1
    是打它自己）。取法两道，任一失败不影响启动：
      · 默认路由法（UDP connect 不发包）→ 最可能是局域网那个地址；
      · 主机名解析 → 兼顶多网卡/多地址的情况。
    """
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))          # 不发包，只为让系统选出出口网卡
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       family=socket.AF_INET):
            ips.append(info[4][0])
    except Exception:
        pass
    out = []
    for ip in ips:
        if ip.startswith("127.") or ip == "0.0.0.0" or ip in out:
            continue
        out.append(ip)
    return [f"http://{ip}:{port}/" for ip in out]


def host_error(host):
    """`--host` 合法性（只做 IPv4）。返回错误文本，None = 可以用。

    为什么要在入口拦住 IPv6：本链路的每一处 socket 都显式建 `AF_INET`
    （模拟器三处监听 / Router↔Server UDP / UI HTTP），给个 IPv6 地址的结果是
    “看着绑上了、其实连不上”——不如当场报错退出。
    """
    if host is None or not str(host).strip():
        return "--host 不能为空（默认 127.0.0.1）"
    if ":" in str(host):
        return f"--host 只支持 IPv4（本链路 Phase 1 未做 IPv6）：{host}"
    return None


def build_parser():
    """命令行参数（单抽一个函数：单测要拿它验证默认值与新加的 --host）。"""
    ap = argparse.ArgumentParser(description="ORPAH-over-HaLow L1 demo Web UI")
    ap.add_argument("--port", type=int, default=HTTP_PORT)
    ap.add_argument("--host", default="127.0.0.1",
                    help="HTTP 监听地址（默认 127.0.0.1 只本机；0.0.0.0 = 同网段可用，"
                         "但页面**无认证**，只在可信局域网临时用）")
    ap.add_argument("--every", type=float, default=2.0, help="自动上报间隔秒")
    ap.add_argument("--sn", default="CN-WH01-9AF3C1D2",
                    help="终端序列号（Orpah ID：CC-ORG-UNIQUE[-CHECK]）")
    ap.add_argument("--rssi", type=int, default=-55)
    ap.add_argument("--no-walk", action="store_true",
                    help="关闭「移动的人」演示数据（回到恒定 RSSI + 不写路由器观测）")
    ap.add_argument("--no-browser", action="store_true")
    return ap


def main():
    global APP
    args = build_parser().parse_args()
    err = host_error(args.host)
    if err:
        print(f"[ui] {err}")
        return 2

    APP = OrpahApp(every=args.every, sn=args.sn, rssi=args.rssi,
                   walk=not args.no_walk)
    # 能量标定（2026-09-13）：**大声说出**这份结论用的是实测还是演示参数 ——
    # 能量卡片上的所有数字都受它影响，而“静默用了演示值”正是要防的那种失真。
    print("[ui] 能量标定：" + APP.cal.summary())
    for _e in APP.cal.errors:
        print("[ui]   ⚠ 标定错误：%s %s" % (_e["key"], json.dumps(_e["args"],
                                                          ensure_ascii=False)))
    if not APP.start():
        return 1

    httpd = Srv((args.host, args.port), Handler)
    local = f"http://127.0.0.1:{args.port}/"
    print(f"[ui] ORPAH L1 demo UI: {local}  (Ctrl-C 退出)")
    if args.host not in ("127.0.0.1", "localhost"):
        # 暴露到局域网时必须**主动说出来**（默认只本机；这是用户显式选的，
        # 但“谁都能驱动这个 demo”不是小事，不能只靠文档）。
        print("=" * 66)
        print("[ui] ⚠ HTTP 已监听 %s:%d —— 同网段的设备都能打开这个页面。"
              % (args.host, args.port))
        print("[ui] ⚠ 本页面没有任何认证：能访问端口的人就能标记走失、注入报文、刷量。"
              "\n     只在可信局域网临时用，用完就关掉。")
        print("[ui] 组件端口（模拟器/ UDP server / Router）仍只在本机，不随它暴露。")
        urls = lan_urls(args.port, args.host)
        if urls:
            print("[ui] 手机/平板（同网段）可试：" + "  ".join(urls))
        else:
            print("[ui] 没识别出局域网 IPv4 地址 —— 用 `ipconfig` 自己看一下再手输。")
        print("[ui] Windows 首次监听可能弹防火墙提示：要选「允许」（专用网络）。")
        print("=" * 66)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(local)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        APP.stop_all()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
