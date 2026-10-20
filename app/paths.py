import sys
from pathlib import Path


def get_app_dir() -> Path:
    """获取应用根目录，兼容 PyInstaller 打包。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = get_app_dir()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
