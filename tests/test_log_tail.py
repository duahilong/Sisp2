"""``app/gui_web/log_tail.py``：增量尾部读取必须与整文件 ``splitlines()`` 等价。

为什么值得单独测：这个模块替代的是 ``read_text(...).splitlines()[-limit:]``，
而调用方是**每 1.5 秒一次**的日志轮询。任何语义偏差都会直接体现在界面上
（少一行、多一个空行、中文乱码、总行数对不上），而这类偏差在真实写盘时
很难复现——所以用"和旧实现逐条比对"的方式钉住。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.gui_web.log_tail import LogTailReader


def _reference(path: Path) -> list[str]:
    """历史实现（``read_text().splitlines()``），作为等价性基准。"""
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "one\n",
        "one",                 # 末尾没有换行：splitlines() 也算一行
        "one\ntwo\nthree\n",
        "one\r\ntwo\r\n",      # 文本模式写盘的真实形态
        "a\n\nb\n",            # 空行
        "中文一行\n第二行\n",
        "trailing blank\n\n",
        "cr only\rlast",       # 只有 \r 也算换行
        "\r",                  # 单独 CR：splitlines() 返回一个空行
        "\r\r",                # 连续 CR：两个空行
        "a\n\r",               # 换行后的 CR：末尾还有一个空行
    ],
)
def test_matches_splitlines(tmp_path: Path, payload: str) -> None:
    log = tmp_path / "x.log"
    log.write_text(payload, encoding="utf-8", newline="")
    reader = LogTailReader()
    lines, total = reader.read(log, 5000)
    reference = _reference(log)
    assert lines == reference
    assert total == len(reference)


def test_tail_limit_returns_last_lines(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_text("\n".join(f"line {index}" for index in range(20)), encoding="utf-8")
    reader = LogTailReader()
    lines, total = reader.read(log, 3)
    assert total == 20
    assert lines == ["line 17", "line 18", "line 19"]


def test_total_counts_unterminated_last_line(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_text("a\nb", encoding="utf-8")
    reader = LogTailReader()
    assert reader.read(log, 10) == (["a", "b"], 2)


def test_incremental_append_stays_consistent(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_text("a\nb\nc\n", encoding="utf-8")
    reader = LogTailReader()
    assert reader.read(log, 10) == (["a", "b", "c"], 3)

    with log.open("a", encoding="utf-8") as handle:
        handle.write("d\ne\n")
    assert reader.read(log, 10) == (["a", "b", "c", "d", "e"], 5)

    # 只取尾部时也不该影响总量
    assert reader.read(log, 2) == (["d", "e"], 5)


def test_unterminated_line_grows_into_complete_line(tmp_path: Path) -> None:
    """写到一半的行：先当一行返回，补全后不重复计数。"""
    log = tmp_path / "x.log"
    log.write_text("a\npar", encoding="utf-8")
    reader = LogTailReader()
    assert reader.read(log, 10) == (["a", "par"], 2)

    with log.open("a", encoding="utf-8") as handle:
        handle.write("tial\n")
    assert reader.read(log, 10) == (["a", "partial"], 2)


def test_crlf_split_across_chunks(tmp_path: Path) -> None:
    """``\\r`` 与 ``\\n`` 落在两次读取之间时，不能多出一个空行。"""
    log = tmp_path / "x.log"
    log.write_bytes(b"a\r")
    reader = LogTailReader()
    assert reader.read(log, 10) == (["a"], 1)

    with log.open("ab") as handle:
        handle.write(b"\nb\r\n")
    assert reader.read(log, 10) == (["a", "b"], 2)


def test_pending_cr_empty_line_survives_next_chunk(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_bytes(b"\r")
    reader = LogTailReader()
    assert reader.read(log, 10) == ([""], 1)

    with log.open("ab") as handle:
        handle.write(b"\nnext")
    assert reader.read(log, 10) == _reference(log)
    assert reader.read(log, 10)[1] == 2


def test_truncated_file_is_recounted(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_text("a\nb\nc\n", encoding="utf-8")
    reader = LogTailReader()
    assert reader.read(log, 10) == (["a", "b", "c"], 3)

    log.write_text("x\n", encoding="utf-8")   # 变短（新文件覆盖旧文件）
    assert reader.read(log, 10) == (["x"], 1)


def test_partial_utf8_character_across_chunks(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    payload = "中文\n".encode()
    log.write_bytes(payload[:1])          # 一个多字节字符被切断
    reader = LogTailReader()
    assert reader.read(log, 10) == ([], 0)

    with log.open("ab") as handle:
        handle.write(payload[1:])
    assert reader.read(log, 10) == (["中文"], 1)


def test_missing_file_raises_oserror(tmp_path: Path) -> None:
    """接口层要靠这个异常返回 ``E_LOG``（等价于旧的 ``read_text`` 失败）。"""
    reader = LogTailReader()
    with pytest.raises(OSError):
        reader.read(tmp_path / "nope.log", 10)


def test_tracked_files_are_pruned(tmp_path: Path) -> None:
    reader = LogTailReader(max_files=2)
    for index in range(4):
        path = tmp_path / f"{index}.log"
        path.write_text("x\n", encoding="utf-8")
        reader.read(path, 10)
    assert len(reader._entries) == 2
