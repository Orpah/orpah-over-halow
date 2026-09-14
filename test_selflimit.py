#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_selflimit.py — 设备侧（客户端）**自愿**自限频（§5.8 设备那一环）离线测试
==============================================================================
被测：`ratelimit.DeviceLimiter`（限流内核）+ `client.ClientHost._gate`（接线）。

三件事必须锁住，且都**用假时钟**（`now=` 参数，不睡眠、毫秒级、可复现）：

1. **数学**：起手满桶 → 连发 burst 条都能过 → 之后**延后**（不是丢弃）→
   等一个最小间隔就又能发一条；`retry_after` 如实报“还差多久”。
2. **默认参数不许误伤好设备**（回归锁，同 Server/Router 两侧那两条）：
   按本 demo 的**真实节奏**（一个周期 3 条挤在 0.25s 内、然后歇 2s）跑 300 个周期，
   **零延后**；单条 1 条/秒 跑 60s 也零延后。
3. **自愿性/边界**：`force=True`（= “不是本机自己的业务上报”：攻击/刷量/重放）
   直接放行**且不扣令牌**；关掉开关后行为与没有它一样。页面/文案的口径也在这里锁一条
   （设备侧必须写「自愿」「延后」，**不许**写「丢弃」「防住了」）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ratelimit as RL
from client import ClientHost


class _FakeSta:
    """假 STA host：只记录"上过空口"的帧（`ClientHost` 里我们只用 send_frame）。

    四个方法要齐（`connect/close/send_frame/recv_frame`）：现在传输是**显式注入**的
    （`ClientHost(bus=…)`），接口不全就**当场报错** —— 少了 `recv_frame` 时实测直接
    `TypeError`（不会拖到运行中才静默失效）。
    """

    def __init__(self):
        self.frames = []

    def connect(self, retries=1):
        return True

    def send_frame(self, eth):
        self.frames.append(bytes(eth))
        return True

    def recv_frame(self, timeout=0.0):
        return None                     # 本套件不测下行

    def close(self):
        pass


def _client(with_limit=True, **kw):
    # 传输用 `ClientHost(bus=…)` **显式注入**（不再靠“构造完再改 `.sta`”）；
    # 它不连 socket，只记帧。
    return ClientHost(sta_port=1, self_limit=with_limit, bus=_FakeSta(), **kw)


class TestDeviceLimiterMath(unittest.TestCase):
    """限流内核：满桶 → burst 条 → 延后 → 回补（纯 `now=`，不碰真实时间）。"""

    def test_burst_then_hold_then_refill(self):
        d = RL.DeviceLimiter(min_interval=0.6, burst=4)
        t = 1000.0
        oks = [d.check(sn="S", kind="ID-REPORT", now=t).ok for _ in range(4)]
        self.assertEqual(oks, [True] * 4)                 # 起手满桶：一个周期的突发能过
        self.assertEqual(d.allowed, 4)
        dec = d.check(sn="S", kind="ID-REPORT", now=t)
        self.assertFalse(dec.ok)
        self.assertEqual(dec.which, "interval")
        self.assertAlmostEqual(dec.retry_after, 0.6, places=6)   # 1/min_interval=1.67/s
        self.assertEqual((d.allowed, d.held), (4, 1))
        # 回补：等一个最小间隔 → 又能发一条（这就是“延后”而不是“丢弃”）
        self.assertTrue(d.check(sn="S", kind="ID-REPORT", now=t + 0.6).ok)
        self.assertEqual((d.allowed, d.held), (5, 1))

    def test_hold_does_not_consume(self):
        """被延后的条**不扣**令牌，也不允许“欠账”（否则刷量时会把恢复时间推远）。"""
        d = RL.DeviceLimiter(min_interval=1.0, burst=1)
        t = 0.0
        self.assertTrue(d.check(now=t).ok)
        for _ in range(50):                             # 连发 50 条被延后
            self.assertFalse(d.check(now=t).ok)
        self.assertEqual(d.held, 50)
        self.assertAlmostEqual(d.wait_for(now=t), 1.0, places=6)   # 恢复时间没被推远
        self.assertTrue(d.check(now=t + 1.0).ok)

    def test_wait_for_is_pure(self):
        """`wait_for` 只算不动（同仓库纪律：不睡眠、不轮询、不消耗）。"""
        d = RL.DeviceLimiter(min_interval=0.5, burst=1)
        self.assertTrue(d.check(now=0.0).ok)
        for _ in range(100):
            self.assertGreater(d.wait_for(now=0.0), 0.0)
        self.assertEqual(d.allowed, 1)                   # 问了 100 次，一条都没多放
        self.assertEqual(d.held, 0)

    def test_disabled_is_transparent(self):
        d = RL.DeviceLimiter(enabled=False)
        for _ in range(1000):
            self.assertTrue(d.check(now=0.0).ok)
        self.assertEqual((d.allowed, d.held), (0, 0))     # 关闭时连计数都不动
        self.assertTrue(d.snapshot()["on"] is False)
        self.assertEqual(d.wait_for(now=0.0), 0.0)

    def test_recent_rows_shape_and_ring(self):
        """留痕行要能被页面**同一张表**渲染（side/which/mtype/sn/retry_after 都在）。"""
        d = RL.DeviceLimiter(min_interval=10.0, burst=1)
        self.assertTrue(d.check(sn="CN-X", kind="REPORT", now=0.0).ok)
        for _ in range(30):
            d.check(sn="CN-X", kind="REPORT", now=0.0)
        rows = d.recent(100)
        self.assertEqual(len(rows), 20)                   # 环形上限，别无限长
        r = rows[0]
        self.assertEqual(r["side"], "client")
        self.assertEqual(r["which"], "interval")
        self.assertEqual(r["mtype"], "REPORT")
        self.assertEqual(r["sn"], "CN-X")
        self.assertEqual(r["router"], "-")
        self.assertIsNotNone(r["retry_after"])
        self.assertEqual(len(d.recent(3)), 3)             # 最新在前
        self.assertIn("t", d.last_hold and d.last_hold)

    def test_reset_counters_keeps_bucket(self):
        """清计数**不**清桶（与页面 `rl_reset` 同一取舍：只清面板上的数，不清状态）。"""
        d = RL.DeviceLimiter(min_interval=10.0, burst=1)
        self.assertTrue(d.check(now=0.0).ok)
        self.assertFalse(d.check(now=0.0).ok)
        d.reset_counters()
        self.assertEqual((d.allowed, d.held, d.recent(10)), (0, 0, []))
        self.assertFalse(d.check(now=0.0).ok)             # 桶还是空的（没被“重置”)成可用）

    def test_snapshot_shape_matches_page_expectations(self):
        """页面用同一套渲染代码吃三种快照 → 必需的键一个都不能少。"""
        snap = RL.DeviceLimiter().snapshot()
        for k in ("on", "params", "allowed", "held", "recent", "since"):
            self.assertIn(k, snap)
        self.assertEqual(sorted(snap["params"]),
                         ["burst", "min_interval", "rate"])   # 参数由服务端下发，页面不写死


class TestEnvAndDefaults(unittest.TestCase):
    """环境变量（第三套前缀 `ORPAH_SELF_*`，别与 `ORPAH_RL_*`/`ORPAH_RLR_*` 混）。"""

    def _with_env(self, **kv):
        old = {k: os.environ.get(k) for k in kv}
        try:
            for k, v in kv.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = str(v)
            return RL.DeviceLimiter.from_env()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_prefix_is_self_not_rl(self):
        """改了 `ORPAH_RL_*`（Server 侧）不能连带改设备侧 —— 三套前缀必须各管各的。"""
        old_rl = os.environ.get("ORPAH_RL_SN_BURST")
        try:
            os.environ["ORPAH_RL_SN_BURST"] = "99"
            d = self._with_env(ORPAH_SELF_BURST=None)
            self.assertNotEqual(d.snapshot()["params"]["burst"], 99)
        finally:
            if old_rl is None:
                os.environ.pop("ORPAH_RL_SN_BURST", None)
            else:
                os.environ["ORPAH_RL_SN_BURST"] = old_rl

    def test_env_applied(self):
        d = self._with_env(ORPAH_SELF_ENABLE="0", ORPAH_SELF_MIN_INTERVAL="1.5",
                           ORPAH_SELF_BURST="7")
        self.assertFalse(d.enabled)
        self.assertAlmostEqual(d.min_interval, 1.5)
        self.assertEqual(d.snapshot()["params"]["burst"], 7)

    def test_bad_env_falls_back(self):
        """非法值回退默认（起不来比限错了更糟 —— 同本模块其余环境变量语义）。"""
        d = self._with_env(ORPAH_SELF_MIN_INTERVAL="abc", ORPAH_SELF_BURST="")
        self.assertAlmostEqual(d.min_interval, RL.SELF_MIN_INTERVAL)
        self.assertEqual(d.snapshot()["params"]["burst"], RL.SELF_BURST)

    def test_zero_interval_means_no_refill(self):
        """`min_interval=0` 不该崩，也不该把 rate 算成 inf（= 只给 burst 个）。"""
        d = RL.DeviceLimiter(min_interval=0.0, burst=2)
        self.assertTrue(d.check(now=0.0).ok)
        self.assertTrue(d.check(now=0.0).ok)
        dec = d.check(now=1e6)
        self.assertFalse(dec.ok)
        self.assertEqual(dec.retry_after, float("inf"))   # 如实说“等不来”，不给个假数


class TestNoDamageOnRealCadence(unittest.TestCase):
    """**回归锁**：默认参数下正常节奏零延后（同 Server/Router 两侧那两条同一性质）。

    真实节奏取自 `ui_server._report_loop`：一个周期里 REQ-CONNECT → +0.25s REPORT →
    同拍 ID-REPORT（**3 条挤在 0.25s 内**），然后歇 `every`（默认 2s；能量轴给的最小也是 2s）。
    """

    def _run(self, cycles, every, per_cycle=("REQ-CONNECT", "REPORT", "ID-REPORT")):
        d = RL.DeviceLimiter.from_env()
        t = 0.0
        for _ in range(cycles):
            for i, kind in enumerate(per_cycle):
                d.check(sn="CN-WH01-9AF3C1D2", kind=kind, now=t + (0.25 if i else 0.0))
            t += every
        return d

    def test_demo_cadence_zero_hold(self):
        d = self._run(cycles=300, every=2.0)      # 300 个周期 ≈ 10 分钟的设备会话
        self.assertEqual(d.held, 0,
                         f"正常节奏被自己延后了 {d.held} 条 —— 默认参数太紧（会误伤好设备）")
        self.assertEqual(d.allowed, 900)

    def test_energy_min_interval_zero_hold(self):
        """能量轴的最快档（2s，`energy.MIN_INTERVAL_S`）也不该触发。"""
        self.assertEqual(self._run(cycles=200, every=2.0).held, 0)

    def test_one_per_second_zero_hold(self):
        d = RL.DeviceLimiter.from_env()
        for i in range(60):
            d.check(sn="S", kind="ID-REPORT", now=float(i))
        self.assertEqual(d.held, 0)

    def test_default_is_below_server_sn_rate(self):
        """默认值必须**低于** Server 侧 per-SN 速率：守规矩的设备不该撞上游的桶。

        （Server 侧默认 2 条/秒；这里用同一个常量比较，改了任一边都会红。）
        """
        self.assertLessEqual(1.0 / RL.SELF_MIN_INTERVAL, RL.SN_RATE)

    def test_flood_does_hold(self):
        """反向锁：真刷量时必须**真的**延后（否则上面那条“零延后”就是空断言）。"""
        d = RL.DeviceLimiter.from_env()
        for _ in range(200):
            d.check(sn="S", kind="ID-REPORT", now=0.0)
        self.assertGreater(d.held, 0)


class TestClientWiring(unittest.TestCase):
    """接线：`ClientHost` 的三条上行都过闸门；`force=True` 绕过且不扣令牌。"""

    def test_off_by_default(self):
        """库对象默认**不**开（验收脚本要精确控制节奏）—— 设备应用（main/ui_server）才开。"""
        c = _client(with_limit=False)
        self.assertFalse(c.limiter.enabled)
        c.sta.frames = []
        for _ in range(50):                       # 关掉后：50 条连续上报一条都不拦
            self.assertTrue(c.send_id_report({"payload": {"sn": "CN-X"}}) > 0)
        self.assertEqual(len(c.sta.frames), 50)
        self.assertEqual(c.limiter.held, 0)

    def test_enabled_gates_sends(self):
        c = _client(with_limit=True)
        c.limiter = RL.DeviceLimiter(min_interval=100.0, burst=2)
        c.sta.frames = []
        self.assertTrue(c.send_req_connect() > 0)
        self.assertTrue(c.report_once() > 0)
        self.assertEqual(len(c.sta.frames), 2)
        self.assertEqual(c.send_id_report({"payload": {"sn": "CN-X"}}), 0)   # 闸门挡住
        self.assertEqual(len(c.sta.frames), 2)      # **没有**上空口（这才是重点）
        self.assertEqual(c.limiter.held, 1)
        self.assertEqual(c.self_held, 1)            # 镜像计数给页面用

    def test_force_bypasses_without_consuming(self):
        """`force=True`（攻击/刷量/重放注入）不扣令牌 → 别赖掉设备自己的额度。"""
        c = _client(with_limit=True)
        c.limiter = RL.DeviceLimiter(min_interval=100.0, burst=1)
        self.assertTrue(c.send_req_connect() > 0)              # 用掉唯一一个令牌
        for _ in range(30):
            self.assertTrue(c.send_id_report({"payload": {"sn": "CN-X"}}, force=True) > 0)
        self.assertEqual(c.limiter.held, 0)                    # 注入不算设备自己的上报
        self.assertEqual(c.limiter.allowed, 1)

    def test_no_network_on_hold(self):
        """被延后时**不发**（不是发了再计数）—— 判据看假 STA 收到的帧数。"""
        c = _client(with_limit=True)
        c.limiter = RL.DeviceLimiter(min_interval=100.0, burst=1)
        n = len(c.sta.frames)
        c.send_req_connect()
        for _ in range(10):
            c.send_id_report({"payload": {"sn": "CN-X"}})
        self.assertEqual(len(c.sta.frames), n + 1)     # 只有第一条真的走了空口


class TestHonestWording(unittest.TestCase):
    """口径守卫：设备侧是**自愿**的，且是**延后**（不是丢弃、更不是“防住了”）。"""

    def test_class_doc_says_voluntary(self):
        doc = RL.DeviceLimiter.__doc__ or ""
        for word in ("自愿", "不是防线", "延后", "漏报"):
            self.assertIn(word, doc, f"`DeviceLimiter` 类注释里必须出现「{word}」")

    def test_env_doc_in_module_header(self):
        doc = RL.__doc__ or ""
        self.assertIn("ORPAH_SELF_", doc)
        self.assertIn("它不是防线", doc)

    def test_i18n_client_side_says_voluntary_not_dropped(self):
        """页面文案：设备侧的行必须写「自愿」+「延后」，不得写「丢弃」/「防住了」。"""
        path = os.path.join(HERE, "ui", "static", "ui_i18n.js")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for key in ('"sl_state"', '"sl_side"', '"sl_held"', '"sl_hint"'):
            self.assertIn(key, src, f"缺 i18n 键 {key}（页面用同一张表渲染设备侧）")
        # 取出这几个键所在的行，逐条检查措辞
        lines = [ln for ln in src.splitlines()
                 if any(k in ln for k in ('"sl_held"', '"sl_hint"', '"sl_side"'))]
        self.assertTrue(lines)
        joined = "\n".join(lines)
        self.assertIn("自愿", joined)
        self.assertIn("延后", joined)
        for bad in ("防住了", "已防住"):
            self.assertNotIn(bad, joined)

    def test_page_renders_held_as_hold(self):
        """页面必须用「延后」这个词渲染 `selflimit`（`rl_*` 那套说的是“丢弃”）。"""
        with open(os.path.join(HERE, "ui", "static", "app.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("s.selflimit", js, "状态里必须真的把 `selflimit` 喂给渲染函数")
        self.assertIn('$("slHeld")', js, "设备侧那一格必须渲染出来")
        self.assertIn('T("sl_held_fmt")', js, "延后数用专属文案键（不是“丢弃”那套）")
        self.assertIn('"sl_which_interval"', js, "最近表里要能把 `interval` 这条防线译出来")


if __name__ == "__main__":
    unittest.main(verbosity=2)
