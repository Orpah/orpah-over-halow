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

### 2.3 接真板（步骤 b–e）——**还没做**

真板阶段要换的只有**底层传输**（`DeviceSim(client=…)` 这一个参数），设备逻辑不动。
现在**没有**任何真板传输实现（不写"看着已支持"的东西）。需要先定物理通路，两条候选见
`docs/real-hw-stage2.md` §4：

| 候选 | 数据面 | 待确认 |
|---|---|---|
| **USB→SPI 桥**（CH341A/CH347A） | MACBUS `DATA_TX`/`DATA_RX`（与 `host_bus.py` 同一套帧语义） | SPI 时序/流控、INT 线怎么读；`halow-demo/simulator/tools/sim_config.py` 有**实验性**的同一思路可参考 |
| RJ45 透明桥（TH-RJ45/WNB 固件） | 以太网 L2（`0x88B5` 是否透传） | 广播/MTU/RSSI 哪端可读 |

判据（走通 = 这几条同时成立）：设备能周期发出 REQ-CONNECT/REPORT，**服务端收到并验签通过** ID 上报，
下行 ACCESS-INFO/TRACKING-STATUS 真到达设备（`snapshot()["tracked"]`/`last_status` 有值）。
有了真板以后，`demo_client_sim.py` 的断言就是**同一套判据**（只是链路换了）。

## 3. 未做 / 未验证（如实）

- **真板传输**（串口 AT / USB→SPI 的 MACBUS 数据面）：未实现 —— 步骤 b 需要它，等物理通路确认。
- **SE 真实驱动**（ATECC608B I2C）：现在用 P-256 的演示派生密钥（`demo_key=True`）；
  真机是 SE 内生成、**私钥不可导出**（协议 §6.4）。
- **真实取能/储能标定**：现在是 `energy.py` 的演示标定值（真机标定见 SPEC F-11）。
- **`seen_routers` 目前是写死的演示值**（`AA:BB:CC:DD:EE:FF` / `-42 dBm`）——
  真机上它应当来自"设备看到的路由器"，要等空口侧给出可用读数（且 `xport` 不在签名内，见 F-12）。
- **`trace`/上报重传**：§9 没规定客户端收到 `TRACKING-STATUS` 后要做什么，故仿真器**只记录**、
  不自行发挥（避免编出规范里没有的行为）。
- **没有真机验证**：本文件与 `client_sim.py` 全部结论都来自 PC 仿真（`demo_client_sim.py`），
  真板结论一律要实测后回填。
