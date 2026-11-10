"""原生选择框：脚本拼装、输出解析、取消/异常分支。

这一层不能连真 GUI 测（CI 上没有桌面，对话框会一直挂着），所以全部用假
runner 打桩 —— 真正的“弹得出来”只在真机上验。
"""

from __future__ import annotations

import subprocess

import pytest

from app.gui_web import native_dialog
from app.gui_web.native_dialog import (
    DEFAULT_SAVE_NAME,
    build_script,
    parse_selection,
    pick_path,
)


# ---------------------------------------------------------------- build_script
@pytest.mark.parametrize(
    ("kind", "dialog"),
    [("file", "OpenFileDialog"), ("save", "SaveFileDialog")],
)
def test_build_script_uses_matching_dialog(kind: str, dialog: str) -> None:
    script = build_script(kind)
    assert f"New-Object System.Windows.Forms.{dialog}" in script
    assert "[System.Windows.Forms.DialogResult]::OK" in script
    assert "[Console]::Out.Write($chosen)" in script


def test_build_script_dir_uses_modern_shell_picker() -> None:
    """目录框走 ``IFileOpenDialog`` + FOS_PICKFOLDERS（带路径栏、能预选目录）。"""
    script = build_script("dir")
    assert "Add-Type -TypeDefinition" in script
    assert "-Language CSharp" in script
    assert "FOS_PICKFOLDERS" in script  # 注释里的常量说明
    assert "0x20u | 0x40u" in script  # FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM
    assert "[FolderPicker]::Pick(" in script
    assert "SHCreateItemFromParsingName" in script


def test_build_script_dir_keeps_classic_dialog_as_fallback() -> None:
    """Add-Type 编不出来（受限语言模式等）时回退到 Win95 老树，不能直接失败。"""
    script = build_script("dir")
    assert "} catch {" in script
    assert "New-Object System.Windows.Forms.FolderBrowserDialog" in script
    assert "$dlg.SelectedPath" in script


def test_build_script_defaults_unknown_kind_to_open_dialog() -> None:
    script = build_script("folder???")
    assert "New-Object System.Windows.Forms.OpenFileDialog" in script


def test_build_script_forces_utf8_stdout() -> None:
    """PS 5.1 默认按 OEM 代码页往管道写，中文路径会乱码。"""
    assert "[Console]::OutputEncoding" in build_script("dir")


def test_build_script_folder_prefills_current_value(tmp_path) -> None:
    script = build_script("dir", initial=str(tmp_path))
    # 现代框通过 SetFolder 预选，老树通过 SelectedPath 预选，两条路都要带上
    assert f"[FolderPicker]::Pick('选择文件夹', '{tmp_path}')" in script
    assert f"$dlg.SelectedPath = '{tmp_path}'" in script


def test_build_script_file_splits_initial_into_dir_and_name(tmp_path) -> None:
    target = tmp_path / "win11.GHO"
    script = build_script("file", initial=str(target))
    assert "$dlg.FileName = 'win11.GHO'" in script
    assert f"$dlg.InitialDirectory = '{tmp_path}'" in script


def test_build_script_save_falls_back_to_default_name() -> None:
    assert f"$dlg.FileName = '{DEFAULT_SAVE_NAME}'" in build_script("save")
    assert "sisp2-mine.json" in build_script("save", save_name="sisp2-mine.json")


def test_build_script_escapes_single_quotes() -> None:
    """路径里带单引号不能把 PowerShell 字符串提前闭合。"""
    script = build_script("dir", title="it's fine", initial="D:\\a'b")
    assert "'it''s fine'" in script
    # 初始值按目录/文件拆开，目录部分单独转义后再注入（现代框与老树各一处）
    assert "[FolderPicker]::Pick('it''s fine', 'D:\\')" in script
    assert "$dlg.SelectedPath = 'D:\\'" in script
    assert "it's fine" not in script

    file_script = build_script("file", initial="D:\\a'b\\c'd.gho")
    assert "$dlg.FileName = 'c''d.gho'" in file_script
    assert "$dlg.InitialDirectory = 'D:\\a''b'" in file_script


def test_build_script_neutralises_injection_in_title() -> None:
    """标题里的 ``'`` 被双写，引号跑不出去，后面的命令不会被执行。"""
    title = "x'; Start-Process calc; '"
    script = build_script("dir", title=title)
    assert "'x''; Start-Process calc; '''" in script
    # 未转义的原文一个都不应该落在脚本里（落进去就意味着引号提前闭合了）
    assert title not in script


# ------------------------------------------------------------ _split_initial
def test_split_initial_empty() -> None:
    assert native_dialog._split_initial("") == ("", "")
    assert native_dialog._split_initial("   ") == ("", "")


def test_split_initial_directory(tmp_path) -> None:
    assert native_dialog._split_initial(str(tmp_path)) == (str(tmp_path), "")


def test_split_initial_file(tmp_path) -> None:
    target = tmp_path / "a.gho"
    assert native_dialog._split_initial(str(target)) == (str(tmp_path), "a.gho")


def test_split_initial_strips_quotes() -> None:
    assert native_dialog._split_initial('"D:\\images\\x.gho"')[1] == "x.gho"


# ---------------------------------------------------------- parse_selection
def test_parse_selection_ok_bytes() -> None:
    assert parse_selection(0, b"D:\\images\\Win11.GHO\r\n") == "D:\\images\\Win11.GHO"


def test_parse_selection_ok_utf8_chinese() -> None:
    assert parse_selection(0, "D:\\镜像\\中文.GHO".encode()) == "D:\\镜像\\中文.GHO"


def test_parse_selection_cancelled() -> None:
    assert parse_selection(1, b"") is None
    assert parse_selection(1, b"D:\\x") is None


def test_parse_selection_empty_output() -> None:
    assert parse_selection(0, b"") is None
    assert parse_selection(0, b"\r\n  \r\n") is None


def test_parse_selection_keeps_last_non_empty_line() -> None:
    """脚本偶发先打一行警告时，选中的路径仍然是最后一行。"""
    assert parse_selection(0, "WARNING: something\r\nD:\\picked\r\n") == "D:\\picked"


# ------------------------------------------------------------------ pick_path
class _Runner:
    """记录 argv，按预设返回；用来验证参数与错误传播。"""

    def __init__(self, code: int = 0, out: bytes = b"") -> None:
        self.code = code
        self.out = out
        self.argv: list[str] = []
        self.timeout: float | None = None

    def __call__(self, argv, timeout):
        self.argv = list(argv)
        self.timeout = timeout
        return self.code, self.out, b""


def test_pick_path_returns_selection() -> None:
    runner = _Runner(0, b"D:\\picked")
    assert pick_path("dir", runner=runner, powershell="pwsh") == "D:\\picked"
    assert runner.argv[0] == "pwsh"
    assert "-STA" in runner.argv, "WinForms 对话框必须跑在 STA 线程"
    assert "-NonInteractive" in runner.argv
    assert runner.timeout == native_dialog.PICK_TIMEOUT


def test_pick_path_cancelled_returns_none() -> None:
    assert pick_path("file", runner=_Runner(1, b""), powershell="pwsh") is None


def test_pick_path_passes_current_value_into_script() -> None:
    runner = _Runner(0, b"D:\\out")
    pick_path("save", initial="D:\\cfg\\sisp2.json", runner=runner, powershell="pwsh")
    script = runner.argv[-1]
    assert "$dlg.FileName = 'sisp2.json'" in script
    assert "$dlg.InitialDirectory = 'D:\\cfg'" in script


def test_pick_path_raises_without_powershell(monkeypatch) -> None:
    monkeypatch.setattr(native_dialog, "find_powershell", lambda: None)
    with pytest.raises(RuntimeError, match="powershell"):
        pick_path("dir", runner=_Runner())


def test_pick_path_propagates_timeout() -> None:
    def runner(argv, timeout):
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=timeout)

    with pytest.raises(subprocess.TimeoutExpired):
        pick_path("dir", runner=runner, powershell="pwsh")


# ---------------------------------------------------------------- 自定义过滤器
def test_build_script_save_accepts_custom_filter() -> None:
    """日志导出用 zip 过滤器：默认 JSON 过滤器会把用户误导成 .json。"""
    from app.gui_web.native_dialog import ZIP_FILTER

    script = build_script("save", save_name="Sisp2-日志-1.zip", filter=ZIP_FILTER)
    assert ZIP_FILTER in script
    assert "$dlg.Filter = " in script


def test_pick_path_passes_filter_through() -> None:
    seen: list[list[str]] = []

    def runner(argv, _timeout):
        seen.append(list(argv))
        return 1, b"", b""

    assert pick_path("save", filter="ZIP (*.zip)|*.zip", runner=runner, powershell="pwsh") is None
    joined = "\n".join(seen[0])
    assert "ZIP (*.zip)|*.zip" in joined
