#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_attack.py — 攻击流量独立面板（`attack.html` + `ui_server` 的攻击通道）离线自检
=====================================================================================
为什么单独一个套件：这个面板把「攻击报文」从**正常签名上报流**里分了出来
（旧做法是让页面从 `id_reports` 里猜哪条是攻击 —— ROADMAP §四那条未做项的由来）。
分开靠的是 **nonce 认领**：注入时记下 nonce，验签结果回来时对上。这条链路一旦错位，
现象很难看：面板要么把正常周期上报当成攻击（假流量），要么把攻击结果记到别的用例头上
（张冠李戴），而且**两种都不会报错**。所以这里把它钉住。

盯五件事：
  ① **认领必须精准**：只有 nonce 对得上的才进攻击流量；对不上的（正常上报）一条都不能进；
  ② **没等到结果 ≠ 被拒 ≠ 通过**：超时条目单独成 state=lost，`ok=None`（不假装判定过）；
  ③ **单一源**：清单/说明/防线/「错误码 → 哪道防线」都只在 `spoof.py`；
     页面（`attack.js`）**不许**出现攻击类型名或拒绝码字面量（否则两份映射必然漂移）；
  ④ **防线映射完备**：`orpah_id.verify_report` 里每一个 `_reject("<码>")` 都映射得出来
     （否则面板会显示「未归类」—— 加新防线时忘了改这里就当场发现）；
  ⑤ **口径**：说明文案中英两套、都不许写 Markdown（会原样上页面）、不许出现归因性词。

不占端口、不起链路、不碰 orpah.db：`ui_server.OrpahApp` 的这几个方法只在内存里记账，
用一个**空壳对象**（只放它们读的那几个属性）直接调用，不构造真的 App。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import spoof                                   # noqa: E402
import orpah_id as oid                         # noqa: E402
import ui_server                               # noqa: E402

STATIC = os.path.join(HERE, "ui", "static")
FAILS = []


def ck(name, cond, extra=""):
    if cond:
        print("PASS  " + name)
    else:
        print("FAIL  " + name + ("   " + str(extra) if extra else ""))
        FAILS.append(name)


def read(fn):
    with open(os.path.join(STATIC, fn), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
class _Stub:
    """空壳 App：只放 `_spoof_*` 系列读写的属性（不构造真的 OrpahApp → 不碰 orpah.db）。"""

    SPOOF_WAIT = ui_server.OrpahApp.SPOOF_WAIT

    def __init__(self):
        self.spoof_pending = {}
        self.spoof_results = {}
        self.spoof_events = __import__("collections").deque(maxlen=60)
        self.spoof_injected = 0
        self.spoof_lost = 0

    # 绑上真方法（未绑定函数 + 本对象当 self）
    _spoof_row = ui_server.OrpahApp._spoof_row
    _spoof_claim = ui_server.OrpahApp._spoof_claim
    _spoof_sweep = ui_server.OrpahApp._spoof_sweep
    _spoof_track = ui_server.OrpahApp._spoof_track
    spoof_view = ui_server.OrpahApp.spoof_view
    spoof_reset = ui_server.OrpahApp.spoof_reset


def report_rec(kind, nonce, accepted, error=None, sn="CN-WH01-9AF3C1D2"):
    """一条 Server 侧验签结果（形状对齐 `OrpahServer.on_id_report` 回调的那份）。"""
    return {"t": "12:00:00", "sn": sn, "alg": "ES256", "level": 0,
            "trust": "high" if accepted else "none", "accepted": accepted,
            "error": error, "nonce": nonce}


# ---------------------------------------------------------------------------
print("== 1. 用例清单（单一源 spoof.py）==")
kinds = [c[0] for c in spoof.CASES]
ck("用例字段仍是 5 项（脚本/演示都按这个解包）",
   all(len(c) == 5 for c in spoof.CASES), str([len(c) for c in spoof.CASES]))
ck("说明是中英两套且都非空",
   all(isinstance(c[4], tuple) and len(c[4]) == 2 and c[4][0] and c[4][1]
       for c in spoof.CASES))
ck("说明里不写 Markdown（会原样显示成星号）",
   not any("**" in c[4][0] or "**" in c[4][1] for c in spoof.CASES),
   [c[0] for c in spoof.CASES if "**" in c[4][0] + c[4][1]])
ck("说明里不出现归因性词（AGENTS §0 术语硬规则）",
   not any(re.search(r"谎报|撒谎|欺骗|作弊|有罪", c[4][0] + c[4][1])
           for c in spoof.CASES))
ck("UI 子集排除 revoked（页面用活密钥库，跑一次会把在跑的设备搞成验不过）",
   "revoked" not in spoof.UI_KINDS and "revoked" in kinds)
ck("UI 子集顺序 = 全量顺序（去掉 revoked）",
   spoof.UI_KINDS == [k for k in kinds if k != "revoked"])
ck("case_note 按语言取、未知 kind 不崩",
   spoof.case_note("bad_format", "en") != spoof.case_note("bad_format", "zh")
   and bool(spoof.case_note("no-such-kind", "en")))

print("\n== 2. 「是哪一道防线拦的」映射完备（单一源 = spoof.DEFENSES）==")
src = open(os.path.join(HERE, "orpah_id.py"), encoding="utf-8").read()
codes = sorted(set(re.findall(r'_reject\(\s*"([a-z0-9_]+)"', src)))
ck("从 orpah_id 抽到拒绝码（抽样正则没跑偏）", len(codes) >= 10, codes)
unmapped = [c for c in codes if spoof.defense_of(c) == "other"]
ck("每个拒绝码都映射到某道防线（新码没登记就会当场露出来）", not unmapped, unmapped)
ck("防线 id 集合 = 映射值 ∪ {accept}（没有写不出来的防线）",
   set(spoof.LINE_IDS) == set(spoof.LINE_OF.values()) | {"accept"},
   sorted(set(spoof.LINE_IDS) ^ (set(spoof.LINE_OF.values()) | {"accept"})))
ck("通过（None）算「未被拦」而不是某道防线", spoof.defense_of(None) == "accept")
ck("未知码 → 未归类（不瞎猜成某道防线）", spoof.defense_of("no_such_code") == "other")
ck("每条用例的期望都落在已有防线上（没有用例指望一道不存在的防线）",
   all(spoof.defense_of(c[3]) in spoof.LINE_IDS for c in spoof.CASES))
ck("第一道防线（格式）有用例指了指它（2026-09-13 补的 bad_format）",
   any(spoof.defense_of(c[3]) == "format" for c in spoof.CASES))

print("\n== 3. 认领：只有攻击报文进攻击流量流 ==")
st = _Stub()
st._spoof_track("N1", "sig_foreign", "signature_invalid", "CN-WH01-9AF3C1D2")
st._spoof_claim(report_rec("x", "NOT-OURS", False, "signature_invalid"))
ck("★ 不是我们注入的（nonce 对不上）一条都不进",
   not st.spoof_results and not st.spoof_events)
st._spoof_claim(report_rec("x", "N1", False, "signature_invalid"))
row = st.spoof_results.get("sig_foreign")
ck("★ nonce 对得上 → 记进结果表与流量流",
   bool(row) and len(st.spoof_events) == 1)
ck("实际裁决/防线/是否与期望一致都算对",
   row["got"] == "signature_invalid" and row["line"] == "sig" and row["ok"] is True
   and row["state"] == "blocked")
ck("认领后 pending 里不再留着它（不重复认领）", "N1" not in st.spoof_pending)

st2 = _Stub()
st2._spoof_track("N2", "xport_tamper", None, "CN-WH01-9AF3C1D2")
st2._spoof_claim(report_rec("x", "N2", True))
r2 = st2.spoof_results["xport_tamper"]
ck("期望「接受」的用例通过 → ok=True、防线=accept（已知边界那条照实算通过）",
   r2["ok"] is True and r2["state"] == "accepted" and r2["line"] == "accept"
   and r2["got"] is None)

st3 = _Stub()
st3._spoof_track("N3", "unknown_sn", "unknown_device")
st3._spoof_claim(report_rec("x", "N3", True))       # 该被拒却通过了 → 不一致
r3 = st3.spoof_results["unknown_sn"]
ck("实际与期望不符 → ok=False（不粉饰成“反正被拒了”）",
   r3["ok"] is False and r3["accepted"] is True)

print("\n== 4. 没等到结果：既不算被拒也算通过 ==")
st4 = _Stub()
st4._spoof_track("N4", "stale", "timestamp_out_of_window")
st4.spoof_pending["N4"]["t"] -= (st4.SPOOF_WAIT + 1)     # 往回拨到超时
st4._spoof_sweep()
r4 = st4.spoof_results["stale"]
ck("超时条目 → state=lost、ok=None（**不假装判定过**）",
   r4["state"] == "lost" and r4["ok"] is None and r4["got"] is None)
ck("超时条目的防线列是 wait（根本不是走到验签，别算成某道防线）",
   r4["line"] == "wait" and r4["line"] not in spoof.LINE_IDS)
ck("超时计数 +1 且从 pending 里清掉", st4.spoof_lost == 1 and not st4.spoof_pending)
st4._spoof_sweep()
ck("再扫一遍不会重复计数（幂等）", st4.spoof_lost == 1 and len(st4.spoof_events) == 1)

st5 = _Stub()
st5._spoof_track("N5", "replay", "replay_detected")
st5._spoof_sweep()
ck("还没到等待上限的条目**不许**被扫成 lost",
   not st5.spoof_results and st5.spoof_lost == 0 and "N5" in st5.spoof_pending)

st6 = _Stub()
for i in range(80):
    st6._spoof_track("P%03d" % i, "legit", None)
ck("pending 有上限（键是攻击者可选的 nonce，只增不减会攒着）",
   len(st6.spoof_pending) == 64, len(st6.spoof_pending))
ck("丢掉的是最老的（最近注入的还认得出来）",
   "P079" in st6.spoof_pending and "P000" not in st6.spoof_pending)

st8 = _Stub()
st8._spoof_track("N8", "legit", None)
st8._spoof_claim(report_rec("x", "N8", True))
st8._spoof_sweep()
ck("已认领的条目不会被 sweep 再记成 lost（扫与认领共用同一张表、不同线程）",
   st8.spoof_lost == 0 and len(st8.spoof_events) == 1)
st9 = _Stub()
st9._spoof_track("N9", "legit", None)
st9._spoof_claim(report_rec("x", "N9", True))          # 先认领掉一条
st9._spoof_track("N10", "stale", "timestamp_out_of_window")
st9.spoof_pending["N10"]["t"] -= (st9.SPOOF_WAIT + 1)
st9._spoof_sweep()
ck("认领掉一条之后，仍能扫出真正超时的另一条（防竞态没变成漏扫）",
   st9.spoof_lost == 1 and st9.spoof_results["stale"]["state"] == "lost")

print("\n== 5. 视图与清空 ==")
st7 = _Stub()
st7._spoof_track("N7", "bad_check", "bad_check", "CN-WH01-9AF3C1D2")
st7.spoof_injected = 3
v = st7.spoof_view()
ck("视图给出注入数/未等到结果数/等待上限（页面不写死阈值）",
   v["injected"] == 3 and v["lost"] == 0 and v["wait"] == st7.SPOOF_WAIT)
ck("等待中的条目带 kind/nonce/期望（页面能显示「等结果…」）",
   v["waiting"] and v["waiting"][0]["kind"] == "bad_check"
   and v["waiting"][0]["nonce"] == "N7")
ck("防线清单随视图下发（页面不另写一份）",
   [d["id"] for d in v["defenses"]] == spoof.LINE_IDS
   and all(d["zh"] and d["en"] for d in v["defenses"]))
st7._spoof_claim(report_rec("x", "N7", False, "bad_check"))
st7.spoof_reset()
ck("清空面板：结果/流量/计数/pending 都归零",
   not st7.spoof_results and not st7.spoof_events and not st7.spoof_pending
   and st7.spoof_injected == 0 and st7.spoof_lost == 0)

print("\n== 6. 页面守卫（单一源 + 入口 + id 对齐）==")
js = read("attack.js")
html = read("attack.html")
idx = read("index.html")

hard = sorted(set(re.findall(
    r'"(signature_invalid|bad_check|replay_detected|timestamp_out_of_window|'
    r'unknown_device|none_requires_level3|bad_typ|revoked)"', js)))
ck("★ attack.js 里没有拒绝码字面量（防线映射只在 spoof.py，页面不抄一份）",
   not hard, hard)
hard2 = sorted(k for k in kinds if ('"%s"' % k) in js)
ck("★ attack.js 里没有攻击类型名（清单只从 /api/status.spoof_kinds 取）",
   not hard2, hard2)
ck("attack.js 确实去取清单与状态（走接口，不内置数据）",
   "spoof_kinds" in js and "/api/status" in js and "spoof_reset" in js)
ck("attack.js 不许出现 trust 判定的另一份实现（那是 pos.js 的事）",
   "consensus(" not in js and "TRUST_COLOR" not in js)

ids = sorted(set(re.findall(r'\$\(\s*"([^"]+)"\s*\)', js)))
missing = [i for i in ids if ('id="%s"' % i) not in html]
ck("attack.js 用到的元素 id 在 attack.html 里都存在（%d 个）" % len(ids),
   not missing, missing)
ck("attack.html 有返回主页/密钥页的入口与语言按钮",
   'href="index.html"' in html and 'id="langBtn"' in html)
ck("首页导航与卡片都能到 attack.html",
   idx.count('href="attack.html"') >= 2, idx.count('href="attack.html"'))
ck("面板把三条口径写在页面上（被拒≠防住了 / xport 边界 / 未等到结果）",
   all(k in html for k in ("at_hint_notproof", "at_hint_xport", "at_hint_ratelimit")))
ck("面板说明了攻击流量与正常上报流是两条流", "at_hint_sep" in html)

print("\n== 7. 文档与自检接入 ==")
rc = open(os.path.join(HERE, "run_checks.py"), encoding="utf-8").read()
ck("run_checks.py 已接入本套件", "test_attack.py" in rc)

print("\n" + ("全部通过" if not FAILS else "失败 %d 项：%s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
