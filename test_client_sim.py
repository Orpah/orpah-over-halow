#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_client_sim.py — 客户端**设备仿真器**离线测试（`client_sim.DeviceSim`）
================================================================================
被测：`client_sim.py`（设备状态机 + 旋钮）。**全部离线**：假 STA（只记帧、不开 socket）、
假时钟（`now_fn`/`sleeper` 注入，**不睡眠**、毫秒级、可复现）。

为什么这套值得有（而不是靠 `demo_l1/demo_l2` 顺手覆盖）：
  1. **它是固件的参照**（步骤 c/d/e 的固件照这个状态机写）→ 它必须能离线、可复现地自证；
  2. 设备侧的旋钮（无 RTC / 能力声明 / §8.2 故障模式 / 能量）**每一个都走规范里那条路径**，
     走错不会报错、只会静默产出"看着正常"的报文（最坏的一类 bug）；
  3. **不许在仿真器里重写协议** —— 报文/签名/选级/限频/间隔都必须来自各自的单一源，
     这条用源码守卫钉住（见最后一节）。

运行：python test_client_sim.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock as mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import client_sim as cs          # noqa: E402
import energy as en              # noqa: E402
import host_serial as hs         # noqa: E402
import keystore as ksdb          # noqa: E402
import orpah_id as oid           # noqa: E402
import orpah_proto as op         # noqa: E402
import ratelimit as RL           # noqa: E402
import waiting                   # noqa: E402
from client import ClientHost    # noqa: E402

SN = "CN-WH01-9AF3C1D2"
NOW0 = 1_800_000_000.0


class _FakeSta:
    """假 STA host：只记"上过空口"的帧 + 可喂下行帧（构造时**不连**任何 socket）。

    刻意**不实现帧格式** —— 帧的编解码在 `orpah_proto`/`host_bus`（单一源），
    这里只做"线"的角色（收字节、给字节）。
    """

    def __init__(self):
        self.frames = []          # 上行：ClientHost 注入的以太网帧
        self.down = []            # 下行：待 `recv_frame` 取走的以太网帧
        self.connect_ok = True

    def connect(self, retries=1):
        return self.connect_ok

    def send_frame(self, eth):
        self.frames.append(bytes(eth))
        return True

    def recv_frame(self, timeout=0.0):
        return self.down.pop(0) if self.down else None

    def close(self):
        pass


class _FakeSerial:
    """假串口（只扮"线"）：记录写出的字节；`read()` 吐预置的输入流，可切成多块模拟粘包。

    `auto_ok=True` 时，凡是写出去的 `AT+…\r\n` 命令都会"回"一行 OK（模拟模块应答）。
    它**不实现帧格式**（那是 `orpah_proto`/`host_serial` 的事）。
    """

    def __init__(self, chunks=None, auto_ok=True):
        self.written = b""
        self.inbuf = b""
        self.chunks = list(chunks or [])
        self.auto_ok = auto_ok
        self.closed = False

    def write(self, data):
        data = bytes(data)
        self.written += data
        if self.auto_ok and data.startswith(b"AT+") and data.endswith(b"\r\n"):
            self.inbuf += b"OK\r\n"

    def read(self, n=256):
        if not self.inbuf and self.chunks:
            self.inbuf += self.chunks.pop(0)
        if not self.inbuf:
            time.sleep(0.002)          # 模拟串口读超时（别把 CPU 占了）
            return b""
        out, self.inbuf = self.inbuf[:n], self.inbuf[n:]
        return out

    def reset_input_buffer(self):
        self.inbuf = b""

    def close(self):
        self.closed = True


class _Clock:
    """假时钟：`now_fn` 与 `sleeper` 都走它（`sleeper` 只把时间往前推，**不真睡**）。"""

    def __init__(self, t=NOW0):
        self.t = float(t)
        self.slept = 0.0

    def now(self):
        return self.t

    def sleep(self, sec):
        self.t += float(sec)
        self.slept += float(sec)


def make_sim(**kw):
    """建一个连到**假 STA** 的设备仿真器（不睡眠、不开 socket）。

    默认**关掉**自限频：多数用例要断言"结构"（发了哪几条、什么顺序），
    限流会把条数改小、让断言变成在测限流；限流单独在 `TestSelfLimit` 里显式打开。
    传输用 `ClientHost(bus=…)` **显式注入**（不再是“构造完再改 `.sta`”）。
    """
    limit = kw.pop("self_limit", False)
    kw.setdefault("client", ClientHost(sta_port=1, self_limit=limit, bus=_FakeSta()))
    sim = cs.DeviceSim(sn=SN, log=None, **kw)
    sim.clock = _Clock()
    sim._now = sim.clock.now
    sim._sleep = sim.clock.sleep
    return sim


def up_frames(sim):
    """仿真器注入的以太网帧 → 已解码报文列表（用真实解码器，不手写解析）。"""
    out = []
    for eth in sim.client.sta.frames:
        p = op.parse_eth_frame(eth)
        if p is None:
            continue
        out.append(op.decode_msg(p[1]))
    return out


def signed_of(msg):
    """ID 报文是**包了一层**的（`build_id_report`）→ 取出内层已签报文 `{hdr,payload,sig}`。"""
    return msg["report"]


def ks_for(sim):
    """带该设备公钥的内存密钥库（= 服务端侧“认识这个 SN”）。"""
    ks = ksdb.KeyStoreDB(":memory:")
    ks.register(sim._ensure_device(), model="test", firmware="sim")
    return ks


class TestSession(unittest.TestCase):
    """一拍里发什么、按什么顺序（与 `ui_server._report_loop` 同序）。"""

    def test_default_every_is_design_normal_60s(self):
        """★默认周期 = 设计常态（单一源）—— 各写一份的坏法是"看着都正常"的漂移。"""
        sim = make_sim()
        self.assertEqual(sim.every, en.NORMAL_INTERVAL_S)
        self.assertEqual(sim.every, 60.0)
        self.assertEqual(make_sim(every=5).every, 5.0)

    def test_cycle_order_and_content(self):
        sim = make_sim(every=60)
        res = sim.cycle()
        msgs = up_frames(sim)
        self.assertEqual([m["type"] for m in msgs],
                         [op.MSG_REQ_CONNECT, op.MSG_REPORT, op.MSG_ID_REPORT])
        self.assertEqual(msgs[1]["sn"], SN)
        self.assertEqual(msgs[1]["rssi"], -55)
        self.assertEqual(msgs[1]["seq"], 1)
        self.assertTrue(res["with_id"] and res["report"] > 0)
        self.assertIsNone(res["level"])               # auto → 级别由 pick_level 算出（L0）

    def test_id_report_can_be_disabled(self):
        sim = make_sim(id_report=False)
        sim.cycle()
        self.assertEqual([m["type"] for m in up_frames(sim)],
                         [op.MSG_REQ_CONNECT, op.MSG_REPORT])

    def test_run_cycles_returns_snapshot(self):
        sim = make_sim(cycles=3)
        snap = sim.run()
        self.assertEqual(snap["tick"], 3)
        self.assertEqual(len(up_frames(sim)), 9)      # 3 拍 × (REQ-CONNECT + REPORT + ID)
        # L2 = REQ-CONNECT + REPORT，**分开报**（看成一个数就分不出握手与上报）
        self.assertEqual((snap["req_sent"], snap["report_sent"]), (3, 3))
        self.assertEqual(snap["id_sent"], 3)

    def test_seq_increments_per_cycle(self):
        sim = make_sim()
        sim.run(cycles=2)
        reps = [m for m in up_frames(sim) if m["type"] == op.MSG_REPORT]
        self.assertEqual([r["seq"] for r in reps], [1, 2])


class TestSignedReport(unittest.TestCase):
    """已签上报：服务端**真的能验过**（不是"我们自认为签了"）。"""

    def test_signed_id_report_verifies(self):
        sim = make_sim()
        sim.cycle()
        rep = sim.last_level0                             # hdr（供断言）
        self.assertEqual(rep["typ"], "orpah-id-report")
        full = signed_of(up_frames(sim)[-1])
        res = oid.verify_report(full, ks_for(sim), now=NOW0)
        self.assertTrue(res["accepted"], res)
        self.assertEqual(res["alg"], oid.ALG_ES256)
        self.assertEqual(res["level"], 0)

    def test_no_rtc_device_sends_ts_zero_and_cap_false(self):
        """无 RTC 终端：`ts=0` + 已签声明 `cap.rtc=False`（谎报会被服务端抓）。"""
        sim = make_sim(ts_zero=True, cap_rtc=False)
        sim.cycle()
        msgs = up_frames(sim)
        self.assertEqual(msgs[1]["ts"], 0)                # REPORT：无可用时钟 → 0
        full = signed_of(msgs[-1])
        self.assertEqual(full["payload"]["ts"], 0)
        self.assertEqual(full["payload"]["cap"], {"rtc": False})
        res = oid.verify_report(full, ks_for(sim), now=NOW0)
        self.assertTrue(res["accepted"], res)

    def test_cap_is_inside_signature(self):
        """★`cap` 在签名预像里 —— 改它就验不过（这就是"能力降级"被抓住的原因）。"""
        sim = make_sim(cap_rtc=False)
        sim.cycle()
        full = signed_of(up_frames(sim)[-1])
        ks = ks_for(sim)
        self.assertTrue(oid.verify_report(full, ks, now=NOW0)["accepted"])
        tampered = json.loads(json.dumps(full))
        tampered["payload"]["cap"] = {"rtc": True}        # 谎称"我有 RTC"
        res = oid.verify_report(tampered, ks, now=NOW0)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["error"], "signature_invalid")

    def test_declares_rtc_when_asked(self):
        sim = make_sim(cap_rtc=True)
        sim.cycle()
        self.assertEqual(signed_of(up_frames(sim)[-1])["payload"]["cap"], {"rtc": True})

    def test_battery_and_firmware_in_signed_payload(self):
        sim = make_sim(battery_mv=3700)
        sim.cycle()
        pl = signed_of(up_frames(sim)[-1])["payload"]
        self.assertEqual(pl["battery_mv"], 3700)          # 电量写进**已签**报文（设备不能抵赖）
        self.assertEqual(pl["firmware"], "sim-1.0")


class TestLevels(unittest.TestCase):
    """§8.2 降级：仿真器传的是"哪个环节坏了"，级别由 `pick_level` 算（单一源）。"""

    def test_level_follows_pick_level(self):
        for mode in oid.LEVEL_MODES:
            lv, _why = oid.pick_level(**oid.LEVEL_MODES[mode])
            with self.subTest(mode=mode):
                sim = make_sim(level_mode=mode)
                sim.cycle()
                hdr = signed_of(up_frames(sim)[-1])["hdr"]
                self.assertEqual((hdr["level"], hdr["alg"]),
                                 (lv, oid.LEVEL_TO_ALG[lv]))

    def test_l3_is_unsigned_and_not_presence(self):
        """L3（无可用密钥）= 裸上报：验签侧必须**不当成"人在场"**。"""
        sim = make_sim(level_mode="no_key")
        sim.cycle()
        full = signed_of(up_frames(sim)[-1])
        self.assertEqual(full["hdr"]["alg"], "none")
        res = oid.verify_report(full, ks_for(sim), now=NOW0)
        self.assertTrue(res["accepted"])                  # 收下了
        self.assertTrue(res["coverage_only"])             # 但只是覆盖发现
        self.assertFalse(oid.counts_as_presence(res))     # **不能当人员出现**


class TestEnergy(unittest.TestCase):
    """能量模式：间隔与级别**由 `energy.plan` 定**（模型单一源，仿真器不自己算）。"""

    def test_interval_and_level_come_from_plan(self):
        sim = make_sim(energy=True, harvest_mw=0.2)
        sim.cycle()
        p = en.plan(0.2, en.CHARGE0_MJ, en.STORE_MJ)
        self.assertEqual(sim.interval_s, p["interval_s"])
        self.assertEqual(sim.level, 1 if p["degraded"] else None)
        if p["degraded"]:
            self.assertEqual(signed_of(up_frames(sim)[-1])["hdr"]["alg"], oid.ALG_HS256)
        self.assertEqual(sim.every, max(0.5, p["interval_s"] / sim.speedup))

    def test_silent_when_harvest_below_overhead(self):
        """采不敷出 → **如实沉默**（不发报，也不编一条出来）；帧数为 0 是判据。"""
        sim = make_sim(energy=True, harvest_mw=0.0)
        res = sim.cycle()
        self.assertTrue(res["silent"])
        self.assertEqual(res["why"], "silent")
        self.assertEqual(sim.client.sta.frames, [])       # 一条都没发
        self.assertTrue(sim.snapshot()["silent"])

    def test_battery_voltage_written_into_signed_report(self):
        sim = make_sim(energy=True, harvest_mw=0.5, battery_mv=None)
        before = sim.charge_mj                            # 报文里写的是**发报那一刻**的电量
        sim.cycle()
        mv = signed_of(up_frames(sim)[-1])["payload"]["battery_mv"]
        self.assertEqual(mv, en.mv_of(before, sim.store_mj))
        self.assertIsInstance(mv, int)


class TestSelfLimit(unittest.TestCase):
    """自限频：语义是**延后**（不是丢弃）—— 下一拍还会发，不丢自己的业务报。

    桶参数默认 0.6 s / 突发 4 条（`ratelimit.DeviceLimiter`），这里显式打开。
    """

    def test_burst_then_hold(self):
        sim = make_sim(self_limit=True)                   # 假时钟**不前进** → 只有突发那几条能过
        sim.run(cycles=4)                                 # 4 拍 × 3 条本应 12 条
        self.assertEqual(len(up_frames(sim)), 4)          # 起手满桶 = 4 条
        self.assertGreater(sim.client.self_held, 0)
        self.assertGreater(sim.snapshot()["held"], 0)

    def test_disabled_matches_no_limiter(self):
        sim = make_sim(self_limit=False)
        sim.run(cycles=6)
        self.assertEqual(len(up_frames(sim)), 18)         # 关掉后一条不少
        self.assertEqual(sim.client.self_held, 0)

    def test_hold_is_not_drop(self):
        """★被延后的条**不是丢弃**：桶回补后下一拍就把欠着的发出去。

        用 50 ms 的小桶（而不是默认 0.6 s）—— 测试里只等一个**回补周期**，
        且用 `waiting.wait_until` 等条件（不是猜次数），超时就**可见地失败**。
        """
        lim = RL.DeviceLimiter(enabled=True, min_interval=0.05, burst=1)
        sim = make_sim(client=ClientHost(sta_port=1, limiter=lim, bus=_FakeSta()))
        sim.cycle()
        n1 = len(sim.client.sta.frames)
        self.assertEqual(n1, 1)                        # 起手只有 1 条能过（突发 1）
        self.assertGreater(sim.client.self_held, 0)     # 其余被**延后**（计数 + 留痕）

        def refilled():
            sim.cycle()                                # 每拍重试；桶回补后应当发得出去
            return len(sim.client.sta.frames) > n1

        # 超时 1.0 s：回补只需 50 ms；这里等的是**真实时间**（限流器的桶用 monotonic，
        # 注入的假时钟管不到它）—— 故不要写大（旧值 2.0 白等）。
        self.assertTrue(waiting.wait_until(refilled, timeout=1.0, interval=0.02),
                        "延后的条本应在桶回补后发出去（延后 ≠ 丢弃）")


class TestDownlink(unittest.TestCase):
    """下行链路：真帧（`orpah_proto` 构）经 `ClientHost` 的读线程 → 设备记录到状态。"""

    def _feed(self, sim, msg):
        sim.client.sta.down.append(op.build_eth_frame(op.encode_msg(msg),
                                                      src_mac=b"\xAA" * 6))
        sim.client.on_recv = sim.on_down
        t = __import__("threading").Thread(target=sim.client._rx_loop, daemon=True)
        t.start()
        ok = waiting.wait_until(lambda: sim.down_count >= 1, timeout=3.0, interval=0.01)
        sim.client._stop.set()
        t.join(timeout=1.0)
        sim.client._stop.clear()
        self.assertTrue(ok, "下行报文本应在 3s 内到达（读线程未处理）")

    def test_access_info_marks_tracked(self):
        sim = make_sim()
        self._feed(sim, op.build_access_info(SN, tracked=True))
        self.assertTrue(sim.snapshot()["tracked"])
        self.assertEqual(sim.down_count, 1)

    def test_tracking_status_and_error_recorded(self):
        sim = make_sim()
        self._feed(sim, op.build_tracking_status(SN, "TRACKED"))
        self.assertEqual(sim.snapshot()["last_status"], "TRACKED")
        self._feed(sim, op.build_error("bad_sn", sn=SN))
        self.assertEqual(sim.snapshot()["last_error"], "bad_sn")

    def test_unknown_message_does_not_crash(self):
        sim = make_sim()
        self._feed(sim, op.build_found(SN))               # 发现上报（不是给 Client 的）
        self.assertEqual(sim.down_count, 1)               # 记下、不崩、不改设备行为


class TestConnectAndKeystore(unittest.TestCase):
    def test_connect_failure_is_visible(self):
        sim = make_sim()
        sim.client.sta.connect_ok = False
        self.assertFalse(cs.connect(sim, retries=1))

    def test_connect_wires_downlink_handler(self):
        sim = make_sim()
        self.assertTrue(cs.connect(sim, retries=1))
        self.assertEqual(sim.client.on_recv, sim.on_down)

    def test_register_keystore_writes_row(self):
        # Windows：SQLite 连接不放会锁住文件 → 临时目录清理报 PermissionError
        # （实测踩过）。故：`ignore_cleanup_errors` 兼底 + **try/finally 里先关连接**
        # （断言失败时也要关，否则清理会因为漏关而失败）。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = os.path.join(d, "ks.db")
            rec = cs.register_keystore(path, SN)
            keys = rec.get("keys") or []
            self.assertEqual([k["gen"] for k in keys], [1], rec)
            self.assertEqual(keys[0]["state"], oid.KEY_ACTIVE)
            self.assertTrue(keys[0]["model"].startswith("CH32V203"), keys[0])
            ks = ksdb.KeyStoreDB(path)                # 重新开库读回（不是只看返回值）
            try:
                act = ks.active_of(SN)
                self.assertEqual(act["model"], "CH32V203+TX-AH+ATECC608B")
                # 登记的必须是**演示派生**密钥（与仿真器真发出去的那把一致）
                self.assertEqual(act["pubkey"], oid.Device(sn=SN, demo_key=True).pubkey)
            finally:
                ks.db.close()


class TestSingleSourceGuard(unittest.TestCase):
    """★源码守卫：仿真器**不许**重写协议（本仓最容易犯、且不会报错的错）。"""

    def setUp(self):
        with open(os.path.join(HERE, "client_sim.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_no_protocol_literals(self):
        """帧/报文字面量、CRC、JCS 都不许出现在仿真器里（要来自各自的单一源）。"""
        bad = [t for t in ('0xAA', "0xaa", "crc8", "jcs(", "b64url_encode",
                           '"typ"', "'typ'", '"alg"', "ORPAH-", "signature")
               if t in self.src]
        self.assertFalse(bad, f"client_sim.py 里出现了协议细节字面量：{bad}")

    def test_uses_single_sources(self):
        for need in ("en.NORMAL_INTERVAL_S", "oid.LEVEL_MODES",
                     "en.plan", "ClientHost", "Device("):
            self.assertIn(need, self.src, f"仿真器必须走单一源：{need}")

    def test_no_hardcoded_normal_interval(self):
        """周期不许写死 60 —— 只能引用 `energy.NORMAL_INTERVAL_S`（改一处通全链）。"""
        self.assertNotIn("= 60.0", self.src)
        self.assertNotIn("default=60", self.src)
        self.assertIn("en.NORMAL_INTERVAL_S if every is None", self.src)

    def test_defaults_come_from_energy_constants(self):
        """能量默认值（初值/储能）也不许抄一份。"""
        self.assertIn("en.CHARGE0_MJ if charge_mj is None", self.src)
        self.assertIn("en.STORE_MJ if store_mj is None", self.src)


class TestSerialTransport(unittest.TestCase):
    """真板（步骤 b）的 **UART/AT 数据面**：`AT+TXDATA` 上行 + `FRAME:RX` 下行。

    这些是"真机契约"级别的断言（等号形式、长度含 14B 以太头、**等 OK 才发裸帧**、粘包）——
    它们错了不会有异常，只会表现为**上机后链路静默失效**，所以离线就要钉死。
    假串口只扮演"线"，帧本身用 `orpah_proto.build_eth_frame` 真构造。
    """

    def _bus(self, chunks=None, auto_ok=True, sysdbg=None):
        fake = _FakeSerial(chunks=chunks, auto_ok=auto_ok)
        bus = hs.SerialAtBus("COMTEST", 115200, sysdbg=sysdbg,
                             serial_factory=lambda p, b: fake, log=None)
        self.assertTrue(bus.connect(retries=1))
        return bus, fake

    def test_open_failure_is_reported(self):
        def bad_factory(p, b):
            raise OSError("no such port")
        bus = hs.SerialAtBus("COMX", serial_factory=bad_factory, log=None)
        self.assertFalse(bus.connect(retries=1, interval=0.0))

    def test_send_frame_wire_format(self):
        """★真机契约：`AT+TXDATA=<len>`（长度**含** 14B 以太头）→ OK → **裸以太帧**。"""
        bus, fake = self._bus()
        eth = op.build_eth_frame(b"{}", src_mac=b"\x4A\x06\x59\x00\x00\x01")
        self.assertTrue(bus.send_frame(eth))
        self.assertEqual(fake.written, f"AT+TXDATA={len(eth)}\r\n".encode() + eth)
        self.assertEqual((bus.tx_frames, bus.tx_fail), (1, 0))

    def test_no_ok_means_no_raw_bytes(self):
        """★没有 OK 就**不许**写裸帧：否则模块会把帧当 AT 命令解析（整条链路静默失效）。"""
        bus, fake = self._bus(auto_ok=False)
        bus.cmd_timeout = 0.2
        eth = op.build_eth_frame(b"{}", src_mac=b"\x4A\x06\x59\x00\x00\x01")
        self.assertFalse(bus.send_frame(eth))
        self.assertNotIn(eth, fake.written)               # 裸帧一个字节都没写
        self.assertIn(b"AT+TXDATA=", fake.written)        # 只发了命令
        self.assertEqual((bus.tx_frames, bus.tx_fail), (0, 1))

    def test_short_frame_rejected(self):
        bus, fake = self._bus()
        self.assertFalse(bus.send_frame(b"\x01\x02\x03"))
        self.assertEqual(bus.tx_fail, 1)
        self.assertEqual(fake.written, b"")

    def test_recv_frame_from_frame_rx_line(self):
        eth = op.build_eth_frame(b'{"type":"ORPAH-ACCESS-INFO"}',
                                 src_mac=b"\xAA" * 6)
        line = b"FRAME:RX " + eth.hex().encode() + b"\r\n"
        bus, _ = self._bus(chunks=[line])
        got = bus.recv_frame(timeout=2.0)
        self.assertEqual(got, eth)                        # 收回来的是**同一条**以太网帧
        self.assertEqual(bus.rx_frames_n, 1)

    def test_frame_rx_split_across_reads(self):
        """`FRAME:RX` 行被切成两段到达也要拼得出来（串口读是任意切分的）。"""
        eth = op.build_eth_frame(b'{"x":1}', src_mac=b"\xAA" * 6)
        head = b"FRAME:RX " + eth.hex().encode()
        bus, _ = self._bus(chunks=[head[:20], head[20:] + b"\r\n"])
        self.assertEqual(bus.recv_frame(timeout=2.0), eth)

    def test_junk_and_tx_echo_are_not_data(self):
        eth = op.build_eth_frame(b'{"x":1}', src_mac=b"\xAA" * 6)
        bus, _ = self._bus(chunks=[b"garbage line\r\n",
                                   b"FRAME:TX " + eth.hex().encode() + b"\r\n",
                                   b"FRAME:RX 0011223344\r\n"])   # 太短：不是以太帧
        self.assertIsNone(bus.recv_frame(timeout=0.3))
        self.assertEqual(bus.rx_frames_n, 0)
        self.assertEqual(bus.bad_lines, 1)                # 只有那条短帧算坏行（TX 回显不算）
        self.assertTrue(any("garbage" in ln for ln in bus.recent_lines()))  # 原样留着排查

    def test_stats_and_dump(self):
        bus, _ = self._bus(chunks=[b"AT+VERSION=2.4.1.5\r\n"])
        time.sleep(0.05)
        st = bus.stats()
        self.assertEqual(st["port"], "COMTEST")
        self.assertEqual([st[k] for k in ("tx_frames", "tx_fail", "rx_frames", "bad_lines")],
                         [0, 0, 0, 0])
        self.assertTrue(any("AT+VERSION" in ln for ln in bus.recent_lines(5)))

    def test_connect_sends_sysdbg_when_enabled(self):
        bus, fake = self._bus(sysdbg="WNB,1")
        self.assertIn(b"AT+SYSDBG=WNB,1\r\n", fake.written)   # 打开帧打印 → 下行才有得解析
        bus2, fake2 = self._bus(sysdbg=None)
        self.assertNotIn(b"SYSDBG", fake2.written)


if __name__ == "__main__":
    unittest.main(verbosity=2)
