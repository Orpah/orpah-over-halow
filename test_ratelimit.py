#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ratelimit.py — 限频（§5.8）离线测试
==========================================
两层：
  ① 模块数学：令牌桶 / per-key 隔离 / key 上限淘汰 / 关闭开关 / 环境变量解析 / 并发不超发；
  ② 服务端接线：REPORT 与 ID-REPORT 在**验签之前**被限、计数与留痕对得上、
     FOUND **不**被限、**轮换 SN 绕过 per-SN 桶但被 per-Router 桶拦下**（这条是这条
     防线"为什么必须有两条"的证据，别删）、正常 1 条/秒流量**零丢弃**（不许把好设备挡在门外）。

**全部用假时钟**（`now=` 参数）→ 不睡眠、不依赖真实时间，跑起来是毫秒级、可复现。
""" 
import io
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import orpah_proto as op
import ratelimit as RL
from server import OrpahServer

ADDR = ("127.0.0.1", 40001)
ADDR2 = ("127.0.0.1", 40002)
SN = "CN-WH01-9AF3C1D2"


class TestBucket(unittest.TestCase):
    """令牌桶：起手满桶 / 连续放行 burst 个 / 补充速率 / retry_after 正确。"""

    def test_burst_then_refill(self):
        b = RL.TokenBucket(rate=2.0, burst=5.0, now=100.0)
        allowed = [b.peek(100.0)[0] for _ in range(4)]
        for _ in range(4):
            b.consume(100.0)
        self.assertEqual(allowed, [True] * 4)
        self.assertEqual(b.tokens, 1.0)
        ok, wait = b.peek(100.0)
        self.assertTrue(ok)
        b.consume(100.0)
        self.assertEqual(b.tokens, 0.0)
        ok, wait = b.peek(100.0)                    # 空桶：拒绝
        self.assertFalse(ok)
        self.assertAlmostEqual(wait, 0.5, places=6)   # rate=2/s → 0.5s 攒够 1 个
        ok, _ = b.peek(100.5)                       # 0.5s 后可以
        self.assertTrue(ok)

    def test_refill_capped_at_burst(self):
        b = RL.TokenBucket(rate=10.0, burst=3.0, now=0.0)
        b.peek(1000.0)                              # 静默很久 → 只补到满桶，不无限攒
        self.assertEqual(b.tokens, 3.0)

    def test_zero_rate_never_refills(self):
        """rate=0 = “只给 burst 个，之后永不放行” → retry_after 是 inf（不骗人说等一会儿）。"""
        b = RL.TokenBucket(rate=0.0, burst=2.0, now=0.0)
        b.consume(0.0)
        b.consume(0.0)
        ok, wait = b.peek(1e6)
        self.assertFalse(ok)
        self.assertEqual(wait, float("inf"))

    def test_burst_floor_is_one(self):
        """burst 传 0/负数也得至少 1，否则"配错了就谁都过不去"（静默拒绝最糟）。"""
        b = RL.TokenBucket(rate=1.0, burst=0, now=0.0)
        self.assertTrue(b.peek(0.0)[0])


class TestBuckets(unittest.TestCase):
    """key 表：隔离、上限淘汰（key 来自报文内容，是不可信输入 → 不能无限建桶）。"""

    def test_keys_isolated(self):
        bs = RL._Buckets(rate=1.0, burst=1.0, max_keys=16, name="sn")
        self.assertTrue(bs.peek("A", 0.0)[0])
        bs.consume("A", 0.0)
        self.assertFalse(bs.peek("A", 0.0)[0])
        self.assertTrue(bs.peek("B", 0.0)[0])       # 另一台不受影响

    def test_max_keys_evicts_oldest(self):
        bs = RL._Buckets(rate=1.0, burst=1.0, max_keys=16, name="sn")
        for i in range(40):                          # 40 个不同 key，上限 16
            bs.consume(f"K{i}", now=float(i))
        st = bs.state()
        self.assertLessEqual(st["keys"], 16)
        self.assertGreater(st["evicted"], 0)
        self.assertIn("K39", bs.buckets)             # 最新的留着
        self.assertNotIn("K0", bs.buckets)           # 最久没用的被淘汰

    def test_eviction_is_lru_not_fifo(self):
        """淘汰的是**最久未用**，不是最早创建：用过一次的老 key 应当留下来。

        （这条同时锁住实现用的是 `OrderedDict` + `move_to_end`：
        改回“扫描找 last 最小”也能过，但会变回 O(n) —— 性能那条看 `_Buckets` 的实测注释。）
        """
        bs = RL._Buckets(rate=1.0, burst=5.0, max_keys=16, name="sn")
        for i in range(16):
            bs.consume(f"K{i}", now=float(i))
        bs.peek("K0", now=100.0)                     # 把最早创建的那个标为刚用过
        bs.consume("NEW", now=101.0)                 # 满了 → 淘汰头部
        self.assertIn("K0", bs.buckets)              # 刚用过 → 留着
        self.assertNotIn("K1", bs.buckets)           # 真正最久未用的是它

    def test_top_lists_thirstiest(self):
        bs = RL._Buckets(rate=0.0, burst=5.0, max_keys=16, name="sn")
        for _ in range(5):
            bs.consume("A", 0.0)                     # A 喝干
        bs.consume("B", 0.0)                         # B 只喝一口
        top = bs.top(2, 0.0)
        self.assertEqual(top[0]["key"], "A")
        self.assertEqual(top[0]["tokens"], 0.0)


class TestLimiterLogic(unittest.TestCase):
    """两条防线：都过才扣；拒绝时明说哪条拒的；关闭开关 = 全放行。"""

    def test_two_lines_both_consumed_only_when_both_pass(self):
        rl = RL.RateLimiter(sn_rate=0.0, sn_burst=2, router_rate=0.0, router_burst=1)
        self.assertTrue(rl.check("A", ADDR, now=0.0).ok)     # sn 2→1, router 1→0
        d = rl.check("A", ADDR, now=0.0)
        self.assertFalse(d.ok)
        self.assertEqual(d.which, "router")                  # 源地址先没额度
        self.assertEqual(rl.dropped_router, 1)
        self.assertEqual(rl.dropped_sn, 0)

    def test_rejected_request_does_not_burn_the_other_bucket(self):
        """router 拒了就不该再扣 sn 的令牌 —— 否则被拒的请求白吃好设备的额度。"""
        rl = RL.RateLimiter(sn_rate=0.0, sn_burst=2, router_rate=0.0, router_burst=0)
        rl.check("A", ADDR, now=0.0)                         # router 桶 burst 至少 1 → 过
        rl.check("A", ADDR, now=0.0)                         # router 拒
        self.assertEqual(rl.sn.buckets["A"].tokens, 1.0)     # sn 只被扣了 1 次

    def test_sn_line_reported_when_sn_bucket_empty(self):
        rl = RL.RateLimiter(sn_rate=0.0, sn_burst=1, router_rate=100.0, router_burst=100)
        self.assertTrue(rl.check("A", ADDR, now=0.0).ok)
        d = rl.check("A", ADDR, now=0.0)
        self.assertFalse(d.ok)
        self.assertEqual(d.which, "sn")
        self.assertEqual((rl.dropped_sn, rl.dropped_router), (1, 0))

    def test_disabled_always_allows(self):
        rl = RL.RateLimiter(enabled=False, sn_rate=0.0, sn_burst=1, router_rate=0.0,
                            router_burst=1)
        for _ in range(100):
            self.assertTrue(rl.check("A", ADDR, now=0.0).ok)
        self.assertEqual(rl.dropped(), 0)

    def test_no_key_means_no_judgement(self):
        """按不住就不拒：连 sn 和源地址都没有时，不该拿"没有依据"去丢报文。"""
        rl = RL.RateLimiter(sn_rate=0.0, sn_burst=1)
        self.assertTrue(rl.check(None, None, now=0.0).ok)

    def test_recent_and_reset(self):
        rl = RL.RateLimiter(sn_rate=0.0, sn_burst=1, router_rate=100.0, router_burst=100)
        rl.check("A", ADDR, now=0.0)
        rl.check("A", ADDR, now=0.0)
        rec = rl.recent()
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["which"], "sn")
        self.assertEqual(rec[0]["sn"], "A")
        rl.reset_counters()
        self.assertEqual(rl.dropped(), 0)
        self.assertEqual(rl.recent(), [])

    def test_snapshot_shape(self):
        """形状 = `on/params/...`（与 /api/energy 同一习惯，页面同一份渲染代码）；
        且**不含全表**（桶表可能有几千 key，不该每秒重传）。

        下面这些字段名是**页面/告警引擎在读的**（`app.js` 的 renderRatelimit、
        `ui_server.rl_alert_view`）—— 改名就会静默显示成空，所以在这里锁住。
        """
        rl = RL.RateLimiter()
        snap = rl.snapshot()
        for k in ("on", "params", "allowed", "dropped", "top_sn", "recent", "since"):
            self.assertIn(k, snap)
        for k in ("sn_rate", "sn_burst", "router_rate", "router_burst", "max_keys"):
            self.assertIn(k, snap["params"])
        for k in ("sn", "router", "total"):
            self.assertIn(k, snap["dropped"])
        self.assertNotIn("buckets", snap)
        self.assertLessEqual(len(snap["top_sn"]), 3)


class TestConcurrency(unittest.TestCase):
    """并发不超发：多线程同时抢，通过的条数不许超过"桶容量 + 期间补充"。

    ★ 这条测的是**判定的原子性**（`RateLimiter.check` 里 peek+consume 整段持锁）。
    2026-09-13 外部评审指出：两个线程可以同时看到"还有 1 个令牌"然后都放行。
    修之前这条用例**碰巧也能过**（GIL 下两个 peek 刚好错开）—— 也就是它当时是**碰运气的**，
    这正是最坏的一种测试：真出问题时不报。现在断言的是**确定性质**：恰好放行 burst 条。
    """

    def test_atomic_decision_no_over_issue(self):
        rl = RL.RateLimiter(sn_rate=0.0, sn_burst=50, router_rate=0.0, router_burst=50)
        passed = []
        lock = threading.Lock()

        def worker():
            for _ in range(50):
                d = rl.check("A", ADDR, now=0.0)     # 同一时刻 → 不会补令牌
                if d.ok:
                    with lock:
                        passed.append(1)

        ts = [threading.Thread(target=worker) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(passed), 50)            # **恰好**桶容量，不多一个
        self.assertEqual(rl.dropped(), 8 * 50 - 50)
        self.assertEqual(rl.allowed, 50)

    def test_concurrent_two_lines_stay_consistent(self):
        """两条防线并发下也必须自洽：放行数 ≤ 两个桶各自容量，且计数对得上。"""
        rl = RL.RateLimiter(sn_rate=0.0, sn_burst=30, router_rate=0.0, router_burst=20)
        ok = []
        lock = threading.Lock()

        def worker():
            for _ in range(20):
                if rl.check("A", ADDR, now=0.0).ok:
                    with lock:
                        ok.append(1)

        ts = [threading.Thread(target=worker) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(ok), 20)                # 受更紧的那条（router=20）约束
        self.assertEqual(rl.allowed, 20)
        self.assertEqual(rl.dropped(), 6 * 20 - 20)


class TestEnv(unittest.TestCase):
    """环境变量解析：非法值一律回退默认（起不来比限错了更糟）。"""

    def _with_env(self, **kw):
        old = {k: os.environ.get(k) for k in kw}
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            return RL.RateLimiter.from_env()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_env_overrides(self):
        rl = self._with_env(ORPAH_RL_SN_RATE="7.5", ORPAH_RL_SN_BURST="3",
                            ORPAH_RL_ROUTER_RATE="99", ORPAH_RL_ROUTER_BURST="8")
        self.assertEqual(rl.sn.rate, 7.5)
        self.assertEqual(rl.sn.burst, 3.0)
        self.assertEqual(rl.router.rate, 99.0)
        self.assertEqual(rl.router.burst, 8.0)

    def test_bad_values_fall_back(self):
        rl = self._with_env(ORPAH_RL_SN_RATE="abc", ORPAH_RL_SN_BURST="",
                            ORPAH_RL_ROUTER_RATE="  ", ORPAH_RL_ENABLE="maybe")
        self.assertEqual(rl.sn.rate, RL.SN_RATE)
        self.assertEqual(rl.sn.burst, float(RL.SN_BURST))
        self.assertEqual(rl.router.rate, RL.ROUTER_RATE)
        self.assertEqual(rl.enabled, RL.ENABLE)

    def test_disable_via_env(self):
        rl = self._with_env(ORPAH_RL_ENABLE="0")
        self.assertFalse(rl.enabled)
        self.assertTrue(rl.check("A", ADDR).ok)

    def test_defaults_do_not_throttle_normal_traffic(self):
        """默认参数下，正常演示流量（1 条/秒）**永远**不该被限 —— 这是"别误伤好设备"的锁。

        算 120 秒：1 条/秒 + 每次 2s 一次的 L2 REPORT 也走同一个桶（共 ~1.5 条/秒）。
        """
        rl = RL.RateLimiter()                        # 默认参数
        drops = 0
        for i in range(120):
            for _ in range(2):                       # 1s ID + 0.5 条/s REPORT ≈ 1.5/s
                if not rl.check(SN, ADDR, now=float(i)).ok:
                    drops += 1
        self.assertEqual(drops, 0, "默认参数把正常流量限住了 → 参数或桶逻辑有问题")


class TestServerWiring(unittest.TestCase):
    """服务端接线：验签前限频、计数/留痕、FOUND 不限、轮换 SN 的兜底。"""

    def _srv(self, **rlkw):
        rl = RL.RateLimiter(**rlkw)
        ev = []
        srv = OrpahServer(rl=rl, on_ratelimit=ev.append)
        srv.sock = _FakeSock()
        return srv, rl, ev

    def test_report_limited_before_processing(self):
        srv, rl, ev = self._srv(sn_rate=0.0, sn_burst=2, router_rate=0.0, router_burst=100)
        for i in range(5):
            msg = op.build_report(SN, seq=i, rssi=-55)
            srv._handle(msg, ADDR)
        # 只有前 2 条进了业务处理（on_report 计数），其余 3 条在验签/业务之前就被丢
        self.assertEqual(srv.rl_dropped, 3)
        self.assertEqual(rl.dropped_sn, 3)
        self.assertEqual(len(ev), 3)
        self.assertEqual(ev[0]["which"], "sn")
        self.assertEqual(ev[0]["mtype"], op.MSG_REPORT)
        # 被丢的**没有**回复（不是"被拒"而是"根本不处理"），也没有记成签名失败
        self.assertEqual(len(srv.sock.sent), 2)

    def test_id_report_limited_before_verify(self):
        """被限的 ID-REPORT **不进验签**（省的就是 ECDSA 的 CPU）→ id_report_total 不涨。"""
        srv, rl, ev = self._srv(sn_rate=0.0, sn_burst=1, router_rate=0.0, router_burst=100)
        rep = {"hdr": {"v": 1, "alg": "ES256", "level": 0}, "payload": {"sn": SN},
               "sig": "x"}
        srv._handle(op.build_id_report(rep), ADDR)
        srv._handle(op.build_id_report(rep), ADDR)
        self.assertEqual(srv.id_report_total, 1)
        self.assertEqual(srv.rl_dropped, 1)
        self.assertEqual(ev[-1]["mtype"], op.MSG_ID_REPORT)

    def test_found_is_never_limited(self):
        """发现走失**故意不限频**：漏一条 = 一个人没被找到（宁可多收不可漏收）。"""
        srv, rl, ev = self._srv(sn_rate=0.0, sn_burst=1, router_rate=0.0, router_burst=1)
        for _ in range(10):
            srv._handle(op.build_found(SN), ADDR)
        self.assertEqual(srv.found_count, 10)
        self.assertEqual(srv.rl_dropped, 0)

    def test_sn_rotation_bypasses_sn_bucket_but_router_bucket_catches(self):
        """★这条防线”为什么必须有两条“的证据：轮换 SN → per-SN 桶形同虚设，
        只有 per-Router 桶还在拦（源地址换不掉）。别删。"""
        srv, rl, ev = self._srv(sn_rate=0.0, sn_burst=2, router_rate=0.0, router_burst=20)
        for i in range(60):                          # 60 条、60 个不同 SN、同一源地址
            srv._handle(op.build_report(f"CN-WH01-9AF3C{i:03d}", seq=i), ADDR)
        self.assertEqual(rl.dropped_sn, 0, "per-SN 桶抓不住轮换 SN（本来就抓不住）")
        self.assertEqual(rl.dropped_router, 60 - 20)
        self.assertEqual(srv.rl_dropped, 40)
        self.assertTrue(all(r["which"] == "router" for r in ev))

    def test_limiter_reopens_for_normal_rate(self):
        """被限过之后，只要按正常速率来，**必须**重新放行（限频不是封禁）。

        用假时钟推时间 —— 不 sleep，结果确定（先同一时刻刷 10 条，再 1 条/秒）。
        """
        rl = RL.RateLimiter(sn_rate=2.0, sn_burst=2, router_rate=0.0, router_burst=100)
        t = 100.0
        burst_ok = sum(1 for _ in range(10) if rl.check(SN, ADDR[0], now=t).ok)
        self.assertEqual(burst_ok, 2, "同一时刻只该放行桶容量 2 条")
        self.assertEqual(rl.dropped(), 8)
        for i in range(1, 6):                        # 之后 1 条/秒 < rate 2/s → 全放行
            self.assertTrue(rl.check(SN, ADDR[0], now=t + i).ok, f"第 {i}s 被误限")
        self.assertEqual(rl.dropped(), 8, "正常速率下不该再有丢弃")

    def test_server_normal_message_passes(self):
        """服务端默认参数下，一条正常报文必须照旧走完（限频不能改动正常路径）。"""
        srv, rl, ev = self._srv()
        srv._handle(op.build_report(SN, seq=1, rssi=-55), ADDR)
        self.assertEqual(srv.rl_dropped, 0)
        self.assertEqual(len(srv.sock.sent), 1)      # 照旧回了 TRACKING-STATUS

    def test_rate_limited_drop_is_not_sig_failure(self):
        """被限的 ID-REPORT 不许进 `id_reports`（否则会污染签名失败率告警）。"""
        srv, rl, ev = self._srv(sn_rate=0.0, sn_burst=1, router_rate=100.0,
                                router_burst=100)
        srv._handle(op.build_id_report({"payload": {"sn": SN}}), ADDR)
        srv._handle(op.build_id_report({"payload": {"sn": SN}}), ADDR)
        self.assertEqual(len(srv.id_reports), 1)


class _FakeSock:
    """替身 UDP socket：记录 sendto，不发真包（与 test_server 同做法）。"""

    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def setsockopt(self, *a):
        pass

    def bind(self, *a):
        pass

    def settimeout(self, *a):
        pass

    def recvfrom(self, *a):
        raise OSError

    def close(self):
        pass


def _run():
    """跑全部用例；判定 = 无 FAIL/ERROR（与 run_checks.py 口径一致）。"""
    buf = io.StringIO()
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    res = unittest.TextTestRunner(stream=buf, verbosity=2).run(suite)
    out = buf.getvalue()
    print(out)
    n = res.testsRun
    bad = len(res.failures) + len(res.errors)
    print(f"限频（§5.8）：{n - bad}/{n} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_run())
