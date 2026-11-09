"""前端资源与静态检查。

这些测试不启动浏览器，只保证“页面引用的东西都在、脚本能过语法检查、
设置页覆盖了后端所有设置项、纯计算函数行为正确”。真正的渲染效果靠
``build/grab_window.py`` 截图人工核对。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.modules.settings.service import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "app" / "gui_web" / "ui"
JS = UI / "js"
SETTINGS_JS = JS / "view-settings.js"

_FIELD_RE = re.compile(
    r"\{\s*section:\s*'(\w+)',\s*key:\s*'(\w+)',\s*kind:\s*'([\w-]+)'"
)
_LABEL_RE = re.compile(r"^\s{2}(\w+):\s*'", re.MULTILINE)


def _js_files() -> list[Path]:
    return sorted(JS.glob("*.js"))


def test_ui_files_present() -> None:
    assert (UI / "index.html").is_file()
    assert (UI / "css" / "app.css").is_file()
    names = {path.name for path in _js_files()}
    assert {"main.js", "api.js", "state.js", "shell.js", "dom.js", "derive.js"} <= names
    assert {"view-main.js", "view-install.js", "view-settings.js", "dialogs.js"} <= names


def test_index_is_thin_shell() -> None:
    text = (UI / "index.html").read_text(encoding="utf-8")
    assert "<style" not in text
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text)
    assert '<link rel="stylesheet" href="/static/css/app.css">' in text
    assert '<script type="module" src="/static/js/main.js"></script>' in text
    # 三个视图的容器必须在静态 HTML 里存在，脚本才不会满世界 createElement
    for node_id in ("tree", "overview", "map", "gridBody", "wizard", "settings", "logBody"):
        assert f'id="{node_id}"' in text, node_id


def test_engine_guard_loads_before_the_module() -> None:
    """兜底脚本必须排在模块前面，而且必须是普通脚本。

    整个界面只有一个 ES 模块入口，而 IE/旧内核对模块连解析都不解析——那种
    情况下唯一能跑的是 ES5 普通脚本。顺序错了（或者改成 type=module）
    就等于兜底也一起失效，用户还是只能看到一个点不动的窗口。
    """
    text = (UI / "index.html").read_text(encoding="utf-8")
    guard = '<script src="/static/js/engine-guard.js"></script>'
    assert guard in text
    assert text.index(guard) < text.index('/static/js/main.js')
    assert (JS / "engine-guard.js").is_file()


def test_engine_guard_is_plain_script() -> None:
    """es5 only：目标读者是跑不了 ES 模块的老内核，用 ES6 语法就白写了。"""
    text = (JS / "engine-guard.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)  # 去掉块注释再判断
    assert not re.search(r"=>", code), "engine-guard.js 不能使用箭头函数"
    assert not re.search(r"\b(const|let)\b", code), "engine-guard.js 不能用 const/let"
    assert "`" not in code, "engine-guard.js 不能用模板字符串"
    # 它得会触发兜底：既认 IE 的 documentMode，也盯主模块有没有置位
    assert "documentMode" in text
    assert "__sispBooted" in text


def test_main_module_flags_boot_for_the_guard() -> None:
    text = (JS / "main.js").read_text(encoding="utf-8")
    assert "window.__sispBooted = true" in text


def test_index_has_noscript_hint() -> None:
    text = (UI / "index.html").read_text(encoding="utf-8")
    assert "<noscript>" in text
    assert "engine-warn" in text


def test_local_imports_resolve() -> None:
    pattern = re.compile(r"""from\s+['"]\./([\w.-]+\.js)['"]""")
    for path in _js_files():
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            assert (JS / name).is_file(), f"{path.name} 引用了不存在的 {name}"


def test_static_references_resolve() -> None:
    text = (UI / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r"""(?:href|src)="(/static/[^"]+)\"""", text)
    assert refs
    for ref in refs:
        assert (UI / ref[len("/static/"):]).is_file(), ref


def test_css_keeps_hidden_attribute_working() -> None:
    """``.fatal{display:flex}`` 这类声明会盖掉 UA 的 ``[hidden]{display:none}``。

    曾经因此让一块空的 #fatal 遮罩盖住整个界面：DOM 齐全、截图纯背景色。
    """
    css = (UI / "css" / "app.css").read_text(encoding="utf-8")
    assert "[hidden]" in css
    assert "display:none!important" in css.replace(" ", "")


def test_css_is_plain_css() -> None:
    css = (UI / "css" / "app.css").read_text(encoding="utf-8")
    assert css.count("{") == css.count("}"), "app.css 花括号不平衡"
    assert ":root" in css
    # CSS 不支持 // 注释；原型里的注释已改成 /* */
    assert not re.search(r"^\s*//", css, re.MULTILINE)
    for name in re.findall(r"url\(([^)]+)\)", css):
        cleaned = name.strip().strip("'\"")
        if cleaned.startswith("data:"):
            continue
        assert not cleaned.startswith("/"), f"app.css 不该引用绝对路径资源: {cleaned}"


def test_no_legacy_fallback() -> None:
    for path in [*list(_js_files()), UI / "index.html"]:
        text = path.read_text(encoding="utf-8")
        assert "legacy" not in text.lower(), path.name


def test_settings_page_covers_backend_keys() -> None:
    text = SETTINGS_JS.read_text(encoding="utf-8")
    keys = {key for _, key, _ in _FIELD_RE.findall(text)}
    missing = set(DEFAULTS) - keys
    extra = keys - set(DEFAULTS)
    assert not missing, f"设置页缺少后端设置项: {sorted(missing)}"
    assert not extra, f"设置页有后端不认识的设置项: {sorted(extra)}"
    # 每个键都要有中文标签，否则界面上会露出英文键名
    labels = set(_LABEL_RE.findall(text.split("const LABELS")[1].split("};")[0]))
    assert set(DEFAULTS) - labels == set(), f"缺少标签: {sorted(set(DEFAULTS) - labels)}"


def test_every_field_has_section_and_kind() -> None:
    text = SETTINGS_JS.read_text(encoding="utf-8")
    rows = _FIELD_RE.findall(text)
    assert len(rows) == len(DEFAULTS)
    known = {"bool", "enum", "int", "auto-int", "path", "text", "list"}
    for section, key, kind in rows:
        assert section in {"general", "install", "external", "system_files", "safety", "logging"}, key
        assert kind in known, f"{key} 的控件类型 {kind} 不认识"


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_partition_defaults_reserve_space_for_efi() -> None:
    """默认数据分区大小要给 EFI 和对齐留余量，否则一进第 4 步就被后端判容量不足。

    余量不再是固定 1 GB：``reserveGb(efiMb) = EFI + 0.2 GB 对齐开销``。固定 1 GB 会让
    界面上白挂着“剩余 1.0 GB”（用户会问“这 1 GB 花哪去了”），按真实 EFI 算只剩 ~0.3 GB。
    """
    derive = (JS / "derive.js").read_text(encoding="utf-8")
    state = (JS / "state.js").read_text(encoding="utf-8")
    install = (JS / "view-install.js").read_text(encoding="utf-8")
    assert "export function reserveGb" in derive
    assert "export const ALIGN_GB" in derive
    assert "export const DEFAULT_EFI_MB" in derive
    assert "efiMb = 100" in derive
    # 三处都要用同一个口径：默认值、加/删 E、快捷容量与拖动
    assert "reserveGb" in state
    assert "reserveGb" in install
    assert "RESERVE_GB" not in derive + state + install


def test_install_wizard_can_add_extra_partition() -> None:
    """第 2 步的 ➕ / ✕、E 行、data_partitions 载荷都不能被删掉。

    真的点按钮由 ``tools/ui_smoke.mjs`` 负责（它在浏览器里加一次 E、删一次、
    再加回来，并要求第 4 步出现“额外数据分区”）；这里只防静态回退。
    """
    install = (JS / "view-install.js").read_text(encoding="utf-8")
    assert "data-add" in install
    assert "data-remove" in install
    # E 行就叫「数据分区」，且没有文件系统选择器（跟 D 保持一致，固定 NTFS/exFAT）
    assert "额外数据分区" not in install
    assert 'data-fs="extra"' not in install
    assert "数据分区" in install
    assert "partition_plan" in install

    state_js = (JS / "state.js").read_text(encoding="utf-8")
    assert "data_partitions" in state_js
    assert "addExtraPartition" in state_js
    assert "removeExtraPartition" in state_js
    # 加 E 时与 D 平分容量（走 fitLayout 的 even 分支，和快捷容量同一套算法）；
    # 文件系统不再由前端传：后端 partitions.DATA_FS_CHOICES 固定 NTFS。
    assert "even: true" in state_js
    assert "dataFs" not in state_js

    css = (UI / "css" / "app.css").read_text(encoding="utf-8")
    assert ".addpart" in css
    assert ".szrow.extra" in css


def test_quick_size_presets_rebalance_extra_partitions(tmp_path: Path) -> None:
    """快捷容量按钮（100/120/150/200）必须把 E 一起重算，而且剩余容量要平分。

    复现过的两个坏状态（477 GB 的盘）：

    1. C=200、D=275.9、E=71.7，相加 547.6 GB——D 拿走了「除 C 以外的全部容量」，
       E 还是旧值；
    2. 只修第 1 个还不够：用户的预期是“系统 200，剩下的 300 由 D / E 一人一半”，
       而不是“E 保持原值、剩下全给 D”。

    现在快捷容量走 ``derive.fitLayout(..., { even: true })``：零头（< 0.1 GB）留作
    未分配，D 与 E 的数字**完全相等**。
    """
    install = (JS / "view-install.js").read_text(encoding="utf-8")
    handler = re.search(r"\$\$\('#wzBody \.presets button'\).*?\n  \}\);", install, re.S)
    assert handler, "view-install.js 里找不到快捷容量按钮的处理逻辑"
    body = handler.group(0)
    assert "fitLayout" in body, "快捷容量按钮没有走 fitLayout，E 不会跟着重算"
    assert "even: true" in body, "快捷容量按钮没有要求 D/E 平分，剩余会全给 D"
    # 老写法：dataGb 直接等于 totalGb - sysGb - RESERVE_GB，把 E 的份额也吞了
    assert "totalGb - target.sysGb - RESERVE_GB" not in body

    derive = (JS / "derive.js").read_text(encoding="utf-8")
    script = tmp_path / "fit_layout_check.mjs"
    script.write_text(
        "import { fitLayout } from './derive_copy.mjs';\n"
        "const cases = [[476.9, 200, [71.7]], [477, 60, [420]], [465.7, 200, [177.9]], [476.9, 120, []]];\n"
        "const out = cases.map(([total, sys, extras]) => {\n"
        "  const fit = fitLayout(total, sys, extras, { even: true });\n"
        "  return [fit.sysGb, fit.dataGb, fit.extrasGb];\n"
        "});\n"
        "console.log(JSON.stringify(out));\n",
        encoding="utf-8",
    )
    (tmp_path / "derive_copy.mjs").write_text(derive, encoding="utf-8")
    proc = subprocess.run(
        [shutil.which("node"), str(script)],
        cwd=str(tmp_path), capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got == [
        [200, 138.3, [138.3]],
        [60, 208.3, [208.3]],
        [200, 132.7, [132.7]],
        [120, 356.6, []],
    ], got


def test_extra_partition_limit_matches_backend() -> None:
    """前端最多 N 个额外分区必须等于后端布局允许的个数，否则会被后端打回。"""
    from app.application.partitions import MAX_DATA_PARTITIONS

    derive = (JS / "derive.js").read_text(encoding="utf-8")
    match = re.search(r"export const MAX_EXTRAS = (\d+);", derive)
    assert match, "derive.js 里找不到 MAX_EXTRAS"
    assert int(match.group(1)) == MAX_DATA_PARTITIONS - 1


def test_frontend_static_check() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "ui_check.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_removing_extra_partition_gives_space_back_to_data(tmp_path: Path) -> None:
    """删掉 E 之后，容量要还给 D，退回「C 指定大小 + D 吃掉剩余」的两分区状态。

    复现过的坏状态：477 GB 的盘上 C=150、加了 E 之后 D=163/E=163，再把 E 删掉，
    D 还停在 163，剩下 164 GB 白变成“未分配”——用户只是想撤销“我要加 E”这件事。
    """
    for name in ("derive.js", "state.js"):
        (tmp_path / name.replace(".js", "_copy.mjs")).write_text(
            (JS / name).read_text(encoding="utf-8").replace("'./derive.js'", "'./derive_copy.mjs'"),
            encoding="utf-8",
        )
    script = tmp_path / "remove_extra_check.mjs"
    script.write_text(
        "import { state, resetWizard, addTarget, addExtraPartition, removeExtraPartition, targetOf }\n"
        "  from './state_copy.mjs';\n"
        "const GB = 1024 ** 3;\n"
        "state.disks = [{ disk_number: 1, size_bytes: Math.round(477 * GB), model: 'X',\n"
        "  partition_style: 'GPT', partitions: [] }];\n"
        "state.settings = { sysDefaultGb: 150, dataDefaultGb: 'auto' };\n"
        "resetWizard();\n"
        "addTarget(1);\n"
        "const target = targetOf(1);\n"
        "const before = { sys: target.sysGb, data: target.dataGb };\n"
        "addExtraPartition(1);\n"
        "const withExtra = { sys: target.sysGb, data: target.dataGb, extra: target.extras[0].sizeGb };\n"
        "removeExtraPartition(1, 0);\n"
        "const after = { sys: target.sysGb, data: target.dataGb, extras: target.extras.length };\n"
        "const spare = 477 - after.sys - after.data;\n"
        "console.log(JSON.stringify({ before, withExtra, after, spare: +spare.toFixed(1) }));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [shutil.which("node"), str(script)],
        cwd=str(tmp_path), capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    # 加 E：与 D 平分（163.3 / 163.3）
    assert got["withExtra"]["data"] == got["withExtra"]["extra"] == 163.3
    # 删 E：D 吃掉全部剩余容量，只剩 EFI + 对齐的余量（约 0.3 GB）
    assert got["after"]["extras"] == 0
    assert got["after"]["data"] >= 326.0, got
    assert got["spare"] <= 0.5, got


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_manual_sys_size_moves_the_auto_partition(tmp_path: Path) -> None:
    """手动改 C 时，最后一个数据分区（auto）的预估必须跟着变。

    回归：改成“永远 auto”之后，输入框里的 D 还是旧值，C+D 加起来不等于盘容量、
    进度条右侧还空一块（用户实测 C 116 + D 276.6 != 477）。
    """
    for name in ("derive.js", "state.js"):
        (tmp_path / name.replace(".js", "_copy.mjs")).write_text(
            (JS / name).read_text(encoding="utf-8").replace("'./derive.js'", "'./derive_copy.mjs'"),
            encoding="utf-8",
        )
    script = tmp_path / "sync_auto.mjs"
    script.write_text(
        chr(10).join([
            "import { state, resetWizard, addTarget, selectionPayload, syncAutoPartition,",
            "  targetOf, reserveGb, autoPartitionBytes } from './state_copy.mjs';",
            "const GB = 1024 ** 3;",
            "state.disks = [{ disk_number: 1, size_bytes: Math.round(477 * GB), model: 'X',",
            "  partition_style: 'GPT', partitions: [] }];",
            "state.settings = { sysDefaultGb: 200, dataDefaultGb: 'auto',",
            "  efiDefaultMb: 100 };",
            "resetWizard();",
            "addTarget(1);",
            "const target = targetOf(1);",
            "const before = target.dataGb;",
            "target.sysGb = 116;            // 用户手填 C",
            "syncAutoPartition(target);",
            "const after = target.dataGb;",
            "const payload = selectionPayload()['1'];",
            "console.log(JSON.stringify({ before, after, sys: target.sysGb,",
            "  reserve: reserveGb(), payload,",
            "  autoGb: autoPartitionBytes(target) / GB }));",
        ]) + chr(10),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [shutil.which("node"), str(script)],
        cwd=str(tmp_path), capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    # 477 GB 盘、C 116 -> D 应该重算成 360.7（477 - 116 - 0.3）
    assert got["before"] != got["after"], "改 C 之后 D 没有跟着动"
    assert abs(got["after"] - 360.7) < 0.2, got["after"]
    # C + D + 预留余量 = 整块盘（不多不少）
    total = got["sys"] + got["after"] + got["reserve"]
    assert abs(total - 477) < 0.25, total
    # 输入框里的数字与“剩余空间”同一个值
    assert abs(got["autoGb"] - got["after"]) < 0.05
    # 发给后端的仍然是 auto
    assert got["payload"]["data_size"] == "auto"


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_size_error_uses_configured_efi_size(tmp_path: Path) -> None:
    """前端容量判定必须用“真实 EFI 大小”，不能永远按标的 100 MB 估。

    回归：`targetError()` 调 `checkLayout()` 时漏传 `efiMb`，于是用默认 100 MB，
    而分配 / 预留给 auto 分区的是设置里的真实值（例：300 MB）。100 GB 盘、
    C=79.8 GB 时前端判 OK、后端 `check_minimums` 拒绝——用户看到的是“下一步能点，
    生成计划却报错”。
    """
    for name in ("derive.js", "state.js"):
        (tmp_path / name.replace(".js", "_copy.mjs")).write_text(
            (JS / name).read_text(encoding="utf-8").replace("'./derive.js'", "'./derive_copy.mjs'"),
            encoding="utf-8",
        )
    script = tmp_path / "size_error.mjs"
    script.write_text(
        chr(10).join([
            "import { state, resetWizard, addTarget, targetOf, targetError } from './state_copy.mjs';",
            "const GB = 1024 ** 3;",
            "state.disks = [{ disk_number: 1, size_bytes: 100 * GB, model: 'X',",
            "  partition_style: 'GPT', partitions: [] }];",
            "state.settings = { sysDefaultGb: 60, dataDefaultGb: 'auto',",
            "  efiDefaultMb: 300 };",
            "resetWizard();",
            "addTarget(1);",
            "const target = targetOf(1);",
            "const out = {};",
            "for (const c of [79.6, 79.8]) { target.sysGb = c; out[c] = targetError(target); }",
            "console.log(JSON.stringify(out));",
        ]) + chr(10),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [shutil.which("node"), str(script)],
        cwd=str(tmp_path), capture_output=True, text=True, check=False,
        # 输出里带中文（容量不足的提示），Windows 默认按 gbk 解码会直接抛
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, f"{proc.stdout}{proc.stderr}"
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    # 79.6 GB：剩 20.1 GB，够 auto 分区的下限（后端同口径也放行）
    assert got["79.6"] == "", got
    # 79.8 GB：剩 19.9 GB，不够 20 GB（旧实现漏算 EFI 会误判成 OK）
    assert "至少要 20 GB" in got["79.8"], got


def test_last_data_partition_is_always_sent_as_auto(tmp_path: Path) -> None:
    """最后一个数据分区**永远**发 ``"auto"``，由 diskpart 自己吃剩余空间。

    ``create partition primary``（不带 ``size=``）会吃掉全部剩余空间，比界面上算
    「总容量 − C − EFI − 对齐余量」更准，而且容量不足从根上不可能发生。
    代价是界面上的数字只是预估：最后那个分区的输入框是只读的，用户改不了。
    """
    for name in ("derive.js", "state.js"):
        (tmp_path / name.replace(".js", "_copy.mjs")).write_text(
            (JS / name).read_text(encoding="utf-8").replace("'./derive.js'", "'./derive_copy.mjs'"),
            encoding="utf-8",
        )
    script = tmp_path / "selection_payload.mjs"
    script.write_text(
        "import { state, resetWizard, addTarget, addExtraPartition, selectionPayload, targetOf }\n"
        "  from './state_copy.mjs';\n"
        "const GB = 1024 ** 3;\n"
        "state.disks = [{ disk_number: 1, size_bytes: Math.round(477 * GB), model: 'X',\n"
        "  partition_style: 'GPT', partitions: [] }];\n"
        "state.settings = { sysDefaultGb: 120, dataDefaultGb: 'auto' };\n"
        "resetWizard();\n"
        "addTarget(1);\n"
        "const filled = selectionPayload()['1'];\n"
        "addExtraPartition(1);\n"
        "const withExtra = selectionPayload()['1'];\n"
        "const target = targetOf(1);\n"
        "target.dataGb = 100;   // 用户故意只给 D 100 GB，留一大块空盘\n"
        "target.extras.length = 0;\n"
        "const shrunk = selectionPayload()['1'];\n"
        "console.log(JSON.stringify({ filled, withExtra, shrunk }));\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [shutil.which("node"), str(script)],
        cwd=str(tmp_path), capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    # 默认两分区：D 发 auto（盘本来就是填满的）
    assert got["filled"]["data_size"] == "auto"
    assert "data_partitions" not in got["filled"]
    # 加了 E：D 显式、最后一份（E）发 auto
    parts = got["withExtra"]["data_partitions"]
    assert len(parts) == 2
    assert parts[0]["size_gb"] == 178.3
    assert parts[-1]["size_gb"] == "auto"
    # 载荷不再携带 file_system：后端按 DATA_FS_CHOICES（NTFS）归一。
    assert all("file_system" not in entry for entry in parts)
    # 用户把 D 的数字改小也不会改变结果：最后一项永远是 auto（输入框只读）
    assert got["shrunk"]["data_size"] == "auto"
