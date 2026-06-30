import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.main import run_minimal_main_flow
import app.main as main_module


OUTPUT_PATH = PROJECT_ROOT / "tests" / "invalid_input_results.txt"
TEST_CASES = [
    ("空输入", ""),
    ("非法字符 abc", "abc"),
    ("不存在的磁盘编号 99", "99"),
    ("中文逗号输入 1，2", "1，2"),
]
SAMPLE_DISK_SUMMARIES = [
    {
        "disk_number": 0,
        "model": "System Disk",
        "size_display": "931.51 GB",
        "partition_style": "GPT",
        "bus_type": "NVMe",
        "drive_letters": ["C"],
        "is_boot": True,
        "is_system": True,
    },
    {
        "disk_number": 1,
        "model": "Target Disk",
        "size_display": "476.94 GB",
        "partition_style": "GPT",
        "bus_type": "SATA",
        "drive_letters": [],
    },
]



def build_successful_preflight_report(config_path: str | Path | None = None) -> dict:
    resolved_path = str(config_path) if config_path else str(PROJECT_ROOT / "json" / "win11.json")
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
            "image_info": {"image_path": str(PROJECT_ROOT / "README.md")},
            "software_paths": {"ghost64_path": str(PROJECT_ROOT / "sw" / "ghost64.exe"), "bcdboot_path": str(PROJECT_ROOT / "sw" / "bcdboot.exe")},
            "copy_info": {"source_dir": str(PROJECT_ROOT)},
            "excluded_disk_names": [],
        },
    }



def run_case(case_name: str, user_input: str) -> str:
    captured = io.StringIO()
    status = "通过"
    exception_text = "无"
    original_scan_disk_summaries = main_module.scan_disk_summaries
    original_scan_used_drive_letters = main_module.scan_used_drive_letters

    try:
        main_module.scan_disk_summaries = lambda: SAMPLE_DISK_SUMMARIES
        main_module.scan_used_drive_letters = lambda: []
        with redirect_stdout(captured):
            run_minimal_main_flow(
                input_func=lambda prompt: user_input,
                preflight_runner=build_successful_preflight_report,
            )
    except Exception as exc:
        status = "出现异常"
        exception_text = f"{type(exc).__name__}: {exc}"
    finally:
        main_module.scan_disk_summaries = original_scan_disk_summaries
        main_module.scan_used_drive_letters = original_scan_used_drive_letters

    output = captured.getvalue().rstrip()
    input_display = user_input if user_input else "<空输入>"

    return "\n".join(
        [
            "=" * 80,
            f"测试项: {case_name}",
            f"输入: {input_display}",
            f"结果: {status}",
            f"异常: {exception_text}",
            "输出开始:",
            output or "<无标准输出>",
            "输出结束",
        ]
    )



def main() -> int:
    try:
        sections = [
            "Sisp 第一阶段异常输入自动测试结果",
            "生成时间: 固定时间（安全测试使用模拟磁盘数据）",
            f"输出文件: {OUTPUT_PATH}",
            f"检测到磁盘数量: {len(SAMPLE_DISK_SUMMARIES)}",
        ]

        for case_name, user_input in TEST_CASES:
            sections.append(run_case(case_name, user_input))

        content = "\n\n".join(sections) + "\n"
        OUTPUT_PATH.write_text(content, encoding="utf-8")
        print(f"异常输入测试已完成，结果已写入: {OUTPUT_PATH}")
        return 0
    except Exception as exc:
        print(f"脚本执行失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
