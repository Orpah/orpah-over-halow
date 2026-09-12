# vendor/halow —— 空口仿真副本（来源：halow-demo）

这目录里的三个文件是**从 `halow-demo` 复制过来的**，不是本项目自己写的：

| 文件 | 上游路径 | 作用 |
|---|---|---|
| `sim.py` | `halow-demo/simulator/host/sim.py` | 空口仿真（AP/STA、关联、信道、RSSI/路径损耗、host 数据口） |
| `devprofiles.py` | `halow-demo/simulator/host/devprofiles.py` | 设备方言/家族画像（`sim.py` 必需依赖） |
| `blang.py` | `halow-demo/simulator/host/blang.py` | 控制台叙事行的多语言文案（`sim.py` 可选依赖，缺了会退化） |

## 来源与校验

- 上游仓库：`F:\git\halow-demo`（GitHub: langhua/halow-demo）
- 上游提交：`dd4ab5ab6c8f1da84e998cafea047751b0a43c19`（2026-09-12，main）
- 复制方式：**逐字节复制**，复制后逐个对比 `git hash-object` 与上游一致（无任何改动）：
  - `sim.py` `a923cdb4f583e3467c749df1f428da0cc64bee1a`
  - `devprofiles.py` `4d1747b3536b0132a7ab42e38b375c5020cae70f`
  - `blang.py` `51d0986a0f25b5b76412bfa4aa74236b327365c5`

## 为什么要副本（而不是依赖上游）

用户明确要求：**本项目要能像现在这样独立运行，不用先启动 halow-demo**（零硬件、单进程一键 demo）。
上游 `halow-demo` 继续作为**空口侧的权威源**（真实链路、真机联调、抓包那套都在那边）。

## 同步约定（改上游后怎么办）

1. 上游 `sim.py` / `devprofiles.py` / `blang.py` 有改动时，重新复制并在本文件更新「上游提交 + 三个 hash」。
2. 校验副本是否与上游一致（上游在本机时）：

```powershell
foreach ($f in 'sim.py','devprofiles.py','blang.py') {
  $a = git hash-object "vendor/halow/$f"
  $b = git -C F:\git\halow-demo hash-object "simulator/host/$f"
  "$f  $a  $b  " + $(if ($a -eq $b) { 'SAME' } else { 'DIFF —— 需要同步' })
}
```

3. **本项目对空口的任何修改都必须回上游**（否则就是两份实现漂移——本仓库最忌讳的那种不一致）。
   本项目自己的代码只通过 `sys.path` 引用这个目录（`HOST_DIR`），不改这三个文件。

## 引用点

`ui_server.py` / `demo_l1.py` / `demo_l2.py` / `demo_l3.py` / `demo_l4.py` / `demo_spoof.py`
里都有一段：

```python
HOST_DIR = os.path.join(HERE, "vendor", "halow")
if HOST_DIR not in sys.path:
    sys.path.insert(0, HOST_DIR)
import sim
```
