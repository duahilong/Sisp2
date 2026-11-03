"""资源路径解析（源码运行 / PyInstaller 打包）的回归测试。

打包后的 exe 里 ``__file__`` 指向不存在的路径，界面文件和默认配置必须
通过 ``app.resources`` 解析，否则会出现「找不到界面文件」。
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
    assert resources.default_config_path() == REPO_ROOT / "json" / "win11.json"


def _fake_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """造一个“已打包”的目录布局：exe、只读资源目录 _MEIPASS。"""
    exe = tmp_path / "dist" / "Sisp2.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")

    bundle = tmp_path / "bundle"
    ui = bundle / "app" / "gui_web" / "ui"
    ui.mkdir(parents=True)
    (ui / "index.html").write_text("<html></html>", encoding="utf-8")
    cfg = bundle / "json"
    cfg.mkdir(parents=True)
    (cfg / "win11.json").write_text('{"partition_info": {}}', encoding="utf-8")

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
    # exe 旁边还没有配置 → 回退随包模板
    assert resources.default_config_path() == bundle / "json" / "win11.json"


def test_frozen_prefers_config_beside_exe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe, bundle = _fake_frozen(monkeypatch, tmp_path)
    local = exe.parent / "json" / "win11.json"
    local.parent.mkdir(parents=True)
    local.write_text('{"partition_info": {"efi_size_mb": 100}}', encoding="utf-8")
    assert resources.default_config_path() == local


def test_ensure_default_config_copies_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe, bundle = _fake_frozen(monkeypatch, tmp_path)
    target = resources.ensure_default_config()
    assert target == exe.parent / "json" / "win11.json"
    assert target.read_text(encoding="utf-8") == (bundle / "json" / "win11.json").read_text(
        encoding="utf-8"
    )


def test_ensure_default_config_falls_back_when_copy_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """装在只读目录时不能因为复制失败就起不来。"""
    exe, bundle = _fake_frozen(monkeypatch, tmp_path)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("拒绝访问")

    monkeypatch.setattr(resources.shutil, "copyfile", boom)
    assert resources.ensure_default_config() == bundle / "json" / "win11.json"


def test_ensure_default_config_keeps_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe, _ = _fake_frozen(monkeypatch, tmp_path)
    local = exe.parent / "json" / "win11.json"
    local.parent.mkdir(parents=True)
    local.write_text('{"image_info": {"image_path": "D:\\\\a.wim"}}', encoding="utf-8")
    assert resources.ensure_default_config() == local
    assert "a.wim" in local.read_text(encoding="utf-8")
