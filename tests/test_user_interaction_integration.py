import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.disk_info.service import scan_disk_summaries
from app.modules.user_interaction.service import print_disk_summaries, prompt_disk_selection



def main() -> int:
    try:
        disks = scan_disk_summaries()
        if not disks:
            raise RuntimeError("没有检测到可供显示的硬盘摘要信息")

        captured = io.StringIO()
        with redirect_stdout(captured):
            print_disk_summaries(disks)

        output = captured.getvalue()
        if "共检测到" not in output:
            raise AssertionError("模块9未正确输出硬盘摘要标题")

        available_numbers = [disk.get("disk_number") for disk in disks if isinstance(disk.get("disk_number"), int)]
        if not available_numbers:
            raise AssertionError("模块1返回的硬盘摘要中没有有效的硬盘编号")

        selectable_numbers = [number for number in available_numbers if number != 0]
        target_number = selectable_numbers[0] if selectable_numbers else available_numbers[0]

        selected = prompt_disk_selection(disks, input_func=lambda prompt: str(target_number))
        if selected != [target_number]:
            raise AssertionError(f"模块9选择结果不正确: {selected}")

        print(output)
        print(f"模块9集成测试结果: 通过，已选择硬盘编号 {selected}")
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
