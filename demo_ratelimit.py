#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""demo_ratelimit.py — 限频（§5.8）端到端演示 + 验收（纯 PC，无硬件）

**演示什么**：签名只能滤掉「伪造」，滤不掉「**洪水**」——
一台设备（或被改过的固件）以极高频率上报时，服务器每条都要走一次 ECDSA 验签，
不设限就是自伤。本脚本搭起真链路（Client host → STA → 空口 → AP → Router 桥 → UDP → Server），
**真的**连发报文，在 Server 侧断言：

  ① 正常速率（1 条/秒）**一条都不丢** —— 限频不能把好设备挡在门外；
  ② 同一 SN 连发 200 条 → **两侧各拦一段**：Router 侧（带宽，per-SN 桶 40）先砍掉大半，
     剩下的在 Server 侧**验签之前**被丢（CPU，per-SN 桶 5）—— 批次数必须能全量对账；
  ③ **限频不是封禁**：等桶回补后，正常上报立刻恢复（不是"拉黑"）；
  ④ 轮换 SN 连发（源地址不变）→ per-SN 桶**形同虚设**（每个新 SN 都是满桶），
     由 **per-Router 桶**兜住 —— 这就是"为什么必须有两条防线"的现场证据；
  ⑤ 未签名的 REQ-CONNECT（相当于 probe）连发 → Router 侧**按源 MAC** 限（§5.8 行 1）；
  ⑥ 但**命中走失表的 REQ-CONNECT 不限**（那条路径会顺便产生 ORPAH-FOUND，
     漏一条 = 一个人没被找到）→ 它产生的 FOUND 一条不丢。

运行：C:\\Python313\\python.exe demo_ratelimit.py
端口：99xx / 19947（与 demo_l1..l4、demo_spoof、ui_server 错开）
依赖的 `OrpahServer` 接口（本脚本是“外部调用方”，改了这些名字记得同步这里）：
  · `rl`（注入的 `RateLimiter`，共用同一实例）、`rl_dropped`（累计丢弃数）、
    `rl_drops`（最近丢弃的 deque，看 `which` 用）
  · `RouterBridge.rl` / `rl_dropped` / `rl_drops`（Router 侧限频，同样读法）
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
import downlink                                # noqa: E402  下行真实性（F-14 B）
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


def wait_budget(rtr, rl, sn, need, mac=None, timeout=20.0):
    """等这条报文**一路上所有桶**都攒够 `need` 个令牌（只等一个桶不够 —— 2026-09-13 实测踩过）。

    一条已签 ID-REPORT 要依次过 **三个**桶：Router 侧转发（按 SN，burst 40）、
    Server 侧 per-SN（burst 5）、Server 侧 per-Router（源地址，burst 10）。
    前置只等其中一个时会出现两种**看起来像限频坏了**的现象：
      · 只等 Server 的 per-SN → 报文死在 Router 转发桶（页面上 0 接受、0 丢弃，查不出死在哪）；
      · 等了两侧的 per-SN 但没等 Server 的 per-Router → 4 条全被 Server 丢掉（`srv 丢 4 / rtr 丢 0`）。
    所以：**“上游有额度”这件事必须按整条路径来判**，缺一个都不算。
    `mac` 给了就连 Router 侧的 probe 桶（未签名 REQ-CONNECT 走那条）一起等。
    """
    def ready():
        if rtr.sn.tokens(sn) < need or rl.sn.tokens(sn) < need or rl.router.tokens("127.0.0.1") < need:
            return False
        return (mac is None) or (rtr.router.tokens(mac) >= need)
    ok = wait_until(ready, timeout=timeout, interval=0.05)
    return ok, (f"rtr.sn={rtr.sn.tokens(sn)} rl.sn={rl.sn.tokens(sn)} "
                f"rl.router={rl.router.tokens('127.0.0.1')}"
                + (f" rtr.probe={rtr.router.tokens(mac)}" if mac else ""))


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
    print(f"  Server 侧  : per-SN burst={rl.sn.burst:.0f}/{rl.sn.rate:.1f}s "
          f"· per-Router burst={rl.router.burst:.0f}/{rl.router.rate:.1f}s")
    print(f"  Router 侧  : per-SN burst={RL.RTR_SN_BURST}/{RL.RTR_SN_RATE:.1f}s（转发）"
          f" · per-MAC burst={RL.RTR_MAC_BURST}/{RL.RTR_MAC_RATE:.1f}s（probe）")
    print()

    coreA = sim.Core("Router-AP", "AP", CONSOLE_A, LINK_A, None, host_port=HOST_A)
    coreB = sim.Core("Client-STA", "STA", CONSOLE_B, LINK_B,
                     ("127.0.0.1", LINK_A), host_port=HOST_B)
    threading.Thread(target=loop_cores, args=([coreA, coreB], stop), daemon=True).start()

    down_priv, down_pub = downlink.demo_pair()     # 下行签名（F-14 B）
    srv = OrpahServer(port=UDP_SRV, keystore=ks, id_nonces=oid.NonceCache(),
                      down_key=down_priv,
                      on_id_report=sink.on_id_report, rl=rl)
    srv.start()
    router = RouterBridge(ap_port=HOST_A, server_port=UDP_SRV, down_pub=down_pub)
    if not router.start():
        print("  Router 连不上 AP host 口，退出")
        return 2
    rtr = router.rl          # Router 侧限频器（与 server 侧是两个实例、两套参数）
    client = ClientHost(sta_port=HOST_B, sn=dev.sn, self_limit=True)
    # ↑ 设备侧**自愿**自限频（§5.8 设备那一环）：本脚本要同时演示三层——
    #   ① 守规矩的设备（走自限频）根本不撞上游的桶；② 被要求过快时自己**延后**；
    #   ③ 绕过它（`force=True` = “一台失控/被改的设备”）才会撞上 Router/Server 的桶。
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

        # ---- ② 同一 SN 连发（两侧各拦一段）----------------------------------
        print(f"\n[2] 同一 SN 连发 {args.flood} 条 → Router 侧砍带宽、Server 侧砍 CPU")
        srv.rl.reset_counters()
        rl_d0, rtr_d0 = srv.rl_dropped, router.rl_dropped
        n1 = sink.count()
        for _ in range(args.flood):
            # force=True：本组模拟的就是“一台失控/被改的设备连发” —— 它**不做**自限频，
            # 否则这一组根本不会撞上 Router/Server 的桶（那正是下面第 [7] 组的对照）。
            client.send_id_report(dev.report(), force=True)
        # 判据：**两侧合计**把这批处理/丢弃完（Router 侧丢的不会到 Server，不能只看 server）。
        # 2026-09-13 实测踩过：只等 server 侧计数，Router 侧一开就永远等不到。
        done = wait_until(
            lambda: (sink.count() - n1) + (srv.rl_dropped - rl_d0)
            + (router.rl_dropped - rtr_d0) >= args.flood,
            timeout=30.0, interval=0.02)
        acc = sink.count() - n1
        d_srv = srv.rl_dropped - rl_d0
        d_rtr = router.rl_dropped - rtr_d0
        check("这批被两侧合计对账完（超时可见地失败，不静默）", done,
              f"接受 {acc} + 丢(Router {d_rtr} / Server {d_srv}) "
              f"= {acc + d_rtr + d_srv}/{args.flood}")
        check("Router 侧先砍带宽（第一道，§5.8 行 2）", d_rtr > 0,
              f"Router 丢 {d_rtr} 条（per-SN 桶容量 {rtr.sn.burst:.0f}）")
        check("Server 侧也拦下一段（两条线都动，这才叫分层）", d_srv > 0,
              f"Server 丢 {d_srv} 条（per-SN 桶容量 {rl.sn.burst:.0f}）")
        check("两侧拒绝原因都明说是 per-SN 防线（不只是'被拒了'）",
              {r["which"] for r in srv.rl_drops} == {"sn"}
              and {r["which"] for r in router.rl_drops} == {"sn"},
              f"server={sorted({r['which'] for r in srv.rl_drops})} "
              f"router={sorted({r['which'] for r in router.rl_drops})}")
        check("Server 侧计数只含**真的到达它**的那部分（没把 Router 丢的算到自己头上）",
              acc + d_srv < args.flood,
              f"server 侧看到 {acc + d_srv} / 全量 {args.flood}")

        # ---- ③ 限频不是封禁：回补后立刻恢复 -------------------------------
        print("\n[3] 限频不是封禁：等桶回补后正常上报立刻恢复")
        # 前置：这条报文要过**三个**桶（Router 转发 / Server per-SN / Server per-Router）——
        # 必须**按整条路径**等额度。2026-09-13 实测踩过：只等 Server 的 per-SN，
        # 报文死在 Router 转发桶 → 现场是“0 接受、0 丢弃”，看着像限频坏了。
        budget, tok = wait_budget(rtr, rl, dev.sn, 1, timeout=20.0)
        check("前置：整条路径上的桶都攒够额度（否则测的不是“回补后恢复”）", budget, tok)
        d_before = srv.rl_dropped
        already = rl.sn.peek(dev.sn)[0]
        check("桶会回补（不是把设备拉黑）", already or budget, f"进组时 {tok}")
        n2 = sink.count()
        rc_d1 = router.rl_dropped
        client.send_id_report(dev.report())
        ok2 = wait_until(lambda: sink.count() > n2, timeout=5.0, interval=0.02)
        check("回补后这一条被接受", ok2 and srv.rl_dropped == d_before,
              f"接受增量 {sink.count() - n2} / 新丢弃 {srv.rl_dropped - d_before}"
              f" / Router 侧丢 {router.rl_dropped - rc_d1} / 进组时 {tok}")

        # ---- ④ 轮换 SN：per-SN 拦不住，per-Router 兜住 --------------------
        print(f"\n[4] 轮换 SN（{args.rotate} 个不同 SN、同一源地址）→ per-Router 兜住")
        srv.rl.reset_counters()
        for i in range(args.rotate):
            # 最简骨架（未签名）：限频层本来就不该信任报文内容，这里只看"这条线怎么反应"
            # force=True：同第 [2] 组 —— 模拟“一台失控设备/外部注入”
            client.send_id_report(op.build_id_report(
                {"payload": {"sn": f"CN-WH01-RL{i:04d}"}}), force=True)
        done = wait_until(lambda: srv.rl_dropped >= args.rotate - rl.router.burst,
                          timeout=20.0, interval=0.02)
        check("per-SN 桶**抓不住**轮换 SN（每个新 SN 都是满桶）—— 这条如实展示",
              rl.dropped_sn == 0, f"sn 防线丢 {rl.dropped_sn} 条")
        check("per-Router 桶兜住了（这才是这条防线存在的理由）",
              done and rl.dropped_router > 0,
              f"router 防线丢 {rl.dropped_router} 条")

        # ---- ⑤ 未签名 REQ-CONNECT（相当于 probe）→ Router 侧按源 MAC 限 ---------
        #       （§5.8 行 1；此时 SN 还没立案，所以**会**走这条线）
        print("\n[5] 未签名 REQ-CONNECT 连发 → Router 侧按**源 MAC** 限（§5.8 行 1）")
        sent_rc = 12
        rc0, rtr_d1 = client.recv, router.rl_dropped
        sn0, mac0 = rtr.dropped_sn, rtr.dropped_router      # 计数是**累计**的 → 本阶段要看增量
        for _ in range(sent_rc):
            # force=True：这一组模拟“未签名 probe 连发”（失控设备/外部扫描器）——
            # 正是自限频那一环**不会**发生的情形，只有绕过它才能看到 Router 侧按源 MAC 限。
            client.send_req_connect(force=True)
        got = wait_until(lambda: router.rl_dropped - rtr_d1 >= 5,
                         timeout=8.0, interval=0.02)
        dropped_rc = router.rl_dropped - rtr_d1
        replies = client.recv - rc0
        check("probe 桶拦下了多余的 REQ-CONNECT", got,
              f"Router 丢 {dropped_rc}/{sent_rc}（mac 桶容量 {rtr.router.burst:.0f}）")
        check("被拦的那些**真的没回 ACCESS-INFO**（不是只记了个数）",
              replies < sent_rc,
              f"收到下行 {replies} 条 < 发出 {sent_rc} 条")
        check("probe 走**源 MAC** 那条防线（which=router），且不吃该设备的转发额度",
              rtr.dropped_router - mac0 > 0 and rtr.dropped_sn == sn0,
              f"本阶段 router 防线丢 {rtr.dropped_router - mac0} 条 / "
              f"sn 防线丢 {rtr.dropped_sn - sn0} 条")

        # ---- ⑥ 但**命中走失表的 REQ-CONNECT 不限**（FOUND 一条都不能丢）-------
        print("\n[6] ORPAH-FOUND（发现走失）**不受限** —— 漏一条 = 一个人没被找到")
        srv.rl.reset_counters()
        srv.mark_tracked(dev.sn, note="demo-rl")       # 立案 → 推给 Router
        synced = wait_until(
            lambda: router.lost_cache.get(dev.sn, {}).get("tracked"),
            timeout=8.0, interval=0.02)
        check("Router 已拿到走失表（否则下面的 FOUND 根本不会发生）", synced,
              f"lost_cache={router.lost_cache}")
        f0 = srv.found_count
        rtr_d2 = router.rl_dropped
        for _ in range(12):
            # force=True：这里模拟的是“probe 连发”（外部/失控设备），不是设备自己的业务上报
            client.send_req_connect(force=True)   # 命中走失表 → Router 每条都上报 FOUND
        got = wait_until(lambda: srv.found_count >= f0 + 12, timeout=8.0, interval=0.02)
        check("12 条 FOUND 全部到达（一条都没被限频吞掉）", got,
              f"收到 {srv.found_count - f0}/12")
        check("这一阶段 Router 侧一条都没丢（命中走失表 → 不走限频）",
              router.rl_dropped == rtr_d2,
              f"Router 丢 {router.rl_dropped - rtr_d2} 条 —— 按上面 [5] 的桶容量，"
              f"若走限频必然要丢")

        # ---- ⑦ 设备侧自限频（自愿）：自己延后 ≠ 丢弃，且根本不撞上游的桶 --------
        print("\n[7] 设备侧自限频（§5.8 设备那一环，**自愿**）：延后 ≠ 丢弃，不撞上游的桶")
        lp = client.limiter
        pp = lp.snapshot()["params"]
        # 前置：这条路径上的**所有**桶都要攒够这一段突发（上面几组刚把桶刷空过）。
        # 不先等就分不清“是设备侧自限频起了作用”还是“上游还没回补” —— 实测踩过三次：
        # ① 完全不等 → 头 3 条全丢，看起来像自限频没用；
        # ② 只等“有额度”（1 个令牌）→ 桶边缘仍会丢掉剩下的；
        # ③ 只等两侧的 per-SN、**漏了 Server 的 per-Router** → 4 条被 Server 全丢
        #    （现场 `srv 丢 4 / rtr 丢 0`）。所以走 wait_budget（整条路径）。
        need = int(pp["burst"])
        budget, tok = wait_budget(rtr, rl, dev.sn, need, timeout=20.0)
        check(f"前置：整条路径上的桶都回补到 ≥{need} 个令牌（否则测的不是设备侧自限频）",
              budget, tok)
        lp.reset_counters()
        srv.rl.reset_counters()
        router.rl.reset_counters()
        print(f"    设备侧参数：最小间隔 {pp['min_interval']}s（≈{pp['rate']} 条/秒）"
              f"、突发 {pp['burst']} 条；测试发 20 条“不等待”的上报")
        d1, r1 = srv.rl_dropped, router.rl_dropped
        n3, a3 = sink.count(), lp.allowed
        for _ in range(20):
            client.send_id_report(dev.report())        # 守规矩的设备：不 force
        held = lp.held
        escaped = lp.allowed - a3                       # 真的上了空口的条数
        check("设备把多余的上报**延后**了（不是丢弃：没上空口，但条数记着）",
              held > 0 and escaped + held == 20,
              f"发出 {escaped} + 延后 {held} = {escaped + held}/20")
        done7 = wait_until(lambda: sink.count() - n3 >= escaped,
                           timeout=10.0, interval=0.02)
        check("这批发出去的都被上游收下了", done7,
              f"接受 {sink.count() - n3}/{escaped}")
        check("★ 守规矩的设备**不撞上游的桶**：Router 与 Server 两侧丢弃都是 0",
              srv.rl_dropped == d1 and router.rl_dropped == r1,
              f"Server 丢 {srv.rl_dropped - d1} / Router 丢 {router.rl_dropped - r1}"
              f"（跟第 [2] 组同一种“不等待连发”，差别只在于走不走自限频）")
        check("桶会回补：等一个最小间隔后这一条能被发出去（所以叫“延后”）",
              wait_until(lambda: lp.wait_for() <= 0.0, timeout=5.0, interval=0.02)
              and client.send_id_report(dev.report()) > 0,
              f"等 {pp['min_interval']}s 后这一条发出去了（累计放行 {lp.allowed}）")
        # 对照：同一节奏、但绕过自限频（= 一台被改/失控的设备）→ 上游开始丢
        srv.rl.reset_counters()
        router.rl.reset_counters()
        d2, r2 = srv.rl_dropped, router.rl_dropped
        n4 = sink.count()
        for _ in range(20):
            client.send_id_report(dev.report(), force=True)     # 不做自限频
        done8 = wait_until(
            lambda: (sink.count() - n4) + (srv.rl_dropped - d2)
            + (router.rl_dropped - r2) >= 20, timeout=20.0, interval=0.02)
        lost = (srv.rl_dropped - d2) + (router.rl_dropped - r2)
        check("★ 对照：绕过自限频（被改/失控的设备）→ 上游开始丢（这就是“自愿”的代价）",
              done8 and lost > 0,
              f"20 条里被上游丢 {lost} 条（Server {srv.rl_dropped - d2} / "
              f"Router {router.rl_dropped - r2}）—— 自限频不是防线，被改的设备不做它")
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
