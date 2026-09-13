#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""demo_ratelimit.py — 限频（§5.8）端到端演示 + 验收（纯 PC，无硬件）

**演示什么**：签名只能滤掉「伪造」，滤不掉「**洪水**」——
一台设备（或被改过的固件）以极高频率上报时，服务器每条都要走一次 ECDSA 验签，
不设限就是自伤。本脚本搭起真链路（Client host → STA → 空口 → AP → Router 桥 → UDP → Server），
**真的**连发报文，在 Server 侧断言：

  ① 正常速率（1 条/秒）**一条都不丢** —— 限频不能把好设备挡在门外；
  ② 同一 SN 连发 200 条 → 只有桶容量那点被接受，其余在**验签之前**被丢（省 CPU），
     且拒绝原因明说是 **per-SN 防线**；
  ③ **限频不是封禁**：等桶回补后，正常上报立刻恢复（不是"拉黑"）；
  ④ 轮换 SN 连发（源地址不变）→ per-SN 桶**形同虚设**（每个新 SN 都是满桶），
     由 **per-Router 桶**兜住 —— 这就是"为什么必须有两条防线"的现场证据；
  ⑤ 发现走失（ORPAH-FOUND）**不受限**（漏一条 = 一个人没被找到）。

运行：C:\\Python313\\python.exe demo_ratelimit.py
端口：99xx / 19947（与 demo_l1..l4、demo_spoof、ui_server 错开）
依赖的 `OrpahServer` 接口（本脚本是“外部调用方”，改了这些名字记得同步这里）：
  · `rl`（注入的 `RateLimiter`，共用同一实例）、`rl_dropped`（累计丢弃数）、
    `rl_drops`（最近丢弃的 deque，看 `which` 用）
  · `mark_tracked(sn)` / `found_count`（第 5 阶段要制造 FOUND）
  · `id_report_total`（被接受的 ID 上报数）、`start()` / `stop()`
  不直接用 `_handle`（那是 `test_ratelimit.py` 的离线用法）；本脚本一律**走真链路**。"""
import argparse
import os
import sys
import threading
import time

# Windows 控制台默认代码页 GBK/cp936：强制 stdout/stderr 用 UTF-8 编码
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
import orpah_id as oid                        # noqa: E402
import orpah_proto as op                      # noqa: E402
import ratelimit as RL                        # noqa: E402
from server import OrpahServer                 # noqa: E402
from router import RouterBridge                # noqa: E402
from client import ClientHost                  # noqa: E402
from waiting import wait_until                 # noqa: E402  按截止时间等待（只这一份实现）

CONSOLE_A, LINK_A, HOST_A = 9901, 9911, 9921   # AP（Router 侧）
CONSOLE_B, LINK_B, HOST_B = 9902, 9912, 9922   # STA（Client 侧）
UDP_SRV = 19947

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK ] {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  {extra}" if extra else ""))


def loop_cores(cores, stop):
    while not stop.is_set():
        for c in cores:
            c.wifi.poll()
            c.link.poll()
        time.sleep(0.005)


class Sink:
    """Server 侧收到的验签结果（线程安全）。"""

    def __init__(self):
        self.all = []
        self.lock = threading.Lock()

    def on_id_report(self, rec):
        with self.lock:
            self.all.append(rec)

    def count(self):
        with self.lock:
            return len(self.all)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sn", default="CN-WH01-9AF3C1D2", help="合法设备 SN")
    ap.add_argument("--flood", type=int, default=200, help="同一 SN 连发条数")
    ap.add_argument("--rotate", type=int, default=60, help="轮换 SN 连发条数")
    args = ap.parse_args()

    stop = threading.Event()
    sink = Sink()

    # 演示用小桶：per-SN 5 个（1 条/秒正常流量远低于补充速率 5/s）、per-Router 10 个
    rl = RL.RateLimiter(enabled=True, sn_rate=5.0, sn_burst=5,
                        router_rate=5.0, router_burst=10)
    dev = oid.Device(sn=args.sn, se_sn="ATECC608B-DEMO")
    ks = oid.KeyStore()
    ks.register(dev, model="CH32V203+TX-AH+ATECC608B", firmware="1.0.3")

    print("=" * 74)
    print("  限频（§5.8）端到端演示")
    print("=" * 74)
    print(f"  链路        : Client→STA→空口→AP→Router→UDP:{UDP_SRV}→Server")
    print(f"  per-SN 桶   : burst={rl.sn.burst:.0f} rate={rl.sn.rate:.1f}/s")
    print(f"  per-Router 桶: burst={rl.router.burst:.0f} rate={rl.router.rate:.1f}/s")
    print()

    coreA = sim.Core("Router-AP", "AP", CONSOLE_A, LINK_A, None, host_port=HOST_A)
    coreB = sim.Core("Client-STA", "STA", CONSOLE_B, LINK_B,
                     ("127.0.0.1", LINK_A), host_port=HOST_B)
    threading.Thread(target=loop_cores, args=([coreA, coreB], stop), daemon=True).start()

    srv = OrpahServer(port=UDP_SRV, keystore=ks, id_nonces=oid.NonceCache(),
                      on_id_report=sink.on_id_report, rl=rl)
    srv.start()
    router = RouterBridge(ap_port=HOST_A, server_port=UDP_SRV)
    if not router.start():
        print("  Router 连不上 AP host 口，退出")
        return 2
    client = ClientHost(sta_port=HOST_B, sn=dev.sn)
    if not client.connect():
        print("  Client 连不上 STA host 口，退出")
        return 2
    if not wait_until(lambda: coreB.wifi.conn == sim.CONN_CONNECTED,
                      timeout=15, interval=0.1):
        print(f"  [!!] 15s 内 STA 未关联上 AP（conn={coreB.wifi.conn_str()}）—— 退出")
        client.close()
        router.stop()
        srv.stop()
        return 2
    print(f"  链路就绪：STA conn = {coreB.wifi.conn_str()}\n")

    try:
        # ---- ① 正常速率：1 条/秒，一条都不该丢 ----------------------------
        print("[1] 正常速率（1 条/秒，桶补 5/s）→ 一条都不丢")
        n0, d0 = sink.count(), srv.rl_dropped
        for _ in range(5):
            client.send_id_report(dev.report())
            time.sleep(1.0)          # 这是**发送节奏**（模拟 1 条/秒的设备），不是"等条件"
        got = wait_until(lambda: sink.count() >= n0 + 5, timeout=5.0, interval=0.02)
        check("5 条全部被接受（限频没把好设备挡在门外）",
              got and sink.count() - n0 == 5 and srv.rl_dropped == d0,
              f"接受 {sink.count() - n0} / 丢 {srv.rl_dropped - d0}")

        # ---- ② 同一 SN 连发 ------------------------------------------------
        print(f"\n[2] 同一 SN 连发 {args.flood} 条 → 只放行桶容量那点，其余在验签前被丢")
        srv.rl.reset_counters()
        n1 = sink.count()
        for _ in range(args.flood):
            client.send_id_report(dev.report())
        # 判据：服务端**处理完**这批（接受的 + 丢弃的 >= 本次条数），不是猜次数
        done = wait_until(lambda: (sink.count() - n1 + srv.rl_dropped) >= args.flood,
                          timeout=20.0, interval=0.02)
        acc = sink.count() - n1
        dropped = srv.rl_dropped
        check("服务端确实处理完了这批（超时可见地失败，不静默）", done,
              f"处理 {acc + dropped}/{args.flood}")
        check("被丢 > 0（洪水被限住）", dropped > 0, f"丢 {dropped} 条")
        check("放行条数 ≈ per-SN 桶容量（不是'全通'也不是'全封'）",
              rl.sn.burst <= acc <= rl.sn.burst + 5,
              f"接受 {acc}（桶容量 {rl.sn.burst:.0f}）")
        rec_which = {r["which"] for r in srv.rl_drops}
        check("拒绝原因明说是 per-SN 防线（不只是'被拒了'）",
              rec_which == {"sn"}, f"which={sorted(rec_which)}")

        # ---- ③ 限频不是封禁：回补后立刻恢复 -------------------------------
        print("\n[3] 限频不是封禁：等桶回补后正常上报立刻恢复")
        d_before = srv.rl_dropped
        refilled = wait_until(lambda: rl.sn.peek(dev.sn)[0], timeout=10.0, interval=0.05)
        check("桶会回补（不是把设备拉黑）", refilled)
        n2 = sink.count()
        client.send_id_report(dev.report())
        ok2 = wait_until(lambda: sink.count() > n2, timeout=5.0, interval=0.02)
        check("回补后这一条被接受", ok2 and srv.rl_dropped == d_before,
              f"接受增量 {sink.count() - n2} / 新丢弃 {srv.rl_dropped - d_before}")

        # ---- ④ 轮换 SN：per-SN 拦不住，per-Router 兜住 --------------------
        print(f"\n[4] 轮换 SN（{args.rotate} 个不同 SN、同一源地址）→ per-Router 兜住")
        srv.rl.reset_counters()
        for i in range(args.rotate):
            # 最简骨架（未签名）：限频层本来就不该信任报文内容，这里只看"这条线怎么反应"
            client.send_id_report(op.build_id_report(
                {"payload": {"sn": f"CN-WH01-RL{i:04d}"}}))
        done = wait_until(lambda: srv.rl_dropped >= args.rotate - rl.router.burst,
                          timeout=20.0, interval=0.02)
        check("per-SN 桶**抓不住**轮换 SN（每个新 SN 都是满桶）—— 这条如实展示",
              rl.dropped_sn == 0, f"sn 防线丢 {rl.dropped_sn} 条")
        check("per-Router 桶兜住了（这才是这条防线存在的理由）",
              done and rl.dropped_router > 0,
              f"router 防线丢 {rl.dropped_router} 条")

        # ---- ⑤ 发现走失不受限 ----------------------------------------------
        print("\n[5] ORPAH-FOUND（发现走失）**不受限** —— 漏一条 = 一个人没被找到")
        srv.rl.reset_counters()
        srv.mark_tracked(dev.sn, note="demo-rl")       # 立案 → 推给 Router
        synced = wait_until(
            lambda: router.lost_cache.get(dev.sn, {}).get("tracked"),
            timeout=8.0, interval=0.02)
        check("Router 已拿到走失表（否则下面的 FOUND 根本不会发生）", synced,
              f"lost_cache={router.lost_cache}")
        f0 = srv.found_count
        for _ in range(12):
            client.send_req_connect()      # 命中走失表 → Router 每条都上报 FOUND
        got = wait_until(lambda: srv.found_count >= f0 + 12, timeout=8.0, interval=0.02)
        check("12 条 FOUND 全部到达（一条都没被限频吞掉）",
              got, f"收到 {srv.found_count - f0}/12，本阶段限频丢 {rl.dropped()}")
    finally:
        stop.set()
        for fn in (client.close, router.stop, srv.stop):
            try:
                fn()
            except Exception:
                pass

    print("\n" + "=" * 74)
    print(f"  结果：{PASS} 通过 / {FAIL} 失败")
    print("  " + ("🎉 限频端到端验收全部通过" if FAIL == 0
                  else "⚠️ 存在失败用例，请检查"))
    print("=" * 74)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
