import re
from typing import Callable

from wcwidth import wcswidth

from app.modules.disk_info.service import scan_disk_summaries


InputFunc = Callable[[str], str]
TABLE_HEADERS = ["编号", "硬盘型号", "容量", "连接方式", "盘符", "分区表格式"]
COLUMN_SEPARATOR = "  "
EXIT_SELECTION = "q"
SELECT_ALL_SELECTION = "a"



def build_table_rows(disks: list[dict]) -> list[list[str]]:
    rows: list[list[str]] = []
    for disk in disks:
        drive_letters = "，".join(disk.get("drive_letters") or []) or "无"
        rows.append(
            [
                str(disk.get("disk_number")),
                str(disk.get("model") or "未知"),
                str(disk.get("size_display") or "未知"),
                str(disk.get("bus_type") or "未知"),
                drive_letters,
                str(disk.get("partition_style") or "未知"),
            ]
        )
    return rows



def get_display_width(text: str) -> int:
    width = wcswidth(text)
    if width < 0:
        raise ValueError(f"无法计算字符串显示宽度: {text!r}")
    return width



def pad_display_text(text: str, target_width: int) -> str:
    padding = max(target_width - get_display_width(text), 0)
    return text + (" " * padding)



def calculate_column_widths(headers: list[str], rows: list[list[str]]) -> list[int]:
    widths = [get_display_width(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], get_display_width(cell))
    return widths



def build_aligned_line(values: list[str], widths: list[int]) -> str:
    cells = [pad_display_text(value, widths[index]) for index, value in enumerate(values)]
    return COLUMN_SEPARATOR.join(cells).rstrip()



def build_disk_summary_text(disks: list[dict]) -> str:
    rows = build_table_rows(disks)
    widths = calculate_column_widths(TABLE_HEADERS, rows)

    lines = [f"共检测到 {len(disks)} 块磁盘"]
    lines.append(build_aligned_line(TABLE_HEADERS, widths))
    lines.append(build_aligned_line(["-" * width for width in widths], widths))

    for row in rows:
        lines.append(build_aligned_line(row, widths))

    return "\n".join(lines)



def print_disk_summaries(disks: list[dict]) -> None:
    print(build_disk_summary_text(disks))



def append_disk_number(selected_numbers: list[int], disk_number: int, available_disk_numbers: list[int]) -> None:
    if disk_number not in available_disk_numbers:
        raise ValueError(f"硬盘编号不存在: {disk_number}")

    if disk_number not in selected_numbers:
        selected_numbers.append(disk_number)



def parse_range_token(token: str, available_disk_numbers: list[int], selected_numbers: list[int]) -> None:
    start_text, end_text = token.split("-", 1)
    if not start_text.isdigit() or not end_text.isdigit():
        raise ValueError(f"范围输入无效: {token}")

    start_number = int(start_text)
    end_number = int(end_text)
    if start_number > end_number:
        raise ValueError(f"范围输入无效: {token}")

    for disk_number in range(start_number, end_number + 1):
        append_disk_number(selected_numbers, disk_number, available_disk_numbers)



def parse_selected_disk_numbers(selection_text: str, available_disk_numbers: list[int]) -> list[int]:
    normalized = selection_text.replace("，", ",").strip()
    if not normalized:
        raise ValueError("输入不能为空，请输入硬盘编号")

    lowered = normalized.lower()
    if lowered == EXIT_SELECTION:
        return []

    if lowered == SELECT_ALL_SELECTION:
        return sorted(dict.fromkeys(available_disk_numbers))

    special_tokens = re.split(r"[\s,]+", lowered)
    if EXIT_SELECTION in special_tokens:
        raise ValueError(f"{EXIT_SELECTION} 只能单独输入，用于退出")

    if SELECT_ALL_SELECTION in special_tokens:
        raise ValueError(f"{SELECT_ALL_SELECTION} 只能单独输入，用于选择全部磁盘")

    tokens = [token for token in re.split(r"[\s,]+", normalized) if token]
    if not tokens:
        raise ValueError("未检测到有效的硬盘编号")

    selected_numbers: list[int] = []
    for token in tokens:
        if token.count("-") == 1:
            parse_range_token(token, available_disk_numbers, selected_numbers)
            continue

        if not token.isdigit():
            raise ValueError(f"存在无效的硬盘编号输入: {token}")

        append_disk_number(selected_numbers, int(token), available_disk_numbers)

    return selected_numbers



def prompt_disk_selection(disks: list[dict], input_func: InputFunc = input) -> list[int]:
    available_disk_numbers = [disk.get("disk_number") for disk in disks if isinstance(disk.get("disk_number"), int)]
    if not available_disk_numbers:
        raise ValueError("没有可供选择的硬盘编号")

    prompt_text = "请输入磁盘编号（单个数字3、范围1-3、多个数字1,3,5或1 3 5、字母a表示全部磁盘，q退出）："
    selection_text = input_func(prompt_text)
    return parse_selected_disk_numbers(selection_text, available_disk_numbers)



def run_user_interaction(input_func: InputFunc = input) -> list[int]:
    disks = scan_disk_summaries()
    print_disk_summaries(disks)
    return prompt_disk_selection(disks, input_func)
