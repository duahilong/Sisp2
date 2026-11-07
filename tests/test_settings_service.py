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
    assert store.probe("extGhost").state == "na"
    # 系统自带 → builtin
    assert store.probe("extDiskpart").state == "builtin"

    store.update({"extGhost": str(exe), "logDir": str(folder)})
    assert store.probe("extGhost").state == "ok"
    assert store.probe("logDir").state == "ok"

    store.update({"extGhost": str(tmp_path / "missing.exe")})
    result = store.probe("extGhost")
    assert result.state == "miss"
    assert "不存在" in result.message

    # 非路径键
    assert store.probe("theme").state == "na"
    assert json_path.exists()


def test_probe_can_check_a_draft_value(tmp_path: Path) -> None:
    """设置页只回填草稿后，探测必须能跟着草稿走。"""
    saved = tmp_path / "saved.exe"
    saved.write_text("x", encoding="utf-8")
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()

    store = SettingsStore(
        tmp_path / "settings.json",
        which=lambda name: "C:/Windows/system32/diskpart.exe" if name == "diskpart" else None,
    )
    store.update({"extGhost": str(saved)})

    # 不传 path → 看已保存的值
    assert store.probe("extGhost").state == "ok"
    # 传 path → 看草稿（类型也要匹配：extGhost 是文件）
    assert store.probe("extGhost", str(saved)).state == "ok"
    assert store.probe("extGhost", str(draft_dir)).state == "miss"
    assert store.probe("extGhost", str(tmp_path / "nope.exe")).state == "miss"
    # 草稿清空 → 回到未设置/系统自带那一套判断
    store.update({"extGhost": ""})
    assert store.probe("extGhost", "").state == "na"
    # 草稿是空值时依然走内置命令回退
    assert store.probe("extDiskpart", "").state == "builtin"
    # 非路径键传什么都不影响
    assert store.probe("theme", str(saved)).state == "na"


def test_probe_all_covers_every_path_key(store: SettingsStore) -> None:
    probed = store.probe_all()
    assert "extGhost" in probed
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
    assert next(section["key"] for section in payload["sections"]) == "general"
    assert "settings" in payload and "pathKeys" in payload


def test_load_tolerates_utf8_bom(tmp_path: Path) -> None:
    """记事本 / Windows PowerShell 5.1 保存的 UTF-8 BOM 不应让设置退化成默认值。"""
    path = tmp_path / "settings.json"
    payload = {
        "version": 1,
        "settings": {"sysDefaultGb": 300, "logDir": r"D:\logs", "logLevel": "debug"},
    }
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    loaded = SettingsStore(path, which=lambda _name: None)

    assert loaded.get_warnings() == []
    assert loaded.get()["sysDefaultGb"] == 300
    assert loaded.get()["logDir"] == r"D:\logs"
    assert loaded.get()["logLevel"] == "debug"


# ------------------------------------------------------------------ EFI / 镜像版本
def test_efi_default_range(store: SettingsStore) -> None:
    for bad in (63, 1025, "abc"):
        with pytest.raises(SettingsError):
            coerce_value("efiDefaultMb", bad)
    assert coerce_value("efiDefaultMb", 64) == 64
    assert coerce_value("efiDefaultMb", "512") == 512


def test_partition_info_uses_efi_default(store: SettingsStore) -> None:
    assert store.partition_info()["efi_size_mb"] == 100
    store.update({"efiDefaultMb": 300})
    assert store.partition_info()["efi_size_mb"] == 300


def test_image_default_index_range(store: SettingsStore) -> None:
    assert store.get()["imageDefaultIndex"] == 0
    for bad in (-1, 100):
        with pytest.raises(SettingsError):
            coerce_value("imageDefaultIndex", bad)
    assert coerce_value("imageDefaultIndex", 3) == 3


# ------------------------------------------------- 两个系统槽位（A / B）
def test_system_slot_labels_are_trimmed_text(store: SettingsStore) -> None:
    assert store.get()["sysImageALabel"] == "Windows 10"
    assert store.get()["sysImageBLabel"] == "Windows 11"
    store.update({"sysImageALabel": "  Windows   10  22H2 "})
    assert store.get()["sysImageALabel"] == "Windows 10 22H2"


def test_system_slot_label_length_limit(store: SettingsStore) -> None:
    assert coerce_value("sysImageBLabel", "x" * 32) == "x" * 32
    with pytest.raises(SettingsError):
        coerce_value("sysImageBLabel", "x" * 33)


def test_two_system_slots_cannot_point_at_the_same_file(store: SettingsStore) -> None:
    with pytest.raises(SettingsError) as excinfo:
        store.update({"sysImageA": "D:/images/win10.GHO", "sysImageB": r"d:\IMAGES\WIN10.gho"})
    assert excinfo.value.field == "sysImageB"
    assert "同一个镜像文件" in excinfo.value.message
    # 只有一边填了不算冲突
    store.update({"sysImageB": ""})
    store.update({"sysImageA": "D:/images/win10.GHO"})
    assert store.get()["sysImageA"].endswith("win10.GHO")


def test_two_system_slots_need_distinct_labels(store: SettingsStore) -> None:
    with pytest.raises(SettingsError) as excinfo:
        store.update(
            {
                "sysImageA": "D:/images/win10.GHO",
                "sysImageB": "D:/images/win11.GHO",
                "sysImageALabel": "Windows",
                "sysImageBLabel": "windows",
            }
        )
    assert excinfo.value.field == "sysImageBLabel"
