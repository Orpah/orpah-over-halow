#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
host_serial.py — **UART（AT 控制台）上的 host 数据面**
=====================================================
真实模块（TX-AH / TH-RJ45 一族）的 UART 数据通路是 **`AT+TXDATA`**：先发命令、收到 `OK` 后
把**一帧原始以太网数据**（含 14 字节以太头）直接写进去。这与 PC 模拟器的 TCP「host 数据口」
**语义相同、线协议不同**：

| 传输 | 上行（host → 模块，DATA_TX） | 下行（模块 → host，DATA_RX） |
|---|---|---|
| `host_bus.HostBus`（TCP，模拟器） | `AA 55 TYPE LEN CRC payload`（`frame_data()`） | 同格式的帧流 |
| **本文件（UART，真机/模拟器固件）** | `AT+TXDATA=<len>` → 等 `OK` → **裸以太网帧** | `FRAME:RX <hex>` 行（需 `AT+SYSDBG=WNB,1` 打开帧打印） |

上层（`ClientHost`/`DeviceSim`）只看见 `send_frame()` / `recv_frame()` —— **换传输不改设备逻辑**。

依依据（都在仓库里，不是猜的）：
  · `T-Halow-RJ45/docs/AT_cmd.md` §`AT+TXDATA`：`AT+TXDATA=[length,txbw,txmcs,priority]`，
    命令返回 OK 后开始发数据；**1-to-many 模式下必须先补 14 字节以太头，长度也含它**。
  · `halow-demo/simulator/host/test_sim.py`：`AT+SYSDBG=WNB,1` → `AT+TXDATA=<len>` → `send_raw(frame)`。
  · `halow-demo/simulator/tools/ui/server.py`：下行从 `FRAME:RX <hex>` 行解析（`SYSDBG=WNB,1` 打开）。
  · `T-Halow-RJ45/tools/thalow_config.py`：控制台会**卡在数据模式**，需要喂填充字节恢复（`resync`）。

⚠ **未在真机验证**（本文件按上面这些资料实现，手上没有板子）。真机首测要确认三件事：
  ① `AT+TXDATA=<len>` 的写法（等号形式？是否要带 `txbw,mcs,priority`？）；
  ② 下行到 host 的格式是否就是 `FRAME:RX <hex>`（真机固件可能不同 → 用 `--dump-lines` 看原样）；
  ③ 数据模式的**粘性**与恢复（本文件的 `resync()` 是照抄 `thalow_config.py` 的做法）。
"""
import socket
import threading
import time

try:
    import serial                      # pyserial
except Exception:                      # pragma: no cover - 没装时给出可读报错
    serial = None

LOG_LINES = 200                        # 保留最近多少行控制台输出（排查用）


def _default_factory(port, baud):
    if serial is None:
        raise RuntimeError("没装 pyserial：pip install pyserial")
    return serial.Serial(port, baud, timeout=0.05, write_timeout=1.0)


class SerialAtBus:
    """把「AT 控制台 + TXDATA 数据模式 + FRAME:RX 帧打印」包成 host 数据口的样子。

    `serial_factory(port, baud)` 可注入（测试用假串口；真机用 pyserial）。
    """

    def __init__(self, port, baud=115200, name="sta", sysdbg="WNB,1",
                 serial_factory=None, log=None, line_log=LOG_LINES):
        self.port, self.baud, self.name = port, baud, name
        self.sysdbg = sysdbg
        self._factory = serial_factory or _default_factory
        self.ser = None
        self.log = log
        self.lines = []                # 最近的控制台行（`--dump-lines` 用；真机排查靠它）
        self._max_lines = int(line_log)
        self.rx_frames = []            # 已解析出的下行以太网帧
        self._buf = b""                # 行缓冲（FRAME:RX 行可能被切成两段到达）
        # 两把锁，各管一事（**必须分开**，否则会死锁）：
        #   `_tx_lock`：串口写入 + 数据模式序列（真机一次只接一条 AT）→ **可重入**
        #     （`send_frame` 会嵌套调 `cmd`）；
        #   `_lock`：行缓冲/帧队列 → 读线程**随时**要能追加（所以它不能和等 OK 的循环用同一把锁；
        #     否则 cmd 等 OK 时把读线程一起阻塞，永远等不到 OK —— 实测踩过这种写法）。
        self._tx_lock = threading.RLock()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reader = None
        # 计数（**分开**计数：丢帧/失败必须看得见，不能静默）
        self.tx_frames = 0             # 成功注入的帧
        self.tx_fail = 0               # 注入失败（无 OK / 写失败）
        self.rx_frames_n = 0           # 解析出的下行帧
        self.bad_lines = 0             # 认不出的行（噪声/半行）
        self.cmd_timeout = 2.0

    # ---------------- 连接 ----------------
    def connect(self, retries=3, interval=0.3):
        for _ in range(max(1, int(retries))):
            try:
                self.ser = self._factory(self.port, self.baud)
            except Exception as e:
                self._say(f"打开串口 {self.port} 失败：{e}")
                time.sleep(interval)
                continue
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()
            if self.sysdbg:
                # 打开帧打印 → 下行才有 `FRAME:RX` 可解析（不做的话只能收到 AT 应答）
                self.cmd(f"AT+SYSDBG={self.sysdbg}", wait=0.3, expect_ok=False)
            return True
        return False

    def close(self):
        self._stop.set()
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass

    def _say(self, *a):
        if self.log:
            self.log("[serial]", *a)

    # ---------------- 控制面（AT） ----------------
    def cmd(self, line, wait=0.5, expect_ok=True):
        """发一条 AT 命令并等应答（返回收到的文本）。

        **逐条**发、发完等应答：真机一次只应答一条，背靠背发会吞掉后面那条
        （`halow-demo/simulator/AGENTS.md` 的实测结论）。
        """
        if self.ser is None:
            return ""
        with self._tx_lock:
            try:
                self.ser.write(line.encode("ascii", "ignore") + b"\r\n")
            except Exception as e:
                self._say(f"写命令失败：{e}")
                return ""
        end = time.monotonic() + wait
        out = ""
        while time.monotonic() < end:
            time.sleep(0.01)
            with self._lock:
                seg = self.lines  # 只读快照
            out = "\n".join(seg[-8:])
            if (expect_ok and "OK" in out) or not expect_ok:
                break
        return out

    def resync(self, attempts=3):
        """从**卡住的数据模式**里恢复（照抄 `T-Halow-RJ45/tools/thalow_config.py` 的做法）。

        为什么会卡：`AT+TXDATA=<len>` 之后模块进入数据模式，若长度没喂满，后续 AT 命令会被
        当成数据字节吃掉。喂填充字节把剩余长度耗完，再确认 `AT` 能回 `OK`。
        """
        for _ in range(int(attempts)):
            try:
                with self._tx_lock:
                    self.ser.write(b"\x55" * 1700)
            except Exception:
                return False
            if "OK" in self.cmd("AT", wait=0.5):
                return True
        return False

    # ---------------- host → 模块（DATA_TX） ----------------
    def send_frame(self, eth_frame):
        """注入一帧**原始以太网帧**（`AT+TXDATA=<len>` → 等 OK → 写裸字节）。

        为什么必须等 OK：不等就把裸字节写进去，模块会把它们当 AT 命令解析（数据模式还没进），
        结果是**一整条链路静默失效**（命令被吃、帧没发出去）。失败一律返回 False 并计数。
        """
        eth = bytes(eth_frame)
        if self.ser is None:
            self.tx_fail += 1
            return False
        if len(eth) < 14:
            self._say(f"帧太短（{len(eth)}B < 14B 以太头）→ 不发")
            self.tx_fail += 1
            return False
        with self._tx_lock:                    # 数据模式是**全局**状态 → 一次只走一条帧
            if not self.cmd(f"AT+TXDATA={len(eth)}", wait=self.cmd_timeout):
                self.tx_fail += 1
                return False
            try:
                self.ser.write(eth)
            except Exception as e:
                self._say(f"写帧失败：{e}")
                self.tx_fail += 1
                return False
        self.tx_frames += 1
        return True

    # ---------------- 模块 → host（DATA_RX） ----------------
    def recv_frame(self, timeout=2.0):
        """取一帧收到的数据（无则 None）。带超时，便于轮询。"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                if self.rx_frames:
                    return self.rx_frames.pop(0)
            time.sleep(0.01)
        return None

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                data = self.ser.read(256)
            except Exception:
                return
            if data:
                self._feed(data)

    def _feed(self, data):
        """按行切分；`FRAME:RX <hex>` → 以太网帧；其余进 `lines`（排查用）。"""
        self._buf += bytes(data)
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            line = raw.rstrip(b"\r").decode("utf-8", "replace").strip()
            if not line:
                continue
            with self._lock:
                self.lines.append(line)
                if len(self.lines) > self._max_lines:
                    del self.lines[:len(self.lines) - self._max_lines]
            if line.startswith("FRAME:RX "):
                hexstr = line[len("FRAME:RX "):].strip().replace(" ", "")
                try:
                    frame = bytes.fromhex(hexstr)
                except ValueError:
                    self.bad_lines += 1
                    continue
                if len(frame) >= 14:
                    with self._lock:
                        if len(self.rx_frames) < 64:
                            self.rx_frames.append(frame)
                    self.rx_frames_n += 1
                else:
                    self.bad_lines += 1
            elif line.startswith("FRAME:TX "):
                pass                            # 自己发的帧回显：不是下行数据
            else:
                pass                            # AT 应答/状态行 → 已在 lines 里

    # ---------------- 观测 ----------------
    def stats(self):
        return {"port": self.port, "baud": self.baud, "tx_frames": self.tx_frames,
                "tx_fail": self.tx_fail, "rx_frames": self.rx_frames_n,
                "bad_lines": self.bad_lines}

    def recent_lines(self, n=20):
        with self._lock:
            return list(self.lines[-int(n):])


class TcpConsoleSerial:
    """把 **TCP 上的 AT 控制台**伪装成串口（`serial_factory` 用）。

    为什么需要它：PC 模拟器（`vendor/halow/sim.py`）的 AT 控制台跑在 TCP 上，而它的
    AT/数据模式行为与真实模块**同源**（同一份规范/同一套实测踩坑）。所以可以拿它**先排练**
    一遍串口数据面的线路协议（`AT+TXDATA` / `FRAME:RX`），把"传输写错了"和"真机固件不同"
    两类问题**分开**——上机前先把前者排干净。（第 b 步的排练用；**不是**真板驱动。）
    """

    def __init__(self, addr, timeout=0.05):
        self.sock = socket.create_connection(addr, timeout=2)
        self.sock.settimeout(timeout)

    def write(self, data):
        self.sock.sendall(bytes(data))

    def read(self, n=256):
        try:
            return self.sock.recv(n)
        except socket.timeout:
            return b""
        except OSError:
            return b""

    def reset_input_buffer(self):
        self.sock.settimeout(0.05)
        try:
            while self.sock.recv(4096):
                pass
        except OSError:
            pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
