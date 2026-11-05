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
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "app" / "gui_web" / "ui"
JS = UI / "js"

_IMPORT_RE = re.compile(r"""from\s+['"]\./([\w.-]+\.js)['"]""")
_ATTR_RE = re.compile(r"""(?:href|src)="(/static/[^"]+)\"""")

DERIVE_SCRIPT = """
import { fmtSize, fsClass, segments, checkSizes, checkLayout, unallocatedBytes, partShortName, fitLayout, reserveGb, autoPartitionRole } from './derive.js';

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
  ok_sizes: checkSizes(1000 * GB, 120, 380),
  small_sys: checkSizes(1000 * GB, 20, 100) !== '',
  too_big: checkSizes(100 * GB, 120, 380) !== '',
  efi_reserved: checkLayout(100 * GB, 90, 0, [], 1024) !== '',
  efi_fits: checkSizes(100 * GB, 60, 39, 300),
  layout_no_extras: checkLayout(1000 * GB, 120, 380),
  layout_ok: checkLayout(1000 * GB, 120, 380, [{ sizeGb: 200 }]),
  layout_rest: unallocatedBytes(1000 * GB, 120, 380, [{ sizeGb: 200 }]) > 0,
  // 最后那个数据分区永远 auto：它的预估大小不参与容量校验，
  // 只有显式项（EFI/C/有 E 时的 D）会顶到容量，剩下的必须够它的下限
  layout_auto_last_data: checkLayout(160 * GB, 120, 100) === '',
  layout_auto_too_small: checkLayout(100 * GB, 120, 100) !== '',
  layout_auto_extra_small: checkLayout(100 * GB, 60, 39.5, [{ sizeGb: 1 }]) !== '',
  auto_role_data: autoPartitionRole([]),
  auto_role_extra: autoPartitionRole([{ sizeGb: 200 }]),
  // 快捷容量（100/120/150/200）必须把 E 一起重算：以前只算 C 和 D，
  // 于是 C=200 + D=275.9 + E=71.7 在 477 GB 的盘上超了一大截。
  // 并且剩下的容量要由 D / E 平分，不能全给 D。
  fit_preset: (() => {
    const fit = fitLayout(476.9, 200, [71.7], { even: true });
    return [fit.sysGb, fit.dataGb, fit.extrasGb];
  })(),
  fit_no_extra: (() => {
    const fit = fitLayout(476.9, 120, []);
    return [fit.sysGb, fit.dataGb];
  })(),
  // 预留余量 = EFI + 对齐，不再是固定 1 GB：“剩余”不应该白扔一整 GB
  reserve_default: reserveGb(),
  reserve_efi300: reserveGb(300),
  // 无论怎么点，C + D + E + 预留余量都不能超过盘容量
  fit_never_over: [
    [476.9, 200, [71.7]], [476.9, 100, [178]], [476.9, 150, []],
    [477, 60, [420]], [476.9, 200, [400]], [100, 60, []],
  ].every(([totalGb, sysGb, extrasGb]) => [false, true].every((even) => {
    const fit = fitLayout(totalGb, sysGb, extrasGb, { even });
    const used = fit.sysGb + fit.dataGb + reserveGb()
      + fit.extrasGb.reduce((sum, gb) => sum + gb, 0);
    return used <= totalGb + 1e-6;
  })),
  // 平分：D、E 的数字必须完全一样（零头留作未分配）
  fit_even_split: [
    [476.9, 200, [71.7]], [477, 60, [420]], [465.7, 200, [177.9]],
  ].every(([totalGb, sysGb, extrasGb]) => {
    const fit = fitLayout(totalGb, sysGb, extrasGb, { even: true });
    return fit.extrasGb.length > 0 && fit.extrasGb.every((gb) => gb === fit.dataGb);
  }),
  // 下限不能被 fitLayout 自己踩掉，否则会出现「20 GB 的系统分区」
  fit_keeps_mins: [
    [477, 60, [420]], [476.9, 200, [400]], [100, 60, []],
  ].every(([totalGb, sysGb, extrasGb]) => {
    const fit = fitLayout(totalGb, sysGb, extrasGb);
    return fit.sysGb >= 60 && fit.dataGb >= 20 && fit.extrasGb.every((gb) => gb >= 1);
  }),
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
    "ok_sizes": "",
    "small_sys": True,
    "too_big": True,
    "efi_reserved": True,
    "efi_fits": "",
    "layout_no_extras": "",
    "layout_ok": "",
    "layout_rest": True,
    "layout_auto_last_data": True,
    "layout_auto_too_small": True,
    "layout_auto_extra_small": True,
    "auto_role_data": "data",
    "auto_role_extra": "extra",
    "fit_preset": [200, 138.3, [138.3]],
    "fit_no_extra": [120, 356.6],
    "reserve_default": 0.3,
    "reserve_efi300": 0.49,
    "fit_never_over": True,
    "fit_even_split": True,
    "fit_keeps_mins": True,
}


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check_syntax(node: str) -> list[str]:
    """逐个模块做语法检查。

    **必须复制成 ``.mjs`` 再检查**：``node --check foo.js`` 对 ESM 语法是宽容
    的（解析失败就当作别的模式，返回码仍是 0），于是模板字符串里写了真实换行、
    漏了括号这类错误会被静默放过——浏览器里才炸。改成 ``.mjs`` 后 node 按模块
    解析，失败就返回非 0。
    """
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="sisp-ui-check-") as tmp:
        scratch = Path(tmp)
        for path in sorted(JS.glob("*.js")):
            target = scratch / (path.stem + ".mjs")
            shutil.copyfile(path, target)
            code, output = _run([node, "--check", str(target)], ROOT)
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
