#!/usr/bin/env python
"""前端静态检查：语法、模块引用、资源引用、纯函数行为。

用法::

    python build/ui_check.py

只依赖 ``node``（PATH 里能找到即可）。检查项：

1. 每个 ``ui/js/*.js`` 都能通过 ``node --check``（语法）；
2. ``index.html`` 里所有 ``/static/...`` 引用都指向真实文件；
3. 每个 ``import ... from './x.js'`` 的目标文件存在；
4. ``derive.js`` 的纯函数在给定输入下输出正确（用 node 真跑一遍）。

返回码：0 全部通过，1 有失败项。失败明细打到 stdout。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "app" / "gui_web" / "ui"
JS = UI / "js"

_IMPORT_RE = re.compile(r"""from\s+['"]\./([\w.-]+\.js)['"]""")
_ATTR_RE = re.compile(r"""(?:href|src)="(/static/[^"]+)\"""")

DERIVE_SCRIPT = """
import { fmtSize, fsClass, segments, checkSizes, partShortName, suggestLetter } from './derive.js';

const GB = 1024 ** 3;
const esp = { partition_number: 1, size_bytes: 100 * 1024 * 1024,
  gpt_type: '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}', file_system: 'FAT32', label: 'EFI' };
const win = { partition_number: 2, size_bytes: 120 * GB, file_system: 'NTFS', drive_letter: 'C', label: '系统' };
const dat = { partition_number: 3, size_bytes: 380 * GB, file_system: 'EXFAT', drive_letter: 'D' };
const disk = { size_bytes: 1000 * GB, partitions: [dat, win, esp] };

const segs = segments(disk);
const out = {
  size_tb: fmtSize(2048 * GB),
  size_gb_int: fmtSize(120 * GB),
  size_mb: fmtSize(300 * 1024 * 1024),
  classes: segs.map((seg) => seg.cls),
  ratios_sum: segs.reduce((sum, seg) => sum + seg.ratio, 0),
  free_bytes: segs.find((seg) => seg.free).sizeBytes,
  ordered: segs.filter((seg) => !seg.free).map((seg) => seg.part.partition_number),
  esp_cls: fsClass(esp),
  short: partShortName(dat),
  letter: suggestLetter([{ partitions: [{ drive_letter: 'Z' }, { drive_letter: 'D' }] }]),
  ok_sizes: checkSizes(1000 * GB, 120, 380),
  small_sys: checkSizes(1000 * GB, 20, 100) !== '',
  too_big: checkSizes(100 * GB, 120, 380) !== '',
  efi_reserved: checkSizes(100 * GB, 60, 39.5, 1024) !== '',
  efi_fits: checkSizes(100 * GB, 60, 39, 300),
};
console.log(JSON.stringify(out));
"""

EXPECTED = {
    "size_tb": "2.00 TB",
    "size_gb_int": "120 GB",
    "size_mb": "300 MB",
    "classes": ["ESP", "NTFS", "exFAT", "FREE"],
    "esp_cls": "ESP",
    "short": "D: exFAT",
    "letter": "Y",
    "ok_sizes": "",
    "small_sys": True,
    "too_big": True,
    "efi_reserved": True,
    "efi_fits": "",
}


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603 - 固定的 node 可执行文件 + 本仓库内路径
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check_syntax(node: str) -> list[str]:
    failures: list[str] = []
    for path in sorted(JS.glob("*.js")):
        code, output = _run([node, "--check", str(path)], ROOT)
        if code != 0:
            failures.append(f"语法错误 {path.name}: {output.strip()[:400]}")
    return failures


def check_imports() -> list[str]:
    failures: list[str] = []
    for path in sorted(JS.glob("*.js")):
        for name in _IMPORT_RE.findall(path.read_text(encoding="utf-8")):
            if not (JS / name).is_file():
                failures.append(f"{path.name} 引用了不存在的模块: {name}")
    return failures


def check_html_assets() -> list[str]:
    index = UI / "index.html"
    if not index.is_file():
        return ["缺少 app/gui_web/ui/index.html"]
    text = index.read_text(encoding="utf-8")
    failures = [
        f"index.html 引用了不存在的资源: {ref}"
        for ref in _ATTR_RE.findall(text)
        if not (UI / ref[len("/static/"):]).is_file()
    ]
    if "<style" in text:
        failures.append("index.html 里还有内联 <style>（样式应全部在 css/app.css）")
    if re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text):
        failures.append("index.html 里还有内联 <script>（脚本应全部在 js/*.js）")
    if "/static/css/app.css" not in text or "/static/js/main.js" not in text:
        failures.append("index.html 没有引用 /static/css/app.css 与 /static/js/main.js")
    return failures


def check_derive(node: str, tmp: Path) -> list[str]:
    script = tmp / "ui_check_derive.mjs"
    script.write_text(DERIVE_SCRIPT, encoding="utf-8")
    target = script.with_name("derive_copy.mjs")
    target.write_text((JS / "derive.js").read_text(encoding="utf-8"), encoding="utf-8")
    script.write_text(DERIVE_SCRIPT.replace("./derive.js", "./derive_copy.mjs"), encoding="utf-8")
    code, output = _run([node, str(script)], tmp)
    if code != 0:
        return [f"derive.js 运行失败: {output.strip()[:600]}"]
    try:
        data = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [f"derive.js 没有输出可解析的 JSON: {output.strip()[:300]}"]

    failures: list[str] = []
    for key, expected in EXPECTED.items():
        actual = data.get(key)
        if key == "ratios_sum":
            if abs(float(actual) - 1.0) > 0.001:
                failures.append(f"derive.segments 比例之和应为 1，实际 {actual}")
            continue
        if key == "free_bytes":
            if abs(int(actual) - 500 * 1024 ** 3) > 1024 ** 2:
                failures.append(f"derive.segments 未分配字节不对: {actual}")
            continue
        if key == "ordered":
            if list(actual) != [1, 2, 3]:
                failures.append(f"derive.segments 没按起始偏移/分区号排序: {actual}")
            continue
        if actual != expected:
            failures.append(f"derive.{key} 期望 {expected!r}，实际 {actual!r}")
    return failures


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("跳过：PATH 里没有 node")
        return 0

    failures: list[str] = []
    failures += check_syntax(node)
    failures += check_imports()
    failures += check_html_assets()

    tmp = Path(tempfile.mkdtemp(prefix="sisp-ui-check-"))
    try:
        failures += check_derive(node, tmp)
    finally:
        for leftover in tmp.iterdir():
            leftover.unlink()
        tmp.rmdir()

    if failures:
        print("前端检查失败：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"前端检查通过：{len(list(JS.glob('*.js')))} 个模块，资源引用与 derive.js 行为正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
