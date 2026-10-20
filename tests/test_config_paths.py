import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.config_loader.service import get_default_config_path, load_config


def test_load_config_resolves_relative_paths_against_config_file(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs"
    (config_dir / "images").mkdir(parents=True)
    (config_dir / "sw").mkdir()
    source_dir = config_dir / "source"
    source_dir.mkdir()

    image = config_dir / "images" / "win.gho"
    ghost = config_dir / "sw" / "ghost64.exe"
    bcd = config_dir / "sw" / "bcdboot.exe"
    for path in (image, ghost, bcd):
        path.write_bytes(b"x")

    config = {
        "description": "relative paths",
        "win_gho": "images/win.gho",
        "efi_size": 100,
        "c_size": 100,
        "software_file": "source",
        "gho_exe": "sw/ghost64.exe",
        "bcd_exe": "sw/bcdboot.exe",
        "excluded_disk_names": [],
    }
    config_path = config_dir / "win11.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    payload = load_config(config_path)
    assert payload["image_info"]["image_path"] == str(image.resolve())
    assert payload["software_paths"]["ghost64_path"] == str(ghost.resolve())
    assert payload["software_paths"]["bcdboot_path"] == str(bcd.resolve())
    assert payload["copy_info"]["source_dir"] == str(source_dir.resolve())


def test_frozen_default_config_falls_back_to_internal(tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "app"
    internal_json = base / "_internal" / "json"
    internal_json.mkdir(parents=True)
    config_path = internal_json / "win11.json"
    config_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(base / "Sisp.exe"))
    assert get_default_config_path() == config_path
