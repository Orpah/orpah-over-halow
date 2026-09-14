# 客户端设备仿真器（`client_sim.py`）

> 2026-09-14 建。这是 ORPAH 客户端三步走的第一步（用户定的路线图见 `ROADMAP.md` §〇）：
> **① 先在软件里把客户端跑成"设备"** → ② PC + TX-AH 开发板 → ③④⑤ CH32 板 / 定制板。
> 本文件写清：它是什么、怎么跑、**边界与未做**。

## 1. 它是什么（与既有两处的关系）

| 代码 | 角色 | 边界 |
|---|---|---|
| `client.py`（`ClientHost`） | **最薄的会话工装**：连 STA host 口、注入/收帧、自限频闸门 | 不决定"发什么/多久发" |
| `ui_server.py` | 演示整条链路 + 网页（内嵌一台客户端，带行走/能量/页面） | 与网页耦合，不是设备 |
| **`client_sim.py`（`DeviceSim`）** | **客户端设备仿真器**：周期、能力声明、无 RTC、电量、降级、能量、自限频，**全部按设备自己的决定** | 只做协议之上的三件事（见下） |

**它不重新实现协议**（本仓最容易犯、且不会报错的错）：报文用 `orpah_proto` 构、签名用
`orpah_id.Device`、§8.2 选级走 `orpah_id.pick_level`（故障模式表 `orpah_id.LEVEL_MODES`）、
自限频用 `ratelimit`（经 `ClientHost`）、间隔与降级用 `energy.plan`、周期基准用
`energy.NORMAL_INTERVAL_S`（设计常态 60 s）。`test_client_sim.py` 的最后一节用**源码守卫**
钉住这条（出现帧/报文字面量、CRC、JCS 就算失败）。

它只做三件事：

1. **会话节奏** —— REQ-CONNECT → REPORT →（可选）ID-REPORT，与 `ui_server._report_loop` 同序；
2. **设备侧旋钮** —— 周期 / `cap.rtc` / 无时钟 / §8.2 故障注入 / 电量 / 能量模式，
   **每个旋钮都走规范里那条路径**（不直接写死级别）；
3. **可观测** —— 每拍一条结果（`--json` 输出 NDJSON）+ 随时可取 `snapshot()`，便于脚本化验收与真机比对。

## 2. 怎么跑

### 2.1 对本机模拟器（现在就能跑）

```bash
# 终端 1：AP（Router 侧）
python vendor/halow/sim.py --name AP  --role AP  --console 9601 --link 9611
# 终端 2：STA（Client 侧，开 host 数据口 9622）
python vendor/halow/sim.py --name STA --role STA --console 9602 --link 9612 \
       --peer 127.0.0.1:9611 --host 9622
# 终端 3：设备仿真器
python client_sim.py --sta-port 9622 --cycles 3 --every 1 --json
```

也可以直接对 `ui_server` 的 STA host 口（默认 `9422`）：`python client_sim.py --sta-port 9422`。

常用参数：

| 参数 | 作用 |
|---|---|
| `--every` | 会话周期秒；**默认 = 设计常态 60 s**（`energy.NORMAL_INTERVAL_S`，单一源） |
| `--cycles N` | 跑 N 拍后退出（脚本化验收用；0 = 一直跑） |
| `--cap-rtc yes/no/none` | 能力声明 `cap.rtc`（在**签名覆盖内**；`none` = 不声明） |
| `--no-clock` | 无可用时钟 → 报文 `ts=0`（服务端用接收时刻记账；**绝不改报文 ts**） |
| `--level-mode auto\|sign_fail\|se_fail\|no_key` | §8.2 故障注入：传"哪个环节坏了"，级别由 `pick_level` 算 |
| `--battery-mv` | 已签上报里的电量（设备不能抵赖"我快没电了"） |
| `--energy --harvest/--charge/--store` | 能量模式：间隔与级别**由 `energy.plan` 定**；采不敷出时**如实沉默** |
| `--no-self-limit` | 关掉设备侧**自愿**自限频（语义是**延后**不是丢弃） |
| `--register-keystore PATH` | 先把这台演示设备登记进该密钥库（**真机联调时服务端要先认识这个 SN**） |
| `--json` / `--quiet` | 每拍一条 NDJSON / 不打印 |

输出读法：`--json` 模式下每拍一行 `{"tick","ts","every_s","interval_s","level","silent",…}`，
最后一行是 `{"snapshot": …}`；`snapshot` 里
`tracked`（ACCESS-INFO 的走失表命中）、`last_status`（TRACKING-STATUS）、`held`（被延后条数，
**不是丢弃**）、`id_sent`/`req_sent`/`report_sent`（分开计数，合成一个数就分不出握手与上报）。

### 2.2 端到端验收（纯 PC，真链路）

```bash
python demo_client_sim.py        # 约 5 s；退出码 0 = 全过
```

它把设备仿真器接进**完整仿真链路**（STA/AP 模拟器 → Router 桥 → Server 验签）并断言：
L2 双向到达、服务端**验签通过**、无 RTC 设备的 `ts_src=server` 与 `cap_rtc=False`、
电量到达、mark 后设备看到 `tracked=True`/`TRACKED`、**上游两侧零丢弃**（守规矩的设备不该被限频误伤）。

### 2.3 接真板（步骤 b–e）

**b 步走 UART（用户 2026-09-14 定）**：PC 经 Type-C 接 TX-AH 开发板的 **AT 串口**，数据面是
`AT+TXDATA`：

| 方向 | 线上做什么 |
|---|---|
| 上行（host → 模块，DATA_TX） | `AT+TXDATA=<len>` → 等 `OK` → 写**裸以太网帧**（含 14B 以太头，`len` 也含它） |
| 下行（模块 → host，DATA_RX） | `FRAME:RX <hex>` 行（需先 `AT+SYSDBG=WNB,1` 打开帧打印） |

实现 = `host_serial.SerialAtBus`（与 `host_bus.HostBus` **同一套语义、不同线协议**）；
用法：

```bash
python client_sim.py --transport serial --serial-port COM13 --baud 115200 --cycles 2 \
                     --dump-lines 20        # 真机排查：把模块控制台原样打出来
```

**上机前先排练**（纯 PC，不用板子）：

```bash
python demo_client_uart.py    # 用模拟器的 AT 控制台跑同一套 AT+TXDATA/FRAME:RX，双向验收
```

它把两类问题**分开**：① 传输/解析写错了 → 这个脚本能当场抓到；② **真机固件与手册不同** →
只能上机才知道。上机第一件事用 `--dump-lines` 确认三件事：

1. `AT+TXDATA=<len>` 的**写法**（等号形式？要不要带 `txbw,mcs,priority`？）；
2. 下行到 host 到底是不是 `FRAME:RX <hex>`（**本仓两份记录不一致**：`T-Halow-RJ45/docs/AT_cmd.md`
   有 `AT+TXDATA` 且是 1-to-many 模式（要补 14B 以太头），而 `halow-demo/simulator/AGENTS.md`
   的真机实测写着 TX-AH 的 fmac 固件“AT 只有控制面、没有用户数据命令”→ **以实测为准**）；
3. 数据模式的**粘性**与恢复（`resync()` 是照抄 `T-Halow-RJ45/tools/thalow_config.py`）。

**判据**（走通 = 这几条同时成立，与步骤 a 的那套一样）：设备能周期发出 REQ-CONNECT/REPORT，
**服务端收到并验签通过** ID 上报，下行 ACCESS-INFO/TRACKING-STATUS **真到达设备**
（`snapshot()["tracked"]`/`last_status` 有值），上游两侧零丢弃。

**c/d/e（换 MCU / 换载板 / 一体板）** 只换“谁在跑这套状态机”：固件照 `DeviceSim` 写，
接口边界仍是同一套（host 数据口 → 模块），所以**判据不变**。

## 3. 未做 / 未验证（如实）

- **真板实测**：`host_serial.py`（UART/AT 数据面）按 AT 手册 + 模拟器固件实现，
  **未在真机验证**；上机首测要确认的三件事见上面 §2.3（另：`AT+TXDATA` 与 fmac 无数据命令
  这两份记录对不上，以实测为准）。
- **SE 真实驱动**（ATECC608B I2C）：现在用 P-256 的演示派生密钥（`demo_key=True`）；
  真机是 SE 内生成、**私钥不可导出**（协议 §6.4）。
- **真实取能/储能标定**：现在是 `energy.py` 的演示标定值（真机标定见 SPEC F-11）。
- **`seen_routers` 目前是写死的演示值**（`AA:BB:CC:DD:EE:FF` / `-42 dBm`）——
  真机上它应当来自"设备看到的路由器"，要等空口侧给出可用读数（且 `xport` 不在签名内，见 F-12）。
- **`trace`/上报重传**：§9 没规定客户端收到 `TRACKING-STATUS` 后要做什么，故仿真器**只记录**、
  不自行发挥（避免编出规范里没有的行为）。
- **没有真机验证**：本文件与 `client_sim.py` 全部结论都来自 PC 仿真（`demo_client_sim.py`），
  真板结论一律要实测后回填。
