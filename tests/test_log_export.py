"""日志导出：``summary.txt`` / ``summary.json`` / ``logs/*.log`` 打成 zip。

现场要求（原话）："如果出现错误可以方便的将日志导出成文件，排查错误"。
导出包必须做到：

1. **人先看**：``summary.txt`` 里每块盘用型号说话，失败步骤与原文都在；
2. **机器也能读**：``summary.json`` 带版本/时间/设置快照/每步详情；
3. **原始日志**：``logs/`` 里是对应的 ``disk-runner-d*.log``（运行中的任务
   还没有 ``log_path``，API 层会按盘号兜底找到最新那份）；
4. 只打包选中的任务；文件不存在/目录不可写不能把接口打挂。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from app.application.log_export import (
    ExportEntry,
    build_bundle,
    default_export_name,
)


def _job_dict(
    *,
    job_id: str = "disk2-abcd1234",
    disk: int = 2,
    status: str = "failed",
    model: str = "Msft Virtual Disk",
    log_path: str = "",
    steps: list[dict] | None = None,
) -> dict:
    return {
        "id": job_id,
        "disk_number": disk,
        "status": status,
        "plan": {
            "disk_number": disk,
            "label": f"{model}（磁盘 {disk}）",
            "model": model,
            "size_display": "120 GB",
            "partition_plan": [
                {"role": "efi", "label": "EFI", "size_text": "100 MB", "letter": "G"},
                {"role": "windows", "label": "Windows", "size_text": "60 GB", "letter": "H"},
            ],
        },
        "spec": {"expected_identity": {"disk_number": disk, "model": model, "serial_number": "SN-1"}},
        "result": {
            "duration_seconds": 52.8,
            "steps": steps
            if steps is not None
            else [
                {"key": "efi", "title": "硬盘分区", "status": "ok", "message": "已创建", "details": {}},
                {
                    "key": "layout",
                    "title": "分区验收",
                    "status": "failed",
                    "message": "分区验收不通过：第 2 个分区（Windows）大小不符",
                    "details": {
                        "diskpart_exit": 2147755027,
                        "diskpart_repairs": 2,
                        "settle_ms": 1000,
                        "parallel_activity": "GeIL P3A 2TB（磁盘 4）=写镜像（Ghost）",
                    },
                },
            ],
        },
        "error": "建分区失败（退出码 2147755027 = 0x80042413 所选卷或分区正在使用）",
        "started_at": "2026-11-09T17:16:29",
        "finished_at": "2026-11-09T17:17:22",
    }


# ---------------------------------------------------------------- 组装
def test_bundle_uses_disk_model_in_table_and_failure_reasons() -> None:
    bundle = build_bundle([_job_dict()], version="1.2.5", log_root=r"D:\logs")
    text = bundle.summary_text()

    assert "Sisp2 排查包" in text
    assert "1.2.5" in text
    # 表格里是型号，不是任务 id / disk2 这种内部编号
    assert "Msft Virtual Disk" in text
    assert "disk2-abcd1234" not in text
    # 失败原因段落带型号 + 失败步骤 + 原文 + 诊断
    assert "Msft Virtual Disk（磁盘 2）" in text
    assert "分区验收" in text
    assert "2147755027" in text
    assert "分区验收不通过：第 2 个分区（Windows）大小不符" in text
    assert "GeIL P3A 2TB（磁盘 4）=写镜像（Ghost）" in text


def test_bundle_summary_json_keeps_steps_and_settings() -> None:
    bundle = build_bundle(
        [_job_dict()], version="1.2.5", settings={"logDir": "D:/logs"}, log_root="D:/logs"
    )
    payload = json.loads(bundle.summary_json())

    assert payload["version"] == "1.2.5"
    assert payload["settings"] == {"logDir": "D:/logs"}
    entry = payload["entries"][0]
    assert entry["label"] == "Msft Virtual Disk（磁盘 2）"
    assert entry["disk_number"] == 2
    assert entry["duration_seconds"] == 52.8
    assert [step["key"] for step in entry["steps"]] == ["efi", "layout"]


def test_bundle_only_includes_selected_jobs() -> None:
    jobs = [
        _job_dict(job_id="disk2-aaaa", disk=2),
        _job_dict(job_id="disk3-bbbb", disk=3, model="GeIL P3A 2TB"),
    ]
    bundle = build_bundle(jobs, job_ids=["disk3-bbbb"], version="x")
    assert [entry.disk_number for entry in bundle.entries] == [3]


def test_bundle_without_jobs_explains_itself() -> None:
    text = build_bundle([], version="x").summary_text()
    assert "这一轮没有任务记录" in text


def test_bundle_sorts_entries_by_disk_number() -> None:
    bundle = build_bundle(
        [_job_dict(job_id="d7", disk=7), _job_dict(job_id="d2", disk=2)], version="x"
    )
    assert [entry.disk_number for entry in bundle.entries] == [2, 7]


# ---------------------------------------------------------------- 日志文件
def test_bundle_picks_up_log_files_and_skips_missing(tmp_path: Path) -> None:
    log = tmp_path / "disk-runner-d2-msft-virtual-disk-20261109-171629-4456-693b62.log"
    log.write_text("[17:16:29] 开始\n", encoding="utf-8")
    missing = tmp_path / "disk-runner-d3-gone.log"

    bundle = build_bundle(
        [_job_dict(), _job_dict(job_id="disk3-x", disk=3, log_path=str(missing))],
        log_paths={"disk2-abcd1234": str(log)},
        version="x",
    )
    assert [name for name, _path in bundle.logs] == [f"logs/{log.name}"]


def test_bundle_log_path_from_result_details(tmp_path: Path) -> None:
    log = tmp_path / "r.log"
    log.write_text("x", encoding="utf-8")
    steps = [
        {"key": "copy", "title": "收尾", "status": "ok", "message": "", "details": {"log_path": str(log)}}
    ]
    bundle = build_bundle([_job_dict(steps=steps)], version="x")
    assert bundle.entries[0].log_path == str(log)
    assert bundle.logs and bundle.logs[0][1] == log


# ---------------------------------------------------------------- 写盘
def test_write_produces_readable_zip(tmp_path: Path) -> None:
    log = tmp_path / "disk-runner-d2-x.log"
    log.write_text("[1] 日志\n", encoding="utf-8")
    artifact = tmp_path / "ghosterr-disk2-1.txt"
    artifact.write_text("GHOST 错误", encoding="utf-8")

    bundle = build_bundle(
        [_job_dict()],
        log_paths={"disk2-abcd1234": str(log)},
        artifacts=[("artifacts/ghosterr-disk2-1.txt", artifact)],
        settings={"logDir": "D:/logs"},
        version="1.2.5",
    )
    written = bundle.write(tmp_path / "out")  # 后缀会自动补 .zip
    target = Path(written["path"])
    assert target.name == "out.zip" and target.is_file()

    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert {"README.txt", "summary.txt", "summary.json", "settings.json"} <= names
        assert f"logs/{log.name}" in names
        assert "artifacts/ghosterr-disk2-1.txt" in names
        summary = archive.read("summary.txt").decode("utf-8")
        assert "Msft Virtual Disk" in summary
        # 文件名必须是 ASCII，中文内容在文件里
        assert all(name.isascii() for name in names)
    assert written["size"] > 0
    assert "summary.txt" in written["files"]


def test_default_export_name_is_stamped_and_suffixed(tmp_path: Path) -> None:
    name = default_export_name()
    assert name.startswith("Sisp2-日志-") and name.endswith(".zip")
    # 例：Sisp2-日志-20261109-1720.zip
    assert len(name) == len("Sisp2-日志-20261109-1720.zip")


# ---------------------------------------------------------------- 类型兼容
def test_bundle_accepts_objects_with_to_dict() -> None:
    class _Job:
        def to_dict(self):
            return _job_dict()

    bundle = build_bundle([_Job()], version="x")
    assert bundle.entries[0].label == "Msft Virtual Disk（磁盘 2）"


def test_export_entry_failed_steps_only() -> None:
    entry = ExportEntry(
        steps=[
            {"key": "a", "status": "ok"},
            {"key": "b", "status": "failed", "message": "boom"},
            {"key": "c", "status": "skipped"},
        ]
    )
    assert [step["key"] for step in entry.failed_steps()] == ["b"]
