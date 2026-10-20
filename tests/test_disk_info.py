import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.modules.disk_info.service as disk_info_module
from app.modules.disk_info.service import (
    INVALID_SIZE_DISPLAY,
    SUMMARY_POWERSHELL_SCRIPT,
    UNKNOWN_SIZE_DISPLAY,
    format_size,
    scan_disk_summaries,
    summarize_disk,
)


def assert_equal(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}，期望: {expected}，实际: {actual}")



def test_format_size() -> None:
    assert_equal(format_size(0), "0.00 B", "0 字节格式化结果不正确")
    assert_equal(format_size(1), "1.00 B", "1 字节格式化结果不正确")
    assert_equal(format_size(1024), "1.00 KB", "1 KB 格式化结果不正确")
    assert_equal(format_size(1024**2), "1.00 MB", "1 MB 格式化结果不正确")
    assert_equal(format_size(1024**3), "1.00 GB", "1 GB 格式化结果不正确")



def test_format_size_invalid_values() -> None:
    assert_equal(format_size(None), UNKNOWN_SIZE_DISPLAY, "None 应显示为未知")
    assert_equal(format_size(True), INVALID_SIZE_DISPLAY, "bool 值应显示为大小异常")
    assert_equal(format_size(False), INVALID_SIZE_DISPLAY, "bool 值应显示为大小异常")
    assert_equal(format_size("1024"), INVALID_SIZE_DISPLAY, "字符串值应显示为大小异常")
    assert_equal(format_size(-1), INVALID_SIZE_DISPLAY, "负数应显示为大小异常")
    assert_equal(format_size(math.nan), INVALID_SIZE_DISPLAY, "NaN 应显示为大小异常")
    assert_equal(format_size(math.inf), INVALID_SIZE_DISPLAY, "inf 应显示为大小异常")



def test_summarize_disk_with_invalid_size() -> None:
    summary = summarize_disk(
        {
            "disk_number": 7,
            "friendly_name": " Demo Disk ",
            "serial_number": "SN-DEMO",
            "unique_id": "UID-DEMO",
            "size_bytes": -1,
            "partition_style": "GPT",
            "bus_type": "USB",
            "partitions": [
                {
                    "drive_letter": "E",
                    "volume": None,
                }
            ],
        }
    )

    assert_equal(summary.get("disk_number"), 7, "磁盘编号摘要不正确")
    assert_equal(summary.get("model"), "Demo Disk", "磁盘型号摘要不正确")
    assert_equal(summary.get("serial_number"), "SN-DEMO", "硬盘序列号摘要不正确")
    assert_equal(summary.get("unique_id"), "UID-DEMO", "硬盘 UniqueId 摘要不正确")
    assert_equal(summary.get("size_display"), INVALID_SIZE_DISPLAY, "非法容量摘要显示不正确")
    assert_equal(summary.get("drive_letters"), ["E"], "盘符摘要不正确")



def test_summarize_disk_includes_safety_flags() -> None:
    summary = summarize_disk(
        {
            "disk_number": 0,
            "friendly_name": "System Disk",
            "size_bytes": 1024,
            "partition_style": "GPT",
            "bus_type": "NVMe",
            "is_boot": True,
            "is_system": True,
            "is_offline": False,
            "is_read_only": True,
            "partitions": [],
        }
    )

    assert_equal(summary.get("is_boot"), True, "is_boot 摘要不正确")
    assert_equal(summary.get("is_system"), True, "is_system 摘要不正确")
    assert_equal(summary.get("is_offline"), False, "is_offline 摘要不正确")
    assert_equal(summary.get("is_read_only"), True, "is_read_only 摘要不正确")



def test_scan_disk_summaries_uses_lightweight_script() -> None:
    calls: list[str] = []
    original_runner = disk_info_module.run_powershell_json

    def fake_run_powershell_json(script: str) -> list[dict]:
        calls.append(script)
        return [
            {
                "disk_number": 1,
                "friendly_name": "Target Disk",
                "serial_number": "SN-1",
                "unique_id": "UID-1",
                "size_bytes": 1024,
                "partition_style": "GPT",
                "bus_type": "USB",
                "partitions": [{"drive_letter": "E"}],
            }
        ]

    disk_info_module.run_powershell_json = fake_run_powershell_json
    try:
        summaries = scan_disk_summaries()
    finally:
        disk_info_module.run_powershell_json = original_runner

    if calls != [SUMMARY_POWERSHELL_SCRIPT]:
        raise AssertionError("磁盘摘要扫描应使用轻量 PowerShell 脚本")
    if "Get-Partition -DiskNumber" in SUMMARY_POWERSHELL_SCRIPT:
        raise AssertionError("轻量磁盘摘要脚本不应按每块磁盘重复调用 Get-Partition")
    if "Get-Partition -ErrorAction SilentlyContinue" not in SUMMARY_POWERSHELL_SCRIPT:
        raise AssertionError("轻量磁盘摘要脚本应一次性获取分区信息")
    assert_equal(summaries[0].get("drive_letters"), ["E"], "轻量扫描结果盘符摘要不正确")



def print_disk_summary(disks: list[dict]) -> None:
    print("=" * 80)
    print(f"共检测到 {len(disks)} 块磁盘")
    print("=" * 80)

    for disk in disks:
        drive_letters = "，".join(disk.get("drive_letters") or []) or "无"
        print(
            f"硬盘编号: {disk.get('disk_number')} | "
            f"硬盘型号: {disk.get('model')} | "
            f"硬盘容量: {disk.get('size_display')} | "
            f"分区表格式: {disk.get('partition_style')} | "
            f"连接方式: {disk.get('bus_type')} | "
            f"包含分区的盘符: {drive_letters}"
        )



def main() -> int:
    try:
        test_format_size()
        test_format_size_invalid_values()
        test_summarize_disk_with_invalid_size()
        test_summarize_disk_includes_safety_flags()
        test_scan_disk_summaries_uses_lightweight_script()
        print("模块1测试结果: 通过")
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
