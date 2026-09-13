# ORPAH-over-HaLow 开发规则（orpah-over-halow）

本项目 = **ORPAH 业务全链路**（Router 桥 / 走失表 / 报文集 / ID 验签 / 定位 / 告警 / IoTDB / Web UI），
纯 PC + Python。**2026-09-12 从 `halow-demo/simulator/orpah/` 整体迁出**（`git subtree split`，保留 160 个
提交历史），从此独立演进。

## 0. 项目边界与两条硬依赖

- **本仓库自带空口仿真副本**：`vendor/halow/{sim.py,devprofiles.py,blang.py}`（来源 halow-demo，
  逐字节复制，见 `vendor/halow/VENDOR.md`）—— 用户要求**零硬件、单进程即可跑 demo**，
  所以 `ui_server.py` / `demo_l*.py` 把 `vendor/halow` 加进 `sys.path` 后 `import sim`，
  **不再依赖 `../host`**。上游 `halow-demo` 仍是空口侧权威源（真实链路/真机联调/抓包在那边）；
  对空口的修改**必须回上游**，否则就是两份实现漂移。
- **两个仓库的分工**（详见 `README.md`）：`halow-demo` = 空口/设备/长距（不碰业务）；
  本仓库 = 业务全链路。真机阶段两者通过 **host 数据口（TCP，帧格式见 `host_bus.py`）** 相接。
- **一键回归 = `python run_checks.py [--e2e]`** —— 见下面 §0 的 `run_checks` 条目；
  跑 `--e2e` 前必须先停掉 `ui_server.py`（:8901 端口会串扰）。
- 上游（空口/设备侧）的规则见 `halow-demo/simulator/AGENTS.md`（本文件不再重复）。
- **原则「孤证不立」（2026-09-13 用户定；写码前必读）**：**任何单条来自机器的证据都不足以
  支撑位置判定**（设备自报 / 单台 Router 观测都是孤证）→ 可信度只能来自**多个互不依赖的
  来源互相印证**（多观测一致性 + 离群剔除）。**唯一例外**：证据**明确来自人**（**办案人 / 搜救队**）
  → 单源可采信，但必须留痕（审计 `actor`）；人工来源优先于机器观测，且**不参与**一致性剔除。
  - **无冗余时必须如实降级**（标「单一来源、未交叉校验」）；**不得**因为「圈算得小」就当可信 ——
    **精度 ≠ 可信度**，两者在接口与页面上必须**分开呈现**。
  - **k 台 Router 合谋不可防**（信息论边界，需 n−k ≥ 3 才能定位）—— **不得**写成「已防住」。
  - **被否决的做法（别再提）**：Router 身份白名单 / 受信集合（只回答「谁在说」）；
    靠运营商绑定 IP 当设备身份（挡不住签约持有者自己作恶，且属运营/合规层）。
  - 规格：`Protocol/docs/orpah-over-halow/SPEC.md` §8 原则 P-1 + §10 F-12；
    `Protocol/docs/OrpahIDProtocol.md` §5.7（v1.18）。
  - **判定实现 = `ui/static/pos.js` 的 `consensus()`（唯一一份，`track.html`/`replay.html` 共用）；
    选项与文案也在这里**：`consOpts(tref, vx, vy)`（漏传速度 → 正常走动被当成冲突）与
    `trustText(c, T)`（可信度措辞）—— 页面**不许**各写一份，`test_posjs.py` 的页面守卫会拦。
    **两页都必须呈现可信度**（规格要求“精度与可信度分开显示”；2026-09-13 回放页补齐：
    逐帧判定 + 整窗统计 + **轨迹/进度条按可信度着色**）。着色走单一源 `TRUST_COLOR`（颜色）+
    `TRUST_STYLE`（线型：实线=通过、虚线=单一来源、点线=冲突）—— **双通道**，红绿色盲也能分辨；
    三处（进度条/画布/地图）共用，页面守卫会拦“另写一套色”。
    阈值必须按实测零假设分布定，不许拍脑袋**（2026-09-13 实测：诚实 max-z 上界 ≈ **3.8 且与噪声幅度无关**
    → 判定阈值取 **5**；原先拍的 3 会误报 11%）。**回归锁两条**：`test_posjs.py` 里
    「诚实 864 样本零误报」+「诚实样本不许误报 `loose`」—— **改阈值/改 σ 分母必看这两条**。
  - **改判据别只看「能不能抓住」，先量「会不会误剔除好台」**（实测踩过：分母漏算预测协方差时，
    其余台里混着一台偏大的会把**好台**算出大 z → 误剔除 11%、路线区域 28%）。
  - **偏差方向不对称，必须如实说**：“偏更远”无上界 → 可靠可检（4 台 77%/5 台 87%）；
    “**偏更近**”误差上限 = 它自己报的距离 → z 有硬上界（≈3.8，与诚实尾部重合）→
    **数学上不可分辨，怎么调阀值都拓不到**（×0.25/×0.1 实测 864/864 全漏）→ 写“已防住”就是骗人。
  - **多台同时偏大（n=4 里 2 台）= 信息论边界**（n−k≥3 才够）→ 定不了是哪台；此时**唯一还能说的事实**
    是**拟合残差 σ0**（诚实 0.2~0.8 vs 2 台偏大 1.5~2.2）→ 页面按阀值 1.8 报「拟合偏松」
    （`consensus().loose`，**只陈述事实、不指认哪台、不保证每帧都报**）。  - **多帧持续偏差扫描（`biasScan`，2026-09-13）= 单帧“不可分辨”那一侧的补充**：单帧对“偏得更小”
    在数学上不可分辨（误差上限 = 自身距离），但**持续**偏差会在多帧的「实测/预测」中位比值上留痕。
    阈值同样**按实测零假设分布**定（诚实上界 60 帧 7.4 / 800 帧 18.5 / 2000 帧 22.2 → **取 30**；
    帧数 >2000 就均匀抽稀，否则上界会继续爬）；检出下限实测：×0.5/×0.25 ~100 帧起可靠、×0.75 要 ≳300 帧。
    **三条边界必须写在页面上**：① 帧数越少越不灵；② 它比的是“这台 vs 其余台”，**全体一起偏看不出来**
    （全局标定偏得靠 F-11 实测）；③ **比值偏离 ≠ 这台有问题** —— 该台落在其余台几何之外时，
    留一法的预测本身有系统外推偏差（演示里 S1 实测中位 0.90）。→ 只能当**线索**，不当结论。
    **实现必须两步（`scanRowsSkip` + `S.biasSkip`，2026-09-13 实测）**：第一遍会把**被真凶拖累的台**
    一起标出（把一台改成持续 ×0.25：真凶 z=588，另两台被拖到 47/32 也越线）→ 必须**剔掉最狠那台重扫**，
    其余台回到 1 附近（实测 z 3.6）才敢说“最像是就它”；剔除后仍有台偏 → 如实说**“定不了是哪台”**。
    页面文案分两条（`rp_bias_hit_clean` 只把最狠那台当线索、被拖累的台单独注明 / `rp_bias_hit_multi` 列全部）。
    验证方式（值得照做）：在**真实演示数据**上把某台的 `dist` 乘 0.25 后重跑 `measureTrustWin()` ——
    单帧判定 799/799 全“通过”（**完全漏**），两步扫描才把真凶挑出来，这就是这项存在的理由。  - **术语硬规则（2026-09-13 用户定）：代码/文档/页面里【不得】用“谎报/撒谎/欺骗/作弊/有罪”这类
    归因性词**（也不得用“受信/不可信”以外的道德措辞），统一用**偏差 / 不一致 / 离群 / 对不上**。
    理由：**偏差 ≠ 有人作恶** —— 现实里成因很多：**遮挡/多径、天线损坏或接头松、元件性能下降/老化、
    标定漂移**，以及（其中一种）被改装/冒充；而我们的信号模型（A/n 对数路径损耗）
    **只假定空旷无遮挡、未建遮挡与多径** → **大偏差不得直接归因为攻击**。
    表达要求：结果一律写成「与其余台不一致 / 拟合偏松」，并且**必须带“仅线索、需人工复核”**；
    只有审计/威胁建模那一层才谈攻击（且要说“可能”）。
  - **密钥生命周期：不做轮换（2026-09-13 用户定，以《Orpah ID 协议规范》§6.3.1 为准，v1.19）**：
    设备侧只持有 **1 把 ECDSA P-256 私钥（Slot 0）+ 1 把 32 字节 HMAC 密钥（降级用）**，
    **一代终身，“不存在第 2 代”**。理由：① 本系统**对使用者无强制约束力** —— 觉得不好用/不想用，
    **抛弃客户端或物理屏蔽（包锡纸）**即可，所以不需要靠轮换应对“疑似泄露”（轮换只会给愿意用的人
    制造失联风险）；② **免电池取能终端做不了轮换** —— 轮换 = **高能耗 + 必须在线完成一次事务**
    （生成新钥 → 上行公钥 → 等 server 确认/绑定 → 按 server 指定时刻切换），而取能设备采集功率
    波动，**承诺不了“换钥那一刻我有电”**（就是能量轴里 `P = 0` 的情形）。
    → demo 里的 `rotate`/`grace`/`retired` **只是“对比用的演示代码”，不是设备模型**；
    **不得基于它继续加功能**，也不得在页面/文档里当成系统能力（规格 SPEC §10 F-13 已结案）。
  - **文案不得写 Markdown（2026-09-13 用户定）**：页面把 i18n 文案当**纯文本**渲染，
    写作 `**加粗**` 会**原样显示成星号**（实测踩过：密钥页副标题/说明里露出 `**…**`）。
    → `ui_i18n.js` 的**值**、页面 HTML 里 `data-i18n` 的**兜底文案**、以及页面上拼出来的可见文本，
    **一律不写 `**` / `_斜体_` / `\`代码\``**；要强调就用「」/（不是）这类中文标点或另起一句。
    Markdown 只用于**源码注释**与 `docs/`、`README`、`AGENTS.md` 这些给读源码的人看的文件。
    `test_i18n.py` 已加两条守卫（字典值、HTML 兜底文案），别再改回去。

本目录是 TXW8301 的纯软件模拟器（`host/` Python 移植 + `tools/ui/` Web UI +
`firmware/` CH32V203 固件）。开发、修改、调试任何部分前，**先遵循以下规则与踩坑记录**。

## 0. ORPAH demo（2026-09-09 起，``）

- 定位：本仓库就是 ORPAH-over-HaLow 的 L1/L2/L3 原型（原在 halow-demo 内，2026-09-12 迁出）。
  L1 = 数据通路最小骨架（Client 上行 payload 到 Server）；L2 = 全消息流 + 走失表 +
  跟踪状态（SPEC §9，纯 PC 无硬件）。
- **⚠ 范围原则（2026-09-11 用户定，最高约束）：ORPAH 是技术搜寻手段，demo 边界取最小。**
  只做「用无线技术找到人」这一段（登记走失态/下发走失表/路由器发现上报/服务器回执落库/
  RSSI 定位/审计与告警）——**不是公安办案系统**。默认**不引入**：组织与机构建模、
  角色/权限体系、警员身份、案件分配/派单/办案流程、多租户隔离。
  新需求先过判据：**「让找人更快更准」→ 做；「让管理/流程更完整」→ 默认不做，先记
  `ROADMAP.md` §〇。** 需要「谁」时止步于审计标签（自由文本 `actor`）；需要「谁负责」
  时先确认是否存在真实运营方（demo 目前无登录者）。细节与已应用示例见 `ROADMAP.md` §〇。
- 分层（对齐 `Protocol/docs/orpah-over-halow/SPEC.md`）：链路 = 以太网帧(ethertype
  `0x88B5`) 经模拟器二层桥透传；Router→Server 段用真实 UDP。角色：Client=`client.py`+STA
  模拟器、Router=`router.py`+AP 模拟器、Server=`server.py`(UDP `19447`)。
- **L2 已实现（2026-09-09）**：`orpah_proto.py` 全套报文（REQ-CONNECT/ACCESS-INFO/
  REPORT/TRACKING-STATUS/ERROR/LOST-TABLE，统一 JSON 公共头 v/type/sn/ts）。**双向打通**
  （Server→Router UDP 应答→注入 AP 空口→STA→Client host 口）：`router.py` 双向桥
  （REQ-CONNECT 查本地缓存回 ACCESS-INFO / REPORT UDP 转发 / 收 Server LOST-TABLE 更新
  缓存 / 下行注入空口）；`server.py` **权威走失库**（mark_tracked/untrack → 下发
  LOST-TABLE；收 REPORT → 校验+查库 → 回 TRACKING-STATUS）；`client.py` 双向会话
  （REQ-CONNECT→ACCESS-INFO→REPORT→TRACKING-STATUS）。**验收：`demo_l2.py`**（两分支
  未命中 NOT-TRACKED / mark 后 TRACKED，PASS）。UI 加「L2 协议消息流」面板 + 走失表
  标记/取消按钮。
- 运行：`python demo_l1.py --n 3`（L1 验收）、`python demo_l2.py`（L2 验收）。
  单独跑见 `README.md`。
- **密钥库（2026-09-12，P1）叮：`orpah.db` 里的 `keys` 表是**已发密钥的真相**，
  改下面任一东西都会让已入库的密钥全部验不过（`signature_invalid`）：
  ① `orpah_id.Device` 的密钥生成（尤其 `derive_demo_privkey` 的派生前缀/算法 ——
  模拟器私钥就靠它「由 (SN, 代次) 确定派生」，否则服务器重启后公钥对不上）；
  ② `verify_report` 的验签入参（预像/JCS 规范化）；③ `b64url`/PEM 编解码。
  改完请跑 `python test_keys.py`（生命周期单测）+ `python demo_id.py`（端到端验收）；
  实在要换算法就删 `orpah.db` 重新播种（会一并清掉清册/案件/站位）。
- **orpah 进程不要改客户端 SN 做测试**（会真的造出新的密钥代次并写进密钥库/审计）。
- **防 spoof（空口无认证）演示（2026-09-12，P1）**：前提是 ORPAH 空口**开放/无认证** ——
  任何人都能往空口里丢一条 ORPAH-ID-REPORT。防线顺序：**格式 → SN 校验位（Damm32/mod97）
  → 时间窗/nonce 去重 → 吊销表 → 设备公钥验签**。
  - 攻击构造**只有一份**：`spoof.py`（13 种，含 1 条合法对照；含 2026-09-13 新增的
    **能力降级** `cap_downgrade` = 改已签声明的 `cap.rtc`）——
    `demo_spoof.py`（真链路端到端）、`test_spoof.py`（离线逐条）、页面（index 选类型注入）共用，
    否则“演示的”与“测的”会漂移。
  - 页面入口：index 的 Orpah ID 卡片 →「注入伪造上报」/「跑全部攻击」（走真空口链路，落 `id_reject`）。
  - **已知边界（必须如实展示，不要包装成“防住了”）**：`xport`（路由器侧观测）**不在签名预像里**，
    篡改它验签照样通过；而定位数据恰恰来自路由器侧测量 → **能冒充路由器就能伪造定位**。
    已记 ROADMAP 开放问题，动手补前先与用户对齐。
  - ⚠ `spoof.CASES` 里 `revoked` **会改密钥库状态，必须放最后**；且 `unrevoke` 会把各代转成
    retired（之后验签就 `unknown_device`）—— 页面因此把 `revoked` 排除在 `UI_KINDS` 之外。
- **等待一律用 `waiting.py`（2026-09-12，review 反馈起）**：
  - **禁止** `for _ in range(N): time.sleep(0.05)` 这种「猜次数」等待 —— 机器快慢/负载一变
    就误判（等太短=假失败，等太久=白等），且循环次数与语义无关，读者无法判断够不够。
  - 统一入口：`wait_until(cond, timeout=10.0, interval=0.05)`（按**截止时间**轮询，先判一次，
    返回 bool，**不抛异常**）、`wait_new(lst, pred, timeout=5.0, interval=0.1)`（只看**新增**元素，
    避免拿到上一轮的旧记录而假 PASS）。
  - **等「网络/回调答复」不要轮询列表**：用回调 + `queue.Queue`（或 `threading.Event`）——
    见 `test_clock.py` 的 `send_id`：`OrpahServer(on_id_report=ID_EVENTS.put)` + 按 nonce 匹配、
    先抽掉陈旧条目。
  - **超时必须可见、失败要响**：`wait_until` 返回 False 时打印 `[!!] …未…` 并让用例失败
    （`return 1/2`），**绝不静默继续**（旧代码就是静默 `continue`，链路没通也报 PASS）。
  - 判据：任何等待都该能回答「等的是什么条件、超时多少、超时后怎么办」；答不上来就是猜次数。
- **拓扑计数是「按内容」分色的三个口径，别合并成一个数（2026-09-12）**：
  - **蓝 `--acc`** = L2 上行（REQ-CONNECT / REPORT）；**橙 `--id`** = Orpah ID 签名上报
    （ID-REPORT）；**浅灰白 `--found`** = 发现（ORPAH-FOUND）。同一颜色 = 同一类内容，节点行与链路拆行一致。
  - **配色按色弱友好选（2026-09-12，用户要求）**：蓝 + 橙是两个不同色觉通道，
    红盲/绿盲/蓝黄盲下都不混；发现用**中性浅灰白**（业务事件，不当内容类型上色）。
    实测（Machado CVD 模拟 + CIEDE2000）：蓝↔橙 50.5–56.1（好）、蓝↔浅灰白 21.3–24.7；
    对比度 7.49 / 8.40 / 12.26（均过 WCAG AA）。
    **别再改回紫 `#a371f7`**：它与蓝在红盲/绿盲下 ΔE00 仅 10.2 / 5.6（几乎一色）；
    **也不要用黄 `#d29922` 当发现**：它与橙 ΔE00=4.2，全类型一色。
    另：数字都带文字标签（L2/ID/REPORT/发现），颜色是辅助通道，不辨色也能读。
  - 各口径（`ui_server.status()` 提供原始计数，前端只做加和/分色）：
    `client_sent` = REQ-CONNECT + REPORT（每周期 2 条，故恒为 `router_up` 的 2 倍）；
    `id_sent` = 本机注入的 ID-REPORT（周期 + 页面重放/超窗/伪造，全部经 `_inject_id` 计数）；
    `tx_sta` = STA 空口**数据**帧 = `client_sent + id_sent`；
    `router_up`/`router_id_up` = 路由器真正发给 Server 的 REPORT / ID（REQ-CONNECT 本机应答不过 UDP）；
    UDP 帧总额 = `router_up + router_id_up + found_total`；`server_recv` 只计 REPORT，
    ID 走验签通道（`id_report_total`，**含被拒**）。
  - **新增一种上行内容时**：在 router 的转发表里加计数 + 回调（仿 `on_up_id`），
    `ui_server` 计一份、`status()` 暴露、前端给颜色与悬停说明，四步都要做；
    只在空口加而不在 UDP 侧加，页面立刻"对不上"（本次 ID 上报就是这样被发现的）。
  - 周期上报的 ID 条数会比会话数多 1：启动时立刻 `_id_tick()` 一次（见 `start()` 注释），不是漏洞。
- **IoTDB 查询：时间可进 `WHERE`，值不行（2026-09-12，回放要按窗取数）**：
  - **时间过滤写进 SQL**（`WHERE time >= <ms> AND time <= <ms>` + `ORDER BY time ASC`）——
    时间戳是 IoTDB 的原生索引，准确且快。`tsdb.query_report_range` / `query_events_range`
    就是这么取「某设备某时间窗」的（回放用，见 `API.md` §10）。
  - **值过滤（etype/sn）不要写进 WHERE**，在本地筛（`query_events*` 就是多取几倍再 filter）——
    树模型对「非投影列」做值过滤不可靠（历史坑）。两件事别混。
  - 历史回读一律**倒序**（最近优先），回放窗口一律**升序**（按时间轴消费）。
- **多路由器观测 / 「移动的人」演示数据（2026-09-12）**：
  - **一条链路分两类数据**：设备自己报的链路值 → `root.orpah.devices.<sn>`；
    **各路由器各自测到它**的强度 → `root.orpah.routers.<sid>.<sn>`（`router_id`/站位 sid 即路由器身份）。
    **绝不能把多台路由器的测量挤进同一路径的同一时间戳** —— IoTDB 同设备同时间戳是
    last-write-wins，会互相覆盖。
  - **演示数据由 `motion.py` 生成**（闭合路线 + 匀速 + 对数距离路径损耗，确定性可复现）：
    `ui_server` 扮四台路由器，每个上报周期各写一条测量；设备 REPORT 的 rssi = 当前最近那台的测量。
    关掉它用 `ui_server.py --no-walk`（回到恒定 RSSI）。
  - **定位需要同一时刻 ≥2 个观察者**：单台路由器只有距离环；「悬停点 + 窗口中位数」模型的前提是
    目标静止 —— 人一走动就必须用「路由器序列 + 取 t 之前最近一条」（`pos.js` 的 `obsOfStation`
    第三条分支，时效 `ROUTER_MAX_AGE_MS`）。观测优先级：手动绑定 > 时间窗 > 路由器序列。
  - **tsdb 写入接口的 `ts` 参数单位是 epoch 秒**（`write_report` / `write_router_obs`）。
    传毫秒会被当成天文数字时间戳 → IoTDB 报错 → 异常被 `try/except` 吞掉，
    **只表现为 `/api/status.tsdb=false` 且数据静默不落库**（排查时先看这条）。
  - **`tsdb._read_rows` 必须整段持锁**（取数据集 + 遍历游标 + 关闭）：IoTDB 的 `Session`
    **不是线程安全的**，同一 Session 上并发的查询会互相踩游标 —— 只锁 `execute_query_statement`
    时，另一线程的查询会让本线程的遍历**读出空结果**（实测：串行 4 发全对，并发 4 发里有 2 条
    返回 0 行 / `ok:false`）。HTTP 是 ThreadingHTTPServer，两个页面/两个标签页同时请求就会撞上。
    排查手法：同一窗口**串行 vs 并发**各发几次对比。
- **回放页硬规则（2026-09-12）**：
  - **定位算法只有一份**：`ui/static/pos.js`（`trilaterate`/`wlsLocate`/`ellipseOf`/
    `obsOfStation`/`qualityOf`…）。`track.html` 与 `replay.html` 都用它 —— 别在页面里
    再抄一份算法（回放与实时必须逐位一致）。同理**回放页的「报文流时间轴」判定也只在
    `pos.js`（`frameGaps`）**：缺口/重复/**序号回退**分开计（回退 = 设备重启或乱序，
    **不是**丢包；天真的 `seq - prev - 1` 会算出负数缺口）。两条已踩过的坑都锁在单测里：
    ① `Number(null) === 0` → “没有 seq”被当成 seq=0 → 整段变成一路回退（加 `hasSeq()` 守卫）；
    ② 结果**别存 `S.frames`** —— 那个名字在 `replay.html` 里已经是位置帧（卡尔曼/CDF 用），
    会静默冲掉平滑与误差统计；报文流用 `S.stream`。`test_posjs.py` 另有两条页面守卫
    （两页必须走 `samplesFor`；`replay.html` 必须走 `frameGaps` 且页面里不许出现 `seq - `）。
  - **不看未来**：`obsOfStation` 内置 `p.t <= t`，回放某时刻只能用该时刻**之前**的样本。
    加任何「预取/缓存未来样本」的优化都会破坏这条。同理**时序平滑也是因果的**：
    `kalmanTrack` 只吃 `t<=光标` 的帧（`smRuns`/`smoothAt` 都带 `p.t <= t`；实测 20 个光标位置
    最多前视 0 ms、0 个未来点）。
  - **测量噪声（`motion.py` 的 `NOISE_DB=2.0`）是平滑/椭圆能验证的前提**：无噪声时定位结果
    恒等于真值（RMS 0.00 m），平滑、残差、置信椭圆全都失去意义。噪声用 SHA-256(sid|t_ms)
    确定性生成（不用随机数库）→ 回放/测试可复现；**逐站位不同**（`ui_server` 必须传 `sid=`），
    否则各台路由器噪声一致 = 又变回「自洽的假数据」。
  - **时序平滑用恒速卡尔曼、不用滑动平均**（`pos.js` 的 `kalmanTrack`，4 状态 x,y,vx,vy）：
    移动目标上 N 点平均滞后 ≈ (N/2)×采样周期，找人场景里滞后就是「指错位置」
    （合成实测：RMS 二者接近 3.5/3.4 m，但滞后 卡尔曼 0.4 m vs 平均 2.7 m）。
    **搜索半径取滤波后协方差 P**（Q+R），不用测量协方差 —— 否则「输出更平滑」会被误当成「更准」，
    给出过小的半径（假自信）。
  - **协方差更新必须用 Joseph 形式** `P=(I−KH)Pp(I−KH)ᵀ+KRKᵀ`：更短的 `P=(I−KH)Pp`
    在浮点下丢对称正定 → 跑几十帧后创新协方差近奇异 → 增益暴冲 → **状态发散**
    （实测真实数据第 84 帧从 −11 m 跳到 12000 m；改 Joseph 后 793 帧全有限）。
    另加「非有限/负方差 → 重置为测量值」兜底，绝不把 NaN 写进轨迹（静默垃圾最糟）。
  - 页面切换语言会带 `?lang=` **整页重载**（`ui_i18n.js` 的设计），因此 JS 动态写出的文案
    切语言后自动刷新，不用各页自己监听。
  - **轨迹导出（GPX/GeoJSON，回放页）三条硬规则**：① 导出**整窗**（不是只导已回放部分）；
    ② **缺口必须断开**（> `ROUTER_MAX_AGE_MS` 或滤波器 `reset` → GPX 多 `<trkseg>` / GeoJSON 多
    `LineString` Feature），跨缺口的直线不是真走出来的路；③ 经纬度**只走 `map.js` 的 `toLatLng`**
    （与地图落点同一个换算），GeoJSON 坐标是 `[lng, lat]`。纯前端，无新接口。
    **④ 导出必须带可信度（2026-09-13）**：否则接收方只看坐标，会把“观测冲突”那段的点当真 ——
    CSV 有 `trust`/`s0` 两列（`sm`/`raw`/`cmp` 三个来源都有）；GPX 每点带
    `<orpah:trust>`/`<orpah:s0>` 扩展（命名空间 `urn:orpah:trust:1`）+ 轨道 `<desc>` 小结；
    GeoJSON 每个 Feature 带**与坐标一一对齐**的 `trusts` 数组（properties 是要素级的，
    没有逐点位置，所以数组对齐并写明，不要让下游去猜）。
  - **对照导出（真值+原始+平滑，2026-09-13 补）**：第四个来源 `cmp` + 第三种格式 **CSV**。
    分工：GPX/GeoJSON 是**地图格式**（“看哪儿偏了”：三组线段 + `properties.source`），
    CSV 是**逐帧对照表**（“偏了多少”：`t,truth_x/y,est_x/y,err_m,sm_x/y,sm_err_m,+经纬度`）——
    比 CDF 多的是**时段定位**（误差大在哪一段）。两条纪律：① 配对与误差 CDF **同一份**配对
    （`S.truthIdx[k]` → `S.frames[i]`/`S.sm[i]`/`S.truth[k]`，按帧时刻精确取真值、不插值）；
    ② **没有真值就不静默退化成单源** —— 选 `cmp` 当场提示“不适用”+`truthWhy` 原因，
    按下导出也不产文件（真值只有模拟环境有，真机看不需要真值的 RMS/椭圆/搜索半径）。
    CSV 里缺口**不补行**：时间戳直接跳（自己会说话）。
  - **告警处置态（2026-09-12，§三 A 方案）**：`case_overtime` **只在「无人接手」时**才报 ——
    `Case.handler` 是与 `status` **正交**的一维（不是新状态；「处置中」是页面派生显示），
    接手人=自由文本（复用审计 `actor`，**不建 operators 表/不做登录**）。改 `cases` 表列名/语义时记住：
    `CREATE TABLE IF NOT EXISTS` **不给老表补列** → `_init_db` 里用 `PRAGMA table_info` + `ALTER TABLE`
    兜迁移（否则老 `orpah.db` 写库报 `no such column`）。`test_alerts.py` 里的假 `Case` 必须带 `handler`
    （缺属性→测试直接崩；多给属性→掩盖真 AttributeError，两种都踩过）。
  - **一键回归 = `run_checks.py`**（2026-09-12）：跑 `test_*.py` 全部离线套件（现 14 个）+
    批量合规用例（`checks_batch.py`，表驱动：黄金样本/SN 边界/parse_sn/报文编解码），
    报告写到 `checks_report.md`（**入库**，同 `host/test_results.txt` 惯例）。
    **改完任何 orpah 代码先跑它**。两条硬规则：① 判定 = 退出码 0 **且** 输出无 `FAIL`/`Traceback`
    （有些脚本自己 catch 异常还往下跑，只看退出码会漏）；② `--e2e`（L1/L2/L3/L3b/防 spoof）
    **必须先在别的终端停掉 orpah-ui** —— demo 与它（:8901 那套端口）串扰会跑出假失败，
    脚本自己也检查并拒绝（退出码 2）。
  - **时钟可信（2026-09-12，无 RTC 设备）**：设备 `ts=0`/缺失 → 协议层跳过时间窗（`verify_report` 已做），
    但**存什么时间必须走唯一入口** `orpah_proto.effective_ts(ts, rx)`（返回 `(ts, src)`）——
    以前 `write_report`/路由器观测/`registry.touch` 各自 `time.time()` 兜底，会有偏差且导致
    「设备流与路由器观测时刻不一致」（定位/回放按时间对齐 → 匹配不到）。**一次算、各处用**；
    `src=server` 必须**留痕**（审计 detail 写 `ts_src=server`，页面可标）。
    **绝不改报文里的 `ts`** —— 它在签名预像里，改了验签就过不了；归一化只用于我们的记录。
    测试：`test_clock.py`（已并入 `run_checks.py`）。
  - **能量轴（免电池客户端，2026-09-13）**：模型 = `energy.py`（三参数：采集 `harvest_mw` /
    储能 `charge_mj` / 每次上报代价 `cost_mj`；**参数是演示标定值，不是实测**）。四条不能碰的约定：
    ① 能量降级的**下限是 L1（HS256）**，**永不降到 L3** —— §8.3 里 L3 是 coverage-only、
    **不能确认人在场**，宁可**如实沉默**（`interval_s=None` → 不发报）也不发无法确认在场的报；
    ② 成因**不新增报文字段**：服务端从两个**在签名覆盖内**的事实（`hdr.level` + `payload.battery_mv`）
    推导 `degraded_reason = "energy" | "key"`（`≤ ORPAH_ALERT_ENERGY_LOW_MV` 且降级 → energy）；
    ③ 沉默必须**归因分叉**：最后一条已签电量低 → `no_report_energy`（warn，等它取能）；
    电量充足 → `no_report`（30s warn → 300s crit，该出警）。**两者不得合并**（处置相反）；
    ④ `/api/status.energy` 与 GET `/api/energy` 的 `on/params/state` **形状必须一致**（页面同一份渲染代码），
    但 `status` 里**不放扫描表**（13 行不该每秒重算/重传）—— **鼎过一回**：曾经 `status.energy`
    就是扁平 `state`，页面按 `{on,params,state}` 读 → 状态格全「—」、输入框不回显、扫描表标不出当前点。
    POST `/api/energy` 里调 `_energy_step(drain=False)`（只重算策略不推电量）以便立即回显。
    测试：`test_energy.py`（51 条，含“各级别间隔单调不增 + 恰好一处有意跳变”的回归锁）+ `test_alerts.py`/`test_server.py`。
  - **查审计事件别读错字段**：`GET /api/ts/events` 返回的键是 **`rows`**（不是 `events`）；
    另注意它按 `etype`/`sn` 在**本地**过滤（值过滤不进 WHERE，见 §0 IoTDB 条），
    所以“某类事件为空”先确认字段名，再确认是不是真没写进去。
  - **限频（§5.8，2026-09-13）**：`ratelimit.py`（令牌桶，纯 `now` 函数、**不睡眠不轮询**）。
    服务端两条防线 **per-SN + per-Router，两条都 peek 通过才各扣一个令牌**（否则被拒的请求
    白吃好设备的额度），放在 `server._handle` 里 **验签之前** —— 限频是为省 ECDSA 的 CPU，不是判真假。
    **必须两条**：per-SN 的 key 取自**尚未验签的 `payload.sn`** → 轮换 SN 就能绕过它
    （`demo_ratelimit.py` 实测：轮换 60 条时 sn 防线丢 0、router 防线丢 34）→
    per-Router（源地址）是兜底；两条都能被「换源」绕过，**不得写成“防住了”**。
    - **丢弃另记 `ratelimit` 事件**，**绝不**记成 `id_reject`：后者是“过了限频但验签链判不合格”，
      混在一起会让「被哪道防线拒」失真，还会污染签名失败率告警。
    - **`ORPAH-FOUND` 故意不限频**（漏一条 = 一个人没被找到）；“某台 Router 刷 FOUND”属 F-12 一类
      （Router 身份），不是限频能解的 —— 这条决定写在 `server._handle` 的注释里，别顺手加上。
    - **参数是部署配置**（`ORPAH_RL_*`；UI 只读 `/api/status.ratelimit.params`，**页面不写死**），
      默认值按“正常 1 条/秒不误伤 + 演示批次 13 条不被拦”选，**不是实测标定**；
      桶表 key 上限（`ORPAH_RL_MAX_KEYS`）必须有 —— key 来自报文内容，是不可信输入。
    - **「限频不是封禁」是回归锁**：`test_ratelimit.py` 里“默认参数下 120s 正常流量零丢弃”
      + “桶回补后立刻放行”两条不许改坏（改动限频参数先看这两条）。
    - 测法照做：**在真实演示数据上刷量**（页面按钮 `flood` / `demo_ratelimit.py`）——
      单帧验签层对“高频刷量”**毫无反应**，只有限频层会动，这就是它存在的理由。
  - **地图代码分两层**：`ui/static/map.js` = **底图源列表 / 条款说明 / 本地坐标↔经纬度换算 /
    底图图层 + 离线回落**的**单一源**（track.html 与 replay.html 共用，两页 `ensureMap()` 都调
    `addBaseLayer(lmap)`）—— 那是**合规相关**的东西
    （各源署名文本、离线限制、按语言两套列表都不同），拄两份迟早漂移；
    **Leaflet 图层管理各页自己写**（实时/回放画法不同：回放轨迹要跳缺口断开）。
  - **离线回落（2026-09-12，B 方案）**：OSM 官方瓦片政策禁止离线/预取 → demo **不带离线底图包**，
    改为瓦片取不到时自动换**本地自绘网格底图**（canvas 现画，不含第三方数据 → 无许可问题）+
    比例尺 + 提示（含「重试底图」）。判据 = **连续 4 张失败且成功数为 0**（个别 404 不误判）；
    自定义源空 URL 直接判离线。演示离线不用拔网线：把自定义瓦片 URL 填成不可达地址。
    PMTiles 区域包（A 方案）未做，做法/合规约束见 ROADMAP §二。
  - 回放地图开图时按「站位 + 轨迹」`fitBounds` **自动框景**：演示场景只有 ~60 m，
    默认 zoom 下几乎看不见（实测 zoom 19 才舒服）。
- **`host/sim.py` 新增「host 数据口」**（`--host <port>` / `Core(host_port=)`，缺省不启用）：
  语义 = SPI MACBUS `DATA_TX`(host 注入→空口转发) / `DATA_RX`(收帧推 host)；帧格式同空口
  `AA 55 TYPE LEN CRC payload`。不加参数完全不影响原有行为（24 项回归仍过）。
- 涉及本 demo 的公共改动只有 `sim.py` 的 HostPort（可选）；orpah 各进程不 import sim
  （`host_bus.py` 独立实现同帧格式，将来换真实 SPI 只替换底层收发）。
- **UI：`python ui_server.py`（浏览器 http://127.0.0.1:8901/，VS Code 任务
  `orpah-ui`）**——内嵌整条链路自动周期会话（REQ-CONNECT→REPORT），页面三层拓扑 +
  ORPAH-REPORT 实时报文流 + L2 协议消息流（方向↑↓/报文/sn/状态/节点）+ 走失表控制。
  两 UI 启动任务（`orpah-ui` / `sim-server-host-sim|tj45|hc01`）**不带端口参数、直接用
  缺省端口**（orpah=8901、tools=8899），一键即跑不弹窗；想换端口用命令行
  `python ... --port <n>`（后端均支持 `--port`，2026-09-09）。
  ⚠ **不要同时跑两个 orpah `ui_server`**（2026-09-13 实测）：`--port` **只改 HTTP**，
  组件端口（`9421/9422/19447`…）是**固定常量** → 两个实例抢同一套端口，现象很有迷惑性：
  后起的那个 `router_up` 在涨、**`server_recv` 却恒为 0**（数据报被先起的收走）→
  它写不出任何观测（`routers.<sid>.<sn>` 全是旧数据）。要并存必须先把那几个常量改成另一套
  （临时脚本里改 `ui_server.CONSOLE_A/HOST_A/…/UDP_SRV` 再 `OrpahApp(...)` 起，见验证脚本的做法）。
  - **页面默认值必须来自服务端单一源**：A/n ← `GET /api/config`；**默认测点 ← `GET /api/stations`**。
    页面里只留一份**离线兜底**，且兜底与服务端种子的一致性由 `test_motion.py` 守卫锁住
    （别在页面里再抄一份"当前演示场景" —— 2026-09-13 实测：后台已 4 台站位、页面仍显示 3 台，
    而且**不报错**，只是少一台、定位看着还挺正常）。
- **Orpah ID SN 硬规则（2026-09-10 用户定）**：
  ① CC = ISO 3166-1 alpha-2，**不套 Crockford 限制**（可含 I/L/O/U，正则 `[A-Z]{2}`）；
  ② **校验位只算 `ORG-UNIQUE`（不含 CC）**——`damm32.py` / `orpah_id.py` / `c/damm32.c`
     的 check 函数输入一律是 ORG-UNIQUE（或 ORG-UNIQUE-CHECK），不含 CC；
  ③ **所有文档与代码样例的 CC 只用 `CN`（中国）**，不使用其它国家——测试向量、黄金样本、
     UI 默认值、自检输出统一 `CN`（黄金样本 `WH01-9AF3C1D2 → B`，整串 `CN-WH01-9AF3C1D2-B`）；
  ④ 工具页（主页头部入口）：`track.html`（定位与轨迹：多路由器持续测 RSSI → 实时定位 +
     轨迹绘制，纯前端模拟）、`rssi.html`（RSSI→距离→2/3/多点定位，canvas 可视化）、
     `sig.html`（ES256/HS256/none 签名验签，后端 `/api/sig`）、
     `checksum.html`（SN 校验码，Damm32/Luhn32/Mod97 三 tab）、
     `damm32.html`（Damm32 构造/验证，计算器在最顶端）。
     checksum/damm32 的 CC 用下拉框（`<datalist>`）+ 支持直接输入 + 提示，CC 输入框回车
     跳 ORG-UNIQUE、右侧实时显示国名、ORG-UNIQUE 自动大写。

## 0b. 编码约定（2026-09-09）：仓库文本一律 UTF-8

- **文件**：git 跟踪文本文件一律 UTF-8（已扫描确认 79 个全合法 UTF-8，无 GBK 文件）。
  新写文件不加 BOM、写 `# -*- coding: utf-8 -*-` 声明。
- **运行时输出**：Windows 控制台默认代码页 GBK/cp936 会把 UTF-8 输出显示成乱码，
  对 emoji（如 ✅）还抛 `UnicodeEncodeError: 'gbk' codec...`。因此**可执行脚本**（打印
  中文/emoji 的）在 import 后必须强制：`for _s in (sys.stdout, sys.stderr):
  _s.reconfigure(encoding="utf-8", errors="replace")`（orpah 各脚本已加；仿照即可）。
- VS Code task（`orpah-ui`/`orpah-demo-cli`）已设 `PYTHONIOENCODING=utf-8` 双保险。
- 检测某文件是否 GBK：python `open(p,'rb').read().decode('utf-8')` 抛错而 `.decode('gbk')`
  成功 → GBK。注意**不要**只读文件前 N 字节判断（会因截断多字节字符误报，2026-09-09 教训）。
- **⚠ 用 PowerShell 5.1 往 HTTP API 发中文会被静默换成 `?`（2026-09-12 实测复现）**：
  `Invoke-RestMethod -ContentType "application/json" -Body '<含中文的 JSON 字符串>'` →
  每个非 ASCII 字符在服务端落库都变成 `?`（实测 `"name":"悬停点Z"` → 库里 `???Z`；
  先前 `"actor":"张警官"` → 事件里 `???`）。**加 `charset=utf-8` 也无效**。
  注意**命令回显里中文是好的**（说明命令文本到了 shell），坏在 PS 组装 body 那一步 ——
  所以别以为是"终端显示乱码"，是**数据真的坏了**。
  - **正路**：① 传 UTF-8 字节（实测有效）：
    `$by=[System.Text.Encoding]::UTF8.GetBytes($json); Invoke-RestMethod ... -Body $by`
    ② 更省事：改用 `C:\Python313\python.exe` 写小脚本（`json.dumps(...).encode("utf-8")`）；
    ③ 或直接在页面上操作（浏览器发的就是 UTF-8）。
  - 破坏是**不可逆**的（服务端收到时就是 `?`，无从还原），只能重新写入正确值。
  - **入库前的自检**：写完中文用 API 读回一眼（`repr()`），别只看 POST 的返回。
  - **同一个坑的第二种用法（2026-09-12 踩到）**：**不要用 PowerShell 改写含中文的源码文件** ——
    `(Get-Content f.py -Raw) -replace ... | Set-Content f.py -Encoding UTF8` 会把所有中文变成乱码
    （`Get-Content` 默认按 ANSI 代码页读 UTF-8 文件），而且**破坏后编译直接语法错**。
    改源码一律用编辑器工具；已在终端里改坏的就 `Remove-Item` 重写。

