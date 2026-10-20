import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.cli as cli_module
import app.orchestrator as orchestrator
from app.models import WorkerResult
from app.modules.common.constants import ErrorCode
from app.modules.common.service import InitializationError
from app.results import read_worker_result, write_worker_result
from app.worker_launcher import LaunchedWorker

GB = 1024 ** 3


def sample_disks() -> list[dict]:
    return [
        {
            "disk_number": 1,
            "model": "Target 1",
            "serial_number": "SN-1",
            "unique_id": "UID-1",
            "size_bytes": 500 * GB,
            "size_display": "465.66 GB",
            "partition_style": "GPT",
            "bus_type": "SATA",
            "drive_letters": [],
            "is_boot": False,
            "is_system": False,
            "is_offline": False,
            "is_read_only": False,
        },
        {
            "disk_number": 2,
            "model": "Target 2",
            "serial_number": "SN-2",
            "unique_id": "UID-2",
            "size_bytes": 500 * GB,
            "size_display": "465.66 GB",
            "partition_style": "GPT",
            "bus_type": "SATA",
            "drive_letters": [],
            "is_boot": False,
            "is_system": False,
            "is_offline": False,
            "is_read_only": False,
        },
    ]


def build_payload(tmp_path: Path) -> dict:
    image = tmp_path / "win.gho"
    ghost = tmp_path / "ghost64.exe"
    bcd = tmp_path / "bcdboot.exe"
    source = tmp_path / "source"
    for path in (image, ghost, bcd):
        path.write_bytes(b"x")
    source.mkdir()
    return {
        "config_path": str(tmp_path / "win11.json"),
        "partition_info": {"efi_size_mb": 100, "c_size_gb": 100},
        "image_info": {"image_path": str(image)},
        "software_paths": {"ghost64_path": str(ghost), "bcdboot_path": str(bcd)},
        "copy_info": {"source_dir": str(source)},
        "excluded_disk_names": [],
    }


def build_preflight_report(payload: dict):
    return {
        "all_passed": True,
        "results": [],
        "config_payload": payload,
    }


def test_run_worker_flow_writes_success_result(tmp_path: Path) -> None:
    payload = build_payload(tmp_path)
    result_file = tmp_path / "disk_2.json"
    received: list[dict] = []

    def fake_single_disk_flow(disk_number, config_payload, expected_identity=None, drive_letters=None, run_context=None, dry_run=False):
        received.append({"disk_number": disk_number, "dry_run": dry_run})
        assert run_context is not None
        run_context.start_step("初始化")
        run_context.record_step("初始化", True, "完成")

    original = orchestrator.run_single_disk_flow
    orchestrator.run_single_disk_flow = fake_single_disk_flow
    try:
        orchestrator.run_worker_flow(
            2,
            preflight_runner=lambda _: build_preflight_report(payload),
            result_file=result_file,
        )
    finally:
        orchestrator.run_single_disk_flow = original

    result = read_worker_result(result_file)
    assert result is not None
    assert result.success is True
    assert result.error_code == ErrorCode.SUCCESS
    assert result.steps[0].name == "初始化"
    assert received == [{"disk_number": 2, "dry_run": False}]


def test_run_worker_flow_writes_failure_result(tmp_path: Path) -> None:
    payload = build_payload(tmp_path)
    result_file = tmp_path / "disk_2.json"

    def fake_single_disk_flow(*args, **kwargs):
        raise InitializationError("硬盘初始化失败: boom")

    original = orchestrator.run_single_disk_flow
    orchestrator.run_single_disk_flow = fake_single_disk_flow
    try:
        with pytest.raises(InitializationError):
            orchestrator.run_worker_flow(
                2,
                preflight_runner=lambda _: build_preflight_report(payload),
                result_file=result_file,
            )
    finally:
        orchestrator.run_single_disk_flow = original

    result = read_worker_result(result_file)
    assert result is not None
    assert result.success is False
    assert result.error_code == ErrorCode.INITIALIZATION_FAILED
    assert result.message == "硬盘初始化失败: boom"


def test_run_worker_flow_writes_unexpected_result(tmp_path: Path) -> None:
    payload = build_payload(tmp_path)
    result_file = tmp_path / "disk_2.json"

    def fake_single_disk_flow(*args, **kwargs):
        raise RuntimeError("boom")

    original = orchestrator.run_single_disk_flow
    orchestrator.run_single_disk_flow = fake_single_disk_flow
    try:
        with pytest.raises(RuntimeError):
            orchestrator.run_worker_flow(
                2,
                preflight_runner=lambda _: build_preflight_report(payload),
                result_file=result_file,
            )
    finally:
        orchestrator.run_single_disk_flow = original

    result = read_worker_result(result_file)
    assert result is not None
    assert result.error_code == ErrorCode.UNEXPECTED_ERROR
    assert "RuntimeError: boom" in result.message


def test_run_minimal_main_flow_collects_worker_results(tmp_path: Path, monkeypatch) -> None:
    payload = build_payload(tmp_path)
    result_files = {}
    for disk_number in (1, 2):
        path = tmp_path / f"disk_{disk_number}.json"
        write_worker_result(
            path,
            WorkerResult(disk_number=disk_number, success=True, error_code=0, message="完成"),
        )
        result_files[disk_number] = path

    monkeypatch.setattr(orchestrator, "scan_disk_summaries", lambda: sample_disks())
    monkeypatch.setattr(orchestrator, "scan_used_drive_letters", lambda: [])
    monkeypatch.setattr(
        orchestrator,
        "run_single_disk_flow",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("多盘模式不应在当前进程执行单盘流程")),
    )

    received_identities: list[list[dict]] = []

    def fake_launcher(identities, config_path):
        received_identities.append(identities)
        return [
            LaunchedWorker(disk_number=1, result_file=result_files[1]),
            LaunchedWorker(disk_number=2, result_file=result_files[2]),
        ]

    selected = orchestrator.run_minimal_main_flow(
        input_func=lambda prompt: "1,2",
        preflight_runner=lambda _: build_preflight_report(payload),
        worker_launcher=fake_launcher,
        worker_result_timeout=1,
    )

    assert selected == [1, 2]
    assert received_identities[0][0]["drive_letters"] == {"efi": "E", "windows": "F", "data1": "G", "data2": "H"}
    assert received_identities[0][1]["drive_letters"] == {"efi": "I", "windows": "J", "data1": "K", "data2": "L"}


def test_dry_run_does_not_call_destructive_functions(tmp_path: Path, monkeypatch, capsys) -> None:
    payload = build_payload(tmp_path)
    monkeypatch.setattr(orchestrator, "scan_disk_summaries", lambda: sample_disks())

    for name in (
        "initialize_disks",
        "partition_and_format_disks",
        "write_ghost_image",
        "copy_directory",
        "create_boot_record",
    ):
        monkeypatch.setattr(
            orchestrator,
            name,
            lambda *args, __name=name, **kwargs: (_ for _ in ()).throw(AssertionError(f"dry-run 不应调用 {__name}")),
        )

    orchestrator.run_single_disk_flow(
        1,
        payload,
        drive_letters={"efi": "E", "windows": "F", "data1": "G", "data2": "H"},
        dry_run=True,
    )

    output = capsys.readouterr().out
    assert "Dry-run 执行计划" in output
    assert "不会执行任何写盘操作" in output
    assert "Get-Disk -Number 1" in output
    assert "Ghost" in output
    assert "未执行任何磁盘操作" in output


def test_dry_run_without_drive_letters_uses_placeholders(tmp_path: Path, monkeypatch, capsys) -> None:
    payload = build_payload(tmp_path)
    monkeypatch.setattr(orchestrator, "scan_disk_summaries", lambda: sample_disks())

    orchestrator.run_single_disk_flow(1, payload, dry_run=True)

    output = capsys.readouterr().out
    assert "<Windows盘符>" in output
    assert "<EFI盘符>" in output


def test_cli_check_config(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source"
    source.mkdir()
    image = tmp_path / "win.gho"
    ghost = tmp_path / "ghost64.exe"
    bcd = tmp_path / "bcdboot.exe"
    for path in (image, ghost, bcd):
        path.write_bytes(b"x")

    config = {
        "description": "test",
        "win_gho": str(image),
        "efi_size": 100,
        "c_size": 100,
        "software_file": str(source),
        "gho_exe": str(ghost),
        "bcd_exe": str(bcd),
        "excluded_disk_names": [],
    }
    good_path = tmp_path / "good.json"
    good_path.write_text(json.dumps(config), encoding="utf-8")
    assert cli_module.check_config(good_path) == ErrorCode.SUCCESS
    assert "配置检查通过" in capsys.readouterr().out

    bad_config = dict(config)
    bad_config["win_gho"] = str(source)
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad_config), encoding="utf-8")
    assert cli_module.check_config(bad_path) == ErrorCode.CONFIG_ERROR
    assert "不是文件" in capsys.readouterr().err


def test_cli_list_disks(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_module, "load_config", lambda _: {"excluded_disk_names": []})
    monkeypatch.setattr(cli_module, "scan_disk_summaries", lambda: sample_disks())

    assert cli_module.list_disks() == ErrorCode.SUCCESS
    output = capsys.readouterr().out
    assert "Sisp - 硬盘列表" in output
    assert "Target 1" in output


def test_cli_main_passes_dry_run(monkeypatch) -> None:
    received: list[dict] = []

    def fake_flow(**kwargs):
        received.append(kwargs)
        return [1]

    monkeypatch.setattr(cli_module, "run_minimal_main_flow", fake_flow)
    assert cli_module.main(["--dry-run", "--no-log"]) == ErrorCode.SUCCESS
    assert received[0]["dry_run"] is True
    assert received[0]["worker_interval"] == 6.0


def test_cli_worker_passes_result_file(monkeypatch) -> None:
    received: list[dict] = []

    def fake_worker(disk_number, **kwargs):
        received.append({"disk_number": disk_number, **kwargs})

    monkeypatch.setattr(cli_module, "run_worker_flow", fake_worker)
    code = cli_module.main(
        [
            "--worker-disk",
            "2",
            "--worker-result-file",
            "D:\\temp\\result.json",
            "--no-log",
        ]
    )
    assert code == ErrorCode.SUCCESS
    assert received[0]["disk_number"] == 2
    assert received[0]["result_file"] == "D:\\temp\\result.json"


def test_cli_version(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["--version"])
    assert exc_info.value.code == 0
    assert "Sisp 0.2.0" in capsys.readouterr().out
