import json
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "json" / "win11.json"
REQUIRED_KEYS = [
    "description",
    "win_gho",
    "efi_size",
    "c_size",
    "software_file",
    "gho_exe",
    "bcd_exe",
    "excluded_disk_names",
]



def read_json_file(config_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"配置文件解析失败: {path}\n{exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"配置文件根节点必须为对象: {path}")

    return data



def validate_non_empty_string(config: dict[str, Any], key: str) -> None:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"配置字段 {key} 必须为非空字符串")



def validate_positive_number(config: dict[str, Any], key: str) -> None:
    value = config.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"配置字段 {key} 必须为数字")
    if value <= 0:
        raise ValueError(f"配置字段 {key} 必须大于 0")



def validate_string_list(config: dict[str, Any], key: str) -> None:
    value = config.get(key)
    if not isinstance(value, list):
        raise ValueError(f"配置字段 {key} 必须为列表")
    if not value:
        raise ValueError(f"配置字段 {key} 不能为空列表")

    invalid_items = [item for item in value if not isinstance(item, str) or not item.strip()]
    if invalid_items:
        raise ValueError(f"配置字段 {key} 中的每一项都必须为非空字符串")



def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    missing_keys = [key for key in REQUIRED_KEYS if key not in config]
    if missing_keys:
        missing_text = "、".join(missing_keys)
        raise ValueError(f"配置文件缺少必要字段: {missing_text}")

    validate_non_empty_string(config, "description")
    validate_non_empty_string(config, "win_gho")
    validate_non_empty_string(config, "software_file")
    validate_non_empty_string(config, "gho_exe")
    validate_non_empty_string(config, "bcd_exe")
    validate_positive_number(config, "efi_size")
    validate_positive_number(config, "c_size")
    validate_string_list(config, "excluded_disk_names")

    return config



def build_config_payload(config: dict[str, Any], config_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

    return {
        "config_path": str(path),
        "config": config,
        "partition_info": {
            "efi_size_mb": config.get("efi_size"),
            "c_size_gb": config.get("c_size"),
        },
        "feature_flags": {},
        "image_info": {
            "image_path": config.get("win_gho"),
        },
        "software_paths": {
            "ghost64_path": config.get("gho_exe"),
            "bcdboot_path": config.get("bcd_exe"),
        },
        "copy_info": {
            "source_dir": config.get("software_file"),
        },
        "excluded_disk_names": config.get("excluded_disk_names"),
    }



def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    config = read_json_file(config_path)
    validate_config(config)
    return build_config_payload(config, config_path)
