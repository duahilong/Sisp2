import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.main as main_module
from app.main import parse_command_line_args, run_minimal_main_flow
from app.modules.disk_info.service import scan_disk_summaries



def build_successful_preflight_report(config_path: str | Path | None = None) -> dict:
    resolved_path = str(config_path) if config_path else "D:\\Code-Project\\Sisp2\\json\\win11.json"
    return {
        "all_passed": True,
        "results": [
            {"name": "管理员权限检查", "passed": True, "message": "当前程序已以管理员权限运行"},
            {"name": "PowerShell 可用性检查", "passed": True, "message": "PowerShell 可正常调用"},
            {"name": "配置文件存在性检查", "passed": True, "message": "配置文件存在"},
            {"name": "配置文件可解析性检查", "passed": True, "message": "配置文件可正常解析"},
        ],
        "config_payload": {
            "config_path": resolved_path,
            "partition_info": {"efi_size_mb": 100, "c_size_gb": 1536},
            "image_info": {"image_path": "D:\\sisp2\\img\\111.GHO"},
            "software_paths": {"ghost64_path": "D:\\sisp2\\sw\\ghost64.exe", "bcdboot_path": "D:\\sisp2\\sw\\bcdboot.exe"},
            "copy_info": {"source_dir": "D:\\常用软件"},
        },
    }



def build_failed_preflight_report(config_path: str | Path | None = None) -> dict:
    return {
        "all_passed": False,
        "results": [
            {"name": "管理员权限检查", "passed": False, "message": "当前程序未以管理员权限运行，请重新以管理员身份启动"},
        ],
        "config_payload": None,
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



def test_parse_command_line_args() -> None:
    args = parse_command_line_args(["-j", "D:\\temp\\demo.json"])
    if args.config_path != "D:\\temp\\demo.json":
        raise AssertionError(f"-j 参数解析结果不正确: {args.config_path}")

    default_args = parse_command_line_args([])
    if default_args.config_path is not None:
        raise AssertionError("未传入 -j 参数时，config_path 应为 None")



def test_successful_main_flow() -> None:
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
    if "管理员权限检查" in output or "PowerShell 可用性检查" in output or "配置文件存在性检查" in output or "配置文件可解析性检查" in output:
        raise AssertionError("运行前检查全部通过时不应输出检查通过信息")
    if "Sisp 当前演示入口" not in output:
        raise AssertionError("主流程成功时未继续进入正式演示入口")

    validate_snapshot(snapshot)
    print(output)
    print(f"最小主流程成功路径测试结果: 通过，已选择硬盘编号 {snapshot['selected_disk_numbers']}")



def test_successful_main_flow_with_custom_json_path() -> None:
    custom_path = "D:\\custom\\demo.json"
    captured = io.StringIO()
    with redirect_stdout(captured):
        snapshot = run_minimal_main_flow(
            input_func=lambda prompt: "a",
            preflight_runner=build_successful_preflight_report,
            config_path=custom_path,
        )

    output = captured.getvalue()
    if f"配置文件: {custom_path}" not in output:
        raise AssertionError("主流程未使用 -j 传入的自定义 JSON 路径")
    if snapshot.get("config_path") != custom_path:
        raise AssertionError("主流程快照中的 config_path 未使用自定义 JSON 路径")



def test_main_uses_json_argument() -> None:
    received_config_paths: list[str | Path | None] = []
    original_runner = main_module.run_minimal_main_flow

    def fake_run_minimal_main_flow(input_func=input, preflight_runner=None, config_path: str | Path | None = None) -> dict:
        received_config_paths.append(config_path)
        return {
            "config_path": str(config_path),
            "partition_info": {},
            "image_info": {},
            "software_paths": {},
            "copy_info": {},
            "disk_count": 1,
            "selected_disk_numbers": [1],
        }

    main_module.run_minimal_main_flow = fake_run_minimal_main_flow
    try:
        captured = io.StringIO()
        with redirect_stdout(captured):
            exit_code = main_module.main(["-j", "D:\\json\\custom.json"])
    finally:
        main_module.run_minimal_main_flow = original_runner

    if exit_code != 0:
        raise AssertionError(f"main 使用 -j 参数时应返回 0，实际为: {exit_code}")
    if received_config_paths != ["D:\\json\\custom.json"]:
        raise AssertionError(f"main 未将 -j 参数传递给主流程: {received_config_paths}")



def test_failed_preflight_main_flow() -> None:
    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            run_minimal_main_flow(
                input_func=lambda prompt: "1",
                preflight_runner=build_failed_preflight_report,
            )
    except RuntimeError as exc:
        if str(exc) != "运行前检查失败":
            raise AssertionError(f"运行前检查失败时异常信息不正确: {exc}")
    else:
        raise AssertionError("运行前检查失败时主流程未中止")

    output = captured.getvalue()
    if "管理员权限检查：当前程序未以管理员权限运行，请重新以管理员身份启动" not in output:
        raise AssertionError("运行前检查失败时未输出失败项错误信息")
    if "Sisp 当前演示入口" in output:
        raise AssertionError("运行前检查失败后主流程不应继续进入正式演示入口")
    if "当前主程序持有的关键数据:" in output:
        raise AssertionError("运行前检查失败后不应输出主流程关键数据")

    print(output)
    print("最小主流程失败路径测试结果: 通过，运行前检查失败后已正确中止")



def main() -> int:
    try:
        test_parse_command_line_args()
        test_successful_main_flow()
        test_successful_main_flow_with_custom_json_path()
        test_main_uses_json_argument()
        test_failed_preflight_main_flow()
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
