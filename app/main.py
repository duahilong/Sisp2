import argparse
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


PreflightRunner = Callable[[str | Path | None], dict[str, Any]]



def parse_command_line_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("-j", dest="config_path", help="JSON 配置文件路径")
    return parser.parse_args(argv)



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



def run_minimal_main_flow(
    input_func=input,
    preflight_runner: PreflightRunner | None = None,
    config_path: str | Path | None = None,
) -> dict:
    runner = preflight_runner or run_preflight_checks
    preflight_report = runner(config_path)
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

    # 按当前文档边界定义，第一阶段应在用户完成硬盘选择后结束；以下分支中的返回快照仍属于选择结果的演示性承载，后续若严格收敛第一阶段边界，可从这里开始调整。
    if not selected_disk_numbers:
        print("\n用户已退出，未选择硬盘。")
        return build_workflow_snapshot(config_payload, disk_summaries, selected_disk_numbers)

    # 按当前文档边界定义，第一阶段应在用户完成硬盘选择后结束；这里继续构建 snapshot 已超出“选择完成即结束”的严格边界，当前仅用于演示和调试。
    snapshot = build_workflow_snapshot(config_payload, disk_summaries, selected_disk_numbers)
    # 这里打印主程序持有的关键数据，属于选择完成后的演示性输出；若后续严格限定第一阶段边界，可移除或后置到下一阶段入口。
    print("\n当前主程序持有的关键数据:")
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return snapshot



def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_command_line_args(argv)
        snapshot = run_minimal_main_flow(config_path=args.config_path)
        # 这里的完成提示发生在用户完成硬盘选择之后，按“第一阶段止于选择完成”的定义，属于越界的演示性收尾输出。
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
