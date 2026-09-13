#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""downlink.py — 下行真实性（Server → Router 的三个**未签名**报文）· F-14 B 方案（2026-09-13）

**为什么需要**（A 方案挡不住什么）：`router._from_server()` 只比 **源 IP + 源端口** ——
同源伪造（本机任何进程、NAT 后面的任何主机、被改装的那台路由器自己）照样能送一张
**伪造的空走失表**进来：`_apply_lost_table` 会整体替换走失缓存并置 `_synced=True`，
此后这台 Router 不再拉表、命中也不答 TRACKED、**不再产生 ORPAH-FOUND**。
一句话：**一条伪造的下行就能“弄瞎”一台 Router**（最阴的版本：只把某个 SN 摸掉，其他一切正常）。

**做法：Server 用私钥签名，Router 只持公钥**（而**不是** HMAC 共享密钥）。理由是威胁模型本身：
本项目的 Router 被假定为**可被改装/不可信**（SPEC §8 威胁 4、原则 P-1）——
共享密钥一旦放进 Router，**改一台 Router 的人就拿到了给别的 Router 发假表的密钥**；
公钥不是秘密，改装一台 Router 学不到伪造能力。代价是 Router 侧每次下行多做一次 ECDSA 验签
（下行条数很少：拉表应答、回执、错误，可接受）。

**签名覆盖什么**：`JCS(整条报文去掉 dsig)` —— entries / rid / status / sn / dts / dn 全在预像里，
改任何一个字段都验不过（与 Orpah ID 的 JCS 预像同一套做法、同一套原语，见 `orpah_id.es256_*`）。

**新鲜性（必须做，否则等于没修）**：只签名不防重放的话，**重放一张旧的空表**照样弄瞎 Router。
故报文带 `dts`（Server 签名时刻，epoch 秒）与 `dn`（Server 侧**单调计数**），Router 要求：
  · `|now − dts| ≤ window`（默认 300 s，与 ID 上报的时间窗一个尺度）；
  · `(dts, dn)` **字典序大于**上次已接受的那一对（**按报文类型各记一份**）。
重放旧报文被拒；Server 重启计数归零也不会卡死（新报文的 dts 更大即可通过）。

**边界（如实写在文档与页面上，不得写成“下行可信了”）**：
  · 只管 **Server → Router** 这一段。**空口那一段（Router → Client）仍然无认证** ——
    低功耗客户端验不了签名，而空口开放正是本项目的前提；能往空口丢帧的人照样能骗客户端。
  · Router **未配公钥**时无法校验：这时**照旧收下**（保持老行为、不让 demo 起不来），
    但**计数并在页面/日志上显示「下行签名：未配置公钥」**（`down_unverified`），不假装已启用。
  · 有公钥时**一律 fail-closed**：没签名 / 签名错 / 过期 / 重放 → 拒 + 计数 + 留痕。
  · 私钥的**保管**不在本模块：demo 是一个 PEM 文件（`ORPAH_DOWN_KEY`），真机要硬件信任根/权限隔离。
  · 这套签名**不防“最后一跳的破坏”**：能长期在线路上做中间人的人仍可丢弃/延迟合法下行（可用性攻击），
    也能重放**更新**的那一条（窗口内）—— 要挡这些得靠 TLS/持久会话，不在本方案范围。

    ORPAH_DOWN_WINDOW   dts 允许的时间偏差（秒，默认 300）
    ORPAH_DOWN_PUB      Router 侧读公钥 PEM 的路径（缺省不配 → 不校验，只计数）
    ORPAH_DOWN_KEY      Server 侧读私钥 PEM 的路径（缺省生成一对临时钥，见 ui_server）
"""
import os
import time

import orpah_id as oid

DTS_FIELD = "dts"       # Server 签名时刻（epoch 秒）
DN_FIELD = "dn"         # Server 侧单调计数（同一进程内递增）
DSIG_FIELD = "dsig"     # base64url(ES256 raw r||s)

# 错误码（机器可读；页面/审计/测试都按它分类，**不拼文案**）
ERR_NO_KEY = "no_key"           # Router 侧没配公钥（无法校验）
ERR_NO_SIG = "no_sig"           # 报文压根没带签名
ERR_BAD_SIG = "bad_sig"         # 签名错（密钥不对/字段被改）
ERR_BAD_TS = "bad_ts"           # dts/dn 缺失或类型不对
ERR_STALE = "stale"             # 超出时间窗
ERR_REPLAY = "replay"           # (dts, dn) 不比上次新（旧报文重放）


def _env_int(name, default):
    try:
        return int(str(os.environ.get(name, "") or default).strip())
    except (TypeError, ValueError):
        return default


WINDOW_SEC = _env_int("ORPAH_DOWN_WINDOW", 300)


def preimage(msg):
    """签名预像 = JCS(报文去掉 dsig) —— 只排除签名自身，**其余字段全盖住**。"""
    return oid.jcs({k: v for k, v in msg.items() if k != DSIG_FIELD})


def sign(msg, privkey, dn, dts=None, now=None):
    """给一条下行报文签名 → **新 dict**（不改原对象）。

    privkey=None 时返回原样（未启用签名，调用方会记成“未签名下发”）。
    """
    if privkey is None:
        return dict(msg)
    out = dict(msg)
    out[DTS_FIELD] = int(dts if dts is not None else
                        (now if now is not None else time.time()))
    out[DN_FIELD] = int(dn)
    out[DSIG_FIELD] = oid.b64url_encode(oid.es256_sign(privkey, preimage(out)))
    return out


def verify(msg, pubkey, now=None, last=None, window=None):
    """验一条下行报文 → `{"ok": bool, "err": code|None, "last": (dts, dn)|None}`。

    `pubkey=None`（Router 没配公钥）→ `ok=False, err="no_key"`，由**调用方**决定怎么办
    （Router 现在的选择是：照旧收下但计数 `down_unverified`，见模块头「边界」）。
    `last` = 上一次已接受的 `(dts, dn)`（同类型）；返回的 `last` 供调用方存回去。
    """
    if pubkey is None:
        return {"ok": False, "err": ERR_NO_KEY, "last": last}
    sig = msg.get(DSIG_FIELD)
    if not isinstance(sig, str) or not sig:
        return {"ok": False, "err": ERR_NO_SIG, "last": last}
    dts, dn = msg.get(DTS_FIELD), msg.get(DN_FIELD)
    if not isinstance(dts, int) or isinstance(dts, bool) or \
            not isinstance(dn, int) or isinstance(dn, bool):
        return {"ok": False, "err": ERR_BAD_TS, "last": last}
    if not oid.es256_verify(pubkey, preimage(msg), sig):
        return {"ok": False, "err": ERR_BAD_SIG, "last": last}
    win = WINDOW_SEC if window is None else int(window)
    cur = int(now if now is not None else time.time())
    if abs(cur - dts) > win:
        return {"ok": False, "err": ERR_STALE, "last": last, "dts": dts, "dn": dn}
    if last is not None and (dts, dn) <= (int(last[0]), int(last[1])):
        # 不比上次新 → 旧报文重放（或乱序到得比更新的那条还晚）。宁可拒也不放行。
        return {"ok": False, "err": ERR_REPLAY, "last": last, "dts": dts, "dn": dn}
    return {"ok": True, "err": None, "last": (dts, dn), "dts": dts, "dn": dn}


# ---------------------------------------------------------------------------
# 密钥读取（demo 用文件；真机见模块头「边界」）
# ---------------------------------------------------------------------------
def load_pub(path=None):
    """读公钥 PEM → 公钥对象；路径为空/读不到 → None（**不抛**：Router 起不来比不校验更糟）。"""
    path = path or os.environ.get("ORPAH_DOWN_PUB") or ""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="ascii") as f:
            return oid.pubkey_from_pem(f.read())
    except Exception:
        return None


def load_priv(path=None):
    """读私钥 PEM → 私钥对象；路径为空/读不到 → None（Server 侧不签名）。"""
    path = path or os.environ.get("ORPAH_DOWN_KEY") or ""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="ascii") as f:
            return oid.privkey_from_pem(f.read())
    except Exception:
        return None


def write_pub(pubkey, path):
    """把公钥写到 `path`（父目录不存在就建）→ 返回写入的路径。

    为什么要落盘：Router/Server 在 demo 里常常是**两个进程**，公钥得有个共同的传递点；
    公钥不是秘密，写文件是安全的（**私钥不能这么干**，见模块头）。
    """
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="ascii") as f:
        f.write(oid.pubkey_to_pem(pubkey))
    return path


def default_pub_path():
    """默认公钥路径：`ORPAH_DOWN_PUB` 或系统临时目录下的一个固定名（两个进程默认能对上）。"""
    import tempfile
    return os.environ.get("ORPAH_DOWN_PUB") or os.path.join(
        tempfile.gettempdir(), "orpah-downlink-pub.pem")


def demo_pair():
    """演示用：造一对下行密钥 → `(privkey, pubkey)`（不落盘，调用方自己决定写哪儿）。"""
    priv = oid.gen_privkey()
    return priv, priv.public_key()
