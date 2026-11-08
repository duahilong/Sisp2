"""分区布局：把 ``partition_info`` 变成一份明确的“要建哪些分区”的列表。

整盘装机的分区结构以前散在三个地方（真实执行器拼 diskpart 脚本、容量预检算
算术、确认页生成脚本预览），加一个数据分区要同时改三处，很容易漂移。这里把
布局收敛成一个纯函数，执行器 / 预检 / 预览 / 界面摘要都消费同一份结果：

    EFI（FAT32） → Windows（NTFS） → Data（NTFS） → Data2（可选，NTFS）

约定：

- ``role`` 同时是盘符字典的键（``efi`` / ``windows`` / ``data`` / ``data2``），
  与 ``drive_letters`` 保持一一对应；
- 盘上**不保留 MSR**：``convert gpt`` 会自动建一个 16 MB 的 MSR，执行器按类型
  删掉它，最终布局就是 EFI + C + D(+E)（见 ``strip_msr_lines``）；
- ``size_mb=None`` 只可能出现在**最后**的数据分区上（业务配置写了
  ``data_size_gb: auto``、或界面永远发的最后一项），表示“吃掉剩余空间”：
  diskpart 收到不带 ``size=`` 的 ``create partition primary``，尺寸由它自己算，
  所以最后一项永远不会容量不足。界面把最后一项的输入框做成只读的预估，
  不再发显式数字；
- 显式项（EFI、C、以及有 E 时的 D）仍然可能顶到盘尾，容量校验把这些算清楚；
- 文件系统统一成规范写法（``NTFS`` / ``FAT32``），拼脚本时再转成
  diskpart 的小写 token；数据分区只允许 NTFS（见 ``DATA_FS_CHOICES``）。
- 容量算术留 ``LAYOUT_SLACK_MB``（GPT 盘首/盘尾各 1 MB，再加 1 MB 冗余）。
  实测（40 GB 盘、diskpart 10.0.19041）：显式分区合计 = 盘容量 − 1 MB 时
  最后一个 ``create`` 失败（``exit=-2147024809``），= 容量 − 2 MB 时成功且
  ``LargestFree=0``。以前这里只留 1 MB，边界配置会“校验通过 + 清盘后失败”。

全部是纯函数，不碰磁盘、不读设置，方便单测。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.application.payload import PartitionLayoutError, require_layout_keys
from app.application.runner_utils import to_int, to_text
from app.units import BYTES_PER_MB, format_size

# EFI 分区：太小放不下引导文件，太大浪费；与设置页的范围保持一致。
EFI_MIN_MB = 64
EFI_DEFAULT_MB = 100

# 装机分区的大小下限：与界面 ``app/gui_web/ui/js/derive.js`` 的
# ``MIN_SYS_GB`` / ``MIN_DATA_GB`` / ``MIN_EXTRA_GB`` 以及设置页的
# ``sysDefaultGb ∈ [60, 2000]`` 同口径（``tests/test_partition_layout.py``
# 会把这两边钉在一起，避免以后只剩一端改了）。
#
# 为什么后端也要拦：界面那套只是表单校验，直连 ``POST /api/prepare``
# 可以传 ``c_size_gb = 1``，那样会一路排到 Ghost 还原才失败——而那时候盘已经清空了。
MIN_C_GB = 60
MIN_DATA_GB = 20
MIN_EXTRA_DATA_GB = 1

# 小尺寸逃生阀：单测与小盘（8 GB VHDX）实测需要低于下限的布局，
# 必须在 payload 里显式写 ``partition_info.allow_small_partitions: true``。
# 界面永远不发这个字段，普通用户也看不到，只给测试/文档用。
ALLOW_SMALL_KEY = "allow_small_partitions"

# C 之后最多再加一个数据分区（E）。盘符池、引导校验都按这个上限设计。
MAX_DATA_PARTITIONS = 2

# MSR（Microsoft Reserved）分区的 GPT 类型；``convert gpt`` 会自动建一个 16 MB 的。
# 我们的最终布局只要 EFI + C + D(+E)，所以建分区前把它按类型删掉（见 strip_msr_lines）。
MSR_GPT = "e3c9e316-0b5c-4db8-817d-f92df00215ae"

# EFI 系统分区（ESP）的 GPT 类型。释放临时盘符时按它找分区号：
# ESP 是隐藏卷，``Get-Partition -DriveLetter`` / ``Get-Volume`` / ``Win32_Volume``
# 都看不到它的临时字母（只有 ``Win32_LogicalDisk`` 看得到），所以“按字母查归属”
# 对 ESP 天然失效；改成“磁盘号 + 分区号”定位才可靠。
ESP_GPT = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"

# 普通数据/系统分区的 GPT 类型（Microsoft Basic Data）；校验分区顺序与类型时用。
BASIC_GPT = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"

# 盘符/对齐之外，GPT 盘真正不能用的空间：盘首 1 MB（主头 + 分区表数组 +
# diskpart 的 1 MB 对齐起点）、盘尾 1 MB（备份头 + 备份表）。
# 实测 40 GB 盘：显式合计 = 容量 − 2 MB 时成功且 LargestFree=0；= 容量 − 1 MB
# 时最后一个 create 失败。这里取 3 MB，给 4Kn / 非整数 MB 容量留 1 MB 冗余。
LAYOUT_SLACK_MB = 3

# 额外数据分区只给 NTFS：
# - FAT32 在 >32 GB 的盘上 diskpart 直接失败，从来没进过候选；
# - exFAT 曾经可选，但它对装机盘没有实际收益（没有 NTFS 的权限/日志/压缩），
#   反而让「格式化参数」多出一个必须跟着设置页走的分叉点。
# 类型本体定义在 ``app.application.payload``（那里需要它来避免循环导入），
# 这里重新导入，保持 ``from app.application.partitions import PartitionLayoutError`` 可用。
DATA_FS_CHOICES = ("NTFS",)

# 规范写法表。**故意保留 exFAT 的映射**：这样传进来的 exFAT 会被规范成
# 「exFAT」而不是静默地被 fallback 换成 NTFS，然后被 DATA_FS_CHOICES 拦下并
# 报一句可读的错误。「不支持的格式要么明确报错，要么必须是用户要的结果」，
# 静默改写成另一种文件系统是最坏的一种。
_FS_CANON = {"ntfs": "NTFS", "exfat": "exFAT", "fat32": "FAT32"}


@dataclass(frozen=True)
class PlannedPartition:
    """一个将要被创建的分区。``size_mb=None`` 表示吃掉剩余空间。

    ``estimate_mb`` 只在 ``size_mb is None`` 时有意义：按盘容量算出来的预估值，
    **只用于界面展示**（“剩余空间（约 455.6 GB）”）。执行器看的是 ``size_mb``，
    所以 diskpart 仍然收到不带 ``size=`` 的命令，由它自己决定最终大小。
    """

    role: str
    label: str
    file_system: str
    size_mb: int | None = None
    letter: str | None = None
    estimate_mb: int | None = None

    @property
    def size_text(self) -> str:
        if self.size_mb is not None:
            return format_size(self.size_mb * BYTES_PER_MB)
        if self.estimate_mb:
            return f"剩余空间（约 {format_size(self.estimate_mb * BYTES_PER_MB)}）"
        return "剩余空间"

    @property
    def is_data(self) -> bool:
        return self.role.startswith("data")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "label": self.label,
            "file_system": self.file_system,
            "size_mb": self.size_mb,
            "estimate_mb": self.estimate_mb,
            "size_display": self.size_text,
            "letter": self.letter,
        }


def partition_info(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """取出 ``partition_info``；不是对象时当作空。"""
    info = (payload or {}).get("partition_info") if isinstance(payload, Mapping) else None
    return info if isinstance(info, Mapping) else {}


def data_partition_count(payload: Mapping[str, Any] | None) -> int:
    """这块盘要建几个数据分区（1 或 2）；盘符分配在解析布局之前就要用到。"""
    raw = partition_info(payload).get("data_partitions")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and raw:
        return min(len(raw), MAX_DATA_PARTITIONS)
    return 1


def allow_small_partitions(payload: Mapping[str, Any] | None) -> bool:
    """这次载荷是否显式放宽了下限（单测 / 小盘实测专用，严格 ``is True``）。"""
    return partition_info(payload).get(ALLOW_SMALL_KEY) is True


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:  # NaN / 非正数
        return None
    return number


def _is_auto_size(value: Any) -> bool:
    """``size_gb`` 是不是“吃掉剩余空间”（缺省 / 空串 / auto）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in ("", "auto")
    return False


def _gb_to_mb(value: Any) -> int | None:
    number = _positive_number(value)
    return None if number is None else round(number * 1024)


def _canonical_fs(value: Any, *, fallback: str = "NTFS") -> str:
    text = str(value or "").strip()
    return _FS_CANON.get(text.lower(), fallback)


def _data_fs(value: Any) -> str:
    """数据分区的文件系统：规范写法 + 只允许 ``DATA_FS_CHOICES`` 里的值。"""
    name = _canonical_fs(value)
    if name not in DATA_FS_CHOICES:
        raise PartitionLayoutError(
            f"数据分区的文件系统不支持：{name}（可选 {' / '.join(DATA_FS_CHOICES)}）"
        )
    return name


def _letter_of(letters: Mapping[str, str] | None, role: str) -> str | None:
    if not letters:
        return None
    text = str(letters.get(role) or "").strip().upper()[:1]
    return text or None


def normalise_data_partitions(raw: Any) -> list[dict[str, Any]]:
    """校验并归一化界面的 ``data_partitions``（保留 ``size_gb`` + 规范文件系统）。

    返回的列表可以直接存回 payload，保证“校验通过”与“存进去的形状”一致。
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PartitionLayoutError("partition_info.data_partitions 必须是数组")
    if not raw:
        raise PartitionLayoutError("partition_info.data_partitions 至少要有一个数据分区")
    if len(raw) > MAX_DATA_PARTITIONS:
        raise PartitionLayoutError(
            f"最多只能有 {MAX_DATA_PARTITIONS} 个数据分区"
            f"（C 之后最多再加 {MAX_DATA_PARTITIONS - 1} 个）"
        )
    normalised: list[dict[str, Any]] = []
    total = len(raw)
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise PartitionLayoutError(f"第 {index} 个数据分区的配置不是对象")
        raw_gb = item.get("size_gb")
        is_last = index == total
        if _is_auto_size(raw_gb):
            # 只有“最后一个数据分区”能吃掉剩余空间：否则后面的分区就没地方放了。
            if not is_last:
                raise PartitionLayoutError(
                    f"第 {index} 个数据分区必须填写大小（只有最后一个可以留空/auto）"
                )
            size_gb: Any = "auto"
        else:
            if _positive_number(raw_gb) is None and _positive_number(item.get("size_mb")) is not None:
                # 也接受直接给 MB 的调用方（手写 REST payload 时更省事）。
                size_gb = round(float(item["size_mb"]) / 1024, 3)
            else:
                size_gb = float(raw_gb) if _positive_number(raw_gb) is not None else 0.0
            if size_gb <= 0:
                raise PartitionLayoutError(f"第 {index} 个数据分区必须填写大小（GB，正整数）")
        file_system = _canonical_fs(item.get("file_system"))
        if file_system not in DATA_FS_CHOICES:
            raise PartitionLayoutError(
                f"第 {index} 个数据分区的文件系统不支持：{file_system}"
                f"（可选 {' / '.join(DATA_FS_CHOICES)}）"
            )
        normalised.append({"size_gb": size_gb, "file_system": file_system})
    return normalised


def _explicit_data_specs(raw: Any) -> list[dict[str, Any]]:
    """把归一化后的数据分区列表换成“MB + 文件系统”，供布局使用。

    ``size_gb == "auto"``（只可能是最后一项）映射成 ``size_mb=None``，
    执行器就会生成不带 ``size=`` 的 ``create partition primary``，
    由 diskpart 自己吃掉剩余空间——比我们估对齐余量更准。
    """
    return [
        {
            "size_mb": None if item["size_gb"] == "auto" else _gb_to_mb(item["size_gb"]),
            "file_system": item["file_system"],
        }
        for item in normalise_data_partitions(raw)
    ]


def plan_partitions(
    payload: Mapping[str, Any] | None,
    *,
    letters: Mapping[str, str] | None = None,
) -> list[PlannedPartition]:
    """把配置解析成明确的分区列表（EFI → Windows → 数据分区）。

    ``letters`` 给出时顺带把盘符写进结果，方便执行器和界面共用同一份计划。
    """
    require_layout_keys(payload)
    info = partition_info(payload)

    raw_efi = _positive_number(info.get("efi_size_mb"))
    efi_mb = max(round(raw_efi), EFI_MIN_MB) if raw_efi is not None else EFI_DEFAULT_MB

    c_gb = _positive_number(info.get("c_size_gb"))
    if c_gb is None:
        raise PartitionLayoutError("配置里 partition_info.c_size_gb 必须是正整数")
    c_mb = round(c_gb * 1024)

    parts: list[PlannedPartition] = [
        PlannedPartition(role="efi", label="EFI", file_system="FAT32", size_mb=efi_mb),
        PlannedPartition(role="windows", label="Windows", file_system="NTFS", size_mb=c_mb),
    ]

    raw_list = info.get("data_partitions")
    if raw_list is None:
        # 老路径：单个数据分区；data_size_gb 缺省/auto 表示吃掉剩余空间。
        raw_size = info.get("data_size_gb")
        if raw_size in (None, "", "auto"):
            data_mb: int | None = None
        else:
            data_mb = _gb_to_mb(raw_size)
            if data_mb is None:
                raise PartitionLayoutError("partition_info.data_size_gb 必须是正数或 auto")
        parts.append(
            PlannedPartition(
                role="data",
                label="Data",
                file_system=_data_fs(info.get("data_fs")),
                size_mb=data_mb,
            )
        )
    else:
        for index, spec in enumerate(_explicit_data_specs(raw_list), start=1):
            size_mb = spec["size_mb"]
            parts.append(
                PlannedPartition(
                    role="data" if index == 1 else f"data{index}",
                    label="Data" if index == 1 else f"Data{index}",
                    file_system=str(spec["file_system"]),
                    size_mb=None if size_mb is None else int(size_mb),
                )
            )

    if letters:
        parts = [replace(part, letter=_letter_of(letters, part.role)) for part in parts]
    return parts


def with_auto_estimates(
    parts: Sequence[PlannedPartition],
    disk_size_bytes: Any,
) -> list[PlannedPartition]:
    """给“吃剩余空间”的分区算一个展示用预估值（不改 ``size_mb``）。

    执行器/diskpart 仍然收不到 ``size=``，最终大小由 diskpart 自己决定；
    这里只是让计划表能显示“剩余空间（约 X GB）”，而不是一句干巴巴的“剩余空间”。
    """
    if not isinstance(disk_size_bytes, int) or isinstance(disk_size_bytes, bool):
        return list(parts)
    if disk_size_bytes <= 0 or not any(part.size_mb is None for part in parts):
        return list(parts)
    explicit = sum(int(part.size_mb) for part in parts if part.size_mb is not None)
    estimate_mb = max(0, disk_size_bytes // BYTES_PER_MB - explicit - LAYOUT_SLACK_MB)
    return [
        part if part.size_mb is not None else replace(part, estimate_mb=estimate_mb)
        for part in parts
    ]


def strip_msr_lines(msr_partition_number: Any) -> list[str]:
    """删掉 ``convert gpt`` 自动生成的 MSR 分区所需的 diskpart 行。

    Windows 在 ``convert gpt`` 时会在盘首放一个 16 MB 的 MSR（实测 Windows 10
    19045 / diskpart 10.0.19041：``p1 Reserved 15.98 MB offset=0.02 MB``）。
    我们的最终布局是 ``EFI + C + D(+E)``，所以在建分区前把它删掉；删完后盘首的
    空闲会与后面的空闲合并，EFI 依然从 1 MB 开始（实测 ``offset=1.00 MB``）。

    执行器把这两行放在**独立的一段** diskpart 里跑，紧跟一次复查（还能不能查到
    MSR）；确认真的没了才去建分区，避免“删失败了却继续建”留下多一个分区。

    ``msr_partition_number`` **必须来自对真实磁盘的查询**（按 ``MSR_GPT`` 类型匹配），
    不能假设“分区 1 就是 MSR”：万一本机没生成 MSR，``select partition 1`` 会失败，
    整条 diskpart 脚本会中止。查不到（``None``）时返回空列表 = 不删任何东西。
    """
    try:
        if isinstance(msr_partition_number, bool):  # True/False 不是分区号
            return []
        number = int(msr_partition_number)
    except (TypeError, ValueError):
        return []
    if number <= 0:
        return []
    return [f"select partition {number}", "delete partition override"]


def layout_summary(parts: Sequence[PlannedPartition]) -> str:
    """``EFI 100MB + C 120GB + D 300GB`` 这种简短描述（报错与界面共用）。"""
    pieces: list[str] = []
    for part in parts:
        if part.role == "efi":
            label = "EFI"
            size = f"{part.size_mb}MB" if part.size_mb else "?"
        elif part.role == "windows":
            label = "C"
            size = f"{part.size_mb / 1024:.0f}GB" if part.size_mb else "?"
        else:
            label = part.label
            size = f"{part.size_mb / 1024:.0f}GB" if part.size_mb else "剩余空间"
        pieces.append(f"{label} {size}")
    return " + ".join(pieces)


def check_minimums(
    parts: Sequence[PlannedPartition],
    disk_size_bytes: Any = None,
    *,
    allow_small: bool = False,
) -> list[str]:
    """分区大小下限（返回阻断项，消息不含硬盘编号）。

    与界面 ``derive.checkLayout`` 同一套语义：

    - 系统分区 ≥ ``MIN_C_GB``（装不下 Windows 的最小可用值）；
    - 第一个数据分区 ≥ ``MIN_DATA_GB``，额外数据分区（E）≥ ``MIN_EXTRA_DATA_GB``；
    - 最后一个数据分区是 auto（``size_mb is None``）时，按
      “容量 − 显式项 − ``LAYOUT_SLACK_MB``”估算它能不能到自己的下限。

    ``disk_size_bytes`` 不给时只看显式项（执行器在清盘前用它兜底，
    它没有盘容量也不影响“显式项太小”这条拦截）。
    """
    if allow_small:
        return []
    problems: list[str] = []
    for part in parts:
        if part.size_mb is None:
            continue
        gb = part.size_mb / 1024
        if part.role == "windows" and gb < MIN_C_GB:
            problems.append(f"系统分区至少 {MIN_C_GB} GB（当前 {gb:.1f} GB）")
            continue
        if part.is_data:
            minimum = MIN_DATA_GB if part.role == "data" else MIN_EXTRA_DATA_GB
            if gb < minimum:
                problems.append(f"{part.label} 分区至少 {minimum} GB（当前 {gb:.1f} GB）")

    if not isinstance(disk_size_bytes, int) or isinstance(disk_size_bytes, bool):
        return problems
    if disk_size_bytes <= 0:
        return problems
    auto = [part for part in parts if part.size_mb is None]
    if not auto:
        return problems
    minimum = MIN_DATA_GB if auto[-1].role == "data" else MIN_EXTRA_DATA_GB
    explicit = sum(int(part.size_mb) for part in parts if part.size_mb is not None)
    left_mb = disk_size_bytes // BYTES_PER_MB - explicit - LAYOUT_SLACK_MB
    if left_mb < minimum * 1024:
        problems.append(
            f"剩下的空间只够 {max(left_mb, 0) / 1024:.1f} GB，"
            f"最后一个数据分区至少要 {minimum} GB"
        )
    return problems


def check_layout(parts: Sequence[PlannedPartition], disk_size_bytes: Any) -> list[str]:
    """容量检查：返回阻断项（空列表 = 放得下），消息不含硬盘编号。"""
    if not isinstance(disk_size_bytes, int) or isinstance(disk_size_bytes, bool):
        return []
    if disk_size_bytes <= 0:
        return []
    # 吃掉剩余空间的那个分区本身不会超容量（最后一个数据分区永远如此），
    # 但显式指定的那些仍然可能超。
    required = sum(int(part.size_mb) for part in parts if part.size_mb is not None) * BYTES_PER_MB
    if required + LAYOUT_SLACK_MB * BYTES_PER_MB <= disk_size_bytes:
        return []
    return [f"{layout_summary(parts)} 超过 {format_size(disk_size_bytes)}"]


def unallocated_bytes(parts: Sequence[PlannedPartition], disk_size_bytes: Any) -> int:
    """按计划建完分区后还剩多少未分配空间（界面用它提示“容量会闲置”）。"""
    if not isinstance(disk_size_bytes, int) or isinstance(disk_size_bytes, bool):
        return 0
    if disk_size_bytes <= 0 or any(part.size_mb is None for part in parts):
        return 0
    used = sum(int(part.size_mb or 0) for part in parts) * BYTES_PER_MB
    return max(disk_size_bytes - used - LAYOUT_SLACK_MB * BYTES_PER_MB, 0)


def diskpart_lines(parts: Sequence[PlannedPartition]) -> list[str]:
    """生成 ``create/format/assign`` 行；调用方负责前面加 select/clean/convert。"""
    lines: list[str] = []
    for part in parts:
        if part.role == "efi":
            lines.append(f"create partition efi size={int(part.size_mb or EFI_DEFAULT_MB)}")
        elif part.size_mb is None:
            lines.append("create partition primary")
        else:
            lines.append(f"create partition primary size={int(part.size_mb)}")
        lines.append(f"format quick fs={part.file_system.lower()} label={part.label}")
        if part.letter:
            lines.append(f"assign letter={part.letter}")
    return lines


# ---------------------------------------------------------------- 脚本生成（纯）
#
# 三段 diskpart 脚本的**行生成**都在这里，而不是散在执行器里：
# ``SispService`` 的确认页预览与真实执行器共用同一个
# 生成器，改段数 / 加语句不可能只改一端——预览与真跑漂移是历史上真实存在过的坑。

def wipe_lines(disk_number: int) -> list[str]:
    """第 1 段：清盘并转 GPT。

    ``convert gpt`` 会顺手在盘首建一个 16 MB 的 MSR（Windows 的行为，不是脚本里的
    命令），所以这一段之后要查到它、单独删掉并复查（见 ``msr_delete_lines``）。
    """
    return [f"select disk {int(disk_number)}", "clean", "convert gpt", "exit"]


def msr_delete_lines(disk_number: int, msr_partition: int | None) -> list[str]:
    """第 2 段：只删 MSR，不顺手建分区。

    单独一段是为了"删完立刻复查"：和建分区合在一条脚本里，删失败就只能看整条
    脚本的退出码，定位不到哪一步，也没机会在清盘后马上收手。
    ``msr_partition`` 必须来自对真实磁盘的查询；``None`` 表示不删任何东西。
    """
    return [f"select disk {int(disk_number)}", *strip_msr_lines(msr_partition), "exit"]


def create_partition_lines(disk_number: int, parts: Sequence[PlannedPartition]) -> list[str]:
    """第 3 段：建 EFI / C / D(+E)；最后一项不带 ``size=``（吃满剩余空间）。"""
    return [f"select disk {int(disk_number)}", *diskpart_lines(parts), "exit"]


def preview_segments(
    disk_number: int,
    parts: Sequence[PlannedPartition],
) -> list[dict[str, Any]]:
    """确认页用的分段预览（真实脚本就是这些 ``lines``）。

    ``dynamic`` 为真的段在执行时才知道具体行（MSR 分区号要现场查），
    预览里用 ``REM`` 占位并附一句 ``note``。
    """
    return [
        {
            "key": "wipe",
            "title": "清盘 + 转 GPT",
            "lines": wipe_lines(disk_number),
            "dynamic": False,
            "note": "convert gpt 会自动建 16 MB 的 MSR，下一步删掉它",
        },
        {
            "key": "msr",
            "title": "删除 convert gpt 自动生成的 MSR（删完立刻复查）",
            "lines": [
                f"select disk {int(disk_number)}",
                "REM 实际执行时按 GPT 类型查出 MSR 分区号（查不到就跳过本段）",
                "REM select partition <MSR 分区号>",
                "REM delete partition override",
                "exit",
            ],
            "dynamic": True,
            "note": "查询失败会中止，绝不猜一个分区号去删",
        },
        {
            "key": "create",
            "title": "建分区（最后一项不带 size=，由 diskpart 吃掉剩余空间）",
            "lines": create_partition_lines(disk_number, parts),
            "dynamic": False,
            "note": "",
        },
    ]


def preview_text(disk_number: int, parts: Sequence[PlannedPartition]) -> str:
    """把三段预览拼成一段可直接展示的文本（确认页用）。"""
    segments = preview_segments(disk_number, parts)
    lines = ["REM === 分区脚本预览：确认页展示的就是真实会执行的命令 ==="]
    for index, segment in enumerate(segments, start=1):
        lines.append(f"REM === {index}/{len(segments)} {segment['title']} ===")
        lines.extend(segment["lines"])
        if segment["note"]:
            lines.append(f"REM 说明：{segment['note']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 分区验收（纯）

def is_reserved_partition(part: Mapping[str, Any]) -> bool:
    """这个分区是不是 MSR / 保留分区。

    ``convert gpt`` 会自动在盘首建一个 16 MB MSR，执行器删掉它之后会**立刻复查**，
    分区验收里再查一次：多出来一个 Reserved 就说明删除没生效或者 MSR 又回来了。
    """
    gpt = str(part.get("gpt_type") or "").strip().lower().strip("{}")
    if gpt == MSR_GPT:
        return True
    return str(part.get("type") or "").strip().lower() == "reserved"


def partition_verdict(
    payload: Mapping[str, Any],
    parts: Sequence[PlannedPartition],
) -> tuple[bool, str, dict[str, Any]]:
    """对照**分区计划**逐项验证盘上的实际结果（分区验收的唯一判据）。

    这一关只看“分区做对了没有”，不要求镜像/引导已经写进去，所以它在
    Ghost 还原（最长 2 小时）之前就能把问题拦住。检查项与计划的对应关系：

    ==================  ==========================================================
    分区表与数量        GPT、分区数 == 计划数、没有 Reserved/MSR
    顺序与类型          按 offset 排序后的第 i 个分区，GPT 类型必须是计划里的那个
                        （EFI → ESP GUID，C/D/E → Basic Data）
    容量                显式分区 |实际 − 计划| ≤ 1 MB；最后那个 auto 分区不能用显式
                        值去比，但要够自己的下限
    容量闭合            sum(实际分区) + GPT 首尾 2 MB == 盘容量（±2 MB）
    盘符                计划里的盘符必须真的挂在这个分区上（C/D/E 硬校验；ESP 的
                        临时字母在 Get-Partition 里看不到，用 WMI 关联查，查不到
                        只记 warning）
    卷标                EFI / Windows / Data / Data2 —— 与计划一致
    文件系统            FAT32 / NTFS / exFAT —— 与计划一致
    盘已吃满            LargestFreeExtent ≤ 2 MB（最后一个分区不带 size=）
    ==================  ==========================================================

    纯函数：输入是 PowerShell 查到的真实分区表 + 计划，方便用假 payload 测每一条
    失败分支。返回 ``(ok, message, details)``，``details`` 里带 ``problems`` /
    ``warnings`` / ``efi_partition``（供收尾释放盘符复用）。
    """
    actual = payload.get("partitions") or []
    if isinstance(actual, Mapping):
        actual = [actual]
    actual = [part for part in actual if isinstance(part, Mapping)]
    style = str(payload.get("partition_style") or "?").strip().upper()
    disk_size = to_int(payload.get("disk_size_bytes")) or 0
    largest_free = to_int(payload.get("largest_free")) or 0

    problems: list[str] = []
    warnings: list[str] = []

    if style != "GPT":
        problems.append(f"分区表不是 GPT（{style}）")

    if len(actual) != len(parts):
        problems.append(f"分区数与计划不符（期望 {len(parts)} 个，实际 {len(actual)} 个）")

    reserved = [part for part in actual if is_reserved_partition(part)]
    if reserved:
        problems.append("盘上还有保留分区（MSR），预期布局是 EFI + C + D(+E)")

    # 按物理顺序（offset）对照计划：第 i 个分区应当就是计划里的第 i 个
    ordered = sorted(actual, key=lambda part: to_int(part.get("offset_bytes")) or 0)
    for index, (got, want) in enumerate(zip(ordered, parts, strict=False), start=1):
        where = f"第 {index} 个分区（{want.label}）"
        expect_gpt = ESP_GPT if want.role == "efi" else BASIC_GPT
        got_gpt = str(got.get("gpt_type") or "").strip("{}").lower()
        if got_gpt and got_gpt != expect_gpt:
            problems.append(f"{where}的 GPT 类型不对（{got_gpt or '空'}）")

        got_mb = (to_int(got.get("size_bytes")) or 0) / BYTES_PER_MB
        if want.size_mb is not None and abs(got_mb - want.size_mb) > 1:
            problems.append(f"{where}大小不符：计划 {want.size_mb} MB，实际 {got_mb:.0f} MB")

        # 盘符：ESP 的临时字母在 Get-Partition 里看不到（隐藏卷），
        # WMI 关联能查到；两者都查不到才算问题（ESP 只告警，因为它收尾就释放）。
        got_letter = to_text(got.get("drive_letter")) or to_text(got.get("mounted_letter"))
        want_letter = to_text(want.letter)
        if want_letter:
            if not got_letter:
                message = f"{where}没有挂上计划盘符 {want_letter}"
                (warnings if want.role == "efi" else problems).append(message)
            elif got_letter.strip().upper()[:1] != want_letter.strip().upper()[:1]:
                problems.append(
                    f"{where}盘符不符：计划 {want_letter}:，实际 {got_letter.upper()}:"
                )

        got_label = to_text(got.get("label"))
        if got_label is not None and got_label.casefold() != want.label.casefold():
            problems.append(f"{where}卷标不符：计划 {want.label}，实际 {got_label}")

        got_fs = to_text(got.get("file_system"))
        if got_fs is not None and got_fs.casefold() != want.file_system.casefold():
            problems.append(f"{where}文件系统不符：计划 {want.file_system}，实际 {got_fs}")

    # 最后一个分区是 auto（不带 size=）：核对它吃到了下限以上，且没有留下大块空闲
    explicit_mb = sum(int(part.size_mb) for part in parts if part.size_mb is not None)
    if disk_size > 0:
        used_mb = sum((to_int(part.get("size_bytes")) or 0) for part in actual) / BYTES_PER_MB
        slack_mb = disk_size / BYTES_PER_MB - used_mb
        # GPT 首尾各 1 MB 是正常开销；超出 2 MB 说明中间留了未分配
        if slack_mb > 2:
            problems.append(f"盘上有 {slack_mb:.0f} MB 未分配（最后分区应当吃满）")
        if abs(slack_mb) > 2 and used_mb > 0:
            problems.append(
                f"容量对不上：分区合计 {used_mb:.0f} MB，盘容量 {disk_size / BYTES_PER_MB:.0f} MB"
            )
        auto = [part for part in parts if part.size_mb is None]
        if auto and used_mb > 0:
            auto_mb = used_mb - explicit_mb
            minimum_gb = MIN_DATA_GB if auto[-1].role == "data" else MIN_EXTRA_DATA_GB
            if auto_mb < minimum_gb * 1024:
                problems.append(
                    f"{auto[-1].label} 只剩 {auto_mb / 1024:.1f} GB，低于下限 {minimum_gb} GB"
                )
    if largest_free > 2 * BYTES_PER_MB:
        problems.append(f"盘上还有 {format_size(largest_free)} 未分配（最后分区应当吃满）")

    summary = "、".join(
        f"#{part.get('number')}"
        f"{('(' + str(part.get('drive_letter') or part.get('mounted_letter')) + ':)' if (part.get('drive_letter') or part.get('mounted_letter')) else '')}"
        f"={format_size(part.get('size_bytes'))}"
        f"{('/' + str(part.get('label'))) if part.get('label') else ''}"
        for part in ordered
    )
    esp = next(
        (
            to_int(part.get("number"))
            for part in ordered
            if str(part.get("gpt_type") or "").strip("{}").lower() == ESP_GPT
        ),
        None,
    )
    details: dict[str, Any] = {
        "partition_style": style,
        "partitions": actual,
        "disk_size_bytes": disk_size,
        "largest_free": largest_free,
        "efi_partition": esp,
        "problems": problems,
        "warnings": warnings,
        "check": "partition_phase",
    }
    if problems:
        return False, "；".join(problems) + f"（实际：{summary or '无分区'}）", details
    message = f"GPT、{len(actual)} 个分区与计划一致：{summary}"
    if warnings:
        message += "；" + "；".join(warnings)
    return True, message, details


# ---------------------------------------------------------------- Ghost 目标
def ghost_target(disk_number: Any, parts: Sequence[PlannedPartition]) -> tuple[int, int]:
    """Ghost 的 ``dst``：``(1 基磁盘号, 1 基分区号)``。

    Ghost 11.x 的编号口径与 Windows 不同：**磁盘和分区都从 1 开始**，而且只认
    ``磁盘:分区``，不认盘符。所以这里要换算，且换算依据必须是**计划**：

    分区验收（``layout`` 步）保证实际分区表与计划逐项一致（数量、顺序、GPT 类型、
    大小），因此"计划里 windows 的位置"就是"实际分区号"，不需要等运行期查分区表。

    两条前置条件不满足就直接拒绝，不猜：
    - EFI 必须是第 1 个分区（Ghost 只写 Windows 分区，EFI 编号不参与，但要保证顺序）；
    - 计划里必须有 windows 分区。
    """
    roles = [str(getattr(part, "role", "")) for part in parts]
    if not roles:
        raise PartitionLayoutError("分区计划为空，无法确定 Ghost 目标")
    if roles[0] != "efi":
        raise PartitionLayoutError("Ghost 写入要求 EFI 是 1 号分区（当前计划不符）")
    if "windows" not in roles:
        raise PartitionLayoutError("分区计划里没有 Windows 分区，无法写入镜像")
    return int(disk_number) + 1, roles.index("windows") + 1
