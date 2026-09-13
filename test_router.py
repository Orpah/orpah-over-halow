#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_router.py — RouterBridge 下行路径离线测试
=================================================
只锁**下行**（Server → Router 那条 UDP 路径），因为那条路径 2026-09-13 起加了一道
**来源校验**（用户选的 A 方案），而它守的是一件很实的事：

    下行报文（LOST-TABLE / TRACKING-STATUS / ERROR）**都没有签名**，
    一条伪造的 LOST-TABLE 会整体替换走失缓存并置 `_lost_event`（→ `_synced=True`）——
    此后这台 Router 不再主动拉表、命中也不答 TRACKED、**不再上报 ORPAH-FOUND**
    （最阴的版本：只把某个 SN 从 entries 里摸掉，其他一切正常）。

所以本文件盯三条：
  ① 不是从 Server 地址来的 → **在解析报文之前**就丢，且计数/留痕；
  ② 是从 Server 地址来的 → 照常处理（不能把正常下行也挡了）；
  ③ 来源校验**必须 fail-closed**，且不能替代真实性（同源也能伪造）—— 这条是认知，写在注释里。

不碰真 socket：手工塞假 UDP socket，直接调 `_udp_loop` 的单条处理路径（`_on_down_packet`）。
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import orpah_proto as op
import downlink
from router import RouterBridge

SERVER = ("127.0.0.1", 19447)


class _FakeSock:
    """替身 UDP socket：按队列喂包，记录 sendto。"""

    def __init__(self, packets=None):
        self.packets = list(packets or [])
        self.sent = []

    def recvfrom(self, n):
        if not self.packets:
            import socket as _s
            raise _s.timeout()
        return self.packets.pop(0)

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def settimeout(self, *a):
        pass

    def setsockopt(self, *a):
        pass

    def close(self):
        pass


def make_rt(server_host="127.0.0.1", server_port=SERVER[1], down_pub=None):
    rt = RouterBridge(ap_port=59999, server_host=server_host,
                      server_port=server_port, down_pub=down_pub)
    rt.udp = _FakeSock()
    return rt


def lost_table_msg(entries=None, rid=None):
    return op.build_lost_table(entries or [], rid=rid)


def wire(msg):
    """报文 → 线上字节（`_handle_down` 吃的是 recvfrom 拿到的 bytes）。"""
    return op.encode_msg(msg)


class TestDownlinkSourceCheck(unittest.TestCase):

    def test_forged_lost_table_from_other_host_is_dropped(self):
        """★ 核心用例：别的源塞来的 LOST-TABLE 不能改走失缓存（A 方案的全部意义）。"""
        rt = make_rt()
        rt._apply_lost_table(lost_table_msg([{"sn": "CN-WH01-9AF3C1D2", "tracked": True}]))
        before = dict(rt.lost_cache)
        rt._handle_down(wire(lost_table_msg([], rid=None)), ("10.0.0.9", 19447))
        self.assertEqual(rt.lost_cache, before, "伪造的表改动了走失缓存")
        self.assertEqual(rt.down_rejected, 1)
        self.assertEqual(rt.down_rejects[0]["from"], "10.0.0.9:19447")

    def test_forged_lost_table_cannot_mark_as_synced(self):
        """伪造的表也不能让 Router 以为“已同步”（那会让它此后不再拉表）。"""
        rt = make_rt()
        self.assertFalse(rt._synced)
        rt._handle_down(wire(lost_table_msg([])), ("10.0.0.9", 19447))
        self.assertFalse(rt._synced)
        self.assertFalse(rt._lost_event.is_set())
        self.assertEqual(rt.lost_push_recv, 0)

    def test_right_port_wrong_ip_is_dropped(self):
        """端口对、IP 不对也不行（伪造者很容易凑端口号）。"""
        rt = make_rt()
        rt._handle_down(wire(lost_table_msg([])), ("127.0.0.2", SERVER[1]))
        self.assertEqual(rt.down_rejected, 1)
        self.assertEqual(rt.lost_push_recv, 0)

    def test_right_ip_wrong_port_is_dropped(self):
        """IP 对、端口不对也不行（Server 就是 bind 在这个端口回包的）。"""
        rt = make_rt()
        rt._handle_down(wire(lost_table_msg([])), (SERVER[0], 19448))
        self.assertEqual(rt.down_rejected, 1)
        self.assertEqual(rt.lost_push_recv, 0)

    def test_from_server_is_accepted(self):
        """从 Server 地址来的照常处理（别把正常下行也挡了）。"""
        rt = make_rt()
        rt._handle_down(wire(lost_table_msg([{"sn": "CN-WH01-9AF3C1D2", "tracked": True}])),
                        SERVER)
        self.assertEqual(rt.down_rejected, 0)
        self.assertTrue(rt.lost_cache.get("CN-WH01-9AF3C1D2", {}).get("tracked"))
        self.assertEqual(rt.lost_push_recv, 1)      # 无 rid → 算“服务器主动推送”

    def test_hostname_server_host_is_resolved(self):
        """`server_host` 写主机名时先解析一次（localhost → 127.0.0.1），否则正常下行会被误丢。"""
        rt = make_rt(server_host="localhost")
        self.assertEqual(rt._server_ip, "127.0.0.1")
        rt._handle_down(wire(lost_table_msg([])), SERVER)
        self.assertEqual(rt.down_rejected, 0)

    def test_tracking_status_from_other_host_is_not_forwarded(self):
        """回执（TRACKING-STATUS）也一样：来源不对就不往空口里注入。

        注意断言要落在**空口注入**上：`_down()` 先 `ap.send_frame()`，成功后才回调 `on_down`
        —— 只断言 `on_down` 在“未连 AP”时会假过（send_frame 返回 False 就 return 了）。
        """
        rt = make_rt()
        frames = []
        rt.ap.send_frame = lambda eth: (frames.append(eth), len(eth))[1]
        down = []
        rt.on_down = down.append
        status = op.build_tracking_status("CN-WH01-9AF3C1D2", op.ST_NOT_TRACKED)
        rt._handle_down(wire(status), ("10.0.0.9", SERVER[1]))
        self.assertEqual(frames, [], "来源不明的回执被注入回 Client 了")
        self.assertEqual(rt.down_rejected, 1)
        rt._handle_down(wire(status), SERVER)
        self.assertEqual(len(frames), 1)            # 真的从 Server 来 → 正常注入
        self.assertEqual(len(down), 1)

    def test_empty_addr_does_not_crash(self):
        rt = make_rt()
        rt._handle_down(wire(lost_table_msg([])), ())
        self.assertEqual(rt.down_rejected, 1)

    def test_rejects_are_capped(self):
        """留痕是个短环形（攻击者可以一直发 → 不能无界增长）。"""
        rt = make_rt()
        for i in range(50):
            rt._handle_down(wire(lost_table_msg([])), (f"10.0.0.{i}", SERVER[1]))
        self.assertEqual(rt.down_rejected, 50)
        self.assertLessEqual(len(rt.down_rejects), 10)


class TestDownlinkSignature(unittest.TestCase):
    """下行真实性（F-14 B 方案，2026-09-13）：**来源对了也要验签**。

    为什么必须与 A（来源校验）分开测：A 只挡“不是 Server 地址发来的”，而同源伪造
    （本机任何进程 / NAT 后面 / 被改装的那台 Router）在 A 眼里与真 Server **一模一样**。
    这里全部从 `SERVER` 地址喂包 —— 即在 A 眼里合法 —— 只看 B 能不能拦住。
    """

    def setUp(self):
        self.priv, self.pub = downlink.demo_pair()

    def signed(self, entries=None, dn=1, dts=None, rid=None):
        return wire(downlink.sign(lost_table_msg(entries or [], rid=rid),
                                  self.priv, dn, dts=dts))

    def test_unsigned_from_server_is_rejected(self):
        """★ 核心：配了公钥后，**没签名**的下行（哪怕源地址就是 Server）一律丢。"""
        rt = make_rt(down_pub=self.pub)
        rt._apply_lost_table(lost_table_msg([{"sn": "CN-WH01-9AF3C1D2", "tracked": True}]))
        before = dict(rt.lost_cache)
        rt._handle_down(wire(lost_table_msg([])), SERVER)
        self.assertEqual(rt.down_sig_fail, 1)
        self.assertEqual(rt.down_sig_fails[0]["err"], "no_sig")
        self.assertEqual(rt.lost_cache, before, "未签名的空表改动了走失缓存")
        self.assertFalse(rt._synced, "未签名的表把 Router 标成“已同步”了")
        self.assertEqual(rt.down_rejected, 0, "这条不是来源问题（A 认不出它）")

    def test_signed_from_server_is_accepted(self):
        rt = make_rt(down_pub=self.pub)
        rt._handle_down(self.signed([{"sn": "CN-WH01-9AF3C1D2", "tracked": True}]), SERVER)
        self.assertEqual(rt.down_sig_fail, 0)
        self.assertEqual(rt.down_verified, 1)
        self.assertTrue(rt.lost_cache.get("CN-WH01-9AF3C1D2", {}).get("tracked"))

    def test_tampered_entries_are_rejected(self):
        """签名后改内容（把走失标志摸掉）→ 必须拒（预像盖住整条报文）。"""
        rt = make_rt(down_pub=self.pub)
        msg = downlink.sign(lost_table_msg([{"sn": "A", "tracked": True}]),
                            self.priv, 1)
        msg["entries"] = []                      # 最阴的改法：换成空表
        rt._handle_down(wire(msg), SERVER)
        self.assertEqual(rt.down_sig_fails[0]["err"], "bad_sig")
        self.assertFalse(rt.lost_cache)

    def test_wrong_key_signature_is_rejected(self):
        rt = make_rt(down_pub=self.pub)
        other, _ = downlink.demo_pair()
        rt._handle_down(wire(downlink.sign(lost_table_msg([]), other, 1)), SERVER)
        self.assertEqual(rt.down_sig_fails[0]["err"], "bad_sig")

    def test_replay_is_rejected(self):
        """★ 只签名不防重放等于没修：重放一张**旧**的合法签名空表照样能弄瞎 Router。"""
        rt = make_rt(down_pub=self.pub)
        now = int(time.time())          # dts 必须在窗口内（否则先被判 stale，测不到重放）
        rt._handle_down(self.signed([{"sn": "A", "tracked": True}], dn=5, dts=now),
                        SERVER)
        self.assertEqual(rt.down_verified, 1)
        n = rt.down_sig_fail
        rt._handle_down(self.signed([], dn=5, dts=now), SERVER)       # 原样重放
        self.assertEqual(rt.down_sig_fail, n + 1)
        self.assertEqual(rt.down_sig_fails[0]["err"], "replay")
        self.assertTrue(rt.lost_cache.get("A", {}).get("tracked"), "重放把缓存改了")
        rt._handle_down(self.signed([], dn=999, dts=now - 5), SERVER)  # 更旧但 dn 更大
        self.assertEqual(rt.down_sig_fails[0]["err"], "replay")
        rt._handle_down(self.signed([], dn=6, dts=now), SERVER)       # 同秒下一条 → 放行
        self.assertEqual(rt.down_last[op.MSG_LOST_TABLE], (now, 6))

    def test_window_env_and_stale(self):
        """超窗（默认 300s）→ 拒；`window` 可注入（不动全局）。"""
        rt = make_rt(down_pub=self.pub)
        v = downlink.verify(downlink.sign(lost_table_msg([]), self.priv, 1,
                                          dts=1000), self.pub,
                            now=1000 + downlink.WINDOW_SEC + 1)
        self.assertEqual(v["err"], "stale")

    def test_no_pubkey_falls_back_but_counts(self):
        """★ 两条边界必须照实：没配公钥 → 无法校验 → **收下但计数**（不假装已启用）。

        不能默认拒收：真机忘了发公钥就把业务搞死（fail-closed 太硬）；也不能默默收下：
        那会让人以为这段路已经有真实性保护。计数 + 日志吵一次是这两者之间的折中。
        """
        rt = make_rt(down_pub=None)
        rt._apply_lost_table(lost_table_msg([{"sn": "A", "tracked": True}]))
        rt._handle_down(wire(lost_table_msg([])), SERVER)
        self.assertEqual(rt.down_unverified, 1)
        self.assertEqual(rt.down_sig_fail, 0)
        self.assertEqual(rt.lost_cache, {}, "未校验时仍应照旧生效（老行为）")
        self.assertEqual(rt.downlink_view()["on"], False)

    def test_source_check_still_runs_first(self):
        """来源不对的包应该记在 `down_rejected`（A），而不是 `down_sig_fail`（B）——
        两个计数分开才能回答“被哪一道拦下的”。"""
        rt = make_rt(down_pub=self.pub)
        rt._handle_down(self.signed([]), ("10.0.0.9", SERVER[1]))
        self.assertEqual(rt.down_rejected, 1)
        self.assertEqual(rt.down_sig_fail, 0)

    def test_view_reports_state_honestly(self):
        rt = make_rt(down_pub=self.pub)
        rt._handle_down(self.signed([]), SERVER)
        rt._handle_down(wire(lost_table_msg([])), SERVER)
        v = rt.downlink_view()
        self.assertTrue(v["on"] and v["verified"] == 1 and v["failed"] == 1)
        self.assertEqual(v["sig_fails"][0]["err"], "no_sig")


if __name__ == "__main__":
    import io
    buf = io.StringIO()
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    res = unittest.TextTestRunner(stream=buf, verbosity=2).run(suite)
    print(buf.getvalue())
    bad = len(res.failures) + len(res.errors)
    print(f"Router 下行路径：{res.testsRun - bad}/{res.testsRun} 通过")
    sys.exit(1 if bad else 0)
