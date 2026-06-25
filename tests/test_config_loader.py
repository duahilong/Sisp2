import json
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.config_loader.service import DEFAULT_CONFIG_PATH, load_config

REQUIRED_TOP_LEVEL_KEYS = [
    "config_path",
    "config",
    "partition_info",
    "feature_flags",
    "image_info",
    "software_paths",
    "copy_info",
    "excluded_disk_names",
]



def print_config_payload(payload: dict) -> None:
    print("=" * 80)
    print("模块4：Json配置文件读取")
    print("=" * 80)
    print(json.dumps(payload, ensure_ascii=False, indent=2))



def validate_payload(payload: dict) -> None:
    missing_keys = [key for key in REQUIRED_TOP_LEVEL_KEYS if key not in payload]
    if missing_keys:
        missing_text = "、".join(missing_keys)
        raise AssertionError(f"缺少顶层字段: {missing_text}")

    if payload.get("config_path") != str(DEFAULT_CONFIG_PATH):
        raise AssertionError("config_path 与默认配置文件路径不一致")

    config = payload.get("config") or {}
    partition_info = payload.get("partition_info") or {}
    image_info = payload.get("image_info") or {}
    software_paths = payload.get("software_paths") or {}
    copy_info = payload.get("copy_info") or {}
    excluded_disk_names = payload.get("excluded_disk_names")

    if not isinstance(config.get("description"), str) or not config.get("description", "").strip():
        raise AssertionError("config.description 必须为非空字符串")

    for key in ["win_gho", "software_file", "gho_exe", "bcd_exe"]:
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise AssertionError(f"config.{key} 必须为非空字符串")

    for key in ["efi_size", "c_size"]:
        value = config.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise AssertionError(f"config.{key} 必须为大于 0 的数字")

    if partition_info.get("efi_size_mb") != config.get("efi_size"):
        raise AssertionError("partition_info.efi_size_mb 与原始配置 efi_size 不一致")

    if partition_info.get("c_size_gb") != config.get("c_size"):
        raise AssertionError("partition_info.c_size_gb 与原始配置 c_size 不一致")

    if image_info.get("image_path") != config.get("win_gho"):
        raise AssertionError("image_info.image_path 与原始配置 win_gho 不一致")

    if software_paths.get("ghost64_path") != config.get("gho_exe"):
        raise AssertionError("software_paths.ghost64_path 与原始配置 gho_exe 不一致")

    if software_paths.get("bcdboot_path") != config.get("bcd_exe"):
        raise AssertionError("software_paths.bcdboot_path 与原始配置 bcd_exe 不一致")

    if copy_info.get("source_dir") != config.get("software_file"):
        raise AssertionError("copy_info.source_dir 与原始配置 software_file 不一致")

    if excluded_disk_names != config.get("excluded_disk_names"):
        raise AssertionError("excluded_disk_names 与原始配置不一致")

    if not isinstance(excluded_disk_names, list):
        raise AssertionError("excluded_disk_names 必须为列表")

    for item in excluded_disk_names:
        if not isinstance(item, str) or not item.strip():
            raise AssertionError("excluded_disk_names 中每一项都必须为非空字符串")



def write_temp_config(data: Any) -> str:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as temp_file:
        if isinstance(data, str):
            temp_file.write(data)
        else:
            json.dump(data, temp_file, ensure_ascii=False, indent=2)
        return temp_file.name



def expect_load_config_failure(data: Any, expected_message: str) -> None:
    temp_path = write_temp_config(data)
    try:
        load_config(temp_path)
    except Exception as exc:
        if expected_message not in str(exc):
            raise AssertionError(f"期望错误信息包含: {expected_message}，实际为: {exc}") from exc
    else:
        raise AssertionError("期望 load_config 失败，但实际成功")
    finally:
        Path(temp_path).unlink(missing_ok=True)



def run_negative_tests() -> None:
    expect_load_config_failure(
        {
            "description": "Windows 11 配置",
            "win_gho": "D:/img/system.gho",
            "efi_size": 100,
            "software_file": "D:/软件",
            "gho_exe": "D:/sw/ghost64.exe",
            "bcd_exe": "D:/sw/bcdboot.exe",
            "excluded_disk_names": ["Disk A"],
        },
        "配置文件缺少必要字段",
    )

    expect_load_config_failure(
        {
            "description": "   ",
            "win_gho": "D:/img/system.gho",
            "efi_size": 100,
            "c_size": 1536,
            "software_file": "D:/软件",
            "gho_exe": "D:/sw/ghost64.exe",
            "bcd_exe": "D:/sw/bcdboot.exe",
            "excluded_disk_names": ["Disk A"],
        },
        "配置字段 description 必须为非空字符串",
    )

    expect_load_config_failure(
        {
            "description": "Windows 11 配置",
            "win_gho": "D:/img/system.gho",
            "efi_size": 0,
            "c_size": 1536,
            "software_file": "D:/软件",
            "gho_exe": "D:/sw/ghost64.exe",
            "bcd_exe": "D:/sw/bcdboot.exe",
            "excluded_disk_names": ["Disk A"],
        },
        "配置字段 efi_size 必须大于 0",
    )

    expect_load_config_failure(
        {
            "description": "Windows 11 配置",
            "win_gho": "D:/img/system.gho",
            "efi_size": 100,
            "c_size": 1536,
            "software_file": "D:/软件",
            "gho_exe": "D:/sw/ghost64.exe",
            "bcd_exe": "D:/sw/bcdboot.exe",
            "excluded_disk_names": ["Disk A", ""],
        },
        "配置字段 excluded_disk_names 中的每一项都必须为非空字符串",
    )

    expect_load_config_failure("[]", "配置文件根节点必须为对象")



def main() -> int:
    try:
        payload = load_config()
        validate_payload(payload)
        run_negative_tests()
        print_config_payload(payload)
        print("\n模块4测试结果: 通过")
        return 0
    except Exception as exc:
        print(f"测试失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
