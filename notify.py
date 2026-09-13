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
2. **失败必须可见 + 有界重试（2026-09-13 补）**：投递失败记计数 + 最近错误 + 交给调用方写审计事件，
   **不静默吞**；失败的那条进**内存重试队列**，按**固定退避序列**（默认 1s / 5s / 30s）再试，
   试完仍失败才计「已失败（放弃）」。
   **语义是「至少一次」**：POST 超时/断连时对方**可能已经收到**，重试就会**重复送达** ——
   接收端要自己做去重（消息里有稳定的 `key` + `event`）。**不假装 exactly-once**。
   **不做持久化队列**：队列在内存里 → 进程重启时**丢弃未送出的重试**（如实记在快照里）。
   每 tick 最多试 `ORPAH_NOTIFY_PER_STEP` 条（默认 3）—— 否则端点挂了时 20 条积压 × 3s 超时
   会把后台巡视线程卡住（连告警评估都停了，比丢通知更糟）。
3. **不落盘**：`seen` 只在内存里 → **进程重启后活跃告警会被重推一遍**，如实写在这里。

**边界（不得写成“通知到了”）**：出站只有一个 HTTP POST，**没有鉴权/签名**、
**没有投递保证**（失败就失败）；URL 由使用者自填（演示用 http://127.0.0.1:… 的假接收端即可）。

    ORPAH_NOTIFY_URL        Webhook 地址（空 = 关闭，默认空）
    ORPAH_NOTIFY_MIN_LEVEL  最低推送级别：warn（默认）| crit（只看严重的）
    ORPAH_NOTIFY_TIMEOUT    单次 POST 超时秒（默认 3）
    ORPAH_NOTIFY_SEC        评估间隔秒（默认 3；ui_server 的后台线程用）
    ORPAH_NOTIFY_RESOLVE    告警消失时是否补推一条 resolved（默认 0=不推）
    ORPAH_NOTIFY_RETRY      首次失败后**再试几次**（默认 2，即最多 3 次尝试；0 = 不重试）
    ORPAH_NOTIFY_BACKOFF    退避秒序列（默认 `1,5,30`；次数多于序列长度时用最后一个值）
    ORPAH_NOTIFY_QUEUE_MAX  重试队列上限（默认 20；满时丢**最旧**的并计数）
    ORPAH_NOTIFY_PER_STEP   每 tick 最多试几条（默认 3；防端点挂掉时把巡视线程卡住）

**文案**：只推机器可读字段（`kind/level/key/msg` + 规则数据）；`msg` 是 i18n 键，
接收端自己本地化 —— 与页面同一个约定（后端不拼中英文）。
"""
import json
import os
import time
import urllib.error
import urllib.request
from collections import deque

LEVELS = {"warn": 1, "crit": 2}


def _backoff_list():
    """退避序列（秒）。写坏了就回默认 —— 起不来比退避不准更糟（同本仓其余 env 语义）。"""
    raw = str(os.environ.get("ORPAH_NOTIFY_BACKOFF", "") or "1,5,30")
    out = []
    for part in raw.split(","):
        try:
            v = float(part.strip())
        except (TypeError, ValueError):
            continue
        if v >= 0:
            out.append(v)
    return tuple(out) or (1.0, 5.0, 30.0)


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
                 post=None, keep=20, retry=None, backoff=None, queue_max=None):
        self.url = (os.environ.get("ORPAH_NOTIFY_URL", "") if url is None else url) or ""
        self.min_level = (os.environ.get("ORPAH_NOTIFY_MIN_LEVEL", "warn")
                          if min_level is None else min_level) or "warn"
        self.timeout = (_env_float("ORPAH_NOTIFY_TIMEOUT", 3.0)
                        if timeout is None else float(timeout))
        self.resolve = (_env_bool("ORPAH_NOTIFY_RESOLVE", False)
                        if resolve is None else bool(resolve))
        self.post = post or _http_post
        self.seen = {}                 # key → level（上次评估时的活跃告警；只在内存）
        self.sent = 0                  # **最终送达**的条数（含重试成功的；每条告警最多 +1）
        self.failed = 0                # **最终放弃**的条数（重试用尽才计）—— 不是“尝试失败次数”
        self.retried = 0               # 重试**次数**（attempt > 1 的投递次数）
        self.dropped = 0               # 队列满 / 换地址 / 关通知时被丢掉的待重试条数
        self.skipped = 0               # 因低于 min_level 而未推（同一 key 只计一次）
        self.resolved_total = 0
        self.keep = keep
        self.deliveries = []           # 最近投递记录（最新在前）
        self.last_error = None         # 最近一次失败原因（页面/“测试发送”都用得到）
        # 有界重试（“至少一次”语义，见模块头）：待重试的投递（FIFO ≈ 按 next_ts 有序）
        self.retry_attempts = (_env_int("ORPAH_NOTIFY_RETRY", 2)
                               if retry is None else int(retry))
        self.backoff = tuple(backoff) if backoff is not None else _backoff_list()
        self.queue_max = (_env_int("ORPAH_NOTIFY_QUEUE_MAX", 20)
                          if queue_max is None else int(queue_max))
        self.per_step = max(1, _env_int("ORPAH_NOTIFY_PER_STEP", 3))
        self.queue = deque()           # [{event, alert, prev_level, tries, next_ts}]

    def _handle_failure(self, rec, event, alert, prev_level, now, tries):
        """一次投递失败后的去向：还有额度 → 入队等退避；额度用完 → **计一次放弃**。

        `retry_attempts = 0`（不重试）时就是“一失败就放弃”（旧行为的语义）。
        """
        if tries - 1 < self.retry_attempts:
            self._enqueue(event, alert, prev_level, now, tries=tries)
        else:
            self._give_up(tries)

    def _wait_for(self, tries):
        """第 `tries` 次尝试失败后等多久再试（`tries` 从 1 起；次数超序列就用最后一个值）。"""
        i = max(0, min(len(self.backoff) - 1, int(tries) - 1))
        return self.backoff[i] if self.backoff else 0.0

    def _enqueue(self, event, alert, prev_level, now, tries=1):
        """把一条投递放进重试队列。

        同 `key` 已在队列里 → **合并**（换上**最新**的载荷：升级/变化以新的一条为准）——
        不合并的话，“失败待重试期间又升级”会变成两条通知（自己制造重复）。
        """
        key = alert.get("key")
        for it in self.queue:
            if it["alert"].get("key") == key:
                it["event"] = event
                it["alert"] = alert
                it["prev_level"] = prev_level
                it["next_ts"] = now + self._wait_for(it["tries"])
                return
        if len(self.queue) >= max(1, self.queue_max):
            self.queue.popleft()       # 丢最旧的（端点久挂时保新弃旧），并让丢弃**可见**
            self.dropped += 1
        self.queue.append({"event": event, "alert": alert, "prev_level": prev_level,
                           "tries": int(tries), "next_ts": now + self._wait_for(tries)})

    def drop_queue(self, reason=""):
        """丢掉所有待重试（换地址 / 关通知时用）—— **计数可见**，不静默。"""
        n = len(self.queue)
        self.queue.clear()
        self.dropped += n
        if n:
            self.last_error = f"queue dropped ({reason})" if reason else "queue dropped"
        return n

    # ---------------- 配置（运行时改，不重启） ----------------
    def set_url(self, url):
        """改 Webhook 地址（空 = 关闭）。改地址**不清 seen** —— 换接收端时
        已推过的告警不会因为换了地址就重推一遍（要重推就重启或用「测试发送」）。
        但**要丢掉待重试队列**：那些条目属于旧端点，往新地址重发是错的通知。"""
        new = (url or "").strip()
        if new != self.url:
            self.drop_queue("url changed" if new else "notify off")
        self.url = new
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
        """一次评估 → 先补做到期的重试 → 再推新增/升级 → 返回本次**实际投递**的记录。

        `alerts` = `alerts.evaluate()` 的结果（当前活跃告警）。
        `now` 是**纯函数参数**（不睡眠不轮询）：退避用 `now` 比较，测试传假时钟即可复现。
        每次调用**最多**发 `per_step` 条（含重试）—— 端点挂掉时不把巡视线程卡住。
        """
        now = float(now if now is not None else time.time())
        self._now = now                    # 供快照算“还要等多久”（避免快照里混真实时钟）
        cur = {}
        for a in alerts or []:
            k = a.get("key")
            if k:
                cur[k] = a
        # 没配地址 = 通知关闭：只维护 seen（开着以后不会把「关着这段时间的活跃告警」当新告警重推
        # 一遍 —— 那是刷新页面级的噪声，不是新事件），不产生投递记录。
        if not self.url:
            self.seen = {k: (a.get("level") or "warn") for k, a in cur.items()}
            self.drop_queue("notify off")
            return []
        out = []
        budget = max(1, self.per_step)
        # 0) 到期的重试（FIFO ≈ 按 next_ts 有序；遇到“还没到期”的就停 —— 后面的更晚）
        while self.queue and budget > 0:
            it = self.queue[0]
            if it["next_ts"] > now:
                break
            self.queue.popleft()
            budget -= 1
            it["tries"] += 1
            self.retried += 1
            rec = self._deliver(it["event"], it["alert"], now,
                                prev_level=it["prev_level"], tries=it["tries"])
            out.append(rec)
            if not rec.get("ok"):
                self._handle_failure(rec, it["event"], it["alert"], it["prev_level"],
                                     now, it["tries"])
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
            if budget <= 0:                    # 本 tick 的额度用完了 → 下 tick 再试
                self._enqueue(ev, a, prev, now)
                self.seen[k] = lv
                continue
            budget -= 1
            rec = self._deliver(ev, a, now, prev_level=prev)
            out.append(rec)
            if not rec.get("ok"):
                self._handle_failure(rec, ev, a, prev, now, 1)
            self.seen[k] = lv
        # 2) 消失（可选）
        gone = [k for k in self.seen if k not in cur]
        for k in gone:
            lv = self.seen.pop(k)
            if self.resolve and LEVELS.get(lv, 0) >= LEVELS.get(self.min_level, 1):
                if budget <= 0:
                    self._enqueue("alert_resolved",
                                  {"key": k, "level": lv, "kind": k.split(":", 1)[0]},
                                  lv, now)
                else:
                    budget -= 1
                    rec = self._deliver("alert_resolved",
                                        {"key": k, "level": lv,
                                         "kind": k.split(":", 1)[0]},
                                        now, prev_level=lv)
                    out.append(rec)
                    if not rec.get("ok"):
                        self._handle_failure(rec, "alert_resolved",
                                             {"key": k, "level": lv,
                                              "kind": k.split(":", 1)[0]}, lv, now, 1)
                self.resolved_total += 1
        return [r for r in out if r]

    def _deliver(self, event, alert, now, prev_level=None, count=True, tries=1):
        """一条投递。失败时**不直接计「已失败」** —— 交给 `step()` 决定入队重试还是放弃。

        `count=False`（「测试发送」用）：只进「最近投递」列表，**不进**计数
        —— 页面上的「已推 N 条」说的是**告警**推了多少条，混进测试发送会虚高。
        「测试发送」是用户点击的同步动作，**不重试**（要重试就再点一下）。
        """
        rec = {"t": time.strftime("%H:%M:%S", time.localtime(now)), "epoch": int(now),
               "event": event, "key": alert.get("key"),
               "kind": alert.get("kind"), "level": alert.get("level"),
               "prev_level": prev_level, "tries": int(tries),
               "ok": None, "status": 0, "err": None}
        if not self.url:
            rec["err"] = "off"                 # 没配地址 = 没开通知，如实标出来
            self._remember(rec)
            return rec
        payload = {"source": "orpah-over-halow", "event": event, "ts": int(now),
                   "key": alert.get("key"), "alert": alert}
        ok, status, err = self.post(self.url, payload, self.timeout)
        rec.update(ok=bool(ok), status=int(status or 0), err=(err or None))
        if count:
            if ok:
                self.sent += 1                 # 最终送达（含重试成功的）
            else:
                self.last_error = err or "unknown"
        self._remember(rec)
        return rec

    def _give_up(self, tries):
        """重试用尽 → 计一次「已失败（放弃）」+ 记原因（页面/审计都看得到）。"""
        self.failed += 1
        self.last_error = self.last_error or f"gave up after {tries} tries"

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
        """给 `/api/status.notify`（页面显示：开没开、推了多少、队列里还有几条）。"""
        nxt = self.queue[0] if self.queue else None
        return {"on": bool(self.url), "url": self.url, "min_level": self.min_level,
                "timeout": self.timeout, "resolve": self.resolve,
                "interval_sec": _env_int("ORPAH_NOTIFY_SEC", 3),
                "sent": self.sent, "failed": self.failed, "skipped": self.skipped,
                "resolved": self.resolved_total,
                # 重试（至少一次语义，见模块头）：attempts = 首次失败后再试几次
                "retry": {"attempts": self.retry_attempts, "backoff": list(self.backoff),
                          "queued": len(self.queue), "retried": self.retried,
                          "dropped": self.dropped,
                          "next_in": (None if nxt is None
                                      else max(0.0, round(nxt["next_ts"]
                                                          - getattr(self, "_now", time.time()), 1))),
                          "next_key": (nxt["alert"].get("key") if nxt else None)},
                "watching": len(self.seen), "last_error": self.last_error,
                "recent": list(self.deliveries[:5])}
