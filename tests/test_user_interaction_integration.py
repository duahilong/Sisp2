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


def test_print_disk_summaries_output() -> None:
    disks = apply_disk_protection(SAMPLE_DISK_SUMMARIES, [])

    captured = io.StringIO()
    with redirect_stdout(captured):
        print_disk_summaries(disks)

    output = captured.getvalue()
    assert "磁盘:" in output, "模块9未正确输出硬盘摘要标题"
    assert "受保护" in output, "模块9未正确输出受保护状态"
    assert "可选" in output, "模块9未正确输出可选状态"


def test_prompt_disk_selection() -> None:
    disks = apply_disk_protection(SAMPLE_DISK_SUMMARIES, [])
    target_number = 1

    selected = prompt_disk_selection(disks, input_func=lambda prompt: str(target_number))
    assert selected == [target_number], f"模块9选择结果不正确: {selected}"


if __name__ == "__main__":
    test_print_disk_summaries_output()
    test_prompt_disk_selection()
    print("模块9集成测试结果: 通过")
    raise SystemExit(0)
