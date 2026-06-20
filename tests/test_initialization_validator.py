import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.initialization_validator.service import validate_initialized_disks



def build_success_result(overrides: dict | None = None) -> dict:
    disk = {
        "disk_number": 2,
        "friendly_name": "Realtek USB 3.2 Device",
        "partition_style": "GPT",
        "is_boot": False,
        "is_system": False,
        "is_offline": False,
        "is_read_only": False,
    }
    if overrides:
        disk.update(overrides)

    return {
        "disk_number": 2,
        "passed": True,
        "message": "硬盘初始化完成",
        "disk": disk,
    }



def assert_failed(result: dict, expected_message: str) -> None:
    if result.get("passed"):
        raise AssertionError(f"期望验证失败，实际通过: {result}")
    if expected_message not in result.get("message", ""):
        raise AssertionError(f"期望错误信息包含: {expected_message}，实际为: {result}")



def test_validate_initialized_disks_success() -> None:
    results = validate_initialized_disks([build_success_result()])
    if results != [{"disk_number": 2, "passed": True, "message": "初始化结果验证通过"}]:
        raise AssertionError(f"初始化验证成功结果不正确: {results}")



def test_validate_initialized_disks_failed_initialization() -> None:
    results = validate_initialized_disks(
        [
            {
                "disk_number": 2,
                "passed": False,
                "message": "硬盘初始化失败",
                "disk": None,
            }
        ]
    )
    assert_failed(results[0], "初始化步骤未通过")



def test_validate_initialized_disks_missing_disk() -> None:
    results = validate_initialized_disks([{"disk_number": 2, "passed": True, "message": "硬盘初始化完成", "disk": None}])
    assert_failed(results[0], "缺少硬盘信息")



def test_validate_initialized_disks_wrong_partition_style() -> None:
    results = validate_initialized_disks([build_success_result({"partition_style": "MBR"})])
    assert_failed(results[0], "分区表格式不是 GPT")



def test_validate_initialized_disks_rejects_boot_disk() -> None:
    results = validate_initialized_disks([build_success_result({"is_boot": True})])
    assert_failed(results[0], "启动盘")



def test_validate_initialized_disks_rejects_system_disk() -> None:
    results = validate_initialized_disks([build_success_result({"is_system": True})])
    assert_failed(results[0], "系统盘")



def test_validate_initialized_disks_rejects_offline_disk() -> None:
    results = validate_initialized_disks([build_success_result({"is_offline": True})])
    assert_failed(results[0], "离线状态")



def test_validate_initialized_disks_rejects_read_only_disk() -> None:
    results = validate_initialized_disks([build_success_result({"is_read_only": True})])
    assert_failed(results[0], "只读状态")



def test_validate_initialized_disks_rejects_empty_results() -> None:
    try:
        validate_initialized_disks([])
    except ValueError as exc:
        if "不能为空" not in str(exc):
            raise AssertionError(f"错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("空初始化结果应失败")



def main() -> int:
    try:
        test_validate_initialized_disks_success()
        test_validate_initialized_disks_failed_initialization()
        test_validate_initialized_disks_missing_disk()
        test_validate_initialized_disks_wrong_partition_style()
        test_validate_initialized_disks_rejects_boot_disk()
        test_validate_initialized_disks_rejects_system_disk()
        test_validate_initialized_disks_rejects_offline_disk()
        test_validate_initialized_disks_rejects_read_only_disk()
        test_validate_initialized_disks_rejects_empty_results()
        print("模块3初始化结果验证测试结果: 通过")
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1



if __name__ == "__main__":
    raise SystemExit(main())
