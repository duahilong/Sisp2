"""分区 / 引导 / 镜像三个模块的纯函数与演练路径。

这三块都会真的改硬盘或调用外部程序，所以这里只测“不碰磁盘也能验证的部分”：
参数校验、脚本生成、诊断判断、索引解析。真实写盘由 VM 实测覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.modules.boot import service as boot
from app.modules.image import service as image
from app.modules.partition import service as partition


# --------------------------------------------------------------------- 分区
def test_parse_actions_rejects_bad_shapes() -> None:
    with pytest.raises(partition.PartitionError, match="必须是数组"):
        partition.parse_actions("create")
    with pytest.raises(partition.PartitionError, match="没有任何分区操作"):
        partition.parse_actions([])
    with pytest.raises(partition.PartitionError, match="格式不正确"):
        partition.parse_actions(["create"])
    with pytest.raises(partition.PartitionError, match="类型不支持"):
        partition.parse_actions([{"kind": "shrink"}])
    with pytest.raises(partition.PartitionError, match="文件系统不支持"):
        partition.parse_actions([{"kind": "create", "size_gb": 10, "file_system": "APFS"}])


def test_parse_actions_normalizes_input() -> None:
    actions = partition.parse_actions(
        [
            {"kind": "CREATE", "size_gb": 20, "file_system": "exfat", "letter": "q", "label": "备份 盘"},
            {"kind": "format", "partition_number": "2"},
        ]
    )
    first, second = actions
    assert first.kind == "create"
    assert first.size_mb == 20 * 1024
    assert first.file_system == "exFAT"
    assert first.letter == "Q"
    assert first.label == "备份 盘"
    assert second.kind == "format"
    assert second.partition_number == 2


def _disk(**overrides) -> partition.DiskSnapshot:
    parts = [
        {"partition_number": 1, "size_bytes": 100 * partition.BYTES_PER_MB,
         "gpt_type": "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}"},
        {"partition_number": 2, "size_bytes": 1000 * partition.BYTES_PER_MB,
         "file_system": "NTFS", "drive_letter": "C", "is_system": True},
        {"partition_number": 3, "size_bytes": 1000 * partition.BYTES_PER_MB,
         "file_system": "NTFS", "drive_letter": "D"},
    ]
    data = {"disk_number": 1, "size_bytes": 4000 * partition.BYTES_PER_MB, "partitions": parts}
    data.update(overrides)
    return partition.DiskSnapshot.from_summary(data)


def test_validate_blocks_protected_partitions() -> None:
    disk = _disk()
    esp = partition.Action(kind="format", partition_number=1)
    system = partition.Action(kind="format", partition_number=2)
    assert "EFI 系统分区" in partition.validate([esp], disk)[0]
    assert "系统分区" in partition.validate([system], disk)[0]


def test_validate_accepts_plain_data_partition() -> None:
    disk = _disk()
    ok = partition.Action(kind="format", partition_number=3, file_system="exFAT")
    assert partition.validate([ok], disk) == []


def test_validate_checks_space_and_letters() -> None:
    disk = _disk()
    too_big = partition.Action(kind="create", size_mb=10_000)
    assert any("未分配" in item for item in partition.validate([too_big], disk))

    clash = partition.Action(kind="create", size_mb=100, letter="D")
    assert any("已被占用" in item for item in partition.validate([clash], disk))

    twice = [
        partition.Action(kind="create", size_mb=100, letter="Q"),
        partition.Action(kind="create", size_mb=100, letter="Q"),
    ]
    assert any("已被占用" in item for item in partition.validate(twice, disk))


def test_build_script_only_uses_validated_values() -> None:
    actions = partition.parse_actions(
        [
            {"kind": "create", "size_mb": 2048, "file_system": "NTFS", "letter": "q", "label": "数据"},
            {"kind": "assign", "partition_number": 3, "letter": "R"},
        ]
    )
    lines = partition.build_script(2, actions)
    assert lines[0].startswith("REM ")
    assert "select disk 2" in lines
    assert "create partition primary size=2048" in lines
    assert "format quick fs=ntfs label=\"数据\"" in lines
    assert "assign letter=Q" in lines
    assert "select partition 3" in lines
    assert "assign letter=R" in lines
    assert lines[-1] == "exit"


def test_partition_runner_dry_run_never_shells_out(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = partition.PartitionRunner(diskpart=None, which=lambda _name: None)
    monkeypatch.setattr(runner, "_scan", lambda number: _disk().__dict__ | {"disk_number": number})

    def explode(*args, **kwargs):
        raise AssertionError("演练模式不该调用 diskpart")

    monkeypatch.setattr(runner, "_run_diskpart", explode)
    result = runner.run(1, [{"kind": "create", "size_mb": 512, "letter": "Q"}], dry_run=True)
    assert result["dry_run"] is True
    assert result["exit"] == 0
    assert result["log_path"] == ""
    assert "演练模式" in result["message"]


def test_partition_runner_real_mode_needs_gate() -> None:
    runner = partition.PartitionRunner(diskpart="diskpart.exe", which=lambda _name: "diskpart.exe")
    with pytest.raises(partition.PartitionError, match="写盘闸门"):
        runner.run(1, [{"kind": "create", "size_mb": 512}], dry_run=False)


# --------------------------------------------------------------------- 引导
def _snapshot(*partitions_per_disk) -> dict:
    return {
        "disks": [
            {"disk_number": index, "model": f"Disk {index}", "partitions": list(parts)}
            for index, parts in enumerate(partitions_per_disk)
        ],
        "firmware_entries": 2,
    }


def test_analyze_without_disks() -> None:
    report = boot.analyze({"disks": []})
    assert [item["code"] for item in report["findings"]] == ["no_disk"]
    assert report["targets"] == []


def test_analyze_detects_missing_esp() -> None:
    report = boot.analyze(_snapshot([{"partition_number": 2, "drive_letter": "C", "has_windows": True}]))
    codes = [item["code"] for item in report["findings"]]
    assert "no_esp" in codes
    assert report["targets"][0]["repairable"] is False


def test_analyze_healthy_disk_is_reported_ok() -> None:
    esp = {"partition_number": 1, "gpt_type": boot.ESP_GPT_TYPE, "drive_letter": "S", "has_bootmgr": True}
    win = {"partition_number": 2, "drive_letter": "C", "has_windows": True}
    report = boot.analyze(_snapshot([esp, win]))
    target = report["targets"][0]
    assert target["boot_ok"] is True
    assert target["repairable"] is True
    assert target["esp_partition"] == 1
    assert [item["code"] for item in report["findings"]] == ["boot_ok"]


def test_analyze_flags_orphan_esp_when_windows_gone() -> None:
    esp = {"partition_number": 1, "gpt_type": boot.ESP_GPT_TYPE, "drive_letter": "S"}
    report = boot.analyze(_snapshot([esp]))
    assert "no_windows" in [item["code"] for item in report["findings"]]


def test_pick_letter_skips_used_letters() -> None:
    snapshot = {"disks": [{"partitions": [{"drive_letter": "Z"}, {"drive_letter": "Y"}]}]}
    letter = boot.pick_letter(snapshot)
    assert letter not in {"Z", "Y", "A", "B", "C"}


def test_boot_build_script_assign_and_release() -> None:
    assign = boot.build_script(1, 1, "S", release=False)
    release = boot.build_script(1, 1, "S", release=True)
    assert assign == ["select disk 1", "select partition 1", "assign letter=S", "exit"]
    assert release[2] == "remove letter=S"


def test_boot_repair_refuses_disk_without_windows() -> None:
    repairer = boot.BootRepairer(scanner=lambda: _snapshot([{"partition_number": 1, "drive_letter": "C"}]))
    with pytest.raises(boot.BootError, match="没有找到 Windows 目录"):
        repairer.repair(0)


# --------------------------------------------------------------------- 镜像
def test_list_directory_errors(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(image.ImageError, match="尚未设置镜像目录"):
        image.list_directory("")
    with pytest.raises(image.ImageError, match="镜像目录不存在"):
        image.list_directory(tmp_path / "missing")
    assert image.list_directory(empty)["images"] == []
    assert image.list_directory(empty)["exists"] is True


def test_list_directory_filters_and_sorts(tmp_path: Path) -> None:
    for name in ("b.wim", "a.iso", "note.txt", "c.esd"):
        (tmp_path / name).write_bytes(b"x" * 8)
    listing = image.list_directory(tmp_path)
    names = [item["name"] for item in listing["images"]]
    assert names == ["a.iso", "b.wim", "c.esd"]
    assert listing["images"][1]["kind"] == "wim"
    assert listing["images"][0]["kind"] == "iso"


def test_parse_wim_info_reads_indexes() -> None:
    text = """
Deployment Image Servicing and Management tool

Details for image : D:\\sisp-test\\test.wim

Index : 1
Name : Windows 11 Pro
Description : Windows 11 Pro
Size : 4,921,234,567 bytes
Edition : Professional
Architecture : x64
Version : 10.0.22631

Index : 2
Name : Windows 11 Home
Size : 4,900,000,000 bytes
"""
    indexes = image.parse_wim_info(text)
    assert [item["index"] for item in indexes] == [1, 2]
    assert indexes[0]["name"] == "Windows 11 Pro"
    assert indexes[0]["edition"] == "Professional"
    assert indexes[1]["description"] == ""


def test_image_probe_reads_indexes_with_injected_dism(tmp_path: Path) -> None:
    target = tmp_path / "install.wim"
    target.write_bytes(b"wim")
    calls: list[list[str]] = []

    def fake_run(command: list[str], timeout: float) -> tuple[int, str]:
        calls.append(command)
        return 0, "Index : 3\nName : Windows 11 LTSC\n"

    probe = image.ImageProbe(dism="dism.exe", run=fake_run)
    result = probe.inspect(str(target))
    assert result["kind"] == "wim"
    assert result["indexes"][0]["index"] == 3
    assert "/Get-WimInfo" in calls[0][2]
    assert calls[0][3].endswith("install.wim")


def test_image_probe_reports_dism_failure(tmp_path: Path) -> None:
    target = tmp_path / "broken.wim"
    target.write_bytes(b"nope")
    probe = image.ImageProbe(dism="dism.exe", run=lambda command, timeout: (87, "error"))
    with pytest.raises(image.ImageError, match="dism 退出码 87"):
        probe.inspect(str(target))


def test_image_probe_rejects_unknown_type(tmp_path: Path) -> None:
    target = tmp_path / "image.vhd"
    target.write_bytes(b"x")
    probe = image.ImageProbe(dism="dism.exe", run=lambda command, timeout: (0, ""))
    with pytest.raises(image.ImageError, match="不支持的镜像类型"):
        probe.inspect(str(target))


def test_iso_inspection_always_unmounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "win11.iso"
    target.write_bytes(b"iso")
    mount_root = tmp_path / "mnt"
    (mount_root / "sources").mkdir(parents=True)
    (mount_root / "sources" / "install.wim").write_bytes(b"wim")

    unmounted: list[str] = []
    probe = image.ImageProbe(
        dism="dism.exe",
        run=lambda command, timeout: (0, "Index : 1\nName : Win11\n"),
        mount=lambda path: {"drive": str(mount_root)},
        unmount=unmounted.append,
    )
    result = probe.inspect(str(target))
    assert result["mounted"] is True
    assert result["indexes"][0]["index"] == 1
    assert unmounted == [str(target)]


def test_iso_inspection_unmounts_even_on_error(tmp_path: Path) -> None:
    target = tmp_path / "bad.iso"
    target.write_bytes(b"iso")
    unmounted: list[str] = []
    probe = image.ImageProbe(
        dism="dism.exe",
        mount=lambda path: {},  # 没有拿到盘符
        unmount=unmounted.append,
    )
    with pytest.raises(image.ImageError, match="没有拿到盘符"):
        probe.inspect(str(target))
    assert unmounted == [str(target)]


def test_find_default_image_prefers_wim() -> None:
    images = [
        {"path": "a.iso", "kind": "iso", "suffix": ".iso"},
        {"path": "b.esd", "kind": "wim", "suffix": ".esd"},
        {"path": "c.wim", "kind": "wim", "suffix": ".wim"},
    ]
    assert image.find_default_image(images) == "c.wim"
    assert image.find_default_image([]) == ""


def test_partition_runner_removes_script_and_logs_output(tmp_path, monkeypatch) -> None:
    """diskpart 脚本必须落到 temp 目录后被删掉，输出要进日志。

    以前 ``mkstemp`` 返回的 fd 没关，Windows 上 ``unlink`` 因“文件被占用”失败，
    脚本就永久留在用户的临时目录里（VM 上实测发现的残留文件）。
    """
    import subprocess

    from app.modules.partition import service as partition_service

    seen: dict[str, list[Path]] = {}

    def fake_run(argv, **kwargs):
        script = Path(argv[-1])
        seen["script"] = [script]
        return subprocess.CompletedProcess(argv, 0, b"DiskPart ok", b"")

    monkeypatch.setattr(partition_service.subprocess, "run", fake_run)

    runner = partition_service.PartitionRunner(
        diskpart="diskpart.exe",
        temp_dir=str(tmp_path),
        log_dir=str(tmp_path / "logs"),
    )
    code, output = runner._run_diskpart(["select disk 1", "exit"])

    assert code == 0
    assert "DiskPart ok" in output
    assert seen["script"][0].exists() is False
    assert list(tmp_path.glob("sisp-partition-*.txt")) == []


def test_partition_run_logs_diskpart_output(tmp_path, monkeypatch) -> None:
    """执行日志必须包含 diskpart 的输出，而不只是脚本。

    ``_RunLog`` 是“攒行 + 一次落盘”，所以输出写完必须再 flush 一次；否则排障时
    打开日志只能看到脚本，看不到 diskpart 到底说了什么。
    """
    import subprocess

    from app.modules.partition import service as partition_service

    monkeypatch.setattr(
        partition_service.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"DiskPart ok", b""),
    )
    runner = partition_service.PartitionRunner(
        diskpart="diskpart.exe",
        temp_dir=str(tmp_path),
        log_dir=str(tmp_path / "logs"),
        dry_run=False,
    )
    monkeypatch.setattr(
        runner,
        "_resolve_disk",
        lambda number, identity, dry_run: {
            "disk_number": number,
            "size_bytes": 100 * 1024**3,
            "partitions": [],
        },
    )

    result = runner.run(1, [{"kind": "create", "size_gb": 1, "label": "T"}])

    assert result["exit"] == 0
    log_file = Path(result["log_path"])
    text = log_file.read_text(encoding="utf-8")
    assert "diskpart 脚本" in text
    assert "DiskPart ok" in text
    assert "diskpart 完成" in text
