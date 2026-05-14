import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.main import run_minimal_main_flow
from app.modules.disk_info.service import scan_disk_summaries



def build_successful_preflight_report() -> dict:
    return {
        "all_passed": True,
        "results": [
            {"name": "管理员权限检查", "passed": True, "message": "当前程序已以管理员权限运行"},
            {"name": "PowerShell 可用性检查", "passed": True, "message": "PowerShell 可正常调用"},
            {"name": "配置文件存在性检查", "passed": True, "message": "配置文件存在"},
            {"name": "配置文件可解析性检查", "passed": True, "message": "配置文件可正常解析"},
        ],
    }



def validate_snapshot(snapshot: dict) -> None:
    required_keys = [
        "config_path",
        "partition_info",
        "image_info",
        "software_paths",
        "copy_info",
        "disk_count",
        "selected_disk_numbers",
    ]

    missing_keys = [key for key in required_keys if key not in snapshot]
    if missing_keys:
        raise AssertionError(f"主流程快照缺少字段: {'、'.join(missing_keys)}")

    if not snapshot.get("config_path"):
        raise AssertionError("config_path 不能为空")

    if not isinstance(snapshot.get("disk_count"), int) or snapshot.get("disk_count") <= 0:
        raise AssertionError("disk_count 必须为大于 0 的整数")

    selected_disk_numbers = snapshot.get("selected_disk_numbers")
    if not isinstance(selected_disk_numbers, list) or not selected_disk_numbers:
        raise AssertionError("selected_disk_numbers 必须为非空列表")

    for number in selected_disk_numbers:
        if not isinstance(number, int):
            raise AssertionError("selected_disk_numbers 中的每一项都必须为整数")



def main() -> int:
    try:
        disk_summaries = scan_disk_summaries()
        if not disk_summaries:
            raise RuntimeError("没有检测到可供测试的硬盘摘要信息")

        available_numbers = [disk.get("disk_number") for disk in disk_summaries if isinstance(disk.get("disk_number"), int)]
        if not available_numbers:
            raise RuntimeError("没有可用的硬盘编号，无法完成主流程测试")

        selectable_numbers = [number for number in available_numbers if number != 0]
        target_number = selectable_numbers[0] if selectable_numbers else available_numbers[0]

        captured = io.StringIO()
        with redirect_stdout(captured):
            snapshot = run_minimal_main_flow(
                input_func=lambda prompt: str(target_number),
                preflight_runner=build_successful_preflight_report,
            )

        output = captured.getvalue()
        if "运行前检查结果:" not in output:
            raise AssertionError("主流程未输出运行前检查结果")

        validate_snapshot(snapshot)
        print(output)
        print(f"最小主流程测试结果: 通过，已选择硬盘编号 {snapshot['selected_disk_numbers']}")
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
