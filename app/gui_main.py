"""PyInstaller 的 GUI 入口。

源码下也可以直接运行 ``python app/gui_main.py``：先把项目根目录加入
``sys.path``，再交给 ``app.gui.app``。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.gui.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
