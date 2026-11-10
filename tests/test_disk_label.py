"""硬盘标签：``型号（磁盘 N）`` 的三种形态 + 文件名片段。

现场的问题是"哪块盘"：盘号会变、四块盘长得一样，日志里只写 ``磁盘 2`` 时
用户和客服都没法确认。这里守着四条规矩：

1. 有型号时**型号在前**，盘号放括号里消歧；
2. 没有型号（扫描失败、驱动给 ``None``、占位值）一律退化成 ``磁盘 N``，
   绝不出现 ``（磁盘 2）`` 前面空一截；
3. 文件名片段只留 ASCII，且不破坏 ``disk-runner-d2-*.log`` 这个既有约定；
4. 任何输入都不抛异常（日志/界面都在调它）。
"""

from __future__ import annotations

import pytest

from app.application.disk_label import (
    clean_model,
    disk_label,
    disk_slug,
    label_from_identity,
    model_of,
)


# ---------------------------------------------------------------- clean_model
@pytest.mark.parametrize(
    "value",
    ["", "   ", None, "None", "unknown", "Unknown", "未知型号", "-", "n/a", "无"],
)
def test_clean_model_treats_placeholders_as_empty(value) -> None:
    assert clean_model(value) == ""


def test_clean_model_collapses_whitespace_and_truncates() -> None:
    assert clean_model("  WDC\tWD10EZEX  \n  08WN4A0 ") == "WDC WD10EZEX 08WN4A0"
    assert len(clean_model("X" * 200)) == 60


# ---------------------------------------------------------------- disk_label
def test_label_puts_model_first_with_disk_number_in_brackets() -> None:
    assert disk_label(2, "Msft Virtual Disk") == "Msft Virtual Disk（磁盘 2）"


def test_label_short_style_keeps_number_first_for_ui_cards() -> None:
    assert disk_label(2, "Msft Virtual Disk", style="short") == "磁盘 2 · Msft Virtual Disk"


def test_label_name_style_is_model_only() -> None:
    assert disk_label(2, "GeIL P3A 2TB", style="name") == "GeIL P3A 2TB"


def test_label_without_model_falls_back_to_number_only() -> None:
    assert disk_label(2, None) == "磁盘 2"
    assert disk_label(2, "unknown") == "磁盘 2"


def test_label_without_model_can_show_size_instead() -> None:
    """没有型号时用容量兜底：``磁盘 2 · 120 GB`` 至少能区分盘。"""
    assert disk_label(2, "", size_display="120 GB") == "磁盘 2 · 120 GB"
    assert disk_label(2, "Msft Virtual Disk", size_display="120 GB") == "Msft Virtual Disk（磁盘 2）"


def test_label_tolerates_garbage_numbers() -> None:
    assert disk_label("x", "Model") == "Model（未知磁盘）"
    assert disk_label(None, "Model") == "Model（未知磁盘）"
    assert disk_label("x", None) == "未知磁盘"


def test_model_of_prefers_model_then_friendly_name() -> None:
    assert model_of({"model": "A", "friendly_name": "B"}) == "A"
    assert model_of({"model": "", "friendly_name": "B"}) == "B"
    assert model_of({"model": None}) == ""
    assert model_of(None) == ""


def test_label_from_identity_uses_disk_number_inside_identity() -> None:
    identity = {"disk_number": 4, "model": "GeIL P3A 2TB", "size_display": "1.82 TB"}
    assert label_from_identity(identity) == "GeIL P3A 2TB（磁盘 4）"
    # 显式盘号优先（身份里没有盘号的老 payload）
    assert label_from_identity({"model": "X"}, 7) == "X（磁盘 7）"


# ---------------------------------------------------------------- disk_slug
def test_disk_slug_is_ascii_and_filename_friendly() -> None:
    assert disk_slug("Msft Virtual Disk") == "msft-virtual-disk"
    assert disk_slug("WDC WD10EZEX-08WN4A0") == "wdc-wd10ezex-08wn4a0"
    assert disk_slug("多 盘") == ""
    assert disk_slug(None) == ""


def test_disk_slug_is_bounded() -> None:
    slug = disk_slug("A" * 80)
    assert len(slug) <= 24
    assert not slug.endswith("-")
