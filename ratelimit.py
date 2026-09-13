#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ratelimit.py — 各环节限频（《Orpah ID 协议规范》§5.8 的参考实现）
==================================================================
规范只要求「最小间隔 + 令牌桶」，**参数属部署配置**（本文件用环境变量给默认值，
见文末 ENV 表）。这里实现两处（服务端两条 + 路由器一条，见下）。

**为什么必须做**（SPEC §8 威胁 1）：签名只滤掉「伪造」，滤不掉「**洪水**」——
合法设备（或其被改的固件）被高频刷量时，每条都要走一次 **ECDSA 验签**，不设限就是自伤。

**放置顺序（关键，别放错）**：限频在**验签之前** —— 它是为了省 CPU，不是为了判真假。
代价必须如实说：per-SN 桶的 key 来自 **尚未验签的 `payload.sn`**，
所以**轮换 SN 就能绕过 per-SN 桶** → per-SN 桶**不是**独立防线，
必须配 **per-router 桶**（key = UDP 源地址，要换源就得换线路/换设备）做兜底。
两者都过才放行；两条防线**都不**声称「防住了攻击」—— 只把「单点被刷」的代价抬高。

**故意不限频的**：`ORPAH-FOUND`（发现走失）**不设限** —— 它是业务关键事件
（漏掉一条 = 一个人没被找到），频率天然低，且"某台 Router 高频刷 FOUND"属于
**Router 身份**问题（F-12 一类），不是限频能解决的；宁可多收不可漏收。

**等待纪律**：本模块**不睡眠、不轮询** —— `allow(now)` 是 `now` 的纯函数
（`now` 缺省取 `time.monotonic()`），测试传假时钟即可确定性复现。
调用方若要「等到可以发」，用 `waiting.wait_until(...)`（见仓库规则）。

用法：
    from ratelimit import RateLimiter
    rl = RateLimiter.from_env()            # 参数取自环境变量（下表）
    dec = rl.check("CN-WH01-9AF3C1D2", "127.0.0.1")   # → Decision(ok, which, retry_after)
    rl.snapshot()                          # /api/status 用（只给计数与参数，不给全表）

ENV（全都可选；非法值一律回退默认，不抛异常 —— 起不来比限错了更糟）：
    ORPAH_RL_ENABLE         1/0   是否启用（默认 1）
    ORPAH_RL_SN_RATE        浮点  单 SN 令牌补充速率（个/秒，默认 2.0）
    ORPAH_RL_SN_BURST       整数  单 SN 桶容量（默认 20）
    ORPAH_RL_ROUTER_RATE    浮点  单 Router（源地址）补充速率（个/秒，默认 20.0）
    ORPAH_RL_ROUTER_BURST   整数  单 Router 桶容量（默认 60）
    ORPAH_RL_MAX_KEYS       整数  桶表的 key 上限（默认 4096，超出淘汰最久未用）

  Router 侧（§5.8 的两行，`ORPAH_RLR_*`，同一套后缀）：
    ORPAH_RLR_ENABLE / _SN_RATE（默认 5.0）/ _SN_BURST（40）/ _ROUTER_RATE（1.0，按源 MAC）
    / _ROUTER_BURST（5）/ _MAX_KEYS。
  **两边默认值不同是有意的**：Router 侧限带宽、Server 侧限 CPU（详见下面 `RTR_*` 注释）。

  设备侧（`DeviceLimiter`，**自愿**自限频，第三套前缀 `ORPAH_SELF_*`，2026-09-13）：
    ORPAH_SELF_ENABLE（默认 1）/ _MIN_INTERVAL（默认 0.6 s）/ _BURST（默认 4）。
    **它不是防线** —— 一台被改过的设备不会做（协议也要求不了它）；只让守规矩的设备
    别占满空口、别白花电、别撞上游的桶。语义是**延后**不是丢弃（丢自己的报 = 漏报）。

默认值怎么来的（**演示/开发配置，不是实测标定**）：
  - 正常流量 = 每台设备 ~1 条/秒（`ui_server` 的 2s 会话周期 + 1s ID 周期）→
    `SN_RATE=2.0` 留 1 倍余量，长期 1 条/秒**永远不会被限**；
  - `SN_BURST=20` 是为了**不误伤演示**：页面「跑全部攻击」一次连发 13 条同类报文，
    burst 必须 > 13，否则攻击演示会被限频挡在验签之前，"被哪道防线拒"就错乱了。
  - `ROUTER_*` 比 SN 宽（一台 Router 后面可能有很多设备）→ 它只挡「单点刷量」。
  真机部署应按实测流量与 CPU 预算重标（SPEC §10 F-11 同一性质）。
"""
import os
import threading
import time
from collections import OrderedDict


def _env_int(name, default):
    """取整数环境变量；未设/空串/非法值 → 回退默认（与 alerts/tsdb 同语义）。"""
    try:
        return int(str(os.environ.get(name, "") or default).strip())
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    """取浮点环境变量；未设/空串/非法值 → 回退默认。"""
    try:
        return float(str(os.environ.get(name, "") or default).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name, default):
    """取布尔环境变量：'1/true/yes/on' 真，'0/false/no/off' 假，其余回退默认。"""
    v = str(os.environ.get(name, "") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


ENABLE = _env_bool("ORPAH_RL_ENABLE", True)
SN_RATE = _env_float("ORPAH_RL_SN_RATE", 2.0)
SN_BURST = _env_int("ORPAH_RL_SN_BURST", 20)
ROUTER_RATE = _env_float("ORPAH_RL_ROUTER_RATE", 20.0)
ROUTER_BURST = _env_int("ORPAH_RL_ROUTER_BURST", 60)
MAX_KEYS = _env_int("ORPAH_RL_MAX_KEYS", 4096)

# Router 侧（网口上行限频，§5.8 的两行）默认值：**比 server 侧宽**，这是有意的 ——
#   · Router 侧限的是**带宽**（空口 + Router→Server 那段）；Server 侧限的是 **CPU**（ECDSA 验签）。
#     两边都做窄 → 报文死在 Router，页面/审计上就只看得到“转发侧丢弃”，
#     「到底哪道防线拦的」失去分辨力（本项存在的意义之一就是能说清这一点）。
#   · 所以 Router 侧允许得宽一点，让 Server 侧仍是“正常流量”的分水岭：
#       转发按 SN：5 条/秒、桶容量 40（server 侧 2/s、20）；
#       未签名的 REQ-CONNECT（相当于 probe）：按**源 MAC** 1 条/秒、桶容量 5。
RTR_SN_RATE, RTR_SN_BURST = 5.0, 40
RTR_MAC_RATE, RTR_MAC_BURST = 1.0, 5
RTR_PREFIX = "ORPAH_RLR_"               # Router 侧参数名前缀（与 Server 的 ORPAH_RL_ 分开）


class TokenBucket:
    """令牌桶（连续补充，无睡眠、无后台线程）。

    状态只有 (tokens, last) 两个数 —— **延迟计算**：每次访问按经过的时间补令牌，
    不补就不算，所以不需要定时器（也就没有"定时器线程崩了没人发现"这种失效模式）。
    """

    __slots__ = ("rate", "burst", "tokens", "last")

    def __init__(self, rate, burst, now=None):
        self.rate = float(rate)
        self.burst = float(max(1.0, burst))     # 容量至少 1，否则谁都过不去
        self.tokens = self.burst                # 起手满桶（否则冷启动会误伤第一条）
        self.last = time.monotonic() if now is None else float(now)

    def peek(self, now=None):
        """看现在能不能过（**不**扣令牌）→ (ok, retry_after)。"""
        now = time.monotonic() if now is None else float(now)
        if self.rate > 0:
            self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            return True, 0.0
        # 还要等多久才攒够 1 个令牌（rate<=0 = 只许 burst 个，之后永不放行）
        wait = (1.0 - self.tokens) / self.rate if self.rate > 0 else float("inf")
        return False, wait

    def consume(self, now=None):
        """扣一个令牌（**只在 peek 通过后调用**）→ 剩余令牌数。"""
        now = time.monotonic() if now is None else float(now)
        self.tokens = max(0.0, self.tokens - 1.0)
        self.last = now
        return self.tokens

    def snapshot(self, now=None):
        ok, wait = self.peek(now)
        return {"rate": self.rate, "burst": self.burst,
                "tokens": round(self.tokens, 2), "allow": ok,
                "retry_after": None if wait == float("inf") else round(wait, 3)}


class _Buckets:
    """key → TokenBucket 的表（带 key 上限，超出淘汰**最久未用**的，O(1)）。

    key 来自报文内容（sn / 源地址），是**不可信输入** → 必须有上限，
    否则"每个 SN 一个桶"就等于给攻击者一个内存放大器（§8 威胁 1 的一种死法）。

    为什么用 `OrderedDict` + `move_to_end` 而不是“扫描找 `last` 最小”：
    后者在表满时**每次新 key 都是 O(n)** —— 攻击者轮换 SN 时每个报文都要扫一遍全网，
    限频层自己变成 CPU 放大器（与它存在的目的相反）。**实测**
    （max_keys=4096，连发 20000 个不同 SN）：扫描式 **219 µs/条**、
    `OrderedDict` **0.6 µs/条**（快 368×），而无淘汰的普通路径才 5.2 µs/条 ——
    即扫描式比正常路径还慢 **42×**。
    """

    def __init__(self, rate, burst, max_keys=MAX_KEYS, name="sn"):
        self.rate = rate
        self.burst = burst
        self.max_keys = max(16, int(max_keys))
        self.name = name
        self.buckets = OrderedDict()
        self._lock = threading.Lock()
        self.evicted = 0                       # 淘汰次数（可观测：不该频繁涨）
        self.created = 0                       # 建桶次数

    def _get(self, key, now):                  # 调用方持锁
        b = self.buckets.get(key)
        if b is None:
            if len(self.buckets) >= self.max_keys:
                # 淘汰最久未用（OrderedDict 的头部）—— O(1)，不用扫描
                self.buckets.popitem(last=False)
                self.evicted += 1
            b = TokenBucket(self.rate, self.burst, now=now)
            self.buckets[key] = b
            self.created += 1
        else:
            self.buckets.move_to_end(key)      # 标为刚用过（LRU 顺序）—— O(1)
        return b

    def peek(self, key, now=None):
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            return self._get(key, now).peek(now)

    def consume(self, key, now=None):
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            return self._get(key, now).consume(now)

    def state(self, now=None):
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            return {"keys": len(self.buckets), "max_keys": self.max_keys,
                    "created": self.created, "evicted": self.evicted,
                    "rate": self.rate, "burst": self.burst}

    def tokens(self, key, now=None):
        """某 key 现在剩几个令牌（**只读观测**：演示/页面前置判断“桶回补够了没”）。

        `peek()` 会顺带把令牌补到当前时刻（延迟补充），所以这个读数是最新的。
        为什么需要它：讲“守规矩的设备不撞上游的桶”时，必须**先确认桶里有额度** ——
        否则上一轮刷量刚把桶刷空，测出来的就是“桶还没回补”，而不是设备侧重不要限。
        """
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            b = self._get(key, now)
            b.peek(now)
            return round(b.tokens, 2)

    def top(self, n=3, now=None):
        """令牌最少的几台（"被刷得最狠"的线索；只给线索，不指认谁在攻击）。"""
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            rows = [(k, b.snapshot(now)) for k, b in self.buckets.items()]
        rows.sort(key=lambda kv: kv[1]["tokens"])   # 这里只能 O(n)——它不在报文热路径上
        return [{"key": k, "tokens": v["tokens"], "allow": v["allow"]}
                for k, v in rows[:n]]


class Decision:
    """一次限频判定：ok=False 时 `which` 明说**是哪条防线**（sn / router）。"""

    __slots__ = ("ok", "which", "retry_after")

    def __init__(self, ok, which=None, retry_after=0.0):
        self.ok = ok
        self.which = which                 # None / "sn" / "router"
        self.retry_after = retry_after

    def __bool__(self):
        return bool(self.ok)

    def as_dict(self):
        return {"ok": self.ok, "which": self.which,
                "retry_after": round(self.retry_after, 3)}

    def __repr__(self):
        return (f"Decision(ok={self.ok}, which={self.which!r}, "
                f"retry_after={self.retry_after:.3f})")


class RateLimiter:
    """两条防线：per-SN（可能有误伤/可被轮换绕过）+ per-Router（源地址兜底）。

    **先 peek 两条、都通过才各扣一个令牌**：否则「SN 通过但 Router 拒绝」会白扣 SN 令牌
    （被拒的请求本不该消耗预算），把好设备的额度无谓吃掉。

    **判定是原子的**（`_lock` 包住 peek+consume）：不然两个线程可以同时看到“还有 1 个令牌”
    然后都放行 → **超量放行**（只存在于高并发下，最难查的那种）。另：`_Buckets` 内部的锁
    只管它自己的表；两层没有嵌套获取不同锁的路径，不会死锁。
    """

    def __init__(self, enabled=ENABLE, sn_rate=SN_RATE, sn_burst=SN_BURST,
                 router_rate=ROUTER_RATE, router_burst=ROUTER_BURST,
                 max_keys=MAX_KEYS):
        self.enabled = bool(enabled)
        self.sn = _Buckets(sn_rate, sn_burst, max_keys, name="sn")
        self.router = _Buckets(router_rate, router_burst, max_keys, name="router")
        self.allowed = 0
        self.dropped_sn = 0
        self.dropped_router = 0
        self.last_drop = None              # 最近一次丢弃的时刻（monotonic）/ 记录
        self.since = time.time()           # 统计起点（epoch 秒，给人看）
        self._recent = []                  # 最近若干条丢弃（给 UI，短列表，不落库）
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, prefix="ORPAH_RL_", enable=ENABLE, sn_rate=SN_RATE,
                 sn_burst=SN_BURST, router_rate=ROUTER_RATE,
                 router_burst=ROUTER_BURST, max_keys=MAX_KEYS):
        """按环境变量构造（每次调用重读环境 → 测试可改 env 后重建）。

        `prefix` 让**两个侧**各用一套参数名（默认那套 = Server 侧 `ORPAH_RL_*`；
        Router 侧用 `ORPAH_RLR_*`，并把 `RTR_*` 值作为默认值传进来）——
        免得两边共用一个名字，改了 Server 的阈值把 Router 也一起改了。
        """
        return cls(enabled=_env_bool(prefix + "ENABLE", enable),
                   sn_rate=_env_float(prefix + "SN_RATE", sn_rate),
                   sn_burst=_env_int(prefix + "SN_BURST", sn_burst),
                   router_rate=_env_float(prefix + "ROUTER_RATE", router_rate),
                   router_burst=_env_int(prefix + "ROUTER_BURST", router_burst),
                   max_keys=_env_int(prefix + "MAX_KEYS", max_keys))

    def check(self, sn=None, router=None, now=None, kind=""):
        """判定一条入站报文是否放行。

        sn / router 都可以是 None（缺哪个就少判一条；两者都 None 时直接放行 ——
        没有可用的 key 就不该拿"没有任何依据"去拒人家的报文）。
        """
        if not self.enabled:
            return Decision(True)
        now = time.monotonic() if now is None else float(now)
        ks = str(sn) if sn else None
        kr = str(router) if router else None
        if ks is None and kr is None:
            return Decision(True)
        # 整段持锁：peek 两条 + 各扣一个令牌必须是**一个原子步骤**（否则并发下会超量放行）
        with self._lock:
            ok_s, wait_s = (self.sn.peek(ks, now) if ks else (True, 0.0))
            ok_r, wait_r = (self.router.peek(kr, now) if kr else (True, 0.0))
            if ok_s and ok_r:
                if ks:
                    self.sn.consume(ks, now)
                if kr:
                    self.router.consume(kr, now)
                self.allowed += 1
                return Decision(True)
            # 拒绝：如实说清是哪条防线拒的（页面/审计按这个字段展示）
            which = "sn" if not ok_s else "router"
            retry = wait_s if not ok_s else wait_r
            if which == "sn":
                self.dropped_sn += 1
            else:
                self.dropped_router += 1
            self.last_drop = {"t": time.time(), "which": which, "sn": ks,
                              "router": kr, "kind": kind}
            self._recent.append({"t": time.strftime("%H:%M:%S"), "which": which,
                                 "sn": ks or "-", "router": kr or "-",
                                 "kind": kind,
                                 "retry_after": None if retry == float("inf")
                                 else round(retry, 2)})
            del self._recent[:-20]           # 只留最近 20 条（环形，别长）
        return Decision(False, which=which, retry_after=retry)

    def dropped(self):
        """累计丢弃数（SN 防线 + Router 防线）。"""
        return self.dropped_sn + self.dropped_router

    def recent(self, n=5):
        """最近被丢弃的记录（最新在前）。"""
        with self._lock:
            return list(reversed(self._recent[-n:]))

    def reset_counters(self):
        """清零计数（演示按钮用：先清零再刷量，页面上的数字才对得上）。"""
        with self._lock:
            self.allowed = 0
            self.dropped_sn = 0
            self.dropped_router = 0
            self._recent = []
            self.since = time.time()

    def snapshot(self, now=None):
        """给 `/api/status`（形状与 `/api/energy` 一样是 `on/params/...` 的单一源）。

        **不含全表**（桶表可能几千个 key，不该每秒重传）——只给计数 + 最狠的几台。
        """
        return {
            "on": self.enabled,
            "params": {"sn_rate": self.sn.rate, "sn_burst": self.sn.burst,
                       "router_rate": self.router.rate,
                       "router_burst": self.router.burst,
                       "max_keys": self.sn.max_keys},
            "allowed": self.allowed,
            "dropped": {"sn": self.dropped_sn, "router": self.dropped_router,
                        "total": self.dropped()},
            "sn_table": self.sn.state(now),
            "router_table": self.router.state(now),
            "top_sn": self.sn.top(3, now),
            "recent": self.recent(5),
            "since": self.since,
        }


# ---- 设备侧（客户端）自限频：第三套前缀 `ORPAH_SELF_*`（别与上面两套混用） ----
# 默认值怎么来的（**演示配置，不是真机标定**，2026-09-13 按本 demo 的真实节奏定的）：
#   · 本 demo 里「设备**自己**的业务上报」的真实节奏（`ui_server._report_loop`）=
#     每个周期 3 条集中在 ~0.25s 内（REQ-CONNECT → +0.25s REPORT → 同拍 ID-REPORT），
#     然后歇 `every`（默认 2s；能量轴给的最小间隔也是 2s）→ **长期 1.5 条/秒、瞬时 3 条**。
#   · `min_interval=0.6`（≈1.67 条/秒）**高于**长期 1.5 条/秒 → 正常节奏**永不触发**
#     （这就是"默认参数不该误伤好设备"的那条回归锁）；同时它**低于** Server 侧 per-SN 的
#     2 条/秒 → 守规矩的设备根本不该撞上上游的桶（撞上 = 白花空口 + 被丢）。
#   · `burst=4` > 一个周期内的 3 条 → 周期内的突发不会被自己延后。
SELF_ENABLE = _env_bool("ORPAH_SELF_ENABLE", True)
SELF_MIN_INTERVAL = _env_float("ORPAH_SELF_MIN_INTERVAL", 0.6)
SELF_BURST = _env_int("ORPAH_SELF_BURST", 4)


class DeviceLimiter:
    """设备**自己**的上行限频（§5.8「各环节」里设备这一环，2026-09-13 补）。

    ## 与 Server / Router 两侧的**根本区别**（这条决定了一切措辞）
    那两处是对**别人**限频（且不相信报文内容）；这里是设备对自己限频 —— **自愿的**：
    一台被改过的、或本来就失控的设备**根本不会做**（协议里也没法要求它做）。
    所以它**不是防线**，不得写成「防住了」；它只让**守规矩的设备**不给自己和邻居添堵：

    1. **空口是共享介质**：Router 侧的桶虽然会把它丢掉，但那些帧**已经上过空口了**
       （带宽已经花掉，丢在 Router 只是省了后面那段的 UDP/CPU）；
    2. **省电**：每次上报都要花 `energy.COST_MJ`（免电池设备最紧的正是这个）；
    3. **不撞上游的桶**：撞上 = 既没被听见、又占了空口、又被记一次丢弃。

    ## 语义是**延后（hold）**，不是丢弃
    丢自己的业务报 = **漏报** = 找人失败。所以这里只回答「现在能不能发」，
    不能发就给「还差多久」（`Decision.retry_after`）——**不睡眠、不轮询**（同本模块纪律），
    调用方要么下一拍再发，要么用 `waiting.wait_until(...)` 等。

    ## 演示里的边界（写在代码里，免得下一个人误读）
    攻击注入 / 刷量 / 重放这些演示动作**不是本机自己的业务上报**（那是"另一台设备"或
    "一台失控设备"的行为）→ 调用方用 `ClientHost.send_*(force=True)` 明确绕过它。
    这不是"留后门"：自限频本来就是自愿的，**被改的设备不绕也拦不住**。
    """

    def __init__(self, enabled=SELF_ENABLE, min_interval=SELF_MIN_INTERVAL,
                 burst=SELF_BURST, name="client"):
        self.enabled = bool(enabled)
        self.min_interval = max(0.0, float(min_interval))
        self.name = name
        self.burst = int(max(1, burst))
        # 惰性建桶（见 `_bucket`）：**桶的时钟必须与调用方给的 `now` 同一套** ——
        # 先在构造里用 `time.monotonic()` 建好，测试传假时钟（`now=0`）时会被算成
        # “负时间差” → 满桶瞬间被扣光（实测踩过：正常节奏 60 条全被延后）。
        self.bucket = None
        self.allowed = 0
        self.held = 0                       # 被延后的次数（**不是**丢弃：下一拍还会发）
        self.last_hold = None
        self.since = time.time()
        self._recent = []
        self._lock = threading.Lock()

    def _bucket(self, now):
        """惰性建桶（第一次用时以调用方的 `now` 为基准）—— 同 `_Buckets._get` 的做法。"""
        if self.bucket is None:
            rate = (1.0 / self.min_interval) if self.min_interval > 0 else 0.0
            self.bucket = TokenBucket(rate, self.burst, now=now)
        return self.bucket

    @classmethod
    def from_env(cls, prefix="ORPAH_SELF_", enable=SELF_ENABLE,
                 min_interval=SELF_MIN_INTERVAL, burst=SELF_BURST):
        """按环境变量构造（每次重读环境 → 测试可改 env 后重建）。"""
        return cls(enabled=_env_bool(prefix + "ENABLE", enable),
                   min_interval=_env_float(prefix + "MIN_INTERVAL", min_interval),
                   burst=_env_int(prefix + "BURST", burst))

    def check(self, sn=None, kind="", now=None):
        """能不能发这一条？允许则扣一格；不允许则计数 + 留痕。

        `kind` 只是给人看的（REQ-CONNECT / REPORT / ID-REPORT），**不参与**判定 ——
        一条上行占的就是一份空口时间，不分类型（分类型限反而能靠换类型绕开）。
        """
        if not self.enabled:
            return Decision(True)
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            b = self._bucket(now)
            ok, wait = b.peek(now)
            if ok:
                b.consume(now)
                self.allowed += 1
                return Decision(True)
            self.held += 1
            retry = None if wait == float("inf") else round(wait, 3)
            rec = {"t": time.strftime("%H:%M:%S"), "side": "client",
                   "mtype": kind or "uplink", "sn": sn or "-", "router": "-",
                   "which": "interval", "retry_after": retry}
            self.last_hold = {"t": time.time(), "which": "interval",
                              "sn": sn, "kind": kind, "retry_after": retry}
            self._recent.append(rec)
            del self._recent[:-20]           # 只留最近 20 条（环形，别长）
        return Decision(False, which="interval", retry_after=wait)

    def wait_for(self, now=None):
        """还要等多久才能再发（秒；0 = 现在就能发）。**纯计算，不睡眠。**"""
        if not self.enabled:
            return 0.0
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            ok, wait = self._bucket(now).peek(now)
            return 0.0 if ok else wait

    def recent(self, n=5):
        with self._lock:
            return list(reversed(self._recent[-n:]))

    def reset_counters(self):
        with self._lock:
            self.allowed = 0
            self.held = 0
            self._recent = []
            self.last_hold = None
            self.since = time.time()

    def snapshot(self, now=None):
        """给 `/api/status`（形状与 `RateLimiter.snapshot` 对齐，页面用同一套渲染代码）。

        `held` 是**延后**，不是丢：页面文案必须跟着这个词（写「丢弃」会让人以为漏报了）。
        """
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            b = self._bucket(now)
        return {
            "on": self.enabled,
            "params": {"min_interval": self.min_interval,
                       "rate": round(b.rate, 3), "burst": int(b.burst)},
            "allowed": self.allowed,
            "held": self.held,
            "last_hold": self.last_hold,
            "recent": self.recent(5),
            "since": self.since,
        }
