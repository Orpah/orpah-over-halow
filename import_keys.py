#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_keys.py — 把**真机产线台账**里的公钥 + 降级 HMAC 钥登记进密钥库（`orpah.db`）。

## 为什么必须另写一个入口 ✗

现成的两条登记路都**登不了真机钥** ✗：

- `ui_server` 的 `action=issue` ⇒ `ks.register(oid.Device(sn=sn, gen=gen))`；
- `client_sim.register_keystore()` ⇒ 同样 `oid.Device(...)`。

`oid.Device(demo_key=…)` 的密钥是**进程内生成/演示派生**的 ✗（`demo_key=False` 也是**当场随机**、不接外部钥 ✗）
⇒ 用它登记一颗**真芯片**的公钥，只会往库里写一把**对不上**的钥 ✗✗（之后真机的报文全验不过 ✗）。

而 `orpah_id.KeyStore.register()` 只读对象的 4 个属性 ——
**`sn` / `pubkey` / `hmac_key` / `se_sn`** ✓ ⇒ 传一个"小壳对象"就能登真机钥 ✓
（**不必改协议层** ✓；本脚本就是这么做的 ✓）。

## 安全（这是一张"已发密钥的真相"表 ✗）

`AGENTS.md` 记着：**改密钥库会让已入库的密钥全部验不过** ✗（`signature_invalid`）⇒ 本脚本：

1. **默认 dry-run** ✓（只打印将写入什么；`--apply` 才写 ✓）；
2. `--apply` 之后**读回核对** ✓（公钥逐字节 + HMAC 指纹 ✓）—— 不靠"我说写了"自证 ✗；
3. SN 必须**能解析** ✓；且**默认拒绝**密钥库里没有的 SN ✓（防手滑造野钥 ✓），确要新增用 `--allow-new` ✓；
4. 该 SN **已有 active 钥且公钥不同** ⇒ **拒绝** ✓（`register()` 是幂等的、**不会更新** ✗ ⇒
   硬写会变成"库里一把、芯片里一把" ✗）；要换钥走 `rotate`（那是密钥生命周期的事，不在这里做 ✗）；
5. HMAC 钥**只打指纹** ✓（`sha256` 前 4 B ✓），**绝不打印钥本身** ✗；也不写进日志 ✓。

## 用法

    # ① 单个（命令行直接给）—— 真机台账里的公钥（hex）+ 那把钥文件
    python import_keys.py --sn CN-WH01-XXXXXXXXXA --pub-hex <128 hex> \\
        --hmac-key-file %USERPROFILE%\\orpah-se-keys\\se1.key --se-sn 0123b43800006003484dcf25ee

    # ② 批量（一个 spec JSON；台账与钥文件按部件走）
    python import_keys.py --spec se_batch.json

    # ③ 真写（加 --apply）；④ 离线自检（不碰真库）
    python import_keys.py --spec se_batch.json --apply
    python import_keys.py --selftest

spec JSON（批量）= 一个列表，每项：

    [{"sn": "CN-WH01-XXXXXXXXXA",
      "ledger": "C:/Users/…/orpah-se-keys/se1.ledger.txt",
      "hmac_key_file": "C:/Users/…/orpah-se-keys/se1.key",
      "model": "CH32V203+TX-AH+ATECC608B"}]

`ledger` = `orpah-client-demo/tools/se_provision.py --record` 写出来的**产线台账** ✓
（本脚本取其中**最新一条**带 `pubkey_hex=` 的行 ✓ 与 `se_sn=` 字段 ✓）。
`hmac_key_file` = 那颗 SE 的 32 B 降级钥（原始 32 B 或 64 位 hex 都行 ✓；**仓库外保管** ✗）。
"""
import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import keystore as ksdb                     # noqa: E402
import orpah_id as oid                      # noqa: E402

DEMO_MODEL = "CH32V203+TX-AH+ATECC608B"
SE_SN_PREFIX = "ATECC608B:"                 # 记真机 SE 时带上型号前缀（与演示的 `ATECC608B-DEMO` 区分 ✓）


# ---------------------------------------------------------------- 小工具
def fp_of(b):
    """指纹 = `sha256` 前 4 B（**不打印钥本身 ✗**，本仓一贯做法 ✓）。"""
    return hashlib.sha256(b).hexdigest()[:8] if b else "-"


def read_key_file(path):
    """32 B HMAC 钥：原始 32 B 或 64 位 hex 文本都收 ✓（两种写法都实测用过 ✓）。返回 bytes 或 None。"""
    if not path or not os.path.exists(path):
        return None
    raw = open(path, "rb").read()
    if len(raw) == 32:
        return raw
    hx = "".join(c for c in raw.decode("utf-8", "replace")
                 if c in "0123456789abcdefABCDEF")
    return bytes.fromhex(hx) if len(hx) == 64 else None


def read_ledger(path):
    """从产线台账里取**最新一条**带公钥的行 ⇒ `(pub64, se_sn | None, sn_in_ledger | None)`。

    ★ 取"最新"而不是"第一条" ✓：同一颗片子可能被跑过多次 `all`（续跑 ✓），**最后写下的才是当前那把** ✓
      （我们已把 `genkey` 做幂等 ✓，所以正常不会变 ✓；但按"最后一条"判总是对的 ✓）。
    """
    if not path or not os.path.exists(path):
        return None, None, None
    pub, se_sn, sn_in = None, None, None
    for line in open(path, encoding="utf-8", errors="replace"):
        f = {}
        for kv in line.rstrip("\n").split("\t"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                f[k.strip()] = v.strip()
        if f.get("pubkey_hex") and f["pubkey_hex"] != "-":
            pub = f["pubkey_hex"]
        if f.get("se_sn") and f["se_sn"] != "-":
            se_sn = f["se_sn"]
        if f.get("sn"):
            sn_in = f["sn"]
    return pub, se_sn, sn_in


def pub_obj(pub64):
    """64 B（X||Y）⇒ `cryptography` 公钥对象（库里存的就是这个类型 ✓，落库走 `pubkey_to_pem` ✓）。

    出处：`keystore._persist_key()` 对 `rec['pubkey']` 调 `oid.pubkey_to_pem()` ✓
    ⇒ **必须是 `cryptography` 对象**、不能是 bytes ✗（原厂 `verify_report` 也用它 ✓）。
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + pub64)


def pub64_of(obj):
    """公钥对象 ⇒ 64 B（X||Y）—— 读回核对时用（`UncompressedPoint` 去掉开头的 `0x04` ✓）。"""
    from cryptography.hazmat.primitives import serialization as ser
    return obj.public_bytes(ser.Encoding.X962, ser.PublicFormat.UncompressedPoint)[1:]


class DevShim:
    """`KeyStore.register()` 只认这 4 个属性 ✓ ⇒ 用它把**真机**的钥喂进去 ✓。"""

    def __init__(self, sn, pubkey, hmac_key, se_sn):
        self.sn, self.pubkey, self.hmac_key, self.se_sn = sn, pubkey, hmac_key, se_sn


# ---------------------------------------------------------------- 一条记录
def build_rec(spec_one, verbose=True):
    """把 spec 的一项变成 `(rec | None, 错误)`；`rec` 含 sn/pubkey 对象/hmac 钥/se_sn。"""
    sn = (spec_one.get("sn") or "").strip()
    if not oid.sn_ok(sn):
        return None, "SN 非法（%s）：%s" % (oid.sn_err(sn), sn or "(空)")
    pubhex, se_ledger, sn_ledger = read_ledger(spec_one.get("ledger"))
    if spec_one.get("pub_hex"):                    # 命令行给的优先 ✓（可与台账互校 ✓）
        pubhex = spec_one["pub_hex"].strip()
    if not pubhex:
        return None, "拿不到公钥：台账里没有带 pubkey_hex 的行，也没给 --pub-hex"
    hx = "".join(c for c in pubhex if c in "0123456789abcdefABCDEF")
    if len(hx) != 128:
        return None, "公钥不是 128 位 hex（应为 X||Y 各 32 B = 64 B；拿到 %d 位）" % len(hx)
    key = read_key_file(spec_one.get("hmac_key_file"))
    if key is None or len(key) != 32:
        return None, "HMAC 钥文件读不到 / 不是 32 B：%s" % spec_one.get("hmac_key_file")
    se_sn = (spec_one.get("se_sn") or se_ledger or "-").strip()
    if sn_ledger and sn_ledger != sn and verbose:
        print("  ⚠ 台账里记的 sn=%s 与本次 `--sn %s` **不一致** ✗（以本次为准 ✓，请核一遍）"
              % (sn_ledger, sn))
    try:
        obj = pub_obj(bytes.fromhex(hx))
    except Exception as e:                                     # noqa: BLE001
        return None, "公钥不是合法的 P-256 点：%r" % (e,)
    return {"sn": sn, "pubkey": obj, "pub64": bytes.fromhex(hx), "hmac_key": key,
            "se_sn": (SE_SN_PREFIX + se_sn) if se_sn and se_sn != "-" else "",
            "model": spec_one.get("model") or DEMO_MODEL}, None


def check_existing(ks, rec, allow_new):
    """写之前的状态检查 ⇒ `("ok" | "skip" | "refuse", 说明)`。"""
    sn = rec["sn"]
    cur = ks.active_of(sn)
    if cur is None:
        if not ks.list_keys(sn) and not allow_new:
            return "refuse", ("密钥库里没有这个 SN ⇒ **默认拒绝**（防手滑造野钥 ✗）；"
                              "确要新增请显式加 `--allow-new` ✓")
        return "ok", "将签发第 1 代（gen=1）"
    same = (cur.get("pubkey") is not None
            and pub64_of(cur["pubkey"]) == rec["pub64"])
    if same and (cur.get("hmac_key") or b"") != rec["hmac_key"]:
        # 公钥一样、钥不一样 ⇒ 只更新降级钥（那是**同一代**的对称钥 ✓）—— 如实报出来 ✓
        return "ok-update-hmac", "公钥相同、HMAC 钥不同 ⇒ 会**更新同一代的降级钥** ✓"
    if same:
        return "skip", "已经登过、公钥逐字节相同 ✓（幂等 ⇒ 本项不动 ✓）"
    return "refuse", ("该 SN 已有 active 钥且**公钥不同** ✗ ⇒ 拒绝（`register()` 是幂等的、"
                      "**不会覆盖** ✗）。要换钥走 `rotate` ✓；别在这里硬写 ✗")


def apply_one(ks, rec, verbose=True):
    """写库 + **读回核对**（公钥逐字节 ✓ + HMAC 指纹 ✓）⇒ `(ok, 说明)`。"""
    ks.register(DevShim(rec["sn"], rec["pubkey"], rec["hmac_key"], rec["se_sn"]),
                model=rec["model"], ts=int(datetime.datetime.now().timestamp()))
    cur = ks.active_of(rec["sn"])
    if cur is None:
        return False, "写完读不回来 ✗"
    if pub64_of(cur["pubkey"]) != rec["pub64"]:
        return False, ("读回的公钥与要写的不一致 ✗（库里 %s… / 期望 %s…）"
                       % (fp_of(pub64_of(cur["pubkey"])), fp_of(rec["pub64"])))
    if (cur.get("hmac_key") or b"") != rec["hmac_key"]:
        return False, ("读回的 HMAC 钥与给的**指纹不同** ✗（库里 %s… / 期望 %s…；"
                       "两边都是指纹，不看钥本身 ✓）"
                       % (fp_of(cur.get("hmac_key") or b""), fp_of(rec["hmac_key"])))
    if verbose:
        print("      kid=%s gen=%d state=%s se_sn=%s model=%s"
              % (cur["kid"], cur["gen"], cur["state"], cur.get("se_sn") or "-",
                 cur.get("model") or "-"))
    return True, "读回核对通过 ✓（公钥逐字节相同 ✓、HMAC 指纹相同（%s…）✓）" % fp_of(rec["hmac_key"])


# ---------------------------------------------------------------- 主流程
def run(args, specs):
    rc = 0
    if not os.path.exists(args.db):
        # ★ 库还不存在：dry-run **不建库** ✓（“看一眼”不该有副作用 ✗）；如果加了 `--apply`
        #   则 `KeyStoreDB` 会顺手把库与建表建出来 ✓（那是预期行为 ✓）。
        print("（密钥库 %s 还不存在：dry-run 不建库 ✓；`--apply` 会新建 ✓）" % args.db)
    ks = ksdb.KeyStoreDB(args.db) if (args.apply or os.path.exists(args.db)) else None
    if not args.apply:
        print("★ **dry-run**（不会写库 ✓；确认无误后加 `--apply` ✓）")
    print("=" * 78)
    print("密钥库 = %s   模式 = %s" % (args.db, "apply（真写）" if args.apply else "dry-run"))
    for i, one in enumerate(specs, 1):
        print("-" * 78)
        print("▶ [%d/%d] sn=%s" % (i, len(specs), (one.get("sn") or "(空)")))
        rec, err = build_rec(one)
        if rec is None:
            print("  FAIL %s" % err)
            rc = 2
            continue
        print("  公钥 fingerprint（sha256 前 4 B）= %s…" % fp_of(rec["pub64"]))
        print("  HMAC 钥指纹 = %s…（**不打印钥本身 ✓**）" % fp_of(rec["hmac_key"]))
        print("  SE 序列号（台账 se_sn）= %s" % (rec["se_sn"] or "-"))
        if ks is None:                       # 库不存在且 dry-run ⇒ 没法判状态（如实说 ✓）
            print("  （库还不存在 ⇒ 状态检查做不了；`--apply` 时会新建并直接签发第 1 代 ✓）")
            continue
        verdict, why = check_existing(ks, rec, args.allow_new)
        print("  状态检查：%s ⇒ %s" % (verdict, why))
        if verdict == "refuse":
            print("  FAIL 拒绝（见上）✗")
            rc = 2
            continue
        if verdict == "skip":
            print("  PASS 无需动作 ✓")
            continue
        if not args.apply:
            print("  PASS 可以写 ✓（dry-run ⇒ 本次没写 ✓）")
            continue
        ok, note = apply_one(ks, rec)
        print("  %s %s" % ("PASS" if ok else "FAIL", note))
        if not ok:
            rc = 2
    print("=" * 78)
    print("%s：共 %d 项" % ("PASS" if rc == 0 else "FAIL", len(specs)))
    return rc


def selftest():
    """离线自检（**临时库**，不碰真 `orpah.db` ✓）：dry-run / 幂等 / 拒绝 / 读回核对 都过一遍。"""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization as ser
    bad = 0
    tmp = os.path.join(tempfile.gettempdir(), "import_keys_selftest.db")
    for p in (tmp, tmp + ".ledger.txt", tmp + ".key"):
        if os.path.exists(p):
            os.remove(p)
    dbp = tmp
    # 造一颗"假真机"：真 P-256 公钥 + 32 B 钥 + 一份产线台账
    priv = ec.generate_private_key(ec.SECP256R1())
    pub64 = priv.public_key().public_bytes(ser.Encoding.X962,
                                          ser.PublicFormat.UncompressedPoint)[1:]
    key = os.urandom(32)
    open(tmp + ".key", "wb").write(key)
    sn = oid.gen_sn(cc="CN", org="WH01", unique_len=10, check="mod97")
    open(tmp + ".ledger.txt", "w", encoding="utf-8").write(
        "2026-01-01 00:00:00\taddr=0x60\tstep=all\tpubkey_hex=%s\thmac_fp=%s\tse_sn=%s\n"
        % (pub64.hex(), fp_of(key), "0123b43800006003484dcf25ee"))
    spec = [{"sn": sn, "ledger": tmp + ".ledger.txt", "hmac_key_file": tmp + ".key"}]

    def ck(name, cond):
        nonlocal bad
        print("  %s %s" % ("ok  " if cond else "BAD ", name))
        bad += 0 if cond else 1

    print("== ① 状态检查：新 SN + 不给 --allow-new ⇒ 必须拒绝 ==")
    ks = ksdb.KeyStoreDB(dbp)
    rec, err = build_rec(spec[0])
    ck("台账/钥文件都读出来了", rec is not None and err is None)
    v, _why = check_existing(ks, rec, allow_new=False)
    ck("拒绝建野钥（unknown_sn 风格）", v == "refuse")
    v, _why = check_existing(ks, rec, allow_new=True)
    ck("--allow-new 后放行", v == "ok")

    print("== ② 写库 + 读回核对 ==")
    ok, note = apply_one(ks, rec)
    ck("apply + 读回核对通过", ok)
    cur = ks.active_of(sn)
    ck("库里公钥逐字节相同", cur is not None and pub64_of(cur["pubkey"]) == rec["pub64"])
    ck("库里 HMAC 钥逐字节相同", (cur.get("hmac_key") or b"") == key)
    ck("se_sn 带上了型号前缀", (cur.get("se_sn") or "").startswith(SE_SN_PREFIX))

    print("== ③ 重启读回（落库真的持久 ✓）==")
    ks2 = ksdb.KeyStoreDB(dbp)
    cur2 = ks2.active_of(sn)
    ck("重开库后还在、公钥不变", cur2 is not None and pub64_of(cur2["pubkey"]) == rec["pub64"])
    v, why = check_existing(ks2, rec, allow_new=False)
    ck("幂等：再写一次是 skip（不造新代）", v == "skip")

    print("== ④ 同 SN、公钥不同 ⇒ 必须拒绝（不覆盖 ✓）==")
    priv2 = ec.generate_private_key(ec.SECP256R1())
    pub64b = priv2.public_key().public_bytes(ser.Encoding.X962,
                                             ser.PublicFormat.UncompressedPoint)[1:]
    rec2 = dict(rec)
    rec2["pubkey"] = priv2.public_key()
    rec2["pub64"] = pub64b
    v, why = check_existing(ks2, rec2, allow_new=False)
    ck("拒绝覆盖（要换钥请走 rotate）", v == "refuse")
    ck("库里那把没被动过", pub64_of(ks2.active_of(sn)["pubkey"]) == rec["pub64"])

    print("== ⑤ 坏输入：SN 非法 / 公钥位数不对 / 钥文件不是 32 B ==")
    r, e = build_rec({"sn": "bad-sn", "ledger": tmp + ".ledger.txt",
                      "hmac_key_file": tmp + ".key"})
    ck("非法 SN 被拒", r is None and "SN 非法" in e)
    r, e = build_rec({"sn": sn, "pub_hex": "ab" * 10, "hmac_key_file": tmp + ".key"})
    ck("公钥位数不对被拒", r is None and "128 位 hex" in e)
    open(tmp + ".key17", "wb").write(os.urandom(17))
    r, e = build_rec({"sn": sn, "ledger": tmp + ".ledger.txt",
                      "hmac_key_file": tmp + ".key17"})
    ck("钥文件不是 32 B 被拒", r is None and "32 B" in e)

    print("== ⑥ 库里状态不被 selftest 之外的东西动过（收尾核对）==")
    ck("总共只签发了 1 代", len(ks2.list_keys(sn)) == 1)
    for p in (tmp, tmp + ".ledger.txt", tmp + ".key", tmp + ".key17"):
        try:
            os.remove(p)
        except OSError:
            pass
    print("%s import_keys 离线自检" % ("PASS" if bad == 0 else "FAIL（%d 项）" % bad))
    return 0 if bad == 0 else 2


def main():
    ap = argparse.ArgumentParser(description="把真机产线台账的公钥/HMAC 钥登记进密钥库（默认 dry-run）")
    ap.add_argument("--db", default=os.path.join(HERE, "orpah.db"),
                    help="密钥库 SQLite（缺省 = 本目录 orpah.db ✓）")
    ap.add_argument("--spec", help="批量 spec JSON（列表；每项 sn/ledger/hmac_key_file[/model] ✓）")
    ap.add_argument("--pub-hex", help="（单个模式）公钥 hex = X||Y 各 32 B")
    ap.add_argument("--hmac-key-file", help="（单个模式）那颗 SE 的 32 B 降级钥文件")
    ap.add_argument("--sn", help="（单个模式）设备 SN（`CC-ORG-UNIQUE[-CHECK]` ✓）")
    ap.add_argument("--se-sn", help="（单个模式）SE 自己的序列号 hex（`cfg[0..12]` ✓，台账里有 ✓）")
    ap.add_argument("--model", help="型号串（缺省 %s）" % DEMO_MODEL)
    ap.add_argument("--allow-new", action="store_true",
                    help="允许给**库里还没有**的 SN 建钥（默认拒绝，防手滑 ✗）")
    ap.add_argument("--apply", action="store_true", help="真写（缺省 = dry-run ✓）")
    ap.add_argument("--selftest", action="store_true", help="离线自检（临时库，不碰真库 ✓）")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.spec:
        with open(args.spec, encoding="utf-8") as f:
            specs = json.load(f)
        if not isinstance(specs, list):
            print("FAIL spec 必须是**列表** ✗（每项一个设备）")
            return 1
    elif args.sn and (args.pub_hex or args.hmac_key_file):
        specs = [{"sn": args.sn, "pub_hex": args.pub_hex,
                  "hmac_key_file": args.hmac_key_file, "se_sn": args.se_sn,
                  "model": args.model}]
    else:
        print("FAIL 要么给 `--spec <json>`（批量），要么给 `--sn` + `--pub-hex`/`--hmac-key-file`；"
              "离线自检用 `--selftest`")
        return 1
    return run(args, specs)


if __name__ == "__main__":
    sys.exit(main())
