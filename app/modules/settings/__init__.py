"""界面设置模块。

对外只暴露 ``SettingsStore`` / ``build_store``；界面层通过
``app.gui_web.api`` 的 ``get_settings`` / ``update_settings`` 访问。
"""

from app.modules.settings.service import (
    DEFAULTS,
    SECTIONS,
    SETTINGS_VERSION,
    ProbeResult,
    SettingsError,
    SettingsStore,
    build_store,
    coerce_value,
    default_settings_path,
)

__all__ = [
    "DEFAULTS",
    "SECTIONS",
    "SETTINGS_VERSION",
    "ProbeResult",
    "SettingsError",
    "SettingsStore",
    "build_store",
    "coerce_value",
    "default_settings_path",
]
