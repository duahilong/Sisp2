"""打包配置的静态检查（不打真的包，几秒钟）。

打包脚本 ``packaging/build.py`` 依赖三样东西，任何一样对不上都会在用户那里
才炸，所以这里先把它们钉住：

1. ``app/version.py`` 与 ``packaging/version_info.txt`` 的版本号一致；
2. spec 里声明的资源（界面文件、默认配置、图标）真实存在；
3. 图标生成器能产出结构合法的 256×256 ICO。
"""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

from app import resources
from app.version import __version__

# Windows 版本资源用四段数字；这里按 ``__version__`` 现场算，
# 不再让运行时代码保留一个只给测试看的常量。
VERSION_TUPLE = tuple(int(part) for part in __version__.split(".")) + (0,) * (
    4 - len(__version__.split("."))
)

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"


def test_version_info_matches_app_version() -> None:
    text = (PACKAGING / "version_info.txt").read_text(encoding="utf-8")
    filevers = re.search(r"filevers=\(([^)]*)\)", text)
    prodvers = re.search(r"prodvers=\(([^)]*)\)", text)
    assert filevers and prodvers

    def parse(raw: str) -> tuple[int, ...]:
        return tuple(int(part.strip()) for part in raw.split(","))

    assert parse(filevers.group(1)) == VERSION_TUPLE
    assert parse(prodvers.group(1)) == VERSION_TUPLE
    assert f"StringStruct('FileVersion', '{__version__}.0')" in text
    assert f"StringStruct('ProductVersion', '{__version__}.0')" in text


def test_spec_declares_single_gui_executable() -> None:
    """命令行入口已移除，只产出一个图形界面 exe。"""
    text = (PACKAGING / "Sisp2.spec").read_text(encoding="utf-8")
    assert 'name="Sisp2"' in text
    assert "Sisp2-CLI" not in text
    assert "app.cli" not in text
    # 图形界面必须是无控制台窗口
    assert re.search(r'name="Sisp2",(.|\n)*?console=False', text)
    # pywebview 的 Windows 后端是惰性导入，必须显式声明
    assert "webview.platforms.winforms" in text
    assert "webview.platforms.edgechromium" in text


def test_spec_resources_exist() -> None:
    assert resources.ui_path().is_file(), "界面文件缺失，打出来的 exe 会打不开窗口"
    # 业务配置那一层已移除：仓库里不该再有 json/ 目录，也不该有任何一份配置模板
    assert not (ROOT / "json").exists(), "json/ 目录应当已经删除"
    assert (PACKAGING / "sisp2.ico").is_file(), "图标缺失，先跑 packaging/make_icon.py"
    assert (PACKAGING / "version_info.txt").is_file()


def test_spec_bundles_static_assets() -> None:
    """界面拆成 ES 模块之后，css/js 必须一起进包。

    只打 `index.html` 的旧 spec 会让 exe 打开一个没有样式、也不执行任何脚本的空壳，
    而源码模式一切正常——这种差异只有打了包才看得出来。
    """
    text = (PACKAGING / "Sisp2.spec").read_text(encoding="utf-8")
    assert '"css"' in text, "spec 没有把 ui/css 打进包"
    assert '"js"' in text, "spec 没有把 ui/js 打进包"

    ui = resources.ui_path().parent
    assert (ui / "css" / "app.css").is_file()
    assert (ui / "js" / "main.js").is_file()
    html = resources.ui_path().read_text(encoding="utf-8")
    refs = re.findall(r'(?:href|src)="(/static/[^"]+)"', html)
    assert refs, "index.html 没有引用任何 /static 资源"
    for ref in refs:
        assert (ui / ref[len("/static/"):]).is_file(), ref


def test_build_script_targets_match_spec() -> None:
    text = (PACKAGING / "build.py").read_text(encoding="utf-8")
    assert "Sisp2.exe" in text
    assert "Sisp2-CLI" not in text
    assert "PyInstaller" in text


def test_make_icon_produces_valid_ico(tmp_path: Path) -> None:
    sys.path.insert(0, str(PACKAGING))
    try:
        from make_icon import build_ico
    finally:
        sys.path.pop(0)

    path = build_ico(tmp_path / "icon.ico")
    raw = path.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", raw[:6])
    assert (reserved, kind, count) == (0, 1, 1)

    width, height, _colors, _reserved, planes, bits, size, offset = struct.unpack(
        "<BBBBHHII", raw[6:22]
    )
    assert (width, height) == (0, 0)  # 0 表示 256
    assert (planes, bits) == (1, 32)
    assert offset + size == len(raw)

    bi_size, bi_width, bi_height = struct.unpack("<Iii", raw[offset : offset + 12])
    assert bi_size == 40
    assert (bi_width, bi_height) == (256, 512)  # 高度含 AND 掩码


def test_gui_exe_does_not_require_admin() -> None:
    """窗口 exe 的清单必须是 ``asInvoker``，不能是 ``requireAdministrator``。

    WebView2 的运行时不支持跑在高完整性（High IL）进程里：它需要把
    ``msedgewebview2.exe`` 降到 Medium IL 启动，而这条降权路径要借
    ``explorer.exe`` 代劳（微软 “Develop secure WebView2 apps” 也明确要求把
    WebView2 宿主放在普通权限进程里）。以前写 ``uac_admin=True``，在部分机器上直接
    崩成 ``msedgewebview2 has stopped working`` + ``Error launching CrashSender.exe``。

    需要管理员的写盘改由界面用 UAC 拉起的后端子进程（``--backend``）承担。
    """
    text = (PACKAGING / "Sisp2.spec").read_text(encoding="utf-8")
    gui_block = text[text.index('name="Sisp2"') : text.index('name="Sisp2"') + 600]
    assert "uac_admin=False" in gui_block
    assert "uac_admin=True" not in gui_block


def test_onefile_unpack_dir_does_not_use_temp() -> None:
    """onefile 的解包目录必须固定到 ``%LOCALAPPDATA%``，不要用默认的 ``%TEMP%``。

    ``%TEMP%`` 被组策略/沙箱/杀软管住的机器上，PyInstaller bootloader 会直接弹
    ``Could not create temporary directory!``（``CreateDirectoryW`` 连试 5 次失败），
    用户看到的就是“双击没反应 + 一个错误弹窗”。

    三条硬约束（都由 bootloader 的 ``_pyi_create_runtime_tmpdir()`` 决定）：

    1. 必须能用 ``ExpandEnvironmentStringsW`` 展开（所以写 ``%LOCALAPPDATA%`` 这类变量）；
    2. 不能写相对路径——``_wfullpath()`` 按**当前工作目录**解析，双击与快捷方式结果不同；
    3. 不能指到 exe 同目录——exe 装在 ``C:/Program Files`` 之类只读位置时就解不开了。
    """
    text = (PACKAGING / "Sisp2.spec").read_text(encoding="utf-8")
    match = re.search(r'^    runtime_tmpdir=(.+?),?$', text, re.MULTILINE)
    assert match, "spec 里找不到 runtime_tmpdir"
    value = match.group(1).strip()
    assert value != "None", "runtime_tmpdir 不能是 None（会退回到 %TEMP%）"
    assert "%LOCALAPPDATA%" in value
    assert "%TEMP%" not in value and "%TMP%" not in value
    assert not re.fullmatch(r'"[^%\\]*"', value), "runtime_tmpdir 不能是相对路径"
