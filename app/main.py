import argparse
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



def run_minimal_main_flow(
    input_func=input,
    preflight_runner: PreflightRunner | None = None,
    config_path: str | Path | None = None,
) -> list[int]:
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
    return prompt_disk_selection(disk_summaries, input_func=input_func)



def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_command_line_args(argv)
        run_minimal_main_flow(config_path=args.config_path)
        return 0
    except Exception as exc:
        print(f"运行失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
