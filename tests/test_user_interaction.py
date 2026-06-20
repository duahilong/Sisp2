import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.modules.user_interaction.service as user_interaction_service
from app.modules.user_interaction.service import apply_disk_protection, build_disk_summary_text, get_display_width, parse_selected_disk_numbers, prompt_disk_selection, run_user_interaction

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
    assert_contains(output, "状态")
    assert_contains(output, "Samsung SSD 980 PRO 1TB")
    assert_contains(output, "931.51 GB")
    assert_contains(output, "NVMe")
    assert_contains(output, "SATA")
    assert_contains(output, "C，D")
    assert_contains(output, "WDC WD10EZEX")
    assert_contains(output, "MBR")
    assert_contains(output, "可选")

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

    selected_all_selectable = parse_selected_disk_numbers("a", available_numbers, [2, 3])
    if selected_all_selectable != [2, 3]:
        raise AssertionError(f"输入 'a' 时应只选择可选硬盘: {selected_all_selectable}")



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



def test_apply_disk_protection() -> None:
    disks = apply_disk_protection(
        [
            {"disk_number": 0, "model": "System Disk", "is_system": True},
            {"disk_number": 1, "model": "Target Disk"},
            {"disk_number": 2, "model": "Excluded Disk"},
            {"disk_number": 3, "model": "Offline Disk", "is_offline": True},
            {"disk_number": 4, "model": "Read Only Disk", "is_read_only": True},
        ],
        ["Excluded Disk"],
    )

    if disks[0].get("is_selectable"):
        raise AssertionError("系统盘不应可选")
    if not disks[1].get("is_selectable"):
        raise AssertionError("普通目标盘应可选")
    if disks[2].get("is_selectable"):
        raise AssertionError("配置排除盘不应可选")
    if disks[3].get("is_selectable"):
        raise AssertionError("离线盘不应可选")
    if disks[4].get("is_selectable"):
        raise AssertionError("只读盘不应可选")

    if "系统盘" not in disks[0].get("protection_reasons", []):
        raise AssertionError("系统盘保护原因不正确")
    if "配置排除盘" not in disks[2].get("protection_reasons", []):
        raise AssertionError("配置排除盘保护原因不正确")



def test_build_disk_summary_text_with_protected_disk() -> None:
    disks = apply_disk_protection(
        [
            {"disk_number": 0, "model": "System Disk", "is_system": True, "drive_letters": ["C"]},
            {"disk_number": 1, "model": "Target Disk", "drive_letters": []},
        ],
        [],
    )
    output = build_disk_summary_text(disks)
    assert_contains(output, "受保护：系统盘")
    assert_contains(output, "可选")



def test_prompt_disk_selection_rejects_protected_disk() -> None:
    disks = apply_disk_protection(
        [
            {"disk_number": 0, "model": "System Disk", "is_system": True},
            {"disk_number": 1, "model": "Target Disk"},
        ],
        [],
    )

    try:
        prompt_disk_selection(disks, input_func=lambda prompt: "0")
    except ValueError as exc:
        if "硬盘编号 0 不可选择" not in str(exc):
            raise AssertionError(f"错误信息不正确: {exc}") from exc
    else:
        raise AssertionError("选择受保护硬盘时应失败")

    selected_all = prompt_disk_selection(disks, input_func=lambda prompt: "a")
    if selected_all != [1]:
        raise AssertionError(f"全选时应只选择可选硬盘: {selected_all}")



def test_run_user_interaction_applies_excluded_disk_names() -> None:
    original_scan_disk_summaries = user_interaction_service.scan_disk_summaries
    user_interaction_service.scan_disk_summaries = lambda: [
        {"disk_number": 0, "model": "Excluded Disk", "drive_letters": ["C"]},
        {"disk_number": 1, "model": "Target Disk", "drive_letters": []},
    ]

    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            selected = run_user_interaction(
                input_func=lambda prompt: "a",
                excluded_disk_names=["Excluded Disk"],
            )
    finally:
        user_interaction_service.scan_disk_summaries = original_scan_disk_summaries

    if selected != [1]:
        raise AssertionError(f"run_user_interaction 应只返回可选硬盘: {selected}")

    output = captured.getvalue()
    assert_contains(output, "受保护：配置排除盘")
    assert_contains(output, "可选")



def main() -> int:
    try:
        captured = io.StringIO()
        with redirect_stdout(captured):
            test_build_disk_summary_text()
        test_get_display_width()
        test_parse_selected_disk_numbers()
        test_parse_selected_disk_numbers_invalid()
        test_prompt_disk_selection()
        test_apply_disk_protection()
        test_build_disk_summary_text_with_protected_disk()
        test_prompt_disk_selection_rejects_protected_disk()
        test_run_user_interaction_applies_excluded_disk_names()
        print("模块9测试结果: 通过")
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
