#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_spoof.py — 防 spoof 攻击清单的自检（离线裁决，不占端口/不起链路）。

验三件事：
  1) 每种攻击都**只**被预期的那道防线拦下（错误码逐条对得上）；
  2) 清单本身自洽（每项都有 zh/en/expect，UI 子集不含改密钥库状态的用例）；
  3) "合法对照"确实通过 —— 否则所有"拒绝"都说明不了问题。
端到端（经空口真注入）见 demo_spoof.py。
跑法：C:\\Python313\\python.exe test_spoof.py
"""
import copy
import sys
import time

import orpah_id as oid
import spoof

FAIL = []


def ck(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


print("== 1. 清单自洽 ==")
ck("用例数 >= 10（覆盖面）", len(spoof.CASES) >= 10, f"{len(spoof.CASES)} 条")
ck("kind 不重复", len(set(spoof.KINDS)) == len(spoof.KINDS))
ck("每项都有 kind/zh/en/expect/说明",
   all(len(c) == 5 and c[0] and c[1] and c[2] for c in spoof.CASES))
ck("期望通过的用例恰好 2 条（合法对照 + 已知边界 xport）",
   sum(1 for c in spoof.CASES if c[3] is None) == 2)
ck("UI 子集排除会改密钥库状态的 revoked",
   "revoked" not in spoof.UI_KINDS and "revoked" in spoof.KINDS)
ck("revoked 排在最后（它改状态，后面不能再有用例）",
   spoof.CASES[-1][0] == "revoked", spoof.CASES[-1][0])
ck("未知 kind 回落到 legit", spoof.case_info("nope")[0] == spoof.case_info("legit")[0])

print("== 2. 逐条裁决（离线） ==")
dev = oid.Device(sn="CN-WH01-9AF3C1D2", se_sn="ATECC608B-DEMO")
attacker = oid.Device(cc="CN", org="WHOFF")
ks = oid.KeyStore()
ks.register(dev, model="CH32V203+TX-AH+ATECC608B", firmware="1.0.3")
now = int(time.time())
rows = spoof.run(dev, ks, now, attacker=attacker)
for r in rows:
    ck(f"{r['kind']:16s} → {str(r['expect'])}", r["ok"], f"got={r['got']}")

print("== 3. 防线语义 ==")
by = {r["kind"]: r for r in rows}
ck("合法对照通过且 trust=high",
   by["legit"]["got"] is None and by["legit"].get("trust") == "high",
   f"trust={by['legit'].get('trust')}")
ck("改 ts / 改观测 / 改 hdr 都落在签名这一道（不是靠时间窗兜）",
   all(by[k]["got"] == "signature_invalid"
       for k in ("ts_tamper", "obs_tamper", "level_downgrade")))
ck("用别人的钥匙签被冒充设备的 SN → signature_invalid",
   by["sig_foreign"]["got"] == "signature_invalid")
ck("能力降级（改已签声明 cap.rtc）→ signature_invalid（声明在预像里，不能赖成“本来就没时钟”）",
   by["cap_downgrade"]["got"] == "signature_invalid")
# 直接把不变式测一步：cap 参与签名 → 改它必定验签失败；而不改的带 cap 报文照常通过
_p = dev.report(level=0, ts=now, cap={"rtc": True})
_ks = oid.KeyStore()
_ks.register(dev, model="CH32V203+TX-AH+ATECC608B", firmware="1.0.3")
_v1 = oid.verify_report(_p, _ks, now=now, used_nonces=oid.NonceCache())
_p2 = copy.deepcopy(_p)
_p2["payload"]["cap"]["rtc"] = False
_v2 = oid.verify_report(_p2, _ks, now=now, used_nonces=oid.NonceCache())
ck("带 cap 的合法 ID 报告能过签（cap 本身不影响合法性）",
   bool(_v1.get("accepted")), f"got={_v1.get('error')}")
ck("把 cap.rtc 改掉 → 验签失败（cap 在 JCS 预像内 = 防篡改的声明）",
   _v2.get("error") == "signature_invalid" and not _v2.get("accepted"),
   f"got={_v2.get('error')}")
ck("校验位错不查库就被挡", by["bad_check"]["got"] == "bad_check")
ck("免签冒充被压到 level3", by["alg_none"]["got"] == "none_requires_level3")
ck("吊销优先于验签", by["revoked"]["got"] == "revoked")
ck("重放被 nonce 去重挡下", by["replay"]["got"] == "replay_detected")
ck("超窗被时间窗挡下", by["stale"]["got"] == "timestamp_out_of_window")
ck("已知边界如实展示：路由器侧 xport 不进签名 → 通过",
   by["xport_tamper"]["got"] is None)

print("== 4. 攻击确实改动了报文（不是把合法报文原样发了一遍） ==")
legit, _, _ = spoof.build_case("legit", dev, now)
for kind in ("sig_foreign", "ts_tamper", "obs_tamper", "level_downgrade",
             "cap_downgrade", "alg_none", "bad_check", "unknown_sn", "stale"):
    r, _, _ = spoof.build_case(kind, dev, now, attacker=attacker)
    diff = (r.get("hdr") != legit.get("hdr")
            or r.get("payload") != legit.get("payload")
            or r.get("sig") != legit.get("sig"))
    ck(f"{kind:16s} 与合法报文不同", diff)
base, _, _ = spoof.build_case("legit", dev, now)
before = {k: copy.deepcopy(base[k]) for k in ("hdr", "payload", "sig")}
r_xt = oid.attach_xport(base, "DE:AD:BE:EF:00:02", "FAKE_AP", -20)
ck("xport 只加在最外层（hdr/payload/sig 逐个不变 → 所以验签照样通过）",
   bool(r_xt.get("xport")) and all(r_xt[k] == before[k] for k in before),
   str(r_xt.get("xport"))[:60])

print("== 5. 顺序无关（revoked 用临时库，不污染传进来的 keystore） ==")
ks3 = oid.KeyStore()
ks3.register(dev, model="CH32V203+TX-AH+ATECC608B", firmware="1.0.3")
rev_first = ["revoked"] + [k for k in spoof.KINDS if k != "revoked"]
rows3 = spoof.run(dev, ks3, now, attacker=attacker, order=rev_first)
bad3 = [r for r in rows3 if not r["ok"]]
ck("把 revoked 排到最前，逐条结论不变",
   not bad3, "；".join(f"{r['kind']}={r['got']}" for r in bad3))
probe, _, _ = spoof.build_case("legit", dev, now)
v = oid.verify_report(probe, ks3, now=now, used_nonces=oid.NonceCache())
ck("跑完一轮后主库仍能正常验签（没被 unrevoke 转成 retired）",
   bool(v.get("accepted")), f"got={v.get('error')}")
ck("revoked 排最前 vs 排最后，逐条 (kind, 裁决) 完全一致",
   sorted((r["kind"], r["got"]) for r in rows)
   == sorted((r["kind"], r["got"]) for r in rows3),
   "（与第 2 节的默认顺序对比；两侧顺序不同，故按集合比）")

print("== 6. nonce 缓存：有界 LRU（对齐 §5.5 建议）—— 刷量不能清空历史 ==")
# 为什么单独立一节：`verify_report` 是在**验签之前**把 nonce 记进缓存的（§9.3 第 3 步），
# 所以**任何人**都能用随机 nonce 刷缓存；而免电池设备 `ts=0` 时**时间窗不生效**（§5.5），
# nonce 去重是那时**唯一**的防重放手段。旧的 `set.clear()` 会把全部历史一次抹掉
# → 攻击者刷满 1024 条就能让"抓到的那条合法报文"的重放在 nonce 这一道消失（实测确认过）。
_sn6 = dev.sn
nc = oid.NonceCache(per_device=8)
for i in range(8):
    nc.add(_sn6, f"N{i:02d}")
ck("容量内不淘汰（装满 8 条都还在）", nc.size(_sn6) == 8)
nc.add(_sn6, "N08")
ck("超过上限只淘汰**最旧**的一条（N00 出局，N01..N08 全在）",
   (not nc.seen(_sn6, "N00")) and all(nc.seen(_sn6, f"N{i:02d}") for i in range(1, 9)),
   f"size={nc.size(_sn6)}")
ck("容量不会无限涨（有界）", nc.size(_sn6) == 8)

nc2 = oid.NonceCache(per_device=16)
nc2.add(_sn6, "ACCEPTED-THEN-FLOODED")
for i in range(15):                       # 攻击者刷随机 nonce（远不到容量）
    nc2.add(_sn6, f"JUNK{i}")
ck("★ 刷量到接近满桶：**最近受理过的 nonce 还在**（旧实现 set.clear() 会把它一起抹掉）",
   nc2.seen(_sn6, "ACCEPTED-THEN-FLOODED"), f"size={nc2.size(_sn6)}")

# 端到端：ts=0 设备（时间窗不生效）真重放一遍
_ks6 = oid.KeyStore()
_ks6.register(dev, model="CH32V203+TX-AH+ATECC608B", firmware="1.0.3")
used6 = oid.NonceCache(per_device=8)
_p6 = dev.report(level=0, ts=0)            # ts=0：跳过时间窗 → 全靠 nonce
_v6a = oid.verify_report(_p6, _ks6, now=now, used_nonces=used6)
ck("ts=0 的合法报文先被受理", bool(_v6a.get("accepted")), f"got={_v6a.get('error')}")
for i in range(6):                         # 刷 6 条垃圾（**不满桶**）
    _junk = copy.deepcopy(_p6)
    _junk["payload"]["nonce"] = f"JUNK6-{i}"
    oid.verify_report(_junk, _ks6, now=now, used_nonces=used6)   # 会 signature_invalid，但 nonce 已入缓存
_v6b = oid.verify_report(_p6, _ks6, now=now, used_nonces=used6)
ck("★ 期间被刷了 6 条（不满桶）后重放 → 仍被 nonce 挡下（replay_detected）",
   _v6b.get("error") == "replay_detected", f"got={_v6b.get('error')}")
# ★ 如实展示残留边界：容量有界 ⇒ 刷**超过容量**后，被挤出的旧 nonce 重放会被**放行**
#   （所以不能把 nonce 说成"已防住重放"；真正的防线是签名+时间窗+限频）
for i in range(20):                        # 这次刷满并超过容量
    _junk = copy.deepcopy(_p6)
    _junk["payload"]["nonce"] = f"FLOOD-{i}"
    oid.verify_report(_junk, _ks6, now=now, used_nonces=used6)
_v6c = oid.verify_report(_p6, _ks6, now=now, used_nonces=used6)
ck("已知边界（如实：旧 nonce 被挤出后，这条重放**会被受理** —— ts=0 设备上时间窗也帮不上）",
   bool(_v6c.get("accepted")) and _v6c.get("error") is None,
   f"got={_v6c.get('error')} accepted={_v6c.get('accepted')}")

print()
if FAIL:
    print(f"失败 {len(FAIL)} 项：" + "；".join(FAIL))
    sys.exit(1)
print("全部通过")
