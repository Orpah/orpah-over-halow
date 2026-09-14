#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_client_sim.py — 客户端**设备仿真器**端到端验收（纯 PC，无硬件）
====================================================================
把 `client_sim.DeviceSim`（步骤 1a 的产物）接到**真实**的仿真链路里跑一遍：

    DeviceSim --(host 数据口)--> STA 模拟器 ==空口==> AP 模拟器
              --host口--> Router 桥 --UDP--> Server（权威走失库 + 验签）

与 `demo_l2.py` 的区别：那边的"客户端"是手写的 `ClientHost` 调用序列（工装），
这边跑的是**设备仿真器本身**（周期、能力声明、无 RTC、电量、自限频都由设备决定）——
验的是"这台设备能不能被整条链正常接待"，也是步骤 b/c/d/e 的**参照判据**。

验收（全部要过）：
  ① 设备侧：L2 完成（REQ-CONNECT → ACCESS-INFO → REPORT → TRACKING-STATUS）
  ② 服务端：收到 REPORT，且**已签 ID 上报验签通过**（`accepted`，不是"我们自认为签了"）
  ③ 设备无 RTC（`cap.rtc=false` + `ts=0`）时，服务端**用接收时刻记账**（`ts_src=server`）——
     这是免电池终端的正常形态，不能被当成"时钟异常"
  ④ mark 走失后重跑一拍：设备看到 `tracked=True` + `TRACKED`

运行：python demo_client_sim.py [--sn CN-WH01-9AF3C1D2]

⚠ 本脚本**没有真机参与**（真板由用户接）；它是步骤 b–e 的"软件侧判据"。
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

import client_sim as cs                      # noqa: E402  被测：设备仿真器
import downlink                              # noqa: E402
import keystore as ksdb                      # noqa: E402
import orpah_id as oid                       # noqa: E402
from router import RouterBridge              # noqa: E402
from server import OrpahServer               # noqa: E402
from waiting import wait_until               # noqa: E402  按截止时间等待（只这一份实现）

# 端口避开 demo_l1..l4 / demo_ratelimit / ui_server（那些是 94xx/95xx 段）
CONSOLE_A, LINK_A, HOST_A = 9801, 9811, 9821   # AP（Router）
CONSOLE_B, LINK_B, HOST_B = 9802, 9812, 9822   # STA（Client）
UDP_SRV = 19847


def _loop(core, stop):
    while not stop.is_set():
        core.wifi.poll()
        core.link.poll()
        time.sleep(0.005)


class Rec:
    """记录上行（服务端侧）与验签结果，供断言。"""

    def __init__(self):
        self.report = 0
        self.id = []          # 验签结果记录（server._on_id_report 给的那种）

    def on_report(self, msg, addr=None):
        self.report += 1

    def on_id_report(self, rec):
        self.id.append(rec)


def main():
    ap = argparse.ArgumentParser(description="客户端设备仿真器端到端验收（纯 PC）")
    ap.add_argument("--sn", default="CN-WH01-9AF3C1D2")
    args = ap.parse_args()
    sn = args.sn

    # 1) 两台 PC 模拟器（AP + STA）
    coreA = sim.Core("Router-AP", "AP", CONSOLE_A, LINK_A, None, host_port=HOST_A)
    coreB = sim.Core("Client-STA", "STA", CONSOLE_B, LINK_B,
                     ("127.0.0.1", LINK_A), host_port=HOST_B)
    stop = threading.Event()
    threading.Thread(target=_loop, args=(coreA, stop), daemon=True).start()
    threading.Thread(target=_loop, args=(coreB, stop), daemon=True).start()

    # 2) Server：**服务端得先认识这台设备**（密钥库里要有它的公钥），否则只会 `unknown_device`。
    #    设备与服务器两边都用 (sn, gen) 派生同一把演示密钥（真机是产线烧录，见协议 §6.2）。
    rec = Rec()
    ks = ksdb.KeyStoreDB(":memory:")
    dev = oid.Device(sn=sn, se_sn="ATECC608B-DEMO", gen=1)
    ks.register(dev, model="CH32V203+TX-AH+ATECC608B", firmware="sim-1.0")
    priv, pub = downlink.demo_pair()
    srv = OrpahServer(port=UDP_SRV, keystore=ks,
                      on_report=rec.on_report, on_id_report=rec.on_id_report,
                      down_key=priv)
    srv.start()

    # 3) Router（双向桥；下行签名校验用公钥）
    router = RouterBridge(ap_port=HOST_A, server_port=UDP_SRV, down_pub=pub)
    assert router.start(), "Router 连不上 AP host 口"

    # 4) ★被测：客户端设备仿真器 —— 按**免电池终端的真实形态**配：
    #    无 RTC（cap.rtc=false + ts=0）、带电量、周期 2 s（演示加速；真机常态 60 s）。
    #    自限频**开着**（默认）——正是要验证"守规矩的设备不会被上游丢"。
    devsim = cs.DeviceSim(sta_host="127.0.0.1", sta_port=HOST_B, sn=sn,
                          rssi=-55, every=2.0, cap_rtc=False, ts_zero=True,
                          battery_mv=3700, log=None)
    if not cs.connect(devsim):
        print(f"[!!] 设备仿真器连不上 STA host 口 :{HOST_B}")
        return 1
    print(f"设备仿真器已连接：sn={sn} 周期 {devsim.every:g}s cap.rtc=False ts=0 "
          f"自限频开（突发 {devsim.client.limiter.snapshot()['params']['burst']} 条）")

    # 5) 等 STA 关联 AP
    print("\n等待 STA 关联 AP…")
    if not wait_until(lambda: coreB.wifi.conn == sim.CONN_CONNECTED,
                      timeout=10, interval=0.1):
        print(f"  [!!] 10s 内 STA 未关联上 AP（conn={coreB.wifi.conn_str()}）")
    print(f"STA conn = {coreB.wifi.conn_str()}")

    # 6) 跑两拍（设备自己决定发什么）
    print("\n--- 第一轮：设备跑两拍（L2 会话 + 已签 ID 上报）---")
    snap1 = devsim.run(cycles=2)
    ok_tracked0 = wait_until(lambda: devsim.tracked is not None, timeout=5, interval=0.1)
    ok_status0 = wait_until(lambda: devsim.last_status is not None, timeout=5, interval=0.1)
    ok_srv = wait_until(lambda: rec.report >= 1, timeout=5, interval=0.1)
    ok_id = wait_until(lambda: any(r["accepted"] for r in rec.id), timeout=5, interval=0.1)
    first = rec.id[0] if rec.id else {}
    print(f"设备看到：tracked={devsim.tracked} / 回执={devsim.last_status}")
    print(f"服务端收到 REPORT={rec.report} 条；ID 上报 {len(rec.id)} 条，"
          f"首条 accepted={first.get('accepted')} alg={first.get('alg')} "
          f"level={first.get('level')} trust={first.get('trust')}")
    # 7) mark 走失 → 再跑一拍：设备应当看到 tracked=True + TRACKED
    print(f"\n--- 第二轮：mark 走失 sn={sn} → 设备再跑一拍 ---")
    srv.mark_tracked(sn, note="demo-client-sim")
    time.sleep(0.5)
    devsim.run(cycles=1)
    ok_tracked1 = wait_until(lambda: devsim.tracked is True, timeout=5, interval=0.1)
    ok_status1 = wait_until(lambda: devsim.last_status == "TRACKED", timeout=5, interval=0.1)
    print(f"设备看到：tracked={devsim.tracked} / 回执={devsim.last_status}")

    # 8) 断言
    print("\n=== 客户端设备仿真器验收 ===")
    chk = [
        ("① L2 完成（ACCESS-INFO 到达设备）", ok_tracked0),
        ("① L2 完成（TRACKING-STATUS 到达设备）", ok_status0),
        ("② 服务端收到 REPORT", ok_srv),
        # 先给一条自己的判据：`rec.id` 为空时后面那些 `first.get(…)` 会全变 None，
        # 失败信息会指向“ts_src 不对”这种错地方（实际是根本没收到 ID 上报）。
        ("② 收到 ID 上报（有验签记录；否则后面几条都无从谈起）", bool(rec.id)),
        ("② 已签 ID 上报**验签通过**", ok_id),
        ("③ 无 RTC 设备：ts_src=server（服务端用接收时刻记账）",
         first.get("ts_src") == "server"),
        ("③ 无 RTC 设备：cap_rtc 三态如实为 False", first.get("cap_rtc") is False),
        ("③ 电量随已签报文到达服务端（3700 mV）", first.get("battery_mv") == 3700),
        ("④ mark 后设备看到 tracked=True", ok_tracked1),
        ("④ mark 后设备看到 TRACKED", ok_status1),
        ("守规矩的设备：上游两侧零丢弃（限频不该误伤）",
         router.rl_dropped == 0 and srv.rl_dropped == 0),
    ]
    for name, ok in chk:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = [n for n, ok in chk if not ok]
    print(f"\n设备快照：{snap1}")
    print("结果:", "[PASS]" if not bad else f"[FAIL]（{len(bad)} 项）")

    stop.set()
    devsim.client.close()
    router.stop()
    srv.stop()
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
