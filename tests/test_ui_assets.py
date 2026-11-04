"""前端资源与静态检查。

这些测试不启动浏览器，只保证“页面引用的东西都在、脚本能过语法检查、
设置页覆盖了后端所有设置项、纯计算函数行为正确”。真正的渲染效果靠
``build/grab_window.py`` 截图人工核对。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.modules.settings.service import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "app" / "gui_web" / "ui"
JS = UI / "js"
SETTINGS_JS = JS / "view-settings.js"

_FIELD_RE = re.compile(
    r"\{\s*section:\s*'(\w+)',\s*key:\s*'(\w+)',\s*kind:\s*'([\w-]+)'"
)
_LABEL_RE = re.compile(r"^\s{2}(\w+):\s*'", re.MULTILINE)


def _js_files() -> list[Path]:
    return sorted(JS.glob("*.js"))


def test_ui_files_present() -> None:
    assert (UI / "index.html").is_file()
    assert (UI / "css" / "app.css").is_file()
    names = {path.name for path in _js_files()}
    assert {"main.js", "api.js", "state.js", "shell.js", "dom.js", "derive.js"} <= names
    assert {"view-main.js", "view-install.js", "view-settings.js", "dialogs.js"} <= names


def test_index_is_thin_shell() -> None:
    text = (UI / "index.html").read_text(encoding="utf-8")
    assert "<style" not in text
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text)
    assert '<link rel="stylesheet" href="/static/css/app.css">' in text
    assert '<script type="module" src="/static/js/main.js"></script>' in text
    # 三个视图的容器必须在静态 HTML 里存在，脚本才不会满世界 createElement
    for node_id in ("tree", "overview", "map", "gridBody", "wizard", "settings", "logBody"):
        assert f'id="{node_id}"' in text, node_id


def test_local_imports_resolve() -> None:
    pattern = re.compile(r"""from\s+['"]\./([\w.-]+\.js)['"]""")
    for path in _js_files():
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            assert (JS / name).is_file(), f"{path.name} 引用了不存在的 {name}"


def test_static_references_resolve() -> None:
    text = (UI / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r"""(?:href|src)="(/static/[^"]+)\"""", text)
    assert refs
    for ref in refs:
        assert (UI / ref[len("/static/"):]).is_file(), ref


def test_css_keeps_hidden_attribute_working() -> None:
    """``.fatal{display:flex}`` 这类声明会盖掉 UA 的 ``[hidden]{display:none}``。

    曾经因此让一块空的 #fatal 遮罩盖住整个界面：DOM 齐全、截图纯背景色。
    """
    css = (UI / "css" / "app.css").read_text(encoding="utf-8")
    assert "[hidden]" in css
    assert "display:none!important" in css.replace(" ", "")


def test_css_is_plain_css() -> None:
    css = (UI / "css" / "app.css").read_text(encoding="utf-8")
    assert css.count("{") == css.count("}"), "app.css 花括号不平衡"
    assert ":root" in css
    # CSS 不支持 // 注释；原型里的注释已改成 /* */
    assert not re.search(r"^\s*//", css, re.MULTILINE)
    for name in re.findall(r"url\(([^)]+)\)", css):
        cleaned = name.strip().strip("'\"")
        if cleaned.startswith("data:"):
            continue
        assert not cleaned.startswith("/"), f"app.css 不该引用绝对路径资源: {cleaned}"


def test_no_legacy_fallback() -> None:
    for path in [*list(_js_files()), UI / "index.html"]:
        text = path.read_text(encoding="utf-8")
        assert "legacy" not in text.lower(), path.name


def test_settings_page_covers_backend_keys() -> None:
    text = SETTINGS_JS.read_text(encoding="utf-8")
    keys = {key for _, key, _ in _FIELD_RE.findall(text)}
    missing = set(DEFAULTS) - keys
    extra = keys - set(DEFAULTS)
    assert not missing, f"设置页缺少后端设置项: {sorted(missing)}"
    assert not extra, f"设置页有后端不认识的设置项: {sorted(extra)}"
    # 每个键都要有中文标签，否则界面上会露出英文键名
    labels = set(_LABEL_RE.findall(text.split("const LABELS")[1].split("};")[0]))
    assert set(DEFAULTS) - labels == set(), f"缺少标签: {sorted(set(DEFAULTS) - labels)}"


def test_every_field_has_section_and_kind() -> None:
    text = SETTINGS_JS.read_text(encoding="utf-8")
    rows = _FIELD_RE.findall(text)
    assert len(rows) == len(DEFAULTS)
    known = {"bool", "enum", "int", "auto-int", "path", "text", "list"}
    for section, key, kind in rows:
        assert section in {"general", "install", "external", "system_files", "safety", "logging"}, key
        assert kind in known, f"{key} 的控件类型 {kind} 不认识"


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_partition_defaults_reserve_space_for_efi() -> None:
    """默认数据分区大小要给 EFI 和对齐留余量，否则一进第 4 步就被后端判容量不足。"""
    derive = (JS / "derive.js").read_text(encoding="utf-8")
    state = (JS / "state.js").read_text(encoding="utf-8")
    install = (JS / "view-install.js").read_text(encoding="utf-8")
    assert "export const RESERVE_GB" in derive
    assert "efiMb = 100" in derive
    assert "RESERVE_GB" in state
    assert "RESERVE_GB" in install


def test_install_wizard_can_add_extra_partition() -> None:
    """第 2 步的 ➕ / ✕、E 行、data_partitions 载荷都不能被删掉。

    真的点按钮由 ``tools/ui_smoke.mjs`` 负责（它在浏览器里加一次 E、删一次、
    再加回来，并要求第 4 步出现“额外数据分区”）；这里只防静态回退。
    """
    install = (JS / "view-install.js").read_text(encoding="utf-8")
    assert "data-add" in install
    assert "data-remove" in install
    assert "额外数据分区" in install
    assert 'data-fs="extra"' in install
    assert "partition_plan" in install

    state_js = (JS / "state.js").read_text(encoding="utf-8")
    assert "data_partitions" in state_js
    assert "addExtraPartition" in state_js
    assert "removeExtraPartition" in state_js

    css = (UI / "css" / "app.css").read_text(encoding="utf-8")
    assert ".addpart" in css
    assert ".szrow.extra" in css


def test_extra_partition_limit_matches_backend() -> None:
    """前端最多 N 个额外分区必须等于后端布局允许的个数，否则会被后端打回。"""
    from app.application.partitions import MAX_DATA_PARTITIONS

    derive = (JS / "derive.js").read_text(encoding="utf-8")
    match = re.search(r"export const MAX_EXTRAS = (\d+);", derive)
    assert match, "derive.js 里找不到 MAX_EXTRAS"
    assert int(match.group(1)) == MAX_DATA_PARTITIONS - 1


def test_frontend_static_check() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "ui_check.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
