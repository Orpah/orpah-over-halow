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
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import orpah_proto as op
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


def make_rt(server_host="127.0.0.1", server_port=SERVER[1]):
    rt = RouterBridge(ap_port=59999, server_host=server_host,
                      server_port=server_port)
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


if __name__ == "__main__":
    import io
    buf = io.StringIO()
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    res = unittest.TextTestRunner(stream=buf, verbosity=2).run(suite)
    print(buf.getvalue())
    bad = len(res.failures) + len(res.errors)
    print(f"Router 下行路径：{res.testsRun - bad}/{res.testsRun} 通过")
    sys.exit(1 if bad else 0)
