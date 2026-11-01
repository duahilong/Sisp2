"""设置模块单元测试。

跑法：
    python -m pytest tests/test_settings_service.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.modules.settings.service import (
    DEFAULTS,
    SettingsError,
    SettingsStore,
    coerce_value,
    default_settings_path,
)


@pytest.fixture()
def store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "settings.json", which=lambda _name: None)


def test_loads_defaults_when_file_missing(store: SettingsStore) -> None:
    settings = store.get()
    assert settings["language"] == "zh-CN"
    assert settings["sysDefaultGb"] == 120
    assert settings["dataDefaultGb"] == "auto"
    assert store.get_warnings() == []


def test_update_persists_and_reloads(store: SettingsStore) -> None:
    store.update({"sysDefaultGb": 300, "theme": "dark", "logLevel": "debug"})
    assert store.path.exists()

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["settings"]["sysDefaultGb"] == 300

    reloaded = SettingsStore(store.path, which=lambda _name: None)
    assert reloaded.get()["theme"] == "dark"
    assert reloaded.get()["logLevel"] == "debug"


def test_invalid_enum_is_rejected_and_not_written(store: SettingsStore) -> None:
    with pytest.raises(SettingsError) as excinfo:
        store.update({"theme": "neon"})
    assert excinfo.value.field == "theme"
    assert store.get()["theme"] == "light"
    assert not store.path.exists()


def test_unknown_key_is_rejected(store: SettingsStore) -> None:
    with pytest.raises(SettingsError) as excinfo:
        store.update({"nope": 1})
    assert excinfo.value.field == "nope"


def test_int_range_validation(store: SettingsStore) -> None:
    with pytest.raises(SettingsError):
        store.update({"sysDefaultGb": 10})
    with pytest.raises(SettingsError):
        store.update({"sysDefaultGb": 99999})
    assert store.update({"sysDefaultGb": 200})["sysDefaultGb"] == 200


def test_bool_coercion() -> None:
    assert coerce_value("typeToConfirm", "false") is False
    assert coerce_value("typeToConfirm", "ON") is True
    assert coerce_value("typeToConfirm", 0) is False
    with pytest.raises(SettingsError):
        coerce_value("typeToConfirm", "maybe")


def test_data_default_accepts_auto_and_number() -> None:
    assert coerce_value("dataDefaultGb", "auto") == "auto"
    assert coerce_value("dataDefaultGb", "") == "auto"
    assert coerce_value("dataDefaultGb", "800") == 800


def test_corrupt_file_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{ not json", encoding="utf-8")
    store = SettingsStore(path, which=lambda _name: None)
    assert store.get()["language"] == "zh-CN"
    assert any("无法解析" in warning for warning in store.get_warnings())


def test_invalid_stored_field_is_dropped(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"version": 1, "settings": {"theme": "neon", "sysDefaultGb": 300}}),
        encoding="utf-8",
    )
    store = SettingsStore(path, which=lambda _name: None)
    assert store.get()["theme"] == "light"  # 回到默认
    assert store.get()["sysDefaultGb"] == 300  # 合法项保留
    assert store.get_warnings()


def test_reset_restores_defaults(store: SettingsStore) -> None:
    store.update({"sysDefaultGb": 500})
    store.reset()
    assert store.get() == DEFAULTS


def test_probe_state_machine(tmp_path: Path) -> None:
    exe = tmp_path / "tool.exe"
    exe.write_text("x", encoding="utf-8")
    folder = tmp_path / "logs"
    folder.mkdir()

    store = SettingsStore(
        tmp_path / "settings.json",
        which=lambda name: "C:/Windows/system32/diskpart.exe" if name == "diskpart" else None,
    )
    json_path = store.path

    # 未设置 → na
    assert store.probe("extDism").state == "na"
    # 系统自带 → builtin
    assert store.probe("extDiskpart").state == "builtin"

    store.update({"extDism": str(exe), "logDir": str(folder)})
    assert store.probe("extDism").state == "ok"
    assert store.probe("logDir").state == "ok"

    store.update({"extDism": str(tmp_path / "missing.exe")})
    result = store.probe("extDism")
    assert result.state == "miss"
    assert "不存在" in result.message

    # 非路径键
    assert store.probe("theme").state == "na"
    assert json_path.exists()


def test_probe_all_covers_every_path_key(store: SettingsStore) -> None:
    probed = store.probe_all()
    assert "extDism" in probed
    assert "sysTemp" in probed
    assert probed["sysTemp"]["state"] == "na"


def test_partition_info_bridges_defaults(store: SettingsStore) -> None:
    info = store.partition_info()
    assert info["efi_size_mb"] == 100
    assert info["c_size_gb"] == 120
    assert info["data_size_gb"] is None

    store.update({"sysDefaultGb": 260, "dataDefaultGb": 900})
    info = store.partition_info()
    assert info["c_size_gb"] == 260
    assert info["data_size_gb"] == 900


def test_relation_rule_temp_equals_log_rejected(store: SettingsStore) -> None:
    with pytest.raises(SettingsError) as excinfo:
        store.update({"sysTemp": "C:/tmp/sisp", "logDir": "C:/tmp/sisp"})
    assert excinfo.value.field == "sysTemp"


def test_default_settings_path_uses_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = default_settings_path()
    assert path.parent.name == "Sisp"
    assert path.name == "settings.json"


def test_to_dict_reports_sections(store: SettingsStore) -> None:
    payload = store.to_dict()
    assert [section["key"] for section in payload["sections"]][0] == "general"
    assert "settings" in payload and "pathKeys" in payload
