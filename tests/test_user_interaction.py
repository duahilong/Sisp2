import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.user_interaction.service import build_disk_summary_text, get_display_width, parse_selected_disk_numbers, prompt_disk_selection

SAMPLE_DISKS = [
    {
        "disk_number": 1,
        "model": "Samsung SSD 980 PRO 1TB",
        "size_display": "931.51 GB",
        "partition_style": "GPT",
        "bus_type": "NVMe",
        "drive_letters": ["C", "D"],
    },
    {
        "disk_number": 2,
        "model": "WDC WD10EZEX",
        "size_display": "931.51 GB",
        "partition_style": "MBR",
        "bus_type": "SATA",
        "drive_letters": ["E"],
    },
    {
        "disk_number": 3,
        "model": "Kingston NV2 500GB",
        "size_display": "465.76 GB",
        "partition_style": "GPT",
        "bus_type": "NVMe",
        "drive_letters": ["F"],
    },
]



def assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"期望输出包含: {expected}\n实际输出:\n{text}")



def test_build_disk_summary_text() -> None:
    output = build_disk_summary_text(SAMPLE_DISKS)
    assert_contains(output, "共检测到 3 块磁盘")
    assert_contains(output, "编号")
    assert_contains(output, "硬盘型号")
    assert_contains(output, "容量")
    assert_contains(output, "连接方式")
    assert_contains(output, "盘符")
    assert_contains(output, "分区表格式")
    assert_contains(output, "Samsung SSD 980 PRO 1TB")
    assert_contains(output, "931.51 GB")
    assert_contains(output, "NVMe")
    assert_contains(output, "SATA")
    assert_contains(output, "C，D")
    assert_contains(output, "WDC WD10EZEX")
    assert_contains(output, "MBR")

    forbidden_chars = ["┌", "┬", "┐", "├", "┼", "┤", "└", "┴", "┘", "│"]
    for char in forbidden_chars:
        if char in output:
            raise AssertionError(f"当前输出不应包含外框字符: {char}\n实际输出:\n{output}")



def test_get_display_width() -> None:
    if get_display_width("编号") <= len("编号"):
        raise AssertionError("中文表头显示宽度计算不正确")

    if get_display_width("ABC123") != len("ABC123"):
        raise AssertionError("英文字符显示宽度计算不正确")



def test_parse_selected_disk_numbers() -> None:
    available_numbers = [1, 2, 3]

    test_cases = [
        ("1,2", [1, 2]),
        ("1，2，2", [1, 2]),
        ("1 2 3", [1, 2, 3]),
        ("2-3", [2, 3]),
        ("1,2-3", [1, 2, 3]),
        ("1 2-3", [1, 2, 3]),
        ("a", [1, 2, 3]),
        ("A", [1, 2, 3]),
        ("q", []),
        ("Q", []),
    ]

    for selection_text, expected in test_cases:
        selected = parse_selected_disk_numbers(selection_text, available_numbers)
        if selected != expected:
            raise AssertionError(f"输入 {selection_text!r} 的选择结果不正确: {selected}，期望: {expected}")



def test_parse_selected_disk_numbers_invalid() -> None:
    invalid_cases = [
        ("", "输入不能为空"),
        ("abc", "存在无效的硬盘编号输入"),
        ("9", "硬盘编号不存在"),
        ("3-2", "范围输入无效"),
        ("q,2", "q 只能单独输入"),
        ("a,2", "a 只能单独输入"),
        ("1-b", "范围输入无效"),
    ]

    for selection_text, expected_message in invalid_cases:
        try:
            parse_selected_disk_numbers(selection_text, [1, 2, 3])
        except ValueError as exc:
            if expected_message not in str(exc):
                raise AssertionError(f"期望错误信息包含: {expected_message}，实际为: {exc}") from exc
        else:
            raise AssertionError(f"期望输入 {selection_text!r} 失败，但实际成功")



def test_prompt_disk_selection() -> None:
    selected = prompt_disk_selection(SAMPLE_DISKS, input_func=lambda prompt: "1,2-3")
    if selected != [1, 2, 3]:
        raise AssertionError(f"prompt_disk_selection 返回结果不正确: {selected}")

    exited = prompt_disk_selection(SAMPLE_DISKS, input_func=lambda prompt: "q")
    if exited != []:
        raise AssertionError(f"prompt_disk_selection 退出结果不正确: {exited}")



def main() -> int:
    try:
        captured = io.StringIO()
        with redirect_stdout(captured):
            test_build_disk_summary_text()
        test_get_display_width()
        test_parse_selected_disk_numbers()
        test_parse_selected_disk_numbers_invalid()
        test_prompt_disk_selection()
        print("模块9测试结果: 通过")
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
