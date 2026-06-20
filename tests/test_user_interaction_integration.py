import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.user_interaction.service import apply_disk_protection, print_disk_summaries, prompt_disk_selection

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



def main() -> int:
    try:
        disks = apply_disk_protection(SAMPLE_DISK_SUMMARIES, [])

        captured = io.StringIO()
        with redirect_stdout(captured):
            print_disk_summaries(disks)

        output = captured.getvalue()
        if "共检测到" not in output:
            raise AssertionError("模块9未正确输出硬盘摘要标题")

        target_number = 1

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
