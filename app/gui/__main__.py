"""支持 ``python -m app.gui``。"""

from app.gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
