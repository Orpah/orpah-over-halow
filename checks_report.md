# ORPAH 批量合规测试报告

- 时间：2026-09-13 10:04:35
- git HEAD：`9041071`
- 解释器：3.13.14 @ C:\Python313\python.exe
- 结论：**全部通过**（15/15 套件通过）

## 套件结果

| 套件 | 脚本 | 结果 | 耗时 | 说明 |
|---|---|---|---|---|
| 运动/定位数据源 | `test_motion.py` | ✅ PASS | 0.1s | 通过 |
| 密钥生命周期 | `test_keys.py` | ✅ PASS | 0.1s | 通过 |
| 防 spoof（离线逐条） | `test_spoof.py` | ✅ PASS | 0.1s | 通过 |
| 告警规则（含处置态） | `test_alerts.py` | ✅ PASS | 0.1s | 通过 |
| 指标面板纯计算 | `test_metrics.py` | ✅ PASS | 0.1s | 通过 |
| 时钟可信（ts=0 无 RTC） | `test_clock.py` | ✅ PASS | 0.5s | 通过 |
| IoTDB 审计/时间窗 | `test_tsdb_audit.py` | ✅ PASS | 0.5s | 通过 |
| UI 服务器契约 | `test_server.py` | ✅ PASS | 0.8s | 通过 |
| 批量合规用例（黄金样本/SN 边界/报文） | `checks_batch.py` | ✅ PASS | 0.1s | 通过 |
| 降级策略（§8.2 选级 / §8.3 服务端） | `test_levels.py` | ✅ PASS | 0.1s | 通过 |
| 能量轴（免电池客户端） | `test_energy.py` | ✅ PASS | 0.0s | 通过 |
| 抓包解析 / 双源对照 | `test_capture.py` | ✅ PASS | 0.1s | 通过 |
| 设备时钟漂移（长基线） | `demo_clock.py` | ✅ PASS | 0.1s | 通过 |
| 定位内核（pos.js，node 跑原文） | `test_posjs.py` | ✅ PASS | 0.2s | 通过 |
| 文案字典（zh/en 一致 + 页面引用无缺失） | `test_i18n.py` | ✅ PASS | 0.1s | 通过 |

## 关键输出

### 运动/定位数据源 — PASS（0.1s）

    == 1. 构造与几何 ==
    == 2. pos 确定性与周期性 ==
    == 3. 连续性（不瞬移） ==
    == 4. RSSI 路径损耗换算 ==
    == 4b. 钳位要盖住加噪后的结果（2026-09-12 审查） ==
    == 5. 多路由器一致（演示数据可用性） ==
      OK   truth_at：空输入 → 空表（页面显示“不适用”，不编 0）
    全部通过

### 密钥生命周期 — PASS（0.1s）

    == 1. 签发（幂等） ==
    == 2. 验签（第 1 代） ==
    == 3. 轮换：旧钥进宽限、新旧都能验 ==
    == 4. 宽限到期 → 退役 ==
    == 5. grace_sec=0：旧钥立即失效 ==
    == 6. 提前强制退役 ==
      OK   另一代（gen=2）签的报文用第 1 代的库验 → 拒（代次不能串）
    all key lifecycle tests passed

### 防 spoof（离线逐条） — PASS（0.1s）

    == 1. 清单自洽 ==
    == 2. 逐条裁决（离线） ==
    == 3. 防线语义 ==
    == 4. 攻击确实改动了报文（不是把合法报文原样发了一遍） ==
    == 5. 顺序无关（revoked 用临时库，不污染传进来的 keystore） ==
      OK   revoked 排最前 vs 排最后，逐条 (kind, 裁决) 完全一致  （与第 2 节的默认顺序对比；两侧顺序不同，故按集合比）
    全部通过

### 告警规则（含处置态） — PASS（0.1s）

    PASS  长未上报：启用/走失中的都报，停用/报废不报
    PASS  长未上报：带 gap 且等级 warn
    PASS  C：默认 no_report 升级阈值 300s（ORPAH_ALERT_NO_REPORT_CRIT_SEC）
    PASS  C：默认 case_handled 升级阈值 48h（ORPAH_ALERT_CASE_HANDLED_CRIT_SEC；注意与起步阈值 CASE_HANDLED_SEC=24h 是两个不同的量）
    PASS  C：刚过起步但未到升级点 → warn
    PASS  C：超过升级点 → crit

### 指标面板纯计算 — PASS（0.1s）

    PASS  签名：总数/通过/被拒
    PASS  签名：失败率 1/4
    PASS  签名：算法分布
    PASS  签名：其它类型事件不进统计
    PASS  签名：取不到 alg → unknown（不猜）
    PASS  签名：无样本 → fail_ratio=None（不编 0）

### 时钟可信（ts=0 无 RTC） — PASS（0.5s）

    == 1. effective_ts 归一化规则（唯一入口，各层共用）==
    == 1b. 能力声明 cap（设备自报“有无 RTC”）==
    == 1c. ts_usable：判断“设备的时间能不能用” ==
    == 2. 验签的时间窗（ts=0 跳过窗口，仅靠 nonce 防重放）==
    == 3. 端到端（进程内 UDP）：ts=0 的报告落库时刻 + 审计留痕 ==
    == 4. 设备时钟偏移/漂移估计 ==
      OK   并发：4 写 × 50 条 + 2 读线程 → 无异常且一条不丢
    时钟可信测试全部通过

### IoTDB 审计/时间窗 — PASS（0.5s）

    == 1. actor 字段 ==
    == 2. query_events 回读 actor ==
    == 3. 保留期限清理 ==
    == 4. 时间窗查询（回放用） ==
    == 6. 路由器侧观测（多路由器定位的数据源） ==
    == 7. 环境变量解析 ==
      OK   合法值生效
    全部通过

### UI 服务器契约 — PASS（0.8s）

    [server] [id 1] ORPAH-ID-REPORT <- 127.0.0.1:12345: sn=CN-WH01-9AF3C1D2 alg=ES256 level=0 trust=high accepted=True
    [server] [id 1] ORPAH-ID-REPORT <- 127.0.0.1:12345: sn=CN-WH01-9AF3C1D2 alg=ES256 level=0 trust=high accepted=True
    [server] [id 2] ORPAH-ID-REPORT <- 127.0.0.1:12345: sn=CN-WH01-9AF3C1D2 alg=ES256 level=0 trust=high accepted=True
    [server] [id 3] ORPAH-ID-REPORT <- 127.0.0.1:12345: sn=CN-WH01-9AF3C1D2 alg=ES256 level=0 trust=high accepted=True
    [server] [id 1] ORPAH-ID-REPORT <- 127.0.0.1:12345: sn=CN-WH01-9AF3C1D2 alg=ES256 level=0 trust=high accepted=True
    [server] [id 1] ORPAH-ID-REPORT <- 127.0.0.1:12345: sn=CN-WH01-9AF3C1D2 alg=ES256 level=0 trust=- accepted=False
    Ran 29 tests in 0.458s
    OK

### 批量合规用例（黄金样本/SN 边界/报文） — PASS（0.1s）

    == 1. 黄金样本（校验位只算 ORG-UNIQUE，不含 CC）==
    == 2. SN 边界（格式：CC-ORG-UNIQUE[-CHECK]，Crockford Base32 去 I L O U）==
    == 3. SN 解析（注册时按 SN 回填 cc/org，不信任前端传值）==
    == 4. 报文编解码边界（通用 JSON 公共头）==
      OK   parse_eth_frame：ethertype 不符 → None
    批量用例：全部通过

### 降级策略（§8.2 选级 / §8.3 服务端） — PASS（0.1s）

    == 1. §8.2 选级流程（真值表） ==
    == 2. §8.1 映射表与级别取值域 ==
    == 3. Device.report 自动选级 ==
    == 4. §8.3 verify_report 的 degraded / coverage_only ==
    == 5. counts_as_presence（能不能当“人员出现”） ==
    == 6. 降级告警（§8.3） ==
      OK   两条 crit 都在（顺序：同级按 since）  ['case_overtime', 'sig_fail_rate']
    全部通过

### 能量轴（免电池客户端） — PASS（0.0s）

    == 1. 电压映射（电量 → battery_mv）==
    == 2. 采集充足：ES256 + 够用的间隔 ==
    == 3. 采集偏低：降级**只为把间隔拉回可用区** ==
    == 4. 采集太低：慢到跟不住人（但仍不降 L3） ==
    == 5. 采不敷出：没有可维持间隔（不装出能持续的样子）+ 给出「还能撑多久」 ==
    == 6. 完全没采集：**如实沉默**，不编间隔 ==
      OK   钳在 [0, store]（不出现负电量、不超容量）
    能量轴测试全部通过

### 抓包解析 / 双源对照 — PASS（0.1s）

    == 1. pcap 往返（写→读） ==
    == 2. 格式错误必须吵（不静默降级） ==
    == 3. 解析与分类 ==
    == 4. 双源对照（只算差，不猜映射） ==
    == 5. 参照 JSON 读取 ==
    == 6. CLI 冒烟（真跑一遍，退出码要对） ==
      OK   --json 结构含 summary+rows 且 rows 全量  {'frames': 8, 'orpah': 5, 'by_type': {'ORPAH-REQ-CONNECT': 1, 'ORPAH-REPORT': 2, 'ORPAH-ACCESS-INFO': 1, 'ORPAH-ID-REPORT': 1}, 'errors': {'not-orpah': 1, 'bad-json': 1, 'bad-frame': 1}, 'linktype': 1, 'linktype_name': 'Ethernet'}
    全部通过

### 设备时钟漂移（长基线） — PASS（0.1s）

    --- A 时钟准（跑 1h）---
      [PASS] A：offset ≈ -0.5s（整数秒截断的固有偏置，不是设备真的慢 0.5s）
      [PASS] A：漂移不给数 —— 原因=noise（噪声里看不出趋势，不编 0 也不编别的）
    --- B 晶振偏快 +200ppm（跑 1h）---
      [PASS] B：offset ≈ +0.2s（当前时刻的偏差：漂移 1h 累计 +0.72s − 截断 0.5s）
      [PASS] B：漂移估出 +200ppm（晶振级，短窗做不到）

### 定位内核（pos.js，node 跑原文） — PASS（0.2s）

      OK   RSSI→距离→RSSI 往返（A=-40 n=2.5, -70dBm）
      OK   三边定位（精确距离）复原 (5,5)
      OK   WLS：远端差站（+40m）比等权线性解明显更接近真值（误差比 < 0.5）
      OK   WLS：标准入参（{s,dist}）在精确距离下复原 (5,5)（证明读的是 o.s.x/o.s.y）
      OK   椭圆：特征值→半轴（√(5.991·4) / √(5.991·1)，长短轴比 2）
      OK   椭圆：退化协方差（近乎共线）→ degenerate=true（半径仍可给，形状不可信）
      OK   两页都走 samplesFor（样本源判据单一源）
      OK   replay.html 走 frameGaps（报文流判定单一源）

### 文案字典（zh/en 一致 + 页面引用无缺失） — PASS（0.1s）

    PASS  字典 zh/en key 集合一致（797 / 797）
    PASS  每个 key 恰好 2 次（zh + en）
    PASS  字典非空且含中文与英文条目（翻译真的两套）
    PASS  扫描 14 个页面/脚本，引用 767 个 key（含动态前缀家族 12 个）
    PASS  页面引用的 key 全部在字典里
    PASS  node 语法检查通过

