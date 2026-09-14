#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_client_uart.py — **UART/AT 数据面**（步骤 b 的排练）+ 验收
==============================================================
真板阶段（b）PC 与 TX-AH 板之间走的是 **UART 上的 AT 数据面**：`AT+TXDATA=<len>` + 裸以太帧
（上行）、`FRAME:RX <hex>` 行（下行）—— 见 `host_serial.py` 头注释里的依据。

本脚本用 **PC 模拟器的 AT 控制台**（`vendor/halow/sim.py`，其 AT/数据模式行为与真实模块同源）
把这条线路协议**先排练一遍**，从而把两类问题分开：

    ① 传输/解析写错了（本脚本能当场抓到）；
    ② 真机固件与手册不同（**必须上机才知道**，本脚本管不了 —— 见末尾"未验证"）。

做法：两台模拟器（AP + STA）本来靠 TCP 空口相连；这里**不用它们的 host 数据口**，
而是各拿一个 `SerialAtBus` 接在**各自的 AT 控制台**上，像真板那样用 AT 命令收发：

    [SerialAtBus A]--AT+TXDATA/FRAME:RX-->[AP 模拟器]~~空口~~[STA 模拟器]<--[SerialAtBus B]
                                                                                  ↑ 设备仿真器

验收（双向都要过）：
  ① 设备（Client 侧）经 `AT+TXDATA` 发出的报文，**AP 侧真的收到了**（帧内容逐字节对得上）；
  ② 反向：AP 侧发一帧 → 设备侧收到（`FRAME:RX` 被解析成以太帧）。
  ③ 粘性数据模式：连发多帧不串（每帧前都等 OK）。

运行：python demo_client_uart.py [--sn CN-WH01-9AF3C1D2]
"""
import argparse
import os
import sys
import threading
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HOST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "halow")
if HOST_DIR not in sys.path:
    sys.path.insert(0, HOST_DIR)
ORPAH_DIR = os.path.dirname(os.path.abspath(__file__))
if ORPAH_DIR not in sys.path:
    sys.path.insert(0, ORPAH_DIR)

import sim                                   # noqa: E402

import client_sim as cs                      # noqa: E402
import host_serial as hs                     # noqa: E402  被测：UART/AT 数据面
import orpah_proto as op                     # noqa: E402
from waiting import wait_until               # noqa: E402  按截止时间等待（只这一份实现）

# 端口避开其它 demo（94xx/95xx/97xx/98xx 已用）
CONSOLE_A, LINK_A = 9901, 9911   # AP（Router 侧）
CONSOLE_B, LINK_B = 9902, 9912   # STA（Client 侧）


def _loop(core, stop):
    while not stop.is_set():
        core.wifi.poll()
        core.link.poll()
        time.sleep(0.005)


def main():
    ap = argparse.ArgumentParser(description="UART/AT 数据面排练 + 验收（纯 PC）")
    ap.add_argument("--sn", default="CN-WH01-9AF3C1D2")
    args = ap.parse_args()

    coreA = sim.Core("Router-AP", "AP", CONSOLE_A, LINK_A, None)
    coreB = sim.Core("Client-STA", "STA", CONSOLE_B, LINK_B, ("127.0.0.1", LINK_A))
    stop = threading.Event()
    threading.Thread(target=_loop, args=(coreA, stop), daemon=True).start()
    threading.Thread(target=_loop, args=(coreB, stop), daemon=True).start()

    # 两条"串口"= 各接一台模拟器的 AT 控制台（TcpConsoleSerial 把它们伪装成串口）
    busB = hs.SerialAtBus(f"tcp://127.0.0.1:{CONSOLE_B}", name="client",
                          serial_factory=lambda p, b: hs.TcpConsoleSerial(("127.0.0.1", CONSOLE_B)))
    busA = hs.SerialAtBus(f"tcp://127.0.0.1:{CONSOLE_A}", name="router",
                          serial_factory=lambda p, b: hs.TcpConsoleSerial(("127.0.0.1", CONSOLE_A)))
    for name, bus in (("client", busB), ("router", busA)):
        if not bus.connect(retries=3, interval=0.3):
            print(f"[!!] {name} 的 AT 控制台连不上（模拟器没起来？）")
            stop.set()
            return 1
    print("两条 AT 控制台已就绪（client→STA 控制台、router→AP 控制台）")

    print("\n等待 STA 关联 AP…")
    if not wait_until(lambda: coreB.wifi.conn == sim.CONN_CONNECTED, timeout=10, interval=0.1):
        print(f"  [!!] 10s 内 STA 未关联上 AP（conn={coreB.wifi.conn_str()}）")

    # ① 设备经 UART 上行：用设备仿真器真发（它自己组报文、签名、决定节奏）
    devsim = cs.DeviceSim(sn=args.sn, every=2.0, cap_rtc=False, ts_zero=True,
                          battery_mv=3700, log=None)
    devsim.client.sta = busB                       # ★换底层传输：TCP host 口 → UART/AT
    devsim.client.on_recv = devsim.on_down
    print("\n--- ① 设备经 AT+TXDATA 发两拍，看 AP 侧是否真收到 ---")
    devsim.run(cycles=2)
    time.sleep(0.5)

    # AP 侧：把它控制台打印的 FRAME:TX/RX 里的帧收下来（它就是"空口上真的过去了什么"）
    got = []
    for _ in range(30):
        f = busA.recv_frame(timeout=0.2)
        if f is not None:
            got.append(f)
    kinds = []
    for f in got:
        p = op.parse_eth_frame(f)
        m = op.decode_msg(p[1]) if p else None
        kinds.append(m.get("type") if m else "?")
    print(f"AP 侧经 AT 控制台看到 {len(got)} 帧：{kinds}")
    up_ok = (kinds.count(op.MSG_REQ_CONNECT) >= 1 and kinds.count(op.MSG_REPORT) >= 1
             and kinds.count(op.MSG_ID_REPORT) >= 1)

    # ② 反向：从 AP 侧用同样的 AT 数据面发一帧 → 设备侧应收到（FRAME:RX → recv_frame）
    print("\n--- ② 反向：AP 侧发一帧，看设备侧是否收到 ---")
    be = op.build_eth_frame(op.encode_msg(op.build_access_info(args.sn, tracked=True)),
                            src_mac=b"\xAA" * 6, dst_mac=op.MAC_BCAST)
    sent_down = busA.send_frame(be)
    down_ok = False
    if sent_down:
        def _got_down():
            f = busB.recv_frame(timeout=1.0)
            if f is None:
                return False
            p = op.parse_eth_frame(f)
            m = op.decode_msg(p[1]) if p else None
            if m:
                devsim.on_down(m)                  # 交给设备处理（真链路里由读线程做）
            return bool(m and m.get("type") == op.MSG_ACCESS_INFO)
        down_ok = wait_until(_got_down, timeout=5, interval=0.1)
    print(f"AP 侧 send_frame={sent_down}；设备侧收到 ACCESS-INFO={devsim.tracked}")

    # ③ 粘性数据模式：连发多帧不串（每帧前都等 OK）
    print("\n--- ③ 连发 5 帧（粘性数据模式）---")
    n_ok = sum(1 for _ in range(5) if busB.send_frame(be))
    print(f"client 侧连发成功 {n_ok}/5；tx_fail={busB.tx_fail}")

    print("\n=== UART/AT 数据面验收 ===")
    chk = [
        ("① 上行：AP 侧收到 REQ-CONNECT/REPORT/ID-REPORT（经 AT+TXDATA 真的过了空口）", up_ok),
        ("② 下行：设备侧收到 AP 发的 ACCESS-INFO（经 FRAME:RX 解析）",
         down_ok and devsim.tracked is True),
        ("③ 连发 5 帧全部成功（每帧前等 OK，数据模式不串）", n_ok == 5 and busB.tx_fail == 0),
        ("计数可见：client 侧无坏行", busB.bad_lines == 0),
    ]
    for name, ok in chk:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = [n for n, ok in chk if not ok]
    print(f"\nclient 统计 {busB.stats()}")
    print("结果:", "[PASS]" if not bad else f"[FAIL]（{len(bad)} 项）")
    print("\n⚠ 未验证：本脚本用的是**模拟器的 AT 控制台**（与真机同源但不是真机）。\n"
          "   上机时先跑 `--dump-lines 20` 看真板原样输出，确认三件事：\n"
          "   ① `AT+TXDATA=<len>` 写法；② 下行是否也是 `FRAME:RX <hex>`；③ 数据模式粘性与恢复。")

    for bus in (busB, busA):
        bus.close()
    stop.set()
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
