import json
import sys
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.disk_info.service import scan_disk_summaries
from app.modules.user_interaction.service import print_disk_summaries, prompt_disk_selection
from app.preflight import print_preflight_report, run_preflight_checks


PreflightRunner = Callable[[], dict[str, Any]]



def build_workflow_snapshot(config_payload: dict, disk_summaries: list[dict], selected_disk_numbers: list[int]) -> dict:
    return {
        "config_path": config_payload.get("config_path"),
        "partition_info": config_payload.get("partition_info"),
        "image_info": config_payload.get("image_info"),
        "software_paths": config_payload.get("software_paths"),
        "copy_info": config_payload.get("copy_info"),
        "disk_count": len(disk_summaries),
        "selected_disk_numbers": selected_disk_numbers,
    }



def run_minimal_main_flow(input_func=input, preflight_runner: PreflightRunner | None = None) -> dict:
    runner = preflight_runner or run_preflight_checks
    preflight_report = runner()
    print_preflight_report(preflight_report)
    if not preflight_report.get("all_passed"):
        raise RuntimeError("运行前检查失败")

    config_payload = preflight_report.get("config_payload")
    if not isinstance(config_payload, dict):
        raise RuntimeError("运行前检查未返回可用的配置数据")

    disk_summaries = scan_disk_summaries()
    if not disk_summaries:
        raise RuntimeError("模块1未返回任何硬盘摘要信息")

    print("=" * 80)
    print("Sisp 当前演示入口")
    print("=" * 80)
    print(f"配置文件: {config_payload.get('config_path')}")
    print_disk_summaries(disk_summaries)
    selected_disk_numbers = prompt_disk_selection(disk_summaries, input_func=input_func)

    if not selected_disk_numbers:
        print("\n用户已退出，未选择硬盘。")
        return build_workflow_snapshot(config_payload, disk_summaries, selected_disk_numbers)

    snapshot = build_workflow_snapshot(config_payload, disk_summaries, selected_disk_numbers)
    print("\n当前主程序持有的关键数据:")
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return snapshot



def main() -> int:
    try:
        snapshot = run_minimal_main_flow()
        if snapshot.get("selected_disk_numbers"):
            print(f"\n演示运行完成，已选择硬盘编号 {snapshot['selected_disk_numbers']}")
        else:
            print("\n演示运行结束。")
        return 0
    except Exception as exc:
        print(f"运行失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
