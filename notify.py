#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""notify.py — 告警通知（出站）：把「新出现的 / 升级了的」告警推给 Webhook（2026-09-13）

**为什么需要**：`alerts.evaluate()` 只是**状态**（每几秒重算一遍，返回当前活跃告警）。
状态只能被「正在看页面的人」看到 —— 没人在看就等于没有告警。通知是**事件**：
它该在「出现/变严重」的那一刻推出去一次，而不是跟着轮询一直推。

三条硬取舍（都踩过/都是有意选的）：

1. **边沿触发，不是每次轮询都推**。只推两类：
   - 新出现的 `key`（`kind:对象`，如 `no_report:CN-WH01-9AF3C1D2`）；
   - 同一个 `key` **级别升高**（warn → crit）—— 升级再推一次是**有意**的：
     那正是「该叫人起来了」的时刻。同级反复出现**不重复推**。
   不差分就会每 3 秒推一遍同一条，把接收端刷爆。
2. **失败必须可见**：投递失败记计数 + 最近错误 + 交给调用方写审计事件，
   **不静默吞**（本仓一贯口径）。**不做重试队列**：demo 不做可靠投递
   —— 重试/退避/持久化队列是另一件事，真要做先与用户对齐。
3. **不落盘**：`seen` 只在内存里 → **进程重启后活跃告警会被重推一遍**，如实写在这里。

**边界（不得写成“通知到了”）**：出站只有一个 HTTP POST，**没有鉴权/签名**、
**没有投递保证**（失败就失败）；URL 由使用者自填（演示用 http://127.0.0.1:… 的假接收端即可）。

    ORPAH_NOTIFY_URL        Webhook 地址（空 = 关闭，默认空）
    ORPAH_NOTIFY_MIN_LEVEL  最低推送级别：warn（默认）| crit（只看严重的）
    ORPAH_NOTIFY_TIMEOUT    单次 POST 超时秒（默认 3）
    ORPAH_NOTIFY_SEC        评估间隔秒（默认 3；ui_server 的后台线程用）
    ORPAH_NOTIFY_RESOLVE    告警消失时是否补推一条 resolved（默认 0=不推）

**文案**：只推机器可读字段（`kind/level/key/msg` + 规则数据）；`msg` 是 i18n 键，
接收端自己本地化 —— 与页面同一个约定（后端不拼中英文）。
"""
import json
import os
import time
import urllib.error
import urllib.request

LEVELS = {"warn": 1, "crit": 2}


def _env_int(name, default):
    try:
        return int(str(os.environ.get(name, "") or default).strip())
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(str(os.environ.get(name, "") or default).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name, default=False):
    v = str(os.environ.get(name, "") or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _http_post(url, payload, timeout):
    """POST JSON → (ok, status, err)。**不抛异常**（网络问题不该把评估循环打断）。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, int(getattr(r, "status", 0) or 0), ""
    except urllib.error.HTTPError as e:            # 有响应但状态码非 2xx
        return False, int(e.code or 0), f"HTTP {e.code}"
    except Exception as e:                          # 连不上/超时/URL 非法…
        return False, 0, f"{type(e).__name__}: {e}"


class Notifier:
    """告警出站通知器（有状态：记住“哪些已经推过”）。

    投递函数可注入（`post=_http_post`），单测用假函数 → 不碰网络。
    """

    def __init__(self, url=None, min_level=None, timeout=None, resolve=None,
                 post=None, keep=20):
        self.url = (os.environ.get("ORPAH_NOTIFY_URL", "") if url is None else url) or ""
        self.min_level = (os.environ.get("ORPAH_NOTIFY_MIN_LEVEL", "warn")
                          if min_level is None else min_level) or "warn"
        self.timeout = (_env_float("ORPAH_NOTIFY_TIMEOUT", 3.0)
                        if timeout is None else float(timeout))
        self.resolve = (_env_bool("ORPAH_NOTIFY_RESOLVE", False)
                        if resolve is None else bool(resolve))
        self.post = post or _http_post
        self.seen = {}                 # key → level（上次评估时的活跃告警；只在内存）
        self.sent = 0                  # 累计投递成功
        self.failed = 0                # 累计投递失败
        self.skipped = 0               # 因低于 min_level 而未推（同一 key 只计一次）
        self.resolved_total = 0
        self.keep = keep
        self.deliveries = []           # 最近投递记录（最新在前）
        self.last_error = None         # 最近一次失败原因（页面/“测试发送”都用得到）

    # ---------------- 配置（运行时改，不重启） ----------------
    def set_url(self, url):
        """改 Webhook 地址（空 = 关闭）。改地址**不清 seen** —— 换接收端时
        已推过的告警不会因为换了地址就重推一遍（要重推就重启或用「测试发送」）。"""
        self.url = (url or "").strip()
        return self.url

    def set_min_level(self, level):
        if level in LEVELS:
            self.min_level = level
        return self.min_level

    def set_resolve(self, on):
        self.resolve = bool(on)
        return self.resolve

    # ---------------- 主流程 ----------------
    def step(self, alerts, now=None):
        """一次评估 → 需要推的推掉 → 返回本次**实际投递**的记录列表。

        `alerts` = `alerts.evaluate()` 的结果（当前活跃告警）。
        """
        now = int(now if now is not None else time.time())
        cur = {}
        for a in alerts or []:
            k = a.get("key")
            if k:
                cur[k] = a
        # 没配地址 = 通知关闭：只维护 seen（开着以后不会把「关着这段时间的活跃告警」当新告警重推
        # 一遍 —— 那是刷新页面级的噪声，不是新事件），不产生投递记录。
        if not self.url:
            self.seen = {k: (a.get("level") or "warn") for k, a in cur.items()}
            return []
        out = []
        # 1) 新增 / 升级
        for k, a in cur.items():
            lv = a.get("level") or "warn"
            prev = self.seen.get(k)
            if prev is not None and LEVELS.get(lv, 0) <= LEVELS.get(prev, 0):
                continue                       # 同级反复出现 → 不重复推（边沿触发）
            if LEVELS.get(lv, 0) < LEVELS.get(self.min_level, 1):
                self.skipped += 1              # 低于最低推送级别 → 不推（但要记进 seen）
                self.seen[k] = lv
                continue
            ev = "alert" if prev is None else "alert_upgraded"
            out.append(self._deliver(ev, a, now, prev_level=prev))
            self.seen[k] = lv
        # 2) 消失（可选）
        gone = [k for k in self.seen if k not in cur]
        for k in gone:
            lv = self.seen.pop(k)
            if self.resolve and LEVELS.get(lv, 0) >= LEVELS.get(self.min_level, 1):
                out.append(self._deliver("alert_resolved",
                                         {"key": k, "level": lv, "kind": k.split(":", 1)[0]},
                                         now, prev_level=lv))
                self.resolved_total += 1
        return [r for r in out if r]

    def _deliver(self, event, alert, now, prev_level=None, count=True):
        """一条投递。没配 URL → 记一条 `off` 记录（**不**算失败，也不算成功）。

        `count=False`（「测试发送」用）：只进「最近投递」列表，**不进** `sent`/`failed`
        —— 页面上的「已推 N 条」说的是**告警**推了多少条，混进测试发送会虚高。
        """
        rec = {"t": time.strftime("%H:%M:%S", time.localtime(now)), "epoch": int(now),
               "event": event, "key": alert.get("key"),
               "kind": alert.get("kind"), "level": alert.get("level"),
               "prev_level": prev_level, "ok": None, "status": 0, "err": None}
        if not self.url:
            rec["err"] = "off"                 # 没配地址 = 没开通知，如实标出来
            self._remember(rec)
            return rec
        payload = {"source": "orpah-over-halow", "event": event, "ts": int(now),
                   "key": alert.get("key"), "alert": alert}
        ok, status, err = self.post(self.url, payload, self.timeout)
        rec.update(ok=bool(ok), status=int(status or 0), err=(err or None))
        if ok:
            if count:
                self.sent += 1
        else:
            if count:
                self.failed += 1
            self.last_error = err or "unknown"
        self._remember(rec)
        return rec

    def _remember(self, rec):
        self.deliveries.insert(0, rec)
        del self.deliveries[self.keep:]

    # ---------------- 测试发送 / 快照 ----------------
    def test(self, now=None):
        """「测试发送」：立刻推一条演示通知（**不改 seen**，不影响边沿触发状态）。"""
        now = int(now if now is not None else time.time())
        fake = {"key": "notify_test", "kind": "notify_test", "level": "warn",
                "msg": "notify_test_msg", "since": now}
        rec = self._deliver("test", fake, now, count=False)
        return rec

    def snapshot(self):
        """给 `/api/status.notify`（页面显示：开没开、推了多少、最近一次什么样）。"""
        return {"on": bool(self.url), "url": self.url, "min_level": self.min_level,
                "timeout": self.timeout, "resolve": self.resolve,
                "interval_sec": _env_int("ORPAH_NOTIFY_SEC", 3),
                "sent": self.sent, "failed": self.failed, "skipped": self.skipped,
                "resolved": self.resolved_total,
                "watching": len(self.seen), "last_error": self.last_error,
                "recent": list(self.deliveries[:5])}
