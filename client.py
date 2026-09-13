#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
client.py — ORPAH Client host（L1 + L2）
========================================
Client = 被追踪终端（真实中 = 免电池可穿戴：TX-AH 电台 + CH32V203 主机/SPI 数据面；
纯 PC 阶段 = 连 STA 模块 host 口，注入/接收）。

L1 角色：把 ORPAH-REPORT（JSON）封装成以太网帧注入 STA → 空口发给 AP（Router）。
L2（双向）：周期握手 REQ-CONNECT → 收 ACCESS-INFO → 发 REPORT → 收
TRACKING-STATUS（Router 注入下行，经 STA host 口到达本 Client）。

    client.py --(host 口 双向)--> STA 模块 <--HaLow--> AP 模块(Router)

可独立运行（周期会话），也可被 demo/ui 内嵌（复用 ClientHost 类）：
    python orpah/client.py --sta-port 9102 --sn CN-WH01-9AF3C1D2 --every 5
"""
import argparse
import socket
import sys
import threading
import time

# Windows 控制台默认代码页 GBK/cp936：强制 stdout/stderr 用 UTF-8 编码，
# 避免中文输出乱码、emoji 抛 UnicodeEncodeError（仓库文本统一 UTF-8）。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from host_bus import HostBus
from orpah_proto import (MAC_BCAST, MSG_REQ_CONNECT, MSG_REPORT,
                         MSG_ACCESS_INFO, MSG_TRACKING_STATUS, MSG_ERROR,
                         build_req_connect, build_report, build_id_report,
                         encode_msg, parse_eth_frame, decode_msg,
                         build_eth_frame)
from ratelimit import DeviceLimiter

LOG = True


def log(*a):
    if LOG:
        print("[client]", *a, flush=True)


class ClientHost:
    """ORPAH Client host（双向）：向 STA 注入 / 从 STA 收下行。"""

    def __init__(self, sta_port, sn="CN-WH01-9AF3C1D2", rssi=-55, mac=None,
                 sta_host="127.0.0.1", on_sent=None, on_recv=None,
                 self_limit=False, limiter=None):
        self.sta = HostBus(host=sta_host, port=sta_port, name="client")
        self.sn = sn
        self.rssi = rssi
        self.seq = 0
        # 设备侧**自愿**自限频（§5.8 里"设备自己那一环"，`ratelimit.DeviceLimiter`）：
        # 只限**本机自己的业务上报**（周期会话 + 周期 ID 上报）。它是**自愿**的，
        # **不是防线**（被改的设备不会做）—— 详见 `DeviceLimiter` 的类注释。
        # 默认关：`ClientHost` 也被验收脚本当"精确控制节奏的工装"用；真正的设备应用
        # （`client.py` 的 main、`ui_server`）才开。演示里注入的**别人**的报文
        # （攻击/刷量/重放）由调用方 `force=True` 明确绕过。
        self.limiter = limiter if limiter is not None else DeviceLimiter(
            enabled=bool(self_limit))
        self.self_held = 0                  # 本机被自己延后的条数（= limiter.held 的镜像）
        # 演示：设备时钟偏移（秒）。0 = 设备时钟正常；非 0 = 模拟 RTC 偏/漂。
        # 服务器侧只**估计**这个偏移（clock.ClockTracker），不改报文与记录（见 §5.5）。
        self.ts_off = 0
        # 演示：设备**能力声明**（`cap`，2026-09-13）。None = 不声明（老行为）；
        # False = 声明「无实时时钟」→ 服务器一律用接收时刻记账、也不估它的时钟偏移。
        self.cap_rtc = None
        # 演示：把自报 ts 置 0（模拟“无晶振/时钟不可用”），用来演示能力声明与不一致判定。
        self.ts_broken = False
        self.on_sent = on_sent              # callable(msg_dict, eth_len) 注入成功
        self.on_recv = on_recv              # callable(msg_dict) 收到下行
        self.sent = 0
        self.recv = 0                       # 收到下行报文数
        # 下行读线程
        self._rx_thread = None
        self._stop = threading.Event()
        # 缺省本机 MAC（4A:06:59:00:00:01），6 字节
        self.mac = mac if mac is not None else bytes([0x4A, 0x06, 0x59, 0, 0, 1])

    def connect(self, retries=100):
        if not self.sta.connect(retries=retries):
            return False
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx_thread.start()
        return True

    def close(self):
        self._stop.set()
        self.sta.close()

    # ---------------- 注入（DATA_TX，host → STA） ----------------
    def _gate(self, kind, force=False):
        """自限频闸门：可以发 → True（扣一格）；该延后 → False（计数 + 留痕，**不抛异常**）。

        `force=True` 的语义**很具体**：这一条**不是本机自己的业务上报**
        （演示里注入的"另一台设备/一台失控设备"的攻击、刷量、重放）→ 绕开自限频。
        这不是后门：自限频本来就是**自愿**的，真被改的设备不绕也拦不住它。
        """
        if force or self.limiter is None or not self.limiter.enabled:
            return True
        dec = self.limiter.check(sn=self.sn, kind=kind)
        if dec.ok:
            return True
        self.self_held = self.limiter.held
        # 日志稀疏化：第 1 条 + 每 20 条说一句（刷量时不要刷屏），细节在 limiter.recent()
        if self.limiter.held == 1 or self.limiter.held % 20 == 0:
            log(f"自限频：{kind} 延后（还差 {dec.retry_after:.2f}s；"
                f"累计延后 {self.limiter.held} 条 —— 延后不是丢弃，下一拍还会发）")
        return False

    def _inject(self, msg, notify=True):
        payload = encode_msg(msg)
        eth = build_eth_frame(payload, src_mac=self.mac, dst_mac=MAC_BCAST)
        if not self.sta.send_frame(eth):
            log("注入失败（STA 未连接？）")
            return -1
        if notify and self.on_sent:
            self.on_sent(msg, len(eth))
        return len(eth)

    def send_req_connect(self, force=False):
        """发 REQ-CONNECT（握手第一步）。"""
        if not self._gate("REQ-CONNECT", force=force):
            return 0
        msg = build_req_connect(self.sn, mac="4a:06:59:00:00:01")
        n = self._inject(msg)
        if n > 0:
            log(f"注入 ORPAH-REQ-CONNECT sn={self.sn}")
        return n

    def report_once(self, extra=None, force=False):
        """发一条 ORPAH-REPORT（握手第二步后）。

        `ts_off`：演示用——把设备自报的 `ts` 拨快/拨慢 N 秒（模拟 RTC 偏移/漂移）。
        服务器侧由 `clock.ClockTracker` 从「设备 ts vs 接收时刻」反推 offset/drift，
        **只估计不改数据**（§5.5 的时间语义仍由 `effective_ts` 负责）。
        `ts_broken`：ts 置 0（无时钟）；`cap_rtc`：随报文声明「有无 RTC」（None=不声明）。
        """
        if not self._gate("REPORT", force=force):
            return 0
        self.seq += 1
        ts = 0 if self.ts_broken else int(time.time()) + int(getattr(self, "ts_off", 0) or 0)
        cap = None if getattr(self, "cap_rtc", None) is None else {"rtc": bool(self.cap_rtc)}
        msg = build_report(sn=self.sn, ts=ts, rssi=self.rssi, seq=self.seq,
                           extra=extra, cap=cap)
        n = self._inject(msg)
        if n > 0:
            self.sent += 1
            log(f"[{self.seq}] 注入 ORPAH-REPORT sn={self.sn} "
                f"rssi={self.rssi} ({n}B)")
        return n

    def send_id_report(self, signed_report, force=False):
        """注入一条 ORPAH-ID-REPORT（已签 orpah-id-report 包一层走既有链路）。

        与 report_once 不同：不动 seq/sent 计数、不触发 on_sent 三阶段点亮；
        Server 验签结果由 OrpahApp._on_id_report 单独展示（见 ui_server.py）。
        """
        if not self._gate("ID-REPORT", force=force):
            return 0
        msg = build_id_report(signed_report)
        n = self._inject(msg, notify=False)
        if n > 0:
            log(f"注入 ORPAH-ID-REPORT sn={msg.get('sn')} ({n}B)")
        return n

    # ---------------- 下行读（DATA_RX，STA → host） ----------------
    def _rx_loop(self):
        while not self._stop.is_set():
            eth = self.sta.recv_frame(timeout=1.0)
            if eth is None:
                continue
            parsed = parse_eth_frame(eth)
            if parsed is None:
                continue
            _et, payload = parsed
            msg = decode_msg(payload)
            if msg is None:
                continue
            self.recv += 1
            mtype = msg.get("type")
            log(f"[rx {self.recv}] {mtype} sn={msg.get('sn', '-')} "
                f"status={msg.get('status', msg.get('code', '-'))}")
            if self.on_recv:
                self.on_recv(msg)

    def last_tracking(self):
        """最近一条 TRACKING-STATUS 的状态码（无则 None）——由 UI 层记录亦可。"""
        return getattr(self, "_last_status", None)


def main():
    ap = argparse.ArgumentParser(description="ORPAH Client host（双向会话）")
    ap.add_argument("--sta-port", type=int, required=True,
                    help="STA 模块 host 数据口 TCP 端口（sim.py --host）")
    ap.add_argument("--sn", default="CN-WH01-9AF3C1D2",
                    help="终端序列号（Orpah ID：CC-ORG-UNIQUE[-CHECK]）")
    ap.add_argument("--rssi", type=int, default=-55, help="注入报文的 rssi")
    ap.add_argument("--every", type=float, default=5.0,
                    help="周期会话秒数（默认 5；<=0 只做一次后退出）")
    ap.add_argument("--no-self-limit", action="store_true",
                    help="关掉设备侧**自愿**自限频（§5.8 设备那一环；默认开）")
    args = ap.parse_args()

    # 设备侧自限频：**自愿**的（不是防线），语义是**延后**不是丢弃（丢自己的报 = 漏报）。
    # 参数是演示配置（`ORPAH_SELF_*` 可覆盖）——真机要按空口与电池实测定。
    c = ClientHost(sta_port=args.sta_port, sn=args.sn, rssi=args.rssi,
                   self_limit=not args.no_self_limit)
    if not c.connect():
        log(f"连不上 STA 模块 host 口 :{args.sta_port}，退出")
        return 1
    lp = c.limiter.snapshot()
    log(f"已连 STA 模块 host 口 :{args.sta_port}，开始会话 sn={args.sn}"
        f"（自限频 {'开' if lp['on'] else '关'}：最小间隔 "
        f"{lp['params']['min_interval']}s、突发 {lp['params']['burst']} 条）")

    def session():
        while True:
            c.send_req_connect()
            time.sleep(0.3)
            c.report_once()
            if args.every <= 0:
                return
            time.sleep(args.every)

    threading.Thread(target=session, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    # 让独立运行也能 Ctrl-C 干净退出
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
