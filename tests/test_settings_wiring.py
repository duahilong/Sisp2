"""界面设置到后端行为的接线测试。

这些设置之前只存在于 ``settings.json`` 和设置页，代码里没人读。
本文件保证它们真的影响：运行前检查、执行器命令行参数。
"""

from __future__ import annotations

import json
import pathlib
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.application.real_runner import WindowsDiskRunner
from app.application.workflow import WorkflowController
from app.models import WorkerResult
from app.preflight import CHECK_SETTING_PATHS, CHECK_TOOLS, run_preflight_checks


def _preflight(settings: dict[str, Any], tmp_path) -> dict[str, Any]:
    return run_preflight_checks(
        None,
        dry_run=True,
        admin_checker=lambda: True,
        command_lookup=lambda name: f"C:/{name}.exe",
        disk_usage=lambda root: (0, 0, 10 * 1024**3),
        settings=settings,
    )


def _check(report: dict[str, Any], name: str) -> dict[str, Any]:
    for item in report["results"]:
        if item["name"] == name:
            return item
    raise AssertionError(f"缺少检查项：{name}")


# ------------------------------------------------------------------ 预检
def test_preflight_without_settings_skips_new_checks(tmp_path) -> None:
    report = _preflight({}, tmp_path)
    names = {item["name"] for item in report["results"]}
    assert CHECK_TOOLS not in names
    assert CHECK_SETTING_PATHS not in names


def test_preflight_reports_missing_configured_tool(tmp_path) -> None:
    report = _preflight({"ext7z": str(tmp_path / "nope" / "7z.exe")}, tmp_path)
    item = _check(report, CHECK_TOOLS)
    assert item["passed"] is False
    assert "不存在" in item["message"]


def test_preflight_accepts_existing_configured_tool(tmp_path) -> None:
    tool = tmp_path / "7z.exe"
    tool.write_text("stub", encoding="utf-8")
    item = _check(_preflight({"ext7z": str(tool)}, tmp_path), CHECK_TOOLS)
    assert item["passed"] is True
    assert "7-Zip" in item["message"]


def test_preflight_reports_missing_system_wim(tmp_path) -> None:
    item = _check(
        _preflight({"sysWim": str(tmp_path / "install.wim")}, tmp_path),
        CHECK_SETTING_PATHS,
    )
    assert item["passed"] is False
    assert "系统 WIM 文件" in item["message"]


def test_preflight_creates_temp_dir_and_passes(tmp_path) -> None:
    temp = tmp_path / "sisp-temp" / "nested"
    item = _check(_preflight({"sysTemp": str(temp)}, tmp_path), CHECK_SETTING_PATHS)
    assert item["passed"] is True
    assert temp.is_dir()


def test_preflight_rejects_wrong_path_kind(tmp_path) -> None:
    directory = tmp_path / "wim-dir"
    directory.mkdir()
    item = _check(_preflight({"sysWim": str(directory)}, tmp_path), CHECK_SETTING_PATHS)
    assert item["passed"] is False
    assert "类型不对" in item["message"]


# ------------------------------------------------------------------ 工作流
def test_workflow_passes_release_switch_both_ways() -> None:
    """releaseEfiLetter 是布尔：True/False 都要原样下发（False 不能被当空值丢掉）。"""
    for value, expected in ((True, True), (False, False)):
        wf = WorkflowController(
            service=SimpleNamespace(allowed_disk_numbers=frozenset({1})),
            dry_run=True,
            preflight_runner=lambda path: {"results": [], "config_payload": {}},
            settings_provider=lambda v=value: {"releaseEfiLetter": v},
        )
        assert wf._runner_options()["release_efi_letter"] is expected


def test_workflow_release_switch_defaults_to_on() -> None:
    wf = WorkflowController(
        service=SimpleNamespace(allowed_disk_numbers=frozenset({1})),
        dry_run=True,
        preflight_runner=lambda path: {"results": [], "config_payload": {}},
        settings_provider=dict,
    )
    assert wf._runner_options()["release_efi_letter"] is True


def test_workflow_maps_settings_to_runner_options() -> None:
    wf = WorkflowController(
        service=SimpleNamespace(allowed_disk_numbers=frozenset({1})),
        dry_run=True,
        preflight_runner=lambda path: {"results": [], "config_payload": {}},
        settings_provider=lambda: {
            "sysTemp": r"D:\temp",
            "sysUnattend": r"D:\answer.xml",
            "sysDrivers": r"D:\drivers",
            "sysBoot": r"D:\boot",
            "unrelated": "ignored",
        },
    )
    assert wf._runner_options() == {
        "temp_dir": r"D:\temp",
        "unattend_path": r"D:\answer.xml",
        "drivers_path": r"D:\drivers",
        "boot_source": r"D:\boot",
        # 布尔开关总是下发（False 也要传下去，否则关不掉释放行为）
        "release_efi_letter": True,
    }


def test_workflow_preflight_receives_settings() -> None:
    seen: list[dict[str, Any]] = []

    def preflight(config_path=None, *, dry_run=False, settings=None):
        seen.append(dict(settings or {}))
        return {"results": [], "config_payload": {}}

    wf = WorkflowController(
        service=SimpleNamespace(allowed_disk_numbers=frozenset({1})),
        dry_run=True,
        preflight_runner=preflight,
        settings_provider=lambda: {"sysTemp": r"D:\temp"},
    )
    wf._run_preflight(None)
    assert seen == [{"sysTemp": r"D:\temp"}]


def test_workflow_survives_broken_settings_provider() -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("设置文件损坏")

    wf = WorkflowController(
        service=SimpleNamespace(allowed_disk_numbers=frozenset({1})),
        dry_run=True,
        preflight_runner=lambda path: {"results": [], "config_payload": {}},
        settings_provider=boom,
    )
    assert wf._settings() == {}
    # 设置读不到时开关回落默认值 True
    assert wf._runner_options() == {"release_efi_letter": True}


# ------------------------------------------------------------------ 执行器
def _capture_runner() -> tuple[WindowsDiskRunner, list[list[str]]]:
    runner = WindowsDiskRunner(
        diskpart="diskpart.exe",
        dism="dism.exe",
        bcdboot="bcdboot.exe",
        powershell="powershell.exe",
    )
    commands: list[list[str]] = []

    def fake_spawn(command, timeout, log, *, env=None):
        commands.append(list(command))
        return 0, "ok", 0.1

    runner._spawn = fake_spawn  # type: ignore[method-assign]
    return runner, commands


def test_apply_image_passes_unattend_and_drivers() -> None:
    from app.application.real_runner import _CommandLog

    runner, commands = _capture_runner()
    code, _out, _elapsed = runner._apply_image(
        r"D:\images\win11.wim",
        2,
        "F:\\",
        _CommandLog(None),
        options={"unattend_path": r"D:\answer.xml", "drivers_path": r"D:\drivers"},
    )
    assert code == 0
    command = commands[0]
    assert "/Apply-Unattend:D:\\answer.xml" in command
    assert "/Add-Driver:D:\\drivers" in command
    assert "/Recurse" in command


def test_apply_image_without_options_is_unchanged() -> None:
    from app.application.real_runner import _CommandLog

    runner, commands = _capture_runner()
    runner._apply_image(r"D:\images\win11.wim", 1, "F:\\", _CommandLog(None))
    command = commands[0]
    assert not any("Unattend" in part for part in command)
    assert not any("Add-Driver" in part for part in command)


def test_write_boot_uses_settings_boot_source() -> None:
    from app.application.real_runner import _CommandLog

    runner, commands = _capture_runner()
    letters = {"efi": "E", "windows": "F", "data": "G"}
    runner._write_boot(letters, _CommandLog(None), options={"boot_source": r"D:\boot"})
    assert commands[0] == ["bcdboot.exe", r"D:\boot", "/s", "E:", "/f", "UEFI"]


def test_write_boot_defaults_to_windows_dir() -> None:
    from app.application.real_runner import _CommandLog

    runner, commands = _capture_runner()
    letters = {"efi": "E", "windows": "F", "data": "G"}
    runner._write_boot(letters, _CommandLog(None))
    assert commands[0] == ["bcdboot.exe", "F:\\Windows", "/s", "E:", "/f", "UEFI"]


def test_runtime_env_creates_temp_dir(tmp_path) -> None:
    from app.application.real_runner import _CommandLog

    temp = tmp_path / "scratch"
    env = WindowsDiskRunner._runtime_env({"temp_dir": str(temp)}, _CommandLog(None))
    assert temp.is_dir()
    assert env["TMP"] == str(temp)
    assert env["TEMP"] == str(temp)


def test_runner_accepts_runner_options_on_spec(tmp_path, monkeypatch) -> None:
    """整条链路：spec.runner_options 会传到 dism/bcdboot 的命令行。"""
    image = tmp_path / "tiny.wim"
    image.write_bytes(b"x")
    runner = WindowsDiskRunner(
        diskpart="diskpart.exe",
        dism="dism.exe",
        bcdboot="bcdboot.exe",
        powershell="powershell.exe",
    )
    commands: list[list[str]] = []

    def fake_run_diskpart(lines, log, timeout=600.0, **kwargs):
        commands.append(list(lines))
        return 0, "ok"

    def fake_spawn(command, timeout, log, *, env=None):
        commands.append(list(command))
        return 0, "ok", 0.1

    runner._run_diskpart = fake_run_diskpart  # type: ignore[method-assign]
    runner._spawn = fake_spawn  # type: ignore[method-assign]
    runner._verify = lambda disk_number, log, letters=None, **_: SimpleNamespace(failed=False)  # type: ignore[method-assign]
    # 分区查询/建完复核走 PowerShell，这里不关心，给一个“正常 GPT、无 MSR”的答案
    runner._query_partitions = lambda disk, log: {  # type: ignore[method-assign]
        "partition_style": "GPT",
        "partitions": [],
        "largest_free": 0,
    }
    runner._verify_partitions_after_create = (  # type: ignore[method-assign]
        lambda disk, parts, log: (True, "ok", {"efi_partition": 1})
    )

    spec = SimpleNamespace(
        disk_number=1,
        config_payload={
            "partition_info": {
                "efi_size_mb": 100,
                "c_size_gb": 3,
                "data_size_gb": "auto",
                # 单测用小尺寸：显式放宽后端下限（C ≥ 60 GB）
                "allow_small_partitions": True,
            },
            "image_info": {"image_path": str(image), "image_index": 1},
        },
        drive_letters={"efi": "E", "windows": "F", "data": "G"},
        runner_options={"unattend_path": r"D:\answer.xml"},
    )
    result = runner.run(spec, threading.Event())

    assert result.success is True, result.error
    assert any("/Apply-Unattend:D:\\answer.xml" in command for command in commands)


@pytest.mark.parametrize("bad", ["", "   "])
def test_workflow_ignores_blank_setting_values(bad: str) -> None:
    wf = WorkflowController(
        service=SimpleNamespace(allowed_disk_numbers=frozenset({1})),
        dry_run=True,
        preflight_runner=lambda path: {"results": [], "config_payload": {}},
        settings_provider=lambda: {"sysTemp": bad},
    )
    # 布尔开关总是在（默认 True），空字符串项不该混进来
    assert wf._runner_options() == {"release_efi_letter": True}


def _noop(*_args: Any, **_kwargs: Any) -> WorkerResult:
    return WorkerResult.failure(1, "unused", dry_run=True)


def test_diskpart_script_written_into_configured_temp_dir(tmp_path) -> None:
    """sysTemp 不只改子进程 TMP/TEMP：自己的 diskpart 脚本也要放进去。"""
    runner = WindowsDiskRunner(
        diskpart="diskpart.exe",
        dism="dism.exe",
        bcdboot="bcdboot.exe",
        powershell="powershell.exe",
    )
    from app.application.real_runner import _CommandLog

    log = _CommandLog(None)
    seen: dict[str, Any] = {}

    def fake_spawn(command, timeout, log, *, env=None):
        script = pathlib.Path(command[2])
        seen["parent"] = script.parent
        seen["existed"] = script.exists()
        return 0, "", 0.1

    runner._spawn = fake_spawn  # type: ignore[assignment]
    runner._run_diskpart(["exit"], log, temp_dir=str(tmp_path))

    assert seen["parent"] == tmp_path
    assert seen["existed"] is True
    # 脚本是临时的，跑完要删掉
    assert list(tmp_path.glob("sisp-diskpart-*.txt")) == []


# ------------------------------------------------------------------ 默认值接线
def _controller(settings: dict[str, Any], payload: dict[str, Any] | None = None) -> WorkflowController:
    return WorkflowController(
        service=SimpleNamespace(
            allowed_disk_numbers=frozenset({1}), dry_run=True, scan_protected_disks=lambda names: []
        ),
        dry_run=True,
        preflight_runner=lambda path, **kwargs: {
            "results": [],
            "config_payload": dict(payload if payload is not None else {}),
        },
        settings_provider=lambda: dict(settings),
        worker_interval=0.0,
    )


def test_live_partition_defaults_follow_settings() -> None:
    """设置页改完 EFI / 分区大小 / 文件系统后，下一次建计划就用新值。"""
    wf = _controller({"efiDefaultMb": 300, "sysDefaultGb": 200, "dataDefaultGb": 800, "defaultFs": "exFAT"})
    defaults = wf._live_partition_defaults()
    assert defaults == {
        "efi_size_mb": 300,
        "c_size_gb": 200,
        "data_size_gb": 800,
        "data_fs": "exFAT",
    }


def test_live_partition_defaults_keeps_auto_for_data() -> None:
    wf = _controller({"dataDefaultGb": "auto"})
    assert wf._live_partition_defaults()["data_size_gb"] is None


def test_merged_excluded_names_unions_settings_and_config() -> None:
    wf = _controller({"excludedDiskNames": ["From Settings"]})
    merged = wf._merged_excluded_names({"excluded_disk_names": ["From Config", "from settings"]})
    # 先出现的那份拼写胜出（配置优先于设置），但重复项不会重复计入
    assert merged == ["From Config", "from settings"]


def test_settings_excluded_names_hide_disks_at_start() -> None:
    """设置里的排除名单必须真的让开盘扫描时置灰。"""
    seen: list[Any] = []

    def scan(names: Any = None) -> list[Any]:
        seen.append(list(names or []))
        return []

    wf = WorkflowController(
        service=SimpleNamespace(allowed_disk_numbers=frozenset({1}), dry_run=True, scan_protected_disks=scan),
        dry_run=True,
        preflight_runner=lambda path, **kwargs: {"results": [], "config_payload": {}},
        settings_provider=lambda: {"excludedDiskNames": ["Samsung SSD"]},
        worker_interval=0.0,
    )
    wf.start(None, dry_run=True)
    assert seen and seen[0] == ["Samsung SSD"]


def test_default_system_image_from_settings() -> None:
    wf = _controller({"sysWim": r"D:\\sisp-test\\test.wim", "imageDefaultIndex": 3})
    wf.start(None, dry_run=True)
    payload = wf.session.config_payload
    assert payload["image_info"]["image_path"] == str(Path(r"D:\\sisp-test\\test.wim"))
    assert payload["image_info"]["image_index"] == 3


def test_settings_image_overrides_config_when_config_not_explicit() -> None:
    """界面设置优先时，sysWim 必须覆盖配置里的占位镜像路径。

    模板里写的是占位路径（``json/win11.example.json``），如果只“补缺失”，
    用户在设置页填了真镜像也会一直挂着「镜像文件不存在」的阻断。
    """
    wf = _controller(
        {"sysWim": r"D:\\sisp-test\\test.wim", "imageDefaultIndex": 1},
        payload={"image_info": {"image_path": r"D:\\images\\win11.wim", "image_index": 3}},
    )
    wf.start(None, dry_run=True)
    info = wf.session.config_payload["image_info"]
    assert info["image_path"] == str(Path(r"D:\\sisp-test\\test.wim"))
    assert info["image_index"] == 1


def test_config_image_wins_when_config_explicit() -> None:
    wf = _controller(
        {"sysWim": r"D:\\sisp-test\\test.wim"},
        payload={"image_info": {"image_path": r"D:\\images\\from-config.wim"}},
    )
    wf.start("preset.json", dry_run=True)
    assert wf.session.config_payload["image_info"]["image_path"] == r"D:\\images\\from-config.wim"


def test_image_default_index_zero_keeps_dism_default() -> None:
    wf = _controller({"sysWim": r"D:\images\win11.wim", "imageDefaultIndex": 0})
    wf.start(None, dry_run=True)
    assert "image_index" not in wf.session.config_payload["image_info"]


def _image_check(report: dict[str, Any]) -> dict[str, Any] | None:
    for item in report["results"]:
        if item["name"] == "镜像文件存在性检查":
            return item
    return None


def test_preflight_image_check_prefers_settings_syswim(tmp_path) -> None:
    """无 -j 时，预检要检查界面设置里的镜像，而不是配置里的占位路径。"""
    wim = tmp_path / "real.wim"
    wim.write_bytes(b"wim")
    report = _preflight({"sysWim": str(wim)}, tmp_path)
    image = _image_check(report)
    assert image is not None and image["passed"] is True, image
    assert "real.wim" in image["message"]


def test_preflight_image_check_uses_config_when_explicit(tmp_path) -> None:
    """显式 -j 时预检看配置里的镜像路径（配置优先），设置只作兜底。"""
    from app.preflight import run_preflight_checks

    settings_wim = tmp_path / "settings.wim"
    settings_wim.write_bytes(b"wim")
    config = tmp_path / "preset.json"
    config.write_text(
        json.dumps({"image_info": {"image_path": str(tmp_path / "missing.wim")}}),
        encoding="utf-8",
    )
    report = run_preflight_checks(
        str(config),
        dry_run=True,
        admin_checker=lambda: True,
        command_lookup=lambda name: f"C:/{name}.exe",
        disk_usage=lambda root: (0, 0, 10 * 1024**3),
        settings={"sysWim": str(settings_wim)},
    )
    image = _image_check(report)
    assert image is not None and image["passed"] is False, image
    assert "missing.wim" in image["message"]
