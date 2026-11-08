"""收尾阶段：附加文件拷贝（纯函数 + 阶段行为）。

守护四件事：

1. **目标口径**：拷到「系统分区之后的第一个数据分区」根目录下的同名文件夹，
   即 `X:\\<文件夹名>\\…`，不是按盘符字母猜；
2. **不删东西**：命令里没有 ``/MIR`` ``/PURGE`` ``/MOV``，护栏还会再拦一次；
3. **属性不丢**：``/COPY:DAT`` + ``/DCOPY:DAT``，拷完按相对路径逐项比对；
4. **失败即整盘失败**：退出码、校验差异、空间不足都要给出可读原因。
"""

from __future__ import annotations

import os
import pathlib
import shutil
import stat
import threading

import pytest

from app.application.command_log import CommandLog
from app.application.partitions import PlannedPartition, plan_partitions
from app.application.phases.base import PhaseContext
from app.application.phases.copy import (
    CopyAssetsPhase,
    compare_trees,
    robocopy_command,
    scan_source,
    target_partition,
)
from app.safety_guard import RealDiskBlocked, ensure_command_allowed
from tests.fakes import FakeProcess

PAYLOAD = {
    "partition_info": {
        "efi_size_mb": 100,
        "c_size_gb": 3,
        "data_size_gb": "auto",
        "allow_small_partitions": True,
    },
    "image_info": {"image_path": "D:/images/test.GHO"},
}
LETTERS = {"efi": "E", "windows": "F", "data": "G"}


def _parts(letters=None):
    return plan_partitions(PAYLOAD, letters=letters if letters is not None else LETTERS)


def _ctx(parts=None, *, cancel: bool = False) -> PhaseContext:
    event = threading.Event()
    if cancel:
        event.set()
    return PhaseContext(
        disk_number=2,
        parts=parts if parts is not None else _parts(),
        letters=LETTERS,
        image_path="D:/images/test.GHO",
        cancel=event,
        log=CommandLog(None),
        options={},
    )


def _source(tmp_path, files=("a.txt", "sub/b.txt")):
    root = tmp_path / "src"
    root.mkdir()
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"content of {name}", encoding="utf-8")
    return root


def _phase(shell, source, target_root, *, robocopy="robocopy.exe"):
    return CopyAssetsPhase(
        shell,
        str(source),
        robocopy=robocopy,
        target_root=str(target_root),
    )


# --------------------------------------------------------------- 纯函数
def test_target_partition_is_first_data_partition() -> None:
    parts = _parts({"efi": "E", "windows": "F", "data": "G", "data2": "H"})
    chosen = target_partition(parts)
    assert chosen is not None
    assert chosen.role == "data"
    assert chosen.letter == "G"


def _no_data_parts():
    """手工构造“只有 EFI + Windows”的分区列表（plan_partitions 总会补一个 data）。"""
    return [
        PlannedPartition(role="efi", label="EFI", file_system="FAT32", size_mb=100, letter="E"),
        PlannedPartition(role="windows", label="Windows", file_system="NTFS", size_mb=3072, letter="F"),
    ]


def test_target_partition_missing_without_data() -> None:
    assert target_partition(_no_data_parts()) is None


def test_robocopy_command_preserves_attributes() -> None:
    command = robocopy_command(r"C:\素材 目录", "G:\\")
    assert command[0] == "robocopy"
    assert command[1] == r"C:\素材 目录"
    assert command[2] == "G:\\"
    for flag in ("/E", "/COPY:DAT", "/DCOPY:DAT", "/XJ", "/R:1", "/W:1"):
        assert flag in command


def test_robocopy_command_never_deletes() -> None:
    command = [part.lower() for part in robocopy_command("C:/a", "G:\\")]
    for forbidden in ("/mir", "/purge", "/mov", "/move", "/del", "/nocopy"):
        assert forbidden not in command


def test_scan_source_counts_files_and_bytes(tmp_path) -> None:
    source = _source(tmp_path)
    stats = scan_source(str(source))
    assert stats.files == 2
    assert stats.dirs == 1
    assert stats.bytes > 0
    assert stats.empty is False


def test_scan_source_empty_directory(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    stats = scan_source(str(empty))
    assert stats == scan_source(str(empty))
    assert stats.empty is True


def test_compare_trees_clean_after_copy(tmp_path) -> None:
    source = _source(tmp_path)
    target = tmp_path / "dst"
    shutil.copytree(source, target)
    assert compare_trees(str(source), str(target)) == []


def test_compare_trees_reports_missing_but_ignores_extra(tmp_path) -> None:
    """只核对“源里的东西都到了”；目标里多出的文件不算失败。
    ``robocopy`` 不带 ``/MIR`` / ``/PURGE``，本来就不负责删目标上的东西，
    而新格式化的 NTFS 分区会自动出现 ``System Volume Information`` 之类目录，
    把它们判成失败会让这一步无故报错。
    """
    source = _source(tmp_path)
    target = tmp_path / "dst"
    shutil.copytree(source, target)
    (target / "a.txt").unlink()
    (target / "extra.txt").write_text("x", encoding="utf-8")
    problems = compare_trees(str(source), str(target))
    assert any("缺少 a.txt" in item for item in problems)
    assert not any("extra.txt" in item for item in problems)


def test_compare_trees_reports_size_difference(tmp_path) -> None:
    source = _source(tmp_path)
    target = tmp_path / "dst"
    shutil.copytree(source, target)
    (target / "a.txt").write_text("much longer content than source", encoding="utf-8")
    problems = compare_trees(str(source), str(target))
    assert any("大小不同" in item for item in problems)


def test_compare_trees_reports_mtime_drift(tmp_path) -> None:
    source = _source(tmp_path)
    target = tmp_path / "dst"
    shutil.copytree(source, target)
    os.utime(target / "a.txt", (0, 0))
    problems = compare_trees(str(source), str(target))
    assert any("修改时间不同" in item for item in problems)


@pytest.mark.skipif(not hasattr(os.stat("."), "st_file_attributes"), reason="仅 Windows 有文件属性位")
def test_compare_trees_detects_lost_hidden_attribute(tmp_path) -> None:
    source = _source(tmp_path)
    target = tmp_path / "dst"
    shutil.copytree(source, target)
    hidden = stat.FILE_ATTRIBUTE_HIDDEN
    for path in (source / "a.txt", target / "a.txt"):
        os.chmod(path, stat.S_IWRITE)
    # 只给源加隐藏位，模拟"拷过去丢了属性"
    attrs = os.stat(source / "a.txt").st_file_attributes | hidden
    os.chmod(source / "a.txt", attrs)
    problems = compare_trees(str(source), str(target))
    assert any("属性不同" in item for item in problems)


# --------------------------------------------------------------- 阶段行为
def test_phase_skipped_without_source(tmp_path) -> None:
    phase = CopyAssetsPhase(FakeProcess(), "")
    outcome = phase.run(_ctx())
    assert outcome.ok is True
    assert outcome.results[0].status == "skipped"
    assert "未设置拷贝目录" in outcome.results[0].message


def test_phase_reports_cancellation(tmp_path) -> None:
    source = _source(tmp_path)
    outcome = _phase(FakeProcess(), source, tmp_path / "dst").run(_ctx(cancel=True))
    assert outcome.cancelled is True
    assert outcome.ok is False
    assert outcome.results[0].status == "skipped"


def test_phase_fails_without_data_partition(tmp_path) -> None:
    source = _source(tmp_path)
    outcome = _phase(FakeProcess(), source, tmp_path / "dst").run(_ctx(_no_data_parts()))
    assert outcome.ok is False
    assert "没有数据分区" in outcome.results[0].message


def test_phase_fails_when_data_partition_has_no_letter(tmp_path) -> None:
    source = _source(tmp_path)
    parts = _parts({"efi": "E", "windows": "F"})
    outcome = _phase(FakeProcess(), source, tmp_path / "dst").run(_ctx(parts))
    assert outcome.ok is False
    assert "没有盘符" in outcome.results[0].message


def test_phase_fails_when_source_missing(tmp_path) -> None:
    missing = tmp_path / "nope"
    outcome = _phase(FakeProcess(), missing, tmp_path / "dst").run(_ctx())
    assert outcome.ok is False
    assert "拷贝目录不可用" in outcome.results[0].message
    assert str(missing) in outcome.results[0].message


def test_phase_fails_when_target_is_inside_source(tmp_path) -> None:
    source = _source(tmp_path)
    outcome = _phase(FakeProcess(), source, source / "dst").run(_ctx())
    assert outcome.ok is False
    assert "重叠" in outcome.results[0].message


def test_phase_skips_empty_source(tmp_path) -> None:
    source = tmp_path / "empty"
    source.mkdir()
    outcome = _phase(FakeProcess(), source, tmp_path / "dst").run(_ctx())
    assert outcome.ok is True
    assert outcome.results[0].status == "skipped"
    assert "源目录为空" in outcome.results[0].message


def test_phase_fails_without_robocopy(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path)
    monkeypatch.setattr(
        "app.application.phases.copy.find_program", lambda name: None
    )
    phase = CopyAssetsPhase(FakeProcess(), str(source), target_root=str(tmp_path / "dst"))
    outcome = phase.run(_ctx())
    assert outcome.ok is False
    assert "robocopy.exe" in outcome.results[0].message


def test_phase_fails_on_free_space(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path)
    monkeypatch.setattr(
        "app.application.phases.copy.CopyAssetsPhase._free_bytes",
        staticmethod(lambda root: 1),
    )
    outcome = _phase(FakeProcess(), source, tmp_path / "dst").run(_ctx())
    assert outcome.ok is False
    assert "空间不足" in outcome.results[0].message


def test_phase_fails_on_error_exit_code(tmp_path) -> None:
    source = _source(tmp_path)
    shell = FakeProcess(handler=lambda command, timeout, env: (8, "ERROR 5 access denied", 1.2))
    outcome = _phase(shell, source, tmp_path / "dst").run(_ctx())
    assert outcome.ok is False
    assert "退出码 8" in outcome.results[0].message


def test_phase_copies_and_verifies(tmp_path) -> None:
    source = _source(tmp_path)          # <tmp>/src，内含 a.txt 与 sub/b.txt
    target = tmp_path / "dst"
    target.mkdir()
    dest = target / "src"               # 目标根下出现同名文件夹

    def handler(command, timeout, env):
        # 模拟 robocopy /E：把源内容搬到命令行给的目标目录
        shutil.copytree(source, command[2], dirs_exist_ok=True)
        return 1, "Files : 2", 0.4

    shell = FakeProcess(handler=handler)
    outcome = _phase(shell, source, target).run(_ctx())

    assert outcome.ok is True
    result = outcome.results[0]
    assert result.status == "ok"
    assert "已拷贝 2 个文件" in result.message
    assert str(dest) in result.message
    assert shell.calls[0][1] == str(source)
    assert shell.calls[0][2] == str(dest)
    assert (dest / "sub" / "b.txt").is_file()


def test_phase_keeps_source_folder_name(tmp_path) -> None:
    """整个文件夹连名字一起拷：目标里是 ``X:\\src\\a.txt``，不是 ``X:\\a.txt``。"""
    source = _source(tmp_path)
    phase = _phase(FakeProcess(), source, tmp_path / "dst")
    assert phase.destination("G") == str(tmp_path / "dst" / "src")
    assert phase.command("G")[2] == str(tmp_path / "dst" / "src")


def test_phase_rejects_drive_root_source(tmp_path) -> None:
    phase = CopyAssetsPhase(FakeProcess(), "C:/", robocopy="robocopy.exe",
                            target_root=str(tmp_path / "dst"))
    outcome = phase.run(_ctx())
    assert outcome.ok is False
    assert "驱动器根目录" in outcome.results[0].message


def test_phase_fails_when_verification_finds_differences(tmp_path) -> None:
    source = _source(tmp_path)
    target = tmp_path / "dst"
    target.mkdir()

    def handler(command, timeout, env):
        # 只拷一个文件：退出码是"成功"，但校验应该抓到缺文件
        dest = pathlib.Path(command[2])
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / "a.txt", dest / "a.txt")
        return 2, "Files : 1", 0.3

    shell = FakeProcess(handler=handler)
    outcome = _phase(shell, source, target).run(_ctx())
    assert outcome.ok is False
    assert "校验未通过" in outcome.results[0].message


# --------------------------------------------------------------- 护栏
def test_guard_blocks_robocopy_mirror() -> None:
    with pytest.raises(RealDiskBlocked):
        ensure_command_allowed(["robocopy", "C:/a", "G:\\", "/MIR"])


def test_guard_blocks_robocopy_purge_and_move() -> None:
    for flag in ("/PURGE", "/MOV", "/MOVE", "/DEL", "/NOCOPY"):
        with pytest.raises(RealDiskBlocked):
            ensure_command_allowed(["robocopy", "C:/a", "G:\\", "/E", flag])


def test_guard_allows_plain_robocopy_copy() -> None:
    ensure_command_allowed(
        ["robocopy", "C:/a", "G:\\", "/E", "/COPY:DAT", "/DCOPY:DAT"],
        allow_real_disk=True,
    )
