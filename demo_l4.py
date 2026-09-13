#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_l4.py — Router 主动拉取 LOST-TABLE 验收（F-03 补充，纯 PC，无硬件）
========================================================================
补齐 L3 的最后一个走失表同步缺口：**Server 只在「变更」或「新 Router 首报」时
主动推**。若一台 Router 重启（缓存清空）或此前从未接触过 Server，而期间又没有走失
表变更事件，它将一直是空表 → 对已 mark 的 sn 误答 NOT-TRACKED。

本 demo 验证 **Router 主动拉取**（真机前续，SPEC F-03）：
  ① 启动拉取：Server 先 mark（此时无任何 Router 联系过 → Server 不会推）。
     Router.start() 启动即发 ORPAH-LOST-TABLE-REQ → Server 回当前全量 →
     缓存即刻含 sn 且 tracked=True（纯主动拉取，不依赖变更推送 / client REPORT）。
  ② 重启后首问即权威：人为清空 Router 缓存并复位“未同步”标志（模拟重启丢缓存）→
     Client REQ-CONNECT → Router 发现未同步 → 向 Server 拉取全量表 → **本次**就回
     ACCESS-INFO.tracked=True（重启后首问即拉取，不再等 Server 变更/首报推送）。
  ③ 推-拉协同：Server 之后 untrack → 变更推送（该 Router 已在“见过集”）→ 缓存
     tracked=False；再 REQ-CONNECT（sn 已在缓存，不触发拉取）→ 答 NOT-TRACKED。
  ④ 下行来源校验（A 方案，2026-09-13）：LOST-TABLE **没有签名**，一条伪造的空表
     会替换缓存并让 `_synced=True`（此后不再拉表、命中也不答 TRACKED、不再产生 FOUND）
     → 本 demo 用**真 UDP** 从“不是 Server 的源地址”打一条假表进去，必须被丢且缓存不动。

报文：R→S ORPAH-LOST-TABLE-REQ（新增，见 orpah_proto）；Server 回 ORPAH-LOST-TABLE。

运行：python demo_l4.py [--sn CN-WH01-9AF3C1D2]
"""
import argparse
import os
import socket
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

import sim                                  # noqa: E402

from server import OrpahServer              # noqa: E402
from router import RouterBridge             # noqa: E402
from client import ClientHost               # noqa: E402
import downlink                             # noqa: E402  下行真实性（F-14 B）：签名/验签
from waiting import wait_until, wait_new    # noqa: E402  等待工具（只这一份实现）
from orpah_proto import (MSG_ACCESS_INFO, MSG_LOST_TABLE_REQ,   # noqa: E402
                         MSG_LOST_TABLE,
                         build_lost_table, build_lost_table_req, new_rid, encode_msg)

# 端口独立（demo_l1/9401..、demo_l2/95xx、demo_l3/96xx、ui/9401..）
CONSOLE_A, LINK_A, HOST_A = 9701, 9711, 9721   # AP（Router 侧）
CONSOLE_B, LINK_B, HOST_B = 9702, 9712, 9722   # STA（Client 侧）
UDP_SRV = 19747


def _loop(core, stop):
    while not stop.is_set():
        core.wifi.poll()
        core.link.poll()
        time.sleep(0.005)


def last_access(access):
    """最近一条 ACCESS-INFO 的 tracked 值；无则 None。"""
    return access[-1].get("tracked") if access else None


def main():
    ap = argparse.ArgumentParser(description="ORPAH Router 主动拉表验收（纯 PC）")
    ap.add_argument("--sn", default="CN-WH01-9AF3C1D2")
    args = ap.parse_args()
    sn = args.sn

    # ---- 1) Server：先 mark（此刻没有任何 Router 联系过 → 不会主动推）----
    # 下行真实性（F-14 B，2026-09-13）：Server 持私钥签下行，Router 只持公钥。
    # 演示里两个角色同进程 → 直接传对象（真机/分进程走 ORPAH_DOWN_PUB 文件）。
    down_priv, down_pub = downlink.demo_pair()
    srv = OrpahServer(port=UDP_SRV, down_key=down_priv)
    srv.start()
    assert len(srv.routers) == 0, "初始不应有见过的 Router"
    srv.mark_tracked(sn, note="pull-demo")
    print(f"① mark sn={sn} 于 Server（此时无 Router 联系 → Server 未推）")

    # ---- 2) 模拟器 + Router：启动即主动拉表 ----
    core_a = sim.Core("Router-AP", "AP", CONSOLE_A, LINK_A, None, host_port=HOST_A)
    core_b = sim.Core("Client-STA", "STA", CONSOLE_B, LINK_B,
                      ("127.0.0.1", LINK_A), host_port=HOST_B)
    stop = threading.Event()
    for c in (core_a, core_b):
        threading.Thread(target=_loop, args=(c, stop), daemon=True).start()

    access = []                              # Client 收到的 ACCESS-INFO
    router = RouterBridge(ap_port=HOST_A, server_port=UDP_SRV, down_pub=down_pub)
    assert router.start(), "Router 连不上 AP host 口"
    # 启动拉取是否生效：缓存立即含 sn 且 tracked=True，且 Server 收到过拉表请求
    startup_pulled = (router.lost_cache.get(str(sn), {}).get("tracked") is True
                      and srv.pull_count >= 1)
    print(f"① Router.start() 启动拉表：缓存={router.lost_cache} · "
          f"pull_count={srv.pull_count} · 生效={startup_pulled}")

    # ---- 3) Client 关联 ----
    client = ClientHost(sta_port=HOST_B, sn=sn,
                        on_recv=lambda m: access.append(m)
                        if m.get("type") == MSG_ACCESS_INFO else None)
    assert client.connect(), "Client 连不上 STA host 口"
    if not wait_until(lambda: core_b.wifi.conn == sim.CONN_CONNECTED,
                      timeout=10, interval=0.1):
        print(f"  [!!] 10s 内 STA 未关联上 AP（conn={core_b.wifi.conn_str()}）—— "
              "后面的拉表检查会失败")
    print(f"STA conn = {core_b.wifi.conn_str()}")

    # ---- 4) 首次 REQ-CONNECT（启动拉表已就绪 → 无需再拉，直接答 tracked=True）----
    client.send_req_connect()
    ok_first = wait_new(access, lambda m: m.get("tracked") is True, timeout=5)
    print(f"② 首次 REQ：ACCESS-INFO.tracked={last_access(access)}"
          f"（启动拉表已让缓存就绪）")

    # ---- 5) 重启后首问即权威：清缓存 + 复位未同步 → REQ 触发同步拉取 ----
    print(f"\n--- 模拟 Router 重启（清空本地缓存 + 未同步）---")
    router.lost_cache.clear()          # 模拟重启丢缓存
    router._synced = False             # + 标记未同步（重启后需重新拉取）
    pulls_before = srv.pull_count
    client.send_req_connect()
    ok_miss = wait_new(access, lambda m: m.get("tracked") is True, timeout=5)
    pulled_now = srv.pull_count > pulls_before     # 这次 REQ 真的触发了一次拉表
    print(f"③ 重启后 REQ：ACCESS-INFO.tracked={last_access(access)} · "
          f"pull_count {pulls_before}→{srv.pull_count} · 触发拉取={pulled_now}")

    # ---- 6) untrack → 变更推送（Router 已在见过集）→ 缓存 tracked=False ----
    print(f"\n--- Server untrack sn={sn}（变更推送，Router 无需再拉）---")
    srv.untrack(sn)
    # 不用 sleep 猜时间：等到缓存真的变成 False（本项目“不猜次数等待”的规矩，见 waiting.py）
    pushed = wait_until(lambda: router.lost_cache.get(str(sn), {}).get("tracked") is False,
                        timeout=3, interval=0.05)
    if not pushed:
        print("  [!!] 3s 内未收到变更推送（缓存仍未见 tracked=False）")
    pulls_before = srv.pull_count
    client.send_req_connect()
    ok_untrack = wait_new(access, lambda m: m.get("tracked") is False, timeout=5)
    no_extra_pull = srv.pull_count == pulls_before  # sn 在缓存 → 不该触发拉表
    print(f"④ untrack 后缓存 tracked={router.lost_cache.get(str(sn))} · "
          f"REQ 应答 tracked={last_access(access)} · 未多拉={no_extra_pull}")

    # ---- 7) 关联号 rid（2026-09-12）：拉表应答 vs Server 主动推送 ----
    # 旧写法用“发出 REQ 后 ≤1.5s 内收到的 LOST-TABLE 算应答”的**时间窗猜测**：
    # 推送恰好落在窗口内会被误算成应答（少计 1 次），反之也会多计。
    # 现在 REQ 带 rid、Server 应答原样回显 → 配对是确定的。
    print("\n--- 关联号 rid：拉表应答 vs Server 主动推送 ---")
    rid_checks = {
        "REQ 带 rid（type 正确）":
            build_lost_table_req(rid="RID-1").get("rid") == "RID-1"
            and build_lost_table_req(rid="RID-1").get("type") == MSG_LOST_TABLE_REQ,
        "应答原样回显 rid": build_lost_table([], rid="RID-1").get("rid") == "RID-1",
        "主动推送不带 rid": "rid" not in build_lost_table([]),
        "new_rid 随机且为 12 位 hex":
            new_rid() != new_rid() and len(new_rid()) == 12,
    }
    # 端到端：sync() 拿到的是**应答** → 不得计入“收到走失表下发”；
    # 而此前的 untrack 变更推送**已经**计过（>0）—— 一升一不升才说明配对对了。
    push_before = router.lost_push_recv
    sync_ok2 = router.sync(timeout=2.0)
    rid_checks["此前 Server 主动推送已计入（untrack 那次）"] = push_before >= 1
    rid_checks["sync() 拿到应答且不计入“收到推送”"] = (sync_ok2
                                                  and router.lost_push_recv == push_before)
    # 回归锁：直接喂三类表到计数逻辑（应答计 0 / 推送计 1 / rid 不匹配也算推送 → 2）
    probe = RouterBridge(ap_port=HOST_A, server_port=UDP_SRV)   # 完整构造但不 start：只喂 _apply_lost_table
    probe._pull_rids.append("RID-2")
    probe._apply_lost_table(build_lost_table([], rid="RID-2"))      # 本机拉表应答
    n_reply = probe.lost_push_recv
    probe._apply_lost_table(build_lost_table([]))                    # 主动推送
    n_push = probe.lost_push_recv
    probe._pull_rids.append("RID-2")
    probe._apply_lost_table(build_lost_table([], rid="RID-3"))      # 别人的/过期的应答
    n_other = probe.lost_push_recv
    rid_checks["计数口径：应答不计 / 推送计 / rid 不匹配计（0→1→2）"] = \
        (n_reply, n_push, n_other) == (0, 1, 2)
    # 回归锁（并发）：**两次拉表重叠**时，两个应答都必须配对成功
    # （2026-09-12 复核修正：原来只存一个 `_pull_rid`，后一次 sync 覆盖前一次 →
    #   先到的应答会掉配对、被当成推送多计 1 次；改成在飞 rid 集合后不再可能）
    probe2 = RouterBridge(ap_port=HOST_A, server_port=UDP_SRV)
    probe2._pull_rids.append("RID-A")
    probe2._pull_rids.append("RID-B")
    probe2._apply_lost_table(build_lost_table([], rid="RID-B"))     # 后发的先回
    two_b = probe2.lost_push_recv
    probe2._apply_lost_table(build_lost_table([], rid="RID-A"))     # 先发的后回
    two_a = probe2.lost_push_recv
    rid_checks["重叠拉表：两个应答都算应答（不计推送）"] = (two_b, two_a) == (0, 0)
    for name, passed in rid_checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")

    # ---- 8) 下行来源校验（A 方案，2026-09-13）：伪造的 LOST-TABLE 进不来 ----
    # 为什么在这测：这张表决定“谁被当成走失者”，而 LOST-TABLE **没有签名** ——
    # 一条伪造的空表会替换缓存并让 `_synced=True`（此后不再拉表、命中也不答 TRACKED、
    # 不再产生 ORPAH-FOUND）。这里用**真 UDP** 打一条假表进去，看它是否被挡在解析之前。
    print("\n--- 下行来源校验：来源不明的 LOST-TABLE 必须被丢（A 方案）---")
    down_checks = {}
    rt_port = router.udp.getsockname()[1]
    cache_before = dict(router.lost_cache)
    rej_before = router.down_rejected
    forged = None
    try:
        # 优先“端口对、IP 不对”（127.0.0.2 是回环段别名，Windows 上可 bind）；
        # bind 不上就退回“IP 对、端口不对”（临时端口）。
        try:
            forged = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            forged.bind(("127.0.0.2", UDP_SRV))
            why = "源 IP 不对（127.0.0.2，端口与 Server 相同）"
        except OSError:
            forged = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            why = f"源端口不对（临时端口，Server 是 {UDP_SRV}）"
        forged.sendto(encode_msg(build_lost_table([], rid="RID-FAKE")),
                      ("127.0.0.1", rt_port))
        got = wait_until(lambda: router.down_rejected > rej_before,
                         timeout=3.0, interval=0.02)
        down_checks[f"伪造表被丢（{why}）"] = got and router.down_rejected == rej_before + 1
        down_checks["伪造表**没有**动走失缓存（这是最关键的一条）"] = \
            router.lost_cache == cache_before
        # 对照组：Server 正常推一张（mark_tracked 触发变更推送）→ 必须生效
        srv.mark_tracked(sn, note="down-guard")
        ok_push = wait_until(
            lambda: router.lost_cache.get(str(sn), {}).get("tracked") is True,
            timeout=3.0, interval=0.02)
        down_checks["对照组：Server 来的一定生效（没把正常下行也挡了）"] = ok_push
    except OSError as e:
        down_checks[f"（真 UDP 用例无法执行：{e}）"] = False
    finally:
        if forged is not None:
            forged.close()
    for name, passed in down_checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")

    # ---- 9) 下行真实性（B 方案，2026-09-13）：**同源**伪造与重放都进不来 ----
    # 为什么必须有这一组：A 方案只比源地址，而伪造者可以从**同一个 IP + 同一个端口**发
    # （本机任何进程、NAT 后面的主机、被改装的那台 Router）→ A 根本看不到区别。
    # B 用 Server 签名解：Router 只持**公钥**（不是共享密钥）—— 改一台 Router 学不到伪造能力。
    print("\n--- 下行真实性：Server 签名 + Router 验签（B 方案）---")
    sig_checks = {}
    sig_checks["① 本机配了 Server 公钥（真的在验，不是“未启用”）"] = router.down_pub is not None
    sig_checks["② 前面几组的合法下行都验签通过了"] = router.down_verified > 0
    sig_checks["③ 已配公钥时不应出现“未验证放行”"] = router.down_unverified == 0
    cache_b4 = dict(router.lost_cache)
    rej_b4 = router.down_rejected
    fail_b4 = router.down_sig_fail
    ours = None
    try:
        # 同源套接字：IP **和**端口都与 Server 相同（SO_REUSEADDR）→ A 方案认不出它
        try:
            ours = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ours.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ours.bind(("127.0.0.1", UDP_SRV))
            why = "源 IP+端口与 Server 完全相同（A 认不出）"
        except OSError:
            ours = None
        if ours is None:
            sig_checks[f"（同源套接字无法 bind :{UDP_SRV} → 本组只能测到“无签名被拒”）"] = None
        else:
            # ① 没签名的空表（这正是最阴的那种载荷）
            ours.sendto(encode_msg(build_lost_table([], rid="RID-FAKE-B")),
                        ("127.0.0.1", rt_port))
            got = wait_until(lambda: router.down_sig_fail > fail_b4,
                             timeout=3.0, interval=0.02)
            sig_checks[f"① 同源无签名空表被丢（{why}）"] = \
                got and router.down_rejected == rej_b4
            sig_checks["② 关键：缓存一动没动（没被空表弄瞎）"] = router.lost_cache == cache_b4
            # ③ 签名是垃圾（伪造者自己乱签）
            bad = dict(build_lost_table([], rid="RID-FAKE-C"),
                       dts=int(time.time()), dn=999999, dsig="AAAA")
            ours.sendto(encode_msg(bad), ("127.0.0.1", rt_port))
            got_bad = wait_until(lambda: router.down_sig_fail > fail_b4 + 1,
                                 timeout=3.0, interval=0.02)
            sig_checks["③ 伪造签名被丢"] = got_bad
            # ④ **重放**：拿一条 (dts,dn) 不比上次新的合法签名报文再发一次。
            #    用真私钥“回拨”只是为了让用例可复现 —— 重放本身**不需要私钥**
            #    （截获旧报文原样再发即可），这才是 B 必须带 (dts,dn) 的原因。
            last = router.down_last.get(MSG_LOST_TABLE)
            sig_checks["④ 前置：本机已接受过 Server 的 LOST-TABLE（有 (dts,dn)）"] = \
                last is not None
            if last is not None:
                replayed = downlink.sign(build_lost_table([], rid="RID-RP"), down_priv,
                                         last[1], dts=last[0])
                ours.sendto(encode_msg(replayed), ("127.0.0.1", rt_port))
                got_rp = wait_until(lambda: router.down_sig_fail > fail_b4 + 2,
                                    timeout=3.0, interval=0.02)
                sig_checks["④ 重放（(dts,dn) 不比上次新）被丢"] = \
                    got_rp and router.down_sig_fails[0]["err"] == "replay"
                sig_checks["⑤ 重放也没改到缓存"] = router.lost_cache == cache_b4
    except OSError as e:
        sig_checks[f"（本组无法执行：{e}）"] = False
    finally:
        if ours is not None:
            ours.close()
    # 对照：Server 自己推的仍然生效（别把正常下行也挡了）。
    # 注意 `untrack` 是**保留条目并置 tracked=False**（不是删条目）→ 断言要看 tracked，
    # 不能写成 `is None`（那会误判成“没生效”）。
    srv.untrack(sn, push=True)
    ok_still = wait_until(
        lambda: router.lost_cache.get(str(sn), {}).get("tracked") is False,
        timeout=3.0, interval=0.02)
    sig_checks["⑥ 对照组：Server 来的一定生效（没把正常下行也挡了）"] = ok_still
    print(f"  验签通过 {router.down_verified} 条 · 被拒 {router.down_sig_fail} 条"
          f" · 未验证放行 {router.down_unverified} 条")
    for name, passed in sig_checks.items():
        if passed is None:
            print(f"  [SKIP] {name}")
        else:
            print(f"  [{'PASS' if passed else 'FAIL'}] {name}")

    # ---- 汇总 ----
    print(f"\n=== ORPAH Router 主动拉表 验收 ===")
    checks = {
        "① mark 后启动 Router 即主动拉表追平（不依赖推送）": startup_pulled,
        "② 首次 REQ 答 tracked=True（启动拉表就绪）": ok_first,
        "③ 重启后 REQ 同步拉表、首问即权威": ok_miss and pulled_now,
        "④ 变更推送仍生效且 REQ 不再多拉": pushed and ok_untrack and no_extra_pull,
    }
    checks.update(rid_checks)
    checks.update(down_checks)
    checks.update({k: v for k, v in sig_checks.items() if v is not None})
    for name, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    ok = all(checks.values())
    print("结果:", "[PASS]" if ok else "[FAIL]")

    stop.set()
    client.close(); router.stop(); srv.stop()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
