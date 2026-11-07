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

from app.application.workflow import WorkflowController
from app.models import WorkerResult
from app.preflight import CHECK_SETTING_PATHS, CHECK_TOOLS, run_preflight_checks


def _preflight(settings: dict[str, Any], tmp_path) -> dict[str, Any]:
    return run_preflight_checks(
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
    report = _preflight({"extGhost": str(tmp_path / "nope" / "ghost64.exe")}, tmp_path)
    item = _check(report, CHECK_TOOLS)
    assert item["passed"] is False
    assert "不存在" in item["message"]


def test_preflight_accepts_existing_configured_tool(tmp_path) -> None:
    tool = tmp_path / "ghost64.exe"
    tool.write_text("stub", encoding="utf-8")
    item = _check(_preflight({"extGhost": str(tool)}, tmp_path), CHECK_TOOLS)
    assert item["passed"] is True
    assert "ghost64" in item["message"]


def test_preflight_reports_missing_system_image(tmp_path) -> None:
    item = _check(
        _preflight({"sysImageA": str(tmp_path / "install.GHO")}, tmp_path),
        CHECK_SETTING_PATHS,
    )
    assert item["passed"] is False
    assert "系统 A 镜像" in item["message"]


def test_preflight_creates_temp_dir_and_passes(tmp_path) -> None:
    temp = tmp_path / "sisp-temp" / "nested"
    item = _check(_preflight({"sysTemp": str(temp)}, tmp_path), CHECK_SETTING_PATHS)
    assert item["passed"] is True
    assert temp.is_dir()


def test_preflight_rejects_wrong_path_kind(tmp_path) -> None:
    directory = tmp_path / "wim-dir"
    directory.mkdir()
    item = _check(_preflight({"sysImageA": str(directory)}, tmp_path), CHECK_SETTING_PATHS)
    assert item["passed"] is False
    assert "类型不对" in item["message"]


# ------------------------------------------------------------------ 工作流
def test_workflow_passes_release_switch_both_ways() -> None:
    """releaseEfiLetter 是布尔：True/False 都要原样下发（False 不能被当空值丢掉）。"""
    for value, expected in ((True, True), (False, False)):
        wf = WorkflowController(
            service=SimpleNamespace(allowed_disk_numbers=frozenset({1})),
            dry_run=True,
            preflight_runner=lambda *, dry_run=False: {"results": []},
            settings_provider=lambda v=value: {"releaseEfiLetter": v},
        )
        assert wf._runner_options()["release_efi_letter"] is expected


def test_workflow_release_switch_defaults_to_on() -> None:
    wf = WorkflowController(
        service=SimpleNamespace(allowed_disk_numbers=frozenset({1})),
        dry_run=True,
        preflight_runner=lambda *, dry_run=False: {"results": []},
        settings_provider=dict,
    )
    assert wf._runner_options()["release_efi_letter"] is True


def test_workflow_maps_settings_to_runner_options() -> None:
    wf = WorkflowController(
        service=SimpleNamespace(allowed_disk_numbers=frozenset({1})),
        dry_run=True,
        preflight_runner=lambda *, dry_run=False: {"results": []},
        settings_provider=lambda: {
            "sysTemp": r"D:\temp",
            "sysBoot": r"D:\boot",
            "unrelated": "ignored",
        },
    )
    assert wf._runner_options() == {
        "temp_dir": r"D:\temp",
        "boot_source": r"D:\boot",
        # 布尔开关总是下发（False 也要传下去，否则关不掉释放行为）
        "release_efi_letter": True,
    }


def test_workflow_preflight_receives_settings() -> None:
    seen: list[dict[str, Any]] = []

    def preflight(*, dry_run=False, settings=None):
        seen.append(dict(settings or {}))
        return {"results": []}

    wf = WorkflowController(
        service=SimpleNamespace(allowed_disk_numbers=frozenset({1})),
        dry_run=True,
        preflight_runner=preflight,
        settings_provider=lambda: {"sysTemp": r"D:\temp"},
    )
    wf._run_preflight()
    assert seen == [{"sysTemp": r"D:\temp"}]


def test_workflow_survives_broken_settings_provider() -> None:
    def boom() -> dict[str, Any]:
        raise RuntimeError("设置文件损坏")

    wf = WorkflowController(
        service=SimpleNamespace(allowed_disk_numbers=frozenset({1})),
        dry_run=True,
        preflight_runner=lambda *, dry_run=False: {"results": []},
        settings_provider=boom,
    )
    assert wf._settings() == {}
    # 设置读不到时开关回落默认值 True
    assert wf._runner_options() == {"release_efi_letter": True}


# ------------------------------------------------------------------ 执行器
def _image_phase(commands: list[list[str]], **kwargs):
    """用替身 ProcessShell 构造 GhostImagePhase，记录拼出来的命令。"""
    from app.application.command_log import CommandLog
    from app.application.phases.ghost import GhostImagePhase
    from app.application.shell import ProcessShell

    def fake_run(command, timeout, *, env=None, cwd=None):
        commands.append(list(command))
        return 0, "ok", 0.1

    shell = ProcessShell(log=CommandLog(None), runner=fake_run)
    return GhostImagePhase(shell, ghost="ghost64.exe", cwd=kwargs.get("cwd"))


# 引导校验脚本的"合格"输出：写引导之后探针要能查到这两个文件
_PROBE_OK = json.dumps(
    {
        "boot_file": r"E:\EFI\Microsoft\Boot\bootmgfw.efi",
        "system_file": r"F:\Windows\System32\config\SYSTEM",
    }
)


def _boot_phase(commands: list[list[str]]):
    from app.application.command_log import CommandLog
    from app.application.shell import ProcessShell
    from app.application.uefi import UefiBootPhase

    def fake_run(command, timeout, *, env=None, cwd=None):
        commands.append(list(command))
        if str(command[0]).lower().startswith("powershell"):
            return 0, _PROBE_OK, 0.1
        return 0, "ok", 0.1

    shell = ProcessShell(log=CommandLog(None), runner=fake_run)
    return UefiBootPhase(shell, bcdboot="bcdboot.exe")


def _ctx(options=None, letters=None):
    import threading

    from app.application.command_log import CommandLog
    from app.application.partitions import plan_partitions
    from app.application.phases.base import PhaseContext

    payload = {
        "partition_info": {
            "efi_size_mb": 100,
            "c_size_gb": 3,
            "data_size_gb": "auto",
            "allow_small_partitions": True,
        }
    }
    return PhaseContext(
        disk_number=1,
        parts=plan_partitions(payload, letters={"efi": "E", "windows": "F", "data": "G"}),
        letters=letters or {"efi": "E", "windows": "F", "data": "G"},
        image_path=r"D:\images	est.GHO",
        image_index=1,
        cancel=threading.Event(),
        log=CommandLog(None),
        options=options or {},
    )


def test_apply_image_uses_ghost_clone() -> None:
    commands: list[list[str]] = []
    phase = _image_phase(commands)
    outcome = phase.run(_ctx())
    assert outcome.ok is True
    command = commands[0]
    assert command[0] == "ghost64.exe"
    assert "mode=pload" in command[1]
    assert "dst=2:2" in command[1]
    assert "-sure" in command and "-batch" in command and "-ntexact" in command
    assert not any(part.startswith("-rb") for part in command)


def test_write_boot_uses_settings_boot_source() -> None:
    commands: list[list[str]] = []
    phase = _boot_phase(commands)
    phase.run(_ctx(options={"boot_source": "D:/boot"}))
    assert commands[0] == ["bcdboot.exe", "D:/boot", "/s", "E:", "/f", "UEFI"]


def test_write_boot_defaults_to_windows_dir() -> None:
    commands: list[list[str]] = []
    phase = _boot_phase(commands)
    phase.run(_ctx())
    windows = "F:" + chr(92) + "Windows"
    assert commands[0] == ["bcdboot.exe", windows, "/s", "E:", "/f", "UEFI"]


def test_runtime_env_creates_temp_dir(tmp_path) -> None:
    from app.application.command_log import CommandLog
    from app.application.runner_utils import runtime_env

    temp = tmp_path / "scratch"
    env = runtime_env({"temp_dir": str(temp)}, CommandLog(None))
    assert temp.is_dir()
    assert env["TMP"] == str(temp)
    assert env["TEMP"] == str(temp)


def test_runner_accepts_runner_options_on_spec(
    tmp_path, make_runner, fake_shell, fake_process, make_snapshot
) -> None:
    """整条链路：runner_options 会传到 bcdboot 的命令行，镜像是 Ghost 命令。"""
    from app.application.partitions import plan_partitions

    image = tmp_path / "tiny.GHO"
    image.write_bytes(b"x")
    payload = {
        "partition_info": {
            "efi_size_mb": 100,
            "c_size_gb": 3,
            "data_size_gb": "auto",
            # 单测用小尺寸：显式放宽后端下限（C ≥ 60 GB）
            "allow_small_partitions": True,
        },
        "image_info": {"image_path": str(image), "image_index": 1},
    }
    letters = {"efi": "E", "windows": "F", "data": "G"}
    parts = plan_partitions(payload, letters=letters)
    shell = fake_shell(query_handler=lambda disk: make_snapshot(parts), msr=None)

    def handler(command, timeout, env):
        if str(command[0]).lower().startswith("powershell"):
            return 0, _PROBE_OK, 0.1
        return 0, "ok", 0.1

    process = fake_process(handler=handler)
    from app.application.uefi import UefiBootPhase

    runner = make_runner(
        shell=shell,
        process=process,
        ghost="ghost64.exe",
        bcdboot="bcdboot.exe",
        uefi=UefiBootPhase(process, bcdboot="bcdboot.exe"),
    )

    spec = SimpleNamespace(
        disk_number=1,
        task_payload=payload,
        drive_letters=letters,
        runner_options={"boot_source": "D:/boot"},
    )
    result = runner.run(spec, threading.Event())

    assert result.success is True, result.error
    assert any("mode=pload" in " ".join(command) for command in process.calls)
    assert any("D:/boot" in command for command in process.calls)


@pytest.mark.parametrize("bad", ["", "   "])
def test_workflow_ignores_blank_setting_values(bad: str) -> None:
    wf = WorkflowController(
        service=SimpleNamespace(allowed_disk_numbers=frozenset({1})),
        dry_run=True,
        preflight_runner=lambda *, dry_run=False: {"results": []},
        settings_provider=lambda: {"sysTemp": bad},
    )
    # 布尔开关总是在（默认 True），空字符串项不该混进来
    assert wf._runner_options() == {"release_efi_letter": True}


def _noop(*_args: Any, **_kwargs: Any) -> WorkerResult:
    return WorkerResult.failure(1, "unused", dry_run=True)


def test_diskpart_script_written_into_configured_temp_dir(tmp_path) -> None:
    """sysTemp 不只改子进程 TMP/TEMP：自己的 diskpart 脚本也要放进去。"""
    from app.application.command_log import CommandLog
    from app.application.shell import DiskpartShell, ProcessShell

    seen: dict[str, Any] = {}

    def fake_run(command, timeout, *, env=None, cwd=None):
        script = pathlib.Path(command[2])
        seen["parent"] = script.parent
        seen["existed"] = script.exists()
        return 0, "", 0.1

    process = ProcessShell(log=CommandLog(None), runner=fake_run)
    shell = DiskpartShell(
        log=CommandLog(None),
        diskpart="diskpart.exe",
        temp_dir=str(tmp_path),
        process=process,
    )
    shell.run_diskpart(["exit"])

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
        preflight_runner=lambda *, dry_run=False: {"results": []},
        partition_defaults=dict(payload or {}),
        settings_provider=lambda: dict(settings),
        worker_interval=0.0,
    )


def test_live_partition_defaults_follow_settings() -> None:
    """设置页改完 EFI / 分区大小 / 文件系统后，下一次建计划就用新值。"""
    wf = _controller({"efiDefaultMb": 300, "sysDefaultGb": 200, "dataDefaultGb": 800})
    defaults = wf._live_partition_defaults()
    assert defaults == {
        "efi_size_mb": 300,
        "c_size_gb": 200,
        "data_size_gb": 800,
    }


def test_live_partition_defaults_keeps_auto_for_data() -> None:
    wf = _controller({"dataDefaultGb": "auto"})
    assert wf._live_partition_defaults()["data_size_gb"] is None


def test_start_scans_without_excluded_names() -> None:
    """排除名单功能连同设置项一起移除：开盘扫描不再接收任何名单参数。"""
    seen: list[Any] = []

    def scan(*args: Any, **kwargs: Any) -> list[Any]:
        seen.append((args, kwargs))
        return []

    wf = WorkflowController(
        service=SimpleNamespace(allowed_disk_numbers=frozenset({1}), dry_run=True, scan_protected_disks=scan),
        dry_run=True,
        preflight_runner=lambda *, dry_run=False: {"results": []},
        worker_interval=0.0,
    )
    wf.start(dry_run=True)
    assert seen == [((), {})]


def test_default_system_image_from_settings() -> None:
    wf = _controller({"sysImageA": r"D:\\sisp-test\\test.GHO", "imageDefaultIndex": 3})
    wf.start(dry_run=True)
    payload = wf.session.task_payload
    assert payload["image_info"]["image_path"] == str(Path(r"D:\\sisp-test\\test.GHO"))
    assert payload["image_info"]["image_index"] == 3


def test_settings_image_is_the_only_source() -> None:
    """``sysImageA`` 是镜像路径的唯一来源：会话载荷里就是设置里那一份。"""
    wf = _controller({"sysImageA": r"D:\sisp-test\test.GHO", "imageDefaultIndex": 1})
    wf.start(dry_run=True)
    info = wf.session.task_payload["image_info"]
    assert info["image_path"] == str(Path(r"D:\sisp-test\test.GHO"))
    assert info["image_index"] == 1


def test_default_system_image_falls_back_to_slot_b() -> None:
    """只配了系统 B 时默认镜像就是 B —— 两个槽位是并列的，没有“主槽位”。"""
    wf = _controller({"sysImageB": r"D:\sisp-test\win11.GHO"})
    wf.start(dry_run=True)
    assert wf.session.task_payload["image_info"]["image_path"] == str(
        Path(r"D:\sisp-test\win11.GHO")
    )


def test_default_system_image_prefers_slot_a() -> None:
    wf = _controller(
        {"sysImageA": r"D:\sisp-test\win10.GHO", "sysImageB": r"D:\sisp-test\win11.GHO"}
    )
    wf.start(dry_run=True)
    assert wf.session.task_payload["image_info"]["image_path"] == str(
        Path(r"D:\sisp-test\win10.GHO")
    )


def test_image_default_index_zero_keeps_ghost_default() -> None:
    wf = _controller({"sysImageA": r"D:\images	est.GHO", "imageDefaultIndex": 0})
    wf.start(dry_run=True)
    assert "image_index" not in wf.session.task_payload["image_info"]


def _image_check(report: dict[str, Any]) -> dict[str, Any] | None:
    for item in report["results"]:
        if item["name"] == "镜像文件存在性检查":
            return item
    return None


def test_preflight_image_check_reads_settings_syswim(tmp_path) -> None:
    """预检只认设置里的 ``sysImageA``（业务配置那一层已移除）。"""
    wim = tmp_path / "real.GHO"
    wim.write_bytes(bytes([0xFE, 0xEF]) + bytes(8))
    report = _preflight({"sysImageA": str(wim)}, tmp_path)
    image = _image_check(report)
    assert image is not None and image["passed"] is True, image
    assert "real.GHO" in image["message"]


def test_preflight_image_check_flags_missing_file(tmp_path) -> None:
    """设置里写的镜像不存在时必须报红，且消息里带全路径。"""
    from app.preflight import run_preflight_checks

    missing = tmp_path / "missing.GHO"
    report = run_preflight_checks(
        dry_run=True,
        admin_checker=lambda: True,
        command_lookup=lambda name: f"C:/{name}.exe",
        disk_usage=lambda root: (0, 0, 10 * 1024**3),
        settings={"sysImageA": str(missing)},
    )
    image = _image_check(report)
    assert image is not None and image["passed"] is False, image
    assert "missing.GHO" in image["message"]

