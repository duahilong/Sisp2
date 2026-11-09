"""建窗口之前先确认 pywebview 会用 Chromium 内核，而不是 IE11。

## 为什么必须自己查一遍

pywebview 6.x 的 winforms 后端在 **import 时**决定内核：只有「.NET Framework
≥ 4.6.2」且「WebView2 运行时主版本 ≥ 86」同时成立才用 ``edgechromium``，
否则**静默**退回 ``mshtml``（IE11）。退回之后对本程序是致命的：

- 界面只有一个入口 ``<script type="module">``，IE11 完全不支持 ES module，
  也不支持 ``fetch`` —— 整块 JS 一行都不执行；
- 用户看到的是「窗口开了、标题栏和导航都在、磁盘一片空白、状态停在连接中」，
  没有任何报错，看起来像“后端没起来”；
- pywebview 的告警只走 ``stderr``，而打包产物是 ``console=False`` 的窗口程序，
  stderr 被丢进空设备（见 ``Docs/打包与分发.md`` 的“实际故障”一节）。

所以 ``_run_desktop`` 在建窗口之前先查一遍：不满足就弹消息框说明原因并退出，
而不是给用户一个点不动的窗口。

## 判定必须与 pywebview 一致

这里的注册表位置与版本规则是照着 ``webview/platforms/winforms.py::_is_chromium``
抄的，包括两处容易被“修正”成不一致的细节：

1. 64 位系统上 HKLM 那条读的是 ``WOW6432Node`` 下的键（不是 ``SOFTWARE\\Microsoft``）；
2. 版本比较只**比主版本**（``86.0.622.0`` 实际只会用到 ``86``）。

自己改成“更严格”的比较会出现两种坏结果：要么我们把能用的机器拒了
（升级提示反而是错的），要么 pywebview 退回 IE 而我们以为一切正常。
"""

from __future__ import annotations

import struct
import winreg
from collections.abc import Callable
from dataclasses import dataclass

# WebView2 在注册表里的客户端 GUID：正式版 + Beta / Dev / Canary
WEBVIEW2_CLIENTS: tuple[tuple[str, str], ...] = (
    ("{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}", "Microsoft Edge WebView2 Runtime"),
    ("{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}", "Microsoft Edge WebView2 Beta"),
    ("{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}", "Microsoft Edge WebView2 Developer"),
    ("{65C35B14-6C1D-4122-AC46-7148CC9D6497}", "Microsoft Edge WebView2 Canary"),
)

# pywebview 的门槛：主版本 ≥ 86（原文 ``_is_new_version('86.0.622.0', build)``）
MIN_WEBVIEW2_MAJOR = 86

# .NET Framework 4.6.2 的 Release 值（pywebview 同样用这个数判断）
MIN_DOTNET_RELEASE = 394802
DOTNET_SUBKEY = r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"

WEBVIEW2_DOWNLOAD_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

# reader(hive, subkey, value_name) -> 字符串值；读不到返回 None。
# 生产用 :func:`read_registry`，测试注入假实现，避免碰到真实注册表。
Reader = Callable[[int, str, str], "str | None"]


def _is_64bit() -> bool:
    return struct.calcsize("P") == 8


def read_registry(hive: int, subkey: str, name: str) -> str | None:
    """读一个注册表字符串/整数值；键或值不存在时返回 None（不抛异常）。"""
    try:
        with winreg.OpenKey(hive, subkey) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return str(value)


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """``'157.0.4341.42'`` → ``(157, 0, 4341, 42)``；解析不了返回 None。

    每段只取开头连续的数字，所以注册表里偶发的 ``120.0.1-beta`` 这类值也能读，
    而 ``'abc'`` / 空串一律当“读不到”（交给调用方按“没装”处理，不抛异常）。
    """
    if not text:
        return None
    parts: list[int] = []
    for chunk in str(text).strip().split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


def major_version(text: str | None) -> int | None:
    parsed = parse_version(text)
    return parsed[0] if parsed else None


def webview2_subkeys() -> tuple[tuple[int, str, str], ...]:
    """要试的 (hive, subkey, 说明) 组合，顺序与 pywebview 一致。"""
    if _is_64bit():
        hklm_subkey = r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
    else:
        hklm_subkey = r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
    hkcu_subkey = r"SOFTWARE\Microsoft\EdgeUpdate\Clients"

    candidates: list[tuple[int, str, str]] = []
    for guid, label in WEBVIEW2_CLIENTS:
        candidates.append((winreg.HKEY_CURRENT_USER, hkcu_subkey + "\\" + guid, label))
        candidates.append((winreg.HKEY_LOCAL_MACHINE, hklm_subkey + "\\" + guid, label))
    return tuple(candidates)


def find_webview2(read: Reader) -> tuple[str | None, str | None]:
    """返回 ``(最高版本号, 对应的客户端名字)``；没装返回 ``(None, None)``。"""
    best: tuple[int, ...] | None = None
    best_text: str | None = None
    best_label: str | None = None
    for hive, subkey, label in webview2_subkeys():
        text = read(hive, subkey, "pv")
        parsed = parse_version(text)
        if parsed is None:
            continue
        if best is None or parsed > best:
            best, best_text, best_label = parsed, str(text), label
    return best_text, best_label


def read_dotnet_release(read: Reader) -> int | None:
    text = read(winreg.HKEY_LOCAL_MACHINE, DOTNET_SUBKEY, "Release")
    if text is None or not str(text).strip():
        return None
    try:
        return int(str(text).strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class EngineReport:
    """一次内核探测的结果。``problems`` 为空表示 pywebview 会走 Chromium。"""

    engine: str
    webview2_version: str | None
    webview2_client: str | None
    dotnet_release: int | None
    problems: tuple[str, ...]

    @property
    def chromium(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "webview2": self.webview2_version,
            "webview2_client": self.webview2_client,
            "dotnet_release": self.dotnet_release,
            "problems": list(self.problems),
        }

    def summary(self) -> str:
        """一行摘要，写进 ``app.log`` 用。"""
        webview2 = self.webview2_version or "未检测到"
        dotnet = self.dotnet_release if self.dotnet_release is not None else "读不到"
        return f"内核={self.engine} WebView2={webview2} .NET Release={dotnet}"

    def message(self) -> str:
        """给用户看的整段说明（消息框正文，纯文本、不用富文本）。"""
        if self.chromium:
            return "渲染内核检查通过：" + self.summary()

        lines: list[str] = [
            "界面打不开：本机缺少可用的网页渲染内核。",
            "",
            "本程序的界面需要 Chromium 内核（Edge WebView2）。没有它时窗口仍会打开，",
            "但只能显示一个静态骨架：磁盘列表是空的、状态一直停在“连接中”，",
            "点任何按钮都没反应——后端其实是好的，是界面脚本没被执行。",
            "",
            "检测结果：",
        ]
        lines.extend("  · " + item for item in self.problems)
        lines.extend(
            [
                "",
                "解决办法（任选其一）：",
                "  1. 安装 Microsoft Edge WebView2 运行时（选 x64 的 Standalone 安装包）：",
                "     " + WEBVIEW2_DOWNLOAD_URL,
                "     老一点的 Windows 10 若提示装不上，先升级到 .NET Framework 4.8 再装。",
                "  2. 不用窗口模式：命令行运行",
                "     Sisp2.exe --serve --port 8765",
                "     然后用 Edge 或 Chrome 打开 http://127.0.0.1:8765/ 。",
                "     注意不要用 IE 打开，IE 同样跑不起来这个界面。",
                "",
                "详细信息见 %APPDATA%\\Sisp\\logs\\app.log 。",
            ]
        )
        return "\n".join(lines)


def inspect_engine(
    read: Reader | None = None,
    *,
    webview2_path: str | None = None,
) -> EngineReport:
    """探测 pywebview 会用的内核；条件与 ``winforms._is_chromium`` 保持一致。

    ``webview2_path`` 对应 pywebview 的 ``settings['WEBVIEW2_RUNTIME_PATH']``：
    显式指定了运行时目录时 pywebview 直接走 Chromium，不看注册表。
    """
    reader: Reader = read or read_registry
    dotnet = read_dotnet_release(reader)
    version, client = find_webview2(reader)

    if webview2_path:
        return EngineReport("chromium", version, client, dotnet, ())

    problems: list[str] = []

    if dotnet is None:
        problems.append(
            "读不到 .NET Framework 版本（注册表 "
            r"HKLM\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full 没有 Release)，"
            "pywebview 会因此退回 IE 内核"
        )
    elif dotnet < MIN_DOTNET_RELEASE:
        problems.append(
            f".NET Framework 太低：Release {dotnet} < {MIN_DOTNET_RELEASE}（需要 4.6.2 或更高）"
        )

    major = major_version(version)
    if major is None:
        problems.append(
            f"没有检测到 Microsoft Edge WebView2 运行时（需要主版本 ≥ {MIN_WEBVIEW2_MAJOR}）"
        )
    elif major < MIN_WEBVIEW2_MAJOR:
        problems.append(
            f"WebView2 运行时太旧：{version}（需要主版本 ≥ {MIN_WEBVIEW2_MAJOR}，"
            "建议直接装最新版）"
        )

    engine = "mshtml" if problems else "chromium"
    return EngineReport(engine, version, client, dotnet, tuple(problems))
