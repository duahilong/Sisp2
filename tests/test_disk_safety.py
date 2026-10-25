import json
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.disk_safety import (
    build_drive_letter_ownership_script,
    build_resource_paths,
    is_unc_path,
    normalize_expected_identities,
    resolve_path_disk_number,
    validate_disk_identity,
    validate_drive_letter_ownership,
    validate_resources_not_on_target_disks,
)
from app.modules.common.service import ConfigError, IdentityMismatchError, PartitionError


def disk() -> dict:
    return {
        "disk_number": 2,
        "unique_id": "UID-2",
        "serial_number": " sn-2 ",
        "model": "Disk 2",
        "size_bytes": 1000,
    }


def identity() -> dict:
    return {
        "disk_number": 2,
        "unique_id": "UID-2",
        "serial_number": "SN-2",
        "model": "Disk 2",
        "size_bytes": 1000,
    }


def test_validate_disk_identity_requires_strong_identity() -> None:
    with pytest.raises(IdentityMismatchError, match="UniqueId 和 SerialNumber"):
        validate_disk_identity(disk(), {"disk_number": 2, "model": "Disk 2", "size_bytes": 1000})

    with pytest.raises(IdentityMismatchError, match="缺少目标硬盘身份信息"):
        validate_disk_identity(disk(), None)


def test_validate_disk_identity_accepts_serial_case_and_whitespace() -> None:
    validate_disk_identity(disk(), identity())


def test_validate_disk_identity_detects_mismatches() -> None:
    with pytest.raises(IdentityMismatchError, match="硬盘编号不一致"):
        validate_disk_identity(disk(), {**identity(), "disk_number": 3})

    with pytest.raises(IdentityMismatchError, match="UniqueId 不一致"):
        validate_disk_identity(disk(), {**identity(), "unique_id": "WRONG"})

    with pytest.raises(IdentityMismatchError, match="硬盘容量不一致"):
        validate_disk_identity(disk(), {**identity(), "size_bytes": 999})


def test_normalize_expected_identities_requires_all_disks() -> None:
    normalized = normalize_expected_identities([2], [identity()])
    assert normalized[2]["unique_id"] == "UID-2"

    with pytest.raises(IdentityMismatchError, match="缺少目标硬盘身份信息"):
        normalize_expected_identities([2, 3], [identity()])

    with pytest.raises(IdentityMismatchError, match="缺少目标硬盘身份信息"):
        normalize_expected_identities([2], [])

    with pytest.raises(IdentityMismatchError, match="UniqueId 和 SerialNumber"):
        normalize_expected_identities(
            [2],
            [{"disk_number": 2, "model": "Disk 2", "size_bytes": 1000}],
        )


def test_resolve_path_disk_number_parses_json() -> None:
    captured: list[str] = []

    def fake_runner(script: str) -> CompletedProcess[str]:
        captured.append(script)
        return CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"disk_number": 3, "path": "D:\\data"}),
            stderr="",
        )

    assert resolve_path_disk_number("README.md", powershell_runner=fake_runner) == 3
    assert captured and "Get-Volume -FilePath" in captured[0]
    assert "Get-Partition -Volume" in captured[0]


def test_resolve_path_disk_number_returns_none_for_unc() -> None:
    assert resolve_path_disk_number("\\\\server\\share\\win.gho") is None


def test_is_unc_path_keeps_extended_local_paths_local() -> None:
    assert is_unc_path("\\\\server\\share\\win.gho")
    assert is_unc_path("\\\\?\\UNC\\server\\share\\win.gho")
    assert not is_unc_path("\\\\?\\C:\\images\\win.gho")


def test_resolve_path_disk_number_fails_closed() -> None:
    def fake_runner(script: str) -> CompletedProcess[str]:
        return CompletedProcess(args=[], returncode=1, stdout="", stderr="access denied")

    with pytest.raises(ConfigError, match="无法确认路径所在磁盘") as excinfo:
        resolve_path_disk_number("README.md", powershell_runner=fake_runner)
    assert "access denied" in str(excinfo.value)


def test_resolve_path_disk_number_reports_returncode_without_stderr() -> None:
    def fake_runner(script: str) -> CompletedProcess[str]:
        return CompletedProcess(args=[], returncode=-1073741510, stdout="", stderr="")

    with pytest.raises(ConfigError, match="返回码 -1073741510"):
        resolve_path_disk_number("README.md", powershell_runner=fake_runner)


def test_resolve_path_disk_number_fails_when_no_existing_ancestor(monkeypatch) -> None:
    monkeypatch.setattr("app.disk_safety.find_existing_path", lambda path: None)
    with pytest.raises(ConfigError, match="路径不存在"):
        resolve_path_disk_number("Z:\\missing\\path")


def test_build_drive_letter_ownership_script() -> None:
    script = build_drive_letter_ownership_script(2, ["E", "F", "E"])
    assert "Get-Volume -DriveLetter 'E'" in script
    assert "Get-Volume -DriveLetter 'F'" in script
    assert "Get-Partition -Volume $volume" in script
    assert "$partition.DiskNumber -ne 2" in script
    assert script.count("Get-Volume -DriveLetter 'E'") == 1


def test_validate_drive_letter_ownership_success() -> None:
    captured: list[str] = []

    def fake_runner(script: str) -> CompletedProcess[str]:
        captured.append(script)
        return CompletedProcess(args=[], returncode=0, stdout="OK", stderr="")

    validate_drive_letter_ownership(2, ["E"], powershell_runner=fake_runner)
    assert captured and "Get-Volume -DriveLetter 'E'" in captured[0]


def test_validate_drive_letter_ownership_fails_closed() -> None:
    def fake_runner(script: str) -> CompletedProcess[str]:
        return CompletedProcess(args=[], returncode=1, stdout="", stderr="盘符属于其他磁盘")

    with pytest.raises(PartitionError, match="盘符归属校验失败"):
        validate_drive_letter_ownership(2, ["E"], powershell_runner=fake_runner)


def test_validate_resources_rejects_target_disk() -> None:
    resources = [
        ("Ghost 镜像文件 (win_gho)", "C:\\images\\win.gho"),
        ("Ghost 可执行文件 (gho_exe)", "C:\\tools\\ghost.exe"),
    ]
    with pytest.raises(ConfigError, match="位于目标硬盘上"):
        validate_resources_not_on_target_disks(
            resources,
            [2],
            resolver=lambda path: 2 if "win.gho" in str(path) else 3,
        )


def test_validate_resources_allows_other_disk_and_unc() -> None:
    resources = [
        ("Ghost 镜像文件 (win_gho)", "C:\\images\\win.gho"),
        ("远程镜像", "\\\\server\\share\\win.gho"),
    ]
    resolved = validate_resources_not_on_target_disks(
        resources,
        [2],
        resolver=lambda path: 3,
    )
    assert resolved["远程镜像"] is None
    assert resolved["Ghost 镜像文件 (win_gho)"] == 3


def test_validate_resources_unknown_disk_fails_closed() -> None:
    with pytest.raises(ConfigError, match="无法确认"):
        validate_resources_not_on_target_disks(
            [("Ghost 镜像文件 (win_gho)", "C:\\images\\win.gho")],
            [2],
            resolver=lambda path: None,
        )


def test_build_resource_paths_includes_key_resources() -> None:
    payload = {
        "config_path": "C:\\config\\win11.json",
        "software_paths": {"ghost64_path": "C:\\tools\\ghost64.exe", "bcdboot_path": "C:\\tools\\bcdboot.exe"},
        "image_info": {"image_path": "C:\\images\\win.gho"},
        "copy_info": {"source_dir": "C:\\source"},
    }
    names = {name for name, _ in build_resource_paths(payload)}
    assert "Ghost 镜像文件 (win_gho)" in names
    assert "目录拷贝源 (software_file)" in names
    assert "配置文件" in names


def test_build_resource_paths_accepts_path_objects(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    result_dir = tmp_path / "results"
    names = {
        name
        for name, _ in build_resource_paths(
            {},
            log_dir=log_dir,
            result_dir=result_dir,
            include_app=False,
        )
    }
    assert "日志目录" in names
    assert "worker 结果目录" in names
