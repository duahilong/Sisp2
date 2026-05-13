import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.disk_info.service import scan_disk_summaries



def print_disk_summary(disks: list[dict]) -> None:
    print("=" * 80)
    print(f"共检测到 {len(disks)} 块磁盘")
    print("=" * 80)

    for disk in disks:
        drive_letters = "，".join(disk.get("drive_letters") or []) or "无"
        print(
            f"硬盘编号: {disk.get('disk_number')} | "
            f"硬盘型号: {disk.get('model')} | "
            f"硬盘容量: {disk.get('size_display')} | "
            f"分区表格式: {disk.get('partition_style')} | "
            f"连接方式: {disk.get('bus_type')} | "
            f"包含分区的盘符: {drive_letters}"
        )



def main() -> int:
    try:
        disks = scan_disk_summaries()
        print_disk_summary(disks)
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
