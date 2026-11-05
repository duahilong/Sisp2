# 事故报告：本机数据盘被格式化（2026-11-06）

> 状态：已修复并加装护栏。
> 影响：开发机一块数据盘（D:）分区表被 `diskpart clean` 清空、重新分成
> EFI + Windows + Data 并格式化，盘上项目目录（`D:\Code-Project\Sisp2`）随之不可访问。
> 后果：该盘数据需要专业工具恢复（`clean` + `format quick` 对 SSD 会触发 TRIM，
> 部分数据不可恢复）。

---

## 1. 一句话结论

**不是虚拟机、不是打包脚本、也不是手工操作，而是 `pytest` 在开发机上真实执行了 `diskpart`。**

根因：重构时把分区实现从 `WindowsDiskRunner` 搬进 `PartitionService`，而三个测试用
`runner._run_diskpart = fake` 这种**直接赋值**去 mock。Python 允许给实例挂任意属性，
赋值成功但没有任何代码调用它 —— 于是 `PartitionService` 用自己的实现，真的调起了
`diskpart.exe /s <脚本>`，脚本第一条就是 `select disk N` + `clean`。

---

## 2. 时间线

| 时间 | 事件 |
| --- | --- |
| 步骤 1 完成 | 抽公共 shell 层，提交 `f7c5c2e`，340 个测试通过（此时 mock 仍有效） |
| 步骤 2（进行中） | 分区实现搬进 `app/modules/partitioning/`，`_partition_phase` 改为调 `PartitionService`；**测试尚未同步修改** |
| 紧接着 | 运行 `python -m pytest tests/ -q` |
| 运行中 | 3 个测试触发真实 `diskpart`：`clean` → `convert gpt` → `create` → `format quick` |
| 运行中 | 目标盘的分区表被清空，挂在 D: 的卷消失，`D:\Code-Project\Sisp2` 不可访问 |
| 运行后 | 57 个测试报 `FileNotFoundError` / `NotADirectoryError`；会话工作目录失效（`cwd.session-unusable`），所有工具调用被阻断 |

---

## 3. 技术细节

### 3.1 失效链条

重构前（安全）：

```python
# real_runner.py
def _partition_phase(self, ...):
    self._run_diskpart(wipe_script(...), log)      # ← 测试 mock 的正是 self._run_diskpart

# tests/test_real_execution.py
runner._run_diskpart = fake_diskpart               # ← 赋值在同一个对象上，生效
```

重构后（失控）：

```python
# real_runner.py
def _partition_phase(self, ...):
    outcome = self._partitions.run(PartitionRequest(...))   # ← 转到 PartitionService

# modules/partitioning/service.py
def _run_diskpart(self, ...):
    self._shell.script(self.diskpart, lines, ...)          # ← 真实 diskpart.exe /s

# tests/test_real_execution.py
runner._run_diskpart = fake_diskpart               # ← 仍是"给 runner 挂属性"，无人调用
```

### 3.2 三个真正执行了 diskpart 的测试

| 测试 | 盘号 | 布局 |
| --- | --- | --- |
| `tests/test_real_execution.py::test_runner_releases_efi_letter_even_when_image_fails` | disk 2 | EFI 100 MB + C 3 GB + D auto |
| `tests/test_real_execution.py::test_runner_keeps_efi_letter_when_switch_off` | disk 2 | 同上 |
| `tests/test_settings_wiring.py::test_runner_accepts_runner_options_on_spec` | disk 1 | 同上 |

三者的共同点：都用**直接赋值**（`runner._x = ...`），而不是 `monkeypatch.setattr`。
`monkeypatch.setattr` 对已删除的方法会抛 `AttributeError`（`raising=True` 默认），
所以另外四十多处用 `monkeypatch` 的测试在 setup 阶段就失败了、**没有**执行到 `run()`——
这也是为什么只有 3 个测试造成了破坏，而不是全部。

### 3.3 实际执行的命令

```
select disk 2
clean                      ← 分区表在这一行被清空
convert gpt
（PowerShell 按 GPT 类型查 MSR，删掉，复查）
create partition efi size=100
format quick fs=fat32 label=EFI
assign letter=E            ← 还会占用主机 E:
create partition primary size=3072
format quick fs=ntfs label=Windows
assign letter=F
create partition primary
format quick fs=ntfs label=Data
assign letter=G
```

测试里 `_apply_image` 被 mock 成返回 `(87, "bad image")`，所以镜像没写、`bcdboot` 没跑；
但分区与格式化已经完成。

### 3.4 为什么没有任何机制拦住

| 应有护栏 | 事发时状态 |
| --- | --- |
| 执行层拒绝在测试环境写盘 | 不存在（`PYTEST_CURRENT_TEST` 从未被检查） |
| `WindowsDiskRunner` 需显式授权才能写盘 | 不存在（构造出来就能真写） |
| 测试用虚构盘号（如 999） | 不存在（用 `disk_number=1/2`，正好是真实盘） |
| 改实现后先改测试、再跑全量 | 顺序做反了：先删实现 → 直接跑全量 |

---

## 4. 已实施的修复

### 4.1 新增 `app/safety_guard.py`

三道判断，任一命中即抛 `RealDiskBlocked`（危险命令**不会**被交给操作系统）：

1. 实例未显式授权（`allow_real_disk=False`，代码默认值）；
2. 进程处于 pytest 环境（`PYTEST_CURRENT_TEST` 存在）；
3. 环境变量 `SISP_BLOCK_REAL_DISK=1`。

只拦"写"不拦"读"：

- 外部程序按**程序名**判定：`diskpart` / `format` / `bcdboot` / `dism` / `bootrec`；
- PowerShell 按**写盘 cmdlet** 判定（`Format-Volume` / `Clear-Disk` / `New-Partition` …），
  并且先剥掉注释——`_VERIFY_SCRIPT` 的注释里就有 "bcdboot" 这个词，按文本匹配会误伤。

### 4.2 接入点

| 位置 | 改动 |
| --- | --- |
| `WindowsDiskRunner.__init__` | 新增 `allow_real_disk: bool = False` |
| `WindowsDiskRunner._spawn` | 执行前调 `ensure_command_allowed`（写日志之前，被拦的命令不会看起来像执行过） |
| `WindowsDiskRunner._verify` | PowerShell 校验同样过护栏 |
| `build_real_runner()` | **唯一**显式打开真实写盘的入口（`allow_real_disk=True`） |
| `BootRepairer.__init__` / `_spawn` | 同上（`allow_real_disk` 默认 False） |
| `app/gui_web/api.py` 的 `_boot_repairer()` | GUI 是生产入口，显式传 `allow_real_disk=True` |
| `tests/conftest.py` | autouse fixture 设置 `SISP_BLOCK_REAL_DISK=1`（双保险） |

### 4.3 回归测试 `tests/test_safety_guard.py`（19 条）

覆盖：程序名解析、四类危险程序、只读查询放行、写盘 cmdlet 拦截、注释不误伤、
环境变量与 pytest 检测、`allow_real_disk=True/False` 两端、runner 三种拦截路径、
`BootRepairer` 拦截、异常类型兼容。

其中 `test_runner_blocks_diskpart_without_authorization` 就是**事故现场复现**：
构造 runner（未授权）→ `_run_diskpart(["select disk 999", "clean"])` → 必须抛错。

### 4.4 人工验证（已执行）

```text
[OK] incident scenario blocked (no process spawned)
[OK] production path allowed: diskpart /? exit=999999999 bytes=219
[OK] env block effective inside test environment
（随后 tasklist 确认没有 diskpart 进程残留、TEMP 下没有 sisp-diskpart-*.txt 残留）
```

---

## 5. 复盘：流程上的三条错误（比代码问题更重要）

1. **先删实现、后改测试，然后直接跑全量。**
   正确顺序：改实现 → 立刻改对应测试 → 只跑受影响的单文件 → 再跑全量。
   在"实现与测试不一致"的窗口期跑全量，等于让测试去调用真实系统。

2. **用"直接赋值"mock 一个内部方法。**
   直接赋值不会校验属性是否存在；`monkeypatch.setattr` 至少会抛 `AttributeError`。
   更根本的是：**mock 实现细节**而不是注入依赖。现在改为注入 `ShellRunner` /
   直接构造 `PartitionService`，边界清晰，搬家时编译期就能发现。

3. **测试里用真实盘号（1、2）。**
   即使将来漏了 mock，盘号也不该指向一台真机上的真实磁盘。规则：单元测试的磁盘号
   一律 ≥ 100（或直接用虚构对象），真实盘号只出现在 VM 集成测试里。

---

## 6. 以后的强制检查清单

改动"执行器 / 分区 / 引导 / 镜像"相关代码时，**逐条打勾**：

- [ ] 新增或修改的实现，是否仍能被测试注入 fake（`shell=` / 构造参数），而不是靠 monkeypatch？
- [ ] 是否有任何测试会走到真实的外部命令？用 `pytest -k <相关> -q` 逐个文件跑，确认没有 `diskpart` 进程。
- [ ] 跑全量之前，先在**没有真实数据盘**的机器 / VM 上跑一遍；本机跑必须确认
      `SISP_BLOCK_REAL_DISK=1` 已设置（`tests/conftest.py` 自动设置）。
- [ ] 提交前扫一遍 `grep -rn "disk_number=1\|disk_number=2" tests/`，确认真实盘号没有出现在新的单元测试里。
- [ ] 涉及 `WindowsDiskRunner` / `BootRepairer` 的构造改动，确认 `allow_real_disk` 默认值没有被改成 True。

---

## 7. 相关文件

| 文件 | 作用 |
| --- | --- |
| `app/safety_guard.py` | 护栏实现 |
| `tests/test_safety_guard.py` | 19 条回归测试 |
| `tests/conftest.py` | 全局 `SISP_BLOCK_REAL_DISK=1` |
| `app/application/real_runner.py` | `allow_real_disk` 参数 + `_spawn` / `_verify` 接入 |
| `app/modules/boot/service.py` | 同上 |
| `app/gui_web/api.py` | GUI 生产入口显式授权 |
| `Docs/testing.md` | 测试规范（新增"测试绝不能碰真实磁盘"一节） |
