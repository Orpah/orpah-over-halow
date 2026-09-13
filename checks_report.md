# ORPAH 批量合规测试报告

- 时间：2026-09-13 20:38:49
- git HEAD：`4c73172`
- 解释器：3.13.14 @ C:\Python313\python.exe
- 结论：**全部通过**（30/30 套件通过）

## 套件结果

| 套件 | 脚本 | 结果 | 耗时 | 说明 |
|---|---|---|---|---|
| 运动/定位数据源 | `test_motion.py` | ✅ PASS | 0.1s | 通过 |
| 密钥生命周期 | `test_keys.py` | ✅ PASS | 0.1s | 通过 |
| 防 spoof（离线逐条） | `test_spoof.py` | ✅ PASS | 0.1s | 通过 |
| 攻击流量面板（认领逻辑 / 单一源 / 页面守卫） | `test_attack.py` | ✅ PASS | 0.6s | 通过 |
| 告警规则（含处置态） | `test_alerts.py` | ✅ PASS | 0.2s | 通过 |
| 告警通知（边沿触发 / Webhook / 失败可见） | `test_notify.py` | ✅ PASS | 0.1s | 通过 |
| 指标面板纯计算 | `test_metrics.py` | ✅ PASS | 0.1s | 通过 |
| 时钟可信（ts=0 无 RTC） | `test_clock.py` | ✅ PASS | 0.5s | 通过 |
| IoTDB 审计/时间窗 | `test_tsdb_audit.py` | ✅ PASS | 0.5s | 通过 |
| UI 服务器契约 | `test_server.py` | ✅ PASS | 0.7s | 通过 |
| 批量合规用例（黄金样本/SN 边界/报文） | `checks_batch.py` | ✅ PASS | 0.1s | 通过 |
| 降级策略（§8.2 选级 / §8.3 服务端） | `test_levels.py` | ✅ PASS | 0.1s | 通过 |
| 能量轴（免电池客户端） | `test_energy.py` | ✅ PASS | 0.0s | 通过 |
| 抓包解析 / 双源对照 | `test_capture.py` | ✅ PASS | 0.1s | 通过 |
| 设备时钟漂移（长基线） | `demo_clock.py` | ✅ PASS | 0.1s | 通过 |
| 定位内核（pos.js，node 跑原文） | `test_posjs.py` | ✅ PASS | 0.2s | 通过 |
| 地图共享件（map.js：基点/野外包 + maps.py） | `test_mapjs.py` | ✅ PASS | 0.2s | 通过 |
| 首页转义口径（app.js：esc/alertText 单一出口） | `test_appjs.py` | ✅ PASS | 0.0s | 通过 |
| 限频（§5.8 令牌桶 / 两条防线） | `test_ratelimit.py` | ✅ PASS | 0.1s | 通过 |
| 设备侧自限频（§5.8 设备那一环，自愿） | `test_selflimit.py` | ✅ PASS | 0.1s | 通过 |
| Router 下行路径（来源校验） | `test_router.py` | ✅ PASS | 0.1s | 通过 |
| 下行真实性（F-14 B：签名/重放/时间窗） | `test_downlink.py` | ✅ PASS | 0.1s | 通过 |
| 文案字典（zh/en 一致 + 页面引用无缺失） | `test_i18n.py` | ✅ PASS | 0.2s | 通过 |
| UI 样式/窄屏守卫 | `test_uicss.py` | ✅ PASS | 0.0s | 通过 |
| L1 端到端 | `demo_l1.py` | ✅ PASS | 1.4s | 通过 |
| L2 消息流 | `demo_l2.py` | ✅ PASS | 2.2s | 通过 |
| L3 多 Router 漫游/去重 | `demo_l3.py` | ✅ PASS | 4.2s | 通过 |
| L3b 主动拉表 | `demo_l4.py` | ✅ PASS | 1.2s | 通过 |
| 防 spoof 空口端到端 | `demo_spoof.py` | ✅ PASS | 1.8s | 通过 |
| 限频（§5.8）端到端 | `demo_ratelimit.py` | ✅ PASS | 7.6s | 通过 |

## 关键输出

### 运动/定位数据源 — PASS（0.1s）

    == 1. �����뼸�� ==
    == 2. pos ȷ������������ ==
    == 3. �����ԣ���˲�ƣ� ==
    == 4. RSSI ·����Ļ��� ==
    == 4b. ǯλҪ��ס�����Ľ����2026-09-12 ��飩 ==
    == 5. ��·����һ�£���ʾ���ݿ����ԣ� ==
      OK   truth_at�������� �� �ձ���ҳ����ʾ�������á������� 0��
    ȫ��ͨ��

### 密钥生命周期 — PASS（0.1s）

    == 1. ǩ�����ݵȣ� ==
    == 2. ��ǩ���� 1 ���� ==
    == 3. �ֻ�����Կ�����ޡ��¾ɶ����� ==
    == 4. ���޵��� �� ���� ==
    == 5. grace_sec=0����Կ����ʧЧ ==
    == 6. ��ǰǿ������ ==
      OK   ��һ����gen=2��ǩ�ı����õ� 1 ���Ŀ��� �� �ܣ����β��ܴ���
    all key lifecycle tests passed

### 防 spoof（离线逐条） — PASS（0.1s）

    == 1. �嵥��Ǣ ==
    == 2. �����þ������ߣ� ==
    == 3. �������� ==
    == 4. ����ȷʵ�Ķ��˱��ģ����ǰѺϷ�����ԭ������һ�飩 ==
    == 5. ˳���޹أ�revoked ����ʱ�⣬����Ⱦ�������� keystore�� ==
      OK   revoked ����ǰ vs ��������� (kind, �þ�) ��ȫһ��  ����� 2 �ڵ�Ĭ��˳��Աȣ�����˳��ͬ���ʰ����ϱȣ�
    ȫ��ͨ��

### 攻击流量面板（认领逻辑 / 单一源 / 页面守卫） — PASS（0.6s）

    == 1. 用例清单（单一源 spoof.py）==
    PASS  用例字段仍是 5 项（脚本/演示都按这个解包）
    PASS  说明是中英两套且都非空
    PASS  说明里不写 Markdown（会原样显示成星号）
    PASS  说明里不出现归因性词（AGENTS §0 术语硬规则）
    PASS  UI 子集排除 revoked（页面用活密钥库，跑一次会把在跑的设备搞成验不过）

### 告警规则（含处置态） — PASS（0.2s）

    PASS  ��δ�ϱ�������/��ʧ�еĶ�����ͣ��/���ϲ���
    PASS  ��δ�ϱ����� gap �ҵȼ� warn
    PASS  C��Ĭ�� no_report ������ֵ 300s��ORPAH_ALERT_NO_REPORT_CRIT_SEC��
    PASS  C��Ĭ�� case_handled ������ֵ 48h��ORPAH_ALERT_CASE_HANDLED_CRIT_SEC��ע��������ֵ CASE_HANDLED_SEC=24h ��������ͬ������
    PASS  C���չ��𲽵�δ�������� �� warn
    PASS  C������������ �� crit

### 告警通知（边沿触发 / Webhook / 失败可见） — PASS（0.1s）

    PASS  默认：没配 ORPAH_NOTIFY_URL → 关闭（on=False）
    PASS  关闭时 step 不投递、不产生投递记录
    PASS  关闭时也不把活跃告警当“新告警”攒着（seen 跟着更新，开启后不会重推一堆旧告警）
    PASS  关闭时 max 也不涨计数
    PASS  首次出现 → 推一次（event=alert）
    PASS  ★ 同一告警持续存在 → 后续评估**一次都不推**（否则每 3 秒刷一遍）

### 指标面板纯计算 — PASS（0.1s）

    PASS  ǩ��������/ͨ��/����
    PASS  ǩ����ʧ���� 1/4
    PASS  ǩ�����㷨�ֲ�
    PASS  ǩ�������������¼�����ͳ��
    PASS  ǩ����ȡ���� alg �� unknown�����£�
    PASS  ǩ���������� �� fail_ratio=None������ 0��

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

    == 1. actor �ֶ� ==
    == 2. query_events �ض� actor ==
    == 3. ������������ ==
    == 4. ʱ�䴰��ѯ���ط��ã� ==
    == 6. ·������۲⣨��·������λ������Դ�� ==
    == 7. ������������ ==
      OK   �Ϸ�ֵ��Ч
    ȫ��ͨ��

### UI 服务器契约 — PASS（0.7s）

    [server] [down 1] ORPAH-LOST-TABLE sn=- -> 127.0.0.1:12345
    [server] [down 2] ORPAH-LOST-TABLE sn=- -> 127.0.0.1:12345
    [server] [down 3] ORPAH-LOST-TABLE sn=- -> 127.0.0.1:12345
    [server] [down 1] ORPAH-LOST-TABLE sn=- -> 127.0.0.1:12345
    [server] [down 1] ORPAH-LOST-TABLE sn=- -> 127.0.0.1:12345
    [server] [down 1] ORPAH-TRACKING-STATUS sn=CN-WH01-9AF3C1D2 -> 127.0.0.1:12345
    Ran 34 tests in 0.489s
    OK

### 批量合规用例（黄金样本/SN 边界/报文） — PASS（0.1s）

    == 1. 黄金样本（校验位只算 ORG-UNIQUE，不含 CC）==
    == 2. SN 边界（格式：CC-ORG-UNIQUE[-CHECK]，Crockford Base32 去 I L O U）==
    == 3. SN 解析（注册时按 SN 回填 cc/org，不信任前端传值）==
    == 4. 报文编解码边界（通用 JSON 公共头）==
      OK   parse_eth_frame：ethertype 不符 → None
    批量用例：全部通过

### 降级策略（§8.2 选级 / §8.3 服务端） — PASS（0.1s）

    == 1. ��8.2 ѡ�����̣���ֵ���� ==
    == 2. ��8.1 ӳ����뼶��ȡֵ�� ==
    == 3. Device.report �Զ�ѡ�� ==
    == 4. ��8.3 verify_report �� degraded / coverage_only ==
    == 5. counts_as_presence���ܲ��ܵ�����Ա���֡��� ==
    == 6. �����澯����8.3�� ==
      OK   ���� crit ���ڣ�˳��ͬ���� since��  ['case_overtime', 'sig_fail_rate']
    ȫ��ͨ��

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

    == 1. pcap ������д������ ==
    == 2. ��ʽ������볳������Ĭ������ ==
    == 3. ��������� ==
    == 4. ˫Դ���գ�ֻ������ӳ�䣩 ==
    == 5. ���� JSON ��ȡ ==
    == 6. CLI ð�̣�����һ�飬�˳���Ҫ�ԣ� ==
      OK   --json �ṹ�� summary+rows �� rows ȫ��  {'frames': 8, 'orpah': 5, 'by_type': {'ORPAH-REQ-CONNECT': 1, 'ORPAH-REPORT': 2, 'ORPAH-ACCESS-INFO': 1, 'ORPAH-ID-REPORT': 1}, 'errors': {'not-orpah': 1, 'bad-json': 1, 'bad-frame': 1}, 'linktype': 1, 'linktype_name': 'Ethernet'}
    ȫ��ͨ��

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
      OK   replay.html 导出带可信度（CSV 列 + GPX 扩展/小结 + GeoJSON trusts）
      OK   replay.html 跑多帧持续偏差扫描（biasScan + 剔除最狠者后重扫）

### 地图共享件（map.js：基点/野外包 + maps.py） — PASS（0.2s）

    == 基点导入/换算/存储（map.js 原文，node 跑） ==
    == 页面守卫（两页一致、入口齐全） ==
    == 口径守卫（WGS84 / 不做转换 / 本机存储 / 不是现场值） ==
    == 野外包（本地 XYZ 目录）：maps.py 逻辑 + 两侧接线 ==
      OK   replay.html：开页拉包列表（loadPacks）
      OK   replay.html：调 packInit(cb) 接线（map.js 顶层碰不了 DOM；换包要重建图层）

### 首页转义口径（app.js：esc/alertText 单一出口） — PASS（0.0s）

    == 转义唯一实现（esc 覆盖字符） ==
    == alertText：调用点必须包 esc、体内不许再包 ==
    == 告警弹窗跳转：ALERT_LINK 必须覆盖 alerts.py 全部 kind ==
    == 声音提醒：默认关 ==
    == 页面守卫（app.js 确实被加载） ==

### 限频（§5.8 令牌桶 / 两条防线） — PASS（0.1s）

    [router] [1] 上行 ORPAH-REPORT sn=CN-WH01-9AF3C1D2 seq=0 -> 127.0.0.1:59998
    [router] [2] 上行 ORPAH-REPORT sn=CN-WH01-9AF3C1D2 seq=1 -> 127.0.0.1:59998
    [router] [3] 上行 ORPAH-REPORT sn=CN-WH01-9AF3C1D2 seq=2 -> 127.0.0.1:59998
    [router] [4] 上行 ORPAH-REPORT sn=CN-WH01-9AF3C1D2 seq=3 -> 127.0.0.1:59998
    [router] [5] 上行 ORPAH-REPORT sn=CN-WH01-9AF3C1D2 seq=4 -> 127.0.0.1:59998
    [router] [6] 上行 ORPAH-REPORT sn=CN-WH01-9AF3C1D2 seq=5 -> 127.0.0.1:59998
    OK
    限频（§5.8）：35/35 通过

### 设备侧自限频（§5.8 设备那一环，自愿） — PASS（0.1s）

    [client] 注入 ORPAH-REQ-CONNECT sn=CN-WH01-9AF3C1D2
    [client] [1] 注入 ORPAH-REPORT sn=CN-WH01-9AF3C1D2 rssi=-55 (102B)
    [client] 自限频：ID-REPORT 延后（还差 100.00s；累计延后 1 条 —— 延后不是丢弃，下一拍还会发）
    [client] 注入 ORPAH-REQ-CONNECT sn=CN-WH01-9AF3C1D2
    [client] 注入 ORPAH-ID-REPORT sn=CN-X (109B)
    [client] 注入 ORPAH-ID-REPORT sn=CN-X (109B)
    Ran 24 tests in 0.007s
    OK

### Router 下行路径（来源校验） — PASS（0.1s）

    [router] LOST-TABLE 更新（1 项）
    [router] 下行**未做真实性校验**（没配 Server 公钥 `ORPAH_DOWN_PUB`）：只做了来源校验（A）—— 同源伪造/重放挡不住。配上公钥后自动启用签名校验（B）
    [router] LOST-TABLE 更新（0 项）
    [router] LOST-TABLE 更新（1 项）
    [router] 丢弃下行（签名校验未过 [replay]）[1] ORPAH-LOST-TABLE <- 127.0.0.1:19447
    [router] 丢弃下行（签名校验未过 [replay]）[2] ORPAH-LOST-TABLE <- 127.0.0.1:19447
    OK
    Router 下行路径：18/18 通过

### 下行真实性（F-14 B：签名/重放/时间窗） — PASS（0.1s）

    PASS  签名后能验过（ok=True，带回 (dts,dn)）
    PASS  签名是 base64url(64 字节 raw r||s)，与 Orpah ID 同一格式
    PASS  签名不改变原报文（sign 返回新 dict）
    PASS  空表也能签能验（攻击载荷就是空表）
    PASS  改字段 entries=[{'sn': 'CN-WH01-9AF3C1D2', 'tracked': False}] → bad_sig（预像盖住整条报文）
    PASS  改字段 entries=[] → bad_sig（预像盖住整条报文）

### 文案字典（zh/en 一致 + 页面引用无缺失） — PASS（0.2s）

    PASS  字典 zh/en key 集合一致（1018 / 1018）
    PASS  每个 key 恰好 2 次（zh + en）
    PASS  字典非空且含中文与英文条目（翻译真的两套）
    PASS  扫描 16 个页面/脚本，引用 956 个 key（含动态前缀家族 14 个）
    PASS  页面引用的 key 全部在字典里
    PASS  i18n 文案值里不含 Markdown 标记（**）

### UI 样式/窄屏守卫 — PASS（0.0s）

    == style.css：页面不再被写死宽度、窄屏档位齐全 ==
    == 页面：内联宽度、viewport、共享件 ==
      OK   每页都加载共享 style.css + ui_i18n.js
    UI 样式守卫：全部通过

### L1 端到端 — PASS（1.4s）

    === ORPAH L1 验收 ===
    结果: [PASS]

### L2 消息流 — PASS（2.2s）

    --- 分支一：走失库未命中 sn=CN-WH01-9AF3C1D2 ---
    --- mark 走失 sn=CN-WH01-9AF3C1D2（Server 下发 LOST-TABLE）---
    --- 分支二：走失库命中 sn=CN-WH01-9AF3C1D2 ---
    === ORPAH L2 验收 ===
    分支一(未命中 NOT-TRACKED): PASS
    分支二(命中 TRACKED): PASS

### L3 多 Router 漫游/去重 — PASS（4.2s）

    --- 阶段A：sn=CN-WH01-9AF3C1D2 在 R1 网络（2 条，未 mark）---
    --- 阶段B：sn=CN-WH01-9AF3C1D2 漫游到 R2 网络（seq 续 3）---
    --- 阶段C：mark 走失 sn=CN-WH01-9AF3C1D2（在 R2 网络）---
    --- 阶段D：sn=CN-WH01-9AF3C1D2 在 R2 再次会话（应 tracked=True）---
    --- 阶段E：去重测试（重发已接受的 seq=4）---
    --- 阶段F：SN 校验（非法 sn → FORMAT-ERR）---

### L3b 主动拉表 — PASS（1.2s）

    --- 模拟 Router 重启（清空本地缓存 + 未同步）---
    --- Server untrack sn=CN-WH01-9AF3C1D2（变更推送，Router 无需再拉）---
    --- 关联号 rid：拉表应答 vs Server 主动推送 ---
      [PASS] REQ 带 rid（type 正确）
      [PASS] 应答原样回显 rid
      [PASS] 主动推送不带 rid

### 防 spoof 空口端到端 — PASS（1.8s）

      无认证空口防 spoof 端到端演示
      被冒充设备 SN : CN-WH01-9AF3C1D2
      攻击者 SN    : CN-WH01-909X3BC7NE-20（未登记 → 服务器不认识）
      链路          : Client→STA→空口→AP→Router→UDP:19847→Server
      用例数        : 14（含 1 条合法对照）
    [server] 监听 127.0.0.1:19847 (UDP)，等待 ORPAH 报文…
      🎉 防 spoof 端到端验收全部通过
    ==========================================================================

### 限频（§5.8）端到端 — PASS（7.6s）

      限频（§5.8）端到端演示
      链路        : Client→STA→空口→AP→Router→UDP:19947→Server
      Server 侧  : per-SN burst=5/5.0s · per-Router burst=10/5.0s
      Router 侧  : per-SN burst=40/5.0s（转发） · per-MAC burst=5/1.0s（probe）
    [server] 监听 127.0.0.1:19947 (UDP)，等待 ORPAH 报文…
    [router] 已连 AP 模块 host 口 :9921；Server -> 127.0.0.1:19947
      🎉 限频端到端验收全部通过
    ==========================================================================

