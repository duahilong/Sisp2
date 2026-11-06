"""资源路径解析（源码运行 / PyInstaller 打包）的回归测试。

打包后的 exe 里 ``__file__`` 指向不存在的路径，界面文件必须通过
``app.resources`` 解析，否则会出现「找不到界面文件」。

（这里曾经还测 ``default_config_path()`` / ``ensure_default_config()``。
业务 JSON 配置文件那一层移除后，这两个函数连同 ``json/`` 目录一起删掉了，
对应的用例也随之删除——现在没有“随包模板要复制到可写目录”这件事。）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app import resources

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_source_run_uses_repo_root() -> None:
    assert resources.is_frozen() is False
    assert resources.app_root() == REPO_ROOT
    assert resources.bundle_root() == REPO_ROOT
    assert resources.ui_path() == REPO_ROOT / "app" / "gui_web" / "ui" / "index.html"
    assert resources.ui_path().is_file()


def test_no_config_file_helpers_left() -> None:
    """业务配置那一层的入口必须彻底消失，避免有人又接回去。"""
    for name in (
        "default_config_path",
        "ensure_default_config",
        "DEFAULT_CONFIG_RELATIVE",
        "TEMPLATE_CONFIG_RELATIVE",
    ):
        assert not hasattr(resources, name), f"app.resources 里不该再有 {name}"


def _fake_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """造一个“已打包”的目录布局：exe、只读资源目录 _MEIPASS。"""
    exe = tmp_path / "dist" / "Sisp2.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")

    bundle = tmp_path / "bundle"
    ui = bundle / "app" / "gui_web" / "ui"
    ui.mkdir(parents=True)
    (ui / "index.html").write_text("<html></html>", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    return exe, bundle


def test_frozen_reads_assets_from_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe, bundle = _fake_frozen(monkeypatch, tmp_path)

    assert resources.is_frozen() is True
    assert resources.app_root() == exe.parent
    assert resources.bundle_root() == bundle
    assert resources.ui_path() == bundle / "app" / "gui_web" / "ui" / "index.html"
    assert resources.ui_path().is_file()


def test_frozen_without_meipass_falls_back_to_exe_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """onedir/开发态没有 ``_MEIPASS`` 时，只读资源根退回 exe 目录。"""
    exe, _bundle = _fake_frozen(monkeypatch, tmp_path)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert resources.bundle_root() == exe.parent
