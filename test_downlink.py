#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_downlink.py — 下行真实性（Server → Router 签名）离线自检 · F-14 B 方案

为什么单独一个套件：`downlink.py` 只有两个函数，但它们守的是**这个 demo 里最实的一条攻击路径** ——
一张伪造的（或**重放的**）空 LOST-TABLE 能整体替换 Router 的走失缓存并置“已同步”，
此后这台 Router 不再拉表、命中也不答 TRACKED、**不再产生 ORPAH-FOUND**。
错法都很安静：签名算错（预像少盖一个字段 → 改了字段也验得过）、时间窗写反（旧报文能过）、
`(dts, dn)` 比较写反（重放能过）—— 都不会报错，只会“看起来在工作”。

盯四件事：
  ① **预像盖住整条报文**：改 entries/rid/status/sn/**新增字段** 都必须验不过；
  ② **新鲜性**：超窗拒、重放拒、同秒多条（dn 递增）要能过、Server 重启（dts 更大、dn 归零）要能过；
  ③ **两条边界照实**：Router 没配公钥 → `no_key`（调用方决定怎么办，不是本模块偷偷放行）；
     Server 没配私钥 → 原样返回（未签名下发）；
  ④ **不抛异常**：畸形输入（缺字段/类型不对/乱塞字符串）一律给错误码，绝不让 Router 崩。

不碰网络、不碰真钥（临时生成），毫秒级。
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 断言默认窗口（300s），先清掉环境里可能设过的值
for _k in ("ORPAH_DOWN_WINDOW", "ORPAH_DOWN_PUB", "ORPAH_DOWN_KEY"):
    os.environ.pop(_k, None)

import downlink as dl                            # noqa: E402
import orpah_id as oid                           # noqa: E402
import orpah_proto as op                         # noqa: E402

FAILS = []
NOW = 1_800_000_000


def ck(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name +
          (("   " + str(extra)) if not cond and extra else ""))
    if not cond:
        FAILS.append(name)


priv, pub = dl.demo_pair()
priv2, pub2 = dl.demo_pair()


def lost_table(entries=None, rid=None, dts=None, dn=1):
    """一条签好名的 LOST-TABLE（默认空表 = 最阴的那种攻击载荷）。"""
    msg = op.build_lost_table(entries or [], rid=rid)
    return dl.sign(msg, priv, dn, dts=dts, now=NOW)


# ---- 1. 正常往返 -----------------------------------------------------------
m = lost_table([{"sn": "CN-WH01-9AF3C1D2", "tracked": True}])
v = dl.verify(m, pub, now=NOW)
ck("签名后能验过（ok=True，带回 (dts,dn)）",
   v["ok"] and v["err"] is None and v["last"] == (NOW, 1), v)
ck("签名是 base64url(64 字节 raw r||s)，与 Orpah ID 同一格式",
   len(oid.b64url_decode(m[dl.DSIG_FIELD])) == 64)
ck("签名不改变原报文（sign 返回新 dict）",
   dl.DSIG_FIELD not in op.build_lost_table([]))
ck("空表也能签能验（攻击载荷就是空表）", dl.verify(lost_table([]), pub, now=NOW)["ok"])

# ---- 2. 预像必须盖住整条报文 ----------------------------------------------
base = lost_table([{"sn": "CN-WH01-9AF3C1D2", "tracked": True}], rid="RID-1")
for field, bad in (
        ("entries", [{"sn": "CN-WH01-9AF3C1D2", "tracked": False}]),   # 把走失标志摸掉
        ("entries", []),                                                # 换成空表（最阴）
        ("rid", "RID-2"),
        ("type", "ORPAH-TRACKING-STATUS"),
        ("v", 999),
        ("extra", "新加的字段")):
    m2 = dict(base, **{field: bad})
    ck("改字段 %s=%r → bad_sig（预像盖住整条报文）" % (field, bad),
       dl.verify(m2, pub, now=NOW)["err"] == dl.ERR_BAD_SIG)
m2 = dict(base)
m2.pop("entries")
ck("删掉 entries → bad_sig（不是“少一个字段就跳过”）",
   dl.verify(m2, pub, now=NOW)["err"] == dl.ERR_BAD_SIG)
m2 = dict(base, dn=2)
ck("改 dn → bad_sig", dl.verify(m2, pub, now=NOW)["err"] == dl.ERR_BAD_SIG)
m2 = dict(base, dts=NOW + 1)
ck("改 dts → bad_sig", dl.verify(m2, pub, now=NOW)["err"] == dl.ERR_BAD_SIG)
ck("换一把钥签的报文 → bad_sig（公钥才对得上）",
   dl.verify(dl.sign(op.build_lost_table([]), priv2, 1, now=NOW), pub,
             now=NOW)["err"] == dl.ERR_BAD_SIG)
ck("拿别人的公钥验 → bad_sig",
   dl.verify(base, pub2, now=NOW)["err"] == dl.ERR_BAD_SIG)

# ---- 3. 新鲜性：超窗 / 重放 / 同秒多条 / 重启 ------------------------------
ck("超窗（600s 前，窗口 300）→ stale",
   dl.verify(lost_table(dts=NOW - 600), pub, now=NOW)["err"] == dl.ERR_STALE)
ck("未来方向超窗也算 stale（时钟跑偏一样拒）",
   dl.verify(lost_table(dts=NOW + 600), pub, now=NOW)["err"] == dl.ERR_STALE)
ck("正好卡在窗口边界（300s）→ 通过（严格大于才拒）",
   dl.verify(lost_table(dts=NOW - 300), pub, now=NOW)["ok"])
ck("窗口可注入（window=10 时 11s 前就 stale）",
   dl.verify(lost_table(dts=NOW - 11), pub, now=NOW, window=10)["err"] == dl.ERR_STALE)
r1 = dl.verify(base, pub, now=NOW)
ck("第一次通过时返回 last=(dts,dn)（调用方存回去）", r1["ok"] and r1["last"] == (NOW, 1))
ck("★ 原样重放同一条 → replay",
   dl.verify(base, pub, now=NOW, last=r1["last"])["err"] == dl.ERR_REPLAY)
ck("★ 重放更旧的一条（dts 更小）→ replay",
   dl.verify(lost_table(dts=NOW - 5, dn=99), pub, now=NOW,
             last=r1["last"])["err"] == dl.ERR_REPLAY)
ck("同一秒内的下一条（dn 递增）→ 通过（同秒多条是常态：回执很密）",
   dl.verify(lost_table(dts=NOW, dn=2), pub, now=NOW, last=r1["last"])["ok"])
ck("Server 重启：dn 归零但 dts 更大 → 通过（不卡死）",
   dl.verify(lost_table(dts=NOW + 2, dn=1), pub, now=NOW + 2, last=r1["last"])["ok"])

# ---- 4. 畸形输入：一律给错误码，不抛 --------------------------------------
ck("没有签名 → no_sig", dl.verify(op.build_lost_table([]), pub,
                                   now=NOW)["err"] == dl.ERR_NO_SIG)
ck("签名是空串 → no_sig",
   dl.verify(dict(base, dsig=""), pub, now=NOW)["err"] == dl.ERR_NO_SIG)
ck("签名不是字符串 → no_sig",
   dl.verify(dict(base, dsig=123), pub, now=NOW)["err"] == dl.ERR_NO_SIG)
ck("签名是垃圾字符串 → bad_sig（base64 解不出也不抛）",
   dl.verify(dict(base, dsig="!!!not-b64!!!"), pub, now=NOW)["err"] == dl.ERR_BAD_SIG)
for bad in (None, "1800000000", 1.5, True):
    ck("dts=%r → bad_ts（类型不对不猜）" % (bad,),
       dl.verify(dict(base, dts=bad), pub, now=NOW)["err"] == dl.ERR_BAD_TS)
for bad in (None, "1", 1.0, False):
    ck("dn=%r → bad_ts" % (bad,),
       dl.verify(dict(base, dn=bad), pub, now=NOW)["err"] == dl.ERR_BAD_TS)
ck("整条报文不是 dict 也不崩（给 bad_ts/no_sig 之类）",
   dl.verify({}, pub, now=NOW)["err"] in (dl.ERR_NO_SIG, dl.ERR_BAD_TS))

# ---- 5. 两条边界（照实，不偷偷放行）--------------------------------------
ck("★ Router 没配公钥 → no_key（**本模块不代替调用方决定**收不收）",
   dl.verify(base, None, now=NOW)["err"] == dl.ERR_NO_KEY
   and dl.verify(base, None, now=NOW)["ok"] is False)
ck("no_key 时 last 原样带回（调用方不必特判）",
   dl.verify(base, None, now=NOW, last=(5, 6))["last"] == (5, 6))
ck("Server 没配私钥 → sign 原样返回（未签名下发，调用方计数）",
   dl.DSIG_FIELD not in dl.sign(op.build_lost_table([]), None, 1))

# ---- 6. 密钥读写（demo 用文件；公钥落盘是为了跨进程传递）------------------
with tempfile.TemporaryDirectory() as d:
    pub_path = os.path.join(d, "sub", "pub.pem")
    dl.write_pub(pub, pub_path)                 # 父目录不存在 → 自己建
    ck("公钥写盘后能读回来，且验签照样通过",
       dl.load_pub(pub_path) is not None
       and dl.verify(base, dl.load_pub(pub_path), now=NOW)["ok"])
    key_path = os.path.join(d, "key.pem")
    with open(key_path, "w", encoding="ascii") as f:
        f.write(oid.privkey_to_pem(priv))
    priv_rt = dl.load_priv(key_path)
    ck("私钥写盘（PKCS#8）后能读回来，签出来的报文仍验得过",
       priv_rt is not None
       and dl.verify(dl.sign(op.build_lost_table([]), priv_rt, 1, now=NOW),
                     pub, now=NOW)["ok"])
    ck("路径不存在 / 空路径 → None 且**不抛**（Router 起不来比不校验更糟）",
       dl.load_pub(os.path.join(d, "nope.pem")) is None and dl.load_pub("") is None
       and dl.load_priv(os.path.join(d, "nope.pem")) is None)
    with open(os.path.join(d, "junk.pem"), "w", encoding="ascii") as f:
        f.write("this is not a pem file")
    ck("PEM 是垃圾 → None 不抛", dl.load_pub(os.path.join(d, "junk.pem")) is None)

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), "; ".join(FAILS)))
    raise SystemExit(1)
print("all downlink tests passed")
