#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
client_sim.py — ORPAH **客户端设备仿真器**（固件的参照实现）
============================================================
把"一台真实的 ORPAH 客户端设备"在 PC 上按规范跑起来，供三个阶段共用：

    ① 纯软件阶段：连 `sim.py` / `ui_server` 的 **STA 模块 host 数据口**（TCP）
    ② 真板阶段（步骤 b）：PC ↔ TX-AH 开发板 —— **底层传输待定**，见 `docs/client_sim.md`
    ③ 固件阶段（步骤 c/d/e）：真机 MCU 上的固件**照这个状态机写**（本文件是它的参照）

角色：Client = 被追踪终端（真实中 = 免电池可穿戴：TX-AH 电台 + CH32V203 + ATECC608B）。

**它不重新实现协议**（这是本仓最容易犯的错）：报文用 `orpah_proto` 构、签名用
`orpah_id.Device`、选级用 `orpah_id.pick_level`、自限频用 `ratelimit`（经 `ClientHost`）、
间隔与降级用 `energy.plan`。本文件只做三件协议层之上的事：
    1. **会话节奏**（REQ-CONNECT → REPORT → ID-REPORT，与 `ui_server._report_loop` 同序）；
    2. **设备侧的旋钮**（周期/能力声明/无时钟/降级故障/电量/能量）—— 每个旋钮都走规范里那条路径；
    3. **可观测**（每拍一条结构化结果 + 随时可取快照），便于脚本化验收与真机比对。

运行（连本机模拟器的 STA host 口）：
    python client_sim.py --sta-port 9422                       # 60 s 常态，一直跑
    python client_sim.py --sta-port 9422 --cycles 3 --json     # 跑 3 拍，输出 NDJSON
    python client_sim.py --sta-port 9422 --no-clock --cap-rtc no   # 无 RTC 终端（ts=0）
    python client_sim.py --sta-port 9422 --level-mode se_fail  # 演示 §8.2：SE 不可用 → L2
    python client_sim.py --sta-port 9422 --energy --harvest 0.5    # 由能量决定间隔与级别

真板阶段（步骤 b）走 **UART**（不是 SPI）：PC 经 Type-C 接 TX-AH 开发板的 AT 串口，
数据面 = `AT+TXDATA=<len>` + 裸以太网帧（上行）、`FRAME:RX <hex>` 行（下行）——
实现见 `host_serial.SerialAtBus`，用法：

    python client_sim.py --transport serial --serial-port COM13 --baud 115200 --cycles 2

未做（如实，见 `docs/client_sim.md`）：
    · **真机实测**：`host_serial.py` 按 AT 手册 + 模拟器固件实现，**未在真机验证**
      （首测要确认三件事，见 `host_serial.py` 头注释的 ⚠）。
    · SE 真实驱动（ATECC608B）—— 现在用 P-256 的演示派生密钥（`demo_key=True`）。
    · 真实取能/储能标定（现在是 `energy.py` 的演示标定值）。
"""
import argparse
import json
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import energy as en
import keystore as ksdb
import orpah_id as oid
import orpah_proto as op
from client import ClientHost


class DeviceSim:
    """一台客户端设备的仿真状态机（纯逻辑 + 一个可替换的传输）。

    `client` 缺省自建 `ClientHost`（= `host_bus.HostBus` 的 TCP 传输）；
    **换传输就换这一个参数**（测试里换成假 STA，真板阶段换成 SPI/串口实现）——
    设备逻辑一行都不用改。
    """

    def __init__(self, sta_host="127.0.0.1", sta_port=9422,
                 sn="CN-WH01-9AF3C1D2", rssi=-55, every=None, cycles=None,
                 cap_rtc=None, ts_zero=False, level_mode="auto", battery_mv=None,
                 id_report=True, self_limit=True, energy=False,
                 harvest_mw=None, charge_mj=None, store_mj=None, push=False,
                 client=None, sleeper=time.sleep, now_fn=time.time, log=print):
        # ★周期默认 = **设计常态**（60 s，"正常每 60 s 连一次 HaLow Router"）——单一源，
        #   不在本文件再写一个数（页面/告警阈值/CLI 都指向 `energy.NORMAL_INTERVAL_S`）。
        self.every = float(en.NORMAL_INTERVAL_S if every is None else every)
        self.cycles = cycles
        self.sn = sn
        self.rssi = rssi
        self.id_report = bool(id_report)
        self.level_mode = level_mode
        self.ts_zero = bool(ts_zero)
        self.cap_rtc = cap_rtc                       # None = 不声明 / True / False
        self.battery_mv = battery_mv
        self.en_on = bool(energy)
        self.push = bool(push)                       # 采不敷出时“硬撑”（默认如实沉默）
        self.harvest_mw = float(harvest_mw if harvest_mw is not None else 0.5)
        self.charge_mj = float(en.CHARGE0_MJ if charge_mj is None else charge_mj)
        self.store_mj = float(en.STORE_MJ if store_mj is None else store_mj)
        self.speedup = 1.0                           # 演示加速（真机常态是 1）
        self._sleep, self._now = sleeper, now_fn
        self.log = log

        # 传输 + 注入（含自愿自限频）；`sn`/`rssi` 由 ClientHost 持有（它才是发送方）
        self.client = client if client is not None else ClientHost(
            sta_host=sta_host, sta_port=sta_port, sn=sn, rssi=rssi,
            self_limit=self_limit)
        self.client.sn = sn
        self.client.rssi = rssi
        # 设备侧旋钮：能力声明 / 时钟坏 / 时钟偏移（走 `ClientHost` 已有字段，不另存一份）
        self.client.cap_rtc = cap_rtc
        self.client.ts_broken = bool(ts_zero)

        self.dev = None                              # 懒建（SN 变更时重建，见 _ensure_device）
        self.tick = 0                                # 第几拍
        self.req_sent = 0                            # 已发 REQ-CONNECT 条数（L2 第一步）
        self.report_sent = 0                         # 已发 REPORT 条数（L2 第二步）
        self.level = None                            # 本拍选定的级别（None=按 §8.2 自动）
        self.interval_s = self.every                 # 本拍实际用的间隔（能量模式会改）
        self.silent = False                          # 本拍如实沉默（采不敷出）
        self.held = 0                                # 被自限频**延后**的条数（不是丢弃）
        self.id_sent = 0                             # 已驱动的 ID-REPORT 条数（含被延后）
        self.last_level0 = None                      # 最近一次 ID 报文里的 hdr（供验收）
        self.down = {}                               # 最近一条下行（按 type 记）
        self.down_count = 0
        self.tracked = None                          # ACCESS-INFO 里的 tracked（走失表命中？）
        self.last_status = None                      # TRACKING-STATUS 的 status
        self.last_error = None                       # ERROR 的 code

    # ---------------- 设备（签名密钥） ----------------
    def _ensure_device(self):
        """按当前 SN 建/复用 Orpah ID 设备（**演示密钥由 (sn, gen) 派生**，非真机做法）。"""
        if self.dev is not None and self.dev.sn == self.sn:
            return self.dev
        self.dev = oid.Device(sn=self.sn, se_sn="ATECC608B-DEMO", gen=1)
        return self.dev

    def _level_kw(self):
        """本拍的选级参数 → 喂给 `orpah_id.Device.report()`。

        两条路都走**规范里那条路径**（不许直接写死 level）：
          · 能量模式：能量决定的就是级别本身（HS256）→ 传 `level=1`；
            **与"哪个环节坏了"是两回事** —— 混起来会把"省电"错报成"Slot0 签名失败"。
            注意：能量模式下**不降级时走 `auto`**（= `pick_level` 全正常 → L0/ES256），
            只有降级时才强制 L1/HS256；也就是说 `level=None` 并不代表“没选级”。
          · 否则：故障注入模式（`orpah_id.LEVEL_MODES`，§8.2）→ 交给 `pick_level` 选。
        """
        if self.en_on and self.level is not None:
            return {"level": self.level}
        return dict(oid.LEVEL_MODES.get(self.level_mode, oid.LEVEL_MODES["auto"]))

    def _energy_step(self):
        """能量轴推进一拍（可选）：用 `energy.plan` 定间隔与级别，再按 `energy.drain` 推电量。

        模型是**唯一源**（`energy.py`）：本文件不自己算间隔/级别/电量，只把结果用到节奏上。
        """
        if not self.en_on:
            return
        p = en.plan(self.harvest_mw, self.charge_mj, self.store_mj)
        self.interval_s = (None if p["interval_s"] is None
                           else float(p["interval_s"]))
        self.level = 1 if p["degraded"] else None      # 降级 = HS256（下限 L1，永不 L3）
        self.silent = bool(self.interval_s is None and not self.push)
        if self.silent:
            self.every = float(self.interval_s or en.MIN_INTERVAL_S)   # 沉默时按最小拍推进
        else:
            use = self.interval_s if self.interval_s is not None else en.EMERGENCY_INTERVAL_S
            self.every = max(0.5, float(use) / self.speedup)
        self.battery_mv = en.mv_of(self.charge_mj, self.store_mj)

    def _drain(self):
        if not self.en_on or self.silent:
            return
        self.charge_mj = en.drain(self.charge_mj, self.harvest_mw, self.interval_s,
                                  self.level, self.every * self.speedup,
                                  store_mj=self.store_mj)

    # ---------------- 一拍：会话 ----------------
    def cycle(self):
        """跑一个上报周期：REQ-CONNECT → REPORT →（可选）ID-REPORT。返回本拍结果 dict。

        顺序与 `ui_server._report_loop` **一致**（那是演示里已在跑的同一件事）：
        先握手再上报，ID 上报跟在后面（周期上报的 ID 条数因此比会话数多 1 是正常现象）。
        """
        self.tick += 1
        self._energy_step()
        res = {"tick": self.tick, "ts": int(self._now()), "sn": self.sn,
               "every_s": round(self.every, 2), "interval_s": self.interval_s,
               "level": self.level, "silent": self.silent, "with_id": False}
        if self.silent:
            # 采不敷出 → **如实沉默**（不发报）。服务端按沉默处置、能归因（低电 vs 异常）。
            res["why"] = "silent"                        # 没电了就不编一条报出来
            self._drain()
            return res
        st = self.client.send_req_connect()
        res["req_connect"] = bool(st)
        self.req_sent += 1 if st else 0
        self._sleep(0.25)
        n = self.client.report_once()
        res["report"] = int(n)
        self.report_sent += 1 if n > 0 else 0
        if self.id_report:
            res["with_id"] = self._send_id()
        res["sent"] = self.report_sent
        res["held"] = self.client.self_held
        self._drain()
        return res

    def _send_id(self):
        """组一条**已签** orpah-id-report 并注入（设备侧"我还在"的正式报文）。"""
        d = self._ensure_device()
        cap = None if self.cap_rtc is None else {"rtc": bool(self.cap_rtc)}
        rep = d.report(
            ts=0 if self.ts_zero else int(self._now()),
            seen_routers=[{"bssid": "AA:BB:CC:DD:EE:FF",
                           "ssid": "ORPAHID_ZONE_A", "rssi": int(self.rssi)}],
            battery_mv=self.battery_mv, firmware="sim-1.0", cap=cap,
            **self._level_kw())
        self.last_level0 = rep.get("hdr")
        n = self.client.send_id_report(rep)
        self.id_sent += 1                                # 计数是"驱动了几条"（含被延后）
        return bool(n)

    def run(self, cycles=None):
        """跑 `cycles` 拍（None = 由构造参数决定；<=0 = 无限）后返回本机快照。"""
        n = self.cycles if cycles is None else cycles
        i = 0
        while n is None or n <= 0 or i < n:
            res = self.cycle()
            if self.log:
                self.log(self._fmt(res))
            i += 1
            if n is not None and n > 0 and i >= n:
                break
            self._sleep(self.every)
        return self.snapshot()

    # ---------------- 下行（Router/Server → Client） ----------------
    def on_down(self, msg):
        """处理一条下行报文（由 `ClientHost.on_recv` 调）。

        只**记录**，不改设备行为 —— 真机的处置（例如按 TRACKING-STATUS 决定是否重传）
        要在明确需求后再做，别在这里自由发挥（§9 没规定客户端收到回执后的动作）。
        """
        self.down_count += 1
        t = msg.get("type")
        self.down[t] = msg
        if t == op.MSG_ACCESS_INFO:
            self.tracked = msg.get("tracked")
        elif t == op.MSG_TRACKING_STATUS:
            self.last_status = msg.get("status")
        elif t == op.MSG_ERROR:
            self.last_error = msg.get("code")
        return t

    # ---------------- 观测 ----------------
    def snapshot(self):
        """当前设备状态（JSON-ready）—— 验收脚本/上位机读它，不解析日志文本。"""
        return {
            "sn": self.sn, "tick": self.tick, "every_s": round(self.every, 2),
            "interval_s": self.interval_s, "silent": self.silent,
            "level": self.level, "level_mode": self.level_mode,
            "level0": self.last_level0, "cap_rtc": self.cap_rtc,
            "ts_zero": self.ts_zero, "battery_mv": self.battery_mv,
            "id_sent": self.id_sent, "req_sent": self.req_sent,
            "report_sent": self.report_sent,
            "recv": self.client.recv, "held": self.client.self_held,
            "tracked": self.tracked, "last_status": self.last_status,
            "last_error": self.last_error, "down_count": self.down_count,
            "energy": ({"on": True, "harvest_mw": self.harvest_mw,
                        "charge_mj": round(self.charge_mj, 2),
                        "store_mj": self.store_mj} if self.en_on else {"on": False}),
        }

    @staticmethod
    def _fmt(res):
        if res.get("why") == "silent":
            return (f"[{res['tick']}] 如实沉默（采不敷出，不发报）"
                    f"  距上次上报 {res['every_s']}s")
        lvl = "" if res["level"] is None else f" level=L{res['level']}"
        extra = " +ID" if res["with_id"] else ""
        return (f"[{res['tick']}] REQ-CONNECT+REPORT{extra}{lvl}  "
                f"间隔 {res['every_s']}s  已发 {res['sent']}（延后 {res['held']}）")


def connect(sim, retries=100):
    """连上 STA 模块的 host 数据口；连不上就**如实失败**（返回 False，不静默继续）。"""
    if not sim.client.connect(retries=retries):
        return False
    sim.client.on_recv = sim.on_down               # 下行交给设备处理
    return True


def register_keystore(path, sn, model="CH32V203+TX-AH+ATECC608B", firmware="sim-1.0"):
    """把本机演示设备登记进**指定**密钥库（SQLite）—— 真机联调时服务端要先认识这个 SN。

    只在显式传 `--register-keystore` 时执行（写库是有副作用的动作，不默认做）。
    密钥是**演示派生**的（`orpah_id.Device(demo_key=True)`），不是真机密钥。
    """
    ks = ksdb.KeyStoreDB(path)
    dev = oid.Device(sn=sn, se_sn="ATECC608B-DEMO", gen=1)
    ks.register(dev, model=model, firmware=firmware)
    return ks.to_dict(sn)


def main():
    ap = argparse.ArgumentParser(
        description="ORPAH 客户端设备仿真器（连 STA 模块 host 数据口）")
    ap.add_argument("--sta-host", default="127.0.0.1", help="STA 模块 host 口地址（tcp）")
    ap.add_argument("--sta-port", type=int, default=9422,
                    help="STA 模块 host 数据口 TCP 端口（sim.py --host）")
    ap.add_argument("--transport", choices=["tcp", "serial"], default="tcp",
                    help="底层传输：tcp=PC 模拟器的 host 口；serial=真板（UART/AT+TXDATA）")
    ap.add_argument("--serial-port", default="COM3",
                    help="串口设备名（serial 传输；Windows 如 COM13，Linux 如 /dev/ttyUSB0）")
    ap.add_argument("--baud", type=int, default=115200, help="串口波特率（serial 传输）")
    ap.add_argument("--dump-lines", type=int, default=0,
                    help="退出时打印最近 N 行控制台输出（真机排查用；0=不打印）")
    ap.add_argument("--sn", default="CN-WH01-9AF3C1D2", help="终端序列号（Orpah ID）")
    ap.add_argument("--rssi", type=int, default=-55, help="报文里的链路强度（设备自报）")
    ap.add_argument("--every", type=float, default=None,
                    help=f"会话周期秒（默认 = 设计常态 {en.NORMAL_INTERVAL_S:.0f}s）")
    ap.add_argument("--cycles", type=int, default=0, help="跑几拍后退出（0=一直跑）")
    ap.add_argument("--cap-rtc", choices=["yes", "no", "none"], default="none",
                    help="能力声明 cap.rtc（none=不声明）")
    ap.add_argument("--no-clock", action="store_true",
                    help="无可用时钟 → 报文 ts=0（服务端用接收时刻；绝不改报文 ts）")
    ap.add_argument("--level-mode", choices=sorted(oid.LEVEL_MODES), default="auto",
                    help="§8.2 降级演示：哪个环节坏了（选级走 pick_level）")
    ap.add_argument("--battery-mv", type=int, default=None, help="已签电量（mV）")
    ap.add_argument("--no-id-report", action="store_true", help="只跑 L2 会话，不发 ID 上报")
    ap.add_argument("--no-self-limit", action="store_true",
                    help="关掉设备侧**自愿**自限频（延后语义）")
    ap.add_argument("--energy", action="store_true", help="开能量模式（间隔/级别由能量定）")
    ap.add_argument("--harvest", type=float, default=0.5, help="采集功率 mW（能量模式）")
    ap.add_argument("--charge", type=float, default=None, help="当前储能 mJ（能量模式）")
    ap.add_argument("--store", type=float, default=None, help="储能上限 mJ（能量模式）")
    ap.add_argument("--push", action="store_true",
                    help="采不敷出时“硬撑”继续发（默认**如实沉默**）")
    ap.add_argument("--speedup", type=float, default=1.0,
                    help="演示加速倍数（把真实间隔压缩，仅供现场观察；真机常态是 1）")
    ap.add_argument("--json", action="store_true", help="每拍输出一条 NDJSON（便于脚本解析）")
    ap.add_argument("--quiet", action="store_true", help="不打印每拍日志")
    ap.add_argument("--register-keystore", metavar="PATH",
                    help="先把本机演示设备登记进该密钥库（SQLite；真机联调用）")
    args = ap.parse_args()

    sn = args.sn
    if args.register_keystore:
        rec = register_keystore(args.register_keystore, sn)
        print(f"[keystore] 已登记演示设备 {sn}（kid={rec.get('kid')}，"
              f"model={rec.get('model')}）→ {args.register_keystore}")

    # `--json`：每拍**实时**一行 NDJSON（便于边跑边看/管道处理），最后再压一行快照。
    ndjson = (lambda r: print(json.dumps(r, ensure_ascii=False), flush=True))
    bus = None
    if args.transport == "serial":
        # 真板（步骤 b）：UART/AT 控制台上的数据面 —— 与 TCP 是**同一套语义、不同线协议**，
        # 所以只换传输对象，设备逻辑一行不改。
        import host_serial
        bus = host_serial.SerialAtBus(args.serial_port, args.baud, name="client_sim")
    client = ClientHost(sta_port=args.sta_port, sta_host=args.sta_host, sn=sn,
                        rssi=args.rssi, self_limit=not args.no_self_limit, bus=bus)
    sim = DeviceSim(
        sta_host=args.sta_host, sta_port=args.sta_port, sn=sn, rssi=args.rssi,
        every=args.every, cycles=args.cycles,
        cap_rtc={"yes": True, "no": False}.get(args.cap_rtc),
        ts_zero=args.no_clock, level_mode=args.level_mode,
        battery_mv=args.battery_mv, id_report=not args.no_id_report,
        self_limit=not args.no_self_limit, energy=args.energy,
        harvest_mw=args.harvest, charge_mj=args.charge, store_mj=args.store,
        push=args.push,
        log=(ndjson if args.json else (None if args.quiet else print)))
    sim.speedup = max(1.0, float(args.speedup))
    if not connect(sim):
        # 连不上就**如实失败退出**（不静默继续跑出一堆"看着正常"的日志）
        if args.transport == "serial":
            print(f"[client_sim] 打不开串口 {args.serial_port}（{args.baud}）—— 检查设备名；"
                  f"或用 --dump-lines 看控制台原样输出", file=sys.stderr)
        else:
            print(f"[client_sim] 连不上 STA host 口 {args.sta_host}:{args.sta_port}，退出",
                  file=sys.stderr)
        return 1
    lp = sim.client.limiter.snapshot()
    if not args.quiet:
        tgt = (f"{args.serial_port}@{args.baud}" if args.transport == "serial"
               else f"{args.sta_host}:{args.sta_port}")
        print(f"[client_sim] 已连 {tgt}（{args.transport}）  sn={sn}  "
              f"周期 {sim.every:g}s（设计常态 {en.NORMAL_INTERVAL_S:g}s）  "
              f"自限频 {'开' if lp['on'] else '关'}  "
              f"{'能量模式' if args.energy else '固定级别'}")
    try:
        snap = sim.run()
    except KeyboardInterrupt:
        snap = sim.snapshot()
    finally:
        sim.client.close()
    if args.json:
        print(json.dumps({"snapshot": snap}, ensure_ascii=False))
    elif not args.quiet:
        print("[client_sim] " + json.dumps(snap, ensure_ascii=False))
    if args.dump_lines and bus is not None:
        # 真机排查：把模块控制台原样打出来（下行格式/卡数据模式/报错都看得到）
        print(f"[client_sim] 串口统计 {bus.stats()}")
        for ln in bus.recent_lines(args.dump_lines):
            print("  | " + ln)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
