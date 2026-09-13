#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spoof.py — 无认证空口防 spoof：攻击报文构造 + 期望裁决（2026-09-12，P1）。

**背景（本项目的硬前提）**：ORPAH 的空口是**开放/无认证**的 —— 低功耗客户端不做入网认证
（"无认证"正是设计前提，见 SPEC）。于是**任何人都能往空口里注入一条 ORPAH-ID-REPORT**。
服务器唯一的防线是一串校验，本模块就是把每一种攻击各造一条、交给
`orpah_id.verify_report` 裁决：

    ① 报文格式（typ/ver/alg 白名单/level）
    ② SN 校验位（Damm32 / mod97）      ← 拦住"乱编的 SN"，不查库就能挡掉一大批
    ③ 时间窗 + nonce 去重               ← 拦住重放与陈旧报文
    ④ 吊销表                            ← 已作废的钥匙不能再验过
    ⑤ **设备公钥验签**（最后一道，也是唯一能证明"这条报文确实出自该设备"的）← 多代轮换逐代试签

**为什么要单独一个模块**：`demo_spoof.py`（自包含端到端脚本）、`ui_server.py`
（`/api/ctl action=spoof` 真的把攻击报文注入空口）与 **攻击流量面板**（`ui/static/attack.html`）
必须用**同一份攻击构造**，否则"演示的"和"测的"会漂移。
同理，「**是哪一道防线拦的**」也只有一份映射：本模块的 `DEFENSES` / `defense_of()`
（页面按错误码查它；没映射过的码显示成「未归类」，不瞎猜）。

**已知边界（演示会如实展示）**：`xport`（路由器侧附加的观测，`attach_xport`）**不在签名预像里** ——
它由路由器在转发时追加，签名只覆盖设备自己的声明（`payload.seen_routers`）。
所以篡改 xport 改不出"拒"，这是**设计使然**，但意味着：**能冒充/控制一台路由器的人可以伪造定位**
（定位数据正来自路由器侧测量）。这条已记进 ROADMAP 开放问题，需要路由器侧身份/签名来补。
"""
import copy

import orpah_id as oid

# 设备自报的观测（进签名）：设备声称"我此刻听到的 AP"
SEEN_ROUTERS = [{"bssid": "AA:BB:CC:DD:EE:FF", "ssid": "ORPAHID_ZONE_A",
                 "rssi": -42}]

# kind → (中文名, 英文名, 期望结果, (中文说明, 英文说明))
# 期望结果 = None 表示"应被接受"，否则为 verify_report 的拒绝码之一
#
# 说明文字是 (zh, en) 两套：`case_info()` 仍只回中文那一份（历史调用方不变），
# 页面用 `case_note(kind, lang)` 按当前语言取。**说明里不许写 Markdown**（页面上会把
# `**` 原样显示成星号，见 AGENTS §0「文案不得写 Markdown」）——本模块的文案会直接上页面。
CASES = [
    ("legit", "对照组：合法签名上报", "control: properly signed report",
     None,
     ("设备用自己那把钥匙签，服务器验签通过 —— 没有它，下面的「拒绝」说明不了任何问题",
      "The device signs with its own key and the server verifies it — without this row the "
      "rejections below would prove nothing")),
    # 补上第一道防线的用例（2026-09-13）：此前 13 种里**没有一条**会被「格式」拦下，
    # 攻击流量面板的防线分布里那条永远是 0 → 看不出第一道防线是活的。
    ("bad_format", "格式非法（改报文头 typ）", "malformed header (bad typ)",
     "bad_typ",
     ("把报文头的 typ 改成别的值（不再是 orpah-id-report）—— 第一道防线就拦下，连密钥都不查",
      "The header typ is changed to something else (no longer orpah-id-report) — the first "
      "line blocks it before any key is looked up")),
    ("sig_foreign", "伪造签名（用别人的钥匙）", "forged signature (foreign key)",
     "signature_invalid",
     ("攻击者自己生成一对钥匙，签一条声称是被冒充设备 SN 的报文 —— "
      "服务器拿被冒充设备的公钥验，过不了",
      "The attacker generates its own key pair and signs a report claiming the impersonated "
      "device SN — the server verifies with that device's public key, so it fails")),
    ("ts_tamper", "篡改时间戳", "tampered timestamp",
     "signature_invalid",
     ("把 ts 挪到时间窗内（仍在 300 s 窗口里）→ 说明拦住它的不是时间窗，而是签名",
      "ts is moved but stays inside the 300 s window → what blocks it is the signature, "
      "not the time window")),
    ("obs_tamper", "篡改设备自报观测", "tampered device-reported observation",
     "signature_invalid",
     ("改 payload.seen_routers（设备声称的 RSSI）—— 想让服务器以为它在别处",
      "payload.seen_routers (the device-claimed RSSI) is changed — an attempt to make the "
      "server think it is somewhere else")),
    ("level_downgrade", "降级攻击（改 hdr）", "header downgrade",
     "signature_invalid",
     ("把 hdr 改成 level=1/alg=HS256（想骗服务器用弱算法）—— hdr 在预像里，签名随即失效",
      "hdr is changed to level=1/alg=HS256 to get a weaker algorithm accepted — hdr is part "
      "of the preimage, so the signature breaks")),
    ("cap_downgrade", "能力降级（改 payload.cap）", "capability downgrade",
     "signature_invalid",
     ("设备已签声明「有 RTC」，把 payload.cap.rtc 改成 false —— 想让服务端把「有 RTC 设备的"
      "异常时钟」当成「无 RTC 的正常设备」而不告警。cap 在 JCS 预像里 → 签名失效",
      "The device had signed \"has RTC\"; cap.rtc is flipped to false so that an abnormal "
      "clock reads as \"no RTC, normal\" and never alerts. cap is inside the JCS preimage → "
      "the signature breaks")),
    ("alg_none", "免签冒充（alg=none）", "alg=none impersonation",
     "none_requires_level3",
     ("声明 alg=none 且想去掉签名冒充 level0 —— 只允许 level3（仅覆盖发现，不做人员确认）",
      "Claims alg=none with the signature dropped, to impersonate level 0 — none is only "
      "allowed at level 3 (coverage only, no identity confirmation)")),
    ("bad_check", "SN 校验位错", "bad SN check digit",
     "bad_check",
     ("拿格式合法但校验位错的 SN（Damm32/mod97）—— 不查库就能挡掉",
      "An SN whose format is valid but whose check digit is wrong (Damm32/mod97) — blocked "
      "without touching the key store")),
    ("unknown_sn", "未登记的 SN", "unregistered SN",
     "unknown_device",
     ("格式与校验位都合法，但密钥库里没有这台设备",
      "Format and check digit are both valid, but the key store has no such device")),
    ("replay", "重放已用过的报文", "replay of a used report",
     "replay_detected",
     ("原样重发一条已经通过过的合法报文 → nonce 去重拦下",
      "A previously accepted valid report is resent unchanged → nonce dedup blocks it")),
    ("stale", "陈旧报文（超窗）", "stale report (outside window)",
     "timestamp_out_of_window",
     ("合法签名但 ts 超窗（演示 1 小时前）",
      "Properly signed, but ts is outside the window (one hour old in this demo)")),
    ("xport_tamper", "篡改路由器侧观测（已知边界）", "tampered router-side xport (known gap)",
     None,
     ("xport 由路由器追加、不进签名预像 → 验签照样通过。如实展示：本项只保护设备声明，"
      "路由器侧测量要靠多观测一致性来判（见 SPEC §8 原则 P-1）",
      "xport is appended by the router and is not part of the signature preimage → "
      "verification still passes. Honest boundary: this defence covers device claims only; "
      "router-side measurements are judged by multi-observation consensus (SPEC §8 P-1)")),
    # ⚠ 此用例会改**密钥库状态**（revoke）：用**活密钥库**的调用方（`demo_spoof.py` 的 server、
    #   页面 /api/ctl）必须把它排在最后 —— `unrevoke` 会把各代置成 retired，
    #   而 retired 不参与验签 → 之后的用例会集体变成 `unknown_device`。
    #   离线自检 `spoof.run()` 对该用例用**临时库**，与顺序无关（见该函数说明）。
    ("revoked", "已吊销设备", "revoked device",
     "revoked",
     ("被撤销的 SN 用仍然正确的签名上报 → 吊销表拦下（不靠签名）",
      "A revoked SN reports with a signature that is still correct → the revocation list "
      "blocks it (not the signature)")),
]

KINDS = [c[0] for c in CASES]

# UI（网页）可用子集：排除会改服务端密钥库状态的用例 ——
# 网页用的是**活的**密钥库，跑一次 revoked 会把在跑的设备搞成验不过。
UI_KINDS = [k for k in KINDS if k != "revoked"]

# ---------------------------------------------------------------------------
# 防线（**单一源**）：错误码 → 是哪一道防线拦的
# ---------------------------------------------------------------------------
# 顺序 = `orpah_id.verify_report` 的**实际评估顺序**（不是文档里那句概述的顺序 ——
# 代码里 SN 校验位排在 nonce 去重**之后**，这里以代码为准，免得演示与实现说两套）。
# 页面/脚本/文档都从这里取，别再各写一份（`test_attack.py` 会拦漏映射的错误码）。
DEFENSES = [
    ("format", "格式（typ/ver/alg/level/ts/sn/nonce）",
     "format (typ/ver/alg/level/ts/sn/nonce)"),
    ("level", "级别策略（alg=none 只允许 L3）",
     "level policy (alg=none is L3 only)"),
    ("time", "时间窗（±300 s）", "time window (±300 s)"),
    ("nonce", "nonce 去重（重放）", "nonce dedup (replay)"),
    ("check", "SN 校验位（Damm32/mod97）", "SN check digit (Damm32/mod97)"),
    ("revoke", "吊销表", "revocation list"),
    ("keys", "密钥库（未登记 / 无可用钥 / 缺签名）",
     "key store (unknown device / no usable key / missing sig)"),
    ("sig", "设备公钥验签", "device public-key signature"),
    ("accept", "未被拦（接受）", "not blocked (accepted)"),
]
LINE_OF = {
    "bad_format": "format", "bad_typ": "format", "bad_ver": "format",
    "bad_alg": "format", "bad_level": "format", "bad_ts": "format",
    "bad_sn": "format", "bad_nonce": "format",
    "none_requires_level3": "level",
    "timestamp_out_of_window": "time",
    "replay_detected": "nonce",
    "bad_check": "check",
    "revoked": "revoke",
    "unknown_device": "keys", "missing_sig": "keys", "no_hmac_key": "keys",
    "signature_invalid": "sig",
}
LINE_IDS = [d[0] for d in DEFENSES]


def defense_of(err):
    """拒绝码 → 防线 id；None（通过）→ "accept"；没映射过的码 → "other"。

    没映射时不抛异常也不瞎猜（页面显示成「未归类」比显示成某道防线更诚实）。
    """
    if err is None:
        return "accept"
    return LINE_OF.get(err, "other")


def defense_info(line_id):
    """防线 id → (zh, en)；未知 → ("未归类", "unclassified")。"""
    for lid, zh, en in DEFENSES:
        if lid == line_id:
            return zh, en
    return "未归类", "unclassified"


def case_info(kind):
    """→ (zh, en, expect, note)；未知 kind 回落到 legit。

    `note` 是**中文**说明（历史调用方用这个签名）；要按语言取用 `case_note()`。
    """
    for k, zh, en, exp, note in CASES:
        if k == kind:
            return zh, en, exp, note[0]
    return CASES[0][1], CASES[0][2], CASES[0][3], CASES[0][4][0]


def case_note(kind, lang="zh"):
    """→ 该用例的说明文字（lang = "zh"/"en"，其它值回落到 zh）。"""
    for k, _zh, _en, _exp, note in CASES:
        if k == kind:
            return note[1] if lang == "en" else note[0]
    return CASES[0][4][1] if lang == "en" else CASES[0][4][0]


def _legit(dev, now, ts=None, nonce=None):
    return dev.report(level=0, ts=ts if ts is not None else now,
                      nonce=nonce, seen_routers=SEEN_ROUTERS,
                      battery_mv=3700, firmware="1.0.3")


def build_case(kind, dev, now, attacker=None, used_nonce=None):
    """构造一条攻击报文 → (report, expect, note_zh)。

    dev      = 合法设备（= **被冒充对象**，其公钥在服务器密钥库里）
    attacker = 攻击者设备（自己生成钥匙，SN 未登记）；缺省时现造一个
    used_nonce = 「replay」用例用的那条**已用过**的 nonce（由调用方从真实上报里取）
    """
    if kind not in KINDS:
        kind = "legit"
    _, _, expect, note = case_info(kind)

    if kind == "legit":
        return _legit(dev, now), expect, note

    if kind == "bad_format":
        # 报文头 typ 不是 orpah-id-report → 第一道防线（格式）拦下。
        # 注意：本函数只造**里面那条已签报文**，外层信封由**调用方**包装
        # （`orpah_proto.build_id_report`：demo_spoof.py / ui_server 各处），
        # 所以外层照旧合法、链路照常把它送到 server 的验签入口 —— 被拒的是内层报文的格式。
        r = _legit(dev, now)
        r["hdr"]["typ"] = "orpah-id-report-x"
        return r, expect, note

    if kind == "sig_foreign":
        atk = attacker or oid.Device(cc="CN", org="WH01")
        r = _legit(atk, now)                      # 攻击者用自己的钥匙签
        r["payload"]["sn"] = dev.sn               # ……但声称是被冒充设备的 SN
        return r, expect, note

    if kind == "ts_tamper":
        r = _legit(dev, now)
        r["payload"]["ts"] = int(now) - 60        # 仍在 300 s 窗口内
        return r, expect, note

    if kind == "obs_tamper":
        r = _legit(dev, now)
        r["payload"]["seen_routers"] = [{"bssid": "DE:AD:BE:EF:00:01",
                                         "ssid": "FAKE_AP", "rssi": -30}]
        return r, expect, note

    if kind == "level_downgrade":
        r = _legit(dev, now)
        r["hdr"]["level"] = 1
        r["hdr"]["alg"] = oid.ALG_HS256
        return r, expect, note

    if kind == "cap_downgrade":
        # 设备**已签**声明「有 RTC」（cap 在 payload 里 → 在签名覆盖范围内）
        r = dev.report(level=0, ts=int(now), seen_routers=SEEN_ROUTERS,
                       battery_mv=3700, firmware="1.0.3", cap={"rtc": True})
        r["payload"]["cap"]["rtc"] = False        # 攻击者想把它说成“本来就没有时钟”
        return r, expect, note

    if kind == "alg_none":
        r = _legit(dev, now)
        r["hdr"]["alg"] = oid.ALG_NONE
        r["hdr"]["level"] = 0                     # 想去掉签名还冒充 level0
        r.pop("sig", None)
        return r, expect, note

    if kind == "bad_check":
        r = _legit(dev, now)
        p = oid.parse_sn(dev.sn)                  # SN = CC-ORG-UNIQUE[-CHECK]
        body = f"{p['org']}-{p['unique']}"        # 校验位只算 ORG-UNIQUE（不含 CC）
        n = int(oid.compute_check_mod97(body)) + 1
        if n > 98:
            n = 2
        r["payload"]["sn"] = f"{p['cc']}-{body}-{n:02d}"   # 格式合法、校验位错
        return r, expect, note

    if kind == "unknown_sn":
        r = _legit(dev, now)
        r["payload"]["sn"] = oid.gen_sn(cc="CN", org="WH99", check="mod97")
        return r, expect, note

    if kind == "revoked":
        return _legit(dev, now), expect, note      # 调用方负责先 revoke

    if kind == "replay":
        n = used_nonce or "0" * 32
        return _legit(dev, now, nonce=n), expect, note

    if kind == "stale":
        return _legit(dev, now, ts=int(now) - 3600), expect, note

    if kind == "xport_tamper":
        r = _legit(dev, now)
        return oid.attach_xport(r, "DE:AD:BE:EF:00:02", "FAKE_AP", -20), expect, note

    return _legit(dev, now), expect, note


def run(dev, ks, now, used_nonces=None, attacker=None, replay_nonce=None,
        verbose=False, order=None):
    """把全部用例过一遍 `oid.verify_report` → [{kind, zh, expect, got, ok, note}]。

    只做**离线裁决**（不起链路）：适合单测与"防线清单"展示；
    真·端到端（经空口注入）见 `demo_spoof.py`。

    **与用例顺序无关**："已吊销"用例在一个**临时 keystore** 里把该 SN 吊销，
    传进来的 `ks` 全程不动。为什么必须如此：`unrevoke` 会把各代密钥置为 `retired`，
    而 `retired` 不参与验签（`verify_keys` 只收 active / 未过期 grace）→ 一旦
    "先跑 revoked 再 unrevoke"，后面所有用例都会变成 `unknown_device`（顺序一变就集体误判）。
    `order` 可自定义遍历顺序（测试用它证明顺序无关）。
    """
    used = used_nonces if used_nonces is not None else oid.NonceCache()
    kinds = list(order) if order else [c[0] for c in CASES]
    # 临时库：同一个 SN、同一套钥匙（demo_key 由 (sn, gen) 确定派生）+ 已吊销
    ks_revoked = oid.KeyStore()
    ks_revoked.register(dev)
    ks_revoked.revoke(dev.sn)
    rows = []
    # 「replay」需要一条**已用过**的 nonce：先真跑一条合法报文把它记进去
    if replay_nonce is None:
        probe = _legit(dev, now)
        replay_nonce = probe["payload"]["nonce"]
        oid.verify_report(probe, ks, now=now, used_nonces=used)
    for kind in kinds:
        report, expect, note = build_case(kind, dev, now, attacker=attacker,
                                         used_nonce=replay_nonce)
        v = oid.verify_report(report, ks_revoked if kind == "revoked" else ks,
                              now=now, used_nonces=used)
        got = None if v.get("accepted") else v.get("error")
        rows.append({"kind": kind, "zh": case_info(kind)[0],
                     "expect": expect, "got": got,
                     "ok": got == expect,
                     "trust": v.get("trust"), "note": note})
        if verbose:
            print(f"  [{'OK ' if got == expect else 'FAIL'}] {kind:16s} "
                  f"expect={expect} got={got}")
    return rows
