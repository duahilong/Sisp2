"""原生文件/目录选择框（不依赖 pywebview）。

**为什么单独一个模块**：桌面模式下窗口进程是父进程（pywebview），而 HTTP API
在 UAC 拉起的提权子进程里 —— 子进程的 ``webview.windows`` 永远是空列表，旧的
``window.create_file_dialog`` 只会得到 ``E_NO_WINDOW 窗口尚未创建``，界面上的
表现就是点「浏览…」一声不响、什么都不弹。选择框必须由**出 API 的那个进程**
自己弹，所以这里用 Windows 自带的 ``System.Windows.Forms`` —— 它正是
pywebview 的 edgechromium 后端内部用的同一套对话框，外观与旧行为一致。

本模块只做三件事：拼脚本（:func:`build_script`）、跑脚本、解析输出
（:func:`parse_selection`）。所有 GUI 交互都在 PowerShell 里完成，这里保持
纯函数，方便用假 runner 测取消/异常/中文路径等分支。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

# 用户可能在对话框上停留很久（慢慢找路径），超时给足。
PICK_TIMEOUT = 600.0

#: ``kind`` → WinForms 对话框类型。未知取值一律当 ``file``。
#: ``dir`` 另走现代 shell 对话框（见 :data:`_CSHARP_FOLDER_PICKER`）。
KIND_DIALOGS: dict[str, str] = {
    "dir": "FolderBrowserDialog",
    "file": "OpenFileDialog",
    "save": "SaveFileDialog",
}

DEFAULT_SAVE_NAME = "sisp2-settings.json"

_POWERSHELL_NAMES = ("powershell", "pwsh")

#: 每个脚本都先做的事：出错即停、强制 UTF-8 输出（否则 PS 5.1 按 OEM 码页往
#: 管道里写，中文路径到手就是乱码）、开视觉样式（否则对话框是 Win95 外观）。
_PREAMBLE: tuple[str, ...] = (
    "$ErrorActionPreference = 'Stop'",
    "[Console]::OutputEncoding = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false",
    "Add-Type -AssemblyName System.Windows.Forms | Out-Null",
    "[System.Windows.Forms.Application]::EnableVisualStyles()",
)

#: 目录选择框的现代实现。
#:
#: ``FolderBrowserDialog`` 永远是 Win95 那棵老树：不能粘贴路径、不能直接输入、
#: 起始目录也只能靠树里翻。所以目录走 shell 的 ``IFileOpenDialog`` + ``FOS_PICKFOLDERS``
#: （与资源管理器同一种框，带面包屑和路径栏，能预选起始目录）。这类 COM 接口
#: 没法用 PowerShell 直接实例化，只能在运行时用 ``Add-Type`` 编一小段 C#。
#:
#: 编译失败（受限语言模式、.NET Framework 缺失等）由调用方回退到老树，所以这段
#: 代码偶尔失效不会把功能弄挂。
_CSHARP_FOLDER_PICKER = r"""
using System;
using System.Runtime.InteropServices;

[ComImport, ClassInterface(ClassInterfaceType.None), Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")]
public class FileOpenDialogRCW { }

[ComImport, Guid("42f85136-db7e-439c-85f1-e4075d135fc8"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IFileOpenDialog
{
    [PreserveSig] int Show(IntPtr parent);
    void SetFileTypes(uint cFileTypes, IntPtr rgFilterSpec);
    void SetFileTypeIndex(uint iFileType);
    void GetFileTypeIndex(out uint piFileType);
    void Advise(IntPtr pfde, out uint pdwCookie);
    void Unadvise(uint dwCookie);
    void SetOptions(uint fos);
    void GetOptions(out uint pfos);
    void SetDefaultFolder(IShellItem psi);
    void SetFolder(IShellItem psi);
    void GetFolder(out IShellItem ppsi);
    void GetCurrentSelection(out IShellItem ppsi);
    void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string pszName);
    void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string pszName);
    void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string pszTitle);
    void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string pszText);
    void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string pszLabel);
    void GetResult(out IShellItem ppsi);
    void AddPlace(IShellItem psi, int fdap);
    void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string pszDefaultExtension);
    void Close([MarshalAs(UnmanagedType.Error)] int hr);
    void SetClientGuid(ref Guid guid);
    void ClearClientData();
    void SetFilter(IntPtr pFilter);
    void GetResults(out IntPtr ppenum);
    void GetSelectedItems(out IntPtr ppsai);
}

[ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IShellItem
{
    void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
    void GetParent(out IShellItem ppsi);
    void GetDisplayName(uint sigdnName, [MarshalAs(UnmanagedType.LPWStr)] out string ppszName);
    void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
    void Compare(IShellItem psi, uint hint, out int piOrder);
}

public static class FolderPicker
{
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = true)]
    private static extern int SHCreateItemFromParsingName(
        [MarshalAs(UnmanagedType.LPWStr)] string pszPath,
        IntPtr pbc,
        ref Guid riid,
        out IShellItem ppv);

    public static string Pick(string title, string initial)
    {
        var dlg = (IFileOpenDialog)new FileOpenDialogRCW();
        uint opts;
        dlg.GetOptions(out opts);
        // FOS_PICKFOLDERS = 0x20, FOS_FORCEFILESYSTEM = 0x40
        dlg.SetOptions(opts | 0x20u | 0x40u);
        if (!string.IsNullOrEmpty(title)) dlg.SetTitle(title);
        if (!string.IsNullOrEmpty(initial))
        {
            Guid iid = new Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe");
            IShellItem item;
            if (SHCreateItemFromParsingName(initial, IntPtr.Zero, ref iid, out item) == 0)
            {
                dlg.SetFolder(item);
            }
        }
        if (dlg.Show(IntPtr.Zero) != 0) return null;
        try
        {
            IShellItem res;
            dlg.GetResult(out res);
            string path;
            // SIGDN_FILESYSPATH: virtual folders (libraries, network places) have
            // no filesystem path - give up and let the caller fall back.
            res.GetDisplayName(0x80058000u, out path);
            return path;
        }
        catch
        {
            return null;
        }
    }
}
"""

#: runner 契约：``(argv, timeout) -> (returncode, stdout, stderr)``，stdout 可 bytes。
Runner = Callable[[Sequence[str], float], "tuple[int, bytes | str, bytes | str]"]


def _ps_string(value: str) -> str:
    """PowerShell 单引号字符串字面量（单引号自身双写，反斜杠无需转义）。"""
    return "'" + str(value).replace("'", "''") + "'"


def _split_initial(initial: str) -> tuple[str, str]:
    """把“当前值”拆成（起始目录, 文件名）。

    目录框只认目录、文件框只认目录 + 文件名，所以先把当前值拆开；拆不出来就
    什么也不设（让对话框用系统的默认位置）。
    """
    raw = str(initial or "").strip().strip('"').strip()
    if not raw:
        return "", ""
    path = Path(raw)
    try:
        if path.is_dir():
            return str(path), ""
    except OSError:  # 网络路径不可达之类：当作文件名处理
        return "", path.name
    parent = path.parent
    if str(parent) not in ("", "."):
        return str(parent), path.name
    return "", path.name


def _folder_script(title: str, folder: str) -> list[str]:
    """目录框：先试现代 shell 框，编译/运行失败再回退到 Win95 老树。"""
    title_lit = _ps_string(title or "选择文件夹")
    folder_lit = _ps_string(folder)
    return [
        "$chosen = $null",
        "try {",
        "  Add-Type -TypeDefinition @'",
        _CSHARP_FOLDER_PICKER,
        "'@ -Language CSharp | Out-Null",
        f"  $chosen = [FolderPicker]::Pick({title_lit}, {folder_lit})",
        "} catch {",
        "  $dlg = New-Object System.Windows.Forms.FolderBrowserDialog",
        f"  $dlg.Description = {title_lit}",
        "  $dlg.ShowNewFolderButton = $true",
        f"  if ({folder_lit}) {{ $dlg.SelectedPath = {folder_lit} }}",
        (
            "  if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
            "{ $chosen = $dlg.SelectedPath }"
        ),
        "}",
        "if (-not $chosen) { exit 1 }",
        "[Console]::Out.Write($chosen)",
        "exit 0",
    ]


def build_script(
    kind: str = "file",
    *,
    initial: str = "",
    title: str = "",
    save_name: str = "",
) -> str:
    """生成 PowerShell 脚本：弹框 → 选中的路径写到 stdout；取消 exit 1。

    输出编码强制 UTF-8（``[Console]::OutputEncoding``），否则 PS 5.1 会按 OEM
    代码页往管道里写，中文路径到手就是乱码。
    """
    dialog = KIND_DIALOGS.get(str(kind or "").strip().lower(), "OpenFileDialog")
    folder, name = _split_initial(initial)
    if dialog == "FolderBrowserDialog":
        return "\n".join([*_PREAMBLE, *_folder_script(title, folder)])
    lines = [
        *_PREAMBLE,
    ]
    if dialog == "SaveFileDialog":
        lines += [
            "$dlg = New-Object System.Windows.Forms.SaveFileDialog",
            f"$dlg.Title = {_ps_string(title or '保存文件')}",
            "$dlg.Filter = 'JSON (*.json)|*.json|所有文件 (*.*)|*.*'",
            "$dlg.OverwritePrompt = $true",
            f"$dlg.FileName = {_ps_string(save_name or name or DEFAULT_SAVE_NAME)}",
            f"if ({_ps_string(folder)}) {{ $dlg.InitialDirectory = {_ps_string(folder)} }}",
        ]
    else:
        lines += [
            "$dlg = New-Object System.Windows.Forms.OpenFileDialog",
            f"$dlg.Title = {_ps_string(title or '选择文件')}",
            "$dlg.Filter = '所有文件 (*.*)|*.*'",
            "$dlg.CheckFileExists = $false",
            f"if ({_ps_string(name)}) {{ $dlg.FileName = {_ps_string(name)} }}",
            f"if ({_ps_string(folder)}) {{ $dlg.InitialDirectory = {_ps_string(folder)} }}",
        ]
    lines += [
        "$result = $dlg.ShowDialog()",
        "if ($result -ne [System.Windows.Forms.DialogResult]::OK) { exit 1 }",
        "$chosen = $dlg.FileName",
        "if (-not $chosen) { exit 1 }",
        "[Console]::Out.Write($chosen)",
        "exit 0",
    ]
    return "\n".join(lines)


def parse_selection(returncode: int, stdout: bytes | str) -> str | None:
    """把脚本结果变成路径：非 0 退出码或空输出都算“用户取消”返回 ``None``。"""
    if int(returncode or 0) != 0:
        return None
    text = (
        stdout.decode("utf-8", "replace")
        if isinstance(stdout, (bytes, bytearray))
        else str(stdout or "")
    )
    for line in reversed(text.splitlines()):
        chosen = line.strip()
        if chosen:
            return chosen
    return None


def find_powershell() -> str | None:
    """找到可用的 PowerShell；找不到返回 None（调用方负责报错）。"""
    for name in _POWERSHELL_NAMES:
        found = shutil.which(name)
        if found:
            return str(found)
    return None


def _default_runner(argv: Sequence[str], timeout: float) -> tuple[int, bytes, bytes]:
    completed = subprocess.run(  # noqa: S603 - argv 全部来自本模块
        list(argv),
        capture_output=True,
        check=False,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return completed.returncode, completed.stdout, completed.stderr


def pick_path(
    kind: str = "file",
    *,
    initial: str = "",
    title: str = "",
    save_name: str = "",
    runner: Runner | None = None,
    powershell: str | None = None,
) -> str | None:
    """弹原生选择框；返回选中路径，取消返回 ``None``。

    ``runner`` 只为测试存在（默认走 ``subprocess``）；异常向上抛，由 API 层
    包成 ``E_PICK``。
    """
    script = build_script(kind, initial=initial, title=title, save_name=save_name)
    exe = powershell or find_powershell()
    if not exe:
        raise RuntimeError("找不到 powershell.exe，无法弹出选择框")
    code, out, _err = (runner or _default_runner)(
        [exe, "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
        PICK_TIMEOUT,
    )
    return parse_selection(int(code or 0), out)


__all__ = [
    "DEFAULT_SAVE_NAME",
    "KIND_DIALOGS",
    "PICK_TIMEOUT",
    "build_script",
    "find_powershell",
    "parse_selection",
    "pick_path",
]
