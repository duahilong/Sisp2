import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.disk_info.service import scan_disks



def print_raw_disks(disks: list[dict]) -> None:
    print("=" * 80)
    print(f"scan_disks() 原始返回，共 {len(disks)} 块磁盘")
    print("=" * 80)
    print(json.dumps(disks, ensure_ascii=False, indent=2))



def main() -> int:
    try:
        disks = scan_disks()
        print_raw_disks(disks)
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
