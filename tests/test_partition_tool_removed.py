"""回归哨兵：独立的单盘分区工具（B 套）已经移除，不能被悄悄拼回来。

历史上有两套分区实现：

- **A. 装机向导**（``app/application/partitions.py`` + ``real_runner.py``）：
  整盘 `clean` + `convert gpt`，重建 EFI + C + D(+E)，是唯一的装机路径；
- **B. 单盘分区工具**（``app/modules/partition/`` + ``/api/partition/*``）：
  在保留现有系统的盘上 create/format/delete/assign。

B 已删除——它和 A 各有一份 diskpart 拼装、容量口径和护栏，语义还相反
（A 主动删 MSR，B 禁止碰 MSR）。这里的断言钉住"删干净了"：

1. 后端没有那个包、没有旧接口方法；
2. 前端没有菜单项、没有弹窗、没有旧 API 客户端；
3. 只服务于 B 的 `suggestLetter` / `usedLetters` 也不再存在；
4. `confirmDelete` 设置项（B 是唯一消费者）已移除。

要重新引入这类能力时，请先在 A 的模型上扩展，而不是再长出一套。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "app" / "gui_web" / "api.py"
SERVER = ROOT / "app" / "gui_web" / "server.py"
UI = ROOT / "app" / "gui_web" / "ui"


def test_partition_module_is_gone() -> None:
    """只断言没有源码：删除后老机器上可能还留着 ``__pycache__``（git 不管它）。"""
    module_dir = ROOT / "app" / "modules" / "partition"
    sources = sorted(path.name for path in module_dir.glob("*.py")) if module_dir.exists() else []
    assert sources == []


def test_backend_has_no_partition_tool_endpoints() -> None:
    server = SERVER.read_text(encoding="utf-8")
    api = API.read_text(encoding="utf-8")
    assert "/api/partition" not in server
    assert "partition_plan" not in api
    assert "partition_execute" not in api
    assert "PartitionRunner" not in api


def test_frontend_has_no_partition_tool() -> None:
    index = (UI / "index.html").read_text(encoding="utf-8")
    dialogs = (UI / "js" / "dialogs.js").read_text(encoding="utf-8")
    api_js = (UI / "js" / "api.js").read_text(encoding="utf-8")
    assert 'data-act="partition"' not in index
    assert "openPartitionDialog" not in dialogs
    assert "/api/partition" not in api_js


def test_letter_suggestion_helpers_are_gone() -> None:
    """``suggestLetter`` / ``usedLetters`` 只服务于 B 的"分配盘符"输入框。"""
    derive = (UI / "js" / "derive.js").read_text(encoding="utf-8")
    assert "suggestLetter" not in derive
    assert "usedLetters" not in derive


def test_confirm_delete_setting_is_gone() -> None:
    """B 是 ``confirmDelete`` 的唯一消费者，设项随它一起删掉。"""
    settings = (ROOT / "app" / "modules" / "settings" / "service.py").read_text(encoding="utf-8")
    settings_js = (UI / "js" / "view-settings.js").read_text(encoding="utf-8")
    assert "confirmDelete" not in settings
    assert "confirmDelete" not in settings_js
