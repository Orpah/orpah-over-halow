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
    """key → TokenBucket 的表（带 key 上限，超出淘汰**最久未用**的）。

    key 来自报文内容（sn / 源地址），是**不可信输入** → 必须有上限，
    否则"每个 SN 一个桶"就等于给攻击者一个内存放大器（§8 威胁 1 的一种死法）。
    """

    def __init__(self, rate, burst, max_keys=MAX_KEYS, name="sn"):
        self.rate = rate
        self.burst = burst
        self.max_keys = max(16, int(max_keys))
        self.name = name
        self.buckets = {}
        self._lock = threading.Lock()
        self.evicted = 0                       # 淘汰次数（可观测：不该频繁涨）
        self.created = 0                       # 建桶次数

    def _get(self, key, now):                  # 调用方持锁
        b = self.buckets.get(key)
        if b is None:
            if len(self.buckets) >= self.max_keys:
                # 淘汰最久未用（last 最小）：不用 OrderedDict，桶数 = key 数，线性扫可接受
                oldest = min(self.buckets, key=lambda k: self.buckets[k].last)
                del self.buckets[oldest]
                self.evicted += 1
            b = TokenBucket(self.rate, self.burst, now=now)
            self.buckets[key] = b
            self.created += 1
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

    def top(self, n=3, now=None):
        """令牌最少的几台（"被刷得最狠"的线索；只给线索，不指认谁在攻击）。"""
        now = time.monotonic() if now is None else float(now)
        with self._lock:
            rows = [(k, b.snapshot(now)) for k, b in self.buckets.items()]
        rows.sort(key=lambda kv: kv[1]["tokens"])
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
    def from_env(cls):
        """按环境变量构造（每次调用重读环境 → 测试可改 env 后重建）。"""
        return cls(enabled=_env_bool("ORPAH_RL_ENABLE", ENABLE),
                   sn_rate=_env_float("ORPAH_RL_SN_RATE", SN_RATE),
                   sn_burst=_env_int("ORPAH_RL_SN_BURST", SN_BURST),
                   router_rate=_env_float("ORPAH_RL_ROUTER_RATE", ROUTER_RATE),
                   router_burst=_env_int("ORPAH_RL_ROUTER_BURST", ROUTER_BURST),
                   max_keys=_env_int("ORPAH_RL_MAX_KEYS", MAX_KEYS))

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
        ok_s, wait_s = (self.sn.peek(ks, now) if ks else (True, 0.0))
        ok_r, wait_r = (self.router.peek(kr, now) if kr else (True, 0.0))
        if ok_s and ok_r:
            if ks:
                self.sn.consume(ks, now)
            if kr:
                self.router.consume(kr, now)
            with self._lock:
                self.allowed += 1
            return Decision(True)
        # 拒绝：如实说清是哪条防线拒的（页面/审计按这个字段展示）
        which = "sn" if not ok_s else "router"
        retry = wait_s if not ok_s else wait_r
        with self._lock:
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
